"""Q006: a smaller work unit can be selected before a large unit that rewrites the same surface.

Each fixture is a stable local tree run through ``generate_doctor_report``,
the path ``itree doctor`` takes.
"""

from __future__ import annotations

from itree.cli import doctor_exit_code
from itree.models import DoctorReport, Finding, GithubIssue, IssueRef, IssueState, RepoDag, RepoRef
from itree.validate import generate_doctor_report

LARGE = ("complexity:high", "size:l")
SMALL = ("complexity:low", "size:xs")
REPO_FILES = frozenset({"src/pkg/parser.py", "src/pkg/lexer.py", "docs/usage.md"})


def _repo_ref() -> RepoRef:
    return RepoRef(owner="testowner", repo="testrepo")


def _ref(number: int) -> IssueRef:
    return IssueRef(repo_ref=_repo_ref(), number=number)


def _issue(number: int, title: str, body: str = "", labels: tuple[str, ...] = ()) -> GithubIssue:
    return GithubIssue(
        id=number + 10000,
        number=number,
        title=title,
        state=IssueState.open,
        html_url=f"https://github.com/testowner/testrepo/issues/{number}",
        body=f"## Acceptance Criteria\n- Done.\n\n{body}",
        labels=labels,
    )


def _report(
    order: tuple[int, ...],
    issues: dict[int, GithubIssue],
    dependencies: dict[int, tuple[int, ...]] | None = None,
) -> DoctorReport:
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={1: _issue(1, "Ledger: testowner/testrepo"), **issues},
        children_of={1: order},
        dependencies=dependencies or {},
    )
    return generate_doctor_report(dag, repo_files=REPO_FILES)


def _q006(report: DoctorReport) -> list[Finding]:
    return [finding for finding in report.findings if finding.code == "Q006"]


def _small_then_large(small_body: str = "Fix a typo in `src/pkg/parser.py`.", large_body: str = "Rewrite src/pkg/parser.py around a grammar.") -> dict[int, GithubIssue]:
    return {
        2: _issue(2, "Typo in parser", small_body, SMALL),
        3: _issue(3, "Parser rewrite", large_body, LARGE),
    }


def test_small_unit_before_large_unit_on_shared_file_is_q006() -> None:
    report = _report((2, 3), _small_then_large())

    findings = _q006(report)
    assert len(findings) == 1
    assert findings[0].witnesses[0].edge_chain == (_ref(2), _ref(3))
    assert "src/pkg/parser.py" in findings[0].evidence[0]


def test_q006_is_advisory() -> None:
    report = _report((2, 3), _small_then_large())

    assert report.status == "ok"
    assert doctor_exit_code(report) == 0


def test_small_unit_blocked_by_large_unit_is_ordered() -> None:
    report = _report((2, 3), _small_then_large(), dependencies={2: (3,)})

    assert _q006(report) == []


def test_transitive_blocker_on_large_unit_is_ordered() -> None:
    issues = {**_small_then_large(), 4: _issue(4, "Middle step", "", SMALL)}
    report = _report((2, 4, 3), issues, dependencies={2: (4,), 4: (3,)})

    assert _q006(report) == []


def test_small_prerequisite_of_large_unit_is_ordered() -> None:
    report = _report((2, 3), _small_then_large(), dependencies={3: (2,)})

    assert _q006(report) == []


def test_ready_large_unit_first_in_preorder_is_ordered() -> None:
    report = _report((3, 2), _small_then_large())

    assert _q006(report) == []


def test_blocked_large_unit_first_in_preorder_still_lets_small_unit_run_first() -> None:
    issues = {**_small_then_large(), 4: _issue(4, "Unrelated prerequisite", "", SMALL)}
    report = _report((3, 2, 4), issues, dependencies={3: (4,)})

    findings = _q006(report)
    assert len(findings) == 1
    assert findings[0].witnesses[0].edge_chain == (_ref(2), _ref(3))


def test_units_without_a_shared_file_are_not_paired() -> None:
    report = _report((2, 3), _small_then_large(small_body="Fix a typo in `docs/usage.md`."))

    assert _q006(report) == []


def test_two_small_units_are_not_paired() -> None:
    issues = _small_then_large()
    issues[3] = _issue(3, "Parser tweak", "Tweak src/pkg/parser.py.", SMALL)
    report = _report((2, 3), issues)

    assert _q006(report) == []


