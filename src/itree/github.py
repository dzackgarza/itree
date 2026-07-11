from __future__ import annotations

import json
import re
import subprocess

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from .models import (
    GithubIndeterminateOperation,
    GithubIssue,
    GithubMilestone,
    GithubRejectedOperation,
    IssueCloseReason,
    IssueRef,
    MilestoneTitle,
    PlannedMilestoneEffect,
    RemoteOperationFailure,
    RepoRef,
)

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


class GithubMutationError(RuntimeError):
    """Base exception carrying a typed terminal mutation outcome."""

    outcome: RemoteOperationFailure

    def __init__(self, outcome: RemoteOperationFailure) -> None:
        self.outcome = outcome
        super().__init__(outcome.detail)


class GithubRejectedError(GithubMutationError):
    """GitHub returned an explicit nonzero response."""


class GithubIndeterminateError(GithubMutationError):
    """The mutation response was lost, interrupted, or unusable."""


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
    return issue_bearing_repos(owner, json.loads(proc.stdout))


def issue_bearing_repos(owner: str, payload: list[dict]) -> tuple[RepoRef, ...]:
    """Keep only the payload repos that have at least one open issue.

    Pure filter over the ``gh repo list --json name,issues`` shape. gh returns
    ``issues=null`` when a repo has issues disabled; such a repo has no open
    issues to scan, so skip it rather than crash on subscripting None.
    """
    return tuple(RepoRef(owner=owner, repo=r["name"]) for r in payload if r["issues"] is not None and r["issues"]["totalCount"] > 0)


def _graphql_error_text(payload: dict) -> str:
    """Extract the error messages from a GraphQL response document.

    The GraphQL spec requires every entry in ``errors`` to carry ``message``,
    so a missing message is a malformed response and fails loudly.
    """
    errors = payload.get("errors")
    if not errors:
        return "no error details in response"
    return "; ".join(str(err["message"]) for err in errors)


def _graphql_data(document: dict, identity: str) -> dict:
    """Return the ``data`` envelope of a GraphQL response document.

    A successful GraphQL response carries a top-level ``data`` object; an absent
    or null ``data`` is a failed/malformed document (typically an ``errors``-only
    envelope). Assert that envelope invariant once here — dumping the offending
    document — so callers subscript the guaranteed keys of ``data`` directly.
    """
    data = document.get("data")
    assert isinstance(data, dict), f"gh api graphql: no data envelope for {identity}; document was {document!r}"
    return data


def parse_subissues_pages(raw: str) -> tuple[GithubIssue, ...]:
    """Parse ``--slurp`` sub-issue pages (an array of REST page arrays).

    Pure counterpart to ``list_subissues``: flattens every page in order so
    >100-child parents keep all their children.
    """
    pages: list[list[dict]] = json.loads(raw)
    return tuple(GithubIssue.model_validate(item) for page in pages for item in page)


def parse_repo_graph_pages(raw: str, owner: str, repo: str) -> tuple[dict, ...]:
    """Parse ``--slurp`` GraphQL pages into a flat tuple of issue nodes.

    Pure counterpart to ``fetch_repo_graph``. A ``repository: null`` page means
    the repo is missing or inaccessible; surface the API's own error text.
    """
    pages: list[dict] = json.loads(raw)
    nodes: list[dict] = []
    for page in pages:
        repository = _graphql_data(page, f"{owner}/{repo}")["repository"]
        if repository is None:
            raise RuntimeError(f"gh api graphql returned no repository for {owner}/{repo}: {_graphql_error_text(page)}")
        nodes.extend(repository["issues"]["nodes"])
    return tuple(nodes)


