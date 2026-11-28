from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, NewType, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    field_validator,
    model_validator,
)

IssueNumber = Annotated[int, Field(gt=0)]
GithubIssueId = Annotated[int, Field(gt=0)]
GithubMilestoneNumber = Annotated[int, Field(gt=0)]
FindingSeverity = Literal["error", "warning", "question", "info"]
ReportStatus = Literal["ok", "warning", "error"]
MissingReportRefReason = Literal["no_root_ledger", "no_open_work_unit"]


class IssueState(StrEnum):
    open = "open"
    closed = "closed"


class RoutedOwnerState(StrEnum):
    """What a routed grouping's foreign owner currently offers."""

    open_with_work = "open_with_work"
    closed = "closed"
    no_open_work = "no_open_work"
    unreadable = "unreadable"


class RoutedOwnerResolution(BaseModel):
    """One read of a routed owner issue and its sub-tree."""

    model_config = ConfigDict(frozen=True)

    state: RoutedOwnerState
    detail: str = ""


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
        m = re.fullmatch(
            r"(?P<owner>[^/\s#]+)/(?P<repo>[^/\s#]+)#(?P<number>[1-9]\d*)", raw
        )
        if not m:
            raise ValueError(f"expected OWNER/REPO#NUMBER, got {raw!r}")
        return cls(
            repo_ref=RepoRef(owner=m["owner"], repo=m["repo"]), number=int(m["number"])
        )

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

    def same_owner(self, other: IssueRef) -> bool:
        """GitHub owner names are case-insensitive."""
        return self.repo_ref.owner.casefold() == other.repo_ref.owner.casefold()

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
        assert isinstance(raw, str), (
            f"milestone title must be text; found type={type(raw).__name__}; fix the CLI value or GitHub response parser"
        )
        normalized = raw.strip()
        assert normalized, (
            "milestone title must contain non-whitespace text; fix the CLI title"
        )
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
        ), (
            f"untargeted milestone effect must not name a work unit; found={self.kind}; fix preflight"
        )
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
        ), (
            f"work-unit milestone effect must name a targeted operation; found={self.kind}; ref={self.ref.slug}; fix preflight"
        )
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

    adopted_milestone: GithubMilestone | None = None


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
        assert self.effects, (
            "milestone execution requires at least one planned effect; fix preflight"
        )
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
        assert effect == self.current, (
            f"confirmed effect must be the current planned effect; current={self.current}; found={effect}; fix orchestration ordering"
        )
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
    node_id: IssueNodeId | None = None
    number: IssueNumber
    title: str
    state: IssueState
    html_url: str
    body: str | None = None
    state_reason: str | None = None
    milestone: Milestone | None = None
    pull_request: dict[str, JsonValue] | None = None
    labels: tuple[str, ...] = ()

    @field_validator("labels", mode="before")
    @classmethod
    def _normalize_labels(cls, value: JsonValue) -> tuple[str, ...]:
        """Accept REST (``[{"name": ...}]``) and plain-string label shapes alike."""
        if value is None:
            return ()
        assert isinstance(value, (list, tuple)), value
        labels: list[str] = []
        for item in value:
            if isinstance(item, dict):
                name = item["name"]
            else:
                name = item
            assert isinstance(name, str), name
            labels.append(name)
        return tuple(labels)

    @classmethod
    def from_graphql(cls, node: dict[str, JsonValue]) -> GithubIssue:
        """Build an issue from a GraphQL ``repository.issues`` node.

        GraphQL field names differ from REST: url vs html_url, databaseId
        vs id, upper-case state/stateReason enums. GraphQL issue nodes are
        never pull requests.
        """
        milestone = node.get("milestone")
        assert milestone is None or isinstance(milestone, dict)
        state_reason = node.get("stateReason")
        assert state_reason is None or isinstance(state_reason, str)
        labels = node["labels"]
        assert isinstance(labels, dict)
        label_nodes = labels["nodes"]
        assert isinstance(label_nodes, list)
        database_id = node["databaseId"]
        assert isinstance(database_id, int) and not isinstance(database_id, bool)
        number = node["number"]
        assert isinstance(number, int) and not isinstance(number, bool)
        title = node["title"]
        assert isinstance(title, str)
        state = node["state"]
        assert isinstance(state, str)
        html_url = node["url"]
        assert isinstance(html_url, str)
        body = node.get("body")
        assert body is None or isinstance(body, str)
        milestone_title = None if milestone is None else milestone["title"]
        assert milestone_title is None or isinstance(milestone_title, str)
        raw_node_id = node.get("id")
        assert raw_node_id is None or isinstance(raw_node_id, str)
        node_id = IssueNodeId(raw_node_id) if raw_node_id else None
        return cls(
            id=database_id,
            node_id=node_id,
            number=number,
            title=title,
            state=IssueState(state.lower()),
            html_url=html_url,
            body=body or None,
            state_reason=state_reason.lower() if state_reason else None,
            milestone=Milestone(title=milestone_title) if milestone_title else None,
            labels=cls._normalize_labels(label_nodes),
        )

    @property
    def is_open(self) -> bool:
        """Check if the issue is in the open state."""
        return self.state == IssueState.open

    @property
    def is_pull_request(self) -> bool:
        """Whether this GitHub issues API record is actually a pull request."""
        return self.pull_request is not None

    @property
    def repository_slug(self) -> str:
        """Return OWNER/REPO of the repository that owns this issue, read from its html_url."""
        m = re.fullmatch(
            r"https://github\.com/(?P<slug>[^/]+/[^/]+)/(?:issues|pull)/\d+",
            self.html_url,
        )
        if not m:
            raise ValueError(f"cannot read repository from html_url {self.html_url!r}")
        return m["slug"]


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


