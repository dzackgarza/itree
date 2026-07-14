from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator

IssueNumber = Annotated[int, Field(gt=0)]
GithubIssueId = Annotated[int, Field(gt=0)]
GithubMilestoneNumber = Annotated[int, Field(gt=0)]
FindingSeverity = Literal["error", "warning", "question", "info"]
ReportStatus = Literal["ok", "warning", "error"]
MissingReportRefReason = Literal["no_root_ledger", "no_open_work_unit"]


class IssueState(StrEnum):
    open = "open"
    closed = "closed"


class IssueCloseReason(StrEnum):
    """Valid reasons for closing a GitHub issue."""

    completed = "completed"
    not_planned = "not_planned"
    duplicate = "duplicate"
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
    model_config = ConfigDict(frozen=True)
    title: str


class GithubMilestoneState(StrEnum):
    open = "open"
    closed = "closed"


class MilestoneTitle(RootModel[str]):
    """One normalized title shared by the GitHub milestone and ledger issue."""

    model_config = ConfigDict(frozen=True)

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, raw: object) -> object:
        assert isinstance(raw, str), f"milestone title must be text; found type={type(raw).__name__}; fix the CLI value or GitHub response parser"
        normalized = raw.strip()
        assert normalized, "milestone title must contain non-whitespace text; fix the CLI title"
        return normalized

    @classmethod
    def parse(cls, raw: str) -> MilestoneTitle:
        return cls.model_validate(raw)

    @property
    def value(self) -> str:
        return self.root

    @property
    def ledger_title(self) -> str:
        return f"Milestone: {self.root}"


class GithubMilestone(BaseModel):
    """Typed subset of the GitHub milestone REST response."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    number: GithubMilestoneNumber
    title: MilestoneTitle
    state: GithubMilestoneState
    html_url: str


class WorkUnitPlacement(StrEnum):
    attach = "attach"
    replace_parent = "replace_parent"


class ParentlessPriorPlacement(BaseModel):
    """A work unit that had no parent before milestone orchestration."""

    model_config = ConfigDict(frozen=True)

    kind: Literal[WorkUnitPlacement.attach]


class ParentedPriorPlacement(BaseModel):
    """A work unit's complete prior position under an existing parent."""

    model_config = ConfigDict(frozen=True)

    kind: Literal[WorkUnitPlacement.replace_parent]
    parent_number: IssueNumber
    position: Annotated[int, Field(ge=0)]


PriorWorkUnitPlacement = Annotated[
    ParentlessPriorPlacement | ParentedPriorPlacement,
    Field(discriminator="kind"),
]


class UnassignedPriorMilestone(BaseModel):
    """A work unit that had no milestone assignment before orchestration."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["unassigned"]


class AssignedPriorMilestone(BaseModel):
    """A work unit's complete prior milestone assignment."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["assigned"]
    title: str = Field(min_length=1)


PriorMilestone = Annotated[
    UnassignedPriorMilestone | AssignedPriorMilestone,
    Field(discriminator="kind"),
]


class MilestoneEffectKind(StrEnum):
    create_milestone = "create_milestone"
    create_ledger = "create_ledger"
    attach_ledger = "attach_ledger"
    assign_ledger = "assign_ledger"
    attach_work_unit = "attach_work_unit"
    replace_work_unit_parent = "replace_work_unit_parent"
    assign_work_unit = "assign_work_unit"


class MilestoneEffect(BaseModel):
    """A remote milestone operation without an existing work-unit target."""

    model_config = ConfigDict(frozen=True)

    kind: MilestoneEffectKind

    @model_validator(mode="after")
    def _coherent_kind(self) -> Self:
        assert self.kind in (
            MilestoneEffectKind.create_milestone,
            MilestoneEffectKind.create_ledger,
            MilestoneEffectKind.attach_ledger,
            MilestoneEffectKind.assign_ledger,
        ), f"untargeted milestone effect must not name a work unit; found={self.kind}; fix preflight"
        return self


