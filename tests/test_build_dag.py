"""Tests for build_dag — specifically the closed-children filtering logic.

Proves that when list_subissues returns closed children that are absent from
list_all_issues (GitHub's default open-only behavior), build_dag silently
excludes them rather than crashing with a KeyError.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from tools.itree.github import GithubApi
from tools.itree.models import GithubIssue, IssueState, RepoRef
from tools.itree.traversal import build_dag


def _open_issue(number: int, title: str = "") -> GithubIssue:
    return GithubIssue(
        id=number,
        number=number,
        title=title or f"Issue #{number}",
        state=IssueState.open,
        html_url=f"https://github.com/testowner/testrepo/issues/{number}",
    )


def _closed_issue(number: int, title: str = "") -> GithubIssue:
    return GithubIssue(
        id=number + 1000,
        number=number,
        title=title or f"Issue #{number}",
        state=IssueState.closed,
        html_url=f"https://github.com/testowner/testrepo/issues/{number}",
    )


def _make_api(
    all_issues: tuple[GithubIssue, ...],
    subissues: dict[int, tuple[GithubIssue, ...]] | None = None,
) -> GithubApi:
    """Build a GithubApi whose methods return the given fixtures."""
    api = MagicMock(spec=GithubApi)
    api.list_all_issues.return_value = all_issues
    api.list_subissues.side_effect = lambda n: (subissues or {}).get(n, ())
    return api


def test_closed_child_filtered_out() -> None:
    """build_dag excludes children returned by list_subissues but absent from list_all_issues."""
    open_only = (_open_issue(1), _open_issue(2))
    # Issue #3 is closed — returned by list_subissues but NOT by list_all_issues.
    subissues = {
        1: (_open_issue(2), _closed_issue(3, title="Already done")),
    }

    api = _make_api(open_only, subissues)
    repo_ref = RepoRef(owner="testowner", repo="testrepo")
    dag = build_dag(repo_ref, api=api)  # type: ignore[arg-type]

    # #3 must not appear in the adjacency list.
    assert 3 not in dag.issues
    assert 3 not in dag.parent_of
    # #1's children should contain only #2.
    assert dag.children_of[1] == (2,)
    # Materialize must not raise KeyError.
    tree = dag.materialize_root(1)
    numbers = [n.issue.number for n in tree.preorder()]
    assert numbers == [1, 2]


def test_all_children_closed_parent_becomes_leaf() -> None:
    """When all children are closed, parent has no children in the DAG."""
    open_only = (_open_issue(1),)
    subissues = {
        1: (_closed_issue(2), _closed_issue(3)),
    }

    api = _make_api(open_only, subissues)
    repo_ref = RepoRef(owner="testowner", repo="testrepo")
    dag = build_dag(repo_ref, api=api)  # type: ignore[arg-type]

    assert dag.children_of.get(1, ()) == ()
    tree = dag.materialize_root(1)
    assert tree.children == ()
    assert tree.issue.number == 1


def test_mixed_open_and_closed_children() -> None:
    """Only open children survive filtering; closed ones are silently dropped."""
    open_only = (_open_issue(1), _open_issue(2), _open_issue(4))
    subissues = {
        1: (_open_issue(2), _closed_issue(3), _open_issue(4)),
    }

    api = _make_api(open_only, subissues)
    repo_ref = RepoRef(owner="testowner", repo="testrepo")
    dag = build_dag(repo_ref, api=api)  # type: ignore[arg-type]

    assert dag.children_of[1] == (2, 4)
    assert 3 not in dag.issues
    tree = dag.materialize_root(1)
    numbers = [n.issue.number for n in tree.preorder()]
    assert numbers == [1, 2, 4]


def test_no_crash_when_child_number_not_in_issues() -> None:
    """The exact scenario that caused KeyError on issue #152.

    Parent issue has a closed child (#152) returned by list_subissues.
    list_all_issues does not return #152. build_dag must not crash.
    """
    open_only = (_open_issue(100), _open_issue(151))
    subissues = {
        100: (_open_issue(151), _closed_issue(152, title="Already closed")),
    }

    api = _make_api(open_only, subissues)
    repo_ref = RepoRef(owner="testowner", repo="testrepo")
    dag = build_dag(repo_ref, api=api)  # type: ignore[arg-type]

    assert 152 not in dag.issues
    assert dag.children_of[100] == (151,)
    tree = dag.materialize_root(100)
    assert tree.issue.number == 100
    assert len(tree.children) == 1
    assert tree.children[0].issue.number == 151
