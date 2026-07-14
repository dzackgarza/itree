"""Completion-contract audit tests for ``itree doctor``.

These fixtures are stable local issue trees. They deliberately do not depend
on any live repository whose state can drift after the PR is opened.
"""

from __future__ import annotations

from itree.cli import doctor_exit_code, render_doctor_report
from itree.contracts import ContractEvidence, ContractKind, parse_issue_contracts
from itree.metrics import AbsentCodeSize, MetricsConfig
from itree.models import DoctorReport, Finding, GithubIssue, IssueRef, IssueState, RepoDag, RepoRef, ReportRef
from itree.validate import generate_doctor_report

ACCEPTANCE = "## Acceptance Criteria\n- The work is complete."


def _repo_ref() -> RepoRef:
    return RepoRef(owner="testowner", repo="testrepo")


def _ref(number: int) -> IssueRef:
    return IssueRef(repo_ref=_repo_ref(), number=number)


def _issue(
    number: int,
    title: str,
    *,
    body: str | None = ACCEPTANCE,
    state: IssueState = IssueState.open,
    labels: tuple[str, ...] = (),
) -> GithubIssue:
    return GithubIssue(
        id=number + 10000,
        number=number,
        title=title,
        state=state,
        html_url=f"https://github.com/testowner/testrepo/issues/{number}",
        body=body,
        labels=labels,
    )


def _dag(
    issues: dict[int, GithubIssue],
    children_of: dict[int, tuple[int, ...]],
    *,
    dependencies: dict[int, tuple[int, ...]] | None = None,
) -> RepoDag:
    return RepoDag(
        repo_ref=_repo_ref(),
        issues=issues,
        children_of=children_of,
        dependencies=dependencies or {},
    )


def _present_number(ref: ReportRef) -> int:
    assert ref.kind == "present"
    return ref.ref.number


def _findings(report: DoctorReport, code: str) -> list[Finding]:
    return [finding for finding in report.findings if finding.code == code]


def test_contract_parser_preserves_typed_valid_declarations_and_ref_order() -> None:
    body = """
Prose links such as #99 are ordinary issue-body text.

```itree-contract
kind = "implementation"
origin = "#2"
owner = "testowner/testrepo#3"
requires = ["#4", "other/repo#5"]
revalidate_on = ["#6"]
evidence = "routes"
```
"""

    parsed = parse_issue_contracts(body, repo_ref=_repo_ref(), issue_number=9)

    assert parsed.errors == ()
    assert len(parsed.declarations) == 1
    declaration = parsed.declarations[0]
    assert declaration.kind is ContractKind.implementation
    assert declaration.evidence is ContractEvidence.routes
    assert declaration.origin == _ref(2)
    assert declaration.owner == _ref(3)
    assert tuple(ref.slug for ref in declaration.requires) == (
        "testowner/testrepo#4",
        "other/repo#5",
    )
    assert tuple(ref.slug for ref in declaration.revalidate_on) == ("testowner/testrepo#6",)


def test_invalid_contract_block_is_reported_as_e018() -> None:
    dag = _dag(
        {
            1: _issue(1, "Ledger: testowner/testrepo"),
            2: _issue(
                2,
                "Work unit with malformed contract",
                body="""
## Acceptance Criteria
- This issue has malformed contract data.

```itree-contract
kind = "mystery"
evidence = "routes"
```
""",
            ),
        },
        {1: (2,)},
    )

    report = generate_doctor_report(dag)
    findings = _findings(report, "E018")

    assert len(findings) == 1
    assert "invalid_completion_contract" in findings[0].title
    assert findings[0].witness is not None
    assert findings[0].witness.current_owner == _ref(2)


def test_closed_implementation_route_reports_each_parallel_unresolved_owner() -> None:
    body = """
## Acceptance Criteria
- Route all remaining implementation branches.

```itree-contract
kind = "implementation"
owner = "#3"
evidence = "routes"
```

```itree-contract
kind = "implementation"
owner = "#4"
evidence = "routes"
```
"""
    dag = _dag(
        {
            1: _issue(1, "Ledger: testowner/testrepo"),
            2: _issue(2, "Original implementation obligation", body=body, state=IssueState.closed),
            3: _issue(3, "Owner branch A"),
            4: _issue(4, "Owner branch B"),
        },
        {1: (2, 3, 4)},
    )

    report = generate_doctor_report(dag)
    findings = _findings(report, "E016")

    assert len(findings) == 1
    assert any("#2 -> #3" in evidence for evidence in findings[0].evidence)
    assert any("#2 -> #4" in evidence for evidence in findings[0].evidence)
    assert findings[0].witness is not None
    assert tuple(ref.number for ref in findings[0].witness.edge_chain) == (2, 3)


def test_audit_discharge_cannot_discharge_implementation_obligation() -> None:
    dag = _dag(
        {
            1: _issue(1, "Ledger: testowner/testrepo"),
            2: _issue(
                2,
                "Original implementation obligation",
                body="""
## Acceptance Criteria
- Implementation must land.

```itree-contract
kind = "implementation"
owner = "#3"
evidence = "routes"
```
""",
                state=IssueState.closed,
            ),
            3: _issue(
                3,
                "Audit note claiming implementation closure",
                body="""
## Acceptance Criteria
- Record the audit outcome.

```itree-contract
kind = "audit"
origin = "#2"
evidence = "discharges"
```
""",
                state=IssueState.closed,
            ),
        },
        {1: (2, 3)},
    )

    report = generate_doctor_report(dag)
    findings = _findings(report, "E019")

    assert len(findings) == 1
    assert findings[0].witness is not None
    assert findings[0].witness.originating_obligation == _ref(2)
    assert findings[0].witness.current_owner == _ref(3)


