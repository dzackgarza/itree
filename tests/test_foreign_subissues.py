"""Cross-repository sub-issues stay foreign through the real doctor path.

GitHub allows a sub-issue from another repository, and ``gh issue transfer``
leaves a transferred issue listed under its old parent. The repo-graph query
must key each child by repository and number, so a foreign child never
collides with the local issue that has the same number (dzackgarza/itree#58).
"""

from __future__ import annotations

from itree.models import GithubIssue, RepoRef
from itree.render import render_tree
from itree.traversal import dag_from_graph_nodes
from itree.validate import generate_doctor_report

LOCAL = "testowner/testrepo"
FOREIGN = "testowner/other"


def _node(
    number: int,
    title: str,
    state: str = "OPEN",
    milestone: str | None = None,
    children: tuple[tuple[str, int], ...] = (),
) -> dict:
    return {
        "number": number,
        "databaseId": number + 5000,
        "title": title,
        "state": state,
        "stateReason": "COMPLETED" if state == "CLOSED" else None,
        "body": "## Acceptance Criteria\n- done",
        "url": f"https://github.com/{LOCAL}/issues/{number}",
        "milestone": {"title": milestone} if milestone else None,
        "labels": {"nodes": []},
        "subIssues": {
            "totalCount": len(children),
            "nodes": [{"number": n, "repository": {"nameWithOwner": repo}} for repo, n in children],
        },
        "blockedBy": {"totalCount": 0, "nodes": []},
    }


def _no_rest(number: int) -> tuple[GithubIssue, ...]:
    raise AssertionError(f"unexpected REST fallback for #{number}")


def _nodes() -> tuple[dict, ...]:
    """Root -> {closed ledger #103 -> closed local #204, Milestone: M1 #420 -> foreign #204, local #10}."""
    return (
        _node(1, "Ledger: Root", children=((LOCAL, 103), (LOCAL, 420))),
        _node(103, "Ledger: Slop", state="CLOSED", children=((LOCAL, 204),)),
        _node(204, "Closed local slop finding", state="CLOSED"),
        _node(420, "Milestone: M1", milestone="M1", children=((FOREIGN, 204), (LOCAL, 10))),
        _node(10, "Local work unit", milestone="M1"),
    )


def test_foreign_subissue_does_not_collide_with_local_issue_of_same_number() -> None:
    """A foreign child numbered like a closed local issue gives no E013/W040 and is named with its repository."""
    dag = dag_from_graph_nodes(RepoRef(owner="testowner", repo="testrepo"), _nodes(), _no_rest)
    report = generate_doctor_report(dag)
    codes = {finding.code for finding in report.findings}

    assert "E013" not in codes
    assert "W040" not in codes
    evidence = [line for finding in report.findings for line in finding.evidence]
    assert any(f"{FOREIGN}#204" in line and "#420" in line for line in evidence), evidence


def test_tree_keeps_local_and_foreign_child_of_same_number_distinguishable() -> None:
    """`itree tree --all` shows the closed local #204 under its ledger and the foreign #204 qualified under #420."""
    dag = dag_from_graph_nodes(RepoRef(owner="testowner", repo="testrepo"), _nodes(), _no_rest)
    out = render_tree(dag.materialize_root(1), dag=dag)

    assert out.splitlines()[:6] == [
        "#1 Ledger: Root  [root]",
        "├── #103 Ledger: Slop  [closed]",
        "│   └── #204 Closed local slop finding  [closed]",
        "└── #420 Milestone: M1  [grouping]",
        "    ├── #10 Local work unit  [WU]",
        f"    └── {FOREIGN}#204  [foreign]",
    ]


def test_grouping_whose_only_child_is_foreign_is_still_dead_locally() -> None:
    """A foreign child is not a live local descendant, so the grouping still reports W030."""
    nodes = (
        _node(1, "Ledger: Root", children=((LOCAL, 2), (LOCAL, 3))),
        _node(2, "Milestone: M1", milestone="M1", children=((FOREIGN, 9),)),
        _node(3, "Local work unit"),
    )
    report = generate_doctor_report(dag_from_graph_nodes(RepoRef(owner="testowner", repo="testrepo"), nodes, _no_rest))
    dead = [finding for finding in report.findings if finding.code == "W030"]

    assert len(dead) == 1
    assert dead[0].evidence == ['#2 "Milestone: M1" has no open descendants']


def test_foreign_blocker_is_not_a_local_dependency() -> None:
    """A foreign blocker numbered like a local issue does not block the local issue and is named in I012."""
    nodes = (
        _node(1, "Ledger: Root", children=((LOCAL, 2), (LOCAL, 3))),
        _node(2, "First work unit"),
        _node(3, "Second work unit") | {"blockedBy": {"totalCount": 1, "nodes": [{"number": 2, "repository": {"nameWithOwner": FOREIGN}}]}},
    )
    dag = dag_from_graph_nodes(RepoRef(owner="testowner", repo="testrepo"), nodes, _no_rest)
    report = generate_doctor_report(dag)

    assert 3 not in dag.dependencies
    foreign = [finding for finding in report.findings if finding.code == "I012"]
    assert foreign[0].evidence == [f"#3 is blocked by foreign issue {FOREIGN}#2"]


def test_repository_match_is_case_insensitive() -> None:
    """GitHub's canonical nameWithOwner may differ in case from the scanned ref; the child stays local."""
    nodes = (
        _node(1, "Ledger: Root", children=(("TestOwner/TestRepo", 2),)),
        _node(2, "Work unit"),
    )
    dag = dag_from_graph_nodes(RepoRef(owner="testowner", repo="testrepo"), nodes, _no_rest)

    assert dag.children_of[1] == (2,)
    assert dag.foreign_children == {}
