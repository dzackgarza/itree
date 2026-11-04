from __future__ import annotations

from collections.abc import Callable

from .github import GithubApi
from .models import GithubIssue, IssueRef, RepoDag, RepoRef

# Resolver for the >100-children fallback: given a parent number, return its
# full sub-issue list. In production this is ``GithubApi.list_subissues``; the
# pure transform depends only on this callable, never on the API object.
SubissueResolver = Callable[[int], tuple[GithubIssue, ...]]


def dag_from_graph_nodes(
    repo_ref: RepoRef,
    nodes: tuple[dict, ...],
    subissue_resolver: SubissueResolver,
) -> RepoDag:
    """Build the issue DAG from raw GraphQL nodes (the pure transform).

    Every issue (open and closed) becomes a node, so closed parents that hide
    open descendants are observable. Sub-issue edge order is sibling priority
    order. When a node reports more children than the fetched page held,
    ``subissue_resolver`` supplies the complete list for that one parent.

    This is IO-free: it consumes already-fetched ``nodes`` and a resolver, so
    it can be proven directly against captured GraphQL shapes.
    """
    issues: dict[int, GithubIssue] = {node["number"]: GithubIssue.from_graphql(node) for node in nodes}

    children_of: dict[int, tuple[int, ...]] = {}
    dependencies: dict[int, tuple[int, ...]] = {}
    foreign_children: dict[int, tuple[IssueRef, ...]] = {}
    foreign_blockers: dict[int, tuple[IssueRef, ...]] = {}
    for node in nodes:
        number = node["number"]
        sub = node["subIssues"]
        child_refs = [_edge_ref(child) for child in sub["nodes"]]
        if sub["totalCount"] > len(child_refs):
            # >100 children: the GraphQL page is truncated; fall back to the
            # REST sub-issues endpoint for this one node to keep edges complete.
            child_refs = [IssueRef(repo_ref=RepoRef.parse(child.repository_slug), number=child.number) for child in subissue_resolver(number) if not child.is_pull_request]
        present = tuple(ref.number for ref in child_refs if _same_repo(ref, repo_ref) and ref.number in issues)
        if present:
            children_of[number] = present
        foreign = tuple(ref for ref in child_refs if not _same_repo(ref, repo_ref))
        if foreign:
            foreign_children[number] = foreign

        blocker_refs = [_edge_ref(blocker) for blocker in node["blockedBy"]["nodes"]]
        blockers = tuple(ref.number for ref in blocker_refs if _same_repo(ref, repo_ref) and ref.number in issues)
        if blockers:
            dependencies[number] = blockers
        foreign_blocker_refs = tuple(ref for ref in blocker_refs if not _same_repo(ref, repo_ref))
        if foreign_blocker_refs:
            foreign_blockers[number] = foreign_blocker_refs

    return RepoDag(
        repo_ref=repo_ref,
        issues=issues,
        children_of=children_of,
        dependencies=dependencies,
        foreign_children=foreign_children,
        foreign_blockers=foreign_blockers,
    )


def _edge_ref(edge: dict) -> IssueRef:
    """Key a GraphQL sub-issue or blocker node by repository and number."""
    return IssueRef(repo_ref=RepoRef.parse(edge["repository"]["nameWithOwner"]), number=edge["number"])


def _same_repo(ref: IssueRef, repo_ref: RepoRef) -> bool:
    """GitHub owner and repository names are case-insensitive."""
    return ref.repo_ref.slug.casefold() == repo_ref.slug.casefold()


def build_dag(repo_ref: RepoRef, api: GithubApi | None = None) -> RepoDag:
    """Construct the full issue DAG from one paginated GraphQL query.

    Thin IO shell: fetch the repo graph, then apply the pure
    ``dag_from_graph_nodes`` transform with the live REST fallback.

    Args:
        repo_ref: A RepoRef identifying the repository.
        api: Optional pre-constructed GithubApi instance.

    Returns:
        A RepoDag containing every issue and its parent-child edges.
    """
    if api is None:
        api = GithubApi.from_repo_ref(repo_ref)

    nodes = api.fetch_repo_graph()
    return dag_from_graph_nodes(repo_ref, nodes, api.list_subissues)
