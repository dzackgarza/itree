"""Tests for Pydantic models."""

from __future__ import annotations

import pytest

from itree.models import (
    AttachRequest,
    GithubIssue,
    IssueRef,
    IssueState,
    MoveRequest,
    RepoRef,
    TreeNode,
)


class TestRepoRef:
    """Tests for RepoRef model."""

    def test_parse_valid_repo(self) -> None:
        """RepoRef.parse correctly parses OWNER/REPO format."""
        repo = RepoRef.parse("owner/repo")
        assert repo.owner == "owner"
        assert repo.repo == "repo"

    def test_parse_invalid_format_raises(self) -> None:
        """RepoRef.parse raises ValueError for invalid format."""
        with pytest.raises(ValueError, match="expected OWNER/REPO"):
            RepoRef.parse("invalid")

    def test_parse_with_hash_raises(self) -> None:
        """RepoRef.parse raises ValueError when hash is included."""
        with pytest.raises(ValueError, match="expected OWNER/REPO"):
            RepoRef.parse("owner/repo#123")

    def test_parse_with_spaces_raises(self) -> None:
        """RepoRef.parse raises ValueError when spaces are present."""
        with pytest.raises(ValueError, match="expected OWNER/REPO"):
            RepoRef.parse("owner name/repo")

    def test_slug_format(self) -> None:
        """RepoRef.slug returns correct format."""
        repo = RepoRef(owner="owner", repo="repo")
        assert repo.slug == "owner/repo"


class TestIssueRef:
    """Tests for IssueRef model."""

    def test_parse_valid_ref(self) -> None:
        """IssueRef.parse correctly parses OWNER/REPO#N format."""
        ref = IssueRef.parse("owner/repo#123")
        assert ref.owner == "owner"
        assert ref.repo == "repo"
        assert ref.number == 123

    def test_parse_invalid_format_raises(self) -> None:
        """IssueRef.parse raises ValueError for invalid format."""
        with pytest.raises(ValueError, match="expected OWNER/REPO#NUMBER"):
            IssueRef.parse("invalid")

    def test_parse_missing_number_raises(self) -> None:
        """IssueRef.parse raises ValueError when number is missing."""
        with pytest.raises(ValueError, match="expected OWNER/REPO#NUMBER"):
            IssueRef.parse("owner/repo#")

    def test_slug_format(self) -> None:
        """IssueRef.slug returns correct format."""
        ref = IssueRef(repo_ref=RepoRef(owner="owner", repo="repo"), number=42)
        assert ref.slug == "owner/repo#42"

    def test_same_repo(self) -> None:
        """IssueRef.same_repo correctly identifies same repository."""
        ref1 = IssueRef(repo_ref=RepoRef(owner="owner", repo="repo"), number=1)
        ref2 = IssueRef(repo_ref=RepoRef(owner="owner", repo="repo"), number=2)
        ref3 = IssueRef(repo_ref=RepoRef(owner="other", repo="repo"), number=1)
        assert ref1.same_repo(ref2) is True
        assert ref1.same_repo(ref3) is False


class TestGithubIssue:
    """Tests for GithubIssue model."""

    def test_create_issue(self) -> None:
        """GithubIssue can be created with required fields."""
        issue = GithubIssue(
            id=123456,
            number=42,
            title="Test Issue",
            state=IssueState.open,
            html_url="https://github.com/owner/repo/issues/42",
        )
        assert issue.number == 42
        assert issue.is_open is True

    def test_closed_issue(self) -> None:
        """GithubIssue.is_open returns False for closed state."""
        issue = GithubIssue(
            id=123456,
            number=42,
            title="Test Issue",
            state=IssueState.closed,
            html_url="https://github.com/owner/repo/issues/42",
        )
        assert issue.is_open is False


