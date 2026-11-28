from __future__ import annotations

import base64
import json
import re
import subprocess
import threading
import time
import urllib.parse
from collections.abc import Callable
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    JsonValue,
    PrivateAttr,
    TypeAdapter,
    ValidationError,
)

JsonObject = dict[str, JsonValue]


def _object(value: JsonValue, context: str) -> JsonObject:
    assert isinstance(value, dict), f"{context} must be a JSON object; found={value!r}"
    return value


def _array(value: JsonValue, context: str) -> list[JsonValue]:
    assert isinstance(value, list), f"{context} must be a JSON array; found={value!r}"
    return value


def _string(value: JsonValue, context: str) -> str:
    assert isinstance(value, str), f"{context} must be a string; found={value!r}"
    return value


def _integer(value: JsonValue, context: str) -> int:
    assert isinstance(value, int) and not isinstance(value, bool), (
        f"{context} must be an integer; found={value!r}"
    )
    return value


from .models import (
    ActionsJobMetadata,
    CollaboratorPermission,
    CommentHistory,
    CommentNodeId,
    CommitSha,
    GithubActor,
    GithubBlob,
    GithubCommit,
    GithubContentFile,
    GithubContentItem,
    GithubContentObject,
    GithubIndeterminateOperation,
    GithubIssue,
    GithubIssueFamily,
    GithubMilestone,
    GithubNonFileContent,
    GithubRefMatch,
    GithubRejectedOperation,
    GithubRepository,
    GitRefIdentity,
    GitRefKind,
    GitRefResolution,
    IssueCloseReason,
    IssueComment,
    IssueRef,
    MilestoneTitle,
    MissingGithubContent,
    PlannedMilestoneEffect,
    RemoteOperationFailure,
    RepoRef,
    RoutedOwnerResolution,
    RoutedOwnerState,
    StageHttpApplied,
    StageHttpIndeterminate,
    StageHttpOutcome,
    StageIndeterminateReason,
    StageHttpRejected,
)

REPO_GRAPH_QUERY = """
query($owner: String!, $name: String!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    issues(states: [OPEN, CLOSED], first: 100, after: $endCursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        number
        databaseId
        title
        state
        stateReason
        body
        url
        milestone { title }
        labels(first: 100) { nodes { name } }
        subIssues(first: 100) {
          totalCount
          nodes { number repository { nameWithOwner } }
        }
        parent { number repository { nameWithOwner } }
        blockedBy(first: 100) {
          totalCount
          nodes { number state repository { nameWithOwner } }
        }
      }
    }
  }
}
"""


ISSUE_FAMILY_QUERY = """
query WorkPackageIssueFamily($owner: String!, $name: String!, $number: Int!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      id
      number
      databaseId
      title
      state
      stateReason
      body
      url
      milestone { title }
      labels(first: 100) { nodes { name } }
      parent { number repository { nameWithOwner } }
      comments(first: 100, after: $endCursor) {
        nodes {
          id
          databaseId
          body
          author { login }
          createdAt
          updatedAt
          url
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


ISSUE_PARENT_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      parent { number repository { nameWithOwner } }
    }
  }
}
"""


class GithubApiError(RuntimeError):
    def __init__(self, diagnostic: str, status: int | None) -> None:
        self.status = status
        super().__init__(diagnostic)


class SealHistoryUnavailable(RuntimeError):
    pass


class GitRefLookupError(RuntimeError):
    def __init__(
        self,
        reason: Literal[
            "unknown_ref", "ambiguous_ref", "unsupported_kind", "not_found"
        ],
        requested_ref: str,
        repository_path: str,
    ) -> None:
        self.reason = reason
        self.requested_ref = requested_ref
        self.repository_path = repository_path
        super().__init__(f"{reason}: {requested_ref} ({repository_path})")


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
        proc = subprocess.run(
            cmd, text=True, capture_output=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"gh repo list timed out after {timeout}s for {owner}"
        ) from e
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh repo list failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return issue_bearing_repos(owner, json.loads(proc.stdout))


def issue_bearing_repos(owner: str, payload: list[JsonObject]) -> tuple[RepoRef, ...]:
    """Keep only the payload repos that have at least one open issue.

    Pure filter over the ``gh repo list --json name,issues`` shape. gh returns
    ``issues=null`` when a repo has issues disabled; such a repo has no open
    issues to scan, so skip it rather than crash on subscripting None.
    """
    repos: list[RepoRef] = []
    for record in payload:
        raw_issues = record["issues"]
        if raw_issues is None:
            continue
        issues = _object(raw_issues, f"issues for {owner}")
        if _integer(issues["totalCount"], f"open issue count for {owner}") > 0:
            repos.append(
                RepoRef(
                    owner=owner,
                    repo=_string(record["name"], f"repository name for {owner}"),
                )
            )
    return tuple(repos)


def _graphql_error_text(payload: JsonObject) -> str:
    """Extract the error messages from a GraphQL response document.

    The GraphQL spec requires every entry in ``errors`` to carry ``message``,
    so a missing message is a malformed response and fails loudly.
    """
    errors = payload.get("errors")
    if not errors:
        return "no error details in response"
    messages: list[str] = []
    for raw_error in _array(errors, "GraphQL errors"):
        error = _object(raw_error, "GraphQL error")
        messages.append(_string(error["message"], "GraphQL error message"))
    return "; ".join(messages)


