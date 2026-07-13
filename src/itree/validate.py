from __future__ import annotations

import re
from typing import NotRequired, TypedDict

import networkx as nx

from .models import (
    AbsentReportRef,
    DoctorMetrics,
    DoctorReport,
    Finding,
    FindingSeverity,
    IssueRef,
    PresentReportRef,
    RepoDag,
    RepoHealth,
    ReportRef,
    ReportStatus,
    TreeNode,
)


class DiagnosticDetails(TypedDict):
    title: str
    severity: FindingSeverity
    ideal_model: NotRequired[str]
    meaning: str
    remediation: list[str]
    maintenance: NotRequired[list[str]]


WARNING_MAINTENANCE = [
    "dispatch issue-itree-maintenance asynchronously; "
    "append this finding code and the selected repair to the root ledger's remediation ledger comment, "
    "then continue substantive work.",
]
ERROR_MAINTENANCE = [
    "dispatch issue-itree-maintenance now; "
    "append the code and selected repair to the root ledger's remediation ledger comment; "
    "await evidence before dependent work. This is not a terminal stop.",
]


DIAGNOSTIC_CATALOG: dict[str, DiagnosticDetails] = {
    "E001": {
        "title": "no_root",
        "severity": "error",
        "ideal_model": "One open parentless `Ledger: ...` issue anchors every planned open issue in one traversal domain.",
        "meaning": "No parentless issue exists, so the repository has no traversal domain.",
        "remediation": [
            '1. Create one root ledger issue: itree init OWNER/REPO "Ledger: OWNER/REPO"',
            "2. Attach every open planned issue under it, directly or through a milestone/backlog ledger.",
            "Do not create multiple ledger issues. A milestone, project, roadmap, or epic is not a second root.",
        ],
        "maintenance": ERROR_MAINTENANCE,
    },
    "E004": {
        "title": "root_not_ledger",
        "severity": "error",
        "ideal_model": "The unique parentless traversal anchor is titled `Ledger: ...` so agents can identify its role without inference.",
        "meaning": "The unique root of the tree is not a ledger (its title does not start with 'Ledger:').",
        "remediation": [
            "1. Rename the root issue title so it starts with 'Ledger:', e.g., 'Ledger: OWNER/REPO'"
        ],
        "maintenance": ERROR_MAINTENANCE,
    },
    "E002": {
        "title": "multiple_root_ledgers",
        "severity": "error",
        "ideal_model": "One root ledger orders all open work; sibling order beneath that root is the repository's traversal order.",
        "meaning": "This is a forest, not a tree. Multiple parentless issues exist in the repository.",
        "remediation": [
            "Run: itree triage OWNER/REPO",
            "It anchors on the 'Ledger:'-titled root and walks every stray through absorb / attach / close, one at a time.",
        ],
        "maintenance": ERROR_MAINTENANCE,
    },
    "E003": {
        "title": "cycle_detected",
        "severity": "error",
        "ideal_model": "Parentage is an acyclic rooted ordered tree, so preorder traversal has one deterministic meaning.",
        "meaning": "A circular dependency exists in the issue hierarchy. Cycles break the poset and prevent traversal.",
        "remediation": [
            "A. Detach one of the edges in the cycle using `itree detach` to break the loop."
        ],
        "maintenance": ERROR_MAINTENANCE,
    },
    "E010": {
        "title": "unreachable_open_issues",
        "severity": "error",
        "ideal_model": "Every open planned issue is reachable from the single root ledger and can therefore be selected by preorder traversal.",
        "meaning": "These issues are not in the repository's deterministic traversal path. An agent following `itree next` will never reach them.",
        "remediation": [
            "Run: itree triage OWNER/REPO",
            "It walks each orphan through absorb / attach / close, one at a time.",
            "Absorb FIRST: an orphan smaller than one PR of work belongs inside an existing work unit, not attached as a new leaf.",
        ],
        "maintenance": ERROR_MAINTENANCE,
    },
    "E011": {
        "title": "parentless_non_root_issues",
        "severity": "error",
        "ideal_model": "Only the root ledger is parentless; every other open planned issue has one parent in the tree.",
        "meaning": "These are accidental roots. They need a parent, a merge, or a close.",
        "remediation": [
            "Run: itree triage OWNER/REPO",
            "It walks each orphan through absorb / attach / close, one at a time.",
        ],
        "maintenance": ERROR_MAINTENANCE,
    },
    "E012": {
        "title": "closed_parent_with_open_descendants",
        "severity": "error",
        "ideal_model": "Open work remains beneath open grouping ancestors so traversal never hides live work behind a closed node.",
        "meaning": "Traversal may skip live work. Reopen the parent or move the descendants.",
        "remediation": [
            "A. Reopen the parent issue.",
            "B. Move or detach the open children so they are attached under an open parent.",
        ],
        "maintenance": ERROR_MAINTENANCE,
    },
    "E013": {
        "title": "duplicate_reachable_issue",
        "severity": "error",
        "ideal_model": "Every reachable issue has exactly one parent, preserving a tree rather than a DAG.",
        "meaning": "Tree invariant is violated: a node has multiple parents in the DAG.",
        "remediation": [
            "A. Detach the issue from one of its multiple parents so it only appears once."
        ],
        "maintenance": ERROR_MAINTENANCE,
    },
    "E014": {
        "title": "dependency_cycle",
        "severity": "error",
        "ideal_model": "Native blocked_by edges express valid hard prerequisites; preorder is the deterministic tie-breaker among ready work. Only dependency cycles are structural errors.",
        "meaning": "A dependency cycle exists in the native blocked_by graph. The cycle prevents any of the involved issues from becoming ready.",
        "remediation": [
            "A. Break the cycle by removing one blocked_by edge using the GitHub UI or API.",
            "B. Restructure the issues so the dependency relation is acyclic.",
        ],
        "maintenance": ERROR_MAINTENANCE,
    },
    "W030": {
        "title": "dead_open_grouping",
        "severity": "warning",
        "ideal_model": "An open grouping ledger either orders live descendants or is explicitly marked deferred for later breakdown.",
        "meaning": "An open grouping issue (milestone/backlog ledger) has no open descendants. It is a stale shelf: traversal gains nothing from it.",
        "remediation": [
            "A. Close the grouping issue if its work is complete.",
            "B. Move live work under it if it is supposed to be active.",
            "C. If it is an intentional long-horizon shelf awaiting breakdown, label it with your "
            "configured deferral label (deferral_label in ~/.config/itree/config.toml; default "
            "'deferred'); doctor then reports it as I010 instead of warning.",
        ],
        "maintenance": WARNING_MAINTENANCE,
    },
    "W020": {
        "title": "depth_near_limit",
        "severity": "warning",
        "ideal_model": "Tree depth stays below GitHub's eight-level sub-issue cap by keeping task lists in work-unit bodies.",
        "meaning": "GitHub supports at most eight nested sub-issue levels; split or flatten before the tree hits the limit.",
        "remediation": [
            "A. Flatten the tree by moving sub-issues to a higher parent.",
            "B. Keep implementation task lists inside the relevant work-unit issue instead of nesting issues for checklist items.",
        ],
        "maintenance": WARNING_MAINTENANCE,
    },
    "E015": {
        "title": "work_unit_decomposed_into_child_issues",
        "severity": "error",
        "ideal_model": "A PR-sized work unit is a leaf; its checklist and proof obligations live in its issue body or comments.",
        "meaning": (
            "A non-organizational issue is a PR-sized work unit. Its stories, proof burdens, "
            "and implementation checklist belong in the issue body/comments, not in child issues."
        ),
        "remediation": [
            "A. Move child issue content into the parent issue body or comments, then close or detach the child issues.",
            "B. If the children are truly separate PR-sized work units, convert the parent into an organizational ledger such as 'Milestone: ...' or 'Backlog: ...'.",
        ],
        "maintenance": ERROR_MAINTENANCE,
    },
    "W040": {
        "title": "milestone_mismatch",
        "severity": "warning",
        "ideal_model": (
            "The root has sibling scope branches: each `Milestone: TITLE` ledger owns release-scoped descendants with native milestone `TITLE`, "
            "while Backlog owns unscoped descendants with no native milestone."
        ),
        "meaning": (
            "A descendant's native milestone conflicts with the scope branch that owns it. Nesting a milestone ledger under Backlog creates this "
            "contradiction: the same descendants are both unscoped and release-scoped."
        ),
        "remediation": [
            "A. Move release-scoped work under its direct-root `Milestone: TITLE` ledger and assign native milestone TITLE.",
            "B. Move unscoped work under Backlog and clear its native milestone.",
            "C. Do not nest a milestone ledger under Backlog; create it directly under the root ledger.",
        ],
        "maintenance": WARNING_MAINTENANCE,
    },
    "W041": {
        "title": "milestone_without_ledger",
        "severity": "warning",
        "ideal_model": "Every release-scoped native GitHub milestone has a matching `Milestone: TITLE` direct child of the root ledger.",
        "meaning": "Create a milestone ledger under the root, or deliberately keep milestones outside tree policy.",
        "remediation": [
            "A. Create a milestone ledger issue under the root ledger representing this milestone.",
            "B. Clear the milestone from the issues if it is not meant to be tracked.",
        ],
        "maintenance": WARNING_MAINTENANCE,
    },
    "W050": {
        "title": "missing_acceptance_criteria",
        "severity": "warning",
        "ideal_model": "Every PR-sized work unit carries its own explicit completion and proof boundary in the issue body.",
        "meaning": "A work-unit issue should not make agents infer completion semantics. Add explicit done criteria to the issue itself.",
        "remediation": [
            'A. Edit the issue body to add a "Done when", "Done Criteria", or "Acceptance Criteria" section.'
        ],
        "maintenance": WARNING_MAINTENANCE,
    },
    "Q001": {
        "title": "too_many_open_work_units",
        "severity": "question",
        "meaning": "More work units are open in parallel than the configured ceiling; claims may be thrashing instead of finishing.",
        "remediation": [
            "1. Finish or close open work units before planning more.",
            "2. Consolidate related work units into one PR-sized unit: itree absorb.",
            "3. If the ceiling is genuinely too low, raise max_open_work_units in ~/.config/itree/config.toml.",
        ],
    },
    "Q002": {
        "title": "work_units_disproportionate_to_code",
        "severity": "question",
        "meaning": "Open work units outnumber what the codebase size supports; planning may be outpacing implementation.",
        "remediation": [
            "1. Consolidate related work units into one PR-sized unit: itree absorb.",
            "2. If the proportion is genuinely wrong, tune loc_per_work_unit in ~/.config/itree/config.toml.",
        ],
    },
    "Q003": {
        "title": "flat_tree",
        "severity": "question",
        "meaning": "Most open issues hang directly off the root ledger; the tree has no grouping structure to order traversal.",
        "remediation": [
            "1. Group related issues under milestone or backlog ledgers: itree move ISSUE --under LEDGER.",
            "2. If flat is intended for this repo, tune flat_children_ratio / flat_min_children in ~/.config/itree/config.toml.",
        ],
    },
    "I001": {
        "title": "valid_tree",
        "severity": "info",
        "meaning": "Traversal is deterministic. The reported next work-unit issue is safe for agents.",
        "remediation": [],
    },
    "I010": {
        "title": "deferred_grouping",
        "severity": "info",
        "meaning": "An intentionally deferred grouping (carrying the configured deferral label) has no open descendants yet. "
        "It is a long-horizon shelf awaiting breakdown once it becomes the next item, not a stale one.",
        "remediation": [],
    },
}


