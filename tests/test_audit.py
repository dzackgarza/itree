"""Tests for the completion-contract audit (itree#41).

These tests prove that ``itree doctor`` detects when a structurally valid
tree still permits an agent to close administrative slices while the
implementation named by the original contract never occurs.

The audit uses explicit ``itree-contract`` declarations parsed from issue
bodies, not heuristic prose matching.  Unstructured prose never produces
a definitive error.
"""

from __future__ import annotations

from itree.audit import (
    audit_completion_contracts,
    detect_label_conflicts,
    detect_role_contradictions,
)
from itree.contracts import (
    ContractDeclaration,
    EvidenceDisposition,
    ObligationKind,
    build_ownership_chains,
    parse_contract_declarations,
    parse_qualified_refs,
)
from itree.models import GithubIssue, IssueRef, IssueState, RepoDag, RepoRef
from itree.validate import DIAGNOSTIC_CATALOG, generate_doctor_report


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


def _contract_block(
    kind: str = "implementation",
    origin: str | None = None,
    owner: str | None = None,
    evidence: str | None = None,
    requires: list[str] | None = None,
    revalidate_on: list[str] | None = None,
) -> str:
    """Build an itree-contract fenced block."""
    lines = ["```itree-contract", f'kind = "{kind}"']
    if origin:
        lines.append(f'origin = "{origin}"')
    if owner:
        lines.append(f'owner = "{owner}"')
    if evidence:
        lines.append(f'evidence = "{evidence}"')
    if requires:
        lines.append(f"requires = {requires!r}")
    if revalidate_on:
        lines.append(f"revalidate_on = {revalidate_on!r}")
    lines.append("```")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Contract declaration parsing
# ---------------------------------------------------------------------------


def test_parse_single_contract_declaration() -> None:
    """Parse a single itree-contract block with all fields."""
    body = _contract_block(
        kind="implementation",
        origin="testowner/testrepo#2",
        owner="testowner/testrepo#3",
        evidence="routes",
        requires=["testowner/testrepo#5"],
        revalidate_on=["testowner/testrepo#6"],
    )
    decls = parse_contract_declarations(body)
    assert len(decls) == 1
    d = decls[0]
    assert d.kind == ObligationKind.implementation
    assert d.origin is not None
    assert d.origin.number == 2
    assert d.owner is not None
    assert d.owner.number == 3
    assert d.evidence == EvidenceDisposition.routes
    assert len(d.requires) == 1
    assert d.requires[0].number == 5
    assert len(d.revalidate_on) == 1
    assert d.revalidate_on[0].number == 6


def test_parse_multiple_contract_blocks() -> None:
    """Parse multiple itree-contract blocks in one body."""
    body = _contract_block(kind="implementation", owner="testowner/testrepo#3") + "\n\n" + _contract_block(kind="audit", revalidate_on=["testowner/testrepo#5"])
    decls = parse_contract_declarations(body)
    assert len(decls) == 2
    assert decls[0].kind == ObligationKind.implementation
    assert decls[1].kind == ObligationKind.audit


def test_no_contract_blocks_returns_empty() -> None:
    """No itree-contract blocks → empty list."""
    assert parse_contract_declarations("Just a regular body") == []
    assert parse_contract_declarations(None) == []


def test_contract_block_inside_other_fence_ignored() -> None:
    """A itree-contract block inside a ```python demonstration is not parsed."""
    body = '```python\n```itree-contract\nkind = "implementation"\n```\n```\n'
    decls = parse_contract_declarations(body)
    assert decls == []


# ---------------------------------------------------------------------------
# Qualified reference parsing
# ---------------------------------------------------------------------------


def test_parse_qualified_ref() -> None:
    """Qualified ref OWNER/REPO#N preserves repository identity."""
    refs = parse_qualified_refs("See other/repo#3", _repo_ref())
    assert len(refs) == 1
    assert refs[0].repo_ref.owner == "other"
    assert refs[0].repo_ref.repo == "repo"
    assert refs[0].number == 3


def test_parse_unqualified_ref_resolved_against_repo() -> None:
    """Unqualified #N resolved against the provided repo_ref."""
    refs = parse_qualified_refs("See #3", _repo_ref())
    assert len(refs) == 1
    assert refs[0].repo_ref == _repo_ref()
    assert refs[0].number == 3