def _graphql_data(document: JsonObject, identity: str) -> JsonObject:
    """Return the ``data`` envelope of a GraphQL response document.

    A successful GraphQL response carries a top-level ``data`` object; an absent
    or null ``data`` is a failed/malformed document (typically an ``errors``-only
    envelope). Assert that envelope invariant once here — dumping the offending
    document — so callers subscript the guaranteed keys of ``data`` directly.
    """
    data = document.get("data")
    assert isinstance(data, dict), (
        f"gh api graphql: no data envelope for {identity}; document was {document!r}"
    )
    return data


def _parse_json_stream(raw: str) -> list[JsonValue]:
    """Decode every JSON value in ``raw``, in order.

    ``gh api --paginate`` writes each page as its own JSON document; the pages
    are newline-separated, not wrapped in one array. Decoding the stream here
    removes the ``--slurp`` version dependency: an older ``gh`` that never
    learned the flag still yields every page.
    """
    decoder = json.JSONDecoder()
    values: list[JsonValue] = []
    index = 0
    while True:
        while index < len(raw) and raw[index].isspace():
            index += 1
        if index >= len(raw):
            return values
        value, index = decoder.raw_decode(raw, index)
        values.append(value)


def _rest_page(value: JsonValue) -> list[JsonObject]:
    if not isinstance(value, list):
        raise TypeError(
            f"gh api --paginate wrote a non-list REST page: {str(value)[:200]!r}"
        )
    return [_object(item, "REST page item") for item in value]


def _rest_pages(raw: str) -> list[list[JsonObject]]:
    """Return the REST pages from streamed ``--paginate`` output.

    Also accepts the older ``--slurp`` shape (one array of page arrays) so
    callers holding captured output are not forced to reshape it.
    """
    values = _parse_json_stream(raw)
    if (
        len(values) == 1
        and isinstance(values[0], list)
        and values[0]
        and isinstance(values[0][0], list)
    ):
        return [_rest_page(page) for page in values[0]]
    return [_rest_page(value) for value in values]


def _graphql_pages(raw: str) -> list[JsonObject]:
    """Return the GraphQL documents from streamed ``--paginate`` output.

    Also accepts the older ``--slurp`` shape (one array of documents).
    """
    values = _parse_json_stream(raw)
    pages: list[JsonValue] = (
        list(values[0]) if len(values) == 1 and isinstance(values[0], list) else values
    )
    documents: list[JsonObject] = []
    for page in pages:
        if not isinstance(page, dict):
            raise TypeError(
                f"gh api graphql wrote a non-object page: {str(page)[:200]!r}"
            )
        documents.append(page)
    return documents


def parse_subissues_pages(raw: str) -> tuple[GithubIssue, ...]:
    """Parse paginated sub-issue pages, newest streamed shape or slurped.

    Pure counterpart to ``list_subissues``: flattens every page in order so
    >100-child parents keep all their children.
    """
    pages = _rest_pages(raw)
    return tuple(GithubIssue.model_validate(item) for page in pages for item in page)


def parse_repo_graph_pages(raw: str, owner: str, repo: str) -> tuple[JsonObject, ...]:
    """Parse paginated GraphQL pages into a flat tuple of issue nodes.

    Pure counterpart to ``fetch_repo_graph``. A ``repository: null`` page means
    the repo is missing or inaccessible; surface the API's own error text.
    """
    pages = _graphql_pages(raw)
    nodes: list[JsonObject] = []
    for page in pages:
        repository = _graphql_data(page, f"{owner}/{repo}")["repository"]
        if repository is None:
            raise RuntimeError(
                f"gh api graphql returned no repository for {owner}/{repo}: {_graphql_error_text(page)}"
            )
        repository_object = _object(repository, f"repository {owner}/{repo}")
        issues = _object(repository_object["issues"], f"issues for {owner}/{repo}")
        nodes.extend(
            _object(node, f"issue graph node for {owner}/{repo}")
            for node in _array(issues["nodes"], f"issue nodes for {owner}/{repo}")
        )
    return tuple(nodes)


def parse_issue_parent(raw: str, owner: str, repo: str, number: int) -> IssueRef | None:
    """Parse the GraphQL ``Issue.parent`` response into the parent's reference or None.

    Pure counterpart to ``get_parent_number``. A null repository or issue means
    the reference could not be resolved and fails loudly.
    """
    payload: JsonObject = json.loads(raw)
    repository = _graphql_data(payload, f"{owner}/{repo}#{number}")["repository"]
    if repository is None:
        raise RuntimeError(
            f"gh api graphql could not resolve {owner}/{repo}#{number}: {_graphql_error_text(payload)}"
        )
    repository_object = _object(repository, f"repository {owner}/{repo}")
    raw_issue = repository_object["issue"]
    if raw_issue is None:
        raise RuntimeError(
            f"gh api graphql could not resolve {owner}/{repo}#{number}: {_graphql_error_text(payload)}"
        )
    issue = _object(raw_issue, f"issue {owner}/{repo}#{number}")
    raw_parent = issue["parent"]
    if raw_parent is None:
        return None
    parent = _object(raw_parent, f"parent of {owner}/{repo}#{number}")
    parent_repository = _object(parent["repository"], "parent repository")
    return IssueRef(
        repo_ref=RepoRef.parse(
            _string(parent_repository["nameWithOwner"], "parent repository slug")
        ),
        number=_integer(parent["number"], "parent issue number"),
    )


