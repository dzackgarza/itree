from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Sequence
from enum import StrEnum
from typing import Annotated, Literal, NoReturn, TypeVar

from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict, Field

from .github import GithubApi
from .models import (
    CanonicalSourceUrl,
    CommitSha,
    GitRefResolution,
    GithubIssue,
    IssueComment,
    IssueRef,
)


class SectionName(StrEnum):
    original_contract = "original-contract"
    requirements = "requirements"
    plan = "plan"
    tasks = "tasks"


class SectionAbsence(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: SectionName
    reason: Literal["legacy_zero_marker"]


class IssueSection(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: SectionName
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    byte_start: int = Field(ge=0)
    byte_end: int = Field(ge=0)
    text: str


class LegacyIssueLayout(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["legacy"] = "legacy"
    body: str
    original_contract: str
    absent_sections: tuple[SectionAbsence, ...]


class ManagedIssueLayout(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["managed"] = "managed"
    body: str
    sections: tuple[IssueSection, ...] = Field(min_length=4, max_length=4)


class SectionError(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    line: int = Field(ge=1)
    reason: Literal[
        "unknown", "duplicate", "nested", "partial", "reordered", "unterminated"
    ]


class InvalidIssueLayout(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["invalid"] = "invalid"
    body: str
    errors: tuple[SectionError, ...] = Field(min_length=1)


ValidIssueLayout = LegacyIssueLayout | ManagedIssueLayout
IssueLayout = ValidIssueLayout | InvalidIssueLayout


class GoverningDocumentRole(StrEnum):
    agents = "AGENTS.md"
    intent = "INTENT.md"
    constitution = ".specify/memory/constitution.md"
    contributing = "CONTRIBUTING.md"
    workflows = "src/itree/WORKFLOWS.md"


class PresentGoverningDocument(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["present"] = "present"
    role: GoverningDocumentRole
    path: str
    resolved_commit_sha: CommitSha
    content: str


class AbsentGoverningDocument(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["absent"] = "absent"
    role: GoverningDocumentRole
    path: str
    absence_reason: Literal["not_found"]


GoverningDocument = Annotated[
    PresentGoverningDocument | AbsentGoverningDocument, Field(discriminator="kind")
]


class GoverningAncestor(BaseModel):
    model_config = ConfigDict(frozen=True)

    issue_ref: IssueRef
    issue: GithubIssue
    comments: tuple[IssueComment, ...]


class Citation(BaseModel):
    model_config = ConfigDict(frozen=True)

    citation: str
    canonical_url: CanonicalSourceUrl
    aliases: tuple[str, ...] = Field(min_length=1)


class PresentSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["present"] = "present"
    citation: Citation
    canonical_url: CanonicalSourceUrl
    source_kind: Literal[
        "github_issue",
        "github_comment",
        "github_blob",
        "markdown",
        "plain_text",
        "html",
    ]
    content: str
    requested_url: str
    final_url: str
    media_type: str
    resolution: GitRefResolution | None = None


class UnresolvedSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["unresolved"] = "unresolved"
    citation: Citation
    reason: Literal[
        "bare_reference",
        "unsupported_kind",
        "not_found",
        "unknown_ref",
        "ambiguous_ref",
        "inaccessible",
        "redirect_failed",
        "unsupported_media_type",
        "invalid_text_encoding",
        "binary",
        "invalid",
    ]


SourceDisposition = Annotated[
    PresentSource | UnresolvedSource, Field(discriminator="kind")
]


class WorkPackage(BaseModel):
    model_config = ConfigDict(frozen=True)

    issue_ref: IssueRef
    selected_issue: GithubIssue
    comments: tuple[IssueComment, ...]
    layout: ValidIssueLayout
    ancestors: tuple[GoverningAncestor, ...]
    governing_documents: tuple[GoverningDocument, ...]
    present_sources: tuple[PresentSource, ...]
    unresolved_sources: tuple[UnresolvedSource, ...]


class ReadableWorkPackage(BaseModel):
    model_config = ConfigDict(frozen=True)

    issue_ref: IssueRef
    selected_issue: GithubIssue
    comments: tuple[IssueComment, ...]
    layout: IssueLayout
    ancestors: tuple[GoverningAncestor, ...]


class WorkPackageContentFailure(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["invalid_layout", "parent_cycle"]
    readable: ReadableWorkPackage
    diagnostics: tuple[str, ...] = Field(min_length=1)


class WorkPackageReadFailure(BaseModel):
    model_config = ConfigDict(frozen=True)

    issue_ref: IssueRef
    operation: str
    diagnostic: str = Field(min_length=1)


class WorkPackageReadError(RuntimeError):
    def __init__(self, failure: WorkPackageReadFailure) -> None:
        self.failure = failure
        super().__init__(failure.diagnostic)


class WorkPackageContentError(RuntimeError):
    def __init__(self, failure: WorkPackageContentFailure) -> None:
        self.failure = failure
        super().__init__(failure.diagnostics[0])


class _Marker(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    edge: Literal["start", "end"]
    line: int
    character_offset: int


def _markdown_parser() -> MarkdownIt:
    return MarkdownIt("commonmark", {"linkify": True}).enable(["linkify"])


def _outside_fence_markers(body: str) -> tuple[_Marker, ...]:
    lines = body.splitlines(keepends=True)
    fenced_lines: set[int] = set()
    for token in _markdown_parser().parse(body):
        if token.type == "fence" and token.map is not None:
            fenced_lines.update(range(token.map[0], token.map[1]))
    pattern = re.compile(r"<!-- itree:(?P<name>[a-z-]+):(?P<edge>start|end) -->")
    markers: list[_Marker] = []
    character_offset = 0
    for index, line in enumerate(lines):
        match = pattern.fullmatch(line.rstrip("\r\n"))
        if match is not None and index not in fenced_lines:
            markers.append(
                _Marker(
                    name=match["name"],
                    edge="start" if match["edge"] == "start" else "end",
                    line=index + 1,
                    character_offset=character_offset,
                )
            )
        character_offset += len(line)
    return tuple(markers)


def _invalid_layout(body: str, error: SectionError) -> InvalidIssueLayout:
    return InvalidIssueLayout(body=body, errors=(error,))


def _validate_marker_order(
    body: str, markers: tuple[_Marker, ...]
) -> IssueLayout | None:
    section_names = {section.value for section in SectionName}
    for marker in markers:
        if marker.name not in section_names:
            return _invalid_layout(
                body, SectionError(name=marker.name, line=marker.line, reason="unknown")
            )
    duplicates = [
        key
        for key, count in Counter(
            (marker.name, marker.edge) for marker in markers
        ).items()
        if count > 1
    ]
    if duplicates:
        name, edge = duplicates[0]
        line = next(
            marker.line
            for marker in markers
            if (marker.name, marker.edge) == (name, edge)
            and marker.line
            > min(
                item.line for item in markers if (item.name, item.edge) == (name, edge)
            )
        )
        return _invalid_layout(
            body, SectionError(name=name, line=line, reason="duplicate")
        )
    open_marker: _Marker | None = None
    starts = Counter(marker.name for marker in markers if marker.edge == "start")
    ends = Counter(marker.name for marker in markers if marker.edge == "end")
    for marker in markers:
        if marker.edge == "start":
            if open_marker is not None:
                return _invalid_layout(
                    body,
                    SectionError(name=marker.name, line=marker.line, reason="nested"),
                )
            open_marker = marker
        elif open_marker is not None:
            if open_marker.name != marker.name:
                return _invalid_layout(
                    body,
                    SectionError(name=marker.name, line=marker.line, reason="nested"),
                )
            open_marker = None
    if open_marker is not None or starts != ends:
        marker = open_marker or next(
            marker for marker in markers if starts[marker.name] != ends[marker.name]
        )
        return _invalid_layout(
            body,
            SectionError(name=marker.name, line=marker.line, reason="unterminated"),
        )
    expected = tuple(
        marker
        for name in SectionName
        for marker in (
            _Marker(name=name.value, edge="start", line=0, character_offset=0),
            _Marker(name=name.value, edge="end", line=0, character_offset=0),
        )
    )
    observed = tuple((marker.name, marker.edge) for marker in markers)
    expected_pairs = tuple((marker.name, marker.edge) for marker in expected)
    if observed != expected_pairs:
        reason: Literal["partial", "reordered"] = (
            "partial" if len(markers) < len(expected) else "reordered"
        )
        marker = next(
            (
                marker
                for marker, expected_marker in zip(markers, expected, strict=False)
                if (marker.name, marker.edge)
                != (expected_marker.name, expected_marker.edge)
            ),
            markers[-1],
        )
        return _invalid_layout(
            body, SectionError(name=marker.name, line=marker.line, reason=reason)
        )
    return None


def parse_issue_layout(body: str) -> IssueLayout:
    markers = _outside_fence_markers(body)
    if not markers:
        return LegacyIssueLayout(
            body=body,
            original_contract=body,
            absent_sections=tuple(
                SectionAbsence(name=name, reason="legacy_zero_marker")
                for name in (
                    SectionName.requirements,
                    SectionName.plan,
                    SectionName.tasks,
                )
            ),
        )
    invalid = _validate_marker_order(body, markers)
    if invalid is not None:
        return invalid
    starts = {marker.name: marker for marker in markers if marker.edge == "start"}
    ends = {marker.name: marker for marker in markers if marker.edge == "end"}
    lines = body.splitlines(keepends=True)
    sections: list[IssueSection] = []
    for name in SectionName:
        start = starts[name.value]
        end = ends[name.value]
        content_start = start.character_offset + len(lines[start.line - 1])
        content_end = end.character_offset
        sections.append(
            IssueSection(
                name=name,
                line_start=start.line + 1,
                line_end=end.line,
                byte_start=len(body[:content_start].encode("utf-8")),
                byte_end=len(body[:content_end].encode("utf-8")),
                text=body[content_start:content_end],
            )
        )
    return ManagedIssueLayout(body=body, sections=tuple(sections))


def extract_citations(body_and_comments: Sequence[str]) -> tuple[Citation, ...]:
    raise NotImplementedError


def resolve_citation(api: GithubApi, citation: Citation) -> SourceDisposition:
    raise NotImplementedError


def _first_heading(body: str) -> str:
    return next(
        (line for line in body.splitlines() if line.startswith("#")), "(no heading)"
    )


def _append_issue(
    lines: list[str],
    issue_ref: IssueRef,
    issue: GithubIssue,
    comments: tuple[IssueComment, ...],
) -> None:
    body = issue.body or ""
    lines.extend(
        (
            f"### {issue_ref.slug}",
            f"title={issue.title}",
            f"state={issue.state.value}",
            f"url={issue.html_url}",
            body,
            "",
            "Comment index:",
        )
    )
    for comment in comments:
        lines.append(
            f"- id={comment.id} author={comment.author} created_at={comment.created_at} updated_at={comment.updated_at} first_heading={_first_heading(comment.body)} url={comment.html_url}"
        )
    for comment in comments:
        lines.extend((f"#### Comment id={comment.id}", comment.body, ""))


def _append_layout_index(lines: list[str], layout: ValidIssueLayout) -> None:
    lines.extend(
        (
            "## Section index",
            "kind=legacy" if isinstance(layout, LegacyIssueLayout) else "kind=managed",
        )
    )
    if isinstance(layout, LegacyIssueLayout):
        lines.append("- original-contract: entire body")
        for absence in layout.absent_sections:
            lines.append(f"- {absence.name.value}: absent reason={absence.reason}")
        return
    for section in layout.sections:
        lines.append(
            f"- {section.name.value}: line_start={section.line_start} line_end={section.line_end} byte_start={section.byte_start} byte_end={section.byte_end}"
        )


def render_readable_work_package(readable: ReadableWorkPackage) -> str:
    lines = [f"# Work package read: {readable.issue_ref.slug}", "## Selected issue"]
    _append_issue(lines, readable.issue_ref, readable.selected_issue, readable.comments)
    if readable.ancestors:
        lines.append("## Governing ancestors")
    for ancestor in readable.ancestors:
        lines.append(f"### Governing ancestor {ancestor.issue_ref.slug}")
        _append_issue(lines, ancestor.issue_ref, ancestor.issue, ancestor.comments)
    return "\n".join(lines).rstrip("\n") + "\n"


def render_work_package(package: WorkPackage) -> str:
    lines = [
        f"# Work package: {package.issue_ref.slug}",
        "Work package complete",
        "## Selected issue",
    ]
    _append_issue(lines, package.issue_ref, package.selected_issue, package.comments)
    _append_layout_index(lines, package.layout)
    if package.ancestors:
        lines.append("## Governing ancestors")
    for ancestor in package.ancestors:
        lines.append(f"### Governing ancestor {ancestor.issue_ref.slug}")
        _append_issue(lines, ancestor.issue_ref, ancestor.issue, ancestor.comments)
    lines.append("## Governing documents")
    lines.append("## Present cited sources")
    lines.append("## Unresolved citations")
    return "\n".join(lines).rstrip("\n") + "\n"


ReadResult = TypeVar("ReadResult")


def _required_read(
    issue_ref: IssueRef, operation: str, read: Callable[[], ReadResult]
) -> ReadResult:
    try:
        return read()
    except (RuntimeError, ValueError, TypeError, KeyError, AssertionError) as error:
        failure = WorkPackageReadFailure(
            issue_ref=issue_ref,
            operation=operation,
            diagnostic=f"{issue_ref.slug}: {operation}: {error}",
        )
        raise WorkPackageReadError(failure) from error


def _content_failure(failure: WorkPackageContentFailure) -> NoReturn:
    raise WorkPackageContentError(failure)


def load_work_package(issue_ref: IssueRef) -> WorkPackage:
    api = GithubApi.from_issue_ref(issue_ref)
    selected_issue = _required_read(
        issue_ref, "read selected issue", lambda: api.get_issue(issue_ref.number)
    )
    comments = _required_read(
        issue_ref,
        "read selected issue comments",
        lambda: api.list_comments(issue_ref.number),
    )
    layout = parse_issue_layout(selected_issue.body or "")
    readable = ReadableWorkPackage(
        issue_ref=issue_ref,
        selected_issue=selected_issue,
        comments=comments,
        layout=layout,
        ancestors=(),
    )
    if isinstance(layout, InvalidIssueLayout):
        diagnostics = tuple(
            f"invalid layout {error.reason} {error.name} at {issue_ref.slug}:{error.line}"
            for error in layout.errors
        )
        _content_failure(
            WorkPackageContentFailure(
                kind="invalid_layout", readable=readable, diagnostics=diagnostics
            )
        )
    ancestors: list[GoverningAncestor] = []
    chain = [issue_ref]
    current = issue_ref
    while True:
        current_api = GithubApi.from_issue_ref(current)
        parent = _required_read(
            issue_ref,
            f"read native parent of {current.slug}",
            lambda: current_api.get_parent(current.number),
        )
        if parent is None:
            break
        if parent in chain:
            cycle = (*chain[chain.index(parent) :], parent)
            path = " -> ".join(ref.slug for ref in cycle)
            partial = readable.model_copy(update={"ancestors": tuple(ancestors)})
            _content_failure(
                WorkPackageContentFailure(
                    kind="parent_cycle",
                    readable=partial,
                    diagnostics=(f"parent cycle: {path}",),
                )
            )
        parent_api = GithubApi.from_issue_ref(parent)
        parent_issue = _required_read(
            issue_ref,
            f"read ancestor {parent.slug}",
            lambda: parent_api.get_issue(parent.number),
        )
        parent_comments = _required_read(
            issue_ref,
            f"read ancestor comments {parent.slug}",
            lambda: parent_api.list_comments(parent.number),
        )
        ancestors.append(
            GoverningAncestor(
                issue_ref=parent, issue=parent_issue, comments=parent_comments
            )
        )
        chain.append(parent)
        current = parent
    return WorkPackage(
        issue_ref=issue_ref,
        selected_issue=selected_issue,
        comments=comments,
        layout=layout,
        ancestors=tuple(ancestors),
        governing_documents=(),
        present_sources=(),
        unresolved_sources=(),
    )
