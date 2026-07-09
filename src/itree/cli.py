#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = [
#   "cyclopts>=2.0",
#   "networkx>=3.0",
#   "pydantic>=2.0",
# ]
# ///
from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Annotated

from cyclopts import App, Parameter

from .github import GithubApi
from .metrics import load_config, measure_code_size, structure_questions
from .models import (
    AttachRequest,
    DetachRequest,
    FindingSeverity,
    IssueCloseReason,
    IssueRef,
    MoveRequest,
    RepoDag,
    RepoRef,
    TreeNode,
)
from .render import prune_closed, render_tree, shape_summary
from .traversal import build_dag
from .validate import (
    DIAGNOSTIC_CATALOG,
    find_root_ledger_candidates,
    first_open_work_unit,
    generate_doctor_report,
    is_grouping_issue,
    is_root_ledger,
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

# Subapp for progressive disclosure help
help_app = App(name="help", help="Organization guide and model explanation.")
app.command(help_app)


def parse_ref(raw: str) -> IssueRef:
    """Parse an issue reference string into an IssueRef."""
    return IssueRef.parse(raw)


def parse_repo(raw: str) -> RepoRef:
    """Parse a repository reference string into a RepoRef."""
    return RepoRef.parse(raw)


# Total over FindingSeverity: adding a severity without a prefix is a type error.
SEVERITY_PREFIX: dict[FindingSeverity, str] = {
    "error": "ERROR",
    "warning": "WARNING",
    "question": "QUESTION",
    "info": "INFO",
}


def print_diagnostic(code: str, evidence: Sequence[str] = ()) -> None:
    """Print one catalog diagnostic: code, meaning, evidence, remediation."""
    details = DIAGNOSTIC_CATALOG[code]
    print(f"{SEVERITY_PREFIX[details['severity']]} {code}: {details['title']}.\n")
    print("Meaning:")
    print(f"  {details['meaning']}")
    if evidence:
        print("\nFound:")
        for line in evidence:
            print(f"  {line}")
    print("\nRepair routes:")
    for route in details["remediation"]:
        print(f"  {route}")


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
        print_diagnostic("E001")
        sys.exit(2)
    if len(candidates) > 1:
        print_diagnostic("E002", evidence=[f"#{c}  {dag.issues[c].title}" for c in candidates])
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


@app.command(group="Structural")
def init(
    repo: Annotated[str, Parameter(help="Repository as OWNER/REPO")],
    title: Annotated[str, Parameter(help="Title for the root ledger issue")],
    *,
    body: Annotated[str, Parameter(help="Issue body in Markdown")] = "",
) -> None:
    """Create a new root ledger issue for a traversal domain.

    Example:
        $ itree init owner/project-alpha "Ledger: owner/project-alpha"
        owner/project-alpha#1
    """
    repo_ref = parse_repo(repo)
    api = GithubApi.from_repo_ref(repo_ref)
    try:
        issue = api.create_issue(title, body or "")
        print(f"{repo_ref.slug}#{issue.number}")
    except Exception as e:
        print(f"Error creating issue: {e}")
        sys.exit(3)


def read_body(body: str, body_file: str | None) -> str:
    """Resolve the issue body from --body or --body-file (mutually exclusive)."""
    if body_file is not None:
        if body:
            print("Error: use either --body or --body-file, not both")
            sys.exit(1)
        return Path(body_file).read_text()
    return body


def candidate_sections(tree_node: TreeNode) -> tuple[list[TreeNode], list[TreeNode]]:
    """Open work-unit and grouping-issue nodes, in preorder."""
    work_units: list[TreeNode] = []
    groupings: list[TreeNode] = []
    for node in tree_node.preorder():
        if not node.issue.is_open:
            continue
        if is_grouping_issue(node.issue.title):
            groupings.append(node)
        else:
            work_units.append(node)
    return work_units, groupings


def candidate_lines(nodes: list[TreeNode]) -> str:
    return "\n".join(f"  #{node.issue.number} {node.issue.title}" for node in nodes) if nodes else "  (none)"


def example_grouping_number(groupings: list[TreeNode], root: TreeNode) -> str:
    """Prefer a non-root grouping for example commands; fall back to the root."""
    for node in groupings:
        if node is not root:
            return str(node.issue.number)
    return str(root.issue.number)


def print_placement_menu(slug: str, title: str, tree_node: TreeNode) -> None:
    """The anti-invention rail: existing work units first, then grouping targets."""
    work_units, groupings = candidate_sections(tree_node)
    print("Nothing was created. Fit the new item into existing work FIRST.\n")
    if work_units:
        print(f"Open work units ({len(work_units)} = {len(work_units)} pending PRs):")
    else:
        print("Open work units:")
    print(candidate_lines(work_units))
    print("\nGrouping issues:")
    print(candidate_lines(groupings))
    print()
    if work_units:
        print("Less than one PR of work -> absorb it into a work unit:")
        print(f'  itree absorb --into {slug}#{work_units[0].issue.number} --title "{title}" --body "..."')
    grouping_number = example_grouping_number(groupings, tree_node)
    print("A full PR-sized unit (independently valuable, reviewable, own")
    print("acceptance criteria) -> create it under a grouping issue:")
    print(f'  itree new {slug} "{title}" --under {slug}#{grouping_number} --body "..."')


@app.command(group="Structural")
def new(
    target: Annotated[str, Parameter(help="Repository as OWNER/REPO, or grouping parent as OWNER/REPO#N")],
    title: Annotated[str, Parameter(help="Title for the new issue")],
    *,
    under: Annotated[str | None, Parameter(help="Grouping issue to attach under, as OWNER/REPO#N")] = None,
    body: Annotated[str, Parameter(help="Issue body in Markdown")] = "",
    body_file: Annotated[str | None, Parameter(help="Read the issue body from a file")] = None,
) -> None:
    """File a new issue into the tree, with guided placement.

    Without --under this creates NOTHING: it lists the existing work
    units and grouping issues and prints the exact next commands, so the
    item is absorbed into existing work unless it truly is a new
    PR-sized work unit.

    Example:
        $ itree new owner/project-alpha "Export command proof" --under owner/project-alpha#2
        owner/project-alpha#7
    """
    resolved_body = read_body(body, body_file)
    parent_raw = target if "#" in target else under

    if parent_raw is None:
        repo_ref, root_num = get_repo_root(target)
        try:
            dag = build_dag(repo_ref)
        except Exception as e:
            print(f"Error: {e}")
            sys.exit(3)
        print_placement_menu(repo_ref.slug, title, dag.materialize_root(root_num))
        sys.exit(1)

    parent_ref = parse_ref(parent_raw)
    api = GithubApi.from_issue_ref(parent_ref)
    try:
        parent_issue = api.get_issue(parent_ref.number)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(3)

    if not parent_issue.is_open:
        print(f"Refusing: #{parent_ref.number} is closed. Attach new work under an open grouping issue.")
        sys.exit(2)
    if not is_grouping_issue(parent_issue.title):
        print(f'Refusing: #{parent_ref.number} "{parent_issue.title}" is a work unit, and work units are leaves.')
        print("Implementation tasks belong in the work-unit issue body or comments.")
        print("If this item is part of that work unit, absorb it instead:")
        print(f'  itree absorb --into {parent_ref.slug} --title "{title}" --body "..."')
        sys.exit(2)

    try:
        child = api.create_issue(title, resolved_body)
        api.add_subissue(parent_ref.number, child.id)
        print(f"{parent_ref.repo_ref.slug}#{child.number}")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(3)


@app.command(group="Structural")
def absorb(
    source: Annotated[str | None, Parameter(help="Existing issue to absorb, as OWNER/REPO#N")] = None,
    *,
    into: Annotated[str, Parameter(help="Target work-unit issue as OWNER/REPO#N")],
    title: Annotated[str | None, Parameter(help="Title for not-yet-filed content (no source issue)")] = None,
    body: Annotated[str, Parameter(help="Body for not-yet-filed content")] = "",
    body_file: Annotated[str | None, Parameter(help="Read the content body from a file")] = None,
) -> None:
    """Merge an issue (or not-yet-filed content) into a work unit, verbatim.

    The source body is appended byte-for-byte to the target work unit
    under an '## Absorbed:' heading with provenance; the source issue is
    cross-linked, detached, and closed as duplicate. Nothing is
    summarized and nothing is lost.

    Examples:
        $ itree absorb owner/repo#31 --into owner/repo#14
        $ itree absorb --into owner/repo#14 --title "Small fix" --body "..."
    """
    target_ref = parse_ref(into)
    api = GithubApi.from_issue_ref(target_ref)
    try:
        target_issue = api.get_issue(target_ref.number)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(3)

    if is_grouping_issue(target_issue.title):
        print(f'Refusing: #{target_ref.number} "{target_issue.title}" is a grouping issue.')
        print("Absorb into a work unit, not a ledger.")
        sys.exit(2)
    if not target_issue.is_open:
        print(f"Refusing: target #{target_ref.number} is closed.")
        sys.exit(2)

    today = date.today().isoformat()
    if source is not None:
        source_ref = parse_ref(source)
        if source_ref.repo_ref != target_ref.repo_ref:
            print("Error: source and target must be in the same repository")
            sys.exit(1)
        if source_ref.number == target_ref.number:
            print("Error: an issue cannot absorb itself")
            sys.exit(1)
        try:
            dag = build_dag(target_ref.repo_ref)
            source_issue = dag.issues.get(source_ref.number) or api.get_issue(source_ref.number)
            section = (
                f"\n\n## Absorbed: {source_issue.title} (#{source_issue.number})\n\n"
                f"_Absorbed verbatim from #{source_issue.number} on {today}. Original: {source_issue.html_url}_\n\n"
                f"{source_issue.body or '(no body)'}"
            )
            api.update_issue_body(target_ref.number, (target_issue.body or "") + section)
            api.add_comment(
                source_ref.number,
                f"Absorbed into #{target_ref.number}; content preserved verbatim there.",
            )
            parent_number = dag.parent_of.get(source_ref.number)
            if parent_number is not None:
                api.remove_subissue(parent_number, source_issue.id)
            api.close_issue(source_ref.number, reason=IssueCloseReason.duplicate)
        except Exception as e:
            print(f"Error: {e}")
            sys.exit(3)
        print(f"Absorbed {source_ref.slug} -> {target_ref.slug}")
    else:
        if title is None:
            print("Error: provide SOURCE, or --title for not-yet-filed content")
            sys.exit(1)
        content = read_body(body, body_file)
        section = f"\n\n## Absorbed: {title}\n\n_Recorded {today}; absorbed at filing time, no separate issue created._\n\n{content or '(no body)'}"
        try:
            api.update_issue_body(target_ref.number, (target_issue.body or "") + section)
        except Exception as e:
            print(f"Error: {e}")
            sys.exit(3)
        print(f"Absorbed new content -> {target_ref.slug}")
    print(f"Next: itree doctor {target_ref.repo_ref.slug}")


@app.command(group="Structural")
def attach(
    parent: Annotated[str, Parameter(help="Parent issue as OWNER/REPO#N")],
    child: Annotated[str, Parameter(help="Child issue as OWNER/REPO#N")],
) -> None:
    """Attach an existing issue as a sub-issue of a parent.

    Example:
        $ itree attach owner/project-alpha#1 owner/project-alpha#5
        owner/project-alpha#5
    """
    req = AttachRequest(parent=parse_ref(parent), child=parse_ref(child))
    api = GithubApi.from_issue_ref(req.parent)
    try:
        child_issue = api.get_issue(req.child.number)
        api.add_subissue(req.parent.number, child_issue.id)
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
        # GitHub 422s replace_parent=true when the parent is unchanged, so a
        # same-parent reorder goes straight to the priority endpoint.
        if api.get_parent_number(req.child.number) != req.parent.number:
            api.replace_parent_subissue(req.parent.number, child_issue.id)
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
    as_json: Annotated[bool, Parameter(name="--json")] = False,
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
    *,
    as_json: Annotated[bool, Parameter(name="--json")] = False,
    show_all: Annotated[bool, Parameter(name="--all", help="Also show closed issues")] = False,
) -> None:
    """Render the repository's ordered issue tree with role annotations.

    Human ASCII by default; --json for the machine-readable tree.

    Example:
        $ itree tree owner/project-alpha
    """
    repo_ref, root_num = get_repo_root(repo)
    try:
        dag = build_dag(repo_ref)
        tree_node = dag.materialize_root(root_num)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(3)

    if as_json:
        print(json.dumps(tree_node.model_dump(), indent=2))
        return

    next_node = first_open_work_unit(tree_node)
    next_number = next_node.issue.number if next_node else None

    if show_all:
        print(render_tree(tree_node, next_number=next_number, hidden_count=0))
        return

    pruned, hidden_count = prune_closed(tree_node)
    if pruned is None:
        print(f"Root ledger #{root_num} is closed; nothing open to render. Run: itree doctor {repo_ref.slug}")
        sys.exit(1)
    print(render_tree(pruned, next_number=next_number, hidden_count=hidden_count))