class GithubApi(BaseModel):
    """Typed boundary owning all GitHub API communication.

    Construct with a RepoRef to get a typed client. Methods accept
    commands, parse JSON, and construct typed Pydantic models via
    model_validate. No raw data ever escapes.
    """

    model_config = ConfigDict(frozen=True)

    repo_ref: RepoRef
    _ref_matches: dict[GitRefKind, tuple[GithubRefMatch, ...]] = PrivateAttr(
        default_factory=dict
    )
    _commit_objects: dict[str, CommitSha] = PrivateAttr(default_factory=dict)
    _object_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

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
        json_body: JsonObject | None = None,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a GitHub CLI API command. Returns the raw subprocess result.

        ``json_body`` is the request body, sent on stdin through ``--input -``.
        Bodies can exceed the Linux per-argument limit (``MAX_ARG_STRLEN``,
        ~128 KiB), so they must never travel as ``-F body=...`` arguments.
        """
        if fields and json_body is not None:
            raise ValueError(
                "pass fields or json_body, not both: -F arguments and --input - cannot be combined"
            )
        cmd = ["gh", "api", "-X", method]
        if path:
            cmd.append(path)
        for key, value in (fields or {}).items():
            cmd += ["-F", f"{key}={value}"]
        if json_body is not None:
            cmd += ["--input", "-"]

        return self._run_api_command(
            cmd,
            path,
            timeout,
            stdin=None if json_body is None else json.dumps(json_body),
        )

    def _run_api_command(
        self,
        cmd: list[str],
        path: str,
        timeout: int,
        *,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            proc = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
                input=stdin,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"gh api timed out after {timeout}s: {path}") from e

        if proc.returncode != 0:
            error_msg = proc.stderr.strip() or proc.stdout.strip()
            matched_status = re.search(r"HTTP (?P<status>[1-5]\d{2})", error_msg)
            status = (
                int(matched_status["status"]) if matched_status is not None else None
            )
            raise GithubApiError(f"gh api failed: {error_msg}", status)

        return proc

    def _run_mutation_command(
        self,
        command: list[str],
        path: str,
        effect: PlannedMilestoneEffect,
        *,
        timeout: int,
        stdin: str | None = None,
    ) -> str:
        """Run one concrete mutation while preserving rejected versus unknown outcome."""
        try:
            proc = subprocess.run(
                command,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
                input=stdin,
            )
        except subprocess.TimeoutExpired as error:
            raise GithubIndeterminateError(
                GithubIndeterminateOperation(
                    effect=effect,
                    detail=(
                        f"gh mutation timed out after invocation; path={path}; timeout={timeout}s; reread live GitHub state"
                    ),
                )
            ) from error
        except KeyboardInterrupt as error:
            raise GithubIndeterminateError(
                GithubIndeterminateOperation(
                    effect=effect,
                    detail=(
                        f"gh mutation was interrupted after invocation; path={path}; reread live GitHub state"
                    ),
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
                    detail=(
                        f"gh mutation returned an unusable issue response after invocation; response_length={len(response)}; reread live GitHub state"
                    ),
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
                    detail=(
                        f"gh mutation returned an unusable milestone response after invocation; response_length={len(response)}; reread live GitHub state"
                    ),
                )
            ) from error

    def list_milestones(self) -> tuple[GithubMilestone, ...]:
        """List all open and closed milestones through the typed REST boundary."""
        path = f"repos/{self.owner}/{self.repo}/milestones?state=all&per_page=100"
        cmd = ["gh", "api", "--paginate", path]
        proc = self._run_api_command(cmd, path, timeout=120)
        pages = TypeAdapter(tuple[tuple[GithubMilestone, ...], ...]).validate_python(
            _rest_pages(proc.stdout)
        )
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
                "--input",
                "-",
            ],
            path,
            effect,
            timeout=30,
            stdin=json.dumps({"title": title, "body": body}),
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

    def get_repository(self) -> GithubRepository:
        proc = self._exec("GET", f"repos/{self.owner}/{self.repo}")
        return GithubRepository.model_validate_json(proc.stdout)

    def _require_commit(
        self, sha: CommitSha, requested_ref: str, repository_path: str
    ) -> CommitSha:
        cached = self._commit_objects.get(sha)
        if cached is not None:
            return cached
        with self._object_lock:
            cached = self._commit_objects.get(sha)
            if cached is not None:
                return cached
            endpoint = f"repos/{self.owner}/{self.repo}/git/commits/{sha}"
            try:
                proc = self._exec("GET", endpoint)
            except GithubApiError as error:
                if error.status == 404:
                    raise GitRefLookupError(
                        "not_found", requested_ref, repository_path
                    ) from error
                if error.status == 422:
                    raise GitRefLookupError(
                        "unsupported_kind", requested_ref, repository_path
                    ) from error
                raise
            commit = GithubCommit.model_validate_json(proc.stdout)
            self._commit_objects[sha] = commit.sha
            return commit.sha

    def _dereference_annotated_tag(
        self, sha: str, requested_ref: str, repository_path: str
    ) -> CommitSha:
        seen: set[str] = set()
        current = sha
        while True:
            path = f"repos/{self.owner}/{self.repo}/git/tags/{current}"
            proc = self._exec("GET", path)
            payload = _object(json.loads(proc.stdout), "annotated tag")
            target = _object(payload["object"], "annotated tag target")
            target_type = _string(target["type"], "annotated tag target type")
            if target_type == "commit":
                return _string(target["sha"], "annotated tag commit SHA")
            if target_type != "tag" or current in seen:
                raise GitRefLookupError(
                    "unsupported_kind", requested_ref, repository_path
                )
            seen.add(current)
            current = _string(target["sha"], "nested annotated tag SHA")

    def _resolve_ref_target(
        self, match: GithubRefMatch, requested_ref: str, repository_path: str
    ) -> CommitSha:
        if match.target_type == "commit":
            return match.object_sha
        if match.target_type == "tag":
            return self._dereference_annotated_tag(
                match.object_sha, requested_ref, repository_path
            )
        raise GitRefLookupError("unsupported_kind", requested_ref, repository_path)

    def list_git_ref_matches(self, kind: GitRefKind) -> tuple[GithubRefMatch, ...]:
        assert kind in (GitRefKind.branch, GitRefKind.tag), (
            f"ref listing requires branch or tag; found={kind}"
        )
        cached = self._ref_matches.get(kind)
        if cached is not None:
            return cached
        with self._object_lock:
            cached = self._ref_matches.get(kind)
            if cached is not None:
                return cached
            prefix = "heads" if kind == GitRefKind.branch else "tags"
            path = f"repos/{self.owner}/{self.repo}/git/matching-refs/{prefix}/"
            proc = self._run_api_command(
                ["gh", "api", "--paginate", path], path, timeout=30
            )
            matches: list[GithubRefMatch] = []
            for page in _rest_pages(proc.stdout):
                for item in page:
                    name = _string(item["ref"], "Git ref name")
                    target = _object(item["object"], "Git ref target")
                    sha = _string(target["sha"], "Git ref target SHA")
                    target_type = _string(target["type"], "Git ref target type")
                    matches.append(
                        GithubRefMatch(
                            name=name,
                            kind=kind,
                            object_sha=sha,
                            target_type=target_type,
                        )
                    )
            resolved = tuple(matches)
            self._ref_matches[kind] = resolved
            return resolved

    def resolve_default_branch(self) -> GitRefResolution:
        repository = self.get_repository()
        branch = repository.default_branch
        path = f"repos/{self.owner}/{self.repo}/git/ref/heads/{urllib.parse.quote(branch, safe='/')}"
        proc = self._exec("GET", path)
        item = _object(json.loads(proc.stdout), "default branch ref")
        name = _string(item["ref"], "default branch ref name")
        target = _object(item["object"], "default branch target")
        return GitRefResolution(
            requested_ref=branch,
            resolved_commit_sha=_string(target["sha"], "default branch SHA"),
            repository_path="",
            matched_refs=(GitRefIdentity(name=name, kind=GitRefKind.branch),),
        )

    def resolve_git_ref(
        self, requested_ref: str, repository_path: str
    ) -> GitRefResolution:
        if re.fullmatch(r"[0-9a-f]{40}", requested_ref):
            commit_sha = self._require_commit(
                requested_ref, requested_ref, repository_path
            )
            return GitRefResolution(
                requested_ref=requested_ref,
                resolved_commit_sha=commit_sha,
                repository_path=repository_path,
                matched_refs=(
                    GitRefIdentity(name=requested_ref, kind=GitRefKind.commit),
                ),
            )
        matches = (
            *self.list_git_ref_matches(GitRefKind.branch),
            *self.list_git_ref_matches(GitRefKind.tag),
        )
        ref_and_path = (
            f"{requested_ref}/{repository_path}" if repository_path else requested_ref
        )
        candidates: list[tuple[GithubRefMatch, str, str]] = []
        for match in matches:
            prefix = "refs/heads/" if match.kind == GitRefKind.branch else "refs/tags/"
            name = match.name.removeprefix(prefix)
            if ref_and_path == name or ref_and_path.startswith(name + "/"):
                path = ref_and_path[len(name) :].removeprefix("/")
                candidates.append((match, name, path))
        if not candidates:
            raise GitRefLookupError("unknown_ref", requested_ref, repository_path)
        longest = max(len(name) for _, name, _ in candidates)
        selected = [
            (match, name, path)
            for match, name, path in candidates
            if len(name) == longest
        ]
        commits = {
            self._resolve_ref_target(match, requested_ref, repository_path)
            for match, _, _ in selected
        }
        if len(commits) != 1:
            raise GitRefLookupError("ambiguous_ref", requested_ref, repository_path)
        match, name, path = selected[0]
        resolved_commit_sha = self._resolve_ref_target(
            match, requested_ref, repository_path
        )
        identities = tuple(
            GitRefIdentity(name=match.name, kind=match.kind) for match, _, _ in selected
        )
        return GitRefResolution(
            requested_ref=name,
            resolved_commit_sha=resolved_commit_sha,
            repository_path=path,
            matched_refs=identities,
        )

    def get_repository_documents(
        self, paths: tuple[str, ...]
    ) -> tuple[GitRefResolution, dict[str, GithubContentObject | MissingGithubContent]]:
        fields = [
            f"d{index:03d}:object(expression:{json.dumps(f'HEAD:{path}')}){{"
            "__typename ... on Blob{oid text isTruncated} ... on Tree{oid} ... on Commit{oid}}"
            for index, path in enumerate(paths)
        ]
        query = (
            "query WorkPackageGoverningDocuments($owner:String!,$name:String!){"
            "repository(owner:$owner,name:$name){"
            "defaultBranchRef{name target{oid}} " + " ".join(fields) + "}}"
        )
        result = self._run_api_command(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-F",
                f"owner={self.owner}",
                "-F",
                f"name={self.repo}",
            ],
            "graphql",
            timeout=120,
        )
        documents = _graphql_pages(result.stdout)
        assert len(documents) == 1
        page = documents[0]
        data = _graphql_data(page, f"governing documents for {self.repo_ref.slug}")
        if page.get("errors"):
            raise RuntimeError(_graphql_error_text(page))
        repository = _object(data["repository"], "governing document repository")
        branch = _object(repository["defaultBranchRef"], "default branch reference")
        target = _object(branch["target"], "default branch target")
        name = _string(branch["name"], "default branch name")
        commit_sha = _string(target["oid"], "default branch commit SHA")
        resolution = GitRefResolution(
            requested_ref=name,
            resolved_commit_sha=commit_sha,
            repository_path="",
            matched_refs=(
                GitRefIdentity(name=f"refs/heads/{name}", kind=GitRefKind.branch),
            ),
        )
        documents_by_path: dict[str, GithubContentObject | MissingGithubContent] = {}
        for index, path in enumerate(paths):
            node = repository[f"d{index:03d}"]
            if node is None:
                documents_by_path[path] = MissingGithubContent(
                    path=path, ref=commit_sha
                )
                continue
            try:
                item = _object(node, f"governing document {path}")
                typename = _string(item["__typename"], "document object type")
                sha = _string(item["oid"], "document object SHA")
                if typename != "Blob":
                    documents_by_path[path] = GithubNonFileContent(
                        type=typename.lower(), path=path, sha=sha
                    )
                    continue
                truncated = item["isTruncated"]
                assert isinstance(truncated, bool)
                text = item["text"]
                assert text is None or isinstance(text, str)
                content = (
                    None
                    if truncated or text is None
                    else base64.b64encode(text.encode("utf-8")).decode("ascii")
                )
                documents_by_path[path] = GithubContentFile(
                    type="file",
                    path=path,
                    encoding="none" if truncated else "base64",
                    content=content,
                    sha=sha,
                )
            except (KeyError, TypeError, ValueError, AssertionError) as error:
                raise RuntimeError(
                    f"invalid governing document response for {self.repo_ref.slug}:{path}: {error}"
                ) from error
        return resolution, documents_by_path

    def get_repository_content(
        self, path: str, ref: str
    ) -> GithubContentObject | tuple[GithubContentItem, ...] | MissingGithubContent:
        endpoint = f"repos/{self.owner}/{self.repo}/contents/{urllib.parse.quote(path, safe='/')}"
        try:
            proc = self._exec("GET", endpoint, fields={"ref": ref})
        except GithubApiError as error:
            if error.status == 404:
                return MissingGithubContent(path=path, ref=ref)
            raise
        payload = json.loads(proc.stdout)
        if isinstance(payload, list):
            return TypeAdapter(tuple[GithubContentItem, ...]).validate_python(payload)
        content_type = _string(payload["type"], "repository content type")
        if content_type == "file":
            return GithubContentFile.model_validate(payload)
        return GithubNonFileContent.model_validate(payload)

    def get_git_blob(self, sha: CommitSha) -> bytes:
        endpoint = f"repos/{self.owner}/{self.repo}/git/blobs/{sha}"
        proc = self._exec("GET", endpoint)
        blob = GithubBlob.model_validate_json(proc.stdout)
        return base64.b64decode(blob.content)

    def get_comment(self, comment_id: int) -> IssueComment:
        proc = self._exec(
            "GET", f"repos/{self.owner}/{self.repo}/issues/comments/{comment_id}"
        )
        return IssueComment.model_validate_json(proc.stdout)

    def get_issue_family(self, number: int) -> GithubIssueFamily:
        issue: GithubIssue | None = None
        parent: IssueRef | None = None
        comments: list[IssueComment] = []
        cursor: str | None = None
        while True:
            command = [
                "gh",
                "api",
                "graphql",
                "-f",
                f"query={ISSUE_FAMILY_QUERY}",
                "-F",
                f"owner={self.owner}",
                "-F",
                f"name={self.repo}",
                "-F",
                f"number={number}",
            ]
            if cursor is not None:
                command.extend(["-F", f"endCursor={cursor}"])
            result = self._run_api_command(command, "graphql", timeout=120)
            pages = _graphql_pages(result.stdout)
            if len(pages) != 1:
                raise RuntimeError(
                    f"expected one GraphQL page for {self.repo_ref.slug}#{number}; "
                    f"found={len(pages)}"
                )
            page = pages[0]
            data = _graphql_data(page, f"{self.repo_ref.slug}#{number}")
            if page.get("errors"):
                raise RuntimeError(_graphql_error_text(page))
            repository = _object(data["repository"], "issue family repository")
            raw_issue = repository["issue"]
            if raw_issue is None:
                raise RuntimeError(
                    f"no issue family exists for {self.repo_ref.slug}#{number}"
                )
            issue_node = _object(raw_issue, "issue family issue")
            try:
                candidate_issue = GithubIssue.from_graphql(issue_node)
                candidate_parent = parse_issue_parent(
                    json.dumps(page), self.owner, self.repo, number
                )
            except (KeyError, TypeError, ValueError, AssertionError) as error:
                raise RuntimeError(
                    f"invalid issue or parent in {self.repo_ref.slug}#{number}: {error}"
                ) from error
            if issue is None:
                issue = candidate_issue
                parent = candidate_parent
            connection = _object(issue_node["comments"], "issue family comments")
            for raw_comment in _array(
                connection["nodes"], "issue family comment nodes"
            ):
                node = _object(raw_comment, "issue family comment")
                author = _object(node["author"], "issue family comment author")
                comments.append(
                    IssueComment.model_validate(
                        {
                            "id": _integer(node["databaseId"], "comment database ID"),
                            "node_id": _string(node["id"], "comment node ID"),
                            "body": _string(node["body"], "comment body"),
                            "user": {
                                "login": _string(author["login"], "comment author")
                            },
                            "created_at": _string(
                                node["createdAt"], "comment creation time"
                            ),
                            "updated_at": _string(
                                node["updatedAt"], "comment update time"
                            ),
                            "html_url": _string(node["url"], "comment URL"),
                        }
                    )
                )
            page_info = _object(connection["pageInfo"], "issue family page info")
            has_next = page_info["hasNextPage"]
            if not isinstance(has_next, bool):
                raise RuntimeError(
                    f"invalid hasNextPage for {self.repo_ref.slug}#{number}: {has_next!r}"
                )
            next_cursor = page_info["endCursor"]
            if not has_next:
                break
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor == cursor
            ):
                raise RuntimeError(
                    f"invalid comment-page cursor for {self.repo_ref.slug}#{number}: {next_cursor!r}"
                )
            cursor = next_cursor
        if issue is None:
            raise RuntimeError(f"no issue family pages were returned for {number}")
        return GithubIssueFamily(issue=issue, comments=tuple(comments), parent=parent)

    def get_issue(self, number: int) -> GithubIssue:
        """Fetch a single GitHub issue by number."""
        proc = self._exec("GET", f"repos/{self.owner}/{self.repo}/issues/{number}")
        return GithubIssue.model_validate_json(proc.stdout)

    def list_subissues(self, number: int) -> tuple[GithubIssue, ...]:
        """List ALL sub-issues of a parent issue, following REST pagination."""
        path = f"repos/{self.owner}/{self.repo}/issues/{number}/sub_issues?per_page=100"
        cmd = ["gh", "api", "--paginate", path]
        proc = self._run_api_command(cmd, path, timeout=120)
        return parse_subissues_pages(proc.stdout)

    def list_comments(self, number: int) -> tuple[IssueComment, ...]:
        path = f"repos/{self.owner}/{self.repo}/issues/{number}/comments?per_page=100"
        proc = self._run_api_command(
            ["gh", "api", "--paginate", path], path, timeout=120
        )
        pages = TypeAdapter(tuple[tuple[IssueComment, ...], ...]).validate_python(
            _rest_pages(proc.stdout)
        )
        return tuple(comment for page in pages for comment in page)

    def list_blocked_by(self, number: int) -> tuple[GithubIssue, ...]:
        """List ALL native blockers of an issue, following REST pagination."""
        path = f"repos/{self.owner}/{self.repo}/issues/{number}/dependencies/blocked_by?per_page=100"
        cmd = ["gh", "api", "--paginate", path]
        proc = self._run_api_command(cmd, path, timeout=120)
        return parse_subissues_pages(proc.stdout)

    def list_tree_files(self) -> frozenset[str]:
        """Paths of every file in the default branch's tree.

        A repository with no commits has no branches and an empty tree; GitHub
        answers a tree read there with HTTP 409, so the branch list decides it.
        """
        if not json.loads(
            self._exec(
                "GET", f"repos/{self.owner}/{self.repo}/branches?per_page=1"
            ).stdout
        ):
            return frozenset()
        return self._tree_files("HEAD", "")

    def _tree_files(self, tree_ish: str, prefix: str) -> frozenset[str]:
        """Files under one tree, each path prefixed with that tree's location.

        GitHub truncates a recursive tree read past its size limit and documents
        the remedy: read the tree non-recursively and fetch one subtree at a
        time (https://docs.github.com/en/rest/git/trees#get-a-tree). Each
        subtree is read recursively first, so a walk descends only where a read
        is still truncated.
        """
        tree_path = f"repos/{self.owner}/{self.repo}/git/trees/{tree_ish}"
        recursive = json.loads(
            self._exec("GET", f"{tree_path}?recursive=1", timeout=120).stdout
        )
        if not recursive["truncated"]:
            return frozenset(
                prefix + e["path"] for e in recursive["tree"] if e["type"] == "blob"
            )
        entries = json.loads(self._exec("GET", tree_path, timeout=120).stdout)["tree"]
        files = {prefix + e["path"] for e in entries if e["type"] == "blob"}
        for e in entries:
            if e["type"] == "tree":
                files |= self._tree_files(e["sha"], f"{prefix}{e['path']}/")
        return frozenset(files)

    def fetch_repo_graph(self) -> tuple[JsonObject, ...]:
        """Fetch the full issue DAG in one paginated GraphQL query.

        Returns the raw issue nodes (open and closed) with sub-issue and
        blocked-by edges. ``--paginate`` streams one JSON document per page.
        """
        cmd = [
            "gh",
            "api",
            "graphql",
            "--paginate",
            "-f",
            f"query={REPO_GRAPH_QUERY}",
            "-F",
            f"owner={self.owner}",
            "-F",
            f"name={self.repo}",
        ]
        proc = self._run_api_command(cmd, "graphql", timeout=120)
        nodes = parse_repo_graph_pages(proc.stdout, self.owner, self.repo)
        for node in nodes:
            blocked_by = _object(node["blockedBy"], "blocked-by relationship")
            total_count = _integer(blocked_by["totalCount"], "blocked-by totalCount")
            blocker_nodes = _array(blocked_by["nodes"], "blocked-by nodes")
            if total_count > len(blocker_nodes):
                issue_number = _integer(node["number"], "issue graph number")
                blockers = self.list_blocked_by(issue_number)
                assert len(blockers) == total_count, (
                    f"incomplete blocked-by fetch for {self.owner}/{self.repo}#{issue_number}: expected {total_count}, fetched {len(blockers)}"
                )
                blocked_by["nodes"] = [
                    {
                        "number": blocker.number,
                        "state": blocker.state.value.upper(),
                        "repository": {"nameWithOwner": blocker.repository_slug},
                    }
                    for blocker in blockers
                ]
        return nodes

    def get_parent(self, number: int) -> IssueRef | None:
        """Return the issue's current parent, in any repository of the owner, via GraphQL Issue.parent."""
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

    def get_parent_number(self, number: int) -> int | None:
        """Return the number of the issue's parent in this repository, or None; a parent elsewhere raises."""
        parent = self.get_parent(number)
        if parent is None:
            return None
        if parent.repo_ref.slug.casefold() != self.repo_ref.slug.casefold():
            raise RuntimeError(
                f"{self.repo_ref.slug}#{number} has its parent in another repository: {parent.slug}"
            )
        return parent.number

    def create_issue(self, title: str, body: str = "") -> GithubIssue:
        """Create a new issue in the repository."""
        proc = self._exec(
            "POST",
            f"repos/{self.owner}/{self.repo}/issues",
            json_body={"title": title, "body": body},
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

    def add_comment(self, number: int, body: str) -> IssueComment:
        """Post a comment on an issue."""
        proc = self._exec(
            "POST",
            f"repos/{self.owner}/{self.repo}/issues/{number}/comments",
            json_body={"body": body},
        )
        return IssueComment.model_validate_json(proc.stdout)

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
            json_body={"body": body},
        )
        return GithubIssue.model_validate(json.loads(proc.stdout))

    def get_current_actor(self) -> GithubActor:
        proc = self._exec("GET", "user")
        return GithubActor.model_validate_json(proc.stdout)

    def get_collaborator_permission(self, login: str) -> CollaboratorPermission:
        assert login, login
        path = f"repos/{self.owner}/{self.repo}/collaborators/{login}/permission"
        proc = self._exec("GET", path)
        return CollaboratorPermission.model_validate_json(proc.stdout)

    def get_actions_job(self, job_id: int) -> ActionsJobMetadata:
        assert job_id > 0, job_id
        path = f"repos/{self.owner}/{self.repo}/actions/jobs/{job_id}"
        proc = self._exec("GET", path)
        return ActionsJobMetadata.model_validate_json(proc.stdout)

    def get_actions_run_job(self, run_id: int, job_node_id: str) -> ActionsJobMetadata:
        assert run_id > 0 and job_node_id, (run_id, job_node_id)
        path = f"repos/{self.owner}/{self.repo}/actions/runs/{run_id}/jobs?per_page=100"
        proc = self._exec("GET", path)
        payload = _object(json.loads(proc.stdout), "Actions run jobs")
        jobs = TypeAdapter(tuple[ActionsJobMetadata, ...]).validate_python(
            payload["jobs"]
        )
        return next(job for job in jobs if job.node_id == job_node_id)

    def get_comment_history(self, comment_node_id: CommentNodeId) -> CommentHistory:
        query = (
            "query StageCommentMetadata($id: ID!) {"
            "node(id: $id) { ... on IssueComment {"
            "id createdAt updatedAt lastEditedAt"
            "} }"
        )
        command = [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"id={comment_node_id}",
        ]
        result = self._run_api_command(command, "graphql", timeout=120)
        documents = _graphql_pages(result.stdout)
        if len(documents) != 1:
            raise SealHistoryUnavailable(str(comment_node_id))
        document = documents[0]
        data = _graphql_data(document, str(comment_node_id))
        raw_node = data["node"]
        if raw_node is None:
            raise SealHistoryUnavailable(str(comment_node_id))
        node = _object(raw_node, "comment history node")
        raw_last_edited = node["lastEditedAt"]
        assert raw_last_edited is None or isinstance(raw_last_edited, str)
        return CommentHistory(
            created_at=_string(node["createdAt"], "comment createdAt"),
            updated_at=_string(node["updatedAt"], "comment updatedAt"),
            last_edited_at=raw_last_edited,
        )

    @staticmethod
    def _stage_outcome(response: str, exit_code: int, stderr: str) -> StageHttpOutcome:
        headers, separator, body = response.partition("\n\n")
        status_line = headers.splitlines()[0] if headers else ""
        matched = re.fullmatch(r"HTTP/\S+ (?P<status>[1-5]\d{2})(?: .*)?", status_line)
        if not separator or matched is None:
            reason: StageIndeterminateReason = (
                "nonzero_without_rejection"
                if exit_code != 0
                else "no_usable_http_response"
            )
            return StageHttpIndeterminate(
                reason=reason,
                diagnostic=f"exit={exit_code}; stderr={stderr.strip()!r}; response={response[:200]!r}",
            )
        status = int(matched["status"])
        if status >= 400:
            return StageHttpRejected(
                status=status,
                diagnostic=f"HTTP {status}; exit={exit_code}; response={body[:1000]!r}",
            )
        if exit_code != 0 or status < 200 or status >= 300:
            return StageHttpIndeterminate(
                reason="nonzero_without_rejection",
                diagnostic=f"HTTP {status}; exit={exit_code}; stderr={stderr.strip()!r}",
            )
        return StageHttpApplied(response_body=body)

    def _run_stage_mutation(
        self, method: str, path: str, payload: JsonObject
    ) -> StageHttpOutcome:
        command = ["gh", "api", "--include", "-X", method, path, "--input", "-"]
        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
                input=json.dumps(payload),
            )
        except subprocess.TimeoutExpired:
            return StageHttpIndeterminate(
                reason="timeout_after_invocation",
                diagnostic=f"{method} {path} timed out after invocation",
            )
        except KeyboardInterrupt:
            return StageHttpIndeterminate(
                reason="interrupted_after_invocation",
                diagnostic=f"{method} {path} was interrupted after invocation",
            )
        return self._stage_outcome(result.stdout, result.returncode, result.stderr)

    def post_stage_comment(self, number: int, body: str) -> StageHttpOutcome:
        return self._run_stage_mutation(
            "POST",
            f"repos/{self.owner}/{self.repo}/issues/{number}/comments",
            {"body": body},
        )

    def patch_stage_body(self, number: int, body: str) -> StageHttpOutcome:
        return self._run_stage_mutation(
            "PATCH", f"repos/{self.owner}/{self.repo}/issues/{number}", {"body": body}
        )

    def dispatch_stage_drain(self, issue_node_id: str, issue_number: int) -> None:
        assert issue_node_id and issue_number > 0, (issue_node_id, issue_number)
        self._run_api_command(
            [
                "gh",
                "workflow",
                "run",
                "itree-issue-writer.yml",
                "-f",
                f"issue_node_id={issue_node_id}",
                "-f",
                f"issue_number={issue_number}",
            ],
            "itree-issue-writer.yml",
            timeout=30,
        )