class WorkUnitMilestoneEffect(MilestoneEffect):
    """A remote milestone operation targeting one preflighted work unit."""

    ref: IssueRef

    @model_validator(mode="after")
    def _coherent_kind(self) -> Self:
        assert self.kind in (
            MilestoneEffectKind.attach_work_unit,
            MilestoneEffectKind.replace_work_unit_parent,
            MilestoneEffectKind.assign_work_unit,
        ), f"work-unit milestone effect must name a targeted operation; found={self.kind}; ref={self.ref.slug}; fix preflight"
        return self


PlannedMilestoneEffect = WorkUnitMilestoneEffect | MilestoneEffect


class PlacementInquiry(BaseModel):
    """Non-write-capable milestone intent produced when placement is omitted."""

    model_config = ConfigDict(frozen=True)

    repo_ref: RepoRef
    title: MilestoneTitle


class CreateMilestoneRequest(BaseModel):
    """Write-capable milestone intent with an explicit grouping parent."""

    model_config = ConfigDict(frozen=True)

    repo_ref: RepoRef
    title: MilestoneTitle
    parent: IssueRef
    body: str
    work_units: tuple[IssueRef, ...] = ()

    @model_validator(mode="after")
    def _same_repository(self) -> Self:
        refs = (self.parent, *self.work_units)
        assert all(ref.repo_ref == self.repo_ref for ref in refs), (
            f"milestone parent and work units must match the target repository; repo={self.repo_ref.slug}; refs={[ref.slug for ref in refs]}; fix the CLI references"
        )
        return self


class ExistingWorkUnit(BaseModel):
    """Preflighted work unit with total recovery-relevant prior state."""

    model_config = ConfigDict(frozen=True)

    ref: IssueRef
    issue_id: GithubIssueId
    prior_placement: PriorWorkUnitPlacement
    prior_milestone: PriorMilestone

    @property
    def placement(self) -> WorkUnitPlacement:
        return self.prior_placement.kind


class MilestonePreflightErrorKind(StrEnum):
    repository_malformed = "repository_malformed"
    parent_invalid = "parent_invalid"
    milestone_title_collision = "milestone_title_collision"
    ledger_title_collision = "ledger_title_collision"
    duplicate_work_unit = "duplicate_work_unit"
    invalid_work_unit = "invalid_work_unit"


class MilestonePreflightRejected(BaseModel):
    """Typed terminal rejection produced before any remote write."""

    model_config = ConfigDict(frozen=True)

    kind: MilestonePreflightErrorKind
    references: tuple[str, ...]


class ValidatedMilestonePlan(BaseModel):
    """A completely preflighted request; only this type may reach execution."""

    model_config = ConfigDict(frozen=True)

    request: CreateMilestoneRequest
    parent_issue: GithubIssue
    work_units: tuple[ExistingWorkUnit, ...]
    effects: tuple[PlannedMilestoneEffect, ...]


class GithubRejectedOperation(BaseModel):
    """GitHub explicitly rejected the current mutation."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["rejected"] = "rejected"
    effect: PlannedMilestoneEffect
    detail: str = Field(min_length=1)


class GithubIndeterminateOperation(BaseModel):
    """A mutation was invoked but its remote outcome cannot be known locally."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["indeterminate"] = "indeterminate"
    effect: PlannedMilestoneEffect
    detail: str = Field(min_length=1)


RemoteOperationFailure = GithubRejectedOperation | GithubIndeterminateOperation