def test_html_entity_not_matched() -> None:
    """HTML entities &#123; are not matched as issue refs."""
    refs = parse_qualified_refs("See &#123; for details", _repo_ref())
    assert 123 not in [r.number for r in refs if r.repo_ref == _repo_ref()]


def test_qualified_ref_not_matched_as_unqualified() -> None:
    """owner/repo#N does not also produce a local #N match."""
    refs = parse_qualified_refs("See other/repo#3 and #3", _repo_ref())
    local_refs = [r for r in refs if r.repo_ref == _repo_ref()]
    assert len(local_refs) == 1  # only the standalone #3


def test_refs_deduplicated() -> None:
    """Duplicate refs are deduplicated preserving order."""
    refs = parse_qualified_refs("See #3 and #3 again", _repo_ref())
    assert len(refs) == 1


# ---------------------------------------------------------------------------
# Ownership chain traversal
# ---------------------------------------------------------------------------


def test_ownership_chain_single_hop_unresolved() -> None:
    """Single hop: origin → owner, owner open, evidence=routes → unresolved."""
    decls = {
        2: [
            ContractDeclaration(
                kind=ObligationKind.implementation,
                owner=IssueRef.parse("testowner/testrepo#3"),
                evidence=EvidenceDisposition.routes,
            )
        ],
    }
    chains = build_ownership_chains(decls, _repo_ref())
    assert len(chains) == 1
    assert chains[0].unresolved
    assert len(chains[0].path) == 2


def test_ownership_chain_two_hops() -> None:
    """Two hops: A → B → C, both routing."""
    decls = {
        2: [
            ContractDeclaration(
                kind=ObligationKind.implementation,
                owner=IssueRef.parse("testowner/testrepo#3"),
                evidence=EvidenceDisposition.routes,
            )
        ],
        3: [
            ContractDeclaration(
                kind=ObligationKind.implementation,
                owner=IssueRef.parse("testowner/testrepo#4"),
                evidence=EvidenceDisposition.routes,
            )
        ],
    }
    chains = build_ownership_chains(decls, _repo_ref())
    assert len(chains) == 1
    assert len(chains[0].path) == 3  # origin → #3 → #4


def test_ownership_chain_cycle_detected() -> None:
    """Cycle: A → B → A → stops with cycle_detected=True."""
    decls = {
        2: [
            ContractDeclaration(
                kind=ObligationKind.implementation,
                owner=IssueRef.parse("testowner/testrepo#3"),
                evidence=EvidenceDisposition.routes,
            )
        ],
        3: [
            ContractDeclaration(
                kind=ObligationKind.implementation,
                owner=IssueRef.parse("testowner/testrepo#2"),
                evidence=EvidenceDisposition.routes,
            )
        ],
    }
    chains = build_ownership_chains(decls, _repo_ref())
    assert len(chains) == 1
    assert chains[0].cycle_detected


def test_ownership_chain_discharged_not_reported() -> None:
    """Discharged obligations are not reported as chains."""
    decls = {
        2: [
            ContractDeclaration(
                kind=ObligationKind.implementation,
                owner=IssueRef.parse("testowner/testrepo#3"),
                evidence=EvidenceDisposition.discharges,
            )
        ],
    }
    chains = build_ownership_chains(decls, _repo_ref())
    assert chains == []


# ---------------------------------------------------------------------------
# E016: false-green closure via declarations
# ---------------------------------------------------------------------------


def test_e016_closed_origin_with_open_owner() -> None:
    """E016 fires when a closed issue routes implementation to an open issue."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(
                2,
                "Original implementation",
                state=IssueState.closed,
                body=_contract_block(
                    kind="implementation",
                    owner="testowner/testrepo#3",
                    evidence="routes",
                ),
            ),
            3: _issue(3, "Actual implementation", state=IssueState.open),
        },
        children_of={1: (2, 3)},
    )
    findings = audit_completion_contracts(dag)
    e016 = [f for f in findings if f.code == "E016"]
    assert len(e016) == 1
    assert 2 in e016[0].issue_numbers
    # Typed witness fields
    assert e016[0].origin is not None
    assert e016[0].owner is not None
    assert e016[0].path
    assert e016[0].obligation_kind == "implementation"


def test_e016_typed_witness_in_doctor_report() -> None:
    """Doctor report Finding carries typed witness fields for E016."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(
                2,
                "Closed",
                state=IssueState.closed,
                body=_contract_block(
                    kind="implementation",
                    owner="testowner/testrepo#3",
                    evidence="routes",
                ),
            ),
            3: _issue(3, "Open owner"),
        },
        children_of={1: (2, 3)},
    )
    report = generate_doctor_report(dag)
    e016 = [f for f in report.findings if f.code == "E016"]
    assert len(e016) == 1
    assert e016[0].origin is not None
    assert e016[0].owner is not None
    assert e016[0].path