def issue_only_dag(dag: RepoDag) -> RepoDag:
    """Return the repository issue DAG with GitHub pull request records removed."""
    issues = {
        number: issue
        for number, issue in dag.issues.items()
        if not issue.is_pull_request
    }
    children_of = {
        parent: tuple(child for child in children if child in issues)
        for parent, children in dag.children_of.items()
        if parent in issues and any(child in issues for child in children)
    }
    dependencies = {
        issue: tuple(blocker for blocker in blockers if blocker in issues)
        for issue, blockers in dag.dependencies.items()
        if issue in issues and any(blocker in issues for blocker in blockers)
    }
    return RepoDag(
        repo_ref=dag.repo_ref,
        issues=issues,
        children_of=children_of,
        dependencies=dependencies,
    )


def parse_milestone_ledger_name(title: str) -> str | None:
    m = re.match(r"(?i)^milestone:\s*(?P<name>.+)$", title)
    if m:
        return m.group("name").strip()
    return None


def is_backlog_ledger(title: str) -> bool:
    return title.lower().startswith("backlog")


def is_root_ledger(title: str) -> bool:
    return title.lower().startswith("ledger:")


def is_roadmap_issue(title: str) -> bool:
    return title.lower().startswith("roadmap:")


def is_phase_issue(title: str) -> bool:
    return title.lower().startswith("phase:")


