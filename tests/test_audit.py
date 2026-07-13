"""Tests for the completion-contract audit (itree#41).

These tests prove that ``itree doctor`` detects when a structurally valid
tree still permits an agent to close administrative slices while the
implementation named by the original contract never occurs.

The pure functions in ``audit.py`` consume an already-built ``RepoDag``
(see ``traversal.py``); no IO is exercised here.  They follow the same
pattern as ``readiness.py``: lightweight result types that
``generate_doctor_report`` converts into ``Finding`` objects via the
diagnostic catalog.

Red-first: these tests are written before the implementation and are
expected to fail until ``src/itree/audit.py`` exists.
"""

from __future__ import annotations

from itree.audit import (
    audit_completion_contracts,
    detect_label_conflicts,
    detect_role_contradictions,
)
from itree.models import GithubIssue, IssueState, RepoDag, RepoRef


def _repo_ref() -> RepoRef:
    return RepoRef(owner="testowner", repo="testrepo")


def _issue(
    number: int,
    title: str = "",
    state: IssueState = IssueState.open,
    body: str | None = None,
    labels: tuple[str, ...] = (),
) -> GithubIssue:
    return GithubIssue(
        id=number + 5000,
        number=number,
        title=title or f"Issue #{number}",
        state=state,
        html_url=f"https://github.com/testowner/testrepo/issues/{number}",
        body=body,
        labels=labels,
    )


# ---------------------------------------------------------------------------
# detect_role_contradictions
# ---------------------------------------------------------------------------


def test_grouping_declared_as_work_unit_leaf_in_body() -> None:
    """A grouping issue whose body declares it a work-unit leaf produces a finding."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Milestone: v1", body="This is a work-unit leaf."),
            3: _issue(3, "Child work unit"),
        },
        children_of={1: (2,), 2: (3,)},
    )
    findings = detect_role_contradictions(dag)
    assert any(f.code == "W060" for f in findings)
    w060 = [f for f in findings if f.code == "W060"][0]
    assert 2 in w060.issue_numbers


def test_grouping_without_work_unit_declaration_is_accepted() -> None:
    """A grouping issue without a body declaring it a work-unit leaf is accepted."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Milestone: v1", body="Some milestone description."),
            3: _issue(3, "Child work unit"),
        },
        children_of={1: (2,), 2: (3,)},
    )
    findings = detect_role_contradictions(dag)
    assert findings == []


def test_work_unit_without_grouping_title_is_accepted() -> None:
    """A regular work unit is not a role contradiction."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Implement feature X", body="Acceptance criteria: done when X works."),
        },
        children_of={1: (2,)},
    )
    findings = detect_role_contradictions(dag)
    assert findings == []


# ---------------------------------------------------------------------------
# detect_label_conflicts
# ---------------------------------------------------------------------------


def test_decomposition_label_on_work_unit_leaf_produces_finding() -> None:
    """A work-unit leaf carrying the configured decomposition label produces a finding."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Implement feature X", labels=("needs-decomposition",)),
        },
        children_of={1: (2,)},
    )
    findings = detect_label_conflicts(dag, decomposition_label="needs-decomposition")
    assert any(f.code == "W061" for f in findings)
    w061 = [f for f in findings if f.code == "W061"][0]
    assert 2 in w061.issue_numbers


def test_decomposition_label_on_partially_decomposed_grouping_accepted() -> None:
    """The same decomposition label on a partially decomposed grouping is accepted."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Milestone: v1", labels=("needs-decomposition",)),
            3: _issue(3, "Child work unit A"),
            4: _issue(4, "Child work unit B"),
        },
        children_of={1: (2,), 2: (3, 4)},
    )
    findings = detect_label_conflicts(dag, decomposition_label="needs-decomposition")
    assert findings == []


def test_decomposition_label_unconfigured_produces_no_findings() -> None:
    """When no decomposition_label is configured, no W061 findings are produced."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Work unit", labels=("needs-decomposition",)),
        },
        children_of={1: (2,)},
    )
    findings = detect_label_conflicts(dag, decomposition_label="")
    assert findings == []


def test_derived_state_label_produces_finding() -> None:
    """A configured derived-state label produces a finding; domain/workflow labels are untouched."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Work unit A", labels=("blocked",)),  # derived state
            3: _issue(3, "Work unit B", labels=("enhancement",)),  # domain label
            4: _issue(4, "Work unit C", labels=("in-progress",)),  # workflow label
        },
        children_of={1: (2, 3, 4)},
    )
    findings = detect_label_conflicts(
        dag,
        decomposition_label="",
        derived_state_labels=("blocked",),
    )
    assert any(f.code == "W062" for f in findings)
    w062 = [f for f in findings if f.code == "W062"][0]
    assert 2 in w062.issue_numbers
    # Domain and workflow labels are not flagged
    assert 3 not in w062.issue_numbers
    assert 4 not in w062.issue_numbers


def test_no_derived_state_labels_configured_produces_no_w062() -> None:
    """When no derived_state_labels are configured, no W062 findings."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Work unit", labels=("blocked",)),
        },
        children_of={1: (2,)},
    )
    findings = detect_label_conflicts(dag, decomposition_label="")
    assert all(f.code != "W062" for f in findings)


