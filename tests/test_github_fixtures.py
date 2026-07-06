"""Tests using real GitHub API fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tools.itree.models import GithubIssue, IssueRef, IssueState, RepoRef

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