def is_grouping_issue(title: str) -> bool:
    return (
        is_root_ledger(title)
        or parse_milestone_ledger_name(title) is not None
        or is_backlog_ledger(title)
        or is_roadmap_issue(title)
        or is_phase_issue(title)
    )


def lacks_acceptance_criteria(body: str | None) -> bool:
    if not body:
        return True
    body_lower = body.lower()
    return not (
        "done when" in body_lower
        or "done criteria" in body_lower
        or "acceptance criteria" in body_lower
        or "acceptance" in body_lower
    )


def first_open_work_unit(root: TreeNode, dag: RepoDag | None = None) -> TreeNode | None:
    """Find the first open work-unit leaf in preorder, optionally filtering by readiness.

    When ``dag`` is provided, leaves are filtered by native dependency readiness:
    a leaf is eligible only when it and every reachable grouping ancestor have
    no open ``blocked_by`` blocker.  Preorder remains the deterministic
    tie-breaker; this function returns exactly one work unit or ``None``.

    When ``dag`` is ``None``, behavior is unchanged from the original pure-preorder
    selection (backwards-compatible for callers that do not yet pass the DAG).
    """
    if dag is not None:
        from .readiness import ReadinessState, compute_readiness

        for node in root.preorder():
            if not node.issue.is_open:
                continue
            if is_grouping_issue(node.issue.title):
                continue
            result = compute_readiness(dag, node.issue.number)
            if result.state == ReadinessState.ready:
                return node
        return None

    for node in root.preorder():
        if not node.issue.is_open:
            continue
        if is_grouping_issue(node.issue.title):
            continue
        return node
    return None


