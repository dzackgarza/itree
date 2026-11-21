"""Regression proofs for the gh transport: body size, pagination, provenance.

Issue #63 explicitly requires these proofs to run against a *substitute ``gh``
executable* ("tests against a substitute `gh` executable" in its Scope, and a
regression test "against a `gh` that rejects `--slurp`"). That substitute is
the one sanctioned exception to the no-fakes rule, recorded here per the
Policy Exception Protocol:

- Policy: ``test-guidelines`` NO MOCKS/SIMULATIONS.
- Requirement: #63 mandates a gh whose behavior (no ``--slurp``, old-kernel
  argument limits) cannot be installed or provoked on GitHub itself.
- Replacement invariant: the substitute is a real executable placed first on
  ``PATH`` for a real ``itree`` subprocess. It models only the external gh
  process contract, is not imported or patched into itree, and every assertion
  is over itree's own argv/stdin/output. It is not a fake GitHub: no HTTP
  behavior is simulated.
- Audit trail: this module and the PR for #63.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from time import strftime

import pytest

from itree.github import GithubApi, parse_repo_graph_pages, parse_subissues_pages
from itree.models import RepoRef

SLUG = "testowner/testrepo"
TARGET = 69

FAKE_GH = """#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
stdin = sys.stdin.read() if "--input" in args else ""
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"argv": args, "stdin": stdin}) + "\\n")

scenario = json.load(open(os.environ["FAKE_GH_SCENARIO"], encoding="utf-8"))
if "--slurp" in args:
    sys.stderr.write("unknown flag: --slurp\\n")
    sys.exit(1)
if scenario.get("fail"):
    sys.stderr.write(scenario["fail"] + "\\n")
    sys.exit(1)
if scenario.get("fail_input") and "--input" in args:
    sys.stderr.write(scenario["fail_input"] + "\\n")
    sys.exit(1)

path = next((a for a in args if a.startswith("repos/")), "").split("?")[0]
if "graphql" in args:
    pages = scenario.get("graphql_pages")
    sys.stdout.write("\\n".join(pages) if pages else scenario["graphql"])
    sys.exit(0)
if path.endswith("/sub_issues"):
    sys.stdout.write(scenario["sub_issues"])
    sys.exit(0)
if path.endswith("/branches"):
    sys.stdout.write(json.dumps(scenario.get("branches", [])))
    sys.exit(0)
if "/git/trees/" in path:
    request = next(a for a in args if a.startswith("repos/")).split("/git/trees/", 1)[1]
    sys.stdout.write(json.dumps(scenario["trees"][request]))
    sys.exit(0)
if path.endswith("/dependencies/blocked_by"):
    sys.stdout.write(json.dumps(scenario["blocked_by"][path.split("/")[-3]]))
    sys.exit(0)
number = path.rsplit("/", 1)[-1]
issue = dict(scenario["issues"][number])
if "--include" in args:
    sys.stdout.write("HTTP/2.0 200 OK\\n\\n" + json.dumps(issue))
    sys.exit(0)
if "--input" in args:
    issue.update(json.loads(stdin))
sys.stdout.write(json.dumps(issue))
"""


class GhBoundary:
    """A real substitute gh on PATH plus the scenario it replays."""

    def __init__(self, root: Path) -> None:
        self.bindir = root / "bin"
        self.bindir.mkdir()
        executable = self.bindir / "gh"
        executable.write_text(FAKE_GH, encoding="utf-8")
        executable.chmod(0o755)
        self.log = root / "calls.jsonl"
        self.scenario_path = root / "scenario.json"

    def configure(self, scenario: dict) -> None:
        self.scenario_path.write_text(json.dumps(scenario), encoding="utf-8")

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Invoke the real CLI as a subprocess with this gh first on PATH."""
        env = {
            **os.environ,
            "PATH": f"{self.bindir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GH_LOG": str(self.log),
            "FAKE_GH_SCENARIO": str(self.scenario_path),
        }
        return subprocess.run(
            [sys.executable, "-m", "itree", *args],
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
            env=env,
        )

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


