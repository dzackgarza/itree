from __future__ import annotations

import re

import networkx as nx
from pydantic import BaseModel

from .models import DoctorReport, Finding, IssueRef, RepoDag, TreeNode

DIAGNOSTIC_CATALOG = {
    "E001": {
        "title": "no_root_ledger",
        "severity": "error",
        "meaning": "The root of the issue tree is not a ledger (its title does not start with 'Ledger:').",
        "remediation": ["1. Rename the root issue title so it starts with 'Ledger:', e.g., 'Ledger: OWNER/REPO'"],
    },
    "E002": {
        "title": "multiple_root_ledgers",
        "severity": "error",
        "meaning": "This is a forest, not a tree. Multiple parentless issues exist in the repository.",
        "remediation": ["1. Choose exactly one issue as the root ledger.", "2. Attach all other parentless issues as descendants of that root ledger."],
    },
    "E003": {
        "title": "cycle_detected",
        "severity": "error",
        "meaning": "A circular dependency exists in the issue hierarchy. Cycles break the poset and prevent traversal.",
        "remediation": ["A. Detach one of the edges in the cycle using `itree detach` to break the loop."],
    },
    "E010": {
        "title": "unreachable_open_issues",
        "severity": "error",
        "meaning": "These issues are not in the repository's deterministic traversal path. An agent following `itree next` will never reach them.",
        "remediation": [
            "A. If the issue belongs to existing work:",
            "     attach it under the root ledger or a milestone/backlog ledger.",
            "B. If the issue is broad:",
            "     keep it as the work-unit issue and put stories, proof burdens, and implementation tasks in its body/comments.",
            "C. If the issue is future work:",
            "     attach it under the Backlog ledger, which must itself be a child of root.",
            "D. If the issue is stale or not planned:",
            "     close it as not planned.",
            "Do not leave it parentless. Parentless open issues are accidental roots.",
        ],
    },
    "E011": {
        "title": "parentless_non_root_issues",
        "severity": "error",
        "meaning": "These are accidental roots. Investigate intended parentage and attach them under the ledger.",
        "remediation": [
            "A. Attach this issue under the root ledger or a milestone/backlog ledger.",
            "B. Close the issue if it is no longer planned.",
        ],
    },
    "E012": {
        "title": "closed_parent_with_open_descendants",
        "severity": "error",
        "meaning": "Traversal may skip live work. Reopen the parent or move the descendants.",
        "remediation": ["A. Reopen the parent issue.", "B. Move or detach the open children so they are attached under an open parent."],
    },
    "E013": {
        "title": "duplicate_reachable_issue",
        "severity": "error",
        "meaning": "Tree invariant is violated: a node has multiple parents in the DAG.",
        "remediation": ["A. Detach the issue from one of its multiple parents so it only appears once."],
    },
    "E014": {
        "title": "dependency_edges_present",
        "severity": "error",
        "meaning": "This model does not use DAG scheduling. Move blockers earlier in preorder or make the blocker its own ordered work-unit issue.",
        "remediation": ["A. Remove the blocked_by dependency using the GitHub UI or API.", "B. Reorder the issues in the tree so the blocker appears earlier in preorder."],
    },
    "W020": {
        "title": "depth_near_limit",
        "severity": "warning",
        "meaning": "GitHub supports at most eight nested sub-issue levels; split or flatten before the tree hits the limit.",
        "remediation": [
            "A. Flatten the tree by moving sub-issues to a higher parent.",
            "B. Keep implementation task lists inside the relevant work-unit issue instead of nesting issues for checklist items.",
        ],
    },
    "E015": {
        "title": "work_unit_decomposed_into_child_issues",
        "severity": "error",
        "meaning": (
            "A non-organizational issue is a PR-sized work unit. Its stories, proof burdens, "
            "and implementation checklist belong in the issue body/comments, not in child issues."
        ),
        "remediation": [
            "A. Move child issue content into the parent issue body or comments, then close or detach the child issues.",
            "B. If the children are truly separate PR-sized work units, convert the parent into an organizational ledger such as 'Milestone: ...' or 'Backlog: ...'.",
        ],
    },
    "W040": {
        "title": "milestone_mismatch",
        "severity": "warning",
        "meaning": (
            "The tree defines traversal. The GitHub milestone defines release/time grouping. They should normally agree so Projects and milestone progress views remain useful."
        ),
        "remediation": [
            "- Move the issue to the correct milestone ledger, or",
            "- Set its GitHub milestone to match the ledger, or",
            "- Move it to Backlog if it is not release-scoped.",
        ],
    },
    "W041": {
        "title": "milestone_without_ledger",
        "severity": "warning",
        "meaning": "Create a milestone ledger under the root, or deliberately keep milestones outside tree policy.",
        "remediation": [
            "A. Create a milestone ledger issue under the root ledger representing this milestone.",
            "B. Clear the milestone from the issues if it is not meant to be tracked.",
        ],
    },
    "W050": {
        "title": "missing_acceptance_criteria",
        "severity": "warning",
        "meaning": "A work-unit issue should not make agents infer completion semantics. Add explicit done criteria to the issue itself.",
        "remediation": ['A. Edit the issue body to add a "Done when", "Done Criteria", or "Acceptance Criteria" section.'],
    },
    "I001": {
        "title": "valid_tree",
        "severity": "info",
        "meaning": "Traversal is deterministic. The reported next work-unit issue is safe for agents.",
        "remediation": [],
    },
}


