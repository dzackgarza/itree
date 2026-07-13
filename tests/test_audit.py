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


# ---------------------------------------------------------------------------
# Integration: audit findings appear in generate_doctor_report
# ---------------------------------------------------------------------------


def test_doctor_report_includes_e016_false_green_closure() -> None:
    """generate_doctor_report includes E016 when a false-green closure is detected."""
    from itree.validate import generate_doctor_report

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
    report = generate_doctor_report(dag)
    e016 = [f for f in report.findings if f.code == "E016"]
    assert len(e016) == 1
    assert e016[0].severity == "error"


def test_doctor_report_includes_w061_decomposition_label() -> None:
    """generate_doctor_report includes W061 when decomposition_label is configured."""
    from itree.validate import generate_doctor_report

    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Work unit", labels=("needs-decomposition",)),
        },
        children_of={1: (2,)},
    )
    report = generate_doctor_report(dag, decomposition_label="needs-decomposition")
    w061 = [f for f in report.findings if f.code == "W061"]
    assert len(w061) == 1


def test_doctor_report_includes_w062_derived_state_label() -> None:
    """generate_doctor_report includes W062 when derived_state_labels are configured."""
    from itree.validate import generate_doctor_report

    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Work unit", labels=("blocked",)),
        },
        children_of={1: (2,)},
    )
    report = generate_doctor_report(dag, derived_state_labels=("blocked",))
    w062 = [f for f in report.findings if f.code == "W062"]
    assert len(w062) == 1


def test_doctor_report_includes_q004_audit_revalidation() -> None:
    """generate_doctor_report includes Q004 for closed broad-scope audit with new owner."""
    from itree.validate import generate_doctor_report

    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(
                2,
                "Broad audit",
                state=IssueState.closed,
                body=("Acceptance Criteria: all of subsystem verified.\n\nAudit complete. New owner: #3 for future cases."),
            ),
            3: _issue(3, "New case family owner", state=IssueState.open),
        },
        children_of={1: (2, 3)},
    )
    report = generate_doctor_report(dag)
    q004 = [f for f in report.findings if f.code == "Q004"]
    assert len(q004) == 1
    assert q004[0].severity == "question"


def test_doctor_report_clean_tree_has_no_audit_findings() -> None:
    """A clean tree produces no completion-contract audit findings in the doctor report."""
    from itree.validate import generate_doctor_report

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
    report = generate_doctor_report(dag)
    audit_codes = {"E016", "E017", "W060", "W061", "W062", "Q004"}
    audit_findings = [f for f in report.findings if f.code in audit_codes]
    assert audit_findings == []


# ---------------------------------------------------------------------------
# Config support: decomposition_label and derived_state_labels
# ---------------------------------------------------------------------------


def test_metrics_config_has_decomposition_label() -> None:
    """MetricsConfig includes decomposition_label with empty default."""
    from itree.metrics import MetricsConfig

    config = MetricsConfig()
    assert config.decomposition_label == ""


def test_metrics_config_has_derived_state_labels() -> None:
    """MetricsConfig includes derived_state_labels with empty tuple default."""
    from itree.metrics import MetricsConfig

    config = MetricsConfig()
    assert config.derived_state_labels == ()


def test_metrics_config_accepts_decomposition_label() -> None:
    """MetricsConfig accepts decomposition_label from config data."""
    from itree.metrics import MetricsConfig

    config = MetricsConfig.model_validate({"decomposition_label": "needs-decomposition"})
    assert config.decomposition_label == "needs-decomposition"


def test_metrics_config_accepts_derived_state_labels() -> None:
    """MetricsConfig accepts derived_state_labels from config data."""
    from itree.metrics import MetricsConfig

    config = MetricsConfig.model_validate({"derived_state_labels": ["blocked", "in-progress"]})
    assert config.derived_state_labels == ("blocked", "in-progress")


# ---------------------------------------------------------------------------
# --explain: all new codes produce meaningful diagnostic text
# ---------------------------------------------------------------------------


def test_explain_e016_in_catalog() -> None:
    """E016 is in DIAGNOSTIC_CATALOG with required fields for --explain."""
    from itree.validate import DIAGNOSTIC_CATALOG

    details = DIAGNOSTIC_CATALOG["E016"]
    assert details["severity"] == "error"
    assert "ideal_model" in details
    assert details["meaning"]
    assert details["remediation"]


def test_explain_e017_in_catalog() -> None:
    """E017 is in DIAGNOSTIC_CATALOG with required fields for --explain."""
    from itree.validate import DIAGNOSTIC_CATALOG

    details = DIAGNOSTIC_CATALOG["E017"]
    assert details["severity"] == "error"
    assert "ideal_model" in details
    assert details["meaning"]
    assert details["remediation"]


