from __future__ import annotations

from .github import GithubApi
from .models import GithubIssue, RepoDag, RepoRef


def build_dag(repo_ref: RepoRef, api: GithubApi | None = None) -> RepoDag:
    """Construct the full issue DAG by scanning every issue and its sub-issues.

    This is the foundational operation: it fetches ALL issues in the repo,
    discovers every parent-child relationship via the sub-issues API,
    and returns a RepoDag that all query commands operate on.

    Args:
        repo_ref: A RepoRef identifying the repository.
        api: Optional pre-constructed GithubApi instance.

    Returns:
        A RepoDag containing every issue and its parent-child edges.
    """
    if api is None:
        api = GithubApi.from_repo_ref(repo_ref)

    all_issues = tuple(issue for issue in api.list_all_issues() if not issue.is_pull_request)
    issues_by_number: dict[int, GithubIssue] = {i.number: i for i in all_issues}

    # Build parent->children adjacency list by scanning every issue's sub-issues.
    # Filter children to only those present in issues_by_number: the GitHub
    # sub-issues API returns ALL children (open and closed), but list_all_issues
    # only returns open issues, so closed children won't be in our dict.
    children_of: dict[int, tuple[int, ...]] = {}
    dependencies: dict[int, tuple[int, ...]] = {}
    for issue in all_issues:
        children = api.list_subissues(issue.number)
        if children:
            present = tuple(c.number for c in children if not c.is_pull_request and c.number in issues_by_number)
            if present:
                children_of[issue.number] = present

        # Check blocked_by dependencies
        blocked_by = api.list_blocked_by(issue.number)
        if blocked_by:
            present_blockers = tuple(b.number for b in blocked_by if not b.is_pull_request and b.number in issues_by_number)
            if present_blockers:
                dependencies[issue.number] = present_blockers

    return RepoDag(
        repo_ref=repo_ref,
        issues=issues_by_number,
        children_of=children_of,
        dependencies=dependencies,
    )
