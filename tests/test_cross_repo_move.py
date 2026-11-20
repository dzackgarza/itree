"""Cross-repository sub-issues through the live CLI.

GitHub's sub-issues REST contract accepts a child from any repository of the
parent's owner ("The sub-issue must belong to the same repository owner as the
parent issue", POST /repos/{owner}/{repo}/issues/{issue_number}/sub_issues).
A computation issue in one repository can therefore sit under the theorem
issue it serves in another, and the parent repository's tree orders it.

Scratch anchors: dzackgarza/itree-e2e-scratch #4 (milestone ledger) holds the
work unit #5; dzackgarza/itree-e2e-scratch-foreign #1 is that repository's
root ledger. Neither anchor is mutated; the proof issue is detached and closed
on teardown.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator

import pytest

LOCAL = "dzackgarza/itree-e2e-scratch"
FOREIGN = "dzackgarza/itree-e2e-scratch-foreign"
MILESTONE, WORKUNIT = 4, 5

PARENT_QUERY = "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){issue(number:$number){id parent{id number repository{nameWithOwner}}}}}"


def _itree(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "itree.cli", *args], capture_output=True, text=True, timeout=120)


def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True, timeout=60).stdout


def _parent(number: int) -> dict | None:
    owner, name = FOREIGN.split("/")
    raw = _gh("api", "graphql", "-f", f"query={PARENT_QUERY}", "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"number={number}")
    return json.loads(raw)["data"]["repository"]["issue"]["parent"]


@pytest.fixture
def foreign_issue() -> Iterator[int]:
    url = _gh("issue", "create", "-R", FOREIGN, "-t", "proof: cross-repository computation", "-b", "## Acceptance Criteria\n- proof fixture").strip()
    number = int(url.rsplit("/", 1)[1])
    try:
        yield number
    finally:
        parent = _parent(number)
        if parent is not None:
            child_id = json.loads(_gh("api", f"repos/{FOREIGN}/issues/{number}"))["id"]
            parent_slug = parent["repository"]["nameWithOwner"]
            _gh("api", "-X", "DELETE", f"repos/{parent_slug}/issues/{parent['number']}/sub_issue", "-F", f"sub_issue_id={child_id}")
        _gh("issue", "close", "-R", FOREIGN, str(number), "--reason", "not planned")


def test_move_places_a_foreign_child_under_the_parent_and_the_parent_tree_orders_it(foreign_issue: int) -> None:
    """`itree move B#N --under A#4 --before A#5` attaches across repositories; A's next and tree name B#N first."""
    moved = _itree("move", f"{FOREIGN}#{foreign_issue}", "--under", f"{LOCAL}#{MILESTONE}", "--before", f"{LOCAL}#{WORKUNIT}")
    assert moved.returncode == 0, moved.stdout + moved.stderr
    assert moved.stdout.strip() == f"{FOREIGN}#{foreign_issue}"

    parent = _parent(foreign_issue)
    assert parent is not None
    assert (parent["repository"]["nameWithOwner"], parent["number"]) == (LOCAL, MILESTONE)

    nxt = _itree("next", LOCAL)
    assert nxt.returncode == 0, nxt.stdout + nxt.stderr
    assert f"{FOREIGN}#{foreign_issue} proof: cross-repository computation" in nxt.stdout

    tree = _itree("tree", LOCAL)
    assert tree.returncode == 0, tree.stdout + tree.stderr
    lines = tree.stdout.splitlines()
    foreign_line = next(i for i, line in enumerate(lines) if f"{FOREIGN}#{foreign_issue}" in line)
    local_line = next(i for i, line in enumerate(lines) if f"#{WORKUNIT} " in line)
    assert foreign_line < local_line
    assert "<- next" in lines[foreign_line]
    assert "[foreign]" in lines[foreign_line]

    # The child's own repository does not count it as a second root or an orphan.
    doctor = _itree("doctor", FOREIGN, "--json")
    codes = {finding["code"] for finding in json.loads(doctor.stdout)["findings"]}
    assert not codes & {"E002", "E010", "E011"}, doctor.stdout


def test_move_refuses_a_child_of_another_owner_before_any_write() -> None:
    """GitHub accepts sub-issues only within one owner; itree refuses the request loudly."""
    refused = _itree("move", "someoneelse/repo#1", "--under", f"{LOCAL}#{MILESTONE}")
    assert refused.returncode != 0
    assert "same owner" in refused.stdout + refused.stderr