class TreeViolation(BaseModel):
    code: str
    message: str
    issue_number: int | None = None


def issue_only_dag(dag: RepoDag) -> RepoDag:
    """Return the repository issue DAG with GitHub pull request records removed."""
    issues = {number: issue for number, issue in dag.issues.items() if not issue.is_pull_request}
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


def validate_dag(dag: RepoDag) -> list[TreeViolation]:
    """Validate DAG-level structural invariants using networkx."""
    dag = issue_only_dag(dag)
    violations: list[TreeViolation] = []

    # 1. Build directed graph
    G = nx.DiGraph()
    G.add_nodes_from(dag.issues.keys())
    for parent, children in dag.children_of.items():
        for child in children:
            G.add_edge(parent, child)

    # 2. Check if it's a DAG (acyclic)
    if not nx.is_directed_acyclic_graph(G):
        cycles = list(nx.simple_cycles(G))
        for cycle in cycles:
            violations.append(
                TreeViolation(
                    code="cycle_detected",
                    message=f"dependency cycle detected: {' -> '.join(f'#{num}' for num in cycle)}",
                )
            )

    # 3. Find parentless nodes (roots of the DAG)
    roots = [n for n in G.nodes if G.in_degree(n) == 0]

    # If there are multiple roots (forest)
    if len(roots) > 1:
        root_numbers = ", ".join(f"#{r}" for r in sorted(roots))
        violations.append(
            TreeViolation(
                code="fragmented_forest",
                message=f"graph has {len(roots)} roots ({root_numbers}) — not a single tree",
            )
        )

    # 4. Check connectivity / orphans
    candidates = find_root_ledger_candidates(dag)
    if candidates:
        primary_root = candidates[0]
        if nx.is_directed_acyclic_graph(G):
            reachable = nx.descendants(G, primary_root) | {primary_root}
        else:
            reachable = set(nx.bfs_tree(G.to_undirected(), primary_root).nodes) if primary_root in G else {primary_root}

        for num in sorted(dag.issues.keys()):
            if num not in reachable:
                violations.append(
                    TreeViolation(
                        code="orphaned_issue",
                        message=f'issue #{num} "{dag.issues[num].title}" is not reachable from the primary root ledger #{primary_root}',
                        issue_number=num,
                    )
                )
    else:
        for num in sorted(dag.issues.keys()):
            if num not in roots:
                violations.append(
                    TreeViolation(
                        code="orphaned_issue",
                        message=f'issue #{num} "{dag.issues[num].title}" is not reachable from any root',
                        issue_number=num,
                    )
                )

    return violations


