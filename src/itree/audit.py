"""Completion-contract audit layer for ``itree doctor``."""

from __future__ import annotations

from collections.abc import Iterable

from .contracts import (
    ContractCompletion,
    ContractDeclaration,
    ContractDecomposition,
    ContractEvidence,
    ContractKind,
    ContractRole,
    ParsedIssueContracts,
    parse_issue_contracts,
)
from .models import AuditFindingWitness, Finding, IssueRef, RepoDag


def audit_completion_contracts(
    dag: RepoDag,
    *,
    deferral_label: str = "deferred",
    decomposition_label: str = "",
    derived_state_labels: tuple[str, ...] = (),
) -> list[Finding]:
    """Evaluate explicit completion contracts without mutating repository state."""
    parsed = {
        number: parse_issue_contracts(issue.body, repo_ref=dag.repo_ref, issue_number=number) for number, issue in sorted(dag.issues.items()) if not issue.is_pull_request
    }
    declarations_by_issue: dict[int, tuple[ContractDeclaration, ...]] = {number: parsed_issue.declarations for number, parsed_issue in parsed.items()}

    findings: list[Finding] = []
    findings.extend(_parse_findings(parsed))
    findings.extend(_invalid_implementation_discharges(dag, declarations_by_issue))
    findings.extend(_unresolved_implementation_routes(dag, declarations_by_issue))
    findings.extend(_required_groupings_without_work(dag, declarations_by_issue, deferral_label=deferral_label))
    findings.extend(_role_contradictions(dag, declarations_by_issue))
    findings.extend(_decomposition_label_contradictions(dag, declarations_by_issue, decomposition_label=decomposition_label))
    findings.extend(_derived_state_label_duplications(dag, derived_state_labels=derived_state_labels))
    findings.extend(_non_monotone_revalidation_questions(dag, declarations_by_issue))
    return findings


def _finding(
    code: str,
    evidence: list[str],
    witness: AuditFindingWitness | None = None,
    witnesses: tuple[AuditFindingWitness, ...] = (),
) -> Finding:
    from .validate import DIAGNOSTIC_CATALOG

    details = DIAGNOSTIC_CATALOG[code]
    return Finding(
        code=code,
        severity=details["severity"],
        title=details["title"],
        evidence=evidence,
        meaning=details["meaning"],
        remediation=details["remediation"],
        witness=witness,
        witnesses=witnesses,
    )


def _parse_findings(parsed: dict[int, ParsedIssueContracts]) -> list[Finding]:
    findings: list[Finding] = []
    for parsed_issue in parsed.values():
        errors = parsed_issue.errors
        for error in errors:
            findings.append(
                _finding(
                    "E018",
                    [f"#{error.issue.number} line {error.line}: {error.message}"],
                    AuditFindingWitness(
                        current_owner=error.issue,
                        conflicting_state="invalid_contract_block",
                        unresolved_burden=error.message,
                    ),
                )
            )
    return findings


def _invalid_implementation_discharges(
    dag: RepoDag,
    declarations_by_issue: dict[int, tuple[ContractDeclaration, ...]],
) -> list[Finding]:
    implementation_origins = _implementation_origin_numbers(dag, declarations_by_issue)

    evidence: list[str] = []
    first_witness: AuditFindingWitness | None = None
    witnesses: list[AuditFindingWitness] = []
    for owner_number, declarations in sorted(declarations_by_issue.items()):
        for declaration in declarations:
            if declaration.kind == ContractKind.implementation or declaration.evidence != ContractEvidence.discharges or declaration.origin is None:
                continue
            if not _is_local(dag, declaration.origin):
                continue
            if declaration.origin.number not in implementation_origins:
                continue
            evidence.append(f"#{owner_number} presents {declaration.kind.value} evidence as discharge for implementation obligation #{declaration.origin.number}")
            witness = AuditFindingWitness(
                originating_obligation=declaration.origin,
                current_owner=declaration.issue,
                edge_chain=(declaration.origin, declaration.issue),
                conflicting_state=dag.issues[owner_number].state.value,
                obligation_kind=ContractKind.implementation.value,
                unresolved_burden="non-implementation evidence cannot discharge implementation",
            )
            witnesses.append(witness)
            first_witness = first_witness or witness

    return [_finding("E019", evidence, first_witness, tuple(witnesses))] if evidence else []


