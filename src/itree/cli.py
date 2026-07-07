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
import sys
from typing import Annotated

from cyclopts import App, Parameter

from .github import GithubApi
from .models import (
    AttachRequest,
    DetachRequest,
    IssueCloseReason,
    IssueRef,
    MoveRequest,
    RepoRef,
)
from .traversal import build_dag
from .validate import (
    DIAGNOSTIC_CATALOG,
    find_root_ledger_candidates,
    first_open_work_unit,
    generate_doctor_report,
    validate_tree,
)

# Core ontology and help text
CORE_HELP_TEXT = """itree maintains a deterministic GitHub issue tree.

Desired structure:

  One repository has exactly one root ledger issue.
  Every open issue that represents planned work must be reachable from that root.
  The order of GitHub sub-issues is the traversal order.
  The next work unit is the first open non-ledger issue in preorder.

Issue roles:

  root ledger
    The single issue anchoring the repository's work tree.
    It is a grouping issue, not a work-unit issue.

  milestone ledger
    Optional grouping issue under the root.
    Use it to mirror a GitHub milestone or backlog area.
    It is not a traversal root.

  work unit
    A coherent review/proof boundary that normally deserves a PR.
    Put implementation checklists, status notes, and proof details in the
    issue body or issue comments.
    Do not create child issues under a work unit. Use child issues only under
    organizational grouping issues, and only for separate PR-sized work units.
"""

app = App(
    help=CORE_HELP_TEXT,
    help_epilogue="""Use `itree help model` for the full organization guide.
Use `itree doctor --explain CODE` for detailed remediation of a diagnostic.""",
)

# Subapps for progressive disclosure help
help_app = App(name="help", help="Organization guide and model explanation.")
app.command(help_app)

milestone_app = App(
    name="milestone",
    help="How milestone ledgers mirror GitHub milestones.",
    help_prologue="""How milestone ledgers mirror GitHub milestones.

Milestone ledgers are grouping issues under the root ledger.
Use them to mirror GitHub milestones or backlog areas.
They are release/time groupings, not traversal roots, and they never replace the single root ledger.""",
)
app.command(milestone_app)

work_unit_app = App(
    name="work-unit",
    help="Work-unit issue policy.",
    help_prologue="""Work-unit issue policy.

A work unit is a GitHub issue that owns a coherent review/proof boundary.
It may stand alone. Its implementation checklist belongs in the issue body,
or issue comments. Do not create child issues for its sub-tasks, sub-stories,
proof burdens, or checklist items.
Open the PR when implementation starts, and synthesize the PR body from the
work-unit issue.""",
)
app.command(work_unit_app)

root_app = App(name="root", help="Create or inspect the repository root ledger.")
app.command(root_app)


def parse_ref(raw: str) -> IssueRef:
    """Parse an issue reference string into an IssueRef."""
    return IssueRef.parse(raw)


def parse_repo(raw: str) -> RepoRef:
    """Parse a repository reference string into a RepoRef."""
    return RepoRef.parse(raw)


def get_repo_root(repo: str) -> tuple[RepoRef, int]:
    """Resolve a repository to its structurally unique root issue."""
    repo_ref = RepoRef.parse(repo)
    try:
        dag = build_dag(repo_ref)
    except Exception as e:
        print(f"Error fetching issues from GitHub: {e}")
        sys.exit(3)

    candidates = find_root_ledger_candidates(dag)
    if not candidates:
        print("ERROR E001: no root ledger exists.\n")
        print("This repository has no traversal domain because it has no parentless issue.")
        print("Repair:")
        print("  1. Create one ledger issue:")
        print('       itree root create OWNER/REPO --title "Ledger: OWNER/REPO"\n')
        print("  2. Attach every open planned issue under that ledger, either directly or")
        print("     through a milestone/backlog ledger:")
        print("       itree attach OWNER/REPO#ROOT OWNER/REPO#ISSUE\n")
        print("Do not create multiple ledger issues. A milestone, project, roadmap, or epic is")
        print("not a second root.")
        sys.exit(2)
    if len(candidates) > 1:
        print("ERROR E002: multiple parentless issues found.\n")
        print("Found:")
        for c in candidates:
            print(f"  #{c}  {dag.issues[c].title}")
        print("\nThis is a forest, not one ordered tree. A repository-wide traversal requires")
        print("one root by construction, not an explicit root selection.\n")
        print("Repair:")
        print("  1. Choose the single repository ledger issue.")
        print("  2. Attach every other parentless planned issue under that ledger.")
        print("  3. Put the active/current child first; preorder defines execution order.")
        sys.exit(2)
    return repo_ref, candidates[0]


