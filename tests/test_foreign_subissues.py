"""Cross-repository sub-issues stay foreign through the real doctor path.

GitHub allows a sub-issue from another repository, and ``gh issue transfer``
leaves a transferred issue listed under its old parent. The repo-graph query
must key each child by repository and number, so a foreign child never
collides with the local issue that has the same number (dzackgarza/itree#58).
"""

from __future__ import annotations

from itree.models import GithubIssue, RepoRef
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
