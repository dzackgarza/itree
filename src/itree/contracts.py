"""Completion-contract declaration parser and typed obligation model.

Issues declare completion contracts using fenced ``itree-contract`` code
blocks in their body:

```markdown
```itree-contract
kind = "implementation"
origin = "dzackgarza/research#24"
owner = "dzackgarza/research#163"
evidence = "routes"
requires = ["dzackgarza/research#101"]
revalidate_on = ["dzackgarza/research#166"]
```
```

This module uses ``markdown-it-py`` to parse the markdown AST and extract
code blocks with ``info="itree-contract"``.  No regex is used for fence
detection — the markdown parser handles nesting, HTML comments, quoted
text, and edge cases correctly.

All functions are pure.
"""

from __future__ import annotations

import re
import tomllib

# ---------------------------------------------------------------------------
# Typed enums
# ---------------------------------------------------------------------------
from enum import StrEnum

from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict

from .models import IssueRef, RepoRef


class ObligationKind(StrEnum):
    implementation = "implementation"
    proof = "proof"
    research = "research"
    audit = "audit"
    coordination = "coordination"


class EvidenceDisposition(StrEnum):
    discharges = "discharges"
    routes = "routes"
    records = "records"
    narrows = "narrows"


# ---------------------------------------------------------------------------
# Typed models
# ---------------------------------------------------------------------------


class ContractDeclaration(BaseModel):
    """A parsed itree-contract block from an issue body."""

    model_config = ConfigDict(frozen=True)

    kind: ObligationKind
    origin: IssueRef | None = None
    owner: IssueRef | None = None
    evidence: EvidenceDisposition | None = None
    requires: tuple[IssueRef, ...] = ()
    revalidate_on: tuple[IssueRef, ...] = ()


class OwnershipChain(BaseModel):
    """A recursive ownership path from an origin to a current owner."""

    model_config = ConfigDict(frozen=True)

    origin: IssueRef
    path: tuple[IssueRef, ...]
    current_owner: IssueRef
    unresolved: bool = True
    cycle_detected: bool = False


# ---------------------------------------------------------------------------
# Markdown AST extraction
# ---------------------------------------------------------------------------

_md = MarkdownIt("commonmark", {"html": True})


def _extract_contract_blocks(body: str) -> list[str]:
    """Extract content from itree-contract fenced code blocks via markdown AST.

    Walks the parsed token stream and collects the content of
    ``fence`` tokens whose ``info`` string is ``itree-contract``.
    This correctly handles nesting, HTML comments, and edge cases
    that regex-based extraction cannot.
    """
    tokens = _md.parse(body)
    blocks: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.type == "fence" and token.info.strip() == "itree-contract":
            blocks.append(token.content)
        i += 1
    return blocks


# ---------------------------------------------------------------------------
# Reference parsing (qualified + unqualified)
# ---------------------------------------------------------------------------

_QUALIFIED_REF_RE = re.compile(
    r"(?P<owner>[^/\s#]+)/(?P<repo>[^/\s#]+)#(?P<number>[1-9]\d*)"
)
_UNQUALIFIED_REF_RE = re.compile(r"(?<![/&\w])#(?P<number>[1-9]\d*)")


def _parse_optional_ref(raw: object) -> IssueRef | None:
    """Parse an IssueRef from raw TOML value, or None if invalid."""
    if not isinstance(raw, str):
        return None
    m = re.fullmatch(
        r"(?P<owner>[^/\s#]+)/(?P<repo>[^/\s#]+)#(?P<number>[1-9]\d*)",
        raw,
    )
    if not m:
        return None
    return IssueRef(
        repo_ref=RepoRef(owner=m["owner"], repo=m["repo"]),
        number=int(m["number"]),
    )


def _parse_ref_list(raw: object) -> tuple[IssueRef, ...]:
    """Parse a list of IssueRefs from raw TOML value."""
    if not isinstance(raw, list):
        return ()
    refs: list[IssueRef] = []
    for item in raw:
        ref = _parse_optional_ref(item)
        if ref is not None:
            refs.append(ref)
    return tuple(refs)


