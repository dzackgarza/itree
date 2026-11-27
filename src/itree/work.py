from __future__ import annotations

import base64
import json
import http.client
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable, Sequence
from enum import StrEnum
from typing import Annotated, Literal, NoReturn, TypeVar

from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .github import GitRefLookupError, GithubApi, GithubApiError
from .models import (
    CanonicalSourceUrl,
    CommitSha,
    GitRefResolution,
    GithubContentFile,
    GithubIssue,
    GithubNonFileContent,
    IssueComment,
    MissingGithubContent,
    IssueRef,
    RepoRef,
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


SourceContentKind = Literal[
    "github_issue", "github_comment", "github_blob", "markdown", "plain_text", "html"
]
UnresolvedReason = Literal[
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
RepositoryTextFailure = Literal[
    "binary", "invalid_text_encoding", "unsupported_kind", "invalid"
]


class WorkContentIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["issue", "issue_comment"]
    issue_ref: IssueRef
    issue_comment_id: int | None = None


class PresentSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["present"] = "present"
    citation: Citation
    canonical_url: CanonicalSourceUrl
    source_kind: SourceContentKind
    content: str
    requested_url: str
    final_url: str
    media_type: str
    resolution: GitRefResolution | None = None
    sub_anchor: str | None = None
    content_identity: WorkContentIdentity | None = None


class UnresolvedSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["unresolved"] = "unresolved"
    citation: Citation
    reason: UnresolvedReason


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


def _canonical_url(raw: str) -> str:
    if raw.startswith("#"):
        return raw
    split = urllib.parse.urlsplit(raw)
    scheme = split.scheme.lower()
    if scheme not in ("http", "https") or split.hostname is None:
        raise ValueError(f"invalid HTTP(S) citation: {raw!r}")
    hostname = split.hostname.lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    port = split.port
    default_port = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    port_text = "" if port is None or default_port else f":{port}"
    userinfo, separator, _ = split.netloc.rpartition("@")
    userinfo_text = f"{userinfo}@" if separator else ""
    netloc = f"{userinfo_text}{hostname}{port_text}"
    canonical = urllib.parse.urlunsplit(
        (scheme, netloc, split.path, split.query, split.fragment)
    )
    empty_query = split.query == "" and "?" in raw.partition("#")[0]
    empty_fragment = split.fragment == "" and "#" in raw
    return canonical + ("?" if empty_query else "") + ("#" if empty_fragment else "")


def _new_citation(raw: str) -> Citation:
    try:
        canonical = _canonical_url(raw)
    except ValueError:
        canonical = raw
    return Citation(citation=raw, canonical_url=canonical, aliases=(raw,))


def extract_citations(body_and_comments: Sequence[str]) -> tuple[Citation, ...]:
    ordered: list[Citation] = []
    by_canonical: dict[str, int] = {}
    for document in body_and_comments:
        for token in _markdown_parser().parse(document):
            for child in token.children or ():
                raw_values: list[str] = []
                if child.type == "link_open":
                    href = child.attrs.get("href")
                    if isinstance(href, str) and href.lower().startswith(
                        ("http://", "https://")
                    ):
                        raw_values.append(href)
                if child.type == "text":
                    raw_values.extend(
                        "#" + match.group("number")
                        for match in re.finditer(
                            r"(?<![\w/#])#(?P<number>[1-9]\d*)\b", child.content
                        )
                    )
                for raw in raw_values:
                    citation = _new_citation(raw)
                    canonical = str(citation.canonical_url)
                    existing_index = by_canonical.get(canonical)
                    if existing_index is None:
                        by_canonical[canonical] = len(ordered)
                        ordered.append(citation)
                        continue
                    existing = ordered[existing_index]
                    if raw not in existing.aliases:
                        ordered[existing_index] = existing.model_copy(
                            update={"aliases": (*existing.aliases, raw)}
                        )
    return tuple(ordered)


def _unresolved(citation: Citation, reason: UnresolvedReason) -> UnresolvedSource:
    return UnresolvedSource(citation=citation, reason=reason)


def _present(
    citation: Citation,
    source_kind: SourceContentKind,
    content: str,
    media_type: str,
    resolution: GitRefResolution | None = None,
    sub_anchor: str | None = None,
    content_identity: WorkContentIdentity | None = None,
) -> PresentSource:
    return PresentSource(
        citation=citation,
        canonical_url=citation.canonical_url,
        source_kind=source_kind,
        content=content,
        requested_url=citation.citation,
        final_url=str(citation.canonical_url),
        media_type=media_type,
        resolution=resolution,
        sub_anchor=sub_anchor,
        content_identity=content_identity,
    )


def _decode_repository_file(
    api: GithubApi, document: GithubContentFile
) -> str | RepositoryTextFailure:
    if document.encoding == "none":
        raw = api.get_git_blob(document.sha)
    elif document.content is None:
        return "invalid"
    else:
        raw = base64.b64decode(document.content)
    if b"\x00" in raw:
        return "binary"
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return "invalid_text_encoding"


def _github_source_error(error: GithubApiError) -> Literal["not_found", "inaccessible"]:
    return "not_found" if error.status == 404 else "inaccessible"


def _resolve_github_issue(
    api: GithubApi,
    citation: Citation,
    number: int,
    comment_id: int | None,
    sub_anchor: str | None,
) -> SourceDisposition:
    try:
        issue = api.get_issue(number)
        if issue.is_pull_request:
            return _unresolved(citation, "unsupported_kind")
        if comment_id is not None:
            comment = api.get_comment(comment_id)
            identity = WorkContentIdentity(
                kind="issue_comment",
                issue_ref=IssueRef(repo_ref=api.repo_ref, number=issue.number),
                issue_comment_id=comment.id,
            )
            return _present(
                citation,
                "github_comment",
                comment.body,
                "text/markdown",
                sub_anchor=sub_anchor,
                content_identity=identity,
            )
        identity = WorkContentIdentity(
            kind="issue",
            issue_ref=IssueRef(repo_ref=api.repo_ref, number=issue.number),
        )
        return _present(
            citation,
            "github_issue",
            issue.body or "",
            "text/markdown",
            sub_anchor=sub_anchor,
            content_identity=identity,
        )
    except GithubApiError as error:
        return _unresolved(citation, _github_source_error(error))


def _resolve_github_blob(
    api: GithubApi, citation: Citation, segments: list[str]
) -> SourceDisposition:
    ref_and_path = "/".join(segments[3:])
    first_ref = segments[3]
    try:
        if re.fullmatch(r"[0-9a-f]{40}", first_ref):
            resolution = api.resolve_git_ref(first_ref, "/".join(segments[4:]))
        else:
            resolution = api.resolve_git_ref(ref_and_path, "")
        document = api.get_repository_content(
            resolution.repository_path, resolution.resolved_commit_sha
        )
    except GitRefLookupError as error:
        return _unresolved(citation, error.reason)
    except GithubApiError as error:
        return _unresolved(citation, _github_source_error(error))
    if isinstance(document, MissingGithubContent):
        return _unresolved(citation, "not_found")
    if isinstance(document, tuple) or isinstance(document, GithubNonFileContent):
        return _unresolved(citation, "unsupported_kind")
    content_or_reason = _decode_repository_file(api, document)
    if content_or_reason == "binary":
        return _unresolved(citation, "binary")
    if content_or_reason == "invalid_text_encoding":
        return _unresolved(citation, "invalid_text_encoding")
    if content_or_reason == "unsupported_kind":
        return _unresolved(citation, "unsupported_kind")
    if content_or_reason == "invalid":
        return _unresolved(citation, "invalid")
    media_type = (
        "text/markdown"
        if document.path.lower().endswith((".md", ".markdown"))
        else "text/plain"
    )
    return _present(citation, "github_blob", content_or_reason, media_type, resolution)


def _external_content_kind(
    media_type: str,
) -> Literal["markdown", "plain_text", "html"] | None:
    if media_type in ("text/markdown", "text/x-markdown"):
        return "markdown"
    if media_type == "text/plain":
        return "plain_text"
    if media_type == "text/html":
        return "html"
    return None


def _resolve_external(citation: Citation) -> SourceDisposition:
    try:
        request = urllib.request.Request(citation.citation, method="GET")
        response = urllib.request.urlopen(request, timeout=60)
        with response:
            payload = response.read()
            final_url = response.geturl()
            media_type = response.headers.get_content_type()
            charset = response.headers.get_content_charset()
    except urllib.error.HTTPError as error:
        final_url = error.geturl()
        redirected = 300 <= error.code < 400 or _canonical_url(
            final_url
        ) != _canonical_url(citation.citation)
        return _unresolved(
            citation, "redirect_failed" if redirected else "inaccessible"
        )
    except http.client.HTTPException, urllib.error.URLError, TimeoutError, OSError:
        return _unresolved(citation, "inaccessible")
    except ValueError:
        return _unresolved(citation, "invalid")
    if media_type == "application/octet-stream":
        return _unresolved(citation, "binary")
    kind = _external_content_kind(media_type)
    if kind is None:
        return _unresolved(citation, "unsupported_media_type")
    if b"\x00" in payload:
        return _unresolved(citation, "binary")
    if charset is not None and charset.lower() not in ("utf-8", "utf8"):
        return _unresolved(citation, "invalid_text_encoding")
    try:
        content = payload.decode("utf-8")
    except UnicodeDecodeError:
        return _unresolved(citation, "invalid_text_encoding")
    return _present(citation, kind, content, media_type).model_copy(
        update={"final_url": final_url}
    )


def _resolve_citation(api: GithubApi, citation: Citation) -> SourceDisposition:
    if str(citation.canonical_url).startswith("#"):
        return _unresolved(citation, "bare_reference")
    try:
        split = urllib.parse.urlsplit(str(citation.canonical_url))
        if split.scheme not in ("http", "https") or split.hostname is None:
            return _unresolved(citation, "invalid")
    except ValueError:
        return _unresolved(citation, "invalid")
    if split.hostname.lower() != "github.com":
        return _resolve_external(citation)
    segments = [
        urllib.parse.unquote(segment) for segment in split.path.split("/") if segment
    ]
    if len(segments) < 3:
        return _unresolved(citation, "unsupported_kind")
    target_repo = RepoRef(owner=segments[0], repo=segments[1])
    if api.repo_ref.slug.casefold() != target_repo.slug.casefold():
        api = GithubApi.from_repo_ref(target_repo)
    if len(segments) == 4 and segments[2] == "issues" and segments[3].isdigit():
        anchor = re.fullmatch(r"issuecomment-(\d+)(?:#(.*))?", split.fragment)
        if split.fragment and anchor is None:
            return _unresolved(citation, "invalid")
        comment_id = int(anchor[1]) if anchor is not None else None
        sub_anchor = anchor[2] if anchor is not None and anchor[2] else None
        return _resolve_github_issue(
            api, citation, int(segments[3]), comment_id, sub_anchor
        )
    if len(segments) >= 5 and segments[2] == "blob":
        return _resolve_github_blob(api, citation, segments)
    return _unresolved(citation, "unsupported_kind")


def resolve_citation(api: GithubApi, citation: Citation) -> SourceDisposition:
    try:
        return _resolve_citation(api, citation)
    except GitRefLookupError as error:
        return _unresolved(citation, error.reason)
    except GithubApiError as error:
        return _unresolved(citation, _github_source_error(error))
    except ValidationError, TypeError, ValueError, KeyError:
        return _unresolved(citation, "invalid")
    except OSError, TimeoutError:
        return _unresolved(citation, "inaccessible")
    except RuntimeError:
        return _unresolved(citation, "inaccessible")


def _first_heading(body: str) -> str:
    return next(
        (line for line in body.splitlines() if line.startswith("#")), "(no heading)"
    )


def _append_issue_body(
    lines: list[str], issue_ref: IssueRef, issue: GithubIssue
) -> None:
    lines.extend(
        (
            f"### {issue_ref.slug}",
            f"title={issue.title}",
            f"state={issue.state.value}",
            f"url={issue.html_url}",
            issue.body or "",
            "",
        )
    )


def _append_issue_comments(
    lines: list[str], comments: tuple[IssueComment, ...]
) -> None:
    lines.append("Comment index:")
    for comment in comments:
        lines.append(
            f"- id={comment.id} author={comment.author} created_at={comment.created_at} updated_at={comment.updated_at} first_heading={_first_heading(comment.body)} url={comment.html_url}"
        )
    for comment in comments:
        lines.extend((f"#### Comment id={comment.id}", comment.body, ""))


def _append_issue(
    lines: list[str],
    issue_ref: IssueRef,
    issue: GithubIssue,
    comments: tuple[IssueComment, ...],
) -> None:
    _append_issue_body(lines, issue_ref, issue)
    _append_issue_comments(lines, comments)


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
    _append_issue_body(lines, package.issue_ref, package.selected_issue)
    _append_layout_index(lines, package.layout)
    _append_issue_comments(lines, package.comments)
    if package.ancestors:
        lines.append("## Governing ancestors")
    for ancestor in package.ancestors:
        lines.append(f"### Governing ancestor {ancestor.issue_ref.slug}")
        _append_issue(lines, ancestor.issue_ref, ancestor.issue, ancestor.comments)
    emitted_identities = {
        WorkContentIdentity(kind="issue", issue_ref=package.issue_ref),
        *(
            WorkContentIdentity(kind="issue", issue_ref=ancestor.issue_ref)
            for ancestor in package.ancestors
        ),
    }
    emitted_identities.update(
        WorkContentIdentity(
            kind="issue_comment",
            issue_ref=package.issue_ref,
            issue_comment_id=comment.id,
        )
        for comment in package.comments
    )
    for ancestor in package.ancestors:
        emitted_identities.update(
            WorkContentIdentity(
                kind="issue_comment",
                issue_ref=ancestor.issue_ref,
                issue_comment_id=comment.id,
            )
            for comment in ancestor.comments
        )
    anchored_identities = {
        source.content_identity
        for source in package.present_sources
        if source.sub_anchor is not None and source.content_identity is not None
    }
    lines.append("## Governing documents")
    for document in package.governing_documents:
        lines.append(f"### {document.role.value}")
        lines.append(f"path={document.path}")
        if isinstance(document, AbsentGoverningDocument):
            lines.append(f"kind=absent reason={document.absence_reason}")
            continue
        lines.append(f"kind=present resolved_commit_sha={document.resolved_commit_sha}")
        lines.append(document.content)
    lines.append("## Present cited sources")
    for source in package.present_sources:
        lines.append(f"### Source {source.canonical_url}")
        lines.append(f"citation={source.citation.citation}")
        lines.append(f"canonical_url={source.canonical_url}")
        lines.append(f"aliases={list(source.citation.aliases)}")
        lines.append(f"source_kind={source.source_kind}")
        lines.append(f"requested_url={source.requested_url}")
        lines.append(f"final_url={source.final_url}")
        lines.append(f"media_type={source.media_type}")
        if source.sub_anchor is not None:
            lines.append(f"sub_anchor={source.sub_anchor}")
        if source.resolution is not None:
            lines.append(f"requested_ref={source.resolution.requested_ref}")
            lines.append(f"resolved_commit_sha={source.resolution.resolved_commit_sha}")
            lines.append(f"repository_path={source.resolution.repository_path}")
            for ref in source.resolution.matched_refs:
                lines.append(f"matched_ref={ref.name} kind={ref.kind.value}")
        anchor_sibling = (
            source.content_identity in anchored_identities and source.sub_anchor is None
        )
        if source.content_identity is not None and (
            source.content_identity in emitted_identities or anchor_sibling
        ):
            lines.append("emission=deduplicated")
            continue
        if source.content_identity is not None:
            emitted_identities.add(source.content_identity)
        lines.append("emission=content")
        lines.append(source.content)
    lines.append("## Unresolved citations")
    for unresolved in package.unresolved_sources:
        lines.append(f"### Unresolved {unresolved.citation.canonical_url}")
        lines.append(f"citation={unresolved.citation.citation}")
        lines.append(f"reason={unresolved.reason}")
    return "\n".join(lines).rstrip("\n") + "\n"


ReadResult = TypeVar("ReadResult")


def _required_read(
    issue_ref: IssueRef, operation: str, read: Callable[[], ReadResult]
) -> ReadResult:
    try:
        return read()
    except (
        RuntimeError,
        ValidationError,
        json.JSONDecodeError,
        TypeError,
        KeyError,
    ) as error:
        failure = WorkPackageReadFailure(
            issue_ref=issue_ref,
            operation=operation,
            diagnostic=f"{issue_ref.slug}: {operation}: {error}",
        )
        raise WorkPackageReadError(failure) from error


def _content_failure(failure: WorkPackageContentFailure) -> NoReturn:
    raise WorkPackageContentError(failure)


def _load_governing_documents(api: GithubApi) -> tuple[GoverningDocument, ...]:
    resolution = api.resolve_default_branch()
    documents: list[GoverningDocument] = []
    for role in GoverningDocumentRole:
        path = role.value
        result = api.get_repository_content(path, resolution.resolved_commit_sha)
        if isinstance(result, MissingGithubContent):
            documents.append(
                AbsentGoverningDocument(
                    role=role, path=path, absence_reason="not_found"
                )
            )
            continue
        if isinstance(result, tuple) or isinstance(result, GithubNonFileContent):
            raise RuntimeError(
                f"governing document {path} resolved to a non-file response"
            )
        content_or_reason = _decode_repository_file(api, result)
        if content_or_reason in (
            "binary",
            "invalid_text_encoding",
            "unsupported_kind",
            "invalid",
        ):
            raise RuntimeError(
                f"governing document {path} could not be read as UTF-8 text: {content_or_reason}"
            )
        documents.append(
            PresentGoverningDocument(
                role=role,
                path=path,
                resolved_commit_sha=resolution.resolved_commit_sha,
                content=content_or_reason,
            )
        )
    return tuple(documents)


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
    governing_documents = _required_read(
        issue_ref, "read governing documents", lambda: _load_governing_documents(api)
    )
    citations = extract_citations(
        (selected_issue.body or "", *(comment.body for comment in comments))
    )
    with ThreadPoolExecutor(max_workers=8) as executor:
        dispositions = tuple(
            executor.map(lambda citation: resolve_citation(api, citation), citations)
        )
    return WorkPackage(
        issue_ref=issue_ref,
        selected_issue=selected_issue,
        comments=comments,
        layout=layout,
        ancestors=tuple(ancestors),
        governing_documents=governing_documents,
        present_sources=tuple(
            source for source in dispositions if isinstance(source, PresentSource)
        ),
        unresolved_sources=tuple(
            source for source in dispositions if isinstance(source, UnresolvedSource)
        ),
    )