SAME_OWNER_RULE = "GitHub sub-issues must belong to the same owner as the parent issue"


class AttachRequest(BaseModel):
    """Request to attach child as a GitHub sub-issue of parent."""

    parent: IssueRef
    child: IssueRef

    @model_validator(mode="after")
    def same_owner(self) -> Self:
        """Validate that parent and child have one owner and are different issues."""
        if not self.parent.same_owner(self.child):
            raise ValueError(SAME_OWNER_RULE)
        if self.parent == self.child:
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
        """Validate move request constraints: before/after mutual exclusivity, one owner, not self."""
        if self.before is not None and self.after is not None:
            raise ValueError("use either --before or --after, not both")
        refs = [
            self.child,
            self.parent,
            *(r for r in [self.before, self.after] if r is not None),
        ]
        if any(not self.parent.same_owner(r) for r in refs):
            raise ValueError(SAME_OWNER_RULE)
        if self.child == self.parent:
            raise ValueError("cannot move an issue under itself")
        return self


class DetachRequest(BaseModel):
    """Request to detach a child issue from its parent."""

    parent: IssueRef
    child: IssueRef

    @model_validator(mode="after")
    def same_owner(self) -> Self:
        """Validate that parent and child have one owner."""
        if not self.parent.same_owner(self.child):
            raise ValueError(SAME_OWNER_RULE)
        return self


class RepoDag(BaseModel):
    """Full directed acyclic graph of a repository's issue tree.

    Constructed by scanning every issue and its sub-issues.  This is
    the single source of truth that all query commands operate on.
    """

    model_config = ConfigDict(frozen=True)

    repo_ref: RepoRef

    issues: dict[int, GithubIssue]

    children_of: dict[int, tuple[int, ...]]

    parent_of: dict[int, int] = Field(default_factory=dict, repr=False)

    dependencies: dict[int, tuple[int, ...]] = Field(default_factory=dict)

    foreign_children: dict[int, tuple[IssueRef, ...]] = Field(default_factory=dict)
    foreign_blockers: dict[int, tuple[IssueRef, ...]] = Field(default_factory=dict)

    foreign_blocker_states: dict[IssueRef, IssueState] = Field(default_factory=dict)

    ordered_children: dict[int, tuple[IssueRef, ...]] = Field(default_factory=dict)
    foreign_issues: dict[IssueRef, GithubIssue] = Field(default_factory=dict)
    foreign_parent: dict[int, IssueRef] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _compute_derived(self) -> Self:
        """Build total adjacency and derived views once at construction time."""
        issue_numbers = set(self.issues)
        unknown_parents = sorted(set(self.children_of) - issue_numbers)
        if unknown_parents:
            raise ValueError(
                f"children_of has parents absent from issues: {unknown_parents}"
            )

        full_children_of: dict[int, tuple[int, ...]] = {}
        for number in sorted(issue_numbers):
            children = self.children_of[number] if number in self.children_of else ()
            unknown_children = [
                child for child in children if child not in issue_numbers
            ]
            if unknown_children:
                raise ValueError(
                    f"children_of[{number}] has children absent from issues: {unknown_children}"
                )
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
        """Issues that are not sub-issues of any other issue, in this or another repository."""
        parents = self.parent_of
        return tuple(
            issue
            for num, issue in sorted(self.issues.items())
            if num not in parents and num not in self.foreign_parent
        )

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
        return tuple(
            issue for num, issue in sorted(self.issues.items()) if num not in reachable
        )

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
    witnesses: tuple[AuditFindingWitness, ...] = ()


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