class TestTreeNode:
    """Tests for TreeNode model."""

    def test_leaf_node(self) -> None:
        """TreeNode with no children is a leaf."""
        issue = GithubIssue(
            id=1,
            number=1,
            title="Root",
            state=IssueState.open,
            html_url="https://github.com/owner/repo/issues/1",
        )
        node = TreeNode(issue=issue, children=())
        assert node.children == ()

    def test_internal_node(self) -> None:
        """TreeNode with children is not a leaf."""
        issue = GithubIssue(
            id=1,
            number=1,
            title="Root",
            state=IssueState.open,
            html_url="https://github.com/owner/repo/issues/1",
        )
        child_issue = GithubIssue(
            id=2,
            number=2,
            title="Child",
            state=IssueState.open,
            html_url="https://github.com/owner/repo/issues/2",
        )
        child = TreeNode(issue=child_issue, children=())
        node = TreeNode(issue=issue, children=(child,))
        assert len(node.children) == 1

    def test_preorder_traversal(self) -> None:
        """TreeNode.preorder returns nodes in preorder."""
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
        preorder = root_node.preorder()
        assert len(preorder) == 3
        assert preorder[0].issue.number == 1
        assert preorder[1].issue.number == 2
        assert preorder[2].issue.number == 3

    def test_first_open_leaf(self) -> None:
        """TreeNode.first_open_leaf returns first open leaf in preorder."""
        root = GithubIssue(id=1, number=1, title="Root", state=IssueState.open, html_url="url")
        child1 = GithubIssue(id=2, number=2, title="Child1", state=IssueState.open, html_url="url")
        grandchild = GithubIssue(id=3, number=3, title="Grandchild", state=IssueState.open, html_url="url")
        child2 = GithubIssue(id=4, number=4, title="Child2", state=IssueState.open, html_url="url")
        root_node = TreeNode(
            issue=root,
            children=(
                TreeNode(
                    issue=child1,
                    children=(TreeNode(issue=grandchild, children=()),),
                ),
                TreeNode(issue=child2, children=()),
            ),
        )
        result = root_node.first_open_leaf()
        assert result is not None
        assert result.issue.number == 3

    def test_path_to(self) -> None:
        """TreeNode.path_to returns correct path."""
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
        path = root_node.path_to(3)
        assert path is not None
        assert len(path) == 3
        assert path[0].issue.number == 1
        assert path[1].issue.number == 2
        assert path[2].issue.number == 3


class TestAttachRequest:
    """Tests for AttachRequest model."""

    def test_valid_attach(self) -> None:
        """AttachRequest validates with different repos and numbers."""
        parent = IssueRef(repo_ref=RepoRef(owner="owner", repo="repo"), number=1)
        child = IssueRef(repo_ref=RepoRef(owner="owner", repo="repo"), number=2)
        req = AttachRequest(parent=parent, child=child)
        assert req.parent == parent
        assert req.child == child

    def test_same_repo_required(self) -> None:
        """AttachRequest raises ValueError for different repos."""
        parent = IssueRef(repo_ref=RepoRef(owner="owner1", repo="repo"), number=1)
        child = IssueRef(repo_ref=RepoRef(owner="owner2", repo="repo"), number=2)
        with pytest.raises(ValueError, match="same repository"):
            AttachRequest(parent=parent, child=child)

    def test_cannot_attach_to_self(self) -> None:
        """AttachRequest raises ValueError for same issue."""
        parent = IssueRef(repo_ref=RepoRef(owner="owner", repo="repo"), number=1)
        with pytest.raises(ValueError, match="cannot be attached under itself"):
            AttachRequest(parent=parent, child=parent)


class TestMoveRequest:
    """Tests for MoveRequest model."""

    def test_valid_move(self) -> None:
        """MoveRequest validates with valid parameters."""
        child = IssueRef(repo_ref=RepoRef(owner="owner", repo="repo"), number=1)
        parent = IssueRef(repo_ref=RepoRef(owner="owner", repo="repo"), number=2)
        req = MoveRequest(child=child, parent=parent)
        assert req.child == child
        assert req.parent == parent

    def test_before_and_after_mutually_exclusive(self) -> None:
        """MoveRequest raises ValueError for both before and after."""
        child = IssueRef(repo_ref=RepoRef(owner="owner", repo="repo"), number=1)
        parent = IssueRef(repo_ref=RepoRef(owner="owner", repo="repo"), number=2)
        before = IssueRef(repo_ref=RepoRef(owner="owner", repo="repo"), number=3)
        after = IssueRef(repo_ref=RepoRef(owner="owner", repo="repo"), number=4)
        with pytest.raises(ValueError, match="either --before or --after"):
            MoveRequest(child=child, parent=parent, before=before, after=after)

    def test_same_repo_required_for_position(self) -> None:
        """MoveRequest raises ValueError for different repos in position args."""
        child = IssueRef(repo_ref=RepoRef(owner="owner", repo="repo"), number=1)
        parent = IssueRef(repo_ref=RepoRef(owner="owner", repo="repo"), number=2)
        before = IssueRef(repo_ref=RepoRef(owner="other", repo="repo"), number=3)
        with pytest.raises(ValueError, match="same repository"):
            MoveRequest(child=child, parent=parent, before=before)

    def test_cannot_move_to_self(self) -> None:
        """MoveRequest raises ValueError for moving under self."""
        child = IssueRef(repo_ref=RepoRef(owner="owner", repo="repo"), number=1)
        with pytest.raises(ValueError, match="cannot move an issue under itself"):
            MoveRequest(child=child, parent=child)
