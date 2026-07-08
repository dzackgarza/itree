from __future__ import annotations

from .github import GithubApi
from .models import GithubIssue, RepoDag, RepoRef


def build_dag(repo_ref: RepoRef, api: GithubApi | None = None) -> RepoDag:
    """Construct the full issue DAG from one paginated GraphQL query.

    Every issue (open and closed) becomes a node, so closed parents that
    hide open descendants are observable. Sub-issue edge order is sibling
    priority order. GraphQL issue nodes are never pull requests.

    Args:
        repo_ref: A RepoRef identifying the repository.
        api: Optional pre-constructed GithubApi instance.

    Returns:
        A RepoDag containing every issue and its parent-child edges.
    """
    if api is None:
        api = GithubApi.from_repo_ref(repo_ref)

    nodes = api.fetch_repo_graph()
    issues: dict[int, GithubIssue] = {node["number"]: GithubIssue.from_graphql(node) for node in nodes}

    children_of: dict[int, tuple[int, ...]] = {}
    dependencies: dict[int, tuple[int, ...]] = {}
    for node in nodes:
        number = node["number"]
        sub = node["subIssues"]
        child_numbers = [child["number"] for child in sub["nodes"]]
        if sub["totalCount"] > len(child_numbers):
            # >100 children: the GraphQL page is truncated; fall back to the
            # REST sub-issues endpoint for this one node to keep edges complete.
            child_numbers = [child.number for child in api.list_subissues(number) if not child.is_pull_request]
        present = tuple(child for child in child_numbers if child in issues)
        if present:
            children_of[number] = present

        blockers = tuple(blocker["number"] for blocker in node["blockedBy"]["nodes"] if blocker["number"] in issues)
        if blockers:
            dependencies[number] = blockers

    return RepoDag(
        repo_ref=repo_ref,
        issues=issues,
        children_of=children_of,
        dependencies=dependencies,
    )
