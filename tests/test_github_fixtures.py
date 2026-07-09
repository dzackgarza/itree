"""Tests using real GitHub API fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from itree.models import GithubIssue, IssueRef, IssueState, RepoRef

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture file."""
    with open(FIXTURE_DIR / name) as f:
        data: dict[str, Any] = json.load(f)
        return data


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


class TestListSubissuesPagination:
    """The wide-node fallback must return ALL children, not one REST page (#15)."""

    CHILDREN = [
        {
            "id": 9000 + n,
            "number": n,
            "title": f"Child {n}",
            "state": "open",
            "html_url": f"https://github.com/testowner/testrepo/issues/{n}",
        }
        for n in range(1, 131)
    ]

    def _fake_gh(self, cmd: list[str]) -> str:
        """Behave like gh api: 30 items per page unless --paginate walks them all."""
        if "--paginate" in cmd:
            assert "--slurp" in cmd, "paginated array responses need --slurp to stay parseable"
            assert any("per_page=100" in part for part in cmd), "pagination should request full pages"
            return json.dumps([self.CHILDREN[:100], self.CHILDREN[100:]])
        return json.dumps(self.CHILDREN[:30])

    def test_130_child_node_returns_all_children_in_order(self) -> None:
        from unittest.mock import MagicMock, patch

        from itree.github import GithubApi

        api = GithubApi(repo_ref=RepoRef(owner="testowner", repo="testrepo"))

        def run(cmd: list[str], path: str, timeout: int) -> MagicMock:
            proc = MagicMock()
            proc.stdout = self._fake_gh(cmd)
            return proc

        with patch.object(GithubApi, "_run_api_command", side_effect=run):
            children = api.list_subissues(7)

        assert [c.number for c in children] == list(range(1, 131))


class TestGetParentNumber:
    """GithubApi.get_parent_number parses GraphQL Issue.parent (#15)."""

    @staticmethod
    def _api_with_payload(payload: dict) -> "tuple[Any, Any]":
        from unittest.mock import MagicMock, patch

        from itree.github import GithubApi

        api = GithubApi(repo_ref=RepoRef(owner="testowner", repo="testrepo"))
        proc = MagicMock()
        proc.stdout = json.dumps(payload)
        return api, patch.object(GithubApi, "_run_api_command", return_value=proc)

    def test_parented_issue_returns_parent_number(self) -> None:
        api, patcher = self._api_with_payload({"data": {"repository": {"issue": {"parent": {"number": 2}}}}})
        with patcher:
            assert api.get_parent_number(15) == 2

    def test_parentless_issue_returns_none(self) -> None:
        api, patcher = self._api_with_payload({"data": {"repository": {"issue": {"parent": None}}}})
        with patcher:
            assert api.get_parent_number(1) is None

    def test_unresolvable_issue_fails_loudly(self) -> None:
        api, patcher = self._api_with_payload(
            {
                "data": {"repository": {"issue": None}},
                "errors": [{"message": "Could not resolve to an issue with the number of 999."}],
            }
        )
        with patcher:
            with pytest.raises(RuntimeError) as exc:
                api.get_parent_number(999)
        assert "Could not resolve to an issue" in str(exc.value)


class TestFetchRepoGraph:
    """Tests for the paginated GraphQL fetch against a captured --slurp payload."""

    def test_pages_merge_into_flat_node_tuple(self) -> None:
        """fetch_repo_graph concatenates issue nodes across slurped pages."""
        import json as json_module
        from unittest.mock import MagicMock, patch

        from itree.github import GithubApi

        with open(FIXTURE_DIR / "graphql_issues_pages.json") as f:
            raw = f.read()

        api = GithubApi(repo_ref=RepoRef(owner="testowner", repo="testrepo"))
        proc = MagicMock()
        proc.stdout = raw
        with patch.object(GithubApi, "_run_api_command", return_value=proc) as run:
            nodes = api.fetch_repo_graph()

        assert [n["number"] for n in nodes] == [1, 2, 3, 4]
        # The command must be the paginated, slurped GraphQL call.
        cmd = run.call_args.args[0]
        assert cmd[:3] == ["gh", "api", "graphql"]
        assert "--paginate" in cmd and "--slurp" in cmd
        # Sanity: the fixture is real slurp output — an array of page documents.
        assert isinstance(json_module.loads(raw), list)

    def test_null_repository_raises_runtime_error_with_api_text(self) -> None:
        """A missing/inaccessible repo fails loudly with the GraphQL error text, not a traceback (#15)."""
        from unittest.mock import MagicMock, patch

        from itree.github import GithubApi

        page = {
            "data": {"repository": None},
            "errors": [{"type": "NOT_FOUND", "message": "Could not resolve to a Repository with the name 'testowner/gone'."}],
        }
        api = GithubApi(repo_ref=RepoRef(owner="testowner", repo="gone"))
        proc = MagicMock()
        proc.stdout = json.dumps([page])
        with patch.object(GithubApi, "_run_api_command", return_value=proc):
            with pytest.raises(RuntimeError) as exc:
                api.fetch_repo_graph()
        # The API's own error text must reach the user (containment, not exact match).
        assert "Could not resolve to a Repository" in str(exc.value)

    def test_closed_parent_chain_reaches_doctor_e012(self) -> None:
        """End-to-end: the fixture's closed parent with an open child fires E012, not E010."""
        from typing import cast
        from unittest.mock import MagicMock

        from itree.github import GithubApi
        from itree.traversal import build_dag
        from itree.validate import generate_doctor_report

        with open(FIXTURE_DIR / "graphql_issues_pages.json") as f:
            pages = json.load(f)
        nodes = tuple(n for page in pages for n in page["data"]["repository"]["issues"]["nodes"])

        api = MagicMock(spec=GithubApi)
        api.fetch_repo_graph.return_value = nodes
        dag = build_dag(RepoRef(owner="testowner", repo="testrepo"), api=cast(GithubApi, api))

        report = generate_doctor_report(dag)
        codes = {f.code for f in report.findings}
        assert "E012" in codes
        assert "E010" not in codes
        assert "E011" not in codes
