from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

IssueNumber = Annotated[int, Field(gt=0)]
GithubIssueId = Annotated[int, Field(gt=0)]


class IssueState(StrEnum):
    open = "open"
    closed = "closed"


class IssueCloseReason(StrEnum):
    """Valid reasons for closing a GitHub issue."""

    completed = "completed"
    not_planned = "not_planned"
    reopened = "reopened"


class RepoRef(BaseModel):
    """User-facing repository reference: OWNER/REPO."""

    model_config = ConfigDict(frozen=True)

    owner: str = Field(..., pattern=r"^[^/\s#]+$")
    repo: str = Field(..., pattern=r"^[^/\s#]+$")

    @classmethod
    def parse(cls, raw: str) -> RepoRef:
        """Parse a repository reference string into a RepoRef.

        Args:
            raw: Repository reference string in format OWNER/REPO.

        Returns:
            RepoRef instance.

        Raises:
            ValueError: If the format is invalid.
        """
        m = re.fullmatch(r"(?P<owner>[^/\s#]+)/(?P<repo>[^/\s#]+)", raw)
        if not m:
            raise ValueError(f"expected OWNER/REPO, got {raw!r}")
        return cls(owner=m["owner"], repo=m["repo"])

    @property
    def slug(self) -> str:
        """Return the repository reference as OWNER/REPO string."""
        return f"{self.owner}/{self.repo}"


class IssueRef(BaseModel):
    """User-facing issue reference: OWNER/REPO#123."""

    model_config = ConfigDict(frozen=True)

    owner: str = Field(..., pattern=r"^[^/\s#]+$")
    repo: str = Field(..., pattern=r"^[^/\s#]+$")
    number: IssueNumber

    @classmethod
    def parse(cls, raw: str) -> Self:
        """Parse an issue reference string into an IssueRef.

        Args:
            raw: Issue reference string in format OWNER/REPO#NUMBER.

        Returns:
            IssueRef instance.

        Raises:
            ValueError: If the format is invalid.
        """
        m = re.fullmatch(r"(?P<owner>[^/\s#]+)/(?P<repo>[^/\s#]+)#(?P<number>[1-9]\d*)", raw)
        if not m:
            raise ValueError(f"expected OWNER/REPO#NUMBER, got {raw!r}")
        return cls(owner=m["owner"], repo=m["repo"], number=int(m["number"]))

    @property
    def slug(self) -> str:
        """Return the issue reference as OWNER/REPO#NUMBER string."""
        return f"{self.owner}/{self.repo}#{self.number}"

    def same_repo(self, other: IssueRef) -> bool:
        """Check if this issue is in the same repository as another issue reference."""
        return (self.owner, self.repo) == (other.owner, other.repo)

    def to_repo_ref(self) -> RepoRef:
        """Extract the repository reference from this issue reference."""
        return RepoRef(owner=self.owner, repo=self.repo)


class GithubIssue(BaseModel):
    """Subset of GitHub issue JSON needed by the traversal layer."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: GithubIssueId
    number: IssueNumber
    title: str
    state: IssueState
    html_url: str
    body: str | None = None
    state_reason: str | None = None

    @property
    def is_open(self) -> bool:
        """Check if the issue is in the open state."""
        return self.state == IssueState.open


class TreeNode(BaseModel):
    """Materialized rooted ordered tree node."""

    model_config = ConfigDict(frozen=True)

    issue: GithubIssue
    children: tuple[TreeNode, ...] = ()

    @property
    def is_leaf(self) -> bool:
        """Check if this node has no open children (is a leaf in the traversal tree)."""
        return len(self.open_children) == 0

    @property
    def open_children(self) -> tuple[TreeNode, ...]:
        """Return only the open child nodes."""
        return tuple(c for c in self.children if c.issue.is_open)

    def preorder(self) -> tuple[TreeNode, ...]:
        """Return all nodes in the tree in preorder traversal order."""
        out: list[TreeNode] = [self]
        for child in self.children:
            out.extend(child.preorder())
        return tuple(out)

    def descendants(self) -> tuple[TreeNode, ...]:
        """Return all descendants in preorder traversal, excluding self.

        Uses short-circuiting recursion to collect children without
        allocating and slicing a full preorder tuple.
        """
        out: list[TreeNode] = []
        for child in self.children:
            out.append(child)
            out.extend(child.descendants())
        return tuple(out)

    def first_open_leaf(self) -> TreeNode | None:
        """Find the first open leaf node in preorder traversal.

        Returns:
            The first open leaf TreeNode, or None if this node is closed or has no open descendants.
        """
        if not self.issue.is_open:
            return None
        for child in self.children:
            found = child.first_open_leaf()
            if found is not None:
                return found
        return self

    def path_to(self, issue_number: int) -> tuple[TreeNode, ...] | None:
        """Find the path from this node to the node with the given issue number.

        Args:
            issue_number: The GitHub issue number to find.

        Returns:
            Tuple of TreeNodes from this node to the target, or None if not found.
        """
        if self.issue.number == issue_number:
            return (self,)
        for child in self.children:
            path = child.path_to(issue_number)
            if path is not None:
                return (self,) + path
        return None


class AttachRequest(BaseModel):
    """Request to attach child as a GitHub sub-issue of parent."""

    parent: IssueRef
    child: IssueRef
    replace_parent: bool = False

    @model_validator(mode="after")
    def same_repository(self) -> Self:
        """Validate that parent and child are in the same repository and are different issues."""
        if not self.parent.same_repo(self.child):
            raise ValueError("GitHub sub-issues must stay in the same repository")
        if self.parent.number == self.child.number:
            raise ValueError("an issue cannot be attached under itself")
        return self


class MoveRequest(BaseModel):
    """Request to place child under parent and optionally order among siblings."""

    child: IssueRef
    parent: IssueRef
    before: IssueRef | None = None
    after: IssueRef | None = None

    @model_validator(mode="after")
    def coherent_position(self) -> Self:
        """Validate move request constraints: before/after mutual exclusivity, same repo, not self."""
        if self.before is not None and self.after is not None:
            raise ValueError("use either --before or --after, not both")
        refs = [self.child, self.parent, *(r for r in [self.before, self.after] if r is not None)]
        if any(not self.parent.same_repo(r) for r in refs):
            raise ValueError("move arguments must be in the same repository")
        if self.child.number == self.parent.number:
            raise ValueError("cannot move an issue under itself")
        return self


class DetachRequest(BaseModel):
    """Request to detach a child issue from its parent."""

    parent: IssueRef
    child: IssueRef

    @model_validator(mode="after")
    def same_repository(self) -> Self:
        """Validate that parent and child are in the same repository."""
        if not self.parent.same_repo(self.child):
            raise ValueError("detach arguments must be in the same repository")
        return self
