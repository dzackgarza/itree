from __future__ import annotations
import re
import networkx as nx

from pydantic import BaseModel
from .models import RepoDag, TreeNode, Finding, DoctorReport, IssueRef, RepoRef, GithubIssue

DIAGNOSTIC_CATALOG = {
    "E001": {
        "title": "no_root_ledger",
        "severity": "error",
        "meaning": "The root of the issue tree is not a ledger (its title does not start with 'Ledger:').",
        "remediation": [
            "1. Rename the root issue title so it starts with 'Ledger:', e.g., 'Ledger: OWNER/REPO'"
        ],
    },
    "E002": {
        "title": "multiple_root_ledgers",
        "severity": "error",
        "meaning": "This is a forest, not a tree. Multiple parentless issues exist in the repository.",
        "remediation": [
            "1. Choose exactly one issue as the root ledger.",
            "2. Attach all other parentless issues as descendants of that root ledger."
        ],
    },
    "E003": {
        "title": "cycle_detected",
        "severity": "error",
        "meaning": "A circular dependency exists in the issue hierarchy. Cycles break the poset and prevent traversal.",
        "remediation": [
            "A. Detach one of the edges in the cycle using `itree detach` to break the loop."
        ],
    },
    "E010": {
        "title": "unreachable_open_issues",
        "severity": "error",
        "meaning": "These issues are not in the repository's deterministic traversal path. An agent following `itree next` will never reach them.",
        "remediation": [
            "A. If the issue belongs to existing work:",
            "     itree attach OWNER/REPO#WORK_UNIT OWNER/REPO#ISSUE",
            "B. If the issue is broad:",
            "     create a work unit under the appropriate milestone ledger,",
            "     then attach this issue beneath it.",
            "C. If the issue is future work:",
            "     attach it under the Backlog ledger, which must itself be a child of root.",
            "D. If the issue is stale or not planned:",
            "     close it as not planned.",
            "Do not leave it parentless. Parentless open issues are accidental roots."
        ],
    },
    "E011": {
        "title": "parentless_non_root_issues",
        "severity": "error",
        "meaning": "These are accidental roots. Investigate intended parentage and attach them under the ledger.",
        "remediation": [
            "A. Attach this issue under the root ledger, a milestone ledger, or a work unit.",
            "B. Close the issue if it is no longer planned."
        ],
    },
    "E012": {
        "title": "closed_parent_with_open_descendants",
        "severity": "error",
        "meaning": "Traversal may skip live work. Reopen the parent or move the descendants.",
        "remediation": [
            "A. Reopen the parent issue.",
            "B. Move or detach the open children so they are attached under an open parent."
        ],
    },
    "E013": {
        "title": "duplicate_reachable_issue",
        "severity": "error",
        "meaning": "Tree invariant is violated: a node has multiple parents in the DAG.",
        "remediation": [
            "A. Detach the issue from one of its multiple parents so it only appears once."
        ],
    },
    "E014": {
        "title": "dependency_edges_present",
        "severity": "error",
        "meaning": "This model does not use DAG scheduling. Move blockers earlier in preorder or decompose the blocked issue.",
        "remediation": [
            "A. Remove the blocked_by dependency using the GitHub UI or API.",
            "B. Reorder the issues in the tree so the blocker appears earlier in preorder."
        ],
    },
    "W020": {
        "title": "depth_near_limit",
        "severity": "warning",
        "meaning": "GitHub supports at most eight nested sub-issue levels; split or flatten before the tree hits the limit.",
        "remediation": [
            "A. Flatten the tree by moving sub-issues to a higher parent.",
            "B. Decompose the work into smaller separate trees or milestone ledgers."
        ],
    },
    "W030": {
        "title": "singleton_work_unit",
        "severity": "warning",
        "meaning": "This repository treats PRs as review units for constellations of work. A single leaf task normally does not justify a standalone PR.",
        "remediation": [
            "- Merge this issue into the neighboring work unit.",
            "- Decompose the issue into several task leaves.",
            "- Mark it as a singleton work unit only if it is a genuinely large change, such as an architecture change, migration, or major refactor."
        ],
    },
    "W031": {
        "title": "oversized_work_unit",
        "severity": "warning",
        "meaning": "Review scope is probably too large. Split into ordered child work units.",
        "remediation": [
            "A. Decompose this work unit into multiple smaller, ordered child work units."
        ],
    },
    "W032": {
        "title": "leaf_has_pr",
        "severity": "warning",
        "meaning": "The leaf is an execution step. The enclosing work unit is the review boundary.",
        "remediation": [
            "- Change the PR description to close/link the work unit issue.",
            "- Mention leaf issues as context, not as the primary review target.",
            "- If the leaf really is the full review unit, mark it as a justified singleton work unit."
        ],
    },
    "W040": {
        "title": "milestone_mismatch",
        "severity": "warning",
        "meaning": "The tree defines traversal. The GitHub milestone defines release/time grouping. They should normally agree so Projects and milestone progress views remain useful.",
        "remediation": [
            "- Move the issue to the correct milestone ledger, or",
            "- Set its GitHub milestone to match the ledger, or",
            "- Move it to Backlog if it is not release-scoped."
        ],
    },
    "W041": {
        "title": "milestone_without_ledger",
        "severity": "warning",
        "meaning": "Create a milestone ledger under the root, or deliberately keep milestones outside tree policy.",
        "remediation": [
            "A. Create a milestone ledger issue under the root ledger representing this milestone.",
            "B. Clear the milestone from the issues if it is not meant to be tracked."
        ],
    },
    "W050": {
        "title": "missing_acceptance_criteria",
        "severity": "warning",
        "meaning": "Agents should not infer completion semantics. Add explicit done criteria.",
        "remediation": [
            "A. Edit the issue body to add a \"Done when\", \"Done Criteria\", or \"Acceptance Criteria\" section."
        ],
    },
    "I001": {
        "title": "valid_tree",
        "severity": "info",
        "meaning": "Traversal is deterministic. The reported next issue is safe for agents.",
        "remediation": [],
    }
}


