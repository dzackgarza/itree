"""Tests for the GitHub adapter's pure response parsers plus live boundary proofs.

The adapter splits each call into a ``gh`` invocation and a pure parser
(``parse_subissues_pages``, ``parse_repo_graph_pages``, ``parse_issue_parent``).
These proofs feed the parsers real captured JSON shapes, and separate live
proofs exercise the ``gh`` boundary end to end against the disposable
integration repo (#24). Nothing is stubbed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from itree.github import (
    GithubApi,
    parse_issue_parent,
    parse_repo_graph_pages,
    parse_subissues_pages,
)
from itree.models import GithubIssue, IssueRef, IssueState, RepoRef
from itree.traversal import build_dag, dag_from_graph_nodes
from itree.validate import generate_doctor_report

FIXTURE_DIR = Path(__file__).parent / "fixtures"
SCRATCH = RepoRef(owner="dzackgarza", repo="itree-e2e-scratch")


def load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture file."""
    with open(FIXTURE_DIR / name) as f:
        data: dict[str, Any] = json.load(f)
        return data


def _no_rest(number: int) -> tuple[GithubIssue, ...]:
    raise AssertionError(f"unexpected REST fallback for #{number}")


class TestGithubIssueFromFixtures:
    """Tests for parsing real GitHub API responses."""

    def test_parse_root_issue(self) -> None:
        """GithubIssue can parse a real root issue fixture."""
        data = load_fixture("issue_single.json")
        issue = GithubIssue.model_validate(data)
        assert issue.number == 42
        assert issue.title == "Root: Implement deterministic issue tree traversal"
        assert issue.state == IssueState.open
        assert issue.is_open is True
        assert issue.html_url == "https://github.com/testowner/testrepo/issues/42"
        assert issue.id == 1234567890

    def test_parse_child_issue(self) -> None:
        """GithubIssue can parse a real child issue fixture."""
        data = load_fixture("issue_child.json")
        issue = GithubIssue.model_validate(data)
        assert issue.number == 43
        assert issue.title == "Task: Implement IssueRef parsing"
        assert issue.state == IssueState.open
        assert issue.body == "Parse OWNER/REPO#NUMBER format from CLI arguments."

    def test_parse_closed_issue(self) -> None:
        """GithubIssue can parse a real closed issue fixture."""
        data = load_fixture("issue_closed.json")
        issue = GithubIssue.model_validate(data)
        assert issue.number == 44
        assert issue.title == "Done: Set up project structure"
        assert issue.state == IssueState.closed
        assert issue.is_open is False
        assert issue.state_reason == "completed"

    def test_parse_sub_issues_list(self) -> None:
        """Can parse list of sub-issues from fixture."""
        from pydantic import TypeAdapter

        data = load_fixture("sub_issues_list.json")
        issues = TypeAdapter(tuple[GithubIssue, ...]).validate_python(data)
        assert len(issues) == 3
        assert issues[0].number == 43
        assert issues[1].number == 45
        assert issues[2].number == 46

    def test_parse_nested_sub_issues(self) -> None:
        """Can parse nested sub-issues list."""
        from pydantic import TypeAdapter

        data = load_fixture("sub_issues_nested.json")
        issues = TypeAdapter(tuple[GithubIssue, ...]).validate_python(data)
        assert len(issues) == 2
        assert issues[0].title == "Decomposition: Implement core features"
        assert issues[1].title == "Decomposition: Add CLI commands"

    def test_repo_ref_from_fixture(self) -> None:
        """Can create RepoRef from fixture data."""
        repo = RepoRef(owner="testowner", repo="testrepo")
        assert repo.owner == "testowner"
        assert repo.repo == "testrepo"
        assert repo.slug == "testowner/testrepo"

    def test_repo_ref_parse(self) -> None:
        """RepoRef.parse works with fixture repo format."""
        repo = RepoRef.parse("testowner/testrepo")
        assert repo.owner == "testowner"
        assert repo.repo == "testrepo"

    def test_issue_ref_from_fixture_issue(self) -> None:
        """Can create IssueRef from fixture issue data."""
        data = load_fixture("issue_single.json")
        issue = GithubIssue.model_validate(data)
        ref = IssueRef(repo_ref=RepoRef(owner="testowner", repo="testrepo"), number=issue.number)
        assert ref.owner == "testowner"
        assert ref.repo == "testrepo"
        assert ref.number == 42
        assert ref.slug == "testowner/testrepo#42"

    def test_issue_ref_to_repo_ref(self) -> None:
        """IssueRef.to_repo_ref extracts repo correctly."""
        ref = IssueRef(repo_ref=RepoRef(owner="testowner", repo="testrepo"), number=42)
        repo = ref.to_repo_ref()
        assert repo.owner == "testowner"
        assert repo.repo == "testrepo"
        assert isinstance(repo, RepoRef)


class TestParseSubissuesPages:
    """The wide-node fallback parser must return ALL children in order (#15)."""

    def test_slurped_pages_flatten_in_order(self) -> None:
        # Real --slurp shape: an array of REST per-page arrays. 130 children
        # across a full page of 100 and a remainder of 30.
        children = [
            {
                "id": 9000 + n,
                "number": n,
                "title": f"Child {n}",
                "state": "open",
                "html_url": f"https://github.com/testowner/testrepo/issues/{n}",
            }
            for n in range(1, 131)
        ]
        raw = json.dumps([children[:100], children[100:]])

        parsed = parse_subissues_pages(raw)
        assert [c.number for c in parsed] == list(range(1, 131))


