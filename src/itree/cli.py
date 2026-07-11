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

import importlib.resources
import json
import shlex
import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Annotated

from cyclopts import App, Parameter

from .github import GithubApi, list_repos
from .metrics import load_config, measure_code_size, structure_questions
from .milestone import execute_milestone, preflight_milestone
from .models import (
    AssignedPriorMilestone,
    AttachRequest,
    CreateMilestoneRequest,
    DetachRequest,
    FindingSeverity,
    GithubIssue,
    IssueCloseReason,
    IssueRef,
    MilestoneCreationFailed,
    MilestonePreflightRejected,
    MilestoneTitle,
    MoveRequest,
    ParentedPriorPlacement,
    PlacementInquiry,
    PlannedMilestoneEffect,
    RepoDag,
    RepoHealth,
    RepoRef,
    TreeNode,
    WorkUnitMilestoneEffect,
)
from .render import prune_closed, render_scan, render_tree, shape_summary
from .traversal import build_dag
from .validate import (
    DIAGNOSTIC_CATALOG,
    find_root_ledger_candidates,
    first_open_work_unit,
    generate_doctor_report,
    is_grouping_issue,
    is_root_ledger,
    repo_health,
)

# Core ontology and help text
CORE_HELP_TEXT = """itree maintains one deterministic GitHub issue tree per repo
and names the single next work unit to do.

  One repo has exactly one root ledger. Every open work issue is reachable
  from it. Sub-issue order is traversal order. The next work unit is the
  first open non-ledger issue in preorder.

  Grouping issues (ledger, milestone, backlog) order work. Work units are
  leaves: checklists, proof, and status live in their body or comments, not
  in child issues.

Run `itree help model` for the full organization model, repo state machine,
guard rails, and proportionality doctrine.
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
    """Print the full organization model: ontology, repo state machine, the four
    guard rails, and proportionality doctrine (the packaged WORKFLOWS.md).
    """
    # Read lazily and as UTF-8: the doc holds non-ASCII (e.g. U+2026), and the
    # read stays inside this command so a packaging regression degrades only
    # `help model`, never every import of itree.cli.
    doc = importlib.resources.files("itree").joinpath("WORKFLOWS.md").read_text(encoding="utf-8")
    print(doc, end="")


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


def reachable_issue_numbers(dag: RepoDag, root_number: int) -> set[int]:
    """Collect one repository root's reachable issue numbers without recursion."""
    reachable: set[int] = set()
    pending = [root_number]
    while pending:
        number = pending.pop()
        if number in reachable:
            continue
        reachable.add(number)
        pending.extend(reversed(dag.children_of[number]))
    return reachable


def issue_lines(issues: Sequence[GithubIssue]) -> str:
    """Render numbered issues for non-mutating milestone placement guidance."""
    return "\n".join(f"  #{issue.number} {issue.title}" for issue in issues) if issues else "  (none)"


def print_milestone_placement(
    inquiry: PlacementInquiry,
    dag: RepoDag,
    body: str,
    body_file: str | None,
    issues: Sequence[str],
) -> None:
    """Render the required, write-incapable response when ``--under`` is absent."""
    roots = tuple(issue for issue in dag.roots if issue.is_open and is_root_ledger(issue.title))
    if len(roots) != 1:
        print("Refusing before mutation: the repository does not have one open root ledger.")
        print(issue_lines(roots))
        sys.exit(2)

    reachable = reachable_issue_numbers(dag, roots[0].number)
    milestone_ledgers = tuple(
        issue for number, issue in sorted(dag.issues.items()) if number in reachable and issue.is_open and issue.title.casefold().startswith("milestone:")
    )
    grouping_targets = tuple(issue for number, issue in sorted(dag.issues.items()) if number in reachable and issue.is_open and is_grouping_issue(issue.title))

    print("Nothing was created. --under is required before milestone mutation.\n")
    print("Existing milestone ledgers:")
    print(issue_lines(milestone_ledgers))
    print("\nValid grouping targets:")
    print(issue_lines(grouping_targets))

    target = grouping_targets[0]
    command = [
        "itree",
        "milestone",
        inquiry.repo_ref.slug,
        inquiry.title.value,
        "--under",
        f"{inquiry.repo_ref.slug}#{target.number}",
    ]
    if body_file is not None:
        command.extend(("--body-file", body_file))
    elif body:
        command.extend(("--body", body))
    if issues:
        command.extend(("--issues", *issues))
    print("\nRun with the intended grouping target:")
    print(f"  {shlex.join(command)}")


def describe_milestone_effect(effect: PlannedMilestoneEffect) -> str:
    """Render one closed effect variant without erasing its target."""
    if isinstance(effect, WorkUnitMilestoneEffect):
        return f"{effect.kind.value} {effect.ref.slug}"
    return effect.kind.value