def test_e016_discharged_does_not_fire() -> None:
    """A closed issue with evidence='discharges' does not produce E016."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(
                2,
                "Done",
                state=IssueState.closed,
                body=_contract_block(
                    kind="implementation",
                    owner="testowner/testrepo#3",
                    evidence="discharges",
                ),
            ),
            3: _issue(3, "Owner", state=IssueState.closed),
        },
        children_of={1: (2, 3)},
    )
    findings = audit_completion_contracts(dag)
    assert all(f.code != "E016" for f in findings)


def test_e016_recursive_two_hop_chain() -> None:
    """E016 follows a two-hop ownership chain: #2 → #3 → #4."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(
                2,
                "Origin",
                state=IssueState.closed,
                body=_contract_block(
                    kind="implementation",
                    owner="testowner/testrepo#3",
                    evidence="routes",
                ),
            ),
            3: _issue(
                3,
                "Middle",
                state=IssueState.closed,
                body=_contract_block(
                    kind="implementation",
                    owner="testowner/testrepo#4",
                    evidence="routes",
                ),
            ),
            4: _issue(4, "Current owner", state=IssueState.open),
        },
        children_of={1: (2, 3, 4)},
    )
    findings = audit_completion_contracts(dag)
    e016 = [f for f in findings if f.code == "E016"]
    assert len(e016) >= 1
    # The chain should have 3 elements: origin → middle → current
    chain_findings = [f for f in e016 if f.path and len(f.path) >= 3]
    assert chain_findings


def test_e016_parallel_owners_not_linearized() -> None:
    """Two parallel owners from one origin do not produce a false linear chain."""
    # The origin routes to #3. A separate declaration routes to #5.
    # These are two separate chains, not #2 → #3 → #5.
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(
                2,
                "Origin",
                state=IssueState.closed,
                body=(
                    _contract_block(
                        kind="implementation",
                        owner="testowner/testrepo#3",
                        evidence="routes",
                    )
                    + "\n\n"
                    + _contract_block(
                        kind="implementation",
                        owner="testowner/testrepo#5",
                        evidence="routes",
                    )
                ),
            ),
            3: _issue(3, "Owner A", state=IssueState.open),
            5: _issue(5, "Owner B", state=IssueState.open),
        },
        children_of={1: (2, 3, 5)},
    )
    findings = audit_completion_contracts(dag)
    e016 = [f for f in findings if f.code == "E016"]
    # Each chain should be 2 hops (origin → owner), not 3 (origin → A → B)
    for f in e016:
        assert len(f.path) == 2, f"Expected 2-hop chain, got {len(f.path)}: {f.path}"


def test_heuristic_prose_does_not_produce_e016() -> None:
    """Plain prose 'moved to #3' without a contract declaration does not produce E016."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(
                2,
                "Closed issue",
                state=IssueState.closed,
                body="Implementation moved to #3. See also #5.",
            ),
            3: _issue(3, "Open issue", state=IssueState.open),
            5: _issue(5, "Another open issue", state=IssueState.open),
        },
        children_of={1: (2, 3, 5)},
    )
    findings = audit_completion_contracts(dag)
    assert all(f.code != "E016" for f in findings)


# ---------------------------------------------------------------------------
# E017: no executable descendants, deferral does not suppress
# ---------------------------------------------------------------------------


def test_e017_grouping_required_by_contract_no_impl_descendants() -> None:
    """E017 fires when a grouping with no impl descendants is required by a contract."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(
                2,
                "Requiring issue",
                body=_contract_block(
                    kind="implementation",
                    requires=["testowner/testrepo#3"],
                    evidence="routes",
                ),
            ),
            3: _issue(3, "Milestone: deferred feature"),
        },
        children_of={1: (2, 3)},
    )
    findings = audit_completion_contracts(dag)
    e017 = [f for f in findings if f.code == "E017"]
    assert len(e017) == 1
    assert 3 in e017[0].issue_numbers


