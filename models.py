from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Self, Literal

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

    repo_ref: RepoRef
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
        return cls(repo_ref=RepoRef(owner=m["owner"], repo=m["repo"]), number=int(m["number"]))

    @property
    def slug(self) -> str:
        """Return the issue reference as OWNER/REPO#NUMBER string."""
        return f"{self.repo_ref.slug}#{self.number}"

    @property
    def owner(self) -> str:
        return self.repo_ref.owner

    @property
    def repo(self) -> str:
        return self.repo_ref.repo

    def same_repo(self, other: IssueRef) -> bool:
        """Check if this issue is in the same repository as another issue reference."""
        return self.repo_ref == other.repo_ref

    def to_repo_ref(self) -> RepoRef:
        """Extract the repository reference from this issue reference."""
        return self.repo_ref


class Milestone(BaseModel):
    title: str


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
    milestone: Milestone | None = None
    pull_request: dict | None = None

    @property
    def is_open(self) -> bool:
        """Check if the issue is in the open state."""
        return self.state == IssueState.open


class TreeNode(BaseModel):
    """Materialized rooted ordered tree node."""

    model_config = ConfigDict(frozen=True)

    issue: GithubIssue
    children: tuple[TreeNode, ...] = ()

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


class RepoDag(BaseModel):
    """Full directed acyclic graph of a repository's issue tree.

    Constructed by scanning every issue and its sub-issues.  This is
    the single source of truth that all query commands operate on.
    """

    model_config = ConfigDict(frozen=True)

    repo_ref: RepoRef

    # Every issue in the repo, keyed by issue number.
    issues: dict[int, GithubIssue]

    # Adjacency list: parent_number -> ordered tuple of child numbers.
    children_of: dict[int, tuple[int, ...]]

    # Pre-computed inverse: child_number -> parent_number.
    parent_of: dict[int, int] = Field(default_factory=dict, repr=False)

    # Dependency relationships: child_number -> tuple of blocker issue numbers.
    dependencies: dict[int, tuple[int, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _compute_derived(self) -> Self:
        """Build derived views once at construction time."""
        parent_of: dict[int, int] = {}
        for parent, kids in self.children_of.items():
            for kid in kids:
                parent_of[kid] = parent
        object.__setattr__(self, "parent_of", parent_of)
        return self

    @property
    def slug(self) -> str:
        return self.repo_ref.slug

    @property
    def roots(self) -> tuple[GithubIssue, ...]:
        """Issues that are not sub-issues of any other issue."""
        parents = self.parent_of
        return tuple(
            issue
            for num, issue in sorted(self.issues.items())
            if num not in parents
        )

    @property
    def orphans(self) -> tuple[GithubIssue, ...]:
        """Issues not reachable from the primary root.

        Walks the adjacency list directly — no TreeNode materialization needed.
        """
        candidates = []
        for number, issue in sorted(self.issues.items()):
            if issue.body and "<!-- itree:role=root-ledger schema=1 -->" in issue.body:
                candidates.append(number)
        
        if not candidates:
            import os
            config_path = os.path.join(".github", "itree.toml")
            if os.path.exists(config_path):
                try:
                    import tomllib
                    with open(config_path, "rb") as f:
                        config = tomllib.load(f)
                        if "root" in config:
                            val = config["root"]
                            num_str = val.split("#")[-1] if (isinstance(val, str) and "#" in val) else str(val)
                            num = int(num_str)
                            if num in self.issues:
                                candidates.append(num)
                except Exception:
                    pass

        if not candidates:
            roots = self.roots
            if not roots:
                return ()
            primary_root_num = roots[0].number
        else:
            primary_root_num = candidates[0]

        reachable: set[int] = set()
        self._collect_reachable(primary_root_num, reachable)
        return tuple(
            issue
            for num, issue in sorted(self.issues.items())
            if num not in reachable
        )

    def _collect_reachable(self, number: int, out: set[int]) -> None:
        """DFS from ``number`` marking all reachable nodes via the adjacency list."""
        if number in out:
            return
        out.add(number)
        for kid in self.children_of.get(number, ()):
            self._collect_reachable(kid, out)

    def materialize_root(self, root_number: int) -> "TreeNode":
        """Build a TreeNode tree from the DAG rooted at the given issue."""
        issue = self.issues[root_number]
        kids = self.children_of.get(root_number, ())
        children = tuple(self.materialize_root(k) for k in kids)
        return TreeNode(issue=issue, children=children)


class Finding(BaseModel):
    code: str
    severity: Literal["error", "warning", "info"]
    title: str
    evidence: list[str]
    meaning: str
    agent_instruction: str | None = None
    remediation: list[str]
    suggested_commands: list[str] = []


class DoctorReport(BaseModel):
    repo: str
    status: Literal["ok", "warning", "error"]
    root: IssueRef | None
    next_issue: IssueRef | None
    enclosing_work_unit: IssueRef | None
    metrics: dict[str, int]
    findings: list[Finding]