class MilestoneExecutionProgress(BaseModel):
    """Immutable cursor over confirmed, current, and untouched effects."""

    model_config = ConfigDict(frozen=True)

    effects: tuple[PlannedMilestoneEffect, ...]
    confirmed: tuple[PlannedMilestoneEffect, ...]
    cursor: int
    work_units: tuple[ExistingWorkUnit, ...] = ()

    @model_validator(mode="after")
    def _coherent_cursor(self) -> Self:
        assert self.effects, "milestone execution requires at least one planned effect; fix preflight"
        assert 0 <= self.cursor < len(self.effects), (
            f"milestone execution cursor must identify the current effect; cursor={self.cursor}; effect_count={len(self.effects)}; fix orchestration"
        )
        assert self.confirmed == self.effects[: self.cursor], (
            f"confirmed effects must equal the plan prefix; cursor={self.cursor}; confirmed={self.confirmed}; effects={self.effects}; fix orchestration"
        )
        return self

    @classmethod
    def start(
        cls,
        effects: tuple[PlannedMilestoneEffect, ...],
        work_units: tuple[ExistingWorkUnit, ...] = (),
    ) -> MilestoneExecutionProgress:
        return cls(effects=effects, confirmed=(), cursor=0, work_units=work_units)

    @property
    def current(self) -> PlannedMilestoneEffect:
        return self.effects[self.cursor]

    @property
    def untouched(self) -> tuple[PlannedMilestoneEffect, ...]:
        return self.effects[self.cursor + 1 :]

    def confirm(self, effect: PlannedMilestoneEffect) -> MilestoneExecutionProgress:
        assert effect == self.current, f"confirmed effect must be the current planned effect; current={self.current}; found={effect}; fix orchestration ordering"
        next_cursor = self.cursor + 1
        assert next_cursor < len(self.effects), (
            f"final effect completes execution and must produce success instead of another progress cursor; effect={effect}; fix orchestration completion"
        )
        return MilestoneExecutionProgress(
            effects=self.effects,
            confirmed=(*self.confirmed, effect),
            cursor=next_cursor,
            work_units=self.work_units,
        )

    def stop(self, outcome: RemoteOperationFailure) -> MilestoneCreationFailed:
        assert outcome.effect == self.current, (
            f"failure outcome must describe the current planned effect; current={self.current}; outcome={outcome}; fix GitHub adapter propagation"
        )
        return MilestoneCreationFailed(progress=self, outcome=outcome)


class MilestoneCreationFailed(BaseModel):
    """Terminal non-success after mutation begins."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["failed"] = "failed"
    progress: MilestoneExecutionProgress
    outcome: RemoteOperationFailure


class MilestoneCreationSucceeded(BaseModel):
    """Terminal success after every planned effect is confirmed."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["succeeded"] = "succeeded"
    milestone: GithubMilestone
    ledger: GithubIssue
    work_units: tuple[ExistingWorkUnit, ...]