def validate_tree(root: TreeNode) -> list[TreeViolation]:
    """Validate tree invariants (stub for backward compatibility)."""
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


def parse_milestone_ledger_name(title: str) -> str | None:
    m = re.match(r"(?i)^milestone:\s*(?P<name>.+)$", title)
    if m:
        return m.group("name").strip()
    return None


def is_backlog_ledger(title: str) -> bool:
    return title.lower().startswith("backlog")


def is_root_ledger(title: str) -> bool:
    return title.lower().startswith("ledger:")


def is_grouping_issue(title: str) -> bool:
    return is_root_ledger(title) or parse_milestone_ledger_name(title) is not None or is_backlog_ledger(title)


def lacks_acceptance_criteria(body: str | None) -> bool:
    if not body:
        return True
    body_lower = body.lower()
    return not ("done when" in body_lower or "done criteria" in body_lower or "acceptance criteria" in body_lower or "acceptance" in body_lower)


def first_open_work_unit(root: TreeNode) -> TreeNode | None:
    for node in root.preorder():
        if not node.issue.is_open:
            continue
        if is_grouping_issue(node.issue.title):
            continue
        return node
    return None


def find_root_ledger_candidates(dag: RepoDag) -> list[int]:
    # The root is discovered structurally by finding parentless nodes in the issue DAG
    dag = issue_only_dag(dag)
    import networkx as nx

    G = nx.DiGraph()
    G.add_nodes_from(dag.issues.keys())
    for parent, children in dag.children_of.items():
        for child in children:
            G.add_edge(parent, child)

    roots = [n for n in G.nodes if G.in_degree(n) == 0]
    return roots