def _issue(number: int, title: str, *, body: str = "") -> dict:
    return {
        "id": 10_000 + number,
        "number": number,
        "title": title,
        "state": "open",
        "html_url": f"https://github.com/{SLUG}/issues/{number}",
        "body": body,
    }


def _graph_node(
    number: int, title: str, children: list[int], *, body: str = "", labels: tuple[str, ...] = ()
) -> dict:
    return {
        "number": number,
        "databaseId": 10_000 + number,
        "title": title,
        "state": "OPEN",
        "stateReason": None,
        "body": body,
        "url": f"https://github.com/{SLUG}/issues/{number}",
        "milestone": None,
        "labels": {"nodes": [{"name": label} for label in labels]},
        "subIssues": {
            "totalCount": len(children),
            "nodes": [{"number": child, "repository": {"nameWithOwner": SLUG}} for child in children],
        },
        "parent": None,
        "blockedBy": {"totalCount": 0, "nodes": []},
    }


def _foreign_issue(number: int, state: str) -> dict:
    return {
        "id": 20_000 + number,
        "number": number,
        "title": f"Foreign {number}",
        "state": state,
        "html_url": f"https://github.com/testowner/other/issues/{number}",
    }


def _graph_document(nodes: list[dict]) -> str:
    return json.dumps(
        {
            "data": {
                "repository": {
                    "issues": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": nodes,
                    }
                }
            }
        }
    )


class TestBodyTransport:
    def test_absorb_above_the_kernel_argument_limit_sends_the_exact_body_on_stdin(self, tmp_path: Path) -> None:
        """A 131 KiB target body (the observed failure size) must reach gh over stdin (#63)."""
        gh = GhBoundary(tmp_path)
        target_body = "t" * 131_221
        content = "@not-a-file literal content"
        gh.configure({"issues": {str(TARGET): _issue(TARGET, "Work unit", body=target_body)}})
        content_file = tmp_path / "content.md"
        content_file.write_text(content, encoding="utf-8")

        result = gh.run(
            "absorb",
            "--into",
            f"{SLUG}#{TARGET}",
            "--title",
            "Large refile",
            "--body-file",
            str(content_file),
        )

        assert result.returncode == 0, result.stderr
        patch = [call for call in gh.calls() if "PATCH" in call["argv"]]
        assert len(patch) == 1
        # Same local wall-clock date the CLI records.
        today = strftime("%Y-%m-%d")
        expected = f"{target_body}\n\n## Absorbed: Large refile\n\n_Recorded {today}; absorbed at filing time._\n\n{content}"
        assert json.loads(patch[0]["stdin"]) == {"body": expected}
        assert len(expected) > 128 * 1024
        assert not any(content in argument for argument in patch[0]["argv"])


class TestSourcelessProvenance:
    def _recorded_body(self, tmp_path: Path, *extra: str) -> str:
        gh = GhBoundary(tmp_path)
        gh.configure({"issues": {str(TARGET): _issue(TARGET, "Work unit")}})
        result = gh.run(
            "absorb",
            "--into",
            f"{SLUG}#{TARGET}",
            "--title",
            "Refiled material",
            "--body",
            "evidence",
            *extra,
        )
        assert result.returncode == 0, result.stderr
        patch = [call for call in gh.calls() if "PATCH" in call["argv"]]
        assert len(patch) == 1
        body: str = json.loads(patch[0]["stdin"])["body"]
        return body

    def test_origin_omitted_records_only_the_observed_filing_time(self, tmp_path: Path) -> None:
        today = strftime("%Y-%m-%d")
        assert self._recorded_body(tmp_path) == f"\n\n## Absorbed: Refiled material\n\n_Recorded {today}; absorbed at filing time._\n\nevidence"

    def test_origin_supplied_records_the_real_origin_and_links_it(self, tmp_path: Path) -> None:
        today = strftime("%Y-%m-%d")
        assert (
            self._recorded_body(tmp_path, "--from", f"{SLUG}#17")
            == f"\n\n## Absorbed: Refiled material\n\n_Recorded {today}; refiled from {SLUG}#17._\n\nevidence"
        )