def test_explain_w060_in_catalog() -> None:
    """W060 is in DIAGNOSTIC_CATALOG with required fields for --explain."""
    from itree.validate import DIAGNOSTIC_CATALOG

    details = DIAGNOSTIC_CATALOG["W060"]
    assert details["severity"] == "warning"
    assert "ideal_model" in details
    assert details["meaning"]
    assert details["remediation"]


def test_explain_w061_in_catalog() -> None:
    """W061 is in DIAGNOSTIC_CATALOG with required fields for --explain."""
    from itree.validate import DIAGNOSTIC_CATALOG

    details = DIAGNOSTIC_CATALOG["W061"]
    assert details["severity"] == "warning"
    assert "ideal_model" in details
    assert details["meaning"]
    assert details["remediation"]


def test_explain_w062_in_catalog() -> None:
    """W062 is in DIAGNOSTIC_CATALOG with required fields for --explain."""
    from itree.validate import DIAGNOSTIC_CATALOG

    details = DIAGNOSTIC_CATALOG["W062"]
    assert details["severity"] == "warning"
    assert "ideal_model" in details
    assert details["meaning"]
    assert details["remediation"]


def test_explain_q004_in_catalog() -> None:
    """Q004 is in DIAGNOSTIC_CATALOG with required fields for --explain."""
    from itree.validate import DIAGNOSTIC_CATALOG

    details = DIAGNOSTIC_CATALOG["Q004"]
    assert details["severity"] == "question"
    assert details["meaning"]
    assert details["remediation"]


# ---------------------------------------------------------------------------
# Spec A: _parse_issue_refs deduplicates issue references
# ---------------------------------------------------------------------------


def test_parse_issue_refs_dedupes_repeated_refs() -> None:
    """An issue body containing #3 twice yields (3,) not (3, 3) (Spec A)."""
    from itree.audit import _parse_issue_refs

    result = _parse_issue_refs("See #3 and #3 again, plus #3 once more.")
    assert result == (3,)


def test_parse_issue_refs_preserves_order_of_first_appearance() -> None:
    """Deduplicated refs preserve the order of first appearance (Spec A)."""
    from itree.audit import _parse_issue_refs

    result = _parse_issue_refs("first #5 then #3 then #5 again then #3 again")
    assert result == (5, 3)


# ---------------------------------------------------------------------------
# Spec B: ref_map skips self-references (no E017 self-contract)
# ---------------------------------------------------------------------------


def test_self_reference_does_not_create_e017_self_contract() -> None:
    """A grouping issue referencing itself in its body does not produce E017 (Spec B)."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Milestone: v1", body="Tracks #2 itself for completion."),
        },
        children_of={1: (2,)},
    )
    findings = audit_completion_contracts(dag)
    assert all(f.code != "E017" for f in findings)


# ---------------------------------------------------------------------------
# Spec C: E017 filters referrers to open issues
# ---------------------------------------------------------------------------


def test_e017_does_not_fire_when_referrer_is_closed() -> None:
    """A grouping with no work-unit descendants referenced only by a closed issue does not produce E017 (Spec C)."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(
                2,
                "Release: v1",
                state=IssueState.closed,
                body="Requires #3 to be complete.",
            ),
            3: _issue(3, "Milestone: deferred feature"),
        },
        children_of={1: (2, 3)},
    )
    findings = audit_completion_contracts(dag)
    assert all(f.code != "E017" for f in findings)


def test_e017_fires_when_referrer_is_open() -> None:
    """A grouping with no work-unit descendants referenced by an open issue produces E017 (Spec C positive case)."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Release: v1", body="Requires #3 to be complete."),
            3: _issue(3, "Milestone: deferred feature"),
        },
        children_of={1: (2, 3)},
    )
    findings = audit_completion_contracts(dag)
    e017 = [f for f in findings if f.code == "E017"]
    assert e017
    assert 3 in e017[0].issue_numbers


# ---------------------------------------------------------------------------
# Spec D: E016 only includes refs on the same line as a transfer keyword
# ---------------------------------------------------------------------------


def test_e016_excludes_refs_on_non_transfer_lines() -> None:
    """A ref on a non-transfer line is not a transfer target (Spec D)."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(
                2,
                "Original implementation",
                state=IssueState.closed,
                body=("Acceptance Criteria: done when feature X is implemented.\n\nImplementation moved to #3.\n\nSee also #5 for context."),
            ),
            3: _issue(3, "Actual implementation", state=IssueState.open),
            5: _issue(5, "Context issue", state=IssueState.open),
        },
        children_of={1: (2, 3, 5)},
    )
    findings = audit_completion_contracts(dag)
    e016 = [f for f in findings if f.code == "E016"]
    assert e016
    evidence_text = " ".join(e016[0].evidence)
    assert "#3" in evidence_text
    assert "#5" not in evidence_text