def settle_parent(
    *,
    target: IssueRef,
    read_parent: Callable[[], IssueRef | None],
    replace_parent: Callable[[IssueRef | None], None],
    settle_seconds: float = 6.0,
    max_attempts: int = 5,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Replace the child's parent and verify the result after GitHub settles.

    GitHub re-applies a pre-transfer parent asynchronously, so one successful
    write is not evidence of the final state. ``replace_parent`` receives the
    parent just read. Apply the replacement whenever the read disagrees, then
    require the target parent on two reads ``settle_seconds`` apart; raise
    rather than report a state that may revert.
    """
    last: IssueRef | None = None
    for _ in range(max_attempts):
        current = read_parent()
        if not _same_issue(current, target):
            replace_parent(current)
        sleep(settle_seconds)
        first = read_parent()
        if not _same_issue(first, target):
            last = first
            continue
        sleep(settle_seconds)
        second = read_parent()
        if _same_issue(second, target):
            return
        last = second
    seen = "no parent" if last is None else last.slug
    raise RuntimeError(
        f"could not settle the child's parent on {target.slug} after {max_attempts} attempts (last read: {seen}); reread live GitHub state"
    )


def _same_issue(candidate: IssueRef | None, target: IssueRef) -> bool:
    """Compare refs ignoring GitHub's canonical case for OWNER/REPO."""
    return (
        candidate is not None
        and candidate.number == target.number
        and candidate.repo_ref.slug.casefold() == target.repo_ref.slug.casefold()
    )


def resolve_routed_owner(ref: IssueRef) -> RoutedOwnerResolution:
    """Read one routed owner issue and its local sub-tree.

    Doctor calls this once per distinct ``I011`` owner. The owner's repository
    graph supplies both the owner's state and every descendant, so a closed
    owner and an owner with no open work unit are distinguished without a
    second read.
    """
    from .traversal import dag_from_graph_nodes
    from .validate import is_grouping_issue

    try:
        api = GithubApi.from_issue_ref(ref)
        nodes = api.fetch_repo_graph()
        owner_dag = dag_from_graph_nodes(ref.to_repo_ref(), nodes, api.list_subissues)
    except (AssertionError, KeyError, RuntimeError, ValueError) as e:
        return RoutedOwnerResolution(state=RoutedOwnerState.unreadable, detail=str(e))

    owner = owner_dag.issues.get(ref.number)
    if owner is None:
        return RoutedOwnerResolution(
            state=RoutedOwnerState.unreadable,
            detail=f"{ref.slug} is absent from its repository graph",
        )
    if not owner.is_open:
        return RoutedOwnerResolution(state=RoutedOwnerState.closed)

    stack = [ref.number]
    seen: set[int] = set()
    while stack:
        number = stack.pop()
        if number in seen:
            continue
        seen.add(number)
        issue = owner_dag.issues[number]
        if issue.is_open and not is_grouping_issue(issue.title):
            return RoutedOwnerResolution(state=RoutedOwnerState.open_with_work)
        stack.extend(owner_dag.children_of.get(number, ()))
    return RoutedOwnerResolution(state=RoutedOwnerState.no_open_work)
