"""Tests for the doctor report generation and diagnostic engine."""

from __future__ import annotations

from itree.models import GithubIssue, IssueState, Milestone, RepoDag, RepoRef, ReportRef
from itree.validate import generate_doctor_report


def _repo_ref() -> RepoRef:
    return RepoRef(owner="testowner", repo="testrepo")


def _present_number(ref: ReportRef) -> int:
    assert ref.kind == "present"
    return ref.ref.number


def _issue(
    number: int,
    title: str = "",
    state: IssueState = IssueState.open,
    body: str | None = None,
    milestone: str | None = None,
    labels: tuple[str, ...] = (),
) -> GithubIssue:
    m = Milestone(title=milestone) if milestone else None
    return GithubIssue(
        id=number,
        number=number,
        title=title or f"Issue #{number}",
        state=state,
        html_url=f"https://github.com/t/t/issues/{number}",
        body=body,
        milestone=m,
        labels=labels,
    )


def _pull_request(number: int, title: str = "") -> GithubIssue:
    return GithubIssue(
        id=number,
        number=number,
        title=title or f"Pull Request #{number}",
        state=IssueState.open,
        html_url=f"https://github.com/t/t/pull/{number}",
        pull_request={"url": "pr_url"},
    )


def test_doctor_report_no_root() -> None:
    """ERROR E001 is triggered when no parentless issue exists at all."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={},
        children_of={},
    )
    report = generate_doctor_report(dag)
    assert report.status == "error"
    findings = [f for f in report.findings if f.code == "E001"]
    assert len(findings) == 1
    assert findings[0].title == "no_root"


def test_doctor_closed_parentless_issue_is_not_a_root_candidate() -> None:
    """Closed parentless issues are finished work, not traversal roots (no E002)."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Old finished thing", state=IssueState.closed),
        },
        children_of={},
    )
    report = generate_doctor_report(dag)
    assert _present_number(report.root) == 1
    assert all(f.code != "E002" for f in report.findings)


def test_doctor_report_root_not_ledger() -> None:
    """ERROR E004 is triggered when the unique root is not titled 'Ledger:'."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={1: _issue(1, "Task 1")},
        children_of={},
    )
    report = generate_doctor_report(dag)
    assert report.status == "error"
    findings = [f for f in report.findings if f.code == "E004"]
    assert len(findings) == 1
    assert findings[0].title == "root_not_ledger"
    assert all(f.code != "E001" for f in report.findings)


def test_doctor_report_multiple_root_ledgers() -> None:
    """ERROR E002 is triggered when multiple issues have the Ledger: prefix."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root 1"),
            2: _issue(2, "Ledger: Root 2"),
        },
        children_of={},
    )
    report = generate_doctor_report(dag)
    assert report.status == "error"
    findings = [f for f in report.findings if f.code == "E002"]
    assert len(findings) == 1
    assert len(findings[0].evidence) == 2


def test_doctor_report_unreachable_open_issues() -> None:
    """ERROR E010 is triggered when open issues are unreachable from the root ledger."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Unreachable Task"),
        },
        children_of={1: ()},
    )
    report = generate_doctor_report(dag)
    assert report.status == "error"
    findings = [f for f in report.findings if f.code == "E010"]
    assert len(findings) == 1
    assert "#2" in findings[0].evidence[0]


def test_doctor_report_parentless_non_root_issues() -> None:
    """ERROR E011 is triggered when open parentless issues exist beside the root ledger."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            3: _issue(3, "Accidental Root"),
        },
        children_of={1: ()},
    )
    report = generate_doctor_report(dag)
    assert report.status == "error"
    findings = [f for f in report.findings if f.code == "E011"]
    assert len(findings) == 1
    assert "#3" in findings[0].evidence[0]


def test_doctor_report_closed_parent_with_open_descendants() -> None:
    """ERROR E012 is triggered when a closed parent issue has open descendants."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Closed Parent", state=IssueState.closed),
            3: _issue(3, "Open Child"),
        },
        children_of={1: (2,), 2: (3,)},
    )
    report = generate_doctor_report(dag)
    assert report.status == "error"
    findings = [f for f in report.findings if f.code == "E012"]
    assert len(findings) == 1
    assert "#2" in findings[0].evidence[0]


def test_doctor_report_dependency_edges_present() -> None:
    """ERROR E014 is triggered when GitHub blocked/blocking relations exist."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Blocked Issue"),
            3: _issue(3, "Blocker"),
        },
        children_of={1: (2, 3)},
        dependencies={2: (3,)},
    )
    report = generate_doctor_report(dag)
    assert report.status == "error"
    findings = [f for f in report.findings if f.code == "E014"]
    assert len(findings) == 1
    assert "blocked by" in findings[0].evidence[0]


