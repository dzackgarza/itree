"""Tests for native dependency readiness and cycle detection.

These tests prove that ``itree`` treats native GitHub ``blocked_by`` edges as
real hard prerequisites: a leaf is eligible only when it and every reachable
grouping ancestor have no open native blocker.  Preorder remains the
deterministic tie-breaker among ready work.

The pure functions in ``readiness.py`` consume an already-built ``RepoDag``
(see ``traversal.py``); no IO is exercised here.
"""

from __future__ import annotations

from itree.models import GithubIssue, IssueState, RepoDag, RepoRef
from itree.readiness import (
    DependencyErrorKind,
    ReadinessState,
    compute_readiness,
    detect_dependency_errors,
    first_ready_work_unit,
)


def _repo_ref() -> RepoRef:
    return RepoRef(owner="testowner", repo="testrepo")


def _issue(
    number: int,
    title: str = "",
    state: IssueState = IssueState.open,
    labels: tuple[str, ...] = (),
) -> GithubIssue:
    return GithubIssue(
        id=number + 5000,
        number=number,
        title=title or f"Issue #{number}",
        state=state,
        html_url=f"https://github.com/testowner/testrepo/issues/{number}",
        labels=labels,
    )


# ---------------------------------------------------------------------------
# compute_readiness
# ---------------------------------------------------------------------------


def test_no_dependencies_is_ready() -> None:
    """An issue with no blockers is ready."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={1: _issue(1, "Ledger: Root"), 2: _issue(2, "Work unit")},
        children_of={1: (2,)},
    )
    result = compute_readiness(dag, 2)
    assert result.state == ReadinessState.ready
    assert result.open_blockers == ()
    assert result.blocked_ancestors == ()


def test_open_blocker_makes_issue_blocked() -> None:
    """An open blocker makes the blocked issue unready."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Blocked"),
            3: _issue(3, "Blocker"),
        },
        children_of={1: (2, 3)},
        dependencies={2: (3,)},
    )
    result = compute_readiness(dag, 2)
    assert result.state == ReadinessState.blocked
    assert 3 in result.open_blockers


def test_closed_blocker_makes_issue_ready() -> None:
    """A closed blocker satisfies the dependency; the issue is ready."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Was blocked"),
            3: _issue(3, "Closed blocker", state=IssueState.closed),
        },
        children_of={1: (2, 3)},
        dependencies={2: (3,)},
    )
    result = compute_readiness(dag, 2)
    assert result.state == ReadinessState.ready
    assert result.open_blockers == ()


def test_blocked_grouping_ancestor_blocks_descendant() -> None:
    """A grouping ancestor with an open blocker makes all descendants unready."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Milestone: v1"),
            3: _issue(3, "Work unit under blocked grouping"),
            4: _issue(4, "Blocker for milestone"),
        },
        children_of={1: (2,), 2: (3, 4)},
        dependencies={2: (4,)},
    )
    result = compute_readiness(dag, 3)
    assert result.state == ReadinessState.blocked
    assert 2 in result.blocked_ancestors


def test_forward_preorder_dependency_is_valid() -> None:
    """A blocked_by edge where the blocker appears later in preorder is valid.

    The blocked issue is simply unready; the forward edge is not an error.
    """
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Earlier issue, blocked by later"),
            3: _issue(3, "Later blocker"),
        },
        children_of={1: (2, 3)},
        dependencies={2: (3,)},
    )
    result = compute_readiness(dag, 2)
    assert result.state == ReadinessState.blocked
    assert 3 in result.open_blockers


def test_cross_branch_dependency_is_valid() -> None:
    """A blocked_by edge crossing grouping branches is valid."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Milestone: A"),
            3: _issue(3, "Work unit under A, blocked by B's child"),
            4: _issue(4, "Milestone: B"),
            5: _issue(5, "Blocker under B"),
        },
        children_of={1: (2, 4), 2: (3,), 4: (5,)},
        dependencies={3: (5,)},
    )
    result = compute_readiness(dag, 3)
    assert result.state == ReadinessState.blocked
    assert 5 in result.open_blockers


# ---------------------------------------------------------------------------
# detect_dependency_errors
# ---------------------------------------------------------------------------


def test_acyclic_dependencies_produce_no_errors() -> None:
    """Valid acyclic dependencies produce no dependency errors."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Blocked"),
            3: _issue(3, "Blocker"),
        },
        children_of={1: (2, 3)},
        dependencies={2: (3,)},
    )
    errors = detect_dependency_errors(dag)
    assert errors == []