def _parse_toml(content: str) -> dict:
    """Parse TOML content from an itree-contract block."""
    return tomllib.loads(content)


def parse_qualified_refs(
    body: str | None,
    repo_ref: RepoRef,
) -> list[IssueRef]:
    """Parse both qualified and unqualified issue references from a body.

    Uses the markdown AST to extract only text from non-code-block tokens,
    so references inside fenced code (including itree-contract blocks) are
    not parsed as ordinary references.

    Qualified refs (``OWNER/REPO#N``) preserve their repository identity.
    Unqualified refs (``#N``) are resolved against ``repo_ref``.

    References are deduplicated, preserving order of first appearance.
    """
    if not body:
        return []

    # Parse markdown and extract text from non-code tokens only
    tokens = _md.parse(body)
    text_parts: list[str] = []
    for token in tokens:
        if token.type in ("fence", "code_block", "code_inline"):
            continue
        if token.type == "inline" and token.content:
            text_parts.append(token.content)

    text = " ".join(text_parts)

    seen: set[tuple[str, int]] = set()
    refs: list[IssueRef] = []

    for m in _QUALIFIED_REF_RE.finditer(text):
        owner = m.group("owner")
        repo = m.group("repo")
        number = int(m.group("number"))
        key = (f"{owner}/{repo}", number)
        if key not in seen:
            seen.add(key)
            refs.append(
                IssueRef(repo_ref=RepoRef(owner=owner, repo=repo), number=number)
            )

    qualified_positions = {
        m.start("number") - 1 for m in _QUALIFIED_REF_RE.finditer(text)
    }

    for m in _UNQUALIFIED_REF_RE.finditer(text):
        if m.start() in qualified_positions:
            continue
        number = int(m.group("number"))
        key = (repo_ref.slug, number)
        if key not in seen:
            seen.add(key)
            refs.append(IssueRef(repo_ref=repo_ref, number=number))

    return refs


# ---------------------------------------------------------------------------
# Contract declaration parsing
# ---------------------------------------------------------------------------


def parse_contract_declarations(body: str | None) -> list[ContractDeclaration]:
    """Find and parse all ``itree-contract`` blocks in an issue body.

    Uses the markdown AST to find fenced code blocks with
    ``info="itree-contract"``.  Returns an empty list if body is None
    or contains no itree-contract blocks.
    """
    if not body:
        return []

    blocks = _extract_contract_blocks(body)
    declarations: list[ContractDeclaration] = []

    for content in blocks:
        if "=" not in content:
            continue
        data = _parse_toml(content)

        if "kind" not in data:
            continue
        kind_raw = data.pop("kind")
        if kind_raw not in ObligationKind._value2member_map_:
            continue
        kind = ObligationKind(kind_raw)

        origin = _parse_optional_ref(data.pop("origin", None))
        owner = _parse_optional_ref(data.pop("owner", None))

        evidence: EvidenceDisposition | None = None
        if "evidence" in data:
            ev_raw = data.pop("evidence")
            if ev_raw in EvidenceDisposition._value2member_map_:
                evidence = EvidenceDisposition(ev_raw)

        requires = _parse_ref_list(data.pop("requires", None))
        revalidate_on = _parse_ref_list(data.pop("revalidate_on", None))

        declarations.append(
            ContractDeclaration(
                kind=kind,
                origin=origin,
                owner=owner,
                evidence=evidence,
                requires=requires,
                revalidate_on=revalidate_on,
            )
        )

    return declarations


# ---------------------------------------------------------------------------
# Ownership chain traversal
# ---------------------------------------------------------------------------


def _resolve_local_number(ref: IssueRef, repo_ref: RepoRef) -> int | None:
    """Resolve an IssueRef to a local issue number if it belongs to repo_ref."""
    if ref.repo_ref == repo_ref:
        return ref.number
    return None


