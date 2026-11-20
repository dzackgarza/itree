"""ASCII rendering of the materialized issue tree.

The point of this module is to make tree shape visually undeniable: a
flat bag of leaves under the root, an absurd work-unit count, or a
work unit decomposed into child issues should be obvious in one screen.
"""

from __future__ import annotations

from .models import IssueRef, RepoDag, RepoHealth, TreeNode
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


def shape_summary(root: TreeNode, next_label: str | None) -> str:
    """One-line shape digest: the absurdity metrics at a glance."""
    nodes = root.preorder()
    open_nodes = [n for n in nodes if n.issue.is_open]
    groupings = [n for n in open_nodes if is_grouping_issue(n.issue.title)]
    work_units = [n for n in open_nodes if not is_grouping_issue(n.issue.title)]

    def depth(node: TreeNode) -> int:
        return 1 + max((depth(child) for child in node.children), default=0)

    fan_out = len([child for child in root.children if child.issue.is_open])
    next_part = f"next {next_label}" if next_label is not None else "next none"
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


def render_tree(
    root: TreeNode,
    *,
    next_number: int | None = None,
    next_foreign: IssueRef | None = None,
    hidden_count: int = 0,
    dag: RepoDag | None = None,
) -> str:
    """Render exactly the given tree as annotated ASCII with a trailing shape line.

    Visibility filtering is not done here: pass an already-pruned tree and the
    count of nodes it dropped as hidden_count (see prune_closed).

    When ``dag`` is provided, blocked issues are annotated with their open
    direct blockers, and foreign sub-issues render as qualified ``[foreign]``
    leaves at their sibling position under their local parent, with their title
    and state read from their own repository. ``next_foreign`` marks a foreign
    leaf as the next work unit.
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
        # Readiness annotation: show open blockers when dag is available
        if dag is not None and node.issue.is_open:
            from .readiness import ReadinessState, compute_readiness

            readiness = compute_readiness(dag, node.issue.number)
            if readiness.state == ReadinessState.blocked:
                blockers = [f"#{b}" for b in readiness.open_blockers]
                blockers.extend(f"ancestor #{a}" for a in readiness.blocked_ancestors)
                if blockers:
                    parts.append(f"[blocked by {', '.join(blockers)}]")
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
        foreign = dag.foreign_children[node.issue.number] if dag is not None and node.issue.number in dag.foreign_children else ()
        if foreign:
            # Interleave local and foreign children in GitHub's sibling order.
            local = {child.issue.number: child for child in children}
            assert dag is not None
            items: tuple[TreeNode | IssueRef, ...] = tuple(
                ref if ref.repo_ref.slug.casefold() != dag.slug.casefold() else local[ref.number]
                for ref in dag.ordered_children[node.issue.number]
                if ref.repo_ref.slug.casefold() != dag.slug.casefold() or ref.number in local
            )
            overflow = ()
        else:
            items = shown
        for i, item in enumerate(items):
            last = i == len(items) - 1 and not overflow
            branch = "└── " if last else "├── "
            if isinstance(item, TreeNode):
                walk(item, child_prefix, branch)
                continue
            assert dag is not None
            issue = dag.foreign_issues.get(item)
            label = f"{item.slug}  [foreign]" if issue is None else f"{item.slug} {issue.title}  [foreign]{'' if issue.is_open else ' [closed]'}"
            lines.append(f"{child_prefix}{branch}{label}{'  <- next' if item == next_foreign else ''}")
        if overflow:
            numbers = " ".join(f"#{child.issue.number}" for child in overflow)
            lines.append(f"{child_prefix}└── ... {len(overflow)} more: {numbers}")

    walk(root, "", "")

    if hidden_count:
        lines.append("")
        lines.append(f"({hidden_count} closed issue{'s' if hidden_count != 1 else ''} hidden; --all to show)")
    next_label = next_foreign.slug if next_foreign is not None else None if next_number is None else f"#{next_number}"
    lines.append(shape_summary(root, next_label))
    return "\n".join(lines)
