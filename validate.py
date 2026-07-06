from __future__ import annotations

from pydantic import BaseModel

from .models import IssueRef, TreeNode
from .traversal import materialize


class TreeViolation(BaseModel):
    """A single tree invariant violation."""

    code: str
    message: str
    issue_number: int | None = None


def _has_open_descendant(node: TreeNode) -> bool:
    """Check if node has any open descendant in its subtree.

    Uses short-circuiting recursion to avoid unnecessary allocations.

    Args:
        node: The TreeNode to check.

    Returns:
        True if any descendant (excluding self) is open, False otherwise.
    """
    for child in node.children:
        if child.issue.is_open:
            return True
        if _has_open_descendant(child):
            return True
    return False


def validate_tree(root: TreeNode) -> list[TreeViolation]:
    """Validate tree invariants.

    Checks for:
    - Duplicate reachable issues
    - Open internal nodes whose decomposition has no open descendants

    Args:
        root: The root TreeNode to validate.

    Returns:
        List of TreeViolation objects describing any issues found.
        Empty list means the tree is valid.
    """
    violations: list[TreeViolation] = []
    seen: set[int] = set()

    def walk(node: TreeNode, *, is_root: bool) -> None:
        """Recursively walk the tree and collect violations.

        Args:
            node: The current TreeNode being visited.
            is_root: Whether this node is the root of the tree.
        """
        if node.issue.id in seen:
            violations.append(TreeViolation(
                code="duplicate_reachable_issue",
                message=f"issue #{node.issue.number} appears more than once under root",
                issue_number=node.issue.number,
            ))
            return
        seen.add(node.issue.id)

        if node.issue.is_open and node.children and not _has_open_descendant(node):
            violations.append(TreeViolation(
                code="dead_open_internal_node",
                message=f"open internal issue #{node.issue.number} has no open descendants",
                issue_number=node.issue.number,
            ))

        for child in node.children:
            walk(child, is_root=False)

    walk(root, is_root=True)
    return violations


def full_validate(root: IssueRef) -> list[TreeViolation]:
    """Perform full validation of the tree rooted at the given issue.

    Args:
        root: The root issue reference.

    Returns:
        List of TreeViolation objects describing any issues found.
    """
    tree = materialize(root)
    return validate_tree(tree)
