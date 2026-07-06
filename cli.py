#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "cyclopts>=2.0",
#   "pydantic>=2.0",
# ]
# ///
from __future__ import annotations

import json
from typing import Annotated

from cyclopts import App, Parameter
from .github import GithubApi
from .models import (
    AttachRequest,
    DetachRequest,
    IssueCloseReason,
    IssueRef,
    MoveRequest,
    RepoDag,
    RepoRef,
    TreeNode,
)
from .traversal import build_dag
from .validate import validate_dag, validate_tree


def _build_tree(root_ref: IssueRef) -> tuple[RepoDag, TreeNode]:
    """Build the full DAG and materialize the tree for a root issue."""
    dag = build_dag(root_ref.to_repo_ref())
    return dag, dag.materialize_root(root_ref.number)


# CLI definitions

_WORKFLOW_PROLOGUE = """\
Decompose-then-traverse workflow:
  1. itree init OWNER/REPO "Title"          create a root issue
  2. itree add OWNER/REPO#1 "Child"          decompose into children
  3. itree next OWNER/REPO#1                 find the next open leaf
  4. itree close OWNER/REPO#5 --reason completed  close when done
  5. repeat 3-4 until the tree collapses
"""

app = App(
    help="Deterministic traversal for GitHub sub-issue trees.",
    help_prologue=_WORKFLOW_PROLOGUE,
)


def parse_ref(raw: str) -> IssueRef:
    """Parse an issue reference string into an IssueRef."""
    return IssueRef.parse(raw)


def parse_repo(raw: str) -> RepoRef:
    """Parse a repository reference string into a RepoRef."""
    return RepoRef.parse(raw)


@app.command(group="Structural")
def init(
    repo: Annotated[str, Parameter(help="Repository as OWNER/REPO")],
    title: Annotated[str, Parameter(help="Title for the root issue")],
    *,
    body: Annotated[str, Parameter(help="Issue body in Markdown")] = "",
) -> None:
    """Create a new root issue for a traversal domain.

    Example:
        $ itree init owner/project-alpha "Project Alpha"
        owner/project-alpha#1
    """
    repo_ref = parse_repo(repo)
    api = GithubApi.from_repo_ref(repo_ref)
    issue = api.create_issue(title, body)
    print(f"{repo_ref.owner}/{repo_ref.repo}#{issue.number}")


@app.command(group="Structural")
def add(
    root: Annotated[str, Parameter(help="Root issue as OWNER/REPO#N")],
    title: Annotated[str, Parameter(help="Title for the new child issue")],
    *,
    body: Annotated[str, Parameter(help="Issue body in Markdown")] = "",
) -> None:
    """Create a new child issue and attach it to the root.

    Example:
        $ itree add owner/project-alpha#1 "Frontend"
        owner/project-alpha#2
    """
    root_ref = parse_ref(root)
    api = GithubApi.from_issue_ref(root_ref)
    child = api.create_issue(title, body)
    api.add_subissue(root_ref.number, child.id, replace_parent=False)
    print(f"{root_ref.owner}/{root_ref.repo}#{child.number}")


@app.command(group="Structural")
def attach(
    parent: Annotated[str, Parameter(help="Parent issue as OWNER/REPO#N")],
    child: Annotated[str, Parameter(help="Child issue as OWNER/REPO#N")],
    *,
    replace_parent: Annotated[bool, Parameter()] = False,
) -> None:
    """Attach an existing issue as a sub-issue of a parent.

    Example:
        $ itree attach owner/project-alpha#1 owner/project-alpha#5
        owner/project-alpha#5
    """
    req = AttachRequest(parent=parse_ref(parent), child=parse_ref(child), replace_parent=replace_parent)
    api = GithubApi.from_issue_ref(req.parent)
    child_issue = api.get_issue(req.child.number)
    api.add_subissue(req.parent.number, child_issue.id, replace_parent=req.replace_parent)
    print(req.child.slug)


@app.command(group="Structural")
def detach(
    parent: Annotated[str, Parameter(help="Parent issue as OWNER/REPO#N")],
    child: Annotated[str, Parameter(help="Child issue as OWNER/REPO#N")],
) -> None:
    """Detach a sub-issue from its parent.

    Example:
        $ itree detach owner/project-alpha#1 owner/project-alpha#5
        owner/project-alpha#5
    """
    req = DetachRequest(parent=parse_ref(parent), child=parse_ref(child))
    api = GithubApi.from_issue_ref(req.parent)
    child_issue = api.get_issue(req.child.number)
    api.remove_subissue(req.parent.number, child_issue.id)
    print(req.child.slug)