def _unresolved_implementation_routes(
    dag: RepoDag,
    declarations_by_issue: dict[int, tuple[ContractDeclaration, ...]],
) -> list[Finding]:
    evidence: list[str] = []
    churn_evidence: list[str] = []
    first_witness: AuditFindingWitness | None = None
    churn_witness: AuditFindingWitness | None = None
    witnesses: list[AuditFindingWitness] = []
    churn_witnesses: list[AuditFindingWitness] = []

    for declaration in _implementation_routes(declarations_by_issue):
        if not _is_route_root(declaration):
            continue
        origin = declaration.origin or declaration.issue
        if not _is_local(dag, origin):
            continue
        if not _starts_from_closed_route(dag, declaration, origin):
            continue
        for chain in _walk_implementation_chain(dag, declarations_by_issue, origin, declaration):
            terminal = chain[-1]
            if _chain_is_discharged(dag, declarations_by_issue, origin, terminal):
                continue
            chain_text = _format_chain(chain)
            terminal_issue = dag.issues.get(terminal.number)
            terminal_state = terminal_issue.state.value if terminal_issue else "missing"
            evidence.append(f"{chain_text} remains unresolved at {terminal_state} terminal owner")
            witness = AuditFindingWitness(
                originating_obligation=origin,
                current_owner=terminal,
                edge_chain=tuple(chain),
                conflicting_state=terminal_state,
                obligation_kind=ContractKind.implementation.value,
                unresolved_burden="implementation obligation lacks a matching implementation discharge",
            )
            witnesses.append(witness)
            first_witness = first_witness or witness
            if len(chain) >= 4:
                churn_evidence.append(f"{chain_text} has {len(chain) - 1} routing hops before implementation discharge")
                churn_route_witness = AuditFindingWitness(
                    originating_obligation=origin,
                    current_owner=terminal,
                    edge_chain=tuple(chain),
                    conflicting_state=terminal_state,
                    obligation_kind=ContractKind.implementation.value,
                    unresolved_burden="routing chain churns before implementation-bearing owner",
                )
                churn_witnesses.append(churn_route_witness)
                churn_witness = churn_witness or churn_route_witness

    findings: list[Finding] = []
    if evidence:
        findings.append(_finding("E016", evidence, first_witness, tuple(witnesses)))
    if churn_evidence:
        findings.append(_finding("Q005", churn_evidence, churn_witness, tuple(churn_witnesses)))
    return findings


def _required_groupings_without_work(
    dag: RepoDag,
    declarations_by_issue: dict[int, tuple[ContractDeclaration, ...]],
    *,
    deferral_label: str,
) -> list[Finding]:
    required_groupings: dict[int, list[ContractDeclaration]] = {}
    for declaration in _implementation_declarations(declarations_by_issue):
        for required in declaration.requires:
            if _is_local(dag, required):
                if required.number not in required_groupings:
                    required_groupings[required.number] = []
                required_groupings[required.number].append(declaration)

    evidence: list[str] = []
    first_witness: AuditFindingWitness | None = None
    for required_number, declarations in sorted(required_groupings.items()):
        issue = dag.issues.get(required_number)
        if issue is None or not _is_grouping(issue.title):
            continue
        if _has_open_executable_descendant(dag, required_number):
            continue
        label_note = " including deferred shell" if deferral_label.casefold() in _label_set(issue.labels) else ""
        evidence.append(f"#{required_number} {issue.title!r} is required but has no executable work-unit descendants{label_note}")
        declaration = declarations[0]
        first_witness = first_witness or AuditFindingWitness(
            originating_obligation=declaration.origin or declaration.issue,
            current_owner=_issue_ref(dag, required_number),
            edge_chain=(declaration.issue, _issue_ref(dag, required_number)),
            conflicting_state=issue.state.value,
            obligation_kind=declaration.kind.value,
            unresolved_burden="required grouping has no executable work-unit descendants",
        )

    return [_finding("E017", evidence, first_witness)] if evidence else []


def _role_contradictions(
    dag: RepoDag,
    declarations_by_issue: dict[int, tuple[ContractDeclaration, ...]],
) -> list[Finding]:
    evidence: list[str] = []
    first_witness: AuditFindingWitness | None = None
    for number, declarations in sorted(declarations_by_issue.items()):
        declared_roles = tuple(declaration.role for declaration in declarations if declaration.role is not None)
        if not declared_roles:
            continue
        actual_role = ContractRole.grouping if _is_structural_grouping(dag, number) else ContractRole.work_unit
        for declared_role in declared_roles:
            if declared_role == actual_role:
                continue
            evidence.append(f"#{number} declares role={declared_role.value} but tree/title structure is {actual_role.value}")
            first_witness = first_witness or AuditFindingWitness(
                current_owner=_issue_ref(dag, number),
                conflicting_state=f"declared={declared_role.value} actual={actual_role.value}",
                unresolved_burden="declared role contradicts tree role",
            )
    return [_finding("W060", evidence, first_witness)] if evidence else []


