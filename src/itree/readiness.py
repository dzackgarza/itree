"""Native dependency readiness and cycle detection.

The readiness model separates two concerns that ``itree`` previously
conflated:

- **Readiness** — an issue is ready when it is open, has no open direct
  ``blocked_by`` blockers, and every reachable grouping ancestor is also
  ready.  This is a *prerequisite* relation, not a scheduling decision.
- **Selection** — among ready work-unit leaves, preorder traversal is the
  stable deterministic tie-breaker.  ``first_ready_work_unit`` returns the
  first eligible leaf in preorder, exactly one work unit.

Dependencies may point earlier or later in preorder and may cross grouping
branches.  A forward-preorder edge is not an error; only dependency cycles
are structural errors.

All functions in this module are pure: they consume an already-built
``RepoDag`` (constructed by ``traversal.py``) and never touch the network.
"""

from __future__ import annotations

from enum import StrEnum

import networkx as nx
from pydantic import BaseModel, ConfigDict

from .models import RepoDag, TreeNode
from .validate import is_grouping_issue


class ReadinessState(StrEnum):
    ready = "ready"
    blocked = "blocked"


class DependencyErrorKind(StrEnum):
    cycle = "cycle"
    deleted_blocker = "deleted_blocker"


class ReadinessResult(BaseModel):
    """Readiness state for a single issue within a DAG.

    - ``open_blockers`` lists the issue numbers of direct open blockers.
    - ``blocked_ancestors`` lists grouping ancestors that have open blockers,
      making this issue unready even if the issue itself has no direct blocker.
    """

    model_config = ConfigDict(frozen=True)

    state: ReadinessState
    open_blockers: tuple[int, ...] = ()
    blocked_ancestors: tuple[int, ...] = ()


class DependencyError(BaseModel):
    """A structural error in the native dependency graph."""

    model_config = ConfigDict(frozen=True)

    kind: DependencyErrorKind
    witness: tuple[int, ...]


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def compute_readiness(dag: RepoDag, issue_number: int) -> ReadinessResult:
    """Compute the readiness of ``issue_number`` within ``dag``.

    An issue is **ready** when:
    - it is open, and
    - none of its direct ``blocked_by`` blockers are open, and
    - none of its reachable grouping ancestors have an open direct blocker.

    An issue is **blocked** when any of those conditions fail.
    """
    issue = dag.issues.get(issue_number)
    if issue is None:
        return ReadinessResult(
            state=ReadinessState.blocked, open_blockers=(), blocked_ancestors=()
        )

    # Direct blockers: absent blockers are unreadable and therefore unsatisfied.
    direct_blockers: tuple[int, ...] = (
        dag.dependencies[issue_number] if issue_number in dag.dependencies else ()
    )
    open_blockers = tuple(
        b for b in direct_blockers if b not in dag.issues or dag.issues[b].is_open
    )

    # Grouping ancestors with open blockers.
    blocked_ancestors: list[int] = []
    ancestor = dag.parent_of.get(issue_number)
    while ancestor is not None:
        ancestor_issue = dag.issues.get(ancestor)
        if ancestor_issue is None:
            break
        ancestor_blockers: tuple[int, ...] = (
            dag.dependencies[ancestor] if ancestor in dag.dependencies else ()
        )
        ancestor_open_blockers = [
            b for b in ancestor_blockers if b not in dag.issues or dag.issues[b].is_open
        ]
        if not ancestor_issue.is_open or ancestor_open_blockers:
            blocked_ancestors.append(ancestor)
        ancestor = dag.parent_of.get(ancestor)

    if not issue.is_open or open_blockers or blocked_ancestors:
        return ReadinessResult(
            state=ReadinessState.blocked,
            open_blockers=open_blockers,
            blocked_ancestors=tuple(blocked_ancestors),
        )
    return ReadinessResult(state=ReadinessState.ready)


def detect_dependency_errors(dag: RepoDag) -> list[DependencyError]:
    """Detect structural errors in the native dependency graph.

    Currently detects:
    - **Dependency cycles**: a cycle in the ``blocked_by`` graph is a
      structural error.  The witness is the complete cycle path.
    """
    errors: list[DependencyError] = []

    # Build a directed graph from dependencies: blocked -> blocker.
    dep_graph: nx.DiGraph[int] = nx.DiGraph()
    for issue_num, blockers in dag.dependencies.items():
        dep_graph.add_node(issue_num)
        for blocker in blockers:
            if blocker in dag.issues:
                dep_graph.add_edge(issue_num, blocker)
            else:
                # Blocker absent from issues: deleted or inaccessible.
                errors.append(
                    DependencyError(
                        kind=DependencyErrorKind.deleted_blocker,
                        witness=(issue_num, blocker),
                    )
                )

    if not nx.is_directed_acyclic_graph(dep_graph):
        for cycle in nx.simple_cycles(dep_graph):
            errors.append(
                DependencyError(
                    kind=DependencyErrorKind.cycle,
                    witness=tuple(cycle),
                )
            )

    return errors


def first_ready_work_unit(root: TreeNode, dag: RepoDag) -> TreeNode | None:
    """Find the first ready open work-unit leaf in preorder.

    A leaf is eligible when:
    - it is open,
    - it is not a grouping issue,
    - it is ready (no open direct blockers, no blocked grouping ancestors),
    - every reachable grouping ancestor is also ready.

    Preorder is the stable tie-breaker; this function returns exactly one
    work unit or ``None``.
    """
    for node in root.preorder():
        if not node.issue.is_open:
            continue
        if is_grouping_issue(node.issue.title):
            continue
        result = compute_readiness(dag, node.issue.number)
        if result.state == ReadinessState.ready:
            return node
    return None