def test_required_grouping_without_executable_descendants_is_e017_even_when_deferred() -> None:
    dag = _dag(
        {
            1: _issue(1, "Ledger: testowner/testrepo"),
            2: _issue(
                2,
                "Closed planner that still requires implementation",
                body="""
## Acceptance Criteria
- Implementation must still happen.

```itree-contract
kind = "implementation"
requires = ["#3"]
evidence = "routes"
```
""",
                state=IssueState.closed,
            ),
            3: _issue(3, "Milestone: deferred shell", body=ACCEPTANCE, labels=("deferred",)),
        },
        {1: (2, 3)},
    )

    report = generate_doctor_report(dag)
    findings = _findings(report, "E017")

    assert len(findings) == 1
    assert "#3" in findings[0].evidence[0]


def test_role_declaration_contradicting_tree_shape_is_w060() -> None:
    dag = _dag(
        {
            1: _issue(1, "Ledger: testowner/testrepo"),
            2: _issue(
                2,
                "Milestone: implementation shell",
                body="""
## Acceptance Criteria
- This grouping should not claim work-unit role.

```itree-contract
kind = "coordination"
evidence = "records"
role = "work_unit"
```
""",
            ),
            3: _issue(3, "Actual work unit"),
        },
        {1: (2,), 2: (3,)},
    )

    report = generate_doctor_report(dag)
    findings = _findings(report, "W060")

    assert len(findings) == 1
    assert "work_unit" in findings[0].evidence[0]


def test_decomposition_label_on_work_unit_leaf_is_w061() -> None:
    dag = _dag(
        {
            1: _issue(1, "Ledger: testowner/testrepo"),
            2: _issue(2, "Leaf work unit", labels=("needs-decomposition",)),
        },
        {1: (2,)},
    )

    report = generate_doctor_report(dag, decomposition_label="needs-decomposition")
    findings = _findings(report, "W061")

    assert len(findings) == 1
    assert "#2" in findings[0].evidence[0]


def test_derived_state_labels_are_w062_not_tree_state() -> None:
    dag = _dag(
        {
            1: _issue(1, "Ledger: testowner/testrepo"),
            2: _issue(2, "Leaf work unit", labels=("ready",)),
        },
        {1: (2,)},
    )

    report = generate_doctor_report(dag, derived_state_labels=("ready",))
    findings = _findings(report, "W062")

    assert len(findings) == 1
    assert "ready" in findings[0].evidence[0]


def test_revalidation_question_does_not_affect_exit_status() -> None:
    dag = _dag(
        {
            1: _issue(1, "Ledger: testowner/testrepo"),
            2: _issue(
                2,
                "Closed audit claim",
                body="""
## Acceptance Criteria
- Audit is recorded.

```itree-contract
kind = "audit"
revalidate_on = ["#3"]
evidence = "records"
```
""",
                state=IssueState.closed,
            ),
            3: _issue(3, "Later capability"),
        },
        {1: (2, 3)},
    )

    report = generate_doctor_report(dag)

    assert _findings(report, "Q004")
    assert report.status == "ok"
    assert doctor_exit_code(report) == 0


def test_churning_route_chain_is_advisory_q005() -> None:
    def route(owner: int) -> str:
        return f"""
## Acceptance Criteria
- Route to the next owner.

```itree-contract
kind = "implementation"
owner = "#{owner}"
evidence = "routes"
```
"""

    dag = _dag(
        {
            1: _issue(1, "Ledger: testowner/testrepo"),
            2: _issue(2, "Original implementation obligation", body=route(3), state=IssueState.closed),
            3: _issue(3, "Coordination hop A", body=route(4), state=IssueState.closed),
            4: _issue(4, "Coordination hop B", body=route(5), state=IssueState.closed),
            5: _issue(5, "Still unresolved owner"),
        },
        {1: (2, 3, 4, 5)},
    )

    report = generate_doctor_report(dag)
    findings = _findings(report, "Q005")

    assert len(findings) == 1
    assert "#2 -> #3 -> #4 -> #5" in findings[0].evidence[0]


def test_contract_findings_have_matching_json_witness_data_and_single_text_render() -> None:
    dag = _dag(
        {
            1: _issue(1, "Ledger: testowner/testrepo"),
            2: _issue(
                2,
                "Original implementation obligation",
                body="""
## Acceptance Criteria
- Route the remaining work.

```itree-contract
kind = "implementation"
owner = "#3"
evidence = "routes"
```
""",
                state=IssueState.closed,
            ),
            3: _issue(3, "Unresolved owner"),
        },
        {1: (2, 3)},
    )
    config = MetricsConfig()
    report = generate_doctor_report(dag)

    payload = report.model_dump(mode="json")
    e016 = next(finding for finding in payload["findings"] if finding["code"] == "E016")
    assert e016["witness"]["originating_obligation"]["number"] == 2
    assert [ref["number"] for ref in e016["witness"]["edge_chain"]] == [2, 3]

    rendered = render_doctor_report(
        _repo_ref(),
        dag,
        report,
        config,
        AbsentCodeSize(reason="no checkout"),
    )
    assert rendered.count("Findings:") == 1
    assert rendered.count("E016:") == 1


def test_native_readiness_still_controls_next_work_unit() -> None:
    dag = _dag(
        {
            1: _issue(1, "Ledger: testowner/testrepo"),
            2: _issue(
                2,
                "Blocked earlier work unit",
                body="""
## Acceptance Criteria
- This issue waits on native readiness.

```itree-contract
kind = "implementation"
owner = "#2"
evidence = "records"
```
""",
            ),
            3: _issue(3, "Later ready work unit"),
        },
        {1: (2, 3)},
        dependencies={2: (3,)},
    )

    report = generate_doctor_report(dag)

    assert _present_number(report.next_issue) == 3