# ---------------------------------------------------------------------------
# audit_completion_contracts
# ---------------------------------------------------------------------------


def test_obligation_transfer_leaves_undischarged() -> None:
    """Work unit A transfers an implementation obligation to later B; closing A leaves it unresolved."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(
                2,
                "Original implementation",
                state=IssueState.closed,
                body=("Acceptance Criteria: done when feature X is implemented.\n\nImplementation moved to #3."),
            ),
            3: _issue(3, "Actual implementation", state=IssueState.open),
        },
        children_of={1: (2, 3)},
        dependencies={3: (2,)},
    )
    findings = audit_completion_contracts(dag)
    assert any(f.code == "E016" for f in findings)
    e016 = [f for f in findings if f.code == "E016"][0]
    assert 2 in e016.issue_numbers
    # The ownership chain should mention both the original and the later owner
    assert any("3" in line for line in e016.evidence)


def test_release_contract_requiring_deferred_grouping_no_descendants() -> None:
    """A release contract requiring deferred grouping G where G has no work-unit descendants."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Release: v1", body="Requires #3 to be complete."),
            3: _issue(3, "Milestone: deferred feature"),
            # No children under #3 — no executable descendants
        },
        children_of={1: (2, 3)},
    )
    findings = audit_completion_contracts(dag)
    assert any(f.code == "E017" for f in findings)
    e017 = [f for f in findings if f.code == "E017"][0]
    assert 3 in e017.issue_numbers


def test_audit_cannot_discharge_implementation_obligation() -> None:
    """An audit that records gaps and routes every implementation to later owners cannot discharge."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(
                2,
                "Audit of feature X",
                state=IssueState.closed,
                body=("Acceptance Criteria: feature X implemented.\n\nAudit found gaps. Implementation routed to #3."),
            ),
            3: _issue(3, "Implement feature X", state=IssueState.open),
        },
        children_of={1: (2, 3)},
        dependencies={3: (2,)},
    )
    findings = audit_completion_contracts(dag)
    assert any(f.code == "E016" for f in findings)


def test_explicit_audit_only_work_accepted() -> None:
    """Explicit audit-only work independent of implementation obligations is accepted."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(
                2,
                "Audit codebase for security",
                body="Done when security audit is complete.\n\nThis is an audit-only task.",
            ),
        },
        children_of={1: (2,)},
    )
    findings = audit_completion_contracts(dag)
    assert all(f.code != "E016" for f in findings)


def test_independent_implementation_slices_accepted() -> None:
    """Independent implementation slices are accepted."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Implement feature A", body="Done when A works."),
            3: _issue(3, "Implement feature B", body="Done when B works."),
        },
        children_of={1: (2, 3)},
    )
    findings = audit_completion_contracts(dag)
    assert all(f.code != "E016" for f in findings)
    assert all(f.code != "E017" for f in findings)


def test_unreferenced_deferred_shelf_accepted() -> None:
    """Unreferenced deferred shelves with the deferral label are accepted."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Milestone: far future", labels=("deferred",)),
        },
        children_of={1: (2,)},
    )
    findings = audit_completion_contracts(dag)
    assert all(f.code != "E017" for f in findings)


def test_verification_unit_with_cross_branch_owner_unready_not_error() -> None:
    """A verification unit with a later cross-branch owner remains unready; forward edge is not an error."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Milestone: A"),
            3: _issue(3, "Verification unit", body="Done when #5 is verified."),
            4: _issue(4, "Milestone: B"),
            5: _issue(5, "Implementation under B"),
        },
        children_of={1: (2, 4), 2: (3,), 4: (5,)},
        dependencies={3: (5,)},  # cross-branch, forward in preorder
    )
    findings = audit_completion_contracts(dag)
    # The forward-preorder edge is not itself a completion-contract error
    assert all(f.code != "E016" for f in findings)
    assert all(f.code != "E017" for f in findings)


def test_closed_broad_scope_audit_revalidation_on_new_owner() -> None:
    """Adding a declared later owner causes a previously closed broad-scope audit to be reported for revalidation."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(
                2,
                "Broad audit of subsystem",
                state=IssueState.closed,
                body=("Acceptance Criteria: all of subsystem verified.\n\nAudit complete. New owner: #3 for future cases."),
            ),
            3: _issue(3, "New case family owner", state=IssueState.open),
        },
        children_of={1: (2, 3)},
    )
    findings = audit_completion_contracts(dag)
    assert any(f.code == "Q004" for f in findings)


def test_no_completion_findings_on_clean_tree() -> None:
    """A clean tree with open work units and no transfers produces no completion findings."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Implement feature X", body="Done when X works."),
            3: _issue(3, "Milestone: v1"),
            4: _issue(4, "Implement feature Y", body="Done when Y works."),
        },
        children_of={1: (3,), 3: (2, 4)},
    )
    findings = audit_completion_contracts(dag)
    assert findings == []