def test_e017_deferral_label_does_not_suppress() -> None:
    """The deferral label does NOT suppress E017 when a contract requires the grouping."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(
                2,
                "Requiring issue",
                body=_contract_block(
                    kind="implementation",
                    requires=["testowner/testrepo#3"],
                    evidence="routes",
                ),
            ),
            3: _issue(3, "Milestone: deferred", labels=("deferred",)),
        },
        children_of={1: (2, 3)},
    )
    findings = audit_completion_contracts(dag)
    e017 = [f for f in findings if f.code == "E017"]
    assert len(e017) == 1
    # Evidence should mention that deferral does not suppress
    assert any("deferral" in e.lower() for e in e017[0].evidence)


def test_e017_closed_contract_still_fires() -> None:
    """E017 fires even when the requiring contract is closed."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(
                2,
                "Closed requiring issue",
                state=IssueState.closed,
                body=_contract_block(
                    kind="implementation",
                    requires=["testowner/testrepo#3"],
                    evidence="routes",
                ),
            ),
            3: _issue(3, "Milestone: required"),
        },
        children_of={1: (2, 3)},
    )
    findings = audit_completion_contracts(dag)
    e017 = [f for f in findings if f.code == "E017"]
    assert len(e017) == 1


def test_e017_unreferenced_deferred_shelf_accepted() -> None:
    """An unreferenced deferred shelf does not produce E017."""
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


def test_e017_administrative_descendant_does_not_suppress() -> None:
    """An administrative (non-implementation) descendant does not suppress E017."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(
                2,
                "Requiring issue",
                body=_contract_block(
                    kind="implementation",
                    requires=["testowner/testrepo#3"],
                    evidence="routes",
                ),
            ),
            3: _issue(3, "Milestone: required"),
            4: _issue(4, "Audit of something", body=_contract_block(kind="audit")),
        },
        children_of={1: (2, 3), 3: (4,)},
    )
    findings = audit_completion_contracts(dag)
    e017 = [f for f in findings if f.code == "E017"]
    # #4 is an audit descendant, not an implementation owner — E017 should still fire
    assert len(e017) == 1


def test_e017_implementation_descendant_suppresses() -> None:
    """A grouping with an implementation-owning descendant does not produce E017."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(
                2,
                "Requiring issue",
                body=_contract_block(
                    kind="implementation",
                    requires=["testowner/testrepo#3"],
                    evidence="routes",
                ),
            ),
            3: _issue(3, "Milestone: required"),
            4: _issue(
                4,
                "Implement feature",
                body=_contract_block(
                    kind="implementation",
                    evidence="discharges",
                ),
            ),
        },
        children_of={1: (2, 3), 3: (4,)},
    )
    findings = audit_completion_contracts(dag)
    assert all(f.code != "E017" for f in findings)


# ---------------------------------------------------------------------------
# Q004: audit revalidation
# ---------------------------------------------------------------------------


def test_q004_closed_audit_with_revalidate_on_open_issue() -> None:
    """Q004 fires for a closed audit with revalidate_on refs to open issues."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(
                2,
                "Broad audit",
                state=IssueState.closed,
                body=_contract_block(
                    kind="audit",
                    revalidate_on=["testowner/testrepo#3"],
                ),
            ),
            3: _issue(3, "New case family", state=IssueState.open),
        },
        children_of={1: (2, 3)},
    )
    findings = audit_completion_contracts(dag)
    q004 = [f for f in findings if f.code == "Q004"]
    assert len(q004) == 1


def test_q004_non_audit_does_not_fire() -> None:
    """Q004 does not fire for non-audit declarations."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(
                2,
                "Implementation",
                state=IssueState.closed,
                body=_contract_block(
                    kind="implementation",
                    revalidate_on=["testowner/testrepo#3"],
                ),
            ),
            3: _issue(3, "New issue", state=IssueState.open),
        },
        children_of={1: (2, 3)},
    )
    findings = audit_completion_contracts(dag)
    assert all(f.code != "Q004" for f in findings)