def generate_doctor_report(dag: RepoDag) -> DoctorReport:
    dag = issue_only_dag(dag)
    findings_list: list[Finding] = []

    # 1. Build networkx directed graph
    G = nx.DiGraph()
    G.add_nodes_from(dag.issues.keys())
    for parent, children in dag.children_of.items():
        for child in children:
            G.add_edge(parent, child)

    # 2. Cycle detection (Is it a DAG?)
    is_acyclic = nx.is_directed_acyclic_graph(G)
    if not is_acyclic:
        f_details = DIAGNOSTIC_CATALOG["E003"]
        cycles = list(nx.simple_cycles(G))
        evidence = [f"dependency cycle: {' -> '.join(f'#{num}' for num in cycle)}" for cycle in cycles]
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

    root_ref = None
    next_issue_ref = None
    enclosing_wu_ref = None

    # Metrics dictionary baseline
    metrics = {
        "errors": 0,
        "warnings": 0,
        "open issues reachable from root": 0,
        "open issues outside root": 0,
        "open work units": 0,
        "work units": 0,
        "max depth": 0,
    }

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
        if not root_issue.title.lower().startswith("ledger:"):
            f_details = DIAGNOSTIC_CATALOG["E001"]
            findings_list.append(
                Finding(
                    code="E001",
                    severity="error",
                    title=f_details["title"],
                    evidence=[f"Root issue #{root_num} \"{root_issue.title}\" title must start with 'Ledger:'"],
                    meaning="The unique root of the tree must be a root ledger (title starts with 'Ledger:').",
                    remediation=[f"Rename issue #{root_num} title to start with 'Ledger:'"],
                )
            )

        root_ref = IssueRef(repo_ref=dag.repo_ref, number=root_num)

    if root_ref is not None:
        root_num = root_ref.number

        # Build tree node
        tree_node = dag.materialize_root(root_num)
        tree_nodes = tree_node.preorder()

        # Connectivity check: reachable nodes from root ledger
        if is_acyclic:
            reachable = nx.descendants(G, root_num) | {root_num}
        else:
            reachable = set(nx.bfs_tree(G.to_undirected(), root_num).nodes) if root_num in G else {root_num}

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
            evidence = [f'#{num} "{dag.issues[num].title}"' for num in parentless_non_root]
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
            evidence = [f"issue #{num} has multiple parent edges" for num in multiple_parents]
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
                visited = set()
                open_desc = []

                def find_open(n):
                    if n in visited:
                        return
                    visited.add(n)
                    for kid in dag.children_of.get(n, ()):
                        k_issue = dag.issues.get(kid)
                        if k_issue:
                            if k_issue.is_open:
                                open_desc.append(kid)
                            find_open(kid)

                find_open(num)
                if open_desc:
                    closed_with_open_descendants.append((num, open_desc))

        if closed_with_open_descendants:
            f_details = DIAGNOSTIC_CATALOG["E012"]
            evidence = [f"closed #{p_num} hides open descendants: {', '.join(f'#{c}' for c in kids)}" for p_num, kids in closed_with_open_descendants]
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

        # E014: dependency edges present
        dependency_issues = []
        for num, blockers in sorted(dag.dependencies.items()):
            if blockers:
                dependency_issues.append((num, blockers))

        if dependency_issues:
            f_details = DIAGNOSTIC_CATALOG["E014"]
            evidence = [f"issue #{num} blocked by: {', '.join(f'#{b}' for b in blockers)}" for num, blockers in dependency_issues]
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

        def calculate_depths(node: TreeNode, current_depth: int):
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

        # W041: active milestone without milestone ledger
        active_milestones = set()
        for issue in dag.issues.values():
            if issue.is_open and issue.milestone is not None:
                active_milestones.add(issue.milestone.title)

        missing_milestones = sorted(active_milestones - milestone_ledger_names)
        if missing_milestones:
            f_details = DIAGNOSTIC_CATALOG["W041"]
            evidence = [f'milestone "{m}" is active but has no ledger child' for m in missing_milestones]
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
                    mismatch_issues.append((desc.issue.number, desc.issue.title, m_name, desc.issue.milestone.title if desc.issue.milestone else "None"))

        for bl_node in backlog_ledgers_in_tree:
            for desc in bl_node.descendants():
                if desc.issue.milestone is not None:
                    mismatch_issues.append((desc.issue.number, desc.issue.title, "None", desc.issue.milestone.title))

        if mismatch_issues:
            f_details = DIAGNOSTIC_CATALOG["W040"]
            evidence = [f'#{num} "{title}" expected milestone {expected}, got {actual}' for num, title, expected, actual in mismatch_issues]
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
                child_refs = ", ".join(f"#{child.issue.number}" for child in child_issues)
                decomposed_work_units.append(f"work unit #{wu.issue.number} has child issues: {child_refs}")

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
                missing_ac_findings.append(f"work unit #{work_unit.issue.number} lacks acceptance criteria")

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

        # Populate metrics
        metrics["open issues reachable from root"] = len([n for n in tree_nodes if n.issue.is_open])
        metrics["open issues outside root"] = len(unreachable_open)
        metrics["open work units"] = len(open_work_units)
        metrics["work units"] = len(work_unit_nodes)
        metrics["max depth"] = max_depth + 1

        next_node = first_open_work_unit(tree_node)
        if next_node:
            next_issue_ref = IssueRef(repo_ref=dag.repo_ref, number=next_node.issue.number)
            enclosing_wu_ref = IssueRef(repo_ref=dag.repo_ref, number=next_node.issue.number)

    # Determine status
    errors_count = sum(1 for f in findings_list if f.severity == "error")
    warnings_count = sum(1 for f in findings_list if f.severity == "warning")

    metrics["errors"] = errors_count
    metrics["warnings"] = warnings_count

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

    return DoctorReport(repo=dag.slug, status=status, root=root_ref, next_issue=next_issue_ref, enclosing_work_unit=enclosing_wu_ref, metrics=metrics, findings=findings_list)
