"""Completion-contract audit for itree doctor (itree#41).

Detects when a structurally valid tree still permits an agent to close
administrative slices while the implementation named by the original
contract never occurs.

The audit is deterministic and builds on #40's readiness model.  All
functions are pure: they consume an already-built ``RepoDag`` and never
touch the network.  ``generate_doctor_report`` converts
``CompletionFinding`` results into ``Finding`` objects via the
diagnostic catalog.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

from .models import RepoDag
from .validate import is_grouping_issue, lacks_acceptance_criteria

# Pattern for issue references in bodies: #N where N is a positive integer.
# Won't match markdown headers (# at line start) because those are followed
# by whitespace, not digits.
_ISSUE_REF_PATTERN = re.compile(r"#([1-9]\d*)")

# Transfer language indicating an implementation obligation was moved
# to another issue rather than discharged.
_TRANSFER_KEYWORDS = ("moved to", "routed to", "transferred to", "deferred to")

# Audit-only language indicating the issue is explicitly not an
# implementation obligation.
_AUDIT_ONLY_KEYWORDS = ("audit-only", "audit only")

# New-owner language indicating a closed broad-scope audit may need
# revalidation when new case families land.
_NEW_OWNER_KEYWORDS = ("new owner", "future cases", "future owner", "later owner")


class CompletionFinding(BaseModel):
    """Intermediate audit result converted to Finding by generate_doctor_report."""

    model_config = ConfigDict(frozen=True)

    code: str
    issue_numbers: tuple[int, ...]
    evidence: tuple[str, ...]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_issue_refs(body: str | None) -> tuple[int, ...]:
    """Extract issue numbers referenced in an issue body via #N pattern."""
    if not body:
        return ()
    return tuple(int(m.group(1)) for m in _ISSUE_REF_PATTERN.finditer(body))


def _children_of(dag: RepoDag, number: int) -> tuple[int, ...]:
    """Return children of ``number`` or empty tuple — no dict.get default."""
    if number in dag.children_of:
        return dag.children_of[number]
    return ()


def _has_work_unit_descendants(dag: RepoDag, grouping_number: int) -> bool:
    """Check if a grouping issue has any non-grouping descendants in its subtree."""
    visited: set[int] = set()
    stack: list[int] = list(_children_of(dag, grouping_number))
    while stack:
        num = stack.pop()
        if num in visited or num not in dag.issues:
            continue
        visited.add(num)
        issue = dag.issues[num]
        if not is_grouping_issue(issue.title):
            return True
        stack.extend(_children_of(dag, num))
    return False


def _body_contains_any(body: str | None, keywords: tuple[str, ...]) -> bool:
    """Case-insensitive check if body contains any of the keywords."""
    if not body:
        return False
    body_lower = body.lower()
    return any(kw in body_lower for kw in keywords)


# ---------------------------------------------------------------------------
# Role contradictions
# ---------------------------------------------------------------------------


