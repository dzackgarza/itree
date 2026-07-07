"""Tests for validate_dag — forest-level structural invariant checks.

Proves that validate_dag correctly flags:
- Fragmented forest: more than one root
- Orphaned issues: issues not reachable from any root
"""

from __future__ import annotations

from itree.models import GithubIssue, IssueState, RepoDag, RepoRef
from itree.validate import validate_dag


def _open(number: int, title: str = "") -> GithubIssue:
    return GithubIssue(
        id=number,
        number=number,
        title=title or f"Issue #{number}",
        state=IssueState.open,
        html_url=f"https://github.com/t/t/issues/{number}",
    )


def _repo_ref() -> RepoRef:
    return RepoRef(owner="testowner", repo="testrepo")


def test_single_tree_no_violations() -> None:
    """A valid single-root tree produces no violations."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={1: _open(1), 2: _open(2)},
        children_of={1: (2,)},
    )
    violations = validate_dag(dag)
    assert violations == []


def test_fragmented_forest_detected() -> None:
    """Two roots produce a fragmented_forest violation."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={1: _open(1), 2: _open(2)},
        children_of={},
    )
    violations = validate_dag(dag)
    codes = [v.code for v in violations]
    assert "fragmented_forest" in codes


def test_fragmented_forest_message_lists_roots() -> None:
    """The fragmented_forest message includes root issue numbers."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={1: _open(1), 2: _open(2)},
        children_of={},
    )
    violations = validate_dag(dag)
    frag = [v for v in violations if v.code == "fragmented_forest"][0]
    assert "#1" in frag.message
    assert "#2" in frag.message


def test_orphaned_issue_detected() -> None:
    """An issue not reachable from any root produces an orphaned_issue violation."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={1: _open(1), 3: _open(3, title="Lost issue")},
        children_of={1: ()},
    )
    violations = validate_dag(dag)
    orphan_violations = [v for v in violations if v.code == "orphaned_issue"]
    assert len(orphan_violations) == 1
    assert orphan_violations[0].issue_number == 3


def test_both_violations() -> None:
    """A fragmented forest with orphans produces both violation types."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _open(1),
            2: _open(2),
            5: _open(5, title="Disconnected"),
        },
        children_of={},
    )
    violations = validate_dag(dag)
    codes = [v.code for v in violations]
    assert "fragmented_forest" in codes
    assert "orphaned_issue" in codes


def test_empty_repo_no_violations() -> None:
    """An empty repo (no issues) produces no violations."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={},
        children_of={},
    )
    violations = validate_dag(dag)
    assert violations == []


def test_three_roots_one_violation() -> None:
    """Three roots produce exactly one fragmented_forest violation with all three listed."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={1: _open(1), 2: _open(2), 3: _open(3)},
        children_of={},
    )
    violations = validate_dag(dag)
    frag = [v for v in violations if v.code == "fragmented_forest"]
    assert len(frag) == 1
    assert "3 roots" in frag[0].message


def test_cycle_detected() -> None:
    """A cycle in issue relations is detected and reported."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={1: _open(1), 2: _open(2)},
        children_of={1: (2,), 2: (1,)},
    )
    violations = validate_dag(dag)
    codes = [v.code for v in violations]
    assert "cycle_detected" in codes