def build_ownership_chains(
    declarations_by_issue: dict[int, list[ContractDeclaration]],
    repo_ref: RepoRef,
) -> list[OwnershipChain]:
    """Build recursive ownership chains from contract declarations.

    For each declaration with ``kind="implementation"`` and evidence in
    ``("routes", "records", "narrows")``, follow the owner chain recursively.

    Only originating declarations start a chain: those where ``origin`` is
    None (the issue introduced the obligation) or ``origin`` points to the
    issue itself.  Declarations with an external origin are routing hops.

    A chain is unresolved if evidence disposition is not ``discharges``.

    Cycles are detected and reported with ``cycle_detected=True``.
    """
    chains: list[OwnershipChain] = []

    issue_numbers_by_slug: dict[str, int] = {}
    for issue_num in declarations_by_issue:
        slug = f"{repo_ref.slug}#{issue_num}"
        issue_numbers_by_slug[slug] = issue_num

    # Build a set of issue numbers that are owned by another declaration.
    owned_issues: set[int] = set()
    for decls in declarations_by_issue.values():
        for d in decls:
            if d.owner is not None:
                owner_local = _resolve_local_number(d.owner, repo_ref)
                if owner_local is not None:
                    owned_issues.add(owner_local)

    # Detect actual cycles: follow the ownership chain from each owned
    # issue and check if we return to the starting issue.
    cycle_starts: set[int] = set()
    for start_issue in owned_issues:
        if start_issue not in declarations_by_issue:
            continue
        cycle_visited: set[int] = set()
        current_node: int | None = start_issue
        while current_node is not None and current_node not in cycle_visited:
            cycle_visited.add(current_node)
            if current_node not in declarations_by_issue:
                break
            next_owner_num: int | None = None
            for d in declarations_by_issue[current_node]:
                if d.kind == ObligationKind.implementation and d.owner is not None:
                    next_local = _resolve_local_number(d.owner, repo_ref)
                    if next_local is not None:
                        next_owner_num = next_local
                        break
            current_node = next_owner_num
        if current_node == start_issue and len(cycle_visited) > 1:
            cycle_starts.add(min(cycle_visited))

    for issue_num, decls in sorted(declarations_by_issue.items()):
        for decl in decls:
            if decl.kind != ObligationKind.implementation:
                continue
            if (
                decl.evidence is not None
                and decl.evidence == EvidenceDisposition.discharges
            ):
                continue
            # Only start chains from issues not owned by another, OR
            # from the designated start of a cycle.
            if issue_num in owned_issues and issue_num not in cycle_starts:
                continue

            origin_ref = IssueRef(repo_ref=repo_ref, number=issue_num)

            if decl.owner is None:
                continue

            path: list[IssueRef] = [origin_ref]
            visited: set[str] = {origin_ref.slug}
            current_owner = decl.owner
            cycle = False

            while current_owner is not None:
                if current_owner.slug in visited:
                    cycle = True
                    break
                visited.add(current_owner.slug)
                path.append(current_owner)

                if current_owner.slug not in issue_numbers_by_slug:
                    break
                owner_issue_num = issue_numbers_by_slug[current_owner.slug]
                if owner_issue_num in declarations_by_issue:
                    owner_decls = declarations_by_issue[owner_issue_num]
                else:
                    owner_decls = []
                next_owner = None
                for od in owner_decls:
                    if (
                        od.kind == ObligationKind.implementation
                        and od.owner is not None
                    ):
                        if (
                            od.evidence is None
                            or od.evidence != EvidenceDisposition.discharges
                        ):
                            next_owner = od.owner
                            break
                if next_owner is None:
                    break
                current_owner = next_owner

            chains.append(
                OwnershipChain(
                    origin=origin_ref,
                    path=tuple(path),
                    current_owner=path[-1],
                    unresolved=True,
                    cycle_detected=cycle,
                )
            )

    return chains