class TestSlurpFreePagination:
    def test_graphql_pages_stream_without_slurp_merge_in_order(self) -> None:
        """gh --paginate without --slurp writes one JSON document per page (#63)."""
        first = _graph_document([_graph_node(1, f"Ledger: {SLUG}", [2])])
        second = _graph_document([_graph_node(2, "Work unit", [])])

        nodes = parse_repo_graph_pages(first + "\n" + second, "testowner", "testrepo")

        assert [node["number"] for node in nodes] == [1, 2]

    def test_rest_pages_flatten_in_order_for_both_gh_shapes(self) -> None:
        """Older gh streams a REST page per document; current gh merges them into one array."""
        children = [_issue(number, f"Child {number}") for number in range(1, 131)]
        streamed = json.dumps(children[:100]) + "\n" + json.dumps(children[100:])
        merged = json.dumps(children)

        assert [child.number for child in parse_subissues_pages(streamed)] == list(range(1, 131))
        assert [child.number for child in parse_subissues_pages(merged)] == list(range(1, 131))

    def test_unexpected_stream_values_are_rejected_not_dropped(self) -> None:
        with pytest.raises(TypeError, match="non-list REST page"):
            parse_subissues_pages(json.dumps({"message": "boom"}))
        with pytest.raises(TypeError, match="non-object page"):
            parse_repo_graph_pages(json.dumps([1]), "testowner", "testrepo")

    def test_blocked_by_rest_fallback_carries_the_blocker_state(self, tmp_path: Path) -> None:
        """Past the 100-blocker GraphQL page, the REST fallback keeps each blocker's state."""
        gh = GhBoundary(tmp_path)
        work_unit = _graph_node(2, "Work unit", [])
        work_unit["blockedBy"] = {
            "totalCount": 2,
            "nodes": [{"number": 8, "state": "OPEN", "repository": {"nameWithOwner": "testowner/other"}}],
        }
        gh.configure(
            {
                "graphql": _graph_document([_graph_node(1, f"Ledger: {SLUG}", [2]), work_unit]),
                "blocked_by": {"2": [_foreign_issue(8, "open"), _foreign_issue(9, "closed")]},
            }
        )

        result = gh.run("tree", SLUG)

        assert result.returncode == 0, result.stderr
        line = next(line for line in result.stdout.splitlines() if "#2 Work unit" in line)
        assert "[blocked by testowner/other#8]" in line
        assert "#9" not in line

    def test_doctor_reports_the_same_tree_over_multiple_pages_against_gh_without_slurp(self, tmp_path: Path) -> None:
        gh = GhBoundary(tmp_path)
        # Split across two streamed documents: the ledger on page 1, the work
        # unit only on page 2. Reading page 1 alone cannot produce this tree.
        gh.configure(
            {
                "graphql_pages": [
                    _graph_document([_graph_node(1, f"Ledger: {SLUG}", [2])]),
                    _graph_document([_graph_node(2, "Work unit", [])]),
                ]
            }
        )

        result = gh.run("doctor", SLUG)

        # The synthetic work unit has no completion contract, so the tree is a
        # warning (exit 1); transport failure would exit 3 with stderr output.
        assert result.returncode == 1, result.stderr
        assert "Root ledger:\n  #1 Ledger: testowner/testrepo" in result.stdout
        assert "Next work unit: #2 Work unit" in result.stdout
        assert result.stderr == ""


def _blob(path: str) -> dict:
    return {"path": path, "type": "blob", "sha": f"blob-{path}"}


