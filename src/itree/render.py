"""ASCII rendering of the materialized issue tree.

The point of this module is to make tree shape visually undeniable: a
flat bag of leaves under the root, an absurd work-unit count, or a
work unit decomposed into child issues should be obvious in one screen.
"""

from __future__ import annotations

from .models import RepoHealth, TreeNode
from .validate import is_grouping_issue

# A parent with more than MAX_CHILDREN_SHOWN visible children renders the
# first TRUNCATE_HEAD children plus one "... N more" line carrying the
# remaining issue numbers, so follow-up commands stay copy-pasteable.
MAX_CHILDREN_SHOWN = 10
TRUNCATE_HEAD = 6


def _role(node: TreeNode, root: TreeNode) -> str:
    if not node.issue.is_open:
        return "[closed]"
    if node is root:
        return "[root]"
    if is_grouping_issue(node.issue.title):
        return "[grouping]"
    return "[WU]"


def prune_closed(root: TreeNode) -> tuple[TreeNode | None, int]:
    """Copy the tree with closed subtrees removed; return (tree, hidden_count).

    A closed node drops with its entire subtree, and each dropped subtree
    counts as len(subtree.preorder()) hidden nodes. A closed root yields
    (None, total).
    """
    if not root.issue.is_open:
        return None, len(root.preorder())
    hidden = 0
    kept: list[TreeNode] = []
    for child in root.children:
        pruned, count = prune_closed(child)
        hidden += count
        if pruned is not None:
            kept.append(pruned)
    return TreeNode(issue=root.issue, children=tuple(kept)), hidden


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


def render_scan(healths: list[RepoHealth], fetch_errors: list[tuple[str, str]]) -> str:
    """Render one health line per repo plus a footer naming the worst repos.

    ``fetch_errors`` are (slug, message) pairs for repos whose graph could not
    be fetched; they render as explicit ERROR lines and count as worst.
    """
    slugs = [h.slug for h in healths] + [slug for slug, _ in fetch_errors]
    width = max((len(s) for s in slugs), default=0)

    lines: list[str] = []
    for h in healths:
        if h.next_work_unit.kind == "present":
            nxt = f"next #{h.next_work_unit.ref.number}"
        else:
            nxt = "next none"
        lines.append(f"{h.slug:<{width}}  {h.open_issues:>3} open  root {h.root_status:<4}  {h.error_count} err  {nxt}")
    for slug, message in fetch_errors:
        lines.append(f"{slug:<{width}}  ERROR: {message}")

    worst = [h.slug for h in healths if h.error_count > 0 or h.root_status != "ok"]
    worst += [slug for slug, _ in fetch_errors]
    lines.append("")
    if worst:
        lines.append(f"Worst repos ({len(worst)}) — run: itree doctor / itree triage")
        for slug in worst:
            lines.append(f"  {slug}")
    else:
        lines.append("All scanned repos have a clean root and no errors.")
    return "\n".join(lines)


def render_tree(root: TreeNode, *, next_number: int | None = None, hidden_count: int = 0) -> str:
    """Render exactly the given tree as annotated ASCII with a trailing shape line.

    Visibility filtering is not done here: pass an already-pruned tree and the
    count of nodes it dropped as hidden_count (see prune_closed).
    """
    lines: list[str] = []
    seen: set[int] = set()

    def walk(node: TreeNode, prefix: str, connector: str) -> None:
        duplicate = node.issue.number in seen
        seen.add(node.issue.number)

        parts = [f"#{node.issue.number} {node.issue.title}  {_role(node, root)}"]
        if node.issue.number == next_number:
            parts.append("<- next")
        if duplicate:
            parts.append("!E013: duplicate")
        elif node.issue.is_open and not is_grouping_issue(node.issue.title) and any(child.issue.is_open for child in node.children):
            parts.append("!E015: has child issues")
        lines.append(f"{prefix}{connector}{'  '.join(parts)}")
        if duplicate:
            return

        children = node.children
        shown = children
        overflow: tuple[TreeNode, ...] = ()
        if len(children) > MAX_CHILDREN_SHOWN:
            shown = children[:TRUNCATE_HEAD]
            overflow = children[TRUNCATE_HEAD:]

        child_prefix = "" if node is root else prefix + ("    " if connector.startswith("└") else "│   ")
        for i, child in enumerate(shown):
            last = i == len(shown) - 1 and not overflow
            walk(child, child_prefix, "└── " if last else "├── ")
        if overflow:
            numbers = " ".join(f"#{child.issue.number}" for child in overflow)
            lines.append(f"{child_prefix}└── ... {len(overflow)} more: {numbers}")

    walk(root, "", "")

    if hidden_count:
        lines.append("")
        lines.append(f"({hidden_count} closed issue{'s' if hidden_count != 1 else ''} hidden; --all to show)")
    lines.append(shape_summary(root, next_number))
    return "\n".join(lines)
