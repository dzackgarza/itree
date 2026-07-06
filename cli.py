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
from pydantic import validate_call

from .github import GithubApi
from .models import AttachRequest, DetachRequest, IssueCloseReason, IssueRef, MoveRequest, RepoRef
from .traversal import get_descendants_preorder, get_direct_children, get_path, materialize, next_leaf
from .validate import full_validate


@validate_call
def create_root_issue(repo: RepoRef, title: str, body: str = "") -> str:
    """Create a new root issue and return its reference string."""
    api = GithubApi.from_repo_ref(repo)
    issue = api.create_issue(title, body)
    return f"{repo.owner}/{repo.repo}#{issue.number}"


@validate_call
def add_child_to_root(root: IssueRef, title: str, body: str = "") -> str:
    """Create a child issue and attach it to root. Returns child reference string."""
    api = GithubApi.from_issue_ref(root)
    child = api.create_issue(title, body)
    child_ref = IssueRef(owner=root.owner, repo=root.repo, number=child.number)
    api.add_subissue(root.number, child.id, replace_parent=False)
    return child_ref.slug


@validate_call
def do_attach(parent: IssueRef, child: IssueRef, replace_parent: bool = False) -> str:
    """Attach child as sub-issue of parent. Returns child reference string."""
    req = AttachRequest(parent=parent, child=child, replace_parent=replace_parent)
    api = GithubApi.from_issue_ref(req.parent)
    child_issue = api.get_issue(req.child.number)
    api.add_subissue(req.parent.number, child_issue.id, replace_parent=req.replace_parent)
    return req.child.slug


@validate_call
def do_detach(parent: IssueRef, child: IssueRef) -> str:
    """Detach child from parent. Returns child reference string."""
    req = DetachRequest(parent=parent, child=child)
    api = GithubApi.from_issue_ref(req.parent)
    child_issue = api.get_issue(req.child.number)
    api.remove_subissue(req.parent.number, child_issue.id)
    return req.child.slug


@validate_call
def do_move(
    child: IssueRef,
    parent: IssueRef,
    before: IssueRef | None = None,
    after: IssueRef | None = None,
) -> str:
    """Move child under parent, optionally positioned relative to siblings."""
    req = MoveRequest(child=child, parent=parent, before=before, after=after)
    api = GithubApi.from_issue_ref(req.parent)
    child_issue = api.get_issue(req.child.number)
    api.add_subissue(req.parent.number, child_issue.id, replace_parent=True)
    if req.before is not None or req.after is not None:
        before_id = api.get_issue(req.before.number).id if req.before is not None else None
        after_id = api.get_issue(req.after.number).id if req.after is not None else None
        api.reprioritize(req.parent.number, child_issue.id, before_id=before_id, after_id=after_id)
    return req.child.slug


@validate_call
def get_children_json(root: IssueRef, recursive: bool = False) -> list[dict]:
    """Get children as list of dicts for JSON output.

    Args:
        root: The root issue reference.
        recursive: If True, returns all descendants; if False, direct children only.

    Returns:
        List of issue dictionaries.
    """
    if recursive:
        nodes = get_descendants_preorder(root)
    else:
        nodes = get_direct_children(root)
    return [node.issue.model_dump() for node in nodes]


@validate_call
def get_tree_json(root: IssueRef) -> dict:
    """Get full tree as dict for JSON output."""
    node = materialize(root)
    return node.model_dump()


@validate_call
def get_next_leaf_json(root: IssueRef) -> dict | None:
    """Get first open leaf as dict for JSON output."""
    node = next_leaf(root)
    if node is None:
        return None
    return node.issue.model_dump()


@validate_call
def get_path_json(issue: IssueRef, root: IssueRef | None = None) -> list[dict] | None:
    """Get path from root to issue as list of dicts."""
    if root is None:
        root = issue
    path_nodes = get_path(issue, root)
    if path_nodes is None:
        return None
    return [node.issue.model_dump() for node in path_nodes]


@validate_call
def validate_tree_command(root: IssueRef) -> list[dict]:
    """Validate tree and return violations as list of dicts."""
    violations = full_validate(root)
    return [v.model_dump() for v in violations]


@validate_call
def do_close(
    issue: IssueRef,
    comment: str | None = None,
    reason: IssueCloseReason = IssueCloseReason.completed,
) -> str:
    """Close issue with optional comment and reason. Returns issue reference string."""
    api = GithubApi.from_issue_ref(issue)
    closed = api.close_issue(issue.number, comment=comment, reason=reason)
    return f"{issue.owner}/{issue.repo}#{closed.number}"


# CLI definitions - thin wrappers only

app = App(help="Deterministic traversal for GitHub sub-issue trees.")


def parse_ref(raw: str) -> IssueRef:
    """Parse an issue reference string into an IssueRef."""
    return IssueRef.parse(raw)


def parse_repo(raw: str) -> RepoRef:
    """Parse a repository reference string into a RepoRef."""
    return RepoRef.parse(raw)


@app.command
@validate_call
def init(
    repo: Annotated[str, Parameter(help="Repository as OWNER/REPO")],
    title: Annotated[str, Parameter(help="Title for the root issue")],
    *,
    body: Annotated[str, Parameter(help="Issue body in Markdown")] = "",
) -> None:
    """Create a new root issue for a traversal domain."""
    repo_ref = parse_repo(repo)
    ref = create_root_issue(repo_ref, title, body)
    print(ref)