def print_milestone_failure(failure: MilestoneCreationFailed) -> None:
    """Report typed partial progress without implying rollback or completion."""
    print(f"Milestone creation stopped: outcome={failure.outcome.kind}")
    print(f"  detail: {failure.outcome.detail}")
    print("  confirmed complete:")
    if failure.progress.confirmed:
        for effect in failure.progress.confirmed:
            print(f"    {describe_milestone_effect(effect)}")
    else:
        print("    (none)")
    print("  current operation:")
    print(f"    {describe_milestone_effect(failure.progress.current)}")
    print("  confirmed not attempted:")
    if failure.progress.untouched:
        for effect in failure.progress.untouched:
            print(f"    {describe_milestone_effect(effect)}")
    else:
        print("    (none)")
    print("  preflight work-unit state:")
    if failure.progress.work_units:
        for work_unit in failure.progress.work_units:
            prior_placement = work_unit.prior_placement
            parent = f"parent=#{prior_placement.parent_number} position={prior_placement.position}" if isinstance(prior_placement, ParentedPriorPlacement) else "parentless"
            prior_milestone = work_unit.prior_milestone
            milestone = f"assigned title={prior_milestone.title!r}" if isinstance(prior_milestone, AssignedPriorMilestone) else "unassigned"
            print(f"    {work_unit.ref.slug} {parent} milestone={milestone}")
    else:
        print("    (none)")
    print("Recovery: reread the live GitHub milestone, issue tree, and assignments before acting.")


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
def milestone(
    repo: Annotated[str, Parameter(help="Repository as OWNER/REPO")],
    title: Annotated[str, Parameter(help="Shared GitHub milestone and ledger title")],
    *,
    under: Annotated[
        str | None,
        Parameter(help="Open grouping parent as OWNER/REPO#N"),
    ] = None,
    body: Annotated[str, Parameter(help="Milestone ledger body in Markdown")] = "",
    body_file: Annotated[
        str | None,
        Parameter(help="Read the milestone ledger body from a file"),
    ] = None,
    issues: Annotated[
        list[str] | None,
        Parameter(
            help="Ordered work-unit references as OWNER/REPO#N",
            consume_multiple=True,
            allow_repeating=False,
        ),
    ] = None,
) -> None:
    """Create a GitHub Milestone and its issue-tree ledger.

    Without --under this creates nothing and prints existing milestone
    ledgers, valid grouping targets, and an exact command with placement.
    With placement supplied, one complete preflight precedes the ordered
    milestone, ledger, parentage, and assignment writes. A rejected or
    indeterminate write stops the untouched suffix without rollback.

    Example:
        $ itree milestone owner/project-alpha "release 2" \\
            --under owner/project-alpha#2 \\
            --issues owner/project-alpha#5 owner/project-alpha#7
        owner/project-alpha#8 milestone=3
    """
    inquiry = PlacementInquiry(
        repo_ref=parse_repo(repo),
        title=MilestoneTitle.parse(title),
    )
    ordered_issue_arguments = tuple(issues) if issues is not None else ()
    api = GithubApi.from_repo_ref(inquiry.repo_ref)
    dag = build_dag(inquiry.repo_ref, api=api)

    if under is None:
        print_milestone_placement(
            inquiry,
            dag,
            body,
            body_file,
            ordered_issue_arguments,
        )
        sys.exit(1)

    request = CreateMilestoneRequest(
        repo_ref=inquiry.repo_ref,
        title=inquiry.title,
        parent=parse_ref(under),
        body=read_body(body, body_file),
        work_units=tuple(parse_ref(raw) for raw in ordered_issue_arguments),
    )
    preflight = preflight_milestone(request, dag, api.list_milestones())
    if isinstance(preflight, MilestonePreflightRejected):
        print(f"Refusing before mutation: {preflight.kind.value}")
        for reference in preflight.references:
            print(f"  {reference}")
        sys.exit(2)

    result = execute_milestone(preflight, api)
    if isinstance(result, MilestoneCreationFailed):
        print_milestone_failure(result)
        sys.exit(3)

    print(f"{request.repo_ref.slug}#{result.ledger.number} milestone={result.milestone.number}")


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

    config = load_config()
    report = generate_doctor_report(dag, deferral_label=config.deferral_label)

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

        m = report.metrics
        print("Summary:")
        print(f"  errors: {m.errors}")
        print(f"  warnings: {m.warnings}")
        print(f"  open issues reachable from root: {m.open_issues_reachable_from_root}")
        print(f"  open issues outside root: {m.open_issues_outside_root}")
        print(f"  open work units: {m.open_work_units}")
        print(f"  work units: {m.work_units}")
        print(f"  max depth: {m.max_depth} / 8")
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
        q_findings = structure_questions(dag, report, config, measure_code_size(repo_ref.slug, Path.cwd()))
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


@app.command(group="Diagnostic")
def scan(
    owner: Annotated[str, Parameter(help="GitHub account login to scan")],
    *,
    as_json: Annotated[bool, Parameter(name="--json")] = False,
) -> None:
    """Account-wide health scan: one line per issue-bearing repo.

    Lists the owner's non-archived, non-fork repos with at least one open
    issue, fetches each issue tree, and prints open count, root status,
    error count, and next work unit, with a footer naming the worst repos.

    Example:
        $ itree scan owner
    """
    try:
        repos = list_repos(owner)
    except Exception as e:
        print(f"Error listing repos for {owner}: {e}")
        sys.exit(3)

    # Read config once at the command boundary (mirrors doctor), then apply the
    # same deferral_label to every scanned repo.
    deferral_label = load_config().deferral_label

    # Each repo is an independent, IO-bound gh round-trip; fetch them
    # concurrently and keep the owner's repo order in the output.
    def health_of(repo_ref: RepoRef) -> RepoHealth | tuple[str, str]:
        try:
            return repo_health(build_dag(repo_ref), deferral_label=deferral_label)
        except Exception as e:
            return (repo_ref.slug, str(e))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(health_of, repos))

    healths = [r for r in results if isinstance(r, RepoHealth)]
    fetch_errors: list[tuple[str, str]] = [r for r in results if isinstance(r, tuple)]

    if as_json:
        print(
            json.dumps(
                {
                    "repos": [h.model_dump() for h in healths],
                    "errors": [{"repo": slug, "message": msg} for slug, msg in fetch_errors],
                },
                indent=2,
            )
        )
        return

    print(render_scan(healths, fetch_errors))


if __name__ == "__main__":
    app()