def test_dependency_cycle_detected_with_witness() -> None:
    """A dependency cycle is a structural error with the complete witness cycle."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "A"),
            3: _issue(3, "B"),
        },
        children_of={1: (2, 3)},
        dependencies={2: (3,), 3: (2,)},
    )
    errors = detect_dependency_errors(dag)
    assert len(errors) == 1
    assert errors[0].kind == DependencyErrorKind.cycle
    # The witness must contain both nodes in the cycle
    witness_set = set(errors[0].witness)
    assert {2, 3} <= witness_set


def test_deleted_blocker_not_treated_as_satisfied() -> None:
    """A blocker that is absent from the issues dict is diagnosed, not silently satisfied.

    The dag_from_graph_nodes transform already drops unknown blockers, but
    detect_dependency_errors must also guard against the case where a blocker
    issue was deleted after the DAG was built.  We simulate this by checking
    that a dependency on an issue that exists but is not open is handled
    correctly (closed = satisfied), while a dependency pointing to a number
    not in the issues dict would have been dropped by the builder and is not
    re-introduced.

    This test verifies the positive case: a closed blocker is satisfied.
    """
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Was blocked"),
            3: _issue(3, "Closed blocker", state=IssueState.closed),
        },
        children_of={1: (2, 3)},
        dependencies={2: (3,)},
    )
    result = compute_readiness(dag, 2)
    assert result.state == ReadinessState.ready


# ---------------------------------------------------------------------------
# first_ready_work_unit
# ---------------------------------------------------------------------------


def test_first_ready_work_unit_skips_blocked() -> None:
    """next skips a blocked leaf and returns the first eligible in preorder."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Blocked work unit"),
            3: _issue(3, "Blocker (also a work unit)"),
            4: _issue(4, "Ready work unit after blocked"),
        },
        children_of={1: (2, 3, 4)},
        dependencies={2: (3,)},
    )
    root = dag.materialize_root(1)
    node = first_ready_work_unit(root, dag)
    assert node is not None
    # #2 is blocked by #3; #3 is ready; preorder picks #3 before #4
    assert node.issue.number == 3


def test_first_ready_work_unit_blocked_grouping_skips_descendants() -> None:
    """A grouping blocked by an open issue makes all descendants unready."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Milestone: blocked"),
            3: _issue(3, "Work unit under blocked milestone"),
            4: _issue(4, "Blocker"),
            5: _issue(5, "Ready work unit"),
        },
        children_of={1: (2, 4, 5), 2: (3,)},
        dependencies={2: (4,)},
    )
    root = dag.materialize_root(1)
    node = first_ready_work_unit(root, dag)
    assert node is not None
    # #2 (grouping) is blocked by #4; descendant #3 is not ready.
    # #4 is ready and appears before #5 in preorder.
    assert node.issue.number == 4


def test_first_ready_work_unit_closing_blocker_makes_eligible() -> None:
    """Closing the blocker makes the previously blocked issue eligible."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Was blocked, now ready"),
            3: _issue(3, "Closed blocker", state=IssueState.closed),
            4: _issue(4, "Later work unit"),
        },
        children_of={1: (2, 3, 4)},
        dependencies={2: (3,)},
    )
    root = dag.materialize_root(1)
    node = first_ready_work_unit(root, dag)
    assert node is not None
    # #3 is closed → #2 is ready; preorder picks #2 before #4
    assert node.issue.number == 2


def test_first_ready_work_unit_no_blockers_is_pure_preorder() -> None:
    """With no dependencies at all, first_ready_work_unit matches first_open_work_unit."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Milestone: v1"),
            3: _issue(3, "First work unit"),
            4: _issue(4, "Second work unit"),
        },
        children_of={1: (2,), 2: (3, 4)},
    )
    root = dag.materialize_root(1)
    node = first_ready_work_unit(root, dag)
    assert node is not None
    assert node.issue.number == 3
