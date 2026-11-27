from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Annotated, Literal

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


def parse_issue_layout(body: str) -> IssueLayout:
    raise NotImplementedError


def extract_citations(body_and_comments: Sequence[str]) -> tuple[Citation, ...]:
    raise NotImplementedError


def resolve_citation(api: GithubApi, citation: Citation) -> SourceDisposition:
    raise NotImplementedError


def render_work_package(package: WorkPackage) -> str:
    raise NotImplementedError


def render_readable_work_package(readable: ReadableWorkPackage) -> str:
    raise NotImplementedError


def load_work_package(issue_ref: IssueRef) -> WorkPackage:
    raise NotImplementedError