def test_doctor_report_depth_near_limit() -> None:
    """WARNING W020 is triggered when depth >= 7."""
    # Create chain of 8 issues: 1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7 -> 8 (depth 7)
    issues: dict[int, GithubIssue] = {1: _issue(1, "Ledger: Root")}
    children_of: dict[int, tuple[int, ...]] = {}
    for i in range(2, 9):
        issues[i] = _issue(i, f"Task {i}")
        children_of[i - 1] = (i,)
    children_of[8] = ()

    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues=issues,
        children_of=children_of,
    )
    report = generate_doctor_report(dag)
    findings = [f for f in report.findings if f.code == "W020"]
    assert len(findings) == 1


def test_doctor_accepts_single_issue_work_unit() -> None:
    """A work-unit issue does not need child task issues or a singleton marker."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Milestone: v1"),
            3: _issue(
                3,
                "Preview sync work unit",
                body="## Acceptance Criteria\n- Preview updates after source edits.",
            ),
        },
        children_of={1: (2,), 2: (3,)},
    )
    report = generate_doctor_report(dag)
    assert _present_number(report.next_issue) == 3
    assert all(f.code != "W030" for f in report.findings)


def test_doctor_rejects_work_unit_decomposed_into_child_issues() -> None:
    """A non-organizational issue is the PR-sized work unit, not a parent for task issues."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(
                2,
                "Preview sync work unit",
                body="## Acceptance Criteria\n- Preview updates after source edits.",
            ),
            3: _issue(
                3,
                "Add event wiring",
                body="## Acceptance Criteria\n- Event wiring is proven through the preview boundary.",
            ),
        },
        children_of={1: (2,), 2: (3,)},
    )
    report = generate_doctor_report(dag)
    findings = [f for f in report.findings if f.code == "E015"]
    assert len(findings) == 1
    assert "work unit #2 has child issues: #3" in findings[0].evidence
    assert _present_number(report.next_issue) == 2


def test_doctor_does_not_require_singleton_marker() -> None:
    """Legacy singleton markers are not part of the work-unit model."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Milestone: v1"),
            3: _issue(
                3,
                "Standalone migration work unit",
                body="## Acceptance Criteria\n- Migration proof passes at the repository boundary.",
            ),
        },
        children_of={1: (2,), 2: (3,)},
    )
    report = generate_doctor_report(dag)
    assert all(f.code != "W030" for f in report.findings)


def test_doctor_report_dead_open_grouping() -> None:
    """WARNING W030 is triggered for an open grouping issue with no open descendants."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Milestone: stale"),
            3: _issue(3, "Finished work", state=IssueState.closed),
            4: _issue(
                4,
                "Live work unit",
                body="## Acceptance Criteria\n- Proven at the boundary.",
            ),
        },
        children_of={1: (2, 4), 2: (3,)},
    )
    report = generate_doctor_report(dag)
    findings = [f for f in report.findings if f.code == "W030"]
    assert len(findings) == 1
    assert "#2" in findings[0].evidence[0]


def test_doctor_report_root_never_flagged_dead_grouping() -> None:
    """The root ledger of a fully closed tree is DONE, not a W030 violation."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Old work", state=IssueState.closed),
        },
        children_of={1: (2,)},
    )
    report = generate_doctor_report(dag)
    assert all(f.code != "W030" for f in report.findings)


def test_doctor_deferred_grouping_suppresses_w030() -> None:
    """A grouping labeled 'deferred' is an intentional long-horizon shelf, not a dead one."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Milestone: far future", labels=("deferred",)),
            3: _issue(
                3,
                "Live work unit",
                body="## Acceptance Criteria\n- Proven at the boundary.",
            ),
        },
        children_of={1: (2, 3), 2: ()},
    )
    report = generate_doctor_report(dag)
    assert all(f.code != "W030" for f in report.findings)


def test_doctor_deferred_grouping_surfaces_info_finding() -> None:
    """Deferred shelves stay visible via a non-warning I010 line that names them."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Milestone: far future", labels=("deferred",)),
            3: _issue(
                3,
                "Live work unit",
                body="## Acceptance Criteria\n- Proven at the boundary.",
            ),
        },
        children_of={1: (2, 3), 2: ()},
    )
    report = generate_doctor_report(dag)
    info = [f for f in report.findings if f.code == "I010"]
    assert len(info) == 1
    assert "#2" in info[0].evidence[0]
    assert info[0].severity == "info"
    # An info-only deferral must not push the repo out of OK status.
    assert report.status == "ok"


def test_doctor_untagged_empty_grouping_still_warns_alongside_deferred() -> None:
    """Only the tagged grouping is spared; an untagged empty shelf still warns W030."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Milestone: far future", labels=("deferred",)),
            3: _issue(3, "Milestone: stale shelf"),
            4: _issue(
                4,
                "Live work unit",
                body="## Acceptance Criteria\n- Proven at the boundary.",
            ),
        },
        children_of={1: (2, 3, 4), 2: (), 3: ()},
    )
    report = generate_doctor_report(dag)
    w030 = [f for f in report.findings if f.code == "W030"]
    assert len(w030) == 1
    assert "#3" in w030[0].evidence[0]
    assert all("#2" not in e for e in w030[0].evidence)


