from __future__ import annotations

from pydantic import BaseModel

from .models import RepoDag, TreeNode


class TreeViolation(BaseModel):
    """A single tree invariant violation."""

    code: str
    message: str
    issue_number: int | None = None


def validate_dag(dag: RepoDag) -> list[TreeViolation]:
    """Validate DAG-level structural invariants.

    Checks for:
    - Fragmented forest: more than one root means the graph is not a tree.
    - Orphaned issues: issues not reachable from any root.

    Args:
        dag: The full repository DAG to validate.

    Returns:
        List of TreeViolation objects describing structural failures.
    """
    violations: list[TreeViolation] = []
    roots = dag.roots
    orphans = dag.orphans

    if len(roots) > 1:
        root_numbers = ", ".join(f"#{r.number}" for r in roots)
        violations.append(
            TreeViolation(
                code="fragmented_forest",
                message=f"graph has {len(roots)} roots ({root_numbers}) \u2014 not a single tree",
            )
        )

    for orphan in orphans:
        violations.append(
            TreeViolation(
                code="orphaned_issue",
                message=f"issue #{orphan.number} \"{orphan.title}\" is not reachable from any root",
                issue_number=orphan.number,
            )
        )

    return violations


def validate_tree(root: TreeNode) -> list[TreeViolation]:
    """Validate tree invariants.

    Checks for:
    - Duplicate reachable issues
    - Open internal nodes whose decomposition has no open descendants

    Uses TreeNode.preorder() for traversal.

    Args:
        root: The root TreeNode to validate.

    Returns:
        List of TreeViolation objects describing any issues found.
        Empty list means the tree is valid.
    """
    violations: list[TreeViolation] = []
    seen: set[int] = set()

    for node in root.preorder():
        if node.issue.id in seen:
            violations.append(
                TreeViolation(
                    code="duplicate_reachable_issue",
                    message=f"issue #{node.issue.number} appears more than once under root",
                    issue_number=node.issue.number,
                )
            )
            continue
        seen.add(node.issue.id)

        if node.issue.is_open and node.children and all(child.first_open_leaf() is None for child in node.children):
            violations.append(
                TreeViolation(
                    code="dead_open_internal_node",
                    message=f"open internal issue #{node.issue.number} has no open descendants",
                    issue_number=node.issue.number,
                )
            )

    return violations