class FixtureState(BaseModel):
    model_config = ConfigDict(frozen=True)

    repo: str
    root: Path
    checkout: Path
    selected: int
    selected_body: str
    roadmap: int
    roadmap_body: str
    roadmap_comment_id: int
    roadmap_comment_url: str
    binding_comment_id: int
    binding_comment_url: str
    cited: int
    cited_comment_id: int
    citation_edge: int
    citation_runtime_gh: Path
    citation_shape_gh: Path
    content_shape_gh: Path
    malformed_issue_gh: Path
    governing_shape_gh: Path
    base64_error_gh: Path
    trace_gh: Path
    large_text_size: int
    pull_number: int
    pagination: int
    pagination_comments: list[str]
    pagination_ids: list[int]
    malformed: dict[str, int]
    malformed_text: dict[str, str]
    fenced: int
    fenced_body: str
    legacy: int
    legacy_body: str
    cycle_a: int
    cycle_b: int
    cycle_gh: Path
    fail_comments_gh: Path
    assertion_gh: Path
    pinned: str
    head: str
    source_urls: dict[str, str]
    external_text: dict[str, str]
    setup_elapsed_seconds: float
    python: str


class RepoHealth(BaseModel):
    """One-line account-scan health digest for a single repository."""

    model_config = ConfigDict(frozen=True)

    slug: str
    open_issues: int

    root_status: str
    error_count: int
    warning_count: int = 0

    next_work_unit: ReportRef


IssueCommentId = Annotated[int, Field(gt=0)]
CommitSha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
CanonicalSourceUrl = Annotated[str, Field(min_length=1)]


class GitRefKind(StrEnum):
    commit = "commit"
    branch = "branch"
    tag = "tag"


class GitRefIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    kind: GitRefKind


class GitRefResolution(BaseModel):
    model_config = ConfigDict(frozen=True)

    requested_ref: str = Field(min_length=1)
    resolved_commit_sha: CommitSha
    repository_path: str
    matched_refs: tuple[GitRefIdentity, ...] = Field(min_length=1)