class TestParseIssueParent:
    """parse_issue_parent maps the GraphQL Issue.parent response (#15)."""

    def test_parented_issue_returns_parent_number(self) -> None:
        raw = json.dumps({"data": {"repository": {"issue": {"parent": {"number": 2}}}}})
        assert parse_issue_parent(raw, "o", "r", 15) == 2

    def test_parentless_issue_returns_none(self) -> None:
        raw = json.dumps({"data": {"repository": {"issue": {"parent": None}}}})
        assert parse_issue_parent(raw, "o", "r", 1) is None

    def test_unresolvable_issue_fails_loudly(self) -> None:
        raw = json.dumps(
            {
                "data": {"repository": {"issue": None}},
                "errors": [{"message": "Could not resolve to an issue with the number of 999."}],
            }
        )
        with pytest.raises(RuntimeError) as exc:
            parse_issue_parent(raw, "o", "r", 999)
        assert "Could not resolve to an issue" in str(exc.value)

    def test_data_absent_envelope_fails_loudly(self) -> None:
        """An errors-only document (no top-level ``data``) trips the envelope
        assertion, dumping the document — not a bare KeyError."""
        raw = json.dumps({"errors": [{"message": "Something went wrong while executing your query."}]})
        with pytest.raises(AssertionError) as exc:
            parse_issue_parent(raw, "o", "r", 999)
        assert "Something went wrong" in str(exc.value)

    def test_data_null_envelope_fails_loudly(self) -> None:
        """A document whose top-level ``data`` is null trips the envelope
        assertion, dumping the document — not a bare TypeError."""
        raw = json.dumps({"data": None, "errors": [{"message": "You do not have permission to view this issue."}]})
        with pytest.raises(AssertionError) as exc:
            parse_issue_parent(raw, "o", "r", 999)
        assert "do not have permission" in str(exc.value)


class TestParseRepoGraphPages:
    """parse_repo_graph_pages merges slurped GraphQL pages into issue nodes."""

    def test_pages_merge_into_flat_node_tuple(self) -> None:
        with open(FIXTURE_DIR / "graphql_issues_pages.json") as f:
            raw = f.read()

        nodes = parse_repo_graph_pages(raw, "testowner", "testrepo")
        assert [n["number"] for n in nodes] == [1, 2, 3, 4]
        # Sanity: the fixture is real slurp output — an array of page documents.
        assert isinstance(json.loads(raw), list)

    def test_null_repository_raises_runtime_error_with_api_text(self) -> None:
        """A missing/inaccessible repo fails loudly with the GraphQL error text (#15)."""
        raw = json.dumps(
            [
                {
                    "data": {"repository": None},
                    "errors": [{"type": "NOT_FOUND", "message": "Could not resolve to a Repository with the name 'testowner/gone'."}],
                }
            ]
        )
        with pytest.raises(RuntimeError) as exc:
            parse_repo_graph_pages(raw, "testowner", "gone")
        assert "Could not resolve to a Repository" in str(exc.value)

    def test_data_absent_envelope_fails_loudly(self) -> None:
        """A page with no top-level ``data`` (errors-only) trips the envelope
        assertion, dumping the document — not a bare KeyError."""
        raw = json.dumps([{"errors": [{"message": "Something went wrong while executing your query."}]}])
        with pytest.raises(AssertionError) as exc:
            parse_repo_graph_pages(raw, "testowner", "testrepo")
        assert "Something went wrong" in str(exc.value)

    def test_data_null_envelope_fails_loudly(self) -> None:
        """A page whose top-level ``data`` is null trips the envelope assertion,
        dumping the document — not a bare TypeError."""
        raw = json.dumps([{"data": None, "errors": [{"message": "API rate limit exceeded."}]}])
        with pytest.raises(AssertionError) as exc:
            parse_repo_graph_pages(raw, "testowner", "testrepo")
        assert "rate limit exceeded" in str(exc.value)

    def test_closed_parent_chain_reaches_doctor_e012(self) -> None:
        """End-to-end over the pure transform: the fixture's closed parent with an
        open child fires E012, not E010."""
        with open(FIXTURE_DIR / "graphql_issues_pages.json") as f:
            pages = json.load(f)
        nodes = tuple(n for page in pages for n in page["data"]["repository"]["issues"]["nodes"])

        dag = dag_from_graph_nodes(RepoRef(owner="testowner", repo="testrepo"), nodes, _no_rest)

        report = generate_doctor_report(dag)
        codes = {f.code for f in report.findings}
        assert "E012" in codes
        assert "E010" not in codes
        assert "E011" not in codes


class TestLiveAdapterBoundary:
    """The gh boundary itself, end to end against the disposable repo (#24).

    Anchors: #3 root ledger -> #4 milestone ledger -> #5 the open work unit.
    """

    def test_live_fetch_repo_graph_returns_the_known_issues(self) -> None:
        api = GithubApi.from_repo_ref(SCRATCH)
        nodes = api.fetch_repo_graph()
        numbers = {n["number"] for n in nodes}
        assert {3, 4, 5}.issubset(numbers)

    def test_live_parent_lookup_resolves_the_work_unit_parent(self) -> None:
        api = GithubApi.from_repo_ref(SCRATCH)
        assert api.get_parent_number(5) == 4

    def test_live_root_ledger_is_parentless(self) -> None:
        api = GithubApi.from_repo_ref(SCRATCH)
        assert api.get_parent_number(3) is None

    def test_live_list_subissues_returns_the_milestone_children(self) -> None:
        api = GithubApi.from_repo_ref(SCRATCH)
        children = api.list_subissues(4)
        assert 5 in {c.number for c in children}

    def test_live_build_dag_over_the_real_boundary(self) -> None:
        dag = build_dag(SCRATCH)
        assert 5 in dag.children_of.get(4, ())