def test_size_xl_alone_marks_a_large_unit() -> None:
    issues = _small_then_large()
    issues[3] = _issue(3, "Parser rewrite", "Rewrite src/pkg/parser.py.", ("complexity:low", "size:xl"))
    report = _report((2, 3), issues)

    assert len(_q006(report)) == 1


def test_path_inside_a_url_is_not_a_shared_file() -> None:
    issues = _small_then_large(small_body="See https://github.com/other/repo/blob/main/src/pkg/parser.py for prior art.")
    report = _report((2, 3), issues)

    assert _q006(report) == []


def test_complexity_high_alone_does_not_mark_a_large_unit() -> None:
    issues = _small_then_large()
    issues[3] = _issue(3, "Interface tweak", "Change one signature in src/pkg/parser.py.", ("complexity:high", "size:m"))
    report = _report((2, 3), issues)

    assert _q006(report) == []


def test_path_outside_the_repository_tree_is_not_a_shared_file() -> None:
    cited = "Source: research writing/statement/Statement.md at ac0732d."
    report = _report((2, 3), _small_then_large(small_body=cited, large_body=cited))

    assert _q006(report) == []


def test_q006_is_not_evaluated_without_the_repository_tree() -> None:
    dag = RepoDag(
        repo_ref=_repo_ref(),
        issues={1: _issue(1, "Ledger: testowner/testrepo"), **_small_then_large()},
        children_of={1: (2, 3)},
        dependencies={},
    )

    assert _q006(generate_doctor_report(dag)) == []


def test_package_relative_path_resolves_to_the_tree_file() -> None:
    report = _report((2, 3), _small_then_large(small_body="Fix a typo in `pkg/parser.py`."))

    findings = _q006(report)
    assert len(findings) == 1
    assert "src/pkg/parser.py" in findings[0].evidence[0]


def test_path_suffix_must_end_at_a_directory_boundary() -> None:
    report = _report((2, 3), _small_then_large(small_body="Fix a typo in `kg/parser.py`."))

    assert _q006(report) == []


def _independence(other: int, *files: str) -> str:
    listed = ", ".join(f'"{file}"' for file in files)
    return f'```itree-contract\nkind = "redo_risk"\nevidence = "records"\nindependent_of = ["#{other}"]\nfiles = [{listed}]\n```'


def test_recorded_independence_on_the_large_unit_removes_the_pair() -> None:
    large_body = f"Rewrite src/pkg/parser.py around a grammar.\n\n{_independence(2, 'pkg/parser.py')}"
    report = _report((2, 3), _small_then_large(large_body=large_body))

    assert _q006(report) == []
    assert [finding.code for finding in report.findings if finding.code == "E018"] == []


def test_recorded_independence_on_the_small_unit_removes_the_pair() -> None:
    small_body = f"Fix a typo in `src/pkg/parser.py`.\n\n{_independence(3, 'src/pkg/parser.py')}"
    report = _report((2, 3), _small_then_large(small_body=small_body))

    assert _q006(report) == []


def test_independence_of_another_unit_keeps_the_pair() -> None:
    issues = {**_small_then_large(), 4: _issue(4, "Lexer fix", "Fix src/pkg/parser.py tokens.", SMALL)}
    issues[3] = _issue(3, "Parser rewrite", f"Rewrite src/pkg/parser.py.\n\n{_independence(4, 'src/pkg/parser.py')}", LARGE)
    report = _report((2, 4, 3), issues)

    findings = _q006(report)
    assert [witness.edge_chain for witness in findings[0].witnesses] == [(_ref(2), _ref(3))]


def test_a_newly_shared_file_brings_the_pair_back() -> None:
    small_body = "Fix a typo in `src/pkg/parser.py` and `src/pkg/lexer.py`."
    large_body = f"Rewrite src/pkg/parser.py and src/pkg/lexer.py.\n\n{_independence(2, 'src/pkg/parser.py')}"
    report = _report((2, 3), _small_then_large(small_body=small_body, large_body=large_body))

    findings = _q006(report)
    assert len(findings) == 1
    assert "src/pkg/lexer.py" in findings[0].evidence[0]


def test_independence_record_without_files_is_e018_and_keeps_the_pair() -> None:
    fence = '```itree-contract\nkind = "redo_risk"\nevidence = "records"\nindependent_of = ["#2"]\n```'
    report = _report((2, 3), _small_then_large(large_body=f"Rewrite src/pkg/parser.py.\n\n{fence}"))

    assert len(_q006(report)) == 1
    assert [finding.code for finding in report.findings if finding.code == "E018"] == ["E018"]