@app.command(group="Query")
def next(
    repo: Annotated[str, Parameter(help="Repository as OWNER/REPO")],
    *,
    as_json: Annotated[bool, Parameter(name="--json")] = False,
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
    as_json: Annotated[bool, Parameter(name="--json")] = False,
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


def triage_root(dag: RepoDag) -> int:
    """Choose the triage root: the ledger-titled candidate when unambiguous."""
    candidates = find_root_ledger_candidates(dag)
    if not candidates:
        print_diagnostic("E001")
        sys.exit(2)
    ledger_candidates = [c for c in candidates if is_root_ledger(dag.issues[c].title)]
    if len(ledger_candidates) == 1:
        return ledger_candidates[0]
    return sorted(candidates)[0]


@app.command(group="Diagnostic")
def triage(
    target: Annotated[str, Parameter(help="Repository as OWNER/REPO, or a specific orphan as OWNER/REPO#N")],
    *,
    as_json: Annotated[bool, Parameter(name="--json")] = False,
) -> None:
    """Repair orphans one at a time: absorb, attach, or close each one.

    Surfaces the lowest-numbered orphan with its body and the candidate
    work units, and prints the exact command for each route. Re-run
    after each decision until no orphans remain.

    Example:
        $ itree triage owner/project-alpha
    """
    explicit: int | None = None
    if "#" in target:
        ref = parse_ref(target)
        repo_ref, explicit = ref.repo_ref, ref.number
    else:
        repo_ref = parse_repo(target)

    try:
        dag = build_dag(repo_ref)
    except Exception as e:
        print(f"Error fetching issues from GitHub: {e}")
        sys.exit(3)

    root_num = triage_root(dag)
    reachable: set[int] = set()
    dag._collect_reachable(root_num, reachable)
    orphans = [n for n, issue in sorted(dag.issues.items()) if issue.is_open and n not in reachable]
    tree_node = dag.materialize_root(root_num)

    if as_json:
        work_units, groupings = candidate_sections(tree_node)
        print(
            json.dumps(
                {
                    "root": root_num,
                    "orphans": [dag.issues[n].model_dump() for n in orphans],
                    "work_units": [node.issue.model_dump() for node in work_units],
                    "groupings": [node.issue.model_dump() for node in groupings],
                },
                indent=2,
            )
        )
        return

    if not orphans:
        print(f"No orphans. Every open issue is reachable from root #{root_num}.")
        print(f"Next: itree doctor {repo_ref.slug}")
        return

    current = explicit if explicit is not None else orphans[0]
    if current not in orphans:
        print(f"#{current} is not an orphan (already reachable from root #{root_num}).")
        sys.exit(1)
    issue = dag.issues[current]
    remaining = len(orphans) - 1

    print(f"Orphan 1 of {len(orphans)}: #{issue.number} {issue.title}")
    preview = (issue.body or "(no body)").strip()
    if len(preview) > 300:
        preview = preview[:300] + " ..."
    for line in preview.splitlines()[:6]:
        print(f"  | {line}")
    print()

    work_units, groupings = candidate_sections(tree_node)
    if work_units:
        print("Open work units:")
        print(candidate_lines(work_units))
    print("Grouping issues:")
    print(candidate_lines(groupings))
    print()

    slug = repo_ref.slug
    first_wu = str(work_units[0].issue.number) if work_units else "WORKUNIT"
    first_grouping = example_grouping_number(groupings, tree_node)
    print("Route it (absorb FIRST if it is less than one PR of work):")
    print("  Part of an existing work unit -> merge, keeping the body verbatim:")
    print(f"    itree absorb {slug}#{current} --into {slug}#{first_wu}")
    print("  A separate PR-sized work unit -> attach under a grouping issue:")
    print(f"    itree move {slug}#{current} --under {slug}#{first_grouping}")
    print("  Stale or never planned -> close it:")
    print(f"    itree close {slug}#{current} --reason not_planned")
    print()
    if remaining:
        print(f"{remaining} orphan{'s' if remaining != 1 else ''} remain after this one. Re-run: itree triage {slug}")
    else:
        print(f"Last orphan. Afterwards run: itree doctor {slug}")


@app.command(group="Diagnostic")
def doctor(
    repo: Annotated[str, Parameter(help="Repository as OWNER/REPO")],
    *,
    as_json: Annotated[bool, Parameter(name="--json")] = False,
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

        if report.root.kind == "present":
            root_ref = report.root.ref
            issue_title = dag.issues[root_ref.number].title
            print("Root ledger:")
            print(f"  #{root_ref.number} {issue_title}\n")
        else:
            print("Root ledger:")
            print("  None\n")

        print("Traversal:")
        if report.next_issue.kind == "present":
            next_ref = report.next_issue.ref
            next_title = dag.issues[next_ref.number].title
            print(f"  Next work unit: #{next_ref.number} {next_title}")
            print(f"  Agent instruction: work from issue #{next_ref.number}; keep planning state on that issue.")
            print("  Open the PR when implementation starts; synthesize its body from the issue.")
            print("  Keep implementation tasks in the issue body or issue comments.")
        else:
            print("  Next work unit: None")
            print("  Agent instruction: No open work units found.")
        print()

        print("Summary:")
        for k, v in report.metrics.items():
            if k == "max depth":
                print(f"  {k}: {v} / 8")
            else:
                print(f"  {k}: {v}")
        if report.root.kind == "present":
            tree_node = dag.materialize_root(report.root.ref.number)
            next_node = first_open_work_unit(tree_node)
            print(f"  {shape_summary(tree_node, next_node.issue.number if next_node else None)}")
        print()

        print("Findings:")
        if not report.findings or (len(report.findings) == 1 and report.findings[0].code == "I001"):
            print("  (none)")
        else:
            for f in report.findings:
                # Format to only display standard code summary
                print(f"  {f.code}: {len(f.evidence)} {f.title.replace('_', ' ')}")
        print()

        # Advisory Q-codes: rendered here, never part of the exit status.
        q_findings = structure_questions(dag, report, load_config(), measure_code_size(repo_ref.slug, Path.cwd()))
        print("Structure questions:")
        if not q_findings:
            print("  (none)")
        else:
            for q in q_findings:
                for ev in q.evidence:
                    print(f"  {q.code}: {ev}")
        print()

        print("Run:")
        if any(f.code in ("E010", "E011") for f in report.findings):
            print(f"  itree triage {repo_ref.slug}")
        # Suggest --explain only for a code actually present in the findings.
        non_info_findings = [f for f in report.findings if f.severity != "info"]
        if non_info_findings:
            print(f"  itree doctor {repo_ref.slug} --explain {non_info_findings[0].code}")
        print(f"  itree tree {repo_ref.slug}")
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