def get_repo_and_issue_or_root(target: str) -> tuple[RepoRef, int]:
    """Resolve an explicit issue target, or the repository's unique root."""
    if "#" in target:
        ref = IssueRef.parse(target)
        return ref.repo_ref, ref.number
    return get_repo_root(target)


@help_app.command(name="model")
def help_model() -> None:
    """Full explanation of root ledger, grouping issues, work units, preorder
    traversal, and PR policy.
    """
    print("""Mental model:
  Repository issue structure = one rooted ordered tree.

  Root ledger issue
    Milestone ledger or backlog ledger
      Work-unit issue
      Work-unit issue

  Inside a work-unit issue:
    Acceptance criteria
    Proof obligations
    Implementation checklist
    Status comments

  Individual implementation tasks stay inside the work-unit issue body,
  or issue comments. Do not create GitHub issues for ordinary implementation tasks.

Traversal:
  next(root) = first open work-unit issue in preorder.

Review policy:
  PRs correspond to work-unit issues. Child issues are justified only under
  organizational grouping issues, and only when they are separate PR-sized work
  units with independent acceptance/proof boundaries.
""")


@root_app.command(name="create")
def root_create(
    repo: Annotated[str, Parameter(help="Repository as OWNER/REPO")],
    *,
    title: Annotated[str, Parameter(help="Title for the root ledger issue")],
    body: Annotated[str, Parameter(help="Issue body in Markdown")] = "",
) -> None:
    """Create a new root ledger issue for a traversal domain."""
    repo_ref = parse_repo(repo)
    api = GithubApi.from_repo_ref(repo_ref)

    try:
        issue = api.create_issue(title, body or "")
        print(f"{repo_ref.slug}#{issue.number}")
    except Exception as e:
        print(f"Error creating issue: {e}")
        sys.exit(3)


@root_app.command(name="inspect")
def root_inspect(
    repo: Annotated[str, Parameter(help="Repository as OWNER/REPO")],
) -> None:
    """Inspect the repository's root ledger details."""
    repo_ref = parse_repo(repo)
    try:
        dag = build_dag(repo_ref)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(3)
    candidates = find_root_ledger_candidates(dag)
    if not candidates:
        print("No root ledger found.")
        sys.exit(2)
    elif len(candidates) > 1:
        print(f"Multiple root ledger candidates found: {candidates}")
        sys.exit(2)
    else:
        issue = dag.issues[candidates[0]]
        print(f'Root ledger: #{issue.number} "{issue.title}"')
        print(f"State: {issue.state}")
        print(f"URL: {issue.html_url}")


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
    root_create(repo, title=title, body=body)


@app.command(group="Structural")
def add(
    parent: Annotated[str, Parameter(help="Parent issue or repository root as OWNER/REPO#N or OWNER/REPO")],
    title: Annotated[str, Parameter(help="Title for the new child issue")],
    *,
    body: Annotated[str, Parameter(help="Issue body in Markdown")] = "",
) -> None:
    """Create a new child issue and attach it to the parent issue.

    Example:
        $ itree add owner/project-alpha#1 "Frontend"
        owner/project-alpha#2
    """
    repo_ref, parent_number = get_repo_and_issue_or_root(parent)
    api = GithubApi.from_repo_ref(repo_ref)
    try:
        child = api.create_issue(title, body)
        api.add_subissue(parent_number, child.id, replace_parent=False)
        print(f"{repo_ref.slug}#{child.number}")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(3)


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
    try:
        child_issue = api.get_issue(req.child.number)
        api.add_subissue(req.parent.number, child_issue.id, replace_parent=req.replace_parent)
        print(req.child.slug)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(3)


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
    try:
        child_issue = api.get_issue(req.child.number)
        api.remove_subissue(req.parent.number, child_issue.id)
        print(req.child.slug)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(3)


