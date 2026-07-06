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
        ref = IssueRef(owner="testowner", repo="testrepo", number=issue.number)
        assert ref.owner == "testowner"
        assert ref.repo == "testrepo"
        assert ref.number == 42
        assert ref.slug == "testowner/testrepo#42"

    def test_issue_ref_to_repo_ref(self) -> None:
        """IssueRef.to_repo_ref extracts repo correctly."""
        ref = IssueRef(owner="testowner", repo="testrepo", number=42)
        repo = ref.to_repo_ref()
        assert repo.owner == "testowner"
        assert repo.repo == "testrepo"
        assert isinstance(repo, RepoRef)


class TestTreeConstructionFromFixtures:
    """Tests for building tree structures from fixtures."""

    def test_build_simple_tree(self) -> None:
        """Can build a simple tree from fixtures."""
        from tools.itree.models import TreeNode
        root_data = load_fixture("issue_single.json")
        root_issue = GithubIssue.model_validate(root_data)
        root_node = TreeNode(issue=root_issue, children=())
        assert root_node.issue.number == 42
        assert root_node.is_leaf is True

    def test_build_tree_with_children(self) -> None:
        """Can build a tree with children from fixtures."""
        from pydantic import TypeAdapter

        from tools.itree.models import TreeNode
        
        root_data = load_fixture("issue_single.json")
        root_issue = GithubIssue.model_validate(root_data)
        
        children_data = load_fixture("sub_issues_list.json")
        children_issues = TypeAdapter(tuple[GithubIssue, ...]).validate_python(children_data)
        
        children_nodes = tuple(
            TreeNode(issue=child, children=())
            for child in children_issues
        )
        
        root_node = TreeNode(issue=root_issue, children=children_nodes)
        assert len(root_node.children) == 3
        assert root_node.is_leaf is False
        assert root_node.children[0].issue.number == 43
        assert root_node.children[1].issue.number == 45
        assert root_node.children[2].issue.number == 46

    def test_preorder_traversal_with_fixtures(self) -> None:
        """Preorder traversal works with fixture-based tree."""
        from pydantic import TypeAdapter

        from tools.itree.models import TreeNode
        
        root_data = load_fixture("issue_single.json")
        root_issue = GithubIssue.model_validate(root_data)
        
        children_data = load_fixture("sub_issues_list.json")
        children_issues = TypeAdapter(tuple[GithubIssue, ...]).validate_python(children_data)
        
        children_nodes = tuple(
            TreeNode(issue=child, children=())
            for child in children_issues
        )
        
        root_node = TreeNode(issue=root_issue, children=children_nodes)
        preorder = root_node.preorder()
        
        assert len(preorder) == 4
        assert preorder[0].issue.number == 42  # root
        assert preorder[1].issue.number == 43  # first child
        assert preorder[2].issue.number == 45  # second child
        assert preorder[3].issue.number == 46  # third child

    def test_first_open_leaf_with_fixtures(self) -> None:
        """First open leaf works with fixture-based tree."""
        from pydantic import TypeAdapter

        from tools.itree.models import TreeNode
        
        root_data = load_fixture("issue_single.json")
        root_issue = GithubIssue.model_validate(root_data)
        
        children_data = load_fixture("sub_issues_list.json")
        children_issues = TypeAdapter(tuple[GithubIssue, ...]).validate_python(children_data)
        
        children_nodes = tuple(
            TreeNode(issue=child, children=())
            for child in children_issues
        )
        
        root_node = TreeNode(issue=root_issue, children=children_nodes)
        first_leaf = root_node.first_open_leaf()
        
        # All issues in fixtures are open, root has children so not a leaf
        # First leaf should be the first child (43)
        assert first_leaf is not None
        assert first_leaf.issue.number == 43


class TestValidationWithFixtures:
    """Tests for validation using fixture data."""

    def test_validate_valid_tree_from_fixtures(self) -> None:
        """Validation passes for tree built from fixtures."""
        from pydantic import TypeAdapter

        from tools.itree.models import TreeNode
        from tools.itree.validate import validate_tree
        
        root_data = load_fixture("issue_single.json")
        root_issue = GithubIssue.model_validate(root_data)
        
        children_data = load_fixture("sub_issues_list.json")
        children_issues = TypeAdapter(tuple[GithubIssue, ...]).validate_python(children_data)
        
        children_nodes = tuple(
            TreeNode(issue=child, children=())
            for child in children_issues
        )
        
        root_node = TreeNode(issue=root_issue, children=children_nodes)
        violations = validate_tree(root_node)
        
        # This should be a valid tree - root is open with open children
        assert violations == []

    def test_validate_tree_with_closed_children(self) -> None:
        """Validation detects dead internal nodes with closed children."""
        from tools.itree.models import TreeNode
        from tools.itree.validate import validate_tree
        
        root_data = load_fixture("issue_single.json")
        root_issue = GithubIssue.model_validate(root_data)
        
        closed_data = load_fixture("issue_closed.json")
        closed_issue = GithubIssue.model_validate(closed_data)
        
        root_node = TreeNode(
            issue=root_issue,
            children=(TreeNode(issue=closed_issue, children=()),)
        )
        
        violations = validate_tree(root_node)
        assert len(violations) == 1
        assert violations[0].code == "dead_open_internal_node"
        assert violations[0].issue_number == 42