def _decomposition_label_contradictions(
    dag: RepoDag,
    declarations_by_issue: dict[int, tuple[ContractDeclaration, ...]],
    *,
    decomposition_label: str,
) -> list[Finding]:
    evidence: list[str] = []
    first_witness: AuditFindingWitness | None = None
    normalized_label = decomposition_label.casefold()
    for number, issue in sorted(dag.issues.items()):
        if normalized_label and issue.is_open and normalized_label in _label_set(issue.labels) and not _is_structural_grouping(dag, number):
            evidence.append(f"#{number} carries decomposition label {decomposition_label!r} but is a work-unit leaf")
            first_witness = first_witness or AuditFindingWitness(
                current_owner=_issue_ref(dag, number),
                conflicting_state=f"label={decomposition_label}",
                unresolved_burden="decomposition label duplicates grouping state on a work unit",
            )
        if not _is_grouping(issue.title):
            continue
        complete_declarations = [declaration for declaration in declarations_by_issue[number] if declaration.decomposition == ContractDecomposition.complete]
        if complete_declarations and not _has_open_executable_descendant(dag, number):
            evidence.append(f"#{number} claims decomposition=complete but has no executable owner descendant")
            first_witness = first_witness or AuditFindingWitness(
                current_owner=_issue_ref(dag, number),
                conflicting_state="decomposition=complete",
                unresolved_burden="complete decomposition lacks executable work-unit descendant",
            )
    return [_finding("W061", evidence, first_witness)] if evidence else []


def _derived_state_label_duplications(
    dag: RepoDag,
    *,
    derived_state_labels: tuple[str, ...],
) -> list[Finding]:
    configured = {label.casefold() for label in derived_state_labels}
    if not configured:
        return []
    evidence: list[str] = []
    first_witness: AuditFindingWitness | None = None
    for number, issue in sorted(dag.issues.items()):
        overlapping = sorted(_label_set(issue.labels) & configured)
        if not issue.is_open or not overlapping:
            continue
        evidence.append(f"#{number} carries derived-state label(s): {', '.join(overlapping)}")
        first_witness = first_witness or AuditFindingWitness(
            current_owner=_issue_ref(dag, number),
            conflicting_state=f"labels={','.join(overlapping)}",
            unresolved_burden="configured label duplicates graph-derived state",
        )
    return [_finding("W062", evidence, first_witness)] if evidence else []


def _non_monotone_revalidation_questions(
    dag: RepoDag,
    declarations_by_issue: dict[int, tuple[ContractDeclaration, ...]],
) -> list[Finding]:
    evidence: list[str] = []
    first_witness: AuditFindingWitness | None = None
    for number, declarations in sorted(declarations_by_issue.items()):
        issue = dag.issues[number]
        if issue.is_open:
            continue
        for declaration in declarations:
            live_refs = [ref for ref in declaration.revalidate_on if _is_local(dag, ref) and ref.number in dag.issues and dag.issues[ref.number].is_open]
            if not live_refs:
                continue
            live_text = ", ".join(f"#{ref.number}" for ref in live_refs)
            evidence.append(f"closed #{number} declares revalidate_on live issue(s): {live_text}")
            first_witness = first_witness or AuditFindingWitness(
                originating_obligation=declaration.origin or declaration.issue,
                current_owner=declaration.issue,
                edge_chain=(declaration.issue, *live_refs),
                conflicting_state="closed claim with live revalidation trigger",
                obligation_kind=declaration.kind.value,
                unresolved_burden="closure claim needs revalidation",
            )
    return [_finding("Q004", evidence, first_witness)] if evidence else []


def _implementation_declarations(
    declarations_by_issue: dict[int, tuple[ContractDeclaration, ...]],
) -> Iterable[ContractDeclaration]:
    for declarations in declarations_by_issue.values():
        for declaration in declarations:
            if declaration.kind == ContractKind.implementation:
                yield declaration


def _implementation_routes(
    declarations_by_issue: dict[int, tuple[ContractDeclaration, ...]],
) -> Iterable[ContractDeclaration]:
    for declaration in _implementation_declarations(declarations_by_issue):
        if declaration.owner is not None and declaration.evidence != ContractEvidence.discharges:
            yield declaration


def _implementation_origin_numbers(
    dag: RepoDag,
    declarations_by_issue: dict[int, tuple[ContractDeclaration, ...]],
) -> set[int]:
    origins: set[int] = set()
    for declaration in _implementation_declarations(declarations_by_issue):
        origin = declaration.origin or declaration.issue
        if _is_local(dag, origin):
            origins.add(origin.number)
    return origins