@app.command
def add(
    root: Annotated[str, Parameter(help="Root issue as OWNER/REPO#N")],
    title: Annotated[str, Parameter(help="Title for the new child issue")],
    *,
    body: Annotated[str, Parameter(help="Issue body in Markdown")] = "",
) -> None:
    """Create a new child issue and attach it to the root."""
    root_ref = parse_ref(root)
    child_ref = add_child_to_root(root_ref, title, body)
    print(child_ref)


@app.command
def attach(
    parent: Annotated[str, Parameter(help="Parent issue as OWNER/REPO#N")],
    child: Annotated[str, Parameter(help="Child issue as OWNER/REPO#N")],
    *,
    replace_parent: Annotated[bool, Parameter()] = False,
) -> None:
    """Attach an existing issue as a sub-issue of a parent."""
    child_ref = do_attach(parse_ref(parent), parse_ref(child), replace_parent)
    print(child_ref)


@app.command
def detach(
    parent: Annotated[str, Parameter(help="Parent issue as OWNER/REPO#N")],
    child: Annotated[str, Parameter(help="Child issue as OWNER/REPO#N")],
) -> None:
    """Detach a sub-issue from its parent."""
    child_ref = do_detach(parse_ref(parent), parse_ref(child))
    print(child_ref)


@app.command
def move(
    child: Annotated[str, Parameter(help="Issue to move as OWNER/REPO#N")],
    under: Annotated[str, Parameter(help="New parent as OWNER/REPO#N")],
    *,
    before: Annotated[str | None, Parameter(help="Place before sibling OWNER/REPO#N")] = None,
    after: Annotated[str | None, Parameter(help="Place after sibling OWNER/REPO#N")] = None,
) -> None:
    """Move issue under new parent, optionally positioned relative to siblings."""
    child_ref = parse_ref(child)
    parent_ref = parse_ref(under)
    before_ref = parse_ref(before) if before else None
    after_ref = parse_ref(after) if after else None
    result = do_move(child_ref, parent_ref, before=before_ref, after=after_ref)
    print(result)


@app.command
def children(
    root: Annotated[str, Parameter(help="Root issue as OWNER/REPO#N")],
    *,
    recursive: Annotated[bool, Parameter()] = False,
    as_json: Annotated[bool, Parameter()] = False,
) -> None:
    """List children of an issue."""
    root_ref = parse_ref(root)
    if recursive:
        nodes = get_descendants_preorder(root_ref)
    else:
        nodes = get_direct_children(root_ref)
    if as_json:
        result = [node.issue.model_dump() for node in nodes]
        print(json.dumps(result, indent=2))
    else:
        for node in nodes:
            print(f"#{node.issue.number}: {node.issue.title}")


@app.command
def tree(root: Annotated[str, Parameter(help="Root issue as OWNER/REPO#N")]) -> None:
    """Print the materialized rooted ordered tree as JSON."""
    root_ref = parse_ref(root)
    result = get_tree_json(root_ref)
    print(json.dumps(result, indent=2))


@app.command
def next(
    root: Annotated[str, Parameter(help="Root issue as OWNER/REPO#N")],
    *,
    as_json: Annotated[bool, Parameter()] = False,
) -> None:
    """Print the first open leaf under ROOT in preorder."""
    root_ref = parse_ref(root)
    if as_json:
        result = get_next_leaf_json(root_ref)
        if result is None:
            print("{}")
        else:
            print(json.dumps(result, indent=2))
    else:
        node = next_leaf(root_ref)
        if node is None:
            print("No open leaves found")
        else:
            print(f"#{node.issue.number}: {node.issue.title}")


@app.command
def path(
    issue: Annotated[str, Parameter(help="Issue as OWNER/REPO#N")],
    *,
    root: Annotated[str | None, Parameter(help="Root issue as OWNER/REPO#N")] = None,
    as_json: Annotated[bool, Parameter()] = False,
) -> None:
    """Print the path from root to the given issue."""
    issue_ref = parse_ref(issue)
    root_ref = parse_ref(root) if root else None
    if as_json:
        result = get_path_json(issue_ref, root_ref)
        if result is None:
            print("[]")
        else:
            print(json.dumps(result, indent=2))
    else:
        path_nodes = get_path(issue_ref, root_ref or issue_ref)
        if path_nodes is None:
            print(f"Issue #{issue_ref.number} not found")
        else:
            for node in path_nodes:
                print(f"#{node.issue.number}: {node.issue.title}")


@app.command
def validate(root: Annotated[str, Parameter(help="Root issue as OWNER/REPO#N")]) -> None:
    """Validate the tree rooted at ROOT."""
    root_ref = parse_ref(root)
    result = validate_tree_command(root_ref)
    print(json.dumps(result, indent=2))


@app.command
@validate_call
def close(
    issue: Annotated[str, Parameter(help="Issue to close as OWNER/REPO#N")],
    *,
    comment: Annotated[str | None, Parameter(help="Comment to post when closing")] = None,
    reason: Annotated[
        IssueCloseReason,
        Parameter(help="Reason for closing: completed, not_planned, or reopened"),
    ] = IssueCloseReason.completed,
) -> None:
    """Close an issue with optional comment and reason."""
    issue_ref = parse_ref(issue)
    result = do_close(issue_ref, comment, reason)
    print(result)


if __name__ == "__main__":
    app()
