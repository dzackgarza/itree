"""Completion-contract audit for itree doctor (itree#41).

Detects when a structurally valid tree still permits an agent to close
administrative slices while the implementation named by the original
contract never occurs.

The audit is deterministic, builds on #40's readiness model, and uses
explicit ``itree-contract`` declarations parsed from issue bodies.
Unstructured prose may produce a Q advisory ("possible undeclared
transfer"), but never a definitive error.

All functions are pure: they consume an already-built ``RepoDag`` and
never touch the network.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .contracts import (
    ContractDeclaration,
    ObligationKind,
    build_ownership_chains,
    parse_contract_declarations,
)
from .models import IssueRef, RepoDag, RepoRef
from .predicates import is_grouping_issue

# ---------------------------------------------------------------------------
# Intermediate result type
# ---------------------------------------------------------------------------


class CompletionFinding(BaseModel):
    """Intermediate audit result converted to Finding by generate_doctor_report."""

    model_config = ConfigDict(frozen=True)

    code: str
    issue_numbers: tuple[int, ...]
    evidence: tuple[str, ...]
    # Typed witness fields
    origin: IssueRef | None = None
    owner: IssueRef | None = None
    path: tuple[IssueRef, ...] = ()
    obligation_kind: str | None = None
    evidence_disposition: str | None = None
    unresolved_burden: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _children_of(dag: RepoDag, number: int) -> tuple[int, ...]:
    """Return children of ``number`` or empty tuple."""
    if number in dag.children_of:
        return dag.children_of[number]
    return ()


def _has_implementation_descendants(
    dag: RepoDag,
    declarations_by_issue: dict[int, list[ContractDeclaration]],
    grouping_number: int,
) -> bool:
    """Check if a grouping has any descendants that own implementation obligations.

    A descendant counts if it has a contract declaration with
    kind="implementation" and evidence="discharges" (or is open with
    an implementation declaration).  An administrative leaf, closed
    planning issue, or audit issue does not count.
    """
    visited: set[int] = set()
    stack: list[int] = list(_children_of(dag, grouping_number))
    while stack:
        num = stack.pop()
        if num in visited or num not in dag.issues:
            continue
        visited.add(num)
        issue = dag.issues[num]
        # Check if this descendant has an implementation declaration
        if num in declarations_by_issue:
            decls = declarations_by_issue[num]
        else:
            decls = []
        for d in decls:
            if d.kind == ObligationKind.implementation:
                # Any descendant with an implementation declaration is
                # an implementation owner, regardless of open/closed
                # state.  The evidence disposition tells us whether the
                # obligation is discharged, but the descendant still
                # owns it.
                return True
        # Continue traversing through grouping descendants
        if is_grouping_issue(issue.title):
            stack.extend(_children_of(dag, num))
    return False


def _build_declarations_map(
    dag: RepoDag,
) -> dict[int, list[ContractDeclaration]]:
    """Parse contract declarations from all issue bodies in the DAG."""
    result: dict[int, list[ContractDeclaration]] = {}
    for number, issue in sorted(dag.issues.items()):
        decls = parse_contract_declarations(issue.body)
        if decls:
            result[number] = decls
    return result


# ---------------------------------------------------------------------------
# Role contradictions (W060)
# ---------------------------------------------------------------------------


def detect_role_contradictions(
    dag: RepoDag,
    declarations_by_issue: dict[int, list[ContractDeclaration]] | None = None,
) -> list[CompletionFinding]:
    """Detect grouping issues whose body declares them as work-unit leaves.

    A grouping issue that declares itself a work-unit leaf (via an
    ``itree-contract`` block with ``kind="implementation"`` and no
    ``requires``) is a role contradiction: its structural role and its
    declared role disagree.
    """
    if declarations_by_issue is None:
        declarations_by_issue = _build_declarations_map(dag)

    findings: list[CompletionFinding] = []
    for number, issue in sorted(dag.issues.items()):
        if not issue.is_open:
            continue
        if not is_grouping_issue(issue.title):
            continue
        if number in declarations_by_issue:
            decls = declarations_by_issue[number]
        else:
            decls = []
        for d in decls:
            if d.kind == ObligationKind.implementation and not d.requires:
                findings.append(
                    CompletionFinding(
                        code="W060",
                        issue_numbers=(number,),
                        evidence=(f'#{number} "{issue.title}" is a grouping issue but declares itself an implementation leaf with no prerequisites',),
                        obligation_kind=d.kind.value,
                    )
                )
    return findings


# ---------------------------------------------------------------------------
# Label conflicts (W061, W062)
# ---------------------------------------------------------------------------


def detect_label_conflicts(
    dag: RepoDag,
    decomposition_label: str = "",
    derived_state_labels: tuple[str, ...] = (),
) -> list[CompletionFinding]:
    """Detect label conflicts: decomposition labels on work-unit leaves, derived-state labels.

    - W061: A work-unit leaf (no children, non-grouping title) carrying
      the configured decomposition label.  The same label on a grouping
      is accepted.
    - W062: Any open issue carrying a configured derived-state label.
    """
    findings: list[CompletionFinding] = []

    # W061: decomposition label on work-unit leaf
    if decomposition_label:
        decomp_lower = decomposition_label.casefold()
        for number, issue in sorted(dag.issues.items()):
            if not issue.is_open:
                continue
            if is_grouping_issue(issue.title):
                continue
            # Must be a leaf (no children)
            if _children_of(dag, number):
                continue
            labels_lower = {label.casefold() for label in issue.labels}
            if decomp_lower in labels_lower:
                findings.append(
                    CompletionFinding(
                        code="W061",
                        issue_numbers=(number,),
                        evidence=(f'#{number} "{issue.title}" is a work-unit leaf carrying decomposition label "{decomposition_label}"',),
                    )
                )

    # W062: derived-state label on open issues
    if derived_state_labels:
        derived_lower = {label.casefold() for label in derived_state_labels}
        w062_issues: list[int] = []
        w062_evidence: list[str] = []
        for number, issue in sorted(dag.issues.items()):
            if not issue.is_open:
                continue
            labels_lower = {label.casefold() for label in issue.labels}
            conflicting = labels_lower & derived_lower
            if conflicting:
                w062_issues.append(number)
                w062_evidence.append(f'#{number} "{issue.title}" carries derived-state label(s): {", ".join(sorted(conflicting))}')
        if w062_issues:
            findings.append(
                CompletionFinding(
                    code="W062",
                    issue_numbers=tuple(w062_issues),
                    evidence=tuple(w062_evidence),
                )
            )

    return findings


# ---------------------------------------------------------------------------
# Completion contracts (E016, E017, Q004)
# ---------------------------------------------------------------------------


def audit_completion_contracts(
    dag: RepoDag,
    deferral_label: str = "deferred",
    declarations_by_issue: dict[int, list[ContractDeclaration]] | None = None,
) -> list[CompletionFinding]:
    """Audit completion contracts using explicit declarations.

    Detects:
    - E016: A closed issue with an implementation declaration whose owner
      is still open and whose evidence disposition is not ``discharges``.
      The ownership chain is followed recursively.
    - E017: A grouping with no implementation-owning descendants that is
      required by an active or closed contract declaration.
    - Q004: A closed audit-type declaration with ``revalidate_on`` refs
      pointing to open issues.  Advisory only.
    """
    if declarations_by_issue is None:
        declarations_by_issue = _build_declarations_map(dag)

    findings: list[CompletionFinding] = []

    # Build ownership chains from declarations
    chains = build_ownership_chains(declarations_by_issue, dag.repo_ref)

    # E016: false-green closure — unresolved implementation obligations
    # found through ownership chain traversal.
    for chain in chains:
        if not chain.unresolved:
            continue
        # Check if the origin issue is closed (false-green) or open (still routed)
        origin_num = _resolve_issue_number(chain.origin, dag.repo_ref)
        if origin_num is None or origin_num not in dag.issues:
            continue
        origin_issue = dag.issues[origin_num]
        owner_num = _resolve_issue_number(chain.current_owner, dag.repo_ref)
        if owner_num is None or owner_num not in dag.issues:
            continue
        owner_issue = dag.issues[owner_num]

        # Only report if the origin is closed (false-green) or if the
        # owner is open (obligation not yet discharged).
        if origin_issue.is_open and not owner_issue.is_open:
            continue  # origin still open, owner closed — not false-green

        path_str = " -> ".join(ref.slug for ref in chain.path)
        findings.append(
            CompletionFinding(
                code="E016",
                issue_numbers=(origin_num,),
                evidence=(
                    f'#{origin_num} "{origin_issue.title}" has an unresolved implementation obligation owned by {chain.current_owner.slug}',
                    f"ownership chain: {path_str}",
                    f"current owner {'is open' if owner_issue.is_open else 'is closed but obligation not discharged'}",
                ),
                origin=chain.origin,
                owner=chain.current_owner,
                path=chain.path,
                obligation_kind=ObligationKind.implementation.value,
                unresolved_burden=(f"implementation obligation routed to {chain.current_owner.slug} but not discharged"),
            )
        )

    # E017: grouping with no implementation-owning descendants required
    # by a contract declaration.  The deferral label does NOT suppress
    # this — a deferred shelf required by a contract remains unresolved.
    deferral_lower = deferral_label.casefold()

    # Build reverse reference map from declarations: which issues declare
    # a requirement on another issue via ``requires`` or ``owner``?
    required_by: dict[int, list[int]] = {}
    for issue_num, decls in declarations_by_issue.items():
        for d in decls:
            # ``requires`` refs
            for req in d.requires:
                req_num = _resolve_issue_number(req, dag.repo_ref)
                if req_num is not None and req_num != issue_num:
                    if req_num in required_by:
                        required_by[req_num].append(issue_num)
                    else:
                        required_by[req_num] = [issue_num]
            # ``owner`` refs also create a requirement
            if d.owner is not None:
                owner_num = _resolve_issue_number(d.owner, dag.repo_ref)
                if owner_num is not None and owner_num != issue_num:
                    if owner_num in required_by:
                        required_by[owner_num].append(issue_num)
                    else:
                        required_by[owner_num] = [issue_num]

    for number, issue in sorted(dag.issues.items()):
        if not issue.is_open:
            continue
        if not is_grouping_issue(issue.title):
            continue
        if _has_implementation_descendants(dag, declarations_by_issue, number):
            continue
        # Check if any contract requires this grouping
        if number not in required_by:
            continue
        # Report — deferral label does NOT suppress this
        referrers = required_by[number]
        labels_lower = {label.casefold() for label in issue.labels}
        is_deferred = deferral_lower in labels_lower
        evidence_lines = [
            f'#{number} "{issue.title}" is a grouping with no implementation-owning descendants but is required by: {", ".join(f"#{r}" for r in referrers)}',
            "traversal will never execute the required implementation through this grouping",
        ]
        if is_deferred:
            evidence_lines.append("the deferral label suppresses W030 (stale shelf) but does not discharge the unresolved contract requirement")
        findings.append(
            CompletionFinding(
                code="E017",
                issue_numbers=(number,),
                evidence=tuple(evidence_lines),
                unresolved_burden=(f"grouping required by {len(referrers)} contract(s) but has no implementation-owning descendants"),
            )
        )

    # Q004: closed audit-type declarations with revalidate_on refs to open issues
    for issue_num, decls in declarations_by_issue.items():
        issue = dag.issues[issue_num]
        if issue.is_open:
            continue
        for d in decls:
            if d.kind != ObligationKind.audit:
                continue
            if not d.revalidate_on:
                continue
            open_revalidate: list[IssueRef] = []
            for ref in d.revalidate_on:
                ref_num = _resolve_issue_number(ref, dag.repo_ref)
                if ref_num is not None and ref_num in dag.issues and dag.issues[ref_num].is_open:
                    open_revalidate.append(ref)
            if open_revalidate:
                findings.append(
                    CompletionFinding(
                        code="Q004",
                        issue_numbers=(issue_num,),
                        evidence=(
                            f'#{issue_num} "{issue.title}" is a closed audit with revalidate_on refs to open issue(s): {", ".join(ref.slug for ref in open_revalidate)}',
                            "previously closed audit may need revalidation for the new case family",
                        ),
                        obligation_kind=d.kind.value,
                    )
                )

    return findings


def _resolve_issue_number(ref: IssueRef, repo_ref: RepoRef) -> int | None:
    """Resolve an IssueRef to a local issue number if it belongs to repo_ref."""
    if ref.repo_ref == repo_ref:
        return ref.number
    return None