def find_root_ledger_candidates(dag: RepoDag) -> list[int]:
    """Discover root candidates structurally: parentless OPEN issues.

    Closed issues are part of the DAG (their subtrees matter for E012 and
    history rendering) but a closed parentless issue is finished work, not
    a traversal root.
    """
    dag = issue_only_dag(dag)
    G: nx.DiGraph[int] = nx.DiGraph()
    G.add_nodes_from(dag.issues.keys())
    for parent, children in dag.children_of.items():
        for child in children:
            G.add_edge(parent, child)

    return [n for n in G.nodes if G.in_degree(n) == 0 and dag.issues[n].is_open]


# Root-shape diagnostics, in the order the scan reports them as root_status.
ROOT_STATUS_CODES = ("E001", "E002", "E004")


def repo_health(dag: RepoDag, deferral_label: str = "deferred") -> RepoHealth:
    """Condense a repo's issue DAG into one account-scan health digest.

    Config is read at the CLI command boundary (once per invocation) and the
    resolved deferral_label is passed in, so a concurrent account scan does not
    re-read config once per repo.
    """
    report = generate_doctor_report(dag, deferral_label=deferral_label)
    codes = {f.code for f in report.findings}
    root_status = next((code for code in ROOT_STATUS_CODES if code in codes), "ok")
    return RepoHealth(
        slug=dag.slug,
        open_issues=sum(1 for issue in dag.issues.values() if issue.is_open),
        root_status=root_status,
        error_count=report.metrics.errors,
        next_work_unit=report.next_issue,
    )


