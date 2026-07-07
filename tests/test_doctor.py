"""Tests for the doctor report generation and diagnostic engine."""

from __future__ import annotations

from itree.models import GithubIssue, IssueState, Milestone, RepoDag, RepoRef
from itree.validate import generate_doctor_report


def _repo_ref() -> RepoRef:
    return RepoRef(owner="testowner", repo="testrepo")


def _issue(
    number: int,
    title: str = "",
    state: IssueState = IssueState.open,
    body: str | None = None,
    milestone: str | None = None,
    is_pr: bool = False,
) -> GithubIssue:
    m = Milestone(title=milestone) if milestone else None
    pr = {"url": "pr_url"} if is_pr else None
    return GithubIssue(
        id=number,
        number=number,
        title=title or f"Issue #{number}",
        state=state,
        html_url=f"https://github.com/t/t/issues/{number}",
        body=body,
        milestone=m,
        pull_request=pr,
    )


def test_doctor_report_no_root_ledger() -> None:
    """ERROR E001 is triggered when no root ledger is declared."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={1: _issue(1, "Task 1")},
        children_of={},
    )
    report = generate_doctor_report(dag)
    assert report.status == "error"
    findings = [f for f in report.findings if f.code == "E001"]
    assert len(findings) == 1
    assert "no_root_ledger" in findings[0].title


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
    issues = {1: _issue(1, "Ledger: Root")}
    children_of = {}
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
    assert report.next_issue is not None
    assert report.next_issue.number == 3
    assert report.enclosing_work_unit is not None
    assert report.enclosing_work_unit.number == 3
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
    assert report.next_issue is not None
    assert report.next_issue.number == 2


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
            3: _issue(3, "Pull Request", is_pr=True, body="Closes #2"),
        },
        children_of={1: (2,)},
    )
    report = generate_doctor_report(dag)
    assert report.next_issue is not None
    assert report.next_issue.number == 2
    assert all(f.code != "W032" for f in report.findings)


def test_doctor_ignores_pull_requests_when_finding_repository_root() -> None:
    """Parentless PRs are not issue-tree roots or unreachable work."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            36: _issue(36, "Open PR: unrelated branch", is_pr=True),
            43: _issue(43, "Ledger: Project roadmap"),
            54: _issue(
                54,
                "Standard editor semantics",
                body="## Acceptance Criteria\n- The editor follows standard desktop behavior.",
            ),
            103: _issue(103, "Open PR: proof cleanup", is_pr=True),
        },
        children_of={43: (54,)},
    )

    report = generate_doctor_report(dag)

    assert report.root is not None
    assert report.root.number == 43
    assert report.next_issue is not None
    assert report.next_issue.number == 54
    assert all(f.code != "E002" for f in report.findings)
    assert all(f.code != "E010" for f in report.findings)
    assert all(f.code != "E011" for f in report.findings)


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