def parse_issue_parent(raw: str, owner: str, repo: str, number: int) -> int | None:
    """Parse the GraphQL ``Issue.parent`` response into a parent number or None.

    Pure counterpart to ``get_parent_number``. A null repository or issue means
    the reference could not be resolved and fails loudly.
    """
    payload: dict = json.loads(raw)
    repository = _graphql_data(payload, f"{owner}/{repo}#{number}")["repository"]
    if repository is None or repository["issue"] is None:
        raise RuntimeError(f"gh api graphql could not resolve {owner}/{repo}#{number}: {_graphql_error_text(payload)}")
    parent = repository["issue"]["parent"]
    return None if parent is None else int(parent["number"])


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

    def _run_mutation_command(
        self,
        command: list[str],
        path: str,
        effect: PlannedMilestoneEffect,
        *,
        timeout: int,
    ) -> str:
        """Run one concrete mutation while preserving rejected versus unknown outcome."""
        try:
            proc = subprocess.run(
                command,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise GithubIndeterminateError(
                GithubIndeterminateOperation(
                    effect=effect,
                    detail=(f"gh mutation timed out after invocation; path={path}; timeout={timeout}s; reread live GitHub state"),
                )
            ) from error
        except KeyboardInterrupt as error:
            raise GithubIndeterminateError(
                GithubIndeterminateOperation(
                    effect=effect,
                    detail=(f"gh mutation was interrupted after invocation; path={path}; reread live GitHub state"),
                )
            ) from error

        status_code, body = self._parse_included_mutation_response(
            proc.stdout,
            effect,
            path,
            proc.returncode,
            proc.stderr,
        )
        if status_code >= 400:
            raise GithubRejectedError(
                GithubRejectedOperation(
                    effect=effect,
                    detail=(
                        f"GitHub API rejected mutation; path={path}; "
                        f"http_status={status_code}; exit={proc.returncode}; "
                        f"response={body.strip()!r}; stderr={proc.stderr.strip()!r}"
                    ),
                )
            )
        if proc.returncode != 0 or status_code < 200 or status_code >= 300:
            raise GithubIndeterminateError(
                GithubIndeterminateOperation(
                    effect=effect,
                    detail=(
                        "gh mutation ended without a confirmed GitHub rejection or usable success; "
                        f"path={path}; http_status={status_code}; exit={proc.returncode}; "
                        "reread live GitHub state"
                    ),
                )
            )
        return body

    @staticmethod
    def _parse_included_mutation_response(
        response: str,
        effect: PlannedMilestoneEffect,
        path: str,
        exit_code: int,
        stderr: str,
    ) -> tuple[int, str]:
        """Extract the HTTP proof required to classify a mutation outcome."""
        headers, separator, body = response.partition("\n\n")
        status_line = headers.splitlines()[0] if headers else ""
        matched = re.fullmatch(r"HTTP/\S+ (?P<status>[1-5]\d{2})(?: .*)?", status_line)
        if not separator or matched is None:
            raise GithubIndeterminateError(
                GithubIndeterminateOperation(
                    effect=effect,
                    detail=(
                        f"gh mutation returned no usable HTTP response after invocation; path={path}; exit={exit_code}; stderr={stderr.strip()!r}; reread live GitHub state"
                    ),
                )
            )
        return int(matched["status"]), body

    @staticmethod
    def _parse_issue_mutation(
        response: str,
        effect: PlannedMilestoneEffect,
    ) -> GithubIssue:
        try:
            return GithubIssue.model_validate_json(response)
        except ValidationError as error:
            raise GithubIndeterminateError(
                GithubIndeterminateOperation(
                    effect=effect,
                    detail=(f"gh mutation returned an unusable issue response after invocation; response_length={len(response)}; reread live GitHub state"),
                )
            ) from error

    @staticmethod
    def _parse_milestone_mutation(
        response: str,
        effect: PlannedMilestoneEffect,
    ) -> GithubMilestone:
        try:
            return GithubMilestone.model_validate_json(response)
        except ValidationError as error:
            raise GithubIndeterminateError(
                GithubIndeterminateOperation(
                    effect=effect,
                    detail=(f"gh mutation returned an unusable milestone response after invocation; response_length={len(response)}; reread live GitHub state"),
                )
            ) from error

    def list_milestones(self) -> tuple[GithubMilestone, ...]:
        """List all open and closed milestones through the typed REST boundary."""
        path = f"repos/{self.owner}/{self.repo}/milestones?state=all&per_page=100"
        cmd = ["gh", "api", "--paginate", "--slurp", path]
        proc = self._run_api_command(cmd, path, timeout=120)
        pages = TypeAdapter(tuple[tuple[GithubMilestone, ...], ...]).validate_json(proc.stdout)
        return tuple(milestone for page in pages for milestone in page)

    def create_planned_milestone(
        self,
        title: MilestoneTitle,
        effect: PlannedMilestoneEffect,
    ) -> GithubMilestone:
        """Create the plan's GitHub milestone."""
        path = f"repos/{self.owner}/{self.repo}/milestones"
        response = self._run_mutation_command(
            [
                "gh",
                "api",
                "--include",
                "-X",
                "POST",
                path,
                "-f",
                f"title={title.value}",
            ],
            path,
            effect,
            timeout=30,
        )
        return self._parse_milestone_mutation(response, effect)

    def create_planned_issue(
        self,
        title: str,
        body: str,
        effect: PlannedMilestoneEffect,
    ) -> GithubIssue:
        """Create the plan's milestone ledger issue."""
        path = f"repos/{self.owner}/{self.repo}/issues"
        response = self._run_mutation_command(
            [
                "gh",
                "api",
                "--include",
                "-X",
                "POST",
                path,
                "-f",
                f"title={title}",
                "-f",
                f"body={body}",
            ],
            path,
            effect,
            timeout=30,
        )
        return self._parse_issue_mutation(response, effect)

    def attach_planned_subissue(
        self,
        parent_number: int,
        child_id: int,
        effect: PlannedMilestoneEffect,
    ) -> GithubIssue:
        """Attach a parentless issue for the validated plan."""
        path = f"repos/{self.owner}/{self.repo}/issues/{parent_number}/sub_issues"
        response = self._run_mutation_command(
            [
                "gh",
                "api",
                "--include",
                "-X",
                "POST",
                path,
                "-F",
                f"sub_issue_id={child_id}",
            ],
            path,
            effect,
            timeout=30,
        )
        return self._parse_issue_mutation(response, effect)

    def replace_planned_parent(
        self,
        parent_number: int,
        child_id: int,
        effect: PlannedMilestoneEffect,
    ) -> GithubIssue:
        """Replace a preflighted work unit's existing parent."""
        path = f"repos/{self.owner}/{self.repo}/issues/{parent_number}/sub_issues"
        response = self._run_mutation_command(
            [
                "gh",
                "api",
                "--include",
                "-X",
                "POST",
                path,
                "-F",
                f"sub_issue_id={child_id}",
                "-F",
                "replace_parent=true",
            ],
            path,
            effect,
            timeout=30,
        )
        return self._parse_issue_mutation(response, effect)

    def assign_planned_issue_milestone(
        self,
        issue_number: int,
        milestone: GithubMilestone,
        effect: PlannedMilestoneEffect,
    ) -> GithubIssue:
        """Assign the GitHub milestone and require the returned assignment."""
        path = f"repos/{self.owner}/{self.repo}/issues/{issue_number}"
        response = self._run_mutation_command(
            [
                "gh",
                "api",
                "--include",
                "-X",
                "PATCH",
                path,
                "-F",
                f"milestone={milestone.number}",
            ],
            path,
            effect,
            timeout=30,
        )
        issue = self._parse_issue_mutation(response, effect)
        if issue.milestone is None or issue.milestone.title != milestone.title.value:
            raise GithubIndeterminateError(
                GithubIndeterminateOperation(
                    effect=effect,
                    detail=(
                        "gh mutation returned without the requested milestone assignment after invocation; "
                        f"issue=#{issue.number}; expected={milestone.title.value!r}; "
                        f"found={issue.milestone}; reread live GitHub state"
                    ),
                )
            )
        return issue

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
        return parse_subissues_pages(proc.stdout)

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
        return parse_repo_graph_pages(proc.stdout, self.owner, self.repo)

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
        return parse_issue_parent(proc.stdout, self.owner, self.repo, number)

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
        """Detach a child issue from its parent's sub-issues.

        The removal endpoint is singular ``/sub_issue`` (unlike the plural
        ``/sub_issues`` used to list and add); the plural path 404s. This was
        only caught once the detach/absorb proofs exercised the live boundary.
        """
        self._exec(
            "DELETE",
            f"repos/{self.owner}/{self.repo}/issues/{parent_number}/sub_issue",
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
