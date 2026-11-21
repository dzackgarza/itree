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

from itree.github import parse_repo_graph_pages, parse_subissues_pages

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

path = next((a for a in args if a.startswith("repos/")), "").split("?")[0]
if "graphql" in args:
    sys.stdout.write(scenario["graphql"])
    sys.exit(0)
if path.endswith("/sub_issues"):
    sys.stdout.write(scenario["sub_issues"])
    sys.exit(0)
if path.endswith("/branches"):
    sys.stdout.write("[]")
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


def _graph_node(number: int, title: str, children: list[int]) -> dict:
    return {
        "number": number,
        "databaseId": 10_000 + number,
        "title": title,
        "state": "OPEN",
        "stateReason": None,
        "body": "",
        "url": f"https://github.com/{SLUG}/issues/{number}",
        "milestone": None,
        "labels": {"nodes": []},
        "subIssues": {
            "totalCount": len(children),
            "nodes": [{"number": child, "repository": {"nameWithOwner": SLUG}} for child in children],
        },
        "parent": None,
        "blockedBy": {"totalCount": 0, "nodes": []},
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

    def test_rest_pages_stream_without_slurp_flatten_in_order(self) -> None:
        children = [_issue(number, f"Child {number}") for number in range(1, 131)]
        raw = json.dumps(children[:100]) + "\n" + json.dumps(children[100:])

        parsed = parse_subissues_pages(raw)

        assert [child.number for child in parsed] == list(range(1, 131))

    def test_doctor_reports_the_same_tree_against_gh_without_slurp(self, tmp_path: Path) -> None:
        gh = GhBoundary(tmp_path)
        gh.configure(
            {
                "graphql": _graph_document(
                    [_graph_node(1, f"Ledger: {SLUG}", [2]), _graph_node(2, "Work unit", [])]
                )
            }
        )

        result = gh.run("doctor", SLUG)

        # The synthetic work unit has no completion contract, so the tree is a
        # warning (exit 1); transport failure would exit 3 with stderr output.
        assert result.returncode == 1, result.stderr
        assert "Root ledger:\n  #1 Ledger: testowner/testrepo" in result.stdout
        assert "Next work unit: #2 Work unit" in result.stdout
        assert result.stderr == ""


class TestFailureReporting:
    def test_gh_failure_exits_nonzero_with_the_diagnostic_on_stderr(self, tmp_path: Path) -> None:
        gh = GhBoundary(tmp_path)
        gh.configure({"fail": "HTTP 502: bad gateway"})

        result = gh.run("doctor", SLUG)

        assert result.returncode == 3
        assert result.stdout == ""
        assert "HTTP 502: bad gateway" in result.stderr