# ---------------------------------------------------------------------------
# Spec E: Q004 only fires on audit-type issues
# ---------------------------------------------------------------------------


def test_q004_does_not_fire_on_non_audit_new_owner_handoff() -> None:
    """A closed non-audit issue with 'new owner' in body does not produce Q004 (Spec E)."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(
                2,
                "Ownership handoff",
                state=IssueState.closed,
                body=("Acceptance Criteria: feature X implemented.\n\nNew owner: #3 for future cases."),
            ),
            3: _issue(3, "New owner issue", state=IssueState.open),
        },
        children_of={1: (2, 3)},
    )
    findings = audit_completion_contracts(dag)
    assert all(f.code != "Q004" for f in findings)


def test_q004_fires_on_audit_with_new_owner() -> None:
    """A closed audit issue with both audit keyword and new-owner keyword produces Q004 (Spec E positive)."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(
                2,
                "Audit of subsystem",
                state=IssueState.closed,
                body=("Acceptance Criteria: all of subsystem verified.\n\nThis is an audit-only task. New owner: #3 for future cases."),
            ),
            3: _issue(3, "New case family owner", state=IssueState.open),
        },
        children_of={1: (2, 3)},
    )
    findings = audit_completion_contracts(dag)
    q004 = [f for f in findings if f.code == "Q004"]
    assert q004
    assert 2 in q004[0].issue_numbers


def test_q004_fires_when_title_contains_audit() -> None:
    """A closed issue with 'audit' in title and new-owner keyword in body produces Q004 (Spec E title path)."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(
                2,
                "Broad audit of subsystem",
                state=IssueState.closed,
                body=("Acceptance Criteria: all of subsystem verified.\n\nNew owner: #3 for future cases."),
            ),
            3: _issue(3, "New case family owner", state=IssueState.open),
        },
        children_of={1: (2, 3)},
    )
    findings = audit_completion_contracts(dag)
    q004 = [f for f in findings if f.code == "Q004"]
    assert q004


# ---------------------------------------------------------------------------
# Spec F: qualified references are parsed as external (not local)
# ---------------------------------------------------------------------------


def test_qualified_cross_repo_ref_not_matched_as_local() -> None:
    """A qualified ref dzackgarza/research#3 does not match local #3 (Spec F)."""
    from itree.audit import _parse_issue_refs

    result = _parse_issue_refs("See dzackgarza/research#3 for the cross-repo work.")
    assert 3 not in result


def test_e016_excludes_qualified_cross_repo_ref() -> None:
    """A closed issue with a qualified cross-repo ref does not include local #3 as a transfer target (Spec F)."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(
                2,
                "Original implementation",
                state=IssueState.closed,
                body=("Acceptance Criteria: done when feature X is implemented.\n\nImplementation moved to dzackgarza/research#3."),
            ),
            3: _issue(3, "Local issue 3", state=IssueState.open),
        },
        children_of={1: (2, 3)},
    )
    findings = audit_completion_contracts(dag)
    e016 = [f for f in findings if f.code == "E016"]
    assert not e016


# ---------------------------------------------------------------------------
# Spec G: W061 only fires on actual leaves (no children)
# ---------------------------------------------------------------------------


def test_w061_does_not_fire_on_issue_with_children() -> None:
    """A non-grouping issue with the decomposition label but WITH children does not produce W061 (Spec G)."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Implement feature X", labels=("needs-decomposition",)),
            3: _issue(3, "Child work unit"),
        },
        children_of={1: (2,), 2: (3,)},
    )
    findings = detect_label_conflicts(dag, decomposition_label="needs-decomposition")
    assert all(f.code != "W061" for f in findings)


def test_w061_fires_on_issue_with_no_children() -> None:
    """A non-grouping issue with the decomposition label and NO children produces W061 (Spec G positive)."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Implement feature X", labels=("needs-decomposition",)),
        },
        children_of={1: (2,)},
    )
    findings = detect_label_conflicts(dag, decomposition_label="needs-decomposition")
    w061 = [f for f in findings if f.code == "W061"]
    assert w061
    assert 2 in w061[0].issue_numbers