MilestoneCreationResult = MilestoneCreationSucceeded | MilestoneCreationFailed


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
    labels: tuple[str, ...] = ()

    @field_validator("labels", mode="before")
    @classmethod
    def _normalize_labels(cls, value: Any) -> tuple[str, ...]:
        """Accept REST (``[{"name": ...}]``) and plain-string label shapes alike."""
        if value is None:
            return ()
        return tuple(item["name"] if isinstance(item, dict) else item for item in value)

    @classmethod
    def from_graphql(cls, node: dict) -> GithubIssue:
        """Build an issue from a GraphQL ``repository.issues`` node.

        GraphQL field names differ from REST: url vs html_url, databaseId
        vs id, upper-case state/stateReason enums. GraphQL issue nodes are
        never pull requests.
        """
        milestone = node.get("milestone")
        state_reason = node.get("stateReason")
        label_nodes = node["labels"]["nodes"]
        return cls(
            id=node["databaseId"],
            number=node["number"],
            title=node["title"],
            state=IssueState(node["state"].lower()),
            html_url=node["url"],
            body=node.get("body") or None,
            state_reason=state_reason.lower() if state_reason else None,
            milestone=Milestone(title=milestone["title"]) if milestone else None,
            labels=tuple(label["name"] for label in label_nodes),
        )

    @property
    def is_open(self) -> bool:
        """Check if the issue is in the open state."""
        return self.state == IssueState.open

    @property
    def is_pull_request(self) -> bool:
        """Whether this GitHub issues API record is actually a pull request."""
        return self.pull_request is not None


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
        """Build total adjacency and derived views once at construction time."""
        issue_numbers = set(self.issues)
        unknown_parents = sorted(set(self.children_of) - issue_numbers)
        if unknown_parents:
            raise ValueError(f"children_of has parents absent from issues: {unknown_parents}")

        full_children_of: dict[int, tuple[int, ...]] = {}
        for number in sorted(issue_numbers):
            children = self.children_of[number] if number in self.children_of else ()
            unknown_children = [child for child in children if child not in issue_numbers]
            if unknown_children:
                raise ValueError(f"children_of[{number}] has children absent from issues: {unknown_children}")
            full_children_of[number] = tuple(children)
        object.__setattr__(self, "children_of", full_children_of)

        parent_of: dict[int, int] = {}
        for parent, kids in full_children_of.items():
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
        return tuple(issue for num, issue in sorted(self.issues.items()) if num not in parents)

    @property
    def orphans(self) -> tuple[GithubIssue, ...]:
        """Issues not reachable from the primary root.

        Walks the adjacency list directly — no TreeNode materialization needed.
        """
        roots = self.roots
        if not roots:
            return ()
        primary_root_num = roots[0].number

        reachable: set[int] = set()
        self._collect_reachable(primary_root_num, reachable)
        return tuple(issue for num, issue in sorted(self.issues.items()) if num not in reachable)

    def _collect_reachable(self, number: int, out: set[int]) -> None:
        """DFS from ``number`` marking all reachable nodes via the adjacency list."""
        if number in out:
            return
        out.add(number)
        for kid in self.children_of[number]:
            self._collect_reachable(kid, out)

    def materialize_root(self, root_number: int) -> TreeNode:
        """Build a TreeNode tree from the DAG rooted at the given issue."""
        issue = self.issues[root_number]
        kids = self.children_of[root_number]
        children = tuple(self.materialize_root(k) for k in kids)
        return TreeNode(issue=issue, children=children)


class AuditFindingWitness(BaseModel):
    """Structured witness for findings whose evidence has graph semantics."""

    model_config = ConfigDict(frozen=True)

    originating_obligation: IssueRef | None = None
    current_owner: IssueRef | None = None
    edge_chain: tuple[IssueRef, ...] = ()
    conflicting_state: str | None = None
    obligation_kind: str | None = None
    unresolved_burden: str | None = None


class Finding(BaseModel):
    code: str
    severity: FindingSeverity
    title: str
    evidence: list[str]
    meaning: str
    agent_instruction: str | None = None
    remediation: list[str]
    suggested_commands: list[str] = []
    witness: AuditFindingWitness | None = None


class PresentReportRef(BaseModel):
    kind: Literal["present"] = "present"
    ref: IssueRef


class AbsentReportRef(BaseModel):
    kind: Literal["absent"] = "absent"
    reason: MissingReportRefReason


ReportRef = PresentReportRef | AbsentReportRef


class DoctorMetrics(BaseModel):
    """Doctor summary counters. Total by construction: every field is
    computed on every run; there is no missing-key state to default."""

    errors: int
    warnings: int
    open_issues_reachable_from_root: int
    open_issues_outside_root: int
    open_work_units: int
    work_units: int
    max_depth: int


class DoctorReport(BaseModel):
    repo: str
    status: ReportStatus
    root: ReportRef
    next_issue: ReportRef
    metrics: DoctorMetrics
    findings: list[Finding]


class RepoHealth(BaseModel):
    """One-line account-scan health digest for a single repository."""

    model_config = ConfigDict(frozen=True)

    slug: str
    open_issues: int
    # "ok" when the tree has exactly one ledger root; otherwise the blocking
    # root diagnostic code (E001/E002/E004).
    root_status: str
    error_count: int
    # Total by construction: present with the next issue, or absent with a
    # reason. Same representation the doctor report uses for next_issue.
    next_work_unit: ReportRef