@app.command(group="Structural")
def move(
    child: str,
    under: Annotated[str, Parameter(name=["under", "--under"])],
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
    try:
        child_issue = api.get_issue(req.child.number)
        api.add_subissue(req.parent.number, child_issue.id, replace_parent=True)
        if req.before is not None or req.after is not None:
            before_id = api.get_issue(req.before.number).id if req.before is not None else None
            after_id = api.get_issue(req.after.number).id if req.after is not None else None
            api.reprioritize(req.parent.number, child_issue.id, before_id=before_id, after_id=after_id)
        print(req.child.slug)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(3)


@app.command(group="Query")
def children(
    target: Annotated[str, Parameter(help="Issue or repository root as OWNER/REPO#N or OWNER/REPO")],
    *,
    recursive: Annotated[bool, Parameter()] = False,
    as_json: Annotated[bool, Parameter()] = False,
) -> None:
    """List children of an issue.

    Example:
        $ itree children owner/project-alpha#1
        #2: Frontend
    """
    repo_ref, root_num = get_repo_and_issue_or_root(target)
    try:
        dag = build_dag(repo_ref)
        tree_node = dag.materialize_root(root_num)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(3)

    if recursive:
        nodes = tree_node.descendants()
    else:
        nodes = tree_node.children

    if as_json:
        print(json.dumps([node.issue.model_dump() for node in nodes], indent=2))
    else:
        for node in nodes:
            print(f"#{node.issue.number}: {node.issue.title}")


@app.command(group="Query")
def tree(
    repo: Annotated[str, Parameter(help="Repository as OWNER/REPO")],
) -> None:
    """Print the repository's materialized ordered issue tree as JSON.

    Example:
        $ itree tree owner/project-alpha
    """
    repo_ref, root_num = get_repo_root(repo)
    try:
        dag = build_dag(repo_ref)
        tree_node = dag.materialize_root(root_num)
        print(json.dumps(tree_node.model_dump(), indent=2))
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(3)


@app.command(group="Query")
def next(
    repo: Annotated[str, Parameter(help="Repository as OWNER/REPO")],
    *,
    as_json: Annotated[bool, Parameter()] = False,
) -> None:
    """Find the next open work-unit issue in repository preorder.

    Example:
        $ itree next owner/project-alpha
    """
    repo_ref, root_num = get_repo_root(repo)
    try:
        dag = build_dag(repo_ref)
        tree_node = dag.materialize_root(root_num)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(3)

    node = first_open_work_unit(tree_node)

    if as_json:
        print("{}" if node is None else json.dumps(node.issue.model_dump(), indent=2))
    else:
        if node is None:
            print("No open work units found")
            return

        print("Next work unit:")
        print(f"  #{node.issue.number} {node.issue.title}\n")
        print("Instruction:")
        print(f"  Work from issue #{node.issue.number}; keep planning state on that issue.")
        print("  Open the PR when implementation starts; synthesize its body from the issue.")
        print("  Keep implementation tasks in the issue body or issue comments.")


@app.command(group="Query")
def path(
    issue: Annotated[str, Parameter(help="Issue as OWNER/REPO#N")],
    *,
    as_json: Annotated[bool, Parameter()] = False,
) -> None:
    """Print the path from root to the given issue.

    Example:
        $ itree path owner/project-alpha#5
    """
    issue_ref = parse_ref(issue)
    repo_ref, root_num = get_repo_root(issue_ref.repo_ref.slug)
    try:
        dag = build_dag(repo_ref)
        tree_node = dag.materialize_root(root_num)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(3)

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
def validate(
    repo: Annotated[str, Parameter(help="Repository as OWNER/REPO")],
) -> None:
    """Validate the repository issue tree.

    Example:
        $ itree validate owner/project-alpha
    """
    repo_ref, root_num = get_repo_root(repo)
    try:
        dag = build_dag(repo_ref)
        tree_node = dag.materialize_root(root_num)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(3)

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
    """
    issue_ref = parse_ref(issue)
    api = GithubApi.from_issue_ref(issue_ref)
    try:
        api.close_issue(issue_ref.number, comment=comment, reason=reason)
        print(issue_ref.slug)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(3)


@app.command(group="Diagnostic")
def doctor(
    repo: Annotated[str, Parameter(help="Repository as OWNER/REPO")],
    *,
    as_json: Annotated[bool, Parameter()] = False,
    explain: Annotated[str | None, Parameter(help="Explain the remediation of a diagnostic code")] = None,
    strict: Annotated[bool, Parameter(help="Treat warnings as errors")] = False,
) -> None:
    """Scan the full repo issue DAG and report structure."""
    if explain:
        code = explain.upper()
        if code in DIAGNOSTIC_CATALOG:
            details = DIAGNOSTIC_CATALOG[code]
            print(f"{code}: {details['title']}.\n")
            print("Meaning:")
            print(f"  {details['meaning']}\n")
            print("Repair routes:")
            for route in details["remediation"]:
                print(f"  {route}")
            sys.exit(0)
        else:
            print(f"Unknown diagnostic code: {explain}")
            sys.exit(1)

    repo_ref = parse_repo(repo)
    try:
        dag = build_dag(repo_ref)
    except Exception as e:
        print(f"GitHub/auth/API failure: {e}")
        sys.exit(3)

    report = generate_doctor_report(dag)

    if as_json:
        print(report.model_dump_json(indent=2))
    else:
        status_str = "OK" if report.status == "ok" else "NOT OK"
        print(f"{repo_ref.slug} issue tree: {status_str}\n")

        if report.root:
            issue_title = dag.issues[report.root.number].title
            print("Root ledger:")
            print(f"  #{report.root.number} {issue_title}\n")
        else:
            print("Root ledger:")
            print("  None\n")

        print("Traversal:")
        if report.next_issue:
            next_title = dag.issues[report.next_issue.number].title
            print(f"  Next work unit: #{report.next_issue.number} {next_title}")

            if report.enclosing_work_unit:
                wu_title = dag.issues[report.enclosing_work_unit.number].title
                print(f"  Work-unit issue: #{report.enclosing_work_unit.number} {wu_title}")
                print(f"  Agent instruction: work from issue #{report.enclosing_work_unit.number}; keep planning state on that issue.")
                print("  Open the PR when implementation starts; synthesize its body from the issue.")
                print("  Keep implementation tasks in the issue body or issue comments.")
            else:
                print("  Work-unit issue: None")
                print(f"  Agent instruction: work on #{report.next_issue.number}.")
        else:
            print("  Next work unit: None")
            print("  Work-unit issue: None")
            print("  Agent instruction: No open work units found.")
        print()

        print("Summary:")
        for k, v in report.metrics.items():
            if k == "max depth":
                print(f"  {k}: {v} / 8")
            else:
                print(f"  {k}: {v}")
        print()

        print("Findings:")
        if not report.findings or (len(report.findings) == 1 and report.findings[0].code == "I001"):
            print("  (none)")
        else:
            for f in report.findings:
                # Format to only display standard code summary
                print(f"  {f.code}: {len(f.evidence)} {f.title.replace('_', ' ')}")
        print()

        print("Run:")
        # Display command suggestion for the first non-info finding if exists
        example_code = "E010"
        non_info_findings = [f for f in report.findings if f.severity != "info"]
        if non_info_findings:
            example_code = non_info_findings[0].code
        print(f"  itree doctor {repo_ref.slug} --explain {example_code}")
        print(f"  itree doctor {repo_ref.slug} --json")

    # Handle exit code based on report status
    if report.status == "error":
        sys.exit(2)
    elif report.status == "warning":
        if strict:
            sys.exit(2)
        else:
            sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    app()