def generate_doctor_report(
    dag: RepoDag, deferral_label: str = "deferred"
) -> DoctorReport:
    dag = issue_only_dag(dag)
    findings_list: list[Finding] = []

    # 1. Build networkx directed graph
    G: nx.DiGraph[int] = nx.DiGraph()
    G.add_nodes_from(dag.issues.keys())
    for parent, children in dag.children_of.items():
        for child in children:
            G.add_edge(parent, child)

    # 2. Cycle detection (Is it a DAG?)
    is_acyclic = nx.is_directed_acyclic_graph(G)
    if not is_acyclic:
        f_details = DIAGNOSTIC_CATALOG["E003"]
        cycles = list(nx.simple_cycles(G))
        evidence = [
            f"dependency cycle: {' -> '.join(f'#{num}' for num in cycle)}"
            for cycle in cycles
        ]
        findings_list.append(
            Finding(
                code="E003",
                severity="error",
                title=f_details["title"],
                evidence=evidence,
                meaning=f_details["meaning"],
                remediation=f_details["remediation"],
            )
        )

    # 3. Discover candidates (parentless nodes in the DAG)
    candidates = find_root_ledger_candidates(dag)

    root_ref: IssueRef | None = None
    next_issue_ref: ReportRef = AbsentReportRef(reason="no_open_work_unit")

    # Tree-scoped counters; zero when no acyclic root exists to measure.
    open_reachable_count = 0
    open_outside_count = 0
    open_work_unit_count = 0
    work_unit_count = 0
    max_depth_count = 0

    if len(candidates) == 0:
        f_details = DIAGNOSTIC_CATALOG["E001"]
        findings_list.append(
            Finding(
                code="E001",
                severity="error",
                title=f_details["title"],
                evidence=["no root candidate found"],
                meaning=f_details["meaning"],
                remediation=f_details["remediation"],
            )
        )
    elif len(candidates) > 1:
        f_details = DIAGNOSTIC_CATALOG["E002"]
        evidence = [f"#{num}  {dag.issues[num].title}" for num in sorted(candidates)]
        findings_list.append(
            Finding(
                code="E002",
                severity="error",
                title=f_details["title"],
                evidence=evidence,
                meaning=f_details["meaning"],
                remediation=f_details["remediation"],
            )
        )
        # Choose the first one to continue analysis
        root_num = sorted(candidates)[0]
        root_ref = IssueRef(repo_ref=dag.repo_ref, number=root_num)
    else:
        root_num = candidates[0]
        root_issue = dag.issues[root_num]

        # Check if the root issue is a ledger (starts with Ledger:)
        if not is_root_ledger(root_issue.title):
            f_details = DIAGNOSTIC_CATALOG["E004"]
            findings_list.append(
                Finding(
                    code="E004",
                    severity="error",
                    title=f_details["title"],
                    evidence=[
                        f"Root issue #{root_num} \"{root_issue.title}\" title must start with 'Ledger:'"
                    ],
                    meaning=f_details["meaning"],
                    remediation=f_details["remediation"],
                )
            )

        root_ref = IssueRef(repo_ref=dag.repo_ref, number=root_num)

    # Tree-dependent checks require an acyclic graph: materializing the root
    # recurses through children_of and would not terminate on a cycle. E003
    # above already reports the cycles.
    if root_ref is not None and is_acyclic:
        root_num = root_ref.number

        # Build tree node
        tree_node = dag.materialize_root(root_num)
        tree_nodes = tree_node.preorder()

        # Connectivity check: reachable nodes from root ledger
        reachable = nx.descendants(G, root_num) | {root_num}

        # Collect open issues outside the root ledger
        unreachable_open = []
        for num, issue in sorted(dag.issues.items()):
            if issue.is_open and num not in reachable and num != root_num:
                unreachable_open.append(num)

        # E010: unreachable open issues
        if unreachable_open:
            f_details = DIAGNOSTIC_CATALOG["E010"]
            evidence = [f'#{num} "{dag.issues[num].title}"' for num in unreachable_open]
            findings_list.append(
                Finding(
                    code="E010",
                    severity="error",
                    title=f_details["title"],
                    evidence=evidence,
                    meaning=f_details["meaning"],
                    remediation=f_details["remediation"],
                )
            )

        # E011: parentless non-root open issues
        parentless_non_root = []
        for num, issue in sorted(dag.issues.items()):
            if issue.is_open and num != root_num and G.in_degree(num) == 0:
                parentless_non_root.append(num)

        if parentless_non_root:
            f_details = DIAGNOSTIC_CATALOG["E011"]
            evidence = [
                f'#{num} "{dag.issues[num].title}"' for num in parentless_non_root
            ]
            findings_list.append(
                Finding(
                    code="E011",
                    severity="error",
                    title=f_details["title"],
                    evidence=evidence,
                    meaning=f_details["meaning"],
                    remediation=f_details["remediation"],
                )
            )

        # E013: duplicate parent / multiple parents (in-degree > 1 in G)
        multiple_parents = [num for num in sorted(reachable) if G.in_degree(num) > 1]
        if multiple_parents:
            f_details = DIAGNOSTIC_CATALOG["E013"]
            evidence = [
                f"issue #{num} has multiple parent edges" for num in multiple_parents
            ]
            findings_list.append(
                Finding(
                    code="E013",
                    severity="error",
                    title=f_details["title"],
                    evidence=evidence,
                    meaning=f_details["meaning"],
                    remediation=f_details["remediation"],
                )
            )

        # E012: closed parent with open descendants
        closed_with_open_descendants = []
        for num, issue in sorted(dag.issues.items()):
            if not issue.is_open:
                visited: set[int] = set()
                open_desc: list[int] = []

                def find_open(n: int) -> None:
                    if n in visited:
                        return
                    visited.add(n)
                    for kid in dag.children_of[n]:
                        k_issue = dag.issues[kid]
                        if k_issue.is_open:
                            open_desc.append(kid)
                        find_open(kid)

                find_open(num)
                if open_desc:
                    closed_with_open_descendants.append((num, open_desc))

        if closed_with_open_descendants:
            f_details = DIAGNOSTIC_CATALOG["E012"]
            evidence = [
                f"closed #{p_num} hides open descendants: {', '.join(f'#{c}' for c in kids)}"
                for p_num, kids in closed_with_open_descendants
            ]
            findings_list.append(
                Finding(
                    code="E012",
                    severity="error",
                    title=f_details["title"],
                    evidence=evidence,
                    meaning=f_details["meaning"],
                    remediation=f_details["remediation"],
                )
            )

        # E014: dependency cycle detection (valid acyclic dependencies are accepted)
        from .readiness import detect_dependency_errors

        dep_errors = detect_dependency_errors(dag)
        if dep_errors:
            f_details = DIAGNOSTIC_CATALOG["E014"]
            evidence = [
                f"dependency cycle: {' -> '.join(f'#{n}' for n in err.witness)}"
                for err in dep_errors
                if err.kind.value == "cycle"
            ]
            if evidence:
                findings_list.append(
                    Finding(
                        code="E014",
                        severity="error",
                        title=f_details["title"],
                        evidence=evidence,
                        meaning=f_details["meaning"],
                        remediation=f_details["remediation"],
                    )
                )

        # Max depth check
        max_depth = 0

        def calculate_depths(node: TreeNode, current_depth: int) -> None:
            nonlocal max_depth
            if current_depth > max_depth:
                max_depth = current_depth
            for child in node.children:
                calculate_depths(child, current_depth + 1)

        calculate_depths(tree_node, 0)

        if max_depth >= 6:
            f_details = DIAGNOSTIC_CATALOG["W020"]
            findings_list.append(
                Finding(
                    code="W020",
                    severity="warning",
                    title=f_details["title"],
                    evidence=[f"materialized tree depth is {max_depth + 1}"],
                    meaning=f_details["meaning"],
                    remediation=f_details["remediation"],
                )
            )

        # Find grouping issues, work units, and milestones.
        work_unit_nodes: list[TreeNode] = []
        milestone_ledgers_in_tree = set()
        milestone_ledger_names = set()
        backlog_ledgers_in_tree = set()

        for node in tree_nodes:
            title = node.issue.title
            m_name = parse_milestone_ledger_name(title)
            if m_name is not None:
                milestone_ledgers_in_tree.add(node)
                milestone_ledger_names.add(m_name)
            elif is_backlog_ledger(title):
                backlog_ledgers_in_tree.add(node)

            if node.issue.is_open and not is_grouping_issue(title):
                work_unit_nodes.append(node)

        # W030: open grouping issue (other than the root) with no open descendants.
        # A grouping carrying the deferral label is an intentional long-horizon
        # shelf awaiting breakdown, not a dead one: it is reported as I010 instead.
        dead_groupings = []
        deferred_groupings = []
        for node in tree_nodes[1:]:
            if (
                node.issue.is_open
                and is_grouping_issue(node.issue.title)
                and not any(d.issue.is_open for d in node.descendants())
            ):
                if deferral_label.casefold() in {
                    label.casefold() for label in node.issue.labels
                }:
                    deferred_groupings.append(
                        f'#{node.issue.number} "{node.issue.title}" is deferred, awaiting breakdown'
                    )
                else:
                    dead_groupings.append(
                        f'#{node.issue.number} "{node.issue.title}" has no open descendants'
                    )

        if dead_groupings:
            f_details = DIAGNOSTIC_CATALOG["W030"]
            findings_list.append(
                Finding(
                    code="W030",
                    severity="warning",
                    title=f_details["title"],
                    evidence=dead_groupings,
                    meaning=f_details["meaning"],
                    remediation=f_details["remediation"],
                )
            )

        if deferred_groupings:
            f_details = DIAGNOSTIC_CATALOG["I010"]
            findings_list.append(
                Finding(
                    code="I010",
                    severity="info",
                    title=f_details["title"],
                    evidence=deferred_groupings,
                    meaning=f_details["meaning"],
                    remediation=f_details["remediation"],
                )
            )

        # W041: active milestone without milestone ledger
        active_milestones = set()
        for issue in dag.issues.values():
            if issue.is_open and issue.milestone is not None:
                active_milestones.add(issue.milestone.title)

        missing_milestones = sorted(active_milestones - milestone_ledger_names)
        if missing_milestones:
            f_details = DIAGNOSTIC_CATALOG["W041"]
            evidence = [
                f'milestone "{m}" is active but has no ledger child'
                for m in missing_milestones
            ]
            findings_list.append(
                Finding(
                    code="W041",
                    severity="warning",
                    title=f_details["title"],
                    evidence=evidence,
                    meaning=f_details["meaning"],
                    remediation=f_details["remediation"],
                )
            )

        # W040: milestone mismatch check
        mismatch_issues = []
        for ml_node in milestone_ledgers_in_tree:
            m_name = parse_milestone_ledger_name(ml_node.issue.title)
            for desc in ml_node.descendants():
                if desc.issue.milestone is None or desc.issue.milestone.title != m_name:
                    mismatch_issues.append(
                        (
                            desc.issue.number,
                            desc.issue.title,
                            m_name,
                            desc.issue.milestone.title
                            if desc.issue.milestone
                            else "None",
                        )
                    )

        for bl_node in backlog_ledgers_in_tree:
            for desc in bl_node.descendants():
                if desc.issue.milestone is not None:
                    mismatch_issues.append(
                        (
                            desc.issue.number,
                            desc.issue.title,
                            "None",
                            desc.issue.milestone.title,
                        )
                    )

        if mismatch_issues:
            f_details = DIAGNOSTIC_CATALOG["W040"]
            evidence = [
                f'#{num} "{title}" expected milestone {expected}, got {actual}'
                for num, title, expected, actual in mismatch_issues
            ]
            findings_list.append(
                Finding(
                    code="W040",
                    severity="warning",
                    title=f_details["title"],
                    evidence=evidence,
                    meaning=f_details["meaning"],
                    remediation=f_details["remediation"],
                )
            )

        # E015: a work-unit issue must not be decomposed into child issues.
        decomposed_work_units = []
        for wu in sorted(work_unit_nodes, key=lambda w: w.issue.number):
            child_issues = [child for child in wu.children if child.issue.is_open]
            if child_issues:
                child_refs = ", ".join(
                    f"#{child.issue.number}" for child in child_issues
                )
                decomposed_work_units.append(
                    f"work unit #{wu.issue.number} has child issues: {child_refs}"
                )

        if decomposed_work_units:
            f_details = DIAGNOSTIC_CATALOG["E015"]
            findings_list.append(
                Finding(
                    code="E015",
                    severity="error",
                    title=f_details["title"],
                    evidence=decomposed_work_units,
                    meaning=f_details["meaning"],
                    remediation=f_details["remediation"],
                )
            )

        # W050: work-unit issues own their acceptance/proof boundary.
        missing_ac_findings = []
        open_work_units = work_unit_nodes

        for work_unit in sorted(work_unit_nodes, key=lambda w: w.issue.number):
            if lacks_acceptance_criteria(work_unit.issue.body):
                missing_ac_findings.append(
                    f"work unit #{work_unit.issue.number} lacks acceptance criteria"
                )

        if missing_ac_findings:
            f_details = DIAGNOSTIC_CATALOG["W050"]
            findings_list.append(
                Finding(
                    code="W050",
                    severity="warning",
                    title=f_details["title"],
                    evidence=missing_ac_findings,
                    meaning=f_details["meaning"],
                    remediation=f_details["remediation"],
                )
            )

        # Populate tree-scoped counters
        open_reachable_count = len([n for n in tree_nodes if n.issue.is_open])
        open_outside_count = len(unreachable_open)
        open_work_unit_count = len(open_work_units)
        work_unit_count = len(work_unit_nodes)
        max_depth_count = max_depth + 1

        next_node = first_open_work_unit(tree_node, dag)
        if next_node:
            next_issue = IssueRef(repo_ref=dag.repo_ref, number=next_node.issue.number)
            next_issue_ref = PresentReportRef(ref=next_issue)

    # Determine status
    errors_count = sum(1 for f in findings_list if f.severity == "error")
    warnings_count = sum(1 for f in findings_list if f.severity == "warning")

    metrics = DoctorMetrics(
        errors=errors_count,
        warnings=warnings_count,
        open_issues_reachable_from_root=open_reachable_count,
        open_issues_outside_root=open_outside_count,
        open_work_units=open_work_unit_count,
        work_units=work_unit_count,
        max_depth=max_depth_count,
    )

    status: ReportStatus
    if errors_count > 0:
        status = "error"
    elif warnings_count > 0:
        status = "warning"
    else:
        status = "ok"
        f_details = DIAGNOSTIC_CATALOG["I001"]
        findings_list.append(
            Finding(
                code="I001",
                severity="info",
                title=f_details["title"],
                evidence=[],
                meaning=f_details["meaning"],
                remediation=[],
            )
        )

    report_root = (
        PresentReportRef(ref=root_ref)
        if root_ref is not None
        else AbsentReportRef(reason="no_root_ledger")
    )
    return DoctorReport(
        repo=dag.slug,
        status=status,
        root=report_root,
        next_issue=next_issue_ref,
        metrics=metrics,
        findings=findings_list,
    )
