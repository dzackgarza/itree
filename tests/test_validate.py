"""Tests for tree validation functions."""

from __future__ import annotations

from itree.models import GithubIssue, IssueState, TreeNode
from itree.validate import validate_tree


class TestValidateTree:
    """Tests for validate_tree function."""

    def test_valid_tree_no_violations(self) -> None:
        """validate_tree returns empty list for valid tree."""
        root = GithubIssue(id=1, number=1, title="Root", state=IssueState.open, html_url="url")
        child1 = GithubIssue(id=2, number=2, title="Child1", state=IssueState.open, html_url="url")
        child2 = GithubIssue(id=3, number=3, title="Child2", state=IssueState.open, html_url="url")
        root_node = TreeNode(
            issue=root,
            children=(
                TreeNode(issue=child1, children=()),
                TreeNode(issue=child2, children=()),
            ),
        )
        violations = validate_tree(root_node)
        assert violations == []

    def test_dead_internal_node_violation(self) -> None:
        """validate_tree detects open internal nodes with no open descendants."""
        root = GithubIssue(id=1, number=1, title="Root", state=IssueState.open, html_url="url")
        child = GithubIssue(id=2, number=2, title="Child", state=IssueState.closed, html_url="url")
        root_node = TreeNode(
            issue=root,
            children=(TreeNode(issue=child, children=()),),
        )
        violations = validate_tree(root_node)
        assert len(violations) == 1
        assert violations[0].code == "dead_open_internal_node"
        assert violations[0].issue_number == 1

    def test_leaf_node_no_violation(self) -> None:
        """validate_tree does not flag open leaf nodes."""
        issue = GithubIssue(id=1, number=1, title="Leaf", state=IssueState.open, html_url="url")
        node = TreeNode(issue=issue, children=())
        violations = validate_tree(node)
        assert violations == []

    def test_closed_internal_node_no_violation(self) -> None:
        """validate_tree does not flag closed internal nodes."""
        root = GithubIssue(id=1, number=1, title="Root", state=IssueState.closed, html_url="url")
        child = GithubIssue(id=2, number=2, title="Child", state=IssueState.closed, html_url="url")
        root_node = TreeNode(
            issue=root,
            children=(TreeNode(issue=child, children=()),),
        )
        violations = validate_tree(root_node)
        assert violations == []

    def test_nested_open_descendants_valid(self) -> None:
        """validate_tree accepts open internal nodes with open descendants."""
        root = GithubIssue(id=1, number=1, title="Root", state=IssueState.open, html_url="url")
        child = GithubIssue(id=2, number=2, title="Child", state=IssueState.open, html_url="url")
        grandchild = GithubIssue(id=3, number=3, title="Grandchild", state=IssueState.open, html_url="url")
        root_node = TreeNode(
            issue=root,
            children=(
                TreeNode(
                    issue=child,
                    children=(TreeNode(issue=grandchild, children=()),),
                ),
            ),
        )
        violations = validate_tree(root_node)
        assert violations == []
