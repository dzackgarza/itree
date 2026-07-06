from __future__ import annotations

from .github import GithubApi
from .models import IssueRef, TreeNode


def materialize(root: IssueRef) -> TreeNode:
    """Materialize the full tree rooted at the given issue reference."""
    api = GithubApi.from_issue_ref(root)
    issue = api.get_issue(root.number)
    children = tuple(
        materialize(IssueRef(owner=root.owner, repo=root.repo, number=child.number))
        for child in api.list_subissues(root.number)
    )
    return TreeNode(issue=issue, children=children)


def next_leaf(root: IssueRef) -> TreeNode | None:
    """Find the first open leaf under root in preorder traversal."""
    return materialize(root).first_open_leaf()


def get_direct_children(root: IssueRef) -> tuple[TreeNode, ...]:
    """Get the direct children of the given issue."""
    tree = materialize(root)
    return tree.children


def get_descendants_preorder(root: IssueRef) -> tuple[TreeNode, ...]:
    """Get all descendants of the given issue in preorder traversal."""
    tree = materialize(root)
    return tree.descendants()


def get_path(issue_ref: IssueRef, root_ref: IssueRef) -> tuple[TreeNode, ...] | None:
    """Get the path from root to the given issue."""
    tree = materialize(root_ref)
    return tree.path_to(issue_ref.number)
