"""Tests for the doctor report generation and diagnostic engine."""

from __future__ import annotations

import pytest
from itree.models import GithubIssue, IssueState, RepoDag, RepoRef, Milestone
from itree.validate import generate_doctor_report, lacks_acceptance_criteria, is_singleton_justified


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
    """ERROR E002 is triggered when multiple issues have the root body marker."""
    body_marker = "<!-- itree:role=root-ledger schema=1 -->"
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Root 1", body=body_marker),
            2: _issue(2, "Root 2", body=body_marker),
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
    body_marker = "<!-- itree:role=root-ledger schema=1 -->"
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Root", body=body_marker),
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
    body_marker = "<!-- itree:role=root-ledger schema=1 -->"
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Root", body=body_marker),
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
    body_marker = "<!-- itree:role=root-ledger schema=1 -->"
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Root", body=body_marker),
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
    body_marker = "<!-- itree:role=root-ledger schema=1 -->"
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Root", body=body_marker),
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
    body_marker = "<!-- itree:role=root-ledger schema=1 -->"
    # Create chain of 8 issues: 1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7 -> 8 (depth 7)
    issues = {1: _issue(1, "Root", body=body_marker)}
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


def test_doctor_report_singleton_work_unit() -> None:
    """WARNING W030 is triggered when a work unit has only one task and no justification."""
    body_marker = "<!-- itree:role=root-ledger schema=1 -->"
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Root", body=body_marker),
            2: _issue(2, "Milestone: v1"),
            3: _issue(3, "Singleton Work Unit"),
            4: _issue(4, "Single task", body="No justification here"),
        },
        children_of={1: (2,), 2: (3,), 3: (4,)},
    )
    report = generate_doctor_report(dag)
    findings = [f for f in report.findings if f.code == "W030"]
    assert len(findings) == 1
    assert "#3" in findings[0].evidence[0]


def test_doctor_report_singleton_work_unit_justified() -> None:
    """WARNING W030 is NOT triggered when a singleton work unit has a valid marker."""
    body_marker = "<!-- itree:role=root-ledger schema=1 -->"
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Root", body=body_marker),
            2: _issue(2, "Milestone: v1"),
            3: _issue(3, "Singleton Work Unit", body="itree:role=singleton"),
            4: _issue(4, "Single task"),
        },
        children_of={1: (2,), 2: (3,), 3: (4,)},
    )
    report = generate_doctor_report(dag)
    findings = [f for f in report.findings if f.code == "W030"]
    assert len(findings) == 0


def test_doctor_report_milestone_mismatch() -> None:
    """WARNING W040 is triggered when issues under milestone ledger disagree with GitHub milestone."""
    body_marker = "<!-- itree:role=root-ledger schema=1 -->"
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Root", body=body_marker),
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
    body_marker = "<!-- itree:role=root-ledger schema=1 -->"
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Root", body=body_marker),
            2: _issue(2, "Task with Milestone", milestone="v1.0"),
        },
        children_of={1: ()},
    )
    report = generate_doctor_report(dag)
    findings = [f for f in report.findings if f.code == "W041"]
    assert len(findings) == 1
    assert "v1.0" in findings[0].evidence[0]


def test_doctor_report_leaf_has_pr() -> None:
    """WARNING W032 is triggered when a leaf task has a linked PR."""
    body_marker = "<!-- itree:role=root-ledger schema=1 -->"
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Root", body=body_marker),
            2: _issue(2, "Work Unit"),
            3: _issue(3, "Leaf Task"),
            4: _issue(4, "Pull Request", is_pr=True, body="Closes #3"),
        },
        children_of={1: (2,), 2: (3,)},
    )
    report = generate_doctor_report(dag)
    findings = [f for f in report.findings if f.code == "W032"]
    assert len(findings) == 1
    assert "#3" in findings[0].evidence[0]


def test_doctor_report_missing_acceptance_criteria() -> None:
    """WARNING W050 is triggered when a leaf task lacks acceptance criteria."""
    body_marker = "<!-- itree:role=root-ledger schema=1 -->"
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Root", body=body_marker),
            2: _issue(2, "Work Unit"),
            3: _issue(3, "Leaf Task", body="Just some comments, no requirements here"),
        },
        children_of={1: (2,), 2: (3,)},
    )
    report = generate_doctor_report(dag)
    findings = [f for f in report.findings if f.code == "W050"]
    assert len(findings) == 1
    assert "#3" in findings[0].evidence[0]
