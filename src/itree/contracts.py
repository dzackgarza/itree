"""Strict completion-contract declarations embedded in issue bodies."""

from __future__ import annotations

import re
import tomllib
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from .models import IssueNumber, IssueRef, RepoRef


class ContractKind(StrEnum):
    implementation = "implementation"
    proof = "proof"
    research = "research"
    audit = "audit"
    coordination = "coordination"


class ContractEvidence(StrEnum):
    discharges = "discharges"
    routes = "routes"
    records = "records"
    narrows = "narrows"


class ContractRole(StrEnum):
    grouping = "grouping"
    work_unit = "work_unit"


class ContractDecomposition(StrEnum):
    complete = "complete"
    partial = "partial"


class ContractCompletion(StrEnum):
    completed = "completed"


class ContractDeclaration(BaseModel):
    """Typed declaration parsed from one ``itree-contract`` fenced block."""

    model_config = ConfigDict(frozen=True)

    issue: IssueRef
    block_index: int
    line: int
    kind: ContractKind
    evidence: ContractEvidence
    origin: IssueRef | None = None
    owner: IssueRef | None = None
    requires: tuple[IssueRef, ...] = ()
    revalidate_on: tuple[IssueRef, ...] = ()
    role: ContractRole | None = None
    decomposition: ContractDecomposition | None = None
    completion: ContractCompletion | None = None


class ContractParseError(BaseModel):
    """A malformed contract block that doctor must report as E018."""

    model_config = ConfigDict(frozen=True)

    issue: IssueRef
    line: int
    message: str


class ParsedIssueContracts(BaseModel):
    """All contract declarations and parse errors from one issue body."""

    model_config = ConfigDict(frozen=True)

    declarations: tuple[ContractDeclaration, ...] = ()
    errors: tuple[ContractParseError, ...] = ()


class _RawContractBlock(BaseModel):
    """Boundary schema before textual refs are resolved to ``IssueRef``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ContractKind
    evidence: ContractEvidence
    origin: str | None = None
    owner: str | None = None
    requires: tuple[str, ...] = ()
    revalidate_on: tuple[str, ...] = ()
    role: ContractRole | None = None
    decomposition: ContractDecomposition | None = None
    completion: ContractCompletion | None = None

    @field_validator("origin", "owner", mode="before")
    @classmethod
    def _optional_ref_is_text(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        raise ValueError("issue ref must be a string")

    @field_validator("requires", "revalidate_on", mode="before")
    @classmethod
    def _ref_list_is_text_sequence(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, list | tuple):
            raise ValueError("issue ref list must be an array of strings")
        refs: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("issue ref list entries must be strings")
            refs.append(item)
        return tuple(refs)

    @model_validator(mode="after")
    def _discharge_names_origin(self) -> Self:
        if self.evidence == ContractEvidence.discharges and self.origin is None:
            raise ValueError("discharge evidence must name the originating obligation")
        return self

    @model_validator(mode="after")
    def _implementation_route_names_owner(self) -> Self:
        if self.kind == ContractKind.implementation and self.evidence == ContractEvidence.routes and self.owner is None:
            raise ValueError("implementation route evidence must name the downstream owner")
        return self


_OPENING_FENCE_RE = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})(?P<info>[^\n]*)$")


def parse_issue_contracts(body: str | None, *, repo_ref: RepoRef, issue_number: IssueNumber) -> ParsedIssueContracts:
    """Parse strict fenced TOML contract declarations from one issue body.

    Only fences whose info string is exactly ``itree-contract`` are parsed.
    Ordinary prose issue references outside those fences remain ordinary text.
    """
    issue_ref = IssueRef(repo_ref=repo_ref, number=issue_number)
    if not body:
        return ParsedIssueContracts()

    declarations: list[ContractDeclaration] = []
    errors: list[ContractParseError] = []
    lines = body.splitlines()
    index = 0
    block_index = 0
    while index < len(lines):
        line = lines[index]
        opening = _OPENING_FENCE_RE.match(line)
        if opening is None:
            index += 1
            continue

        fence = opening["fence"]
        info_string = opening["info"].strip()
        closing_re = re.compile(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$")
        start_line = index + 1
        content: list[str] = []
        index += 1
        while index < len(lines) and closing_re.match(lines[index]) is None:
            content.append(lines[index])
            index += 1

        if index == len(lines):
            if info_string == "itree-contract":
                errors.append(
                    ContractParseError(
                        issue=issue_ref,
                        line=start_line,
                        message="unterminated itree-contract fence",
                    )
                )
            break

        if info_string != "itree-contract":
            index += 1
            continue

        block_index += 1
        declaration = _parse_contract_block(
            "\n".join(content),
            issue_ref=issue_ref,
            block_index=block_index,
            line=start_line,
        )
        if isinstance(declaration, ContractParseError):
            errors.append(declaration)
        else:
            declarations.append(declaration)
        index += 1

    return ParsedIssueContracts(declarations=tuple(declarations), errors=tuple(errors))


def _parse_contract_block(
    raw_toml: str,
    *,
    issue_ref: IssueRef,
    block_index: int,
    line: int,
) -> ContractDeclaration | ContractParseError:
    try:
        data = tomllib.loads(raw_toml)
        block = _RawContractBlock.model_validate(data)
        return ContractDeclaration(
            issue=issue_ref,
            block_index=block_index,
            line=line,
            kind=block.kind,
            evidence=block.evidence,
            origin=_parse_ref(block.origin, issue_ref.repo_ref) if block.origin else None,
            owner=_parse_ref(block.owner, issue_ref.repo_ref) if block.owner else None,
            requires=tuple(_parse_ref(raw, issue_ref.repo_ref) for raw in block.requires),
            revalidate_on=tuple(_parse_ref(raw, issue_ref.repo_ref) for raw in block.revalidate_on),
            role=block.role,
            decomposition=block.decomposition,
            completion=block.completion,
        )
    except (tomllib.TOMLDecodeError, ValidationError, ValueError) as exc:
        return ContractParseError(
            issue=issue_ref,
            line=line,
            message=_summarize_parse_error(exc),
        )


def _parse_ref(raw: str, repo_ref: RepoRef) -> IssueRef:
    if re.fullmatch(r"#[1-9]\d*", raw):
        return IssueRef(repo_ref=repo_ref, number=int(raw[1:]))
    return IssueRef.parse(raw)


def _summarize_parse_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        errors: list[str] = []
        for error in exc.errors(include_url=False):
            loc = ".".join(str(part) for part in error["loc"]) or "contract"
            errors.append(f"{loc}: {error['msg']}")
        return "; ".join(errors)
    return str(exc)