class GithubInstallation(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    id: int = Field(gt=0)
    node_id: str = Field(min_length=1)
    slug: str = Field(min_length=1)


class IssueComment(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: IssueCommentId
    node_id: CommentNodeId
    body: str
    author: str = Field(alias="user")
    created_at: str
    updated_at: str
    html_url: str
    performed_via_github_app: GithubInstallation | None = None

    @field_validator("author", mode="before")
    @classmethod
    def _author_login(cls, value: dict[str, str]) -> str:
        return value["login"]


class GithubIssueFamily(BaseModel):
    model_config = ConfigDict(frozen=True)

    issue: GithubIssue
    comments: tuple[IssueComment, ...]
    parent: IssueRef | None


class GithubRepository(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    default_branch: str = Field(min_length=1)


class GithubContentFile(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    kind: Literal["file"] = "file"
    type: Literal["file"]
    path: str
    encoding: str
    content: str | None
    sha: CommitSha


class GithubNonFileContent(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    kind: Literal["non_file"] = "non_file"
    type: str
    path: str
    sha: CommitSha


GithubContentObject = Annotated[
    GithubContentFile | GithubNonFileContent, Field(discriminator="kind")
]


class GithubBlob(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    encoding: Literal["base64"]
    content: str


class GithubContentItem(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    type: str
    path: str


class MissingGithubContent(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    ref: str


class GithubRefMatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    kind: GitRefKind
    object_sha: CommitSha
    target_type: str


class GithubCommit(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    sha: CommitSha


IssueNodeId = NewType("IssueNodeId", Annotated[str, Field(min_length=1)])
RequestId = NewType("RequestId", Annotated[str, Field(min_length=1)])
AttemptId = NewType("AttemptId", Annotated[str, Field(min_length=1)])
OperationId = NewType("OperationId", Annotated[str, Field(min_length=1)])
SessionId = NewType("SessionId", Annotated[str, Field(min_length=1)])
TaskId = NewType("TaskId", Annotated[str, Field(pattern=r"^T[0-9]+$")])
DeclaredOrder = NewType("DeclaredOrder", Annotated[int, Field(ge=0)])
RequirementId = NewType("RequirementId", Annotated[str, Field(min_length=1)])
GoalId = NewType("GoalId", Annotated[str, Field(min_length=1)])
ArtifactKey = NewType("ArtifactKey", Annotated[str, Field(min_length=1)])
Sha256 = NewType("Sha256", Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")])
CommentNodeId = NewType("CommentNodeId", Annotated[str, Field(min_length=1)])
ActorLogin = NewType("ActorLogin", Annotated[str, Field(min_length=1)])
RepoSlug = Annotated[str, Field(pattern=r"^[^/\s]+/[^/\s]+$")]


class StageModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class TaskField(StrEnum):
    state = "state"
    prerequisites = "prerequisites"
    requirements = "requirements"
    evidence = "evidence"
    goals = "goals"
    description = "description"
    acceptance = "acceptance"
    order = "order"
    opaque = "opaque"


TaskParseFailure = Literal[
    "duplicate_id",
    "missing_id",
    "missing_field",
    "malformed_ref",
    "ambiguous_span",
    "unsupported_field",
    "invalid_order",
    "unknown_evidence_kind",
    "unsupported_task_wire",
]


class IssueCommentEvidence(StageModel):
    kind: Literal["issue_comment"]
    repository: RepoSlug
    issue_number: IssueNumber
    comment_id: IssueCommentId
    comment_node_id: CommentNodeId
    sha256: Sha256
    url: str

    @model_validator(mode="after")
    def _matches_issue_comment_url(self) -> Self:
        expected = (
            f"https://github.com/{self.repository}/issues/{self.issue_number}"
            f"#issuecomment-{self.comment_id}"
        )
        assert self.url == expected, (self.url, expected)
        return self


class CommitEvidence(StageModel):
    kind: Literal["commit"]
    repository: RepoSlug
    sha: CommitSha
    url: str

    @model_validator(mode="after")
    def _matches_commit_url(self) -> Self:
        expected = f"https://github.com/{self.repository}/commit/{self.sha}"
        assert self.url == expected, (self.url, expected)
        return self


class PullRequestEvidence(StageModel):
    kind: Literal["pull_request"]
    repository: RepoSlug
    number: IssueNumber
    head_sha: CommitSha
    url: str

    @model_validator(mode="after")
    def _matches_pull_request_url(self) -> Self:
        expected = f"https://github.com/{self.repository}/pull/{self.number}"
        assert self.url == expected, (self.url, expected)
        return self


class DocumentedTestReport(StageModel):
    repository: RepoSlug
    issue_number: IssueNumber
    attempt_id: AttemptId
    report_id: str = Field(min_length=1)


class DocumentedTestEvidence(StageModel):
    kind: Literal["documented_test"]
    command: str = Field(min_length=1)
    report: DocumentedTestReport


EvidenceRef = Annotated[
    IssueCommentEvidence
    | CommitEvidence
    | PullRequestEvidence
    | DocumentedTestEvidence,
    Field(discriminator="kind"),
]


class PendingEvidence(StageModel):
    pending: str = Field(min_length=1)


class ResolvedEvidence(StageModel):
    resolved: tuple[EvidenceRef, ...] = Field(min_length=1)


Evidence = PendingEvidence | ResolvedEvidence


class GoalContribution(StageModel):
    id: GoalId
    rationale: str = Field(min_length=1)


class LocalTaskRef(StageModel):
    task_id: TaskId


class QualifiedTaskRef(StageModel):
    issue_ref: IssueRef
    task_id: TaskId


TaskPrerequisite = LocalTaskRef | QualifiedTaskRef
TaskState = Literal["unfinished", "complete"]


class SourceSpan(StageModel):
    character_start: int = Field(ge=0)
    character_end: int = Field(ge=0)
    byte_start: int = Field(ge=0)
    byte_end: int = Field(ge=0)


class OpaqueSpan(StageModel):
    source: str
    span: SourceSpan


class TaskRecord(StageModel):
    id: TaskId
    order: DeclaredOrder
    state: TaskState
    prerequisites: tuple[TaskPrerequisite, ...]
    requirements: tuple[RequirementId, ...]
    evidence: Evidence
    goals: tuple[GoalContribution, ...]
    description: str
    acceptance: str
    source: str
    field_spans: dict[str, SourceSpan]
    state_span: SourceSpan


class TaskDocument(StageModel):
    source: str
    wrapped: bool
    tasks: tuple[TaskRecord, ...]
    opaque: tuple[OpaqueSpan, ...]


class WriterSealInvalidContext(StageModel):
    kind: Literal["writer_seal_invalid"]
    repository: RepoSlug
    expected_default_branch: str = Field(min_length=1)
    observed_ref: str | None
    expected_workflow_head: CommitSha | None
    observed_workflow_head: str | None
    workflow_blob: str | None
    run_id: int | None = Field(default=None, gt=0)
    expected_run_attempt: int | None = Field(default=None, gt=0)
    observed_run_attempt: int | None = Field(default=None, gt=0)
    job_id: int | None = Field(default=None, gt=0)
    job_node_id: str | None
    failure_dimension: Literal[
        "fake_human_authority",
        "wrong_ref",
        "stale_workflow",
        "mismatched_run_attempt",
        "writer_identity_unavailable",
        "writer_permission_insufficient",
    ]


class ExternalSubmissionDetail(StageModel):
    kind: Literal["external_submission"]
    actor: ActorLogin | None
    reason: Literal["seal_unavailable", "edited_record", "role_or_session"]


class MergeConflictDetail(StageModel):
    kind: Literal["merge_conflict"]
    section: Literal["original-contract", "requirements", "plan", "tasks"]
    task_id: TaskId | None
    field: TaskField | None
    competing_request_id: RequestId | None


class OperationLogBinding(StageModel):
    kind: Literal["operation_log_binding"] = "operation_log_binding"
    operation_id: OperationId
    issue_node_id: IssueNodeId
    request_id: RequestId
    attempt_id: AttemptId
    revision: CommitSha
    workflow_path: Literal[".github/workflows/itree-issue-writer.yml"]
    workflow_sha256: Sha256


class OperationFailureDetail(StageModel):
    kind: Literal["operation_failure"]
    reason: Literal[
        "github_http_rejection",
        "layout_invalid",
        "fence_changed",
        "candidate_changed",
        "readback_mismatch",
        "authorization_revoked",
        "node_changed",
    ]
    diagnostic: str = Field(min_length=1)


AdmissionRejectionDetails = Annotated[
    WriterSealInvalidContext | ExternalSubmissionDetail | MergeConflictDetail,
    Field(discriminator="kind"),
]
OperationRejectionDetails = AdmissionRejectionDetails | OperationFailureDetail


class CommentHistory(StageModel):
    created_at: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)
    edit_total_count: int = Field(ge=0)
    history_exhausted: bool

    def is_zero_edit(self) -> bool:
        return self.edit_total_count == 0 and self.history_exhausted


class SubmissionSeal(StageModel):
    native_issue_comment: IssueComment
    logical_payload_sha256: Sha256
    exact_wire_body_sha256: Sha256
    history: CommentHistory
    target_issue_node: IssueNodeId


class WriterSeal(StageModel):
    repository: RepoSlug
    default_branch: str = Field(min_length=1)
    default_head_sha: CommitSha
    workflow_path: Literal[".github/workflows/itree-issue-writer.yml"]
    workflow_blob: str = Field(min_length=1)
    workflow_sha256: Sha256
    run_id: int = Field(gt=0)
    run_attempt: int = Field(gt=0)
    job_id: int = Field(gt=0)
    job_node_id: str = Field(min_length=1)
    job_name: str = Field(min_length=1)
    run_url: str = Field(min_length=1)
    job_url: str = Field(min_length=1)
    actions_installation_identity: str = Field(min_length=1)
    effective_token_permissions: dict[str, Literal["read", "write"]]

    @model_validator(mode="after")
    def _least_permissions(self) -> Self:
        assert self.effective_token_permissions == {
            "contents": "read",
            "issues": "write",
            "actions": "read",
        }, self.effective_token_permissions
        return self


class SealedPayload(StageModel):
    submission_seal: SubmissionSeal
    logical_payload_sha256: Sha256
    external_actor_permission: Literal[
        "admin", "maintain", "write", "triage", "read", "none", "custom"
    ]


class WriterRecord(StageModel):
    writer_seal: WriterSeal
    logical_payload_sha256: Sha256
    native_issue_comment: IssueComment
    zero_edit_history: CommentHistory


class RequestIdentity(StageModel):
    issue_node_id: IssueNodeId
    issue_ref: IssueRef
    request_id: RequestId
    attempt_id: AttemptId


ArtifactKind = Literal[
    "research",
    "data-model",
    "contract",
    "quickstart",
    "clarification",
    "finding",
    "evidence",
    "checkpoint",
    "plan-history",
    "task-history",
]


class ArtifactIndexEntry(StageModel):
    key: ArtifactKey
    kind: Literal[
        "research",
        "data-model",
        "contract",
        "quickstart",
        "clarification",
        "finding",
        "evidence",
        "checkpoint",
        "plan-history",
        "task-history",
    ]
    comment_id: IssueCommentId
    comment_node_id: CommentNodeId
    url: str = Field(min_length=1)
    content_sha256: Sha256
    attempt_id: AttemptId


class ReceiptContent(StageModel):
    base_body_sha256: Sha256 | None = None
    candidate_body_sha256: Sha256 | None = None
    live_body_sha256: Sha256 | None = None
    original_sha256: Sha256 | None = None
    fence_sha256: Sha256 | None = None
    no_body_write: bool = False
    stage_complete: bool = False
    recovery_action: str | None = None
    high_water_comment_id: IssueCommentId | None = None
    artifacts: tuple[ArtifactIndexEntry, ...] = ()


ReceiptStatus = Literal[
    "intended",
    "confirmed",
    "rejected",
    "indeterminate",
    "session_admitted",
    "session_released",
    "drain_scanned",
    "seal_invalidated",
]
ReceiptRejectionReason = Literal[
    "duplicate_payload_conflict",
    "github_http_rejection",
    "conflict",
    "writer_seal_invalid",
    "layout_invalid",
    "fence_changed",
    "candidate_changed",
    "readback_mismatch",
    "authorization_revoked",
    "node_changed",
    "external_submission",
]


class ReceiptPayload(StageModel):
    version: Literal[1]
    issue_node_id: IssueNodeId
    issue_ref: IssueRef
    request_id: RequestId
    attempt_id: AttemptId
    operation_id: OperationId | None
    status: Literal[
        "intended",
        "confirmed",
        "rejected",
        "indeterminate",
        "session_admitted",
        "session_released",
        "drain_scanned",
        "seal_invalidated",
    ]
    content: ReceiptContent
    rejection_reason: (
        Literal[
            "duplicate_payload_conflict",
            "github_http_rejection",
            "conflict",
            "writer_seal_invalid",
            "layout_invalid",
            "fence_changed",
            "candidate_changed",
            "readback_mismatch",
            "authorization_revoked",
            "node_changed",
            "external_submission",
        ]
        | None
    ) = None
    rejection_details: OperationRejectionDetails | None = None
    indeterminate_reason: (
        Literal[
            "timeout_after_invocation",
            "interrupted_after_invocation",
            "no_usable_http_response",
            "nonzero_without_rejection",
            "unusable_success_response",
            "readback_unavailable",
            "receipt_unavailable",
        ]
        | None
    ) = None
    writer_seal: WriterSeal


class ReceiptEnvelope(StageModel):
    native_issue_comment: IssueComment
    payload: ReceiptPayload
    writer_seal: WriterSeal


class Confirmed(StageModel):
    kind: Literal["confirmed"] = "confirmed"
    receipt_id: IssueCommentId
    live_body_sha256: Sha256
    original_sha256: Sha256
    fence_sha256: Sha256
    artifacts: tuple[ArtifactIndexEntry, ...]
    no_body_write: bool
    stage_complete: Literal[True] = True


class Rejected(StageModel):
    kind: Literal["rejected"] = "rejected"
    receipt_id: IssueCommentId
    rejection_reason: Literal[
        "duplicate_payload_conflict",
        "github_http_rejection",
        "conflict",
        "writer_seal_invalid",
        "layout_invalid",
        "fence_changed",
        "candidate_changed",
        "readback_mismatch",
        "authorization_revoked",
        "node_changed",
        "external_submission",
    ]
    retained_live_sha256: Sha256
    rejection_details: OperationRejectionDetails
    competing_request_id: RequestId | None = None
    stage_complete: Literal[False] = False


class Indeterminate(StageModel):
    kind: Literal["indeterminate"] = "indeterminate"
    receipt_id: IssueCommentId
    indeterminate_reason: Literal[
        "timeout_after_invocation",
        "interrupted_after_invocation",
        "no_usable_http_response",
        "nonzero_without_rejection",
        "unusable_success_response",
        "readback_unavailable",
        "receipt_unavailable",
    ]
    operation_id: OperationId
    last_observed_body_sha256: Sha256 | None
    recovery_action: str = Field(min_length=1)
    stage_complete: Literal[False] = False


OperationOutcome = Confirmed | Rejected | Indeterminate


class PartDescriptor(StageModel):
    role: str = Field(min_length=1)
    ordinal: int = Field(ge=0)
    payload_sha256: Sha256


class UploadManifest(StageModel):
    version: Literal[1]
    issue_node_id: IssueNodeId
    issue_ref: IssueRef
    request_id: RequestId
    attempt_id: AttemptId
    session_id: SessionId
    base_body_sha256: Sha256 | None = None
    request_kind: Literal[
        "sections",
        "task_field",
        "legacy_intake",
        "artifacts",
        "session_admit",
        "session_release",
    ]
    targets: tuple[str, ...]
    base_roles: tuple[str, ...]
    proposed_roles: tuple[str, ...]
    expected_part_count: int = Field(gt=0)
    parts: tuple[PartDescriptor, ...] = Field(min_length=1)
    finalized_proposal_sha256: Sha256

    @model_validator(mode="after")
    def _counts_and_ordinals(self) -> Self:
        assert self.expected_part_count == len(self.parts), self.expected_part_count
        identities = [(part.role, part.ordinal) for part in self.parts]
        assert len(identities) == len(set(identities)), identities
        return self


class UploadPart(StageModel):
    version: Literal[1]
    issue_node_id: IssueNodeId
    issue_ref: IssueRef
    request_id: RequestId
    attempt_id: AttemptId
    manifest_request_id: RequestId
    manifest_payload_sha256: Sha256
    role: str = Field(min_length=1)
    ordinal: int = Field(ge=0)
    payload: str
    payload_sha256: Sha256


class RegeneratedUploadPart(UploadPart):
    writer_seal: WriterSeal


class MissingPart(StageModel):
    role: str = Field(min_length=1)
    ordinal: int = Field(ge=0)
    expected_payload_sha256: Sha256


class PendingUpload(StageModel):
    kind: Literal["pending_upload"] = "pending_upload"
    validated_manifest: UploadManifest
    present_sealed_parts: tuple[UploadPart, ...]
    missing_parts: tuple[MissingPart, ...] = Field(min_length=1)


class FinalizedProposal(StageModel):
    kind: Literal["finalized"] = "finalized"
    manifest: UploadManifest
    parts: tuple[UploadPart, ...] = Field(min_length=1)
    logical_payload_sha256: Sha256
    proposal: JsonValue


UploadRejectionReason = Literal[
    "duplicate_part",
    "unexpected_part",
    "wrong_order",
    "digest_mismatch",
    "manifest_mismatch",
    "unauthenticated_regeneration",
]


class UploadRejected(StageModel):
    kind: Literal["upload_rejected"] = "upload_rejected"
    reason: UploadRejectionReason


class UploadContent(StageModel):
    role: str = Field(min_length=1)
    ordinal: int = Field(ge=0)
    payload: str


class RestartedAttempt(StageModel):
    kind: Literal["restarted_attempt"] = "restarted_attempt"
    prior: PendingUpload
    required_new_attempt: AttemptId


class AdoptedNewAttempt(StageModel):
    kind: Literal["adopted_new_attempt"] = "adopted_new_attempt"
    prior: PendingUpload
    new_attempt: AttemptId


UploadResolution = PendingUpload | FinalizedProposal | UploadRejected


class AdmissionRejected(StageModel):
    kind: Literal["rejected"] = "rejected"
    request: RequestIdentity
    reason: Literal[
        "missing_node",
        "node_mismatch",
        "wrong_repository",
        "issue_unavailable",
        "missing_permission",
        "actor_not_enrolled",
        "role_insufficient",
        "writer_seal_invalid",
        "seal_unavailable",
        "edited_record",
        "seal_invalidated",
        "malformed_payload",
        "changed_payload",
        "invalid_base",
        "invalid_layout",
        "original_contract_target",
        "external_mutation",
    ]
    live_body_sha256: Sha256
    rejection_details: AdmissionRejectionDetails


class PendingManagedSession(StageModel):
    kind: Literal["pending_managed_session"] = "pending_managed_session"
    reason: Literal[
        "direct_edits_unsettled",
        "editor_not_enrolled",
        "previous_operation_indeterminate",
    ]
    required_action: str = Field(min_length=1)


class SectionReplacement(StageModel):
    section: Literal["requirements", "plan", "tasks"]
    base_text: str
    proposed_text: str
    base_sha256: Sha256
    proposed_sha256: Sha256


class SectionEditProposal(StageModel):
    version: Literal[1]
    request_kind: Literal["sections"]
    issue_node_id: IssueNodeId
    issue_ref: IssueRef
    request_id: RequestId
    attempt_id: AttemptId
    session_id: SessionId
    base_body_sha256: Sha256
    base_body: str
    changes: tuple[SectionReplacement, ...] = Field(min_length=1)
    artifacts: tuple[ArtifactDraft, ...] = ()


class SessionAdmissionRequest(StageModel):
    version: Literal[1]
    request_kind: Literal["session_admit"]
    issue_node_id: IssueNodeId
    issue_ref: IssueRef
    request_id: RequestId
    attempt_id: AttemptId
    session_id: SessionId
    entry_body_sha256: Sha256
    enrolled_actors: tuple[ActorLogin, ...] = Field(min_length=1)


class SessionReleaseRequest(StageModel):
    version: Literal[1]
    request_kind: Literal["session_release"]
    issue_node_id: IssueNodeId
    issue_ref: IssueRef
    request_id: RequestId
    attempt_id: AttemptId
    session_id: SessionId
    expected_last_scanned_comment_id: IssueCommentId


class LegacyIntakeProposal(StageModel):
    version: Literal[1]
    request_kind: Literal["legacy_intake"]
    issue_node_id: IssueNodeId
    issue_ref: IssueRef
    request_id: RequestId
    attempt_id: AttemptId
    session_id: SessionId
    base_body_sha256: Sha256
    legacy_base: str
    requirements: str
    plan: str
    tasks: str


class TaskFieldProposal(StageModel):
    version: Literal[1]
    request_kind: Literal["task_field"]
    issue_node_id: IssueNodeId
    issue_ref: IssueRef
    request_id: RequestId
    attempt_id: AttemptId
    session_id: SessionId
    base_body_sha256: Sha256
    base_body: str
    task_id: TaskId
    field: TaskField
    full_task_base_sha256: Sha256
    base_value: str
    proposed_value: str


class ArtifactDraft(StageModel):
    key: ArtifactKey
    kind: Literal[
        "research",
        "data-model",
        "contract",
        "quickstart",
        "clarification",
        "finding",
        "evidence",
        "checkpoint",
        "plan-history",
        "task-history",
    ]
    content: str


class GithubActor(StageModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    login: ActorLogin


class CollaboratorPermission(StageModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    permission: Literal[
        "admin", "maintain", "write", "triage", "read", "none", "custom"
    ]
    role_name: str = Field(min_length=1)


class ActionsStepMetadata(StageModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    name: str = Field(min_length=1)
    number: int = Field(gt=0)
    status: Literal["queued", "pending", "in_progress", "completed"]
    conclusion: Literal["success", "failure", "cancelled", "skipped"] | None


class ActionsJobMetadata(StageModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    id: int = Field(gt=0)
    node_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    run_id: int = Field(gt=0)
    run_attempt: int = Field(gt=0)
    head_sha: CommitSha
    head_branch: str = Field(min_length=1)
    html_url: str = Field(min_length=1)
    run_url: str = Field(min_length=1)
    steps: tuple[ActionsStepMetadata, ...]


class StageHttpApplied(StageModel):
    kind: Literal["applied"] = "applied"
    response_body: str


class StageHttpRejected(StageModel):
    kind: Literal["http_rejected"] = "http_rejected"
    status: int = Field(ge=400, le=599)
    diagnostic: str = Field(min_length=1)


StageIndeterminateReason = Literal[
    "timeout_after_invocation",
    "interrupted_after_invocation",
    "no_usable_http_response",
    "nonzero_without_rejection",
    "unusable_success_response",
    "readback_unavailable",
    "receipt_unavailable",
]


class StageHttpIndeterminate(StageModel):
    kind: Literal["indeterminate"] = "indeterminate"
    reason: StageIndeterminateReason
    diagnostic: str = Field(min_length=1)


StageHttpOutcome = StageHttpApplied | StageHttpRejected | StageHttpIndeterminate


class ArtifactProposal(StageModel):
    version: Literal[1]
    request_kind: Literal["artifacts"]
    issue_node_id: IssueNodeId
    issue_ref: IssueRef
    request_id: RequestId
    attempt_id: AttemptId
    session_id: SessionId
    base_body_sha256: Sha256
    base_body: str
    artifacts: tuple[ArtifactDraft, ...] = Field(min_length=1)


StageProposal = Annotated[
    SectionEditProposal | LegacyIntakeProposal | TaskFieldProposal | ArtifactProposal,
    Field(discriminator="request_kind"),
]


class ArtifactPayload(StageModel):
    version: Literal[1]
    issue_node_id: IssueNodeId
    issue_ref: IssueRef
    request_id: RequestId
    attempt_id: AttemptId
    key: ArtifactKey
    kind: Literal[
        "research",
        "data-model",
        "contract",
        "quickstart",
        "clarification",
        "finding",
        "evidence",
        "checkpoint",
        "plan-history",
        "task-history",
    ]
    payload_sha256: Sha256
    content: str
    writer_seal: WriterSeal