def _subtree(path: str, sha: str) -> dict:
    return {"path": path, "type": "tree", "sha": sha}


class TestTruncatedTree:
    def test_doctor_reads_a_truncated_tree_one_subtree_at_a_time(self, tmp_path: Path) -> None:
        """GitHub truncates large recursive trees; doctor must still read every file (#79).

        The recursive read of HEAD is truncated and omits src/pkg/parser.py.
        That file is visible only through a read of the src subtree, and the
        src/pkg subtree is itself truncated, so the walk must descend twice.
        """
        gh = GhBoundary(tmp_path)
        gh.configure(
            {
                "branches": [{"name": "main"}],
                "trees": {
                    "HEAD?recursive=1": {"truncated": True, "tree": [_blob("README.md"), _subtree("src", "t-src")]},
                    "HEAD": {"truncated": False, "tree": [_blob("README.md"), _subtree("src", "t-src")]},
                    "t-src?recursive=1": {"truncated": True, "tree": [_subtree("pkg", "t-pkg")]},
                    "t-src": {"truncated": False, "tree": [_subtree("pkg", "t-pkg")]},
                    "t-pkg?recursive=1": {"truncated": False, "tree": [_blob("parser.py")]},
                },
                "graphql": _graph_document(
                    [
                        _graph_node(1, f"Ledger: {SLUG}", [3, 2]),
                        _graph_node(2, "Rewrite the parser", [], body="Replace src/pkg/parser.py.", labels=("size:l",)),
                        _graph_node(3, "Fix one parser rule", [], body="Edit pkg/parser.py.", labels=("size:xs",)),
                    ]
                ),
            }
        )

        result = gh.run("doctor", SLUG, "--json")

        assert result.returncode == 1, result.stderr
        q006 = [f["evidence"] for f in json.loads(result.stdout)["findings"] if f["code"] == "Q006"]
        assert q006 == [["large unit #2 can be preceded by work on the files it names: #3 (src/pkg/parser.py)"]]


class TestFailureReporting:
    def test_gh_failure_exits_nonzero_with_the_diagnostic_on_stderr(self, tmp_path: Path) -> None:
        gh = GhBoundary(tmp_path)
        gh.configure({"fail": "HTTP 502: bad gateway"})

        result = gh.run("doctor", SLUG)

        assert result.returncode == 3
        assert result.stdout == ""
        assert "HTTP 502: bad gateway" in result.stderr

    def test_absorb_body_rejection_reports_on_stderr_and_exits_nonzero(self, tmp_path: Path) -> None:
        """The updated body is the very call #63 fixes; its failure must not go to stdout."""
        gh = GhBoundary(tmp_path)
        gh.configure({"issues": {str(TARGET): _issue(TARGET, "Work unit")}, "fail_input": "HTTP 422: body too long"})

        result = gh.run("absorb", "--into", f"{SLUG}#{TARGET}", "--title", "Refiled", "--body", "evidence")

        assert result.returncode == 3
        assert result.stdout == ""
        assert "HTTP 422: body too long" in result.stderr

    def test_absorb_rejects_from_when_a_whole_issue_source_is_given(self, tmp_path: Path) -> None:
        """--from describes refiled content; silently discarding it with SOURCE hides user input."""
        gh = GhBoundary(tmp_path)

        result = gh.run("absorb", f"{SLUG}#17", "--into", f"{SLUG}#{TARGET}", "--from", f"{SLUG}#3")

        assert result.returncode == 1
        assert "--from" in result.stdout
        assert result.stderr == ""
        assert not gh.log.exists()


def test_exec_refuses_fields_and_json_body_together() -> None:
    """A request body and -F arguments are mutually exclusive in gh api."""
    api = GithubApi.from_repo_ref(RepoRef(owner="owner", repo="repo"))

    with pytest.raises(ValueError, match="not both"):
        api._exec("PATCH", "repos/owner/repo/issues/1", fields={"state": "closed"}, json_body={"body": "x"})
