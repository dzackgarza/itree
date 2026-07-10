"""Tests for build_dag over the GraphQL repo-graph fetch.

build_dag consumes fetch_repo_graph node dicts: every issue (open and
closed) becomes a DAG node, sub-issue edge order is sibling priority
order, truncated sub-issue pages fall back to the REST endpoint, and
blocked-by edges land in dependencies.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

from itree.github import GithubApi
from itree.models import GithubIssue, IssueState, RepoRef
from itree.traversal import build_dag


def _node(
    number: int,
    title: str = "",
    state: str = "OPEN",
    children: tuple[int, ...] = (),
    blocked_by: tuple[int, ...] = (),
    total_children: int | None = None,
    labels: tuple[str, ...] = (),
) -> dict:
    return {
        "number": number,
        "databaseId": number + 5000,
        "title": title or f"Issue #{number}",
        "state": state,
        "stateReason": "COMPLETED" if state == "CLOSED" else None,
        "body": None,
        "url": f"https://github.com/testowner/testrepo/issues/{number}",
        "milestone": None,
        "labels": {"nodes": [{"name": name} for name in labels]},
        "subIssues": {
            "totalCount": total_children if total_children is not None else len(children),
            "nodes": [{"number": child} for child in children],
        },
        "blockedBy": {"nodes": [{"number": blocker} for blocker in blocked_by]},
    }


def _make_api(nodes: tuple[dict, ...], rest_subissues: dict[int, tuple[GithubIssue, ...]] | None = None) -> MagicMock:
    api = MagicMock(spec=GithubApi)
    api.fetch_repo_graph.return_value = nodes
    rest_map = rest_subissues or {}
    api.list_subissues.side_effect = lambda n: rest_map[n]
    return api


def _repo_ref() -> RepoRef:
    return RepoRef(owner="testowner", repo="testrepo")


def test_open_and_closed_issues_both_in_dag() -> None:
    """Closed issues are DAG nodes so closed parents of open work are observable."""
    nodes = (
        _node(1, "Ledger: Root", children=(2,)),
        _node(2, "Closed parent", state="CLOSED", children=(3,)),
        _node(3, "Open child"),
    )
    dag = build_dag(_repo_ref(), api=cast(GithubApi, _make_api(nodes)))

    assert set(dag.issues) == {1, 2, 3}
    assert dag.issues[2].state == IssueState.closed
    assert dag.children_of[2] == (3,)
    tree = dag.materialize_root(1)
    assert [n.issue.number for n in tree.preorder()] == [1, 2, 3]


def test_children_order_is_subissue_order() -> None:
    """Sub-issue edge order defines sibling priority order, verbatim."""
    nodes = (
        _node(1, "Ledger: Root", children=(9, 4, 7)),
        _node(9),
        _node(4),
        _node(7),
    )
    dag = build_dag(_repo_ref(), api=cast(GithubApi, _make_api(nodes)))
    assert dag.children_of[1] == (9, 4, 7)


def test_child_absent_from_repo_graph_is_dropped() -> None:
    """An edge to an issue absent from the fetched graph (e.g. transferred out) is dropped."""
    nodes = (_node(1, "Ledger: Root", children=(2, 999)), _node(2))
    dag = build_dag(_repo_ref(), api=cast(GithubApi, _make_api(nodes)))
    assert dag.children_of[1] == (2,)
    assert 999 not in dag.issues


def test_truncated_subissue_page_falls_back_to_rest() -> None:
    """totalCount above the fetched page size triggers the per-node REST follow-up."""
    kids = tuple(range(2, 7))
    nodes = (
        # Page carries only 2 of 5 children.
        _node(1, "Ledger: Root", children=(2, 3), total_children=5),
        *(_node(k) for k in kids),
    )
    rest_children = tuple(
        GithubIssue(
            id=k + 5000,
            number=k,
            title=f"Issue #{k}",
            state=IssueState.open,
            html_url=f"https://github.com/testowner/testrepo/issues/{k}",
        )
        for k in kids
    )
    api = _make_api(nodes, rest_subissues={1: rest_children})
    dag = build_dag(_repo_ref(), api=cast(GithubApi, api))
    assert dag.children_of[1] == kids
    api.list_subissues.assert_called_once_with(1)


def test_blocked_by_edges_become_dependencies() -> None:
    """blockedBy edges land in dag.dependencies for the E014 check."""
    nodes = (
        _node(1, "Ledger: Root", children=(2, 3)),
        _node(2, blocked_by=(3,)),
        _node(3),
    )
    dag = build_dag(_repo_ref(), api=cast(GithubApi, _make_api(nodes)))
    assert dag.dependencies == {2: (3,)}


def test_from_graphql_field_mapping() -> None:
    """GraphQL node fields map onto the REST-shaped GithubIssue model."""
    node = {
        "number": 7,
        "databaseId": 1234,
        "title": "Mapped issue",
        "state": "CLOSED",
        "stateReason": "NOT_PLANNED",
        "body": "Body text",
        "url": "https://github.com/o/r/issues/7",
        "milestone": {"title": "v1"},
        "labels": {"nodes": [{"name": "bug"}]},
    }
    issue = GithubIssue.from_graphql(node)
    assert issue.labels == ("bug",)
    assert issue.id == 1234
    assert issue.number == 7
    assert issue.state == IssueState.closed
    assert issue.state_reason == "not_planned"
    assert issue.html_url == "https://github.com/o/r/issues/7"
    assert issue.body == "Body text"
    assert issue.milestone is not None and issue.milestone.title == "v1"
    assert not issue.is_pull_request