# ---------------------------------------------------------------------------
# W060: role contradiction via declarations
# ---------------------------------------------------------------------------


def test_w060_grouping_with_implementation_declaration_no_requires() -> None:
    """W060 fires when a grouping declares itself an implementation leaf."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Milestone: v1", body=_contract_block(kind="implementation")),
            3: _issue(3, "Child"),
        },
        children_of={1: (2,), 2: (3,)},
    )
    findings = detect_role_contradictions(dag)
    assert any(f.code == "W060" for f in findings)


def test_w060_grouping_with_implementation_and_requires_accepted() -> None:
    """A grouping with an implementation declaration that has requires is accepted."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(
                2,
                "Milestone: v1",
                body=_contract_block(
                    kind="implementation",
                    requires=["testowner/testrepo#3"],
                ),
            ),
            3: _issue(3, "Child"),
        },
        children_of={1: (2,), 2: (3,)},
    )
    findings = detect_role_contradictions(dag)
    assert all(f.code != "W060" for f in findings)


# ---------------------------------------------------------------------------
# W061, W062: label conflicts (unchanged from prior implementation)
# ---------------------------------------------------------------------------


def test_w061_decomposition_label_on_work_unit_leaf() -> None:
    """W061 fires on a work-unit leaf with the decomposition label."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Work unit", labels=("needs-decomposition",)),
        },
        children_of={1: (2,)},
    )
    findings = detect_label_conflicts(dag, decomposition_label="needs-decomposition")
    assert any(f.code == "W061" for f in findings)


def test_w061_does_not_fire_on_issue_with_children() -> None:
    """W061 does not fire on an issue with children."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Non-grouping with child", labels=("needs-decomposition",)),
            3: _issue(3, "Child"),
        },
        children_of={1: (2,), 2: (3,)},
    )
    findings = detect_label_conflicts(dag, decomposition_label="needs-decomposition")
    assert all(f.code != "W061" for f in findings)


def test_w062_derived_state_label_on_open_issue() -> None:
    """W062 fires on an open issue with a configured derived-state label."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Work unit", labels=("blocked",)),
        },
        children_of={1: (2,)},
    )
    findings = detect_label_conflicts(dag, derived_state_labels=("blocked",))
    assert any(f.code == "W062" for f in findings)


def test_w062_does_not_fire_on_closed_issues() -> None:
    """W062 does not fire on closed issues."""
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={
            1: _issue(1, "Ledger: Root"),
            2: _issue(2, "Closed", state=IssueState.closed, labels=("blocked",)),
        },
        children_of={1: (2,)},
    )
    findings = detect_label_conflicts(dag, derived_state_labels=("blocked",))
    assert all(f.code != "W062" for f in findings)


# ---------------------------------------------------------------------------
# --explain catalog entries
# ---------------------------------------------------------------------------


def test_all_new_codes_in_catalog() -> None:
    """All new finding codes are in DIAGNOSTIC_CATALOG with required fields."""
    for code in ("E016", "E017", "W060", "W061", "W062", "Q004"):
        details = DIAGNOSTIC_CATALOG[code]
        assert details["severity"]
        assert details["meaning"]
        assert details["remediation"]


# ---------------------------------------------------------------------------
# Config support
# ---------------------------------------------------------------------------


def test_itree_config_has_completion_audit_fields() -> None:
    """ItreeConfig includes decomposition_label and derived_state_labels."""
    from itree.metrics import ItreeConfig

    config = ItreeConfig()
    assert config.decomposition_label == ""
    assert config.derived_state_labels == ()
    assert config.deferral_label == "deferred"


def test_itree_config_accepts_audit_fields() -> None:
    """ItreeConfig accepts audit fields from config data."""
    from itree.metrics import ItreeConfig

    config = ItreeConfig.model_validate(
        {
            "decomposition_label": "needs-decomposition",
            "derived_state_labels": ["blocked", "in-progress"],
        }
    )
    assert config.decomposition_label == "needs-decomposition"
    assert config.derived_state_labels == ("blocked", "in-progress")


# ---------------------------------------------------------------------------
# Clean tree: no audit findings
# ---------------------------------------------------------------------------


def test_clean_tree_no_audit_findings() -> None:
    """A clean tree with open work units and no contract declarations produces no audit findings."""
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