def detect_role_contradictions(dag: RepoDag) -> list[CompletionFinding]:
    """Detect grouping issues whose body declares them as work-unit leaves.

    A grouping issue (Milestone:, Backlog:, Roadmap:, Phase:) that declares
    itself a "work-unit leaf" in its body is a role contradiction: its
    structural role and its declared role disagree.
    """
    findings: list[CompletionFinding] = []
    for number, issue in sorted(dag.issues.items()):
        if not issue.is_open:
            continue
        if not is_grouping_issue(issue.title):
            continue
        body = issue.body
        if body and ("work-unit leaf" in body.lower() or "work unit leaf" in body.lower()):
            findings.append(
                CompletionFinding(
                    code="W060",
                    issue_numbers=(number,),
                    evidence=(f'#{number} "{issue.title}" is a grouping issue but its body declares it a work-unit leaf',),
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Label conflicts
# ---------------------------------------------------------------------------


def detect_label_conflicts(
    dag: RepoDag,
    decomposition_label: str = "",
    derived_state_labels: tuple[str, ...] = (),
) -> list[CompletionFinding]:
    """Detect label conflicts: decomposition labels on work-unit leaves, derived-state labels.

    - W061: A work-unit leaf carrying the configured decomposition label.
      The same label on a partially decomposed grouping is accepted.
    - W062: Any issue carrying a configured derived-state label.
      Domain and workflow labels are untouched.
    """
    findings: list[CompletionFinding] = []

    # W061: decomposition label on work-unit leaf
    if decomposition_label:
        decomp_lower = decomposition_label.casefold()
        for number, issue in sorted(dag.issues.items()):
            if not issue.is_open:
                continue
            if is_grouping_issue(issue.title):
                # Grouping with decomposition label is accepted regardless
                # of whether it has children yet — the label marks it as
                # needing decomposition, which is its intended use on a grouping.
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

    # W062: derived-state label on any issue
    if derived_state_labels:
        derived_lower = {label.casefold() for label in derived_state_labels}
        w062_issues: list[int] = []
        w062_evidence: list[str] = []
        for number, issue in sorted(dag.issues.items()):
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
# Completion contracts
# ---------------------------------------------------------------------------


def audit_completion_contracts(
    dag: RepoDag,
    deferral_label: str = "deferred",
) -> list[CompletionFinding]:
    """Audit completion contracts for false-green closure and non-monotone completion.

    Detects:
    - E016: A closed issue transferred an implementation obligation to a later
      issue that is still open. Closing the original leaves the obligation
      unresolved.
    - E017: A grouping issue with no work-unit descendants is required by a
      completion contract. Traversal will never execute the required
      implementation through this grouping.
    - Q004: A closed broad-scope audit declared a later owner for future cases.
      The previously closed audit may need revalidation when the new owner
      lands new case families.
    """
    findings: list[CompletionFinding] = []

    # Build reverse reference map: issue N -> issues that reference N in body
    ref_map: dict[int, list[int]] = {}
    for number, issue in sorted(dag.issues.items()):
        for ref in _parse_issue_refs(issue.body):
            if ref in ref_map:
                ref_map[ref].append(number)
            else:
                ref_map[ref] = [number]

    # E016: false-green closure
    for number, issue in sorted(dag.issues.items()):
        if issue.is_open:
            continue
        if lacks_acceptance_criteria(issue.body):
            continue
        if _body_contains_any(issue.body, _AUDIT_ONLY_KEYWORDS):
            continue
        if not _body_contains_any(issue.body, _TRANSFER_KEYWORDS):
            continue
        refs = _parse_issue_refs(issue.body)
        open_targets = [r for r in refs if r in dag.issues and dag.issues[r].is_open]
        if open_targets:
            chain = " -> ".join(f"#{n}" for n in [number, *open_targets])
            findings.append(
                CompletionFinding(
                    code="E016",
                    issue_numbers=(number,),
                    evidence=(
                        f'#{number} "{issue.title}" is closed but transferred implementation obligation to open issue(s): {", ".join(f"#{r}" for r in open_targets)}',
                        f"ownership chain: {chain}",
                    ),
                )
            )

    # E017: grouping with no work-unit descendants required by a contract
    deferral_lower = deferral_label.casefold()
    for number, issue in sorted(dag.issues.items()):
        if not issue.is_open:
            continue
        if not is_grouping_issue(issue.title):
            continue
        if _has_work_unit_descendants(dag, number):
            continue
        labels_lower = {label.casefold() for label in issue.labels}
        if deferral_lower in labels_lower:
            continue
        if number in ref_map:
            referrers = ref_map[number]
            findings.append(
                CompletionFinding(
                    code="E017",
                    issue_numbers=(number,),
                    evidence=(
                        f'#{number} "{issue.title}" is a grouping with no work-unit descendants but is required by: {", ".join(f"#{r}" for r in referrers)}',
                        "traversal will never execute the required implementation through this grouping",
                    ),
                )
            )

    # Q004: closed broad-scope audit with declared later owner
    for number, issue in sorted(dag.issues.items()):
        if issue.is_open:
            continue
        if not _body_contains_any(issue.body, _NEW_OWNER_KEYWORDS):
            continue
        refs = _parse_issue_refs(issue.body)
        open_targets = [r for r in refs if r in dag.issues and dag.issues[r].is_open]
        if open_targets:
            findings.append(
                CompletionFinding(
                    code="Q004",
                    issue_numbers=(number,),
                    evidence=(
                        f'#{number} "{issue.title}" is a closed broad-scope audit with a declared later owner: {", ".join(f"#{r}" for r in open_targets)}',
                        "previously closed audit may need revalidation for the new case family",
                    ),
                )
            )

    return findings