@app.command(group="Structural")
def move(
    child: Annotated[str, Parameter(help="Issue to move as OWNER/REPO#N")],
    under: Annotated[str, Parameter(help="New parent as OWNER/REPO#N")],
    *,
    before: Annotated[str | None, Parameter(help="Place before sibling OWNER/REPO#N")] = None,
    after: Annotated[str | None, Parameter(help="Place after sibling OWNER/REPO#N")] = None,
) -> None:
    """Move issue under new parent, optionally positioned relative to siblings.

    Example:
        $ itree move owner/project-alpha#5 --under owner/project-alpha#3
        owner/project-alpha#5
    """
    req = MoveRequest(
        child=parse_ref(child),
        parent=parse_ref(under),
        before=parse_ref(before) if before else None,
        after=parse_ref(after) if after else None,
    )
    api = GithubApi.from_issue_ref(req.parent)
    child_issue = api.get_issue(req.child.number)
    api.add_subissue(req.parent.number, child_issue.id, replace_parent=True)
    if req.before is not None or req.after is not None:
        before_id = api.get_issue(req.before.number).id if req.before is not None else None
        after_id = api.get_issue(req.after.number).id if req.after is not None else None
        api.reprioritize(req.parent.number, child_issue.id, before_id=before_id, after_id=after_id)
    print(req.child.slug)


@app.command(group="Query")
def children(
    root: Annotated[str, Parameter(help="Root issue as OWNER/REPO#N")],
    *,
    recursive: Annotated[bool, Parameter()] = False,
    as_json: Annotated[bool, Parameter()] = False,
) -> None:
    """List children of an issue.

    Builds the full DAG, then extracts children from the materialized tree.

    Example:
        $ itree children owner/project-alpha#1
        #2: Frontend
        #3: Backend
        #4: Docs
    """
    dag, tree = _build_tree(parse_ref(root))
    if recursive:
        nodes = tree.descendants()
    else:
        nodes = tree.children
    if as_json:
        print(json.dumps([node.issue.model_dump() for node in nodes], indent=2))
    else:
        for node in nodes:
            print(f"#{node.issue.number}: {node.issue.title}")


@app.command(group="Query")
def tree(root: Annotated[str, Parameter(help="Root issue as OWNER/REPO#N")]) -> None:
    """Print the materialized rooted ordered tree as JSON.

    Builds the full DAG, then materializes the tree rooted at the given issue.

    Example:
        $ itree tree owner/project-alpha#1
        {"issue": {"number": 1, ...}, "children": [{...}, ...]}
    """
    _dag, tree_node = _build_tree(parse_ref(root))
    print(json.dumps(tree_node.model_dump(), indent=2))


@app.command(group="Query")
def next(
    root: Annotated[str, Parameter(help="Root issue as OWNER/REPO#N")],
    *,
    as_json: Annotated[bool, Parameter()] = False,
) -> None:
    """Find the first open leaf under ROOT in preorder.

    An open leaf has no open children -- it is the smallest undecomposed
    unit of work. Returns None if no open leaves remain.

    Builds the full DAG, then finds the first open leaf.

    Example:
        $ itree next owner/project-alpha#1
        #5: Login page
    """
    _dag, tree_node = _build_tree(parse_ref(root))
    node = tree_node.first_open_leaf()
    if as_json:
        print("{}" if node is None else json.dumps(node.issue.model_dump(), indent=2))
    else:
        print("No open leaves found" if node is None else f"#{node.issue.number}: {node.issue.title}")


@app.command(group="Query")
def path(
    issue: Annotated[str, Parameter(help="Issue as OWNER/REPO#N")],
    *,
    root: Annotated[str | None, Parameter(help="Root issue as OWNER/REPO#N")] = None,
    as_json: Annotated[bool, Parameter()] = False,
) -> None:
    """Print the path from root to the given issue.

    Builds the full DAG, then finds the path.

    Example:
        $ itree path owner/project-alpha#5 --root owner/project-alpha#1
        #1: Project Alpha
        #2: Frontend
        #5: Login page
    """
    issue_ref = parse_ref(issue)
    _dag, tree_node = _build_tree(parse_ref(root) if root else issue_ref)
    path_nodes = tree_node.path_to(issue_ref.number)
    if as_json:
        print("[]" if path_nodes is None else json.dumps([n.issue.model_dump() for n in path_nodes], indent=2))
    else:
        if path_nodes is None:
            print(f"Issue #{issue_ref.number} not found")
        else:
            for node in path_nodes:
                print(f"#{node.issue.number}: {node.issue.title}")


@app.command(group="Query")
def validate(root: Annotated[str, Parameter(help="Root issue as OWNER/REPO#N")]) -> None:
    """Validate the tree rooted at ROOT.

    Builds the full DAG, materializes the tree, then checks for:
    - Duplicate reachable issues
    - Open internal nodes whose decomposition has stalled

    Example:
        $ itree validate owner/project-alpha#1
        []
    """
    _dag, tree_node = _build_tree(parse_ref(root))
    violations = validate_tree(tree_node)
    print(json.dumps([v.model_dump() for v in violations], indent=2))


@app.command(group="Terminal")
def close(
    issue: Annotated[str, Parameter(help="Issue to close as OWNER/REPO#N")],
    *,
    comment: Annotated[str | None, Parameter(help="Comment to post when closing")] = None,
    reason: Annotated[
        IssueCloseReason,
        Parameter(help="Reason for closing: completed, not_planned, or reopened"),
    ] = IssueCloseReason.completed,
) -> None:
    """Close an issue with optional comment and reason.

    Example:
        $ itree close owner/project-alpha#5 --reason completed
        owner/project-alpha#5
    """
    issue_ref = parse_ref(issue)
    api = GithubApi.from_issue_ref(issue_ref)
    api.close_issue(issue_ref.number, comment=comment, reason=reason)
    print(issue_ref.slug)


