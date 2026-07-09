"""Tests for the Q-code proportionality metrics (#7).

Q findings are structure questions: they render in the doctor
"Structure questions:" section and never change the exit code.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from itree.metrics import (
    AbsentCodeSize,
    MetricsConfig,
    PresentCodeSize,
    load_config,
    measure_code_size,
    parse_scc_total,
    structure_questions,
)
from itree.models import DoctorReport, GithubIssue, IssueState, Milestone, RepoDag, RepoRef
from itree.validate import generate_doctor_report




def _issue(number: int, title: str, body: str | None = "## Acceptance Criteria\n- ok", milestone: str | None = None) -> GithubIssue:
    return GithubIssue(
        id=number,
        number=number,
        title=title,
        state=IssueState.open,
        html_url=f"https://github.com/t/t/issues/{number}",
        body=body,
        milestone=Milestone(title=milestone) if milestone else None,
    )


def _report(dag: RepoDag) -> DoctorReport:
    return generate_doctor_report(dag)


def _flat_dag(work_units: int) -> RepoDag:
    """Root ledger with N open work units hanging directly off it."""
    issues = {1: _issue(1, "Ledger: t/t")}
    for n in range(2, 2 + work_units):
        issues[n] = _issue(n, f"Work unit {n}")
    return RepoDag(
        repo_ref=RepoRef(owner="t", repo="t"),
        issues=issues,
        children_of={1: tuple(range(2, 2 + work_units))},
    )


def _grouped_dag(work_units: int) -> RepoDag:
    """Root ledger -> milestone ledger -> N open work units."""
    issues = {1: _issue(1, "Ledger: t/t"), 2: _issue(2, "Milestone: v1")}
    for n in range(3, 3 + work_units):
        issues[n] = _issue(n, f"Work unit {n}", milestone="v1")
    return RepoDag(
        repo_ref=RepoRef(owner="t", repo="t"),
        issues=issues,
        children_of={1: (2,), 2: tuple(range(3, 3 + work_units))},
    )


class TestLoadConfig:

    def test_missing_file_returns_documented_defaults(self, tmp_path: Path) -> None:
        config = load_config(tmp_path / "does-not-exist.toml")
        assert config == MetricsConfig(
            max_open_work_units=8,
            loc_per_work_unit=400,
            flat_children_ratio=0.5,
            flat_min_children=6,
        )


    def test_real_toml_overrides_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("max_open_work_units = 3\nloc_per_work_unit = 250\n")
        config = load_config(path)
        assert config.max_open_work_units == 3
        assert config.loc_per_work_unit == 250
        assert config.flat_min_children == 6


    def test_malformed_toml_fails_loudly(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("max_open_work_units = = 3")
        with pytest.raises(tomllib.TOMLDecodeError):
            load_config(path)


    def test_wrong_typed_field_fails_loudly(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text('max_open_work_units = "many"')
        with pytest.raises(ValidationError):
            load_config(path)


class TestCodeSize:

    def test_parse_scc_total_sums_code_lines_across_languages(self) -> None:
        scc_json = '[{"Name": "Python", "Code": 3100, "Lines": 4000}, {"Name": "Markdown", "Code": 420, "Lines": 500}]'
        assert parse_scc_total(scc_json) == 3520


    def test_non_matching_checkout_is_absent(self) -> None:
        evidence = measure_code_size("no-such-owner/no-such-repo", Path(__file__).resolve().parents[1])
        assert evidence.kind == "absent"
        assert "no-such-owner/no-such-repo" in evidence.reason


class TestPredicates:

    def test_q001_flags_open_work_units_above_ceiling(self) -> None:
        dag = _grouped_dag(3)
        config = MetricsConfig(max_open_work_units=2)
        codes = [f.code for f in structure_questions(dag, _report(dag), config, AbsentCodeSize(reason="n/a"))]
        assert "Q001" in codes


    def test_q001_silent_at_the_ceiling(self) -> None:
        dag = _grouped_dag(2)
        config = MetricsConfig(max_open_work_units=2)
        codes = [f.code for f in structure_questions(dag, _report(dag), config, AbsentCodeSize(reason="n/a"))]
        assert "Q001" not in codes


    def test_q002_flags_work_units_disproportionate_to_code(self) -> None:
        dag = _grouped_dag(3)
        config = MetricsConfig(loc_per_work_unit=400)
        code_size = PresentCodeSize(total_loc=800)  # supports ~2 work units
        findings = structure_questions(dag, _report(dag), config, code_size)
        q002 = [f for f in findings if f.code == "Q002"]
        assert len(q002) == 1
        assert any("800" in ev for ev in q002[0].evidence)


    def test_q002_absent_without_code_evidence(self) -> None:
        dag = _grouped_dag(3)
        config = MetricsConfig(loc_per_work_unit=400)
        codes = [f.code for f in structure_questions(dag, _report(dag), config, AbsentCodeSize(reason="no checkout"))]
        assert "Q002" not in codes


    def test_q003_flags_flat_tree(self) -> None:
        dag = _flat_dag(6)
        config = MetricsConfig(flat_min_children=6, flat_children_ratio=0.5)
        codes = [f.code for f in structure_questions(dag, _report(dag), config, AbsentCodeSize(reason="n/a"))]
        assert "Q003" in codes


    def test_q003_silent_on_grouped_tree(self) -> None:
        dag = _grouped_dag(6)
        config = MetricsConfig(flat_min_children=6, flat_children_ratio=0.5)
        codes = [f.code for f in structure_questions(dag, _report(dag), config, AbsentCodeSize(reason="n/a"))]
        assert "Q003" not in codes


class TestDoctorIntegration:
    """Q findings render in their own section and never change the exit code (#7)."""


    def test_q_findings_render_without_changing_exit_code(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        from itree import cli

        dag = _grouped_dag(3)  # otherwise-clean tree
        monkeypatch.setattr(cli, "build_dag", lambda *args, **kwargs: dag)
        monkeypatch.setattr(cli, "load_config", lambda: MetricsConfig(max_open_work_units=1))

        with pytest.raises(SystemExit) as exc:
            cli.doctor("t/t")

        assert exc.value.code == 0  # Q001 fires below, yet status stays ok
        out = capsys.readouterr().out
        assert "Structure questions:" in out
        assert "Q001" in out


    def test_clean_tree_renders_empty_structure_questions(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        from itree import cli

        dag = _grouped_dag(2)
        monkeypatch.setattr(cli, "build_dag", lambda *args, **kwargs: dag)
        monkeypatch.setattr(cli, "load_config", lambda: MetricsConfig())

        with pytest.raises(SystemExit) as exc:
            cli.doctor("t/t")

        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "Structure questions:" in out
        assert "Q001" not in out and "Q002" not in out and "Q003" not in out


    def test_explain_resolves_q_codes(self, capsys: pytest.CaptureFixture[str]) -> None:
        from itree import cli

        with pytest.raises(SystemExit) as exc:
            cli.doctor("t/t", explain="q001")
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "Q001: too_many_open_work_units" in out
        assert "Repair routes:" in out