class TreeViolation(BaseModel):
    code: str
    message: str
    issue_number: int | None = None


def validate_dag(dag: RepoDag) -> list[TreeViolation]:
    """Validate DAG-level structural invariants using networkx."""
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
                        message=f"issue #{num} \"{dag.issues[num].title}\" is not reachable from the primary root ledger #{primary_root}",
                        issue_number=num,
                    )
                )
    else:
        for num in sorted(dag.issues.keys()):
            if num not in roots:
                violations.append(
                    TreeViolation(
                        code="orphaned_issue",
                        message=f"issue #{num} \"{dag.issues[num].title}\" is not reachable from any root",
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


def is_singleton_justified(issue: GithubIssue) -> bool:
    if not issue.body:
        return False
    body_lower = issue.body.lower()
    return (
        "itree:role=singleton" in body_lower
        or "itree:singleton" in body_lower
        or "itree:role=singleton-work-unit" in body_lower
        or "itree:singleton=true" in body_lower
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


def find_enclosing_work_unit(path: tuple[TreeNode, ...]) -> TreeNode | None:
    for node in path[1:]:
        title = node.issue.title
        if parse_milestone_ledger_name(title) is None and not is_backlog_ledger(title):
            return node
    return None


def get_linked_issue_numbers(body: str | None) -> set[int]:
    if not body:
        return set()
    pattern = r"(?i)\b(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)\s*(?::\s*)?(?:[^\s#]+)?#(?P<number>\d+)\b"
    return {int(m.group("number")) for m in re.finditer(pattern, body)}


def find_root_ledger_candidates(dag: RepoDag, root_flag: str | None = None) -> list[int]:
    # The root is discovered structurally by finding parentless nodes in the issue DAG
    import networkx as nx
    G = nx.DiGraph()
    G.add_nodes_from(dag.issues.keys())
    for parent, children in dag.children_of.items():
        for child in children:
            G.add_edge(parent, child)
            
    roots = [n for n in G.nodes if G.in_degree(n) == 0]
    return roots


def generate_doctor_report(dag: RepoDag, root_flag: str | None = None) -> DoctorReport:
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
        findings_list.append(Finding(
            code="E003",
            severity="error",
            title=f_details["title"],
            evidence=evidence,
            meaning=f_details["meaning"],
            remediation=f_details["remediation"],
        ))

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
        "open leaves": 0,
        "work units": 0,
        "singleton work-unit warnings": 0,
        "max depth": 0,
    }

    if len(candidates) == 0:
        f_details = DIAGNOSTIC_CATALOG["E001"]
        findings_list.append(Finding(
            code="E001",
            severity="error",
            title=f_details["title"],
            evidence=["no root candidate found"],
            meaning=f_details["meaning"],
            remediation=f_details["remediation"],
        ))
    elif len(candidates) > 1:
        f_details = DIAGNOSTIC_CATALOG["E002"]
        evidence = [f"#{num}  {dag.issues[num].title}" for num in sorted(candidates)]
        findings_list.append(Finding(
            code="E002",
            severity="error",
            title=f_details["title"],
            evidence=evidence,
            meaning=f_details["meaning"],
            remediation=f_details["remediation"],
        ))
        # Choose the first one to continue analysis
        root_num = sorted(candidates)[0]
        root_ref = IssueRef(repo_ref=dag.repo_ref, number=root_num)
    else:
        root_num = candidates[0]
        root_issue = dag.issues[root_num]
        
        # Check if the root issue is a ledger (starts with Ledger:)
        if not root_issue.title.lower().startswith("ledger:"):
            f_details = DIAGNOSTIC_CATALOG["E001"]
            findings_list.append(Finding(
                code="E001",
                severity="error",
                title=f_details["title"],
                evidence=[f"Root issue #{root_num} \"{root_issue.title}\" title must start with 'Ledger:'"],
                meaning="The unique root of the tree must be a root ledger (title starts with 'Ledger:').",
                remediation=[f"Rename issue #{root_num} title to start with 'Ledger:'"],
            ))
            
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
            evidence = [f"#{num} \"{dag.issues[num].title}\"" for num in unreachable_open]
            findings_list.append(Finding(
                code="E010",
                severity="error",
                title=f_details["title"],
                evidence=evidence,
                meaning=f_details["meaning"],
                remediation=f_details["remediation"],
            ))

        # E011: parentless non-root open issues
        parentless_non_root = []
        for num, issue in sorted(dag.issues.items()):
            if issue.is_open and num != root_num and G.in_degree(num) == 0:
                parentless_non_root.append(num)
                
        if parentless_non_root:
            f_details = DIAGNOSTIC_CATALOG["E011"]
            evidence = [f"#{num} \"{dag.issues[num].title}\"" for num in parentless_non_root]
            findings_list.append(Finding(
                code="E011",
                severity="error",
                title=f_details["title"],
                evidence=evidence,
                meaning=f_details["meaning"],
                remediation=f_details["remediation"],
            ))

        # E013: duplicate parent / multiple parents (in-degree > 1 in G)
        multiple_parents = [num for num in sorted(reachable) if G.in_degree(num) > 1]
        if multiple_parents:
            f_details = DIAGNOSTIC_CATALOG["E013"]
            evidence = [f"issue #{num} has multiple parent edges" for num in multiple_parents]
            findings_list.append(Finding(
                code="E013",
                severity="error",
                title=f_details["title"],
                evidence=evidence,
                meaning=f_details["meaning"],
                remediation=f_details["remediation"],
            ))

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
            findings_list.append(Finding(
                code="E012",
                severity="error",
                title=f_details["title"],
                evidence=evidence,
                meaning=f_details["meaning"],
                remediation=f_details["remediation"],
            ))

        # E014: dependency edges present
        dependency_issues = []
        for num, blockers in sorted(dag.dependencies.items()):
            if blockers:
                dependency_issues.append((num, blockers))

        if dependency_issues:
            f_details = DIAGNOSTIC_CATALOG["E014"]
            evidence = [f"issue #{num} blocked by: {', '.join(f'#{b}' for b in blockers)}" for num, blockers in dependency_issues]
            findings_list.append(Finding(
                code="E014",
                severity="error",
                title=f_details["title"],
                evidence=evidence,
                meaning=f_details["meaning"],
                remediation=f_details["remediation"],
            ))

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
            findings_list.append(Finding(
                code="W020",
                severity="warning",
                title=f_details["title"],
                evidence=[f"materialized tree depth is {max_depth + 1}"],
                meaning=f_details["meaning"],
                remediation=f_details["remediation"],
            ))

        # Find enclosing work units and milestones
        work_units_set = set()
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

            path = tree_node.path_to(node.issue.number)
            if path:
                wu = find_enclosing_work_unit(path)
                if wu:
                    work_units_set.add(wu)

        # W041: active milestone without milestone ledger
        active_milestones = set()
        for issue in dag.issues.values():
            if issue.is_open and issue.milestone is not None:
                active_milestones.add(issue.milestone.title)

        missing_milestones = sorted(active_milestones - milestone_ledger_names)
        if missing_milestones:
            f_details = DIAGNOSTIC_CATALOG["W041"]
            evidence = [f"milestone \"{m}\" is active but has no ledger child" for m in missing_milestones]
            findings_list.append(Finding(
                code="W041",
                severity="warning",
                title=f_details["title"],
                evidence=evidence,
                meaning=f_details["meaning"],
                remediation=f_details["remediation"],
            ))

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
            evidence = [f"#{num} \"{title}\" expected milestone {expected}, got {actual}" for num, title, expected, actual in mismatch_issues]
            findings_list.append(Finding(
                code="W040",
                severity="warning",
                title=f_details["title"],
                evidence=evidence,
                meaning=f_details["meaning"],
                remediation=f_details["remediation"],
            ))

        # W030 & W031: work unit size check
        singleton_wu_count = 0
        singleton_wu_findings = []
        oversized_wu_findings = []
        for wu in sorted(work_units_set, key=lambda w: w.issue.number):
            desc_tasks = wu.descendants()
            open_desc_count = len([d for d in desc_tasks if d.issue.is_open])

            if open_desc_count <= 1 and not is_singleton_justified(wu.issue):
                singleton_wu_count += 1
                singleton_wu_findings.append(f"work unit #{wu.issue.number} has {open_desc_count} open tasks and no justification")

            leaves = [d for d in desc_tasks if not d.children]
            if len(leaves) > 10:
                oversized_wu_findings.append(f"work unit #{wu.issue.number} has {len(leaves)} task leaves")

        if singleton_wu_findings:
            f_details = DIAGNOSTIC_CATALOG["W030"]
            findings_list.append(Finding(
                code="W030",
                severity="warning",
                title=f_details["title"],
                evidence=singleton_wu_findings,
                meaning=f_details["meaning"],
                remediation=f_details["remediation"],
            ))

        if oversized_wu_findings:
            f_details = DIAGNOSTIC_CATALOG["W031"]
            findings_list.append(Finding(
                code="W031",
                severity="warning",
                title=f_details["title"],
                evidence=oversized_wu_findings,
                meaning=f_details["meaning"],
                remediation=f_details["remediation"],
            ))

        # Collect PR references
        linked_by_prs = set()
        prs = [issue for issue in dag.issues.values() if issue.pull_request is not None]
        for pr in prs:
            linked_by_prs.update(get_linked_issue_numbers(pr.body))

        # W032 & W050: leaf task validations
        leaf_pr_findings = []
        missing_ac_findings = []
        open_leaves = [n for n in tree_nodes if n.issue.is_open and all(not child.first_open_leaf() for child in n.children)]

        for leaf in sorted(open_leaves, key=lambda l: l.issue.number):
            if not is_singleton_justified(leaf.issue):
                if leaf.issue.number in linked_by_prs:
                    leaf_pr_findings.append(f"leaf task #{leaf.issue.number} has linked PR")
                if lacks_acceptance_criteria(leaf.issue.body):
                    missing_ac_findings.append(f"leaf task #{leaf.issue.number} lacks acceptance criteria")

        if leaf_pr_findings:
            f_details = DIAGNOSTIC_CATALOG["W032"]
            findings_list.append(Finding(
                code="W032",
                severity="warning",
                title=f_details["title"],
                evidence=leaf_pr_findings,
                meaning=f_details["meaning"],
                remediation=f_details["remediation"],
            ))

        if missing_ac_findings:
            f_details = DIAGNOSTIC_CATALOG["W050"]
            findings_list.append(Finding(
                code="W050",
                severity="warning",
                title=f_details["title"],
                evidence=missing_ac_findings,
                meaning=f_details["meaning"],
                remediation=f_details["remediation"],
            ))

        # Populate metrics
        metrics["open issues reachable from root"] = len([n for n in tree_nodes if n.issue.is_open])
        metrics["open issues outside root"] = len(unreachable_open)
        metrics["open leaves"] = len(open_leaves)
        metrics["work units"] = len(work_units_set)
        metrics["singleton work-unit warnings"] = singleton_wu_count
        metrics["max depth"] = max_depth + 1

        next_node = tree_node.first_open_leaf()
        if next_node:
            next_issue_ref = IssueRef(repo_ref=dag.repo_ref, number=next_node.issue.number)
            path_to_next = tree_node.path_to(next_node.issue.number)
            if path_to_next:
                wu_node = find_enclosing_work_unit(path_to_next)
                if wu_node:
                    enclosing_wu_ref = IssueRef(repo_ref=dag.repo_ref, number=wu_node.issue.number)

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
        findings_list.append(Finding(
            code="I001",
            severity="info",
            title=f_details["title"],
            evidence=[],
            meaning=f_details["meaning"],
            remediation=[],
        ))

    return DoctorReport(
        repo=dag.slug,
        status=status,
        root=root_ref,
        next_issue=next_issue_ref,
        enclosing_work_unit=enclosing_wu_ref,
        metrics=metrics,
        findings=findings_list
    )
