from __future__ import annotations

import json
import subprocess

from pydantic import BaseModel, ConfigDict

from .models import GithubIssue, IssueCloseReason, IssueRef, RepoRef

# One paginated query returns the entire issue DAG: every issue (open and
# closed) with its ordered sub-issue edges and blocked-by edges. Sub-issue
# order IS sibling priority order. PRs never appear: repository.issues is
# issues-only in GraphQL, unlike the REST issues endpoint.
REPO_GRAPH_QUERY = """
query($owner: String!, $name: String!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    issues(states: [OPEN, CLOSED], first: 100, after: $endCursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        databaseId
        title
        state
        stateReason
        body
        url
        milestone { title }
        labels(first: 100) { nodes { name } }  # GitHub single-page max; one issue never exceeds this
        subIssues(first: 100) {
          totalCount
          nodes { number }
        }
        blockedBy(first: 50) {
          nodes { number }
        }
      }
    }
  }
}
"""


# Single-issue parent lookup: the sub-issues REST surface has no parent
# endpoint, but GraphQL exposes Issue.parent directly.
ISSUE_PARENT_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      parent { number }
    }
  }
}
"""


def list_repos(owner: str, *, timeout: int = 60) -> tuple[RepoRef, ...]:
    """List an owner's non-archived, non-fork repos that have >=1 open issue.

    ``--source`` excludes forks; ``--no-archived`` excludes archived repos;
    ``issues.totalCount`` is the open-issue prefilter so empty repos never
    incur a per-repo graph fetch. Order follows gh's default (pushed-at).
    """
    cmd = [
        "gh",
        "repo",
        "list",
        owner,
        "--source",
        "--no-archived",
        "--limit",
        "1000",
        "--json",
        "name,issues",
    ]
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"gh repo list timed out after {timeout}s for {owner}") from e
    if proc.returncode != 0:
        raise RuntimeError(f"gh repo list failed: {proc.stderr.strip() or proc.stdout.strip()}")
    repos: list[dict] = json.loads(proc.stdout)
    # gh returns issues=null when a repo has issues disabled; such a repo has
    # no open issues to scan, so skip it rather than crash on subscripting None.
    return tuple(RepoRef(owner=owner, repo=r["name"]) for r in repos if r["issues"] is not None and r["issues"]["totalCount"] > 0)


def _graphql_error_text(payload: dict) -> str:
    """Extract the error messages from a GraphQL response document.

    The GraphQL spec requires every entry in ``errors`` to carry ``message``,
    so a missing message is a malformed response and fails loudly.
    """
    errors = payload.get("errors")
    if not errors:
        return "no error details in response"
    return "; ".join(str(err["message"]) for err in errors)


class GithubApi(BaseModel):
    """Typed boundary owning all GitHub API communication.

    Construct with a RepoRef to get a typed client. Methods accept
    commands, parse JSON, and construct typed Pydantic models via
    model_validate. No raw data ever escapes.
    """

    model_config = ConfigDict(frozen=True)

    repo_ref: RepoRef

    @classmethod
    def from_repo_ref(cls, ref: RepoRef) -> GithubApi:
        """Construct from a RepoRef."""
        return cls(repo_ref=ref)

    @classmethod
    def from_issue_ref(cls, ref: IssueRef) -> GithubApi:
        """Construct from an IssueRef (extracts owner/repo)."""
        return cls(repo_ref=ref.repo_ref)

    @property
    def owner(self) -> str:
        return self.repo_ref.owner

    @property
    def repo(self) -> str:
        return self.repo_ref.repo

    def _exec(
        self,
        method: str,
        path: str,
        *,
        fields: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a GitHub CLI API command. Returns the raw subprocess result."""
        cmd = ["gh", "api", "-X", method]
        if path:
            cmd.append(path)
        for key, value in (fields or {}).items():
            cmd += ["-F", f"{key}={value}"]

        return self._run_api_command(cmd, path, timeout)

    def _run_api_command(
        self,
        cmd: list[str],
        path: str,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        try:
            proc = subprocess.run(cmd, text=True, capture_output=True, check=False, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"gh api timed out after {timeout}s: {path}") from e

        if proc.returncode != 0:
            error_msg = proc.stderr.strip() or proc.stdout.strip()
            raise RuntimeError(f"gh api failed: {error_msg}")

        return proc

    def get_issue(self, number: int) -> GithubIssue:
        """Fetch a single GitHub issue by number."""
        proc = self._exec("GET", f"repos/{self.owner}/{self.repo}/issues/{number}")
        return GithubIssue.model_validate(json.loads(proc.stdout))

    def list_subissues(self, number: int) -> tuple[GithubIssue, ...]:
        """List ALL sub-issues of a parent issue, following REST pagination.

        REST returns 30 items per page by default; this is the >100-children
        GraphQL fallback, so it must walk every page. ``--slurp`` wraps the
        per-page arrays in one array.
        """
        path = f"repos/{self.owner}/{self.repo}/issues/{number}/sub_issues?per_page=100"
        cmd = ["gh", "api", "--paginate", "--slurp", path]
        proc = self._run_api_command(cmd, path, timeout=120)
        pages: list[list[dict]] = json.loads(proc.stdout)
        return tuple(GithubIssue.model_validate(item) for page in pages for item in page)

    def fetch_repo_graph(self) -> tuple[dict, ...]:
        """Fetch the full issue DAG in one paginated GraphQL query.

        Returns the raw issue nodes (open and closed) with sub-issue and
        blocked-by edges. ``--slurp`` wraps the per-page JSON documents in
        a single array.
        """
        cmd = [
            "gh",
            "api",
            "graphql",
            "--paginate",
            "--slurp",
            "-f",
            f"query={REPO_GRAPH_QUERY}",
            "-F",
            f"owner={self.owner}",
            "-F",
            f"name={self.repo}",
        ]
        proc = self._run_api_command(cmd, "graphql", timeout=120)
        pages: list[dict] = json.loads(proc.stdout)
        nodes: list[dict] = []
        for page in pages:
            repository = page["data"]["repository"]
            if repository is None:
                raise RuntimeError(f"gh api graphql returned no repository for {self.owner}/{self.repo}: {_graphql_error_text(page)}")
            nodes.extend(repository["issues"]["nodes"])
        return tuple(nodes)

    def get_parent_number(self, number: int) -> int | None:
        """Return the issue's current parent number via GraphQL Issue.parent, or None."""
        cmd = [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={ISSUE_PARENT_QUERY}",
            "-F",
            f"owner={self.owner}",
            "-F",
            f"name={self.repo}",
            "-F",
            f"number={number}",
        ]
        proc = self._run_api_command(cmd, "graphql", timeout=30)
        payload: dict = json.loads(proc.stdout)
        repository = payload["data"]["repository"]
        if repository is None or repository["issue"] is None:
            raise RuntimeError(f"gh api graphql could not resolve {self.owner}/{self.repo}#{number}: {_graphql_error_text(payload)}")
        parent = repository["issue"]["parent"]
        return None if parent is None else int(parent["number"])

    def create_issue(self, title: str, body: str = "") -> GithubIssue:
        """Create a new issue in the repository."""
        proc = self._exec(
            "POST",
            f"repos/{self.owner}/{self.repo}/issues",
            fields={"title": title, "body": body},
        )
        return GithubIssue.model_validate(json.loads(proc.stdout))

    def add_subissue(self, parent_number: int, child_id: int) -> GithubIssue:
        """Attach a child issue as a sub-issue of a parent issue."""
        proc = self._exec(
            "POST",
            f"repos/{self.owner}/{self.repo}/issues/{parent_number}/sub_issues",
            fields={"sub_issue_id": str(child_id)},
        )
        return GithubIssue.model_validate(json.loads(proc.stdout))

    def replace_parent_subissue(self, parent_number: int, child_id: int) -> GithubIssue:
        """Attach a child issue and replace its previous parent."""
        proc = self._exec(
            "POST",
            f"repos/{self.owner}/{self.repo}/issues/{parent_number}/sub_issues",
            fields={
                "sub_issue_id": str(child_id),
                "replace_parent": "true",
            },
        )
        return GithubIssue.model_validate(json.loads(proc.stdout))

    def remove_subissue(self, parent_number: int, child_id: int) -> None:
        """Detach a child issue from its parent's sub-issues."""
        self._exec(
            "DELETE",
            f"repos/{self.owner}/{self.repo}/issues/{parent_number}/sub_issues",
            fields={"sub_issue_id": str(child_id)},
        )

    def reprioritize(
        self,
        parent_number: int,
        child_id: int,
        *,
        before_id: int | None = None,
        after_id: int | None = None,
    ) -> GithubIssue:
        """Reprioritize a sub-issue relative to its siblings."""
        fields: dict[str, str] = {"sub_issue_id": str(child_id)}
        if before_id is not None:
            fields["before_id"] = str(before_id)
        if after_id is not None:
            fields["after_id"] = str(after_id)
        proc = self._exec(
            "PATCH",
            f"repos/{self.owner}/{self.repo}/issues/{parent_number}/sub_issues/priority",
            fields=fields,
        )
        return GithubIssue.model_validate(json.loads(proc.stdout))

    def add_comment(self, number: int, body: str) -> None:
        """Post a comment on an issue."""
        self._exec(
            "POST",
            f"repos/{self.owner}/{self.repo}/issues/{number}/comments",
            fields={"body": body},
        )

    def close_issue(
        self,
        number: int,
        *,
        comment: str | None = None,
        reason: IssueCloseReason = IssueCloseReason.completed,
    ) -> GithubIssue:
        """Close a GitHub issue with an optional comment and reason."""
        if comment:
            self.add_comment(number, comment)
        proc = self._exec(
            "PATCH",
            f"repos/{self.owner}/{self.repo}/issues/{number}",
            fields={"state": "closed", "state_reason": reason.value},
        )
        return GithubIssue.model_validate(json.loads(proc.stdout))

    def update_issue_body(self, number: int, body: str) -> GithubIssue:
        """Update the body of a GitHub issue."""
        proc = self._exec(
            "PATCH",
            f"repos/{self.owner}/{self.repo}/issues/{number}",
            fields={"body": body},
        )
        return GithubIssue.model_validate(json.loads(proc.stdout))
