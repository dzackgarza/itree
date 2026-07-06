"""Tests for tree traversal algorithms — tested directly on TreeNode objects.

These tests prove the tree traversal algorithms (first_open_leaf, descendants,
preorder, path_to, children) work correctly on real TreeNode structures.
No mocking of materialize() is used — the orchestration boundary is proved by
its constituent pieces.
"""

from __future__ import annotations

from tools.itree.models import GithubIssue, IssueState, TreeNode

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _issue(number: int, state: IssueState = IssueState.open, title: str = "") -> GithubIssue:
    """Build a minimal GithubIssue for testing."""
    return GithubIssue(
        id=number,
        number=number,
        title=title or f"Issue #{number}",
        state=state,
        html_url=f"https://github.com/testowner/testrepo/issues/{number}",
    )


def _leaf(number: int, state: IssueState = IssueState.open, title: str = "") -> TreeNode:
    """Build a leaf TreeNode."""
    return TreeNode(issue=_issue(number, state=state, title=title), children=())


def _root_with_three_children() -> TreeNode:
    """Root with three open leaf children."""
    return TreeNode(
        issue=_issue(42, title="Root"),
        children=(
            _leaf(43, title="Child A"),
            _leaf(45, title="Child B"),
            _leaf(46, title="Child C"),
        ),
    )


def _nested_tree() -> TreeNode:
    """Two-level tree: root -> child1 (with grandchild) + child2."""
    return TreeNode(
        issue=_issue(1, title="Root"),
        children=(
            TreeNode(
                issue=_issue(2, title="Child1"),
                children=(_leaf(3, title="Grandchild"),),
            ),
            _leaf(4, title="Child2"),
        ),
    )


# ---------------------------------------------------------------------------
# first_open_leaf — preorder leaf resolution
# ---------------------------------------------------------------------------


class TestFirstOpenLeaf:
    """Tests for TreeNode.first_open_leaf."""

    def test_single_open_leaf(self) -> None:
        node = TreeNode(issue=_issue(1), children=())
        result = node.first_open_leaf()
        assert result is not None
        assert result.issue.number == 1

    def test_returns_first_child(self) -> None:
        root = _root_with_three_children()
        result = root.first_open_leaf()
        assert result is not None
        assert result.issue.number == 43

    def test_deepest_preorder_leaf(self) -> None:
        root = _nested_tree()
        result = root.first_open_leaf()
        assert result is not None
        assert result.issue.number == 3

    def test_closed_root_returns_none(self) -> None:
        node = TreeNode(issue=_issue(1, state=IssueState.closed), children=())
        assert node.first_open_leaf() is None

    def test_closed_child_skipped(self) -> None:
        root = TreeNode(
            issue=_issue(1),
            children=(
                TreeNode(issue=_issue(2, state=IssueState.closed), children=()),
                _leaf(3),
            ),
        )
        result = root.first_open_leaf()
        assert result is not None
        assert result.issue.number == 3


# ---------------------------------------------------------------------------
# descendants — preorder traversal excluding root
# ---------------------------------------------------------------------------


class TestDescendants:
    """Tests for TreeNode.descendants."""

    def test_leaf_has_no_descendants(self) -> None:
        node = TreeNode(issue=_issue(1), children=())
        assert node.descendants() == ()

    def test_children_only(self) -> None:
        root = _root_with_three_children()
        desc = root.descendants()
        assert len(desc) == 3
        assert [n.issue.number for n in desc] == [43, 45, 46]

    def test_nested_preorder(self) -> None:
        root = _nested_tree()
        desc = root.descendants()
        numbers = [n.issue.number for n in desc]
        assert numbers == [2, 3, 4]


# ---------------------------------------------------------------------------
# preorder — full tree traversal including root
# ---------------------------------------------------------------------------


class TestPreorder:
    """Tests for TreeNode.preorder."""

    def test_leaf_only(self) -> None:
        node = TreeNode(issue=_issue(1), children=())
        result = node.preorder()
        assert len(result) == 1
        assert result[0].issue.number == 1

    def test_root_plus_children(self) -> None:
        root = _root_with_three_children()
        result = root.preorder()
        assert len(result) == 4
        assert result[0].issue.number == 42
        assert [n.issue.number for n in result[1:]] == [43, 45, 46]

    def test_nested(self) -> None:
        root = _nested_tree()
        result = root.preorder()
        assert [n.issue.number for n in result] == [1, 2, 3, 4]


# ---------------------------------------------------------------------------
# children — child access
# ---------------------------------------------------------------------------


class TestChildrenAccess:
    """Tests for TreeNode.children."""

    def test_children_count(self) -> None:
        root = _root_with_three_children()
        assert len(root.children) == 3

    def test_filters_closed_children(self) -> None:
        root = TreeNode(
            issue=_issue(1),
            children=(
                _leaf(2),
                TreeNode(issue=_issue(3, state=IssueState.closed), children=()),
                _leaf(4),
            ),
        )
        open_children = [n for n in root.children if n.issue.is_open]
        assert len(open_children) == 2
        assert [n.issue.number for n in open_children] == [2, 4]

    def test_leaf_has_no_children(self) -> None:
        node = TreeNode(issue=_issue(1), children=())
        assert node.children == ()

    def test_internal_node_has_children(self) -> None:
        root = _root_with_three_children()
        assert len(root.children) == 3


# ---------------------------------------------------------------------------
# path_to — path from root to a target issue
# ---------------------------------------------------------------------------


class TestPathTo:
    """Tests for TreeNode.path_to."""

    def test_path_to_self(self) -> None:
        node = TreeNode(issue=_issue(1), children=())
        path = node.path_to(1)
        assert path is not None
        assert len(path) == 1
        assert path[0].issue.number == 1

    def test_path_to_child(self) -> None:
        root = _nested_tree()
        path = root.path_to(2)
        assert path is not None
        assert len(path) == 2
        assert path[0].issue.number == 1
        assert path[1].issue.number == 2

    def test_path_to_grandchild(self) -> None:
        root = _nested_tree()
        path = root.path_to(3)
        assert path is not None
        assert len(path) == 3
        assert [n.issue.number for n in path] == [1, 2, 3]

    def test_path_not_found(self) -> None:
        root = _nested_tree()
        assert root.path_to(999) is None

    def test_path_across_siblings(self) -> None:
        root = _root_with_three_children()
        path = root.path_to(46)
        assert path is not None
        assert [n.issue.number for n in path] == [42, 46]