def test_doctor_deferral_label_is_configurable() -> None:
    """The sanctioned label name is a knob; a custom label suppresses W030 too."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Milestone: far future", labels=("long-horizon",)),
            3: _issue(
                3,
                "Live work unit",
                body="## Acceptance Criteria\n- Proven at the boundary.",
            ),
        },
        children_of={1: (2, 3), 2: ()},
    )
    report = generate_doctor_report(dag, deferral_label="long-horizon")
    assert all(f.code != "W030" for f in report.findings)


def test_doctor_report_duplicate_reachable_issue() -> None:
    """ERROR E013 is triggered when an issue has multiple parents under the root."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Milestone: a"),
            3: _issue(3, "Milestone: b"),
            4: _issue(
                4,
                "Shared child",
                body="## Acceptance Criteria\n- Proven.",
            ),
        },
        children_of={1: (2, 3), 2: (4,), 3: (4,)},
    )
    report = generate_doctor_report(dag)
    findings = [f for f in report.findings if f.code == "E013"]
    assert len(findings) == 1
    assert "#4" in findings[0].evidence[0]


def test_doctor_report_has_no_enclosing_work_unit_field() -> None:
    """The fictional next-vs-enclosing-work-unit distinction is gone from the report."""
    from itree.models import DoctorReport

    assert "enclosing_work_unit" not in DoctorReport.model_fields


def test_doctor_report_milestone_mismatch() -> None:
    """WARNING W040 is triggered when issues under milestone ledger disagree with GitHub milestone."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Milestone: v1.0"),
            3: _issue(3, "Work unit"),
            4: _issue(4, "Mismatched Task", milestone="v2.0"),
        },
        children_of={1: (2,), 2: (3,), 3: (4,)},
    )
    report = generate_doctor_report(dag)
    findings = [f for f in report.findings if f.code == "W040"]
    assert len(findings) == 1
    assert any("#4" in ev for ev in findings[0].evidence)


def test_doctor_report_milestone_without_ledger() -> None:
    """WARNING W041 is triggered when active milestone has issues but no milestone ledger child."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Task with Milestone", milestone="v1.0"),
        },
        children_of={1: ()},
    )
    report = generate_doctor_report(dag)
    findings = [f for f in report.findings if f.code == "W041"]
    assert len(findings) == 1
    assert "v1.0" in findings[0].evidence[0]


def test_doctor_accepts_pr_linked_to_work_unit_issue() -> None:
    """A PR may close the work-unit issue returned by traversal."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(
                2,
                "Preview sync work unit",
                body="## Acceptance Criteria\n- Preview updates after source edits.",
            ),
            3: _pull_request(3, "Pull Request"),
        },
        children_of={1: (2,)},
    )
    report = generate_doctor_report(dag)
    assert _present_number(report.next_issue) == 2
    assert all(f.code != "W032" for f in report.findings)


def test_doctor_ignores_pull_requests_when_finding_repository_root() -> None:
    """Parentless PRs are not issue-tree roots or unreachable work."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            36: _pull_request(36, "Open PR: unrelated branch"),
            43: _issue(43, "Ledger: Project roadmap"),
            54: _issue(
                54,
                "Standard editor semantics",
                body="## Acceptance Criteria\n- The editor follows standard desktop behavior.",
            ),
            103: _pull_request(103, "Open PR: proof cleanup"),
        },
        children_of={43: (54,)},
    )

    report = generate_doctor_report(dag)

    assert _present_number(report.root) == 43
    assert _present_number(report.next_issue) == 54
    assert all(f.code != "E002" for f in report.findings)
    assert all(f.code != "E010" for f in report.findings)
    assert all(f.code != "E011" for f in report.findings)


def test_doctor_cyclic_children_reports_e003_without_crashing() -> None:
    """Regression (#15): a children_of cycle yields E003 and a usable report, not RecursionError."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Milestone: v1"),
            3: _issue(3, "Task in cycle", body="## Acceptance Criteria\n- ok"),
        },
        children_of={1: (2,), 2: (3,), 3: (2,)},
    )
    report = generate_doctor_report(dag)
    assert report.status == "error"
    findings = [f for f in report.findings if f.code == "E003"]
    assert len(findings) == 1
    assert any("#2" in ev and "#3" in ev for ev in findings[0].evidence)


def test_doctor_report_missing_acceptance_criteria() -> None:
    """WARNING W050 is triggered when a work-unit issue lacks acceptance criteria."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Work Unit", body="Just some comments, no requirements here"),
        },
        children_of={1: (2,)},
    )
    report = generate_doctor_report(dag)
    findings = [f for f in report.findings if f.code == "W050"]
    assert len(findings) == 1
    assert "#2" in findings[0].evidence[0]