def _transitive_implementation_owner_origins(
    dag: RepoDag,
    declarations_by_issue: dict[int, tuple[ContractDeclaration, ...]],
) -> dict[int, set[int]]:
    owners: dict[int, set[int]] = {}
    for declaration in _implementation_routes(declarations_by_issue):
        if not _is_route_root(declaration):
            continue
        origin = declaration.origin or declaration.issue
        if not _is_local(dag, origin):
            continue
        for chain in _walk_implementation_chain(dag, declarations_by_issue, origin, declaration):
            for owner in chain[1:]:
                if not _is_local(dag, owner):
                    continue
                if owner.number not in owners:
                    owners[owner.number] = set()
                owners[owner.number].add(origin.number)
    return owners


def _is_route_root(declaration: ContractDeclaration) -> bool:
    return declaration.origin is None or declaration.origin == declaration.issue


def _starts_from_closed_route(dag: RepoDag, declaration: ContractDeclaration, origin: IssueRef) -> bool:
    if declaration.completion == ContractCompletion.completed:
        return True
    declaration_issue = dag.issues.get(declaration.issue.number)
    origin_issue = dag.issues.get(origin.number)
    if declaration_issue is not None and not declaration_issue.is_open:
        return True
    return origin_issue is not None and not origin_issue.is_open


def _walk_implementation_chain(
    dag: RepoDag,
    declarations_by_issue: dict[int, tuple[ContractDeclaration, ...]],
    origin: IssueRef,
    declaration: ContractDeclaration,
) -> list[list[IssueRef]]:
    if declaration.owner is None:
        return [[declaration.issue]]
    return _walk_owner(dag, declarations_by_issue, origin, declaration.owner, [declaration.issue], set())


def _walk_owner(
    dag: RepoDag,
    declarations_by_issue: dict[int, tuple[ContractDeclaration, ...]],
    origin: IssueRef,
    owner: IssueRef,
    chain: list[IssueRef],
    seen: set[int],
) -> list[list[IssueRef]]:
    if not _is_local(dag, owner) or owner.number not in dag.issues:
        return [chain + [owner]]
    if owner.number in seen or owner in chain:
        return [chain + [owner]]
    if _chain_is_discharged(dag, declarations_by_issue, origin, owner):
        return [chain + [owner]]

    next_routes = [
        declaration
        for declaration in declarations_by_issue[owner.number]
        if declaration.kind == ContractKind.implementation
        and declaration.owner is not None
        and declaration.evidence != ContractEvidence.discharges
        and declaration.origin == origin
    ]
    if not next_routes:
        return [chain + [owner]]

    seen_with_owner = seen | {owner.number}
    chains: list[list[IssueRef]] = []
    for route in next_routes:
        next_owner = route.owner
        if next_owner is not None:
            chains.extend(_walk_owner(dag, declarations_by_issue, origin, next_owner, chain + [owner], seen_with_owner))
    return chains


def _chain_is_discharged(
    dag: RepoDag,
    declarations_by_issue: dict[int, tuple[ContractDeclaration, ...]],
    origin: IssueRef,
    owner: IssueRef,
) -> bool:
    if not _is_local(dag, owner):
        return False
    if owner.number not in declarations_by_issue:
        return False
    for declaration in declarations_by_issue[owner.number]:
        if declaration.kind == ContractKind.implementation and declaration.evidence == ContractEvidence.discharges and declaration.origin == origin:
            return True
    return False


def _has_open_executable_descendant(dag: RepoDag, issue_number: int) -> bool:
    pending = list(dag.children_of[issue_number])
    seen: set[int] = set()
    while pending:
        current = pending.pop(0)
        if current in seen:
            continue
        seen.add(current)
        issue = dag.issues[current]
        if issue.is_open and not _is_grouping(issue.title):
            return True
        pending.extend(dag.children_of[current])
    return False


def _is_structural_grouping(dag: RepoDag, issue_number: int) -> bool:
    issue = dag.issues[issue_number]
    return _is_grouping(issue.title) or any(dag.issues[child].is_open for child in dag.children_of[issue_number])


def _is_grouping(title: str) -> bool:
    from .validate import is_grouping_issue

    return is_grouping_issue(title)


def _is_local(dag: RepoDag, ref: IssueRef) -> bool:
    return ref.repo_ref == dag.repo_ref


def _issue_ref(dag: RepoDag, number: int) -> IssueRef:
    return IssueRef(repo_ref=dag.repo_ref, number=number)


def _format_chain(chain: list[IssueRef]) -> str:
    return " -> ".join(f"#{ref.number}" if ref.repo_ref == chain[0].repo_ref else ref.slug for ref in chain)


def _label_set(labels: tuple[str, ...]) -> set[str]:
    return {label.casefold() for label in labels}
