"""ASCII rendering of the materialized issue tree.

The point of this module is to make tree shape visually undeniable: a
flat bag of leaves under the root, an absurd work-unit count, or a
work unit decomposed into child issues should be obvious in one screen.
"""

from __future__ import annotations

from .models import TreeNode
from .validate import is_grouping_issue

# A parent with more than MAX_CHILDREN_SHOWN visible children renders the
# first TRUNCATE_HEAD children plus one "... N more" line carrying the
# remaining issue numbers, so follow-up commands stay copy-pasteable.
MAX_CHILDREN_SHOWN = 10
TRUNCATE_HEAD = 6


def _role(node: TreeNode, is_root: bool) -> str:
    if not node.issue.is_open:
        return "[closed]"
    if is_root:
        return "[root]"
    if is_grouping_issue(node.issue.title):
        return "[grouping]"
    return "[WU]"


def _label(node: TreeNode, *, is_root: bool, next_number: int | None, duplicate: bool) -> str:
    parts = [f"#{node.issue.number} {node.issue.title}  {_role(node, is_root)}"]
    if node.issue.number == next_number:
        parts.append("<- next")
    if duplicate:
        parts.append("!E013: duplicate")
    elif node.issue.is_open and not is_grouping_issue(node.issue.title) and any(child.issue.is_open for child in node.children):
        parts.append("!E015: has child issues")
    return "  ".join(parts)


def shape_summary(root: TreeNode, next_number: int | None) -> str:
    """One-line shape digest: the absurdity metrics at a glance."""
    nodes = root.preorder()
    open_nodes = [n for n in nodes if n.issue.is_open]
    groupings = [n for n in open_nodes if is_grouping_issue(n.issue.title)]
    work_units = [n for n in open_nodes if not is_grouping_issue(n.issue.title)]

    def depth(node: TreeNode) -> int:
        return 1 + max((depth(child) for child in node.children), default=0)

    fan_out = len([child for child in root.children if child.issue.is_open])
    next_part = f"next #{next_number}" if next_number is not None else "next none"
    return f"shape: {len(open_nodes)} open | {len(groupings)} groupings | {len(work_units)} work units | depth {depth(root)} | root fan-out {fan_out} | {next_part}"


def render_tree(root: TreeNode, *, next_number: int | None = None, show_closed: bool = False) -> str:
    """Render the tree as annotated ASCII with a trailing shape line."""
    lines: list[str] = []
    seen: set[int] = set()
    hidden_closed = 0

    def visible(node: TreeNode) -> bool:
        nonlocal hidden_closed
        if node.issue.is_open or show_closed:
            return True
        hidden_closed += len(node.preorder())
        return False

    def walk(node: TreeNode, prefix: str, connector: str, is_root: bool) -> None:
        duplicate = node.issue.number in seen
        seen.add(node.issue.number)
        lines.append(f"{prefix}{connector}{_label(node, is_root=is_root, next_number=next_number, duplicate=duplicate)}")
        if duplicate:
            return

        children = [child for child in node.children if visible(child)]
        shown = children
        overflow: list[TreeNode] = []
        if len(children) > MAX_CHILDREN_SHOWN:
            shown = children[:TRUNCATE_HEAD]
            overflow = children[TRUNCATE_HEAD:]

        child_prefix = "" if is_root else prefix + ("    " if connector.startswith("└") else "│   ")
        for i, child in enumerate(shown):
            last = i == len(shown) - 1 and not overflow
            walk(child, child_prefix, "└── " if last else "├── ", is_root=False)
        if overflow:
            numbers = " ".join(f"#{child.issue.number}" for child in overflow)
            lines.append(f"{child_prefix}└── ... {len(overflow)} more: {numbers}")

    walk(root, "", "", is_root=True)

    if hidden_closed:
        lines.append("")
        lines.append(f"({hidden_closed} closed issue{'s' if hidden_closed != 1 else ''} hidden; --all to show)")
    lines.append(shape_summary(root, next_number))
    return "\n".join(lines)
