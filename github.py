from __future__ import annotations

import json
import subprocess

from pydantic import BaseModel, ConfigDict

from .models import GithubIssue, IssueCloseReason, IssueRef, RepoRef


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
        paginate: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a GitHub CLI API command. Returns the raw subprocess result."""
        cmd = ["gh", "api", "-X", method]
        if paginate:
            cmd.append("--paginate")
        if path:
            cmd.append(path)
        for key, value in (fields or {}).items():
            cmd += ["-F", f"{key}={value}"]

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
        """List all sub-issues of a parent issue."""
        proc = self._exec("GET", f"repos/{self.owner}/{self.repo}/issues/{number}/sub_issues")
        return tuple(GithubIssue.model_validate(item) for item in json.loads(proc.stdout))

    def list_all_issues(self) -> tuple[GithubIssue, ...]:
        """Fetch every issue in the repository (paginated).

        Uses gh api's built-in pagination support: ``--paginate`` streams
        all pages into a single JSON array on stdout.
        """
        proc = self._exec("GET", f"repos/{self.owner}/{self.repo}/issues", timeout=120, paginate=True)
        items: list[dict] = json.loads(proc.stdout)
        return tuple(GithubIssue.model_validate(item) for item in items)


    def create_issue(self, title: str, body: str = "") -> GithubIssue:
        """Create a new issue in the repository."""
        proc = self._exec(
            "POST",
            f"repos/{self.owner}/{self.repo}/issues",
            fields={"title": title, "body": body},
        )
        return GithubIssue.model_validate(json.loads(proc.stdout))

    def add_subissue(self, parent_number: int, child_id: int, *, replace_parent: bool = False) -> GithubIssue:
        """Attach a child issue as a sub-issue of a parent issue."""
        proc = self._exec(
            "POST",
            f"repos/{self.owner}/{self.repo}/issues/{parent_number}/sub_issues",
            fields={
                "sub_issue_id": str(child_id),
                "replace_parent": str(replace_parent).lower(),
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

    def close_issue(
        self,
        number: int,
        *,
        comment: str | None = None,
        reason: IssueCloseReason = IssueCloseReason.completed,
    ) -> GithubIssue:
        """Close a GitHub issue with an optional comment and reason."""
        if comment:
            self._exec(
                "POST",
                f"repos/{self.owner}/{self.repo}/issues/{number}/comments",
                fields={"body": comment},
            )
        proc = self._exec(
            "PATCH",
            f"repos/{self.owner}/{self.repo}/issues/{number}",
            fields={"state": "closed", "state_reason": reason.value},
        )
        return GithubIssue.model_validate(json.loads(proc.stdout))