@app.command(group="Diagnostic")
def doctor(
    repo: Annotated[str, Parameter(help="Repository as OWNER/REPO")],
    *,
    as_json: Annotated[bool, Parameter()] = False,
) -> None:
    """Scan the full repo issue DAG and report structure.

    Builds the entire issue graph, identifies roots, orphans,
    and validates every tree for invariant violations.

    Example:
        $ itree owner/project-alpha
        Repository: owner/project-alpha
        Issues: 12 total, 8 open

        Roots: 1
          #1 "Project Alpha" (open, 11 descendants)

        Orphans: 0

        Violations: 0
    """
    dag = build_dag(parse_repo(repo))

    total = len(dag.issues)
    open_count = sum(1 for i in dag.issues.values() if i.is_open)
    roots = dag.roots
    orphans = dag.orphans

    # Collect violations: DAG-level structural failures + per-tree invariants.
    all_violations: list = []
    all_violations.extend(validate_dag(dag))
    root_nodes: list[TreeNode] = []
    for root in roots:
        tree_node = dag.materialize_root(root.number)
        root_nodes.append(tree_node)
        all_violations.extend(validate_tree(tree_node))

    if as_json:
        result = {
            "repository": dag.slug,
            "total_issues": total,
            "open_issues": open_count,
            "roots": [
                {
                    "number": r.number,
                    "title": r.title,
                    "state": r.state,
                    "descendants": len(root_node.descendants()),
                }
                for r, root_node in zip(roots, root_nodes)
            ],
            "orphans": [
                {"number": o.number, "title": o.title, "state": o.state}
                for o in orphans
            ],
            "violations": [v.model_dump() for v in all_violations],
        }
        print(json.dumps(result, indent=2))
    else:
        print(f"Repository: {dag.slug}")
        print(f"Issues: {total} total, {open_count} open")
        print()

        print(f"Roots: {len(roots)}")
        for root_node, r in zip(root_nodes, roots):
            print(f'  #{r.number} "{r.title}" ({r.state}, {len(root_node.descendants())} descendants)')
        if not roots:
            print("  (none)")
        print()

        print(f"Orphans: {len(orphans)}")
        for o in orphans:
            print(f'  #{o.number} "{o.title}" ({o.state})')
        if not orphans:
            print("  (none)")
        print()

        print(f"Violations: {len(all_violations)}")
        for v in all_violations:
            loc = f" (#{v.issue_number})" if v.issue_number else ""
            print(f"  {v.code}: {v.message}{loc}")
        if not all_violations:
            print("  (none)")


@app.command(group="Diagnostic")
def forest(
    repo: Annotated[str, Parameter(help="Repository as OWNER/REPO")],
    *,
    as_json: Annotated[bool, Parameter()] = False,
) -> None:
    """List every independent tree (root) in the repository.

    Builds the full DAG and reports each root with its subtree size.

    Example:
        $ itree owner/project-alpha
        #1 "Project Alpha" (12 issues, 8 open)
        #15 "Sprint 2024-W3" (5 issues, 3 open)
    """
    dag = build_dag(parse_repo(repo))
    roots = dag.roots

    if as_json:
        result = []
        for r in roots:
            tree = dag.materialize_root(r.number)
            all_nodes = tree.preorder()
            result.append({
                "number": r.number,
                "title": r.title,
                "state": r.state,
                "total_issues": len(all_nodes),
                "open_issues": sum(1 for n in all_nodes if n.issue.is_open),
            })
        print(json.dumps(result, indent=2))
    else:
        if not roots:
            print("No root issues found in this repository.")
            return
        for r in roots:
            tree = dag.materialize_root(r.number)
            all_nodes = tree.preorder()
            total = len(all_nodes)
            open_count = sum(1 for n in all_nodes if n.issue.is_open)
            print(f'#{r.number} "{r.title}" ({total} issues, {open_count} open)')


@app.command(group="Diagnostic")
def orphans(
    repo: Annotated[str, Parameter(help="Repository as OWNER/REPO")],
    *,
    as_json: Annotated[bool, Parameter()] = False,
) -> None:
    """List issues not reachable from any root.

    These are issues that exist in the repo but aren't attached
    to any sub-issue tree.

    Example:
        $ itree owner/project-alpha
        #23 "Fix login redirect" (open)
        #27 "Update README" (closed)
    """
    orphan_list = build_dag(parse_repo(repo)).orphans

    if as_json:
        print(json.dumps([
            {"number": o.number, "title": o.title, "state": o.state}
            for o in orphan_list
        ], indent=2))
    else:
        if not orphan_list:
            print("No orphaned issues.")
            return
        for o in orphan_list:
            print(f'#{o.number} "{o.title}" ({o.state})')


if __name__ == "__main__":
    app()
