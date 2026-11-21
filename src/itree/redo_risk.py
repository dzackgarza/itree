"""Q006: work that ``next`` can hand out before a large unit rewrites its surface.

A large work unit (``complexity:high``, ``size:l`` or ``size:xl``; see the
ai-review-ci label-routing taxonomy) tends to rewrite the surface it touches.
Another open work unit on the same surface is wasted if ``next`` selects it
first. The tree records that relation natively: absorb the smaller unit,
supersede it, or add a ``blocked_by`` edge in either direction. Q006 names the
pairs where none of those exist.

The shared surface is a repository file path named in both issue bodies. A
path is the concrete evidence an agent can check; topical similarity is not.
"""

from __future__ import annotations

import re

import networkx as nx

from .audit import _finding
from .models import AuditFindingWitness, Finding, IssueRef, RepoDag, TreeNode
from .readiness import ReadinessState, compute_readiness

LARGE_UNIT_LABELS = frozenset({"complexity:high", "size:l", "size:xl"})

# A relative path with at least one directory and a file extension. The
# lookbehind rejects a match that starts inside a URL or a longer path.
_PATH = re.compile(r"(?<![\w/.:-])((?:[\w-][\w.-]*/)+[\w-][\w.-]*\.[A-Za-z0-9]+)(?![\w/])")


def named_paths(body: str | None) -> frozenset[str]:
    """File paths named in an issue body."""
    return frozenset(_PATH.findall(body or ""))


def redo_risk_questions(dag: RepoDag, work_units: list[TreeNode]) -> list[Finding]:
    """Q006 for each (earlier-selectable unit, large unit) pair on a shared file.

    ``work_units`` are the open work units of the tree in preorder, the order
    ``next`` walks. A unit X is selectable while large unit Y is open when no
    blocker chain links them in either direction and X precedes Y in preorder
    or Y is not ready.
    """
    blockers: nx.DiGraph[int] = nx.DiGraph()
    for issue, blocked_by in dag.dependencies.items():
        blockers.add_edges_from((issue, blocker) for blocker in blocked_by)

    paths = {unit.issue.number: named_paths(unit.issue.body) for unit in work_units}
    blockers.add_nodes_from(paths)

    def linked(a: int, b: int) -> bool:
        return nx.has_path(blockers, a, b) or nx.has_path(blockers, b, a)

    position = {unit.issue.number: index for index, unit in enumerate(work_units)}
    evidence: list[str] = []
    witnesses: list[AuditFindingWitness] = []
    for large in work_units:
        y = large.issue.number
        if not LARGE_UNIT_LABELS & set(large.issue.labels):
            continue
        large_ready = compute_readiness(dag, y).state == ReadinessState.ready
        for unit in work_units:
            x = unit.issue.number
            shared = paths[x] & paths[y]
            if x == y or not shared or linked(x, y):
                continue
            if position[x] > position[y] and large_ready:
                continue
            evidence.append(f"#{x} can be selected before large unit #{y}; both name {', '.join(sorted(shared))}")
            witnesses.append(
                AuditFindingWitness(
                    edge_chain=(IssueRef(repo_ref=dag.repo_ref, number=x), IssueRef(repo_ref=dag.repo_ref, number=y)),
                    unresolved_burden="absorb, supersede, or add a blocked_by edge",
                )
            )
    if not evidence:
        return []
    return [_finding("Q006", evidence, witnesses=tuple(witnesses))]
