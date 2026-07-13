"""Tests for CLI commands and orchestration functions."""

from __future__ import annotations

import subprocess
from inspect import signature
from pathlib import Path

import pytest

from itree import cli
from itree.cli import (
    app,
    doctor,
    parse_ref,
    parse_repo,
    path,
    print_diagnostic,
)
from itree.models import AttachRequest, GithubIssue, IssueRef, IssueState, MoveRequest, RepoDag, RepoRef


class TestParseFunctions:
    """Tests for CLI parsing helper functions."""

    def test_parse_ref_valid(self) -> None:
        """parse_ref correctly parses OWNER/REPO#N format."""
        ref = parse_ref("owner/repo#123")
        assert ref.owner == "owner"
        assert ref.repo == "repo"
        assert ref.number == 123

    def test_parse_ref_invalid_raises(self) -> None:
        """parse_ref raises ValueError for invalid format."""
        with pytest.raises(ValueError, match="expected OWNER/REPO#NUMBER"):
            parse_ref("invalid")

    def test_parse_repo_valid(self) -> None:
        """parse_repo correctly parses OWNER/REPO format."""
        repo = parse_repo("owner/repo")
        assert repo.owner == "owner"
        assert repo.repo == "repo"

    def test_parse_repo_invalid_raises(self) -> None:
        """parse_repo raises ValueError for invalid format."""
        with pytest.raises(ValueError, match="expected OWNER/REPO"):
            parse_repo("invalid")

    def test_parse_repo_with_hash_raises(self) -> None:
        """parse_repo raises ValueError when hash is included."""
        with pytest.raises(ValueError, match="expected OWNER/REPO"):
            parse_repo("owner/repo#123")


class TestPrintDiagnostic:
    """print_diagnostic renders the catalog severity truthfully (#15)."""

    def test_warning_code_renders_warning_prefix(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A W-code routed through print_diagnostic must not claim to be an ERROR."""
        print_diagnostic("W020")
        out = capsys.readouterr().out
        assert out.startswith("WARNING W020:")

    def test_error_code_renders_error_prefix(self, capsys: pytest.CaptureFixture[str]) -> None:
        """E-codes keep their ERROR prefix."""
        print_diagnostic("E001")
        out = capsys.readouterr().out
        assert out.startswith("ERROR E001:")

    def test_warning_diagnostic_routes_to_async_maintenance(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Warnings preserve substantive work while an itree-maintenance agent heals structure."""
        print_diagnostic("W040")
        out = capsys.readouterr().out
        assert "Maintenance: dispatch issue-itree-maintenance asynchronously" in out
        assert "root ledger's remediation ledger comment" in out

    def test_every_error_and_warning_has_a_model_and_maintenance_route(self) -> None:
        """Every actionable doctor finding teaches its protected model and handoff."""
        from itree.validate import DIAGNOSTIC_CATALOG

        for details in DIAGNOSTIC_CATALOG.values():
            if details["severity"] in ("error", "warning"):
                assert details["ideal_model"]
                assert details["remediation"]
                assert details["maintenance"]


class TestDoctorExplainFooter:
    """The Run: footer only suggests --explain for codes present in findings (#15)."""

    @staticmethod
    def _dag(issues: dict[int, GithubIssue], children_of: dict[int, tuple[int, ...]]) -> RepoDag:
        return RepoDag(repo_ref=RepoRef(owner="t", repo="t"), issues=issues, children_of=children_of)

    @staticmethod
    def _issue(number: int, title: str, body: str | None = None) -> GithubIssue:
        return GithubIssue(
            id=number,
            number=number,
            title=title,
            state=IssueState.open,
            html_url=f"https://github.com/t/t/issues/{number}",
            body=body,
        )

    @staticmethod
    def _render(dag: RepoDag) -> tuple[str, int]:
        """Rendered footer text and exit code for a constructed tree, no IO."""
        from itree.cli import doctor_exit_code, render_doctor_report
        from itree.metrics import AbsentCodeSize, MetricsConfig
        from itree.validate import generate_doctor_report

        config = MetricsConfig()
        report = generate_doctor_report(dag, deferral_label=config.deferral_label)
        out = render_doctor_report(dag.repo_ref, dag, report, config, AbsentCodeSize(reason="n/a"))
        return out, doctor_exit_code(report)

    def test_clean_tree_omits_explain_suggestion(self) -> None:
        dag = self._dag(
            {
                1: self._issue(1, "Ledger: t/t"),
                2: self._issue(2, "Work unit", body="## Acceptance Criteria\n- ok"),
            },
            {1: (2,)},
        )
        out, code = self._render(dag)
        assert code == 0
        assert "--explain" not in out

    def test_error_tree_suggests_a_present_code(self) -> None:
        dag = self._dag(
            {
                1: self._issue(1, "Ledger: Root 1"),
                2: self._issue(2, "Ledger: Root 2"),
            },
            {},
        )
        out, code = self._render(dag)
        assert code == 2
        assert "--explain E002" in out
        assert "--explain E010" not in out


class TestOrchestrationFunctions:
    """Tests for request-model validation boundaries.

    These prove that AttachRequest, MoveRequest, and DetachRequest reject
    invalid inputs before the GitHub API boundary is reached. Orchestration
    logic itself requires integration tests against a captured or live gh
    boundary.
    """

    def test_do_attach_same_repo_validation_in_request(self) -> None:
        """AttachRequest validates same repository requirement."""
        parent = IssueRef(repo_ref=RepoRef(owner="owner1", repo="repo"), number=1)
        child = IssueRef(repo_ref=RepoRef(owner="owner2", repo="repo"), number=2)
        with pytest.raises(ValueError, match="same repository"):
            AttachRequest(parent=parent, child=child)

    def test_do_move_before_after_exclusive_in_request(self) -> None:
        """MoveRequest validates before/after mutual exclusivity."""
        child = IssueRef(repo_ref=RepoRef(owner="owner", repo="repo"), number=1)
        parent = IssueRef(repo_ref=RepoRef(owner="owner", repo="repo"), number=2)
        before = IssueRef(repo_ref=RepoRef(owner="owner", repo="repo"), number=3)
        after = IssueRef(repo_ref=RepoRef(owner="owner", repo="repo"), number=4)
        with pytest.raises(ValueError, match="either --before or --after"):
            MoveRequest(child=child, parent=parent, before=before, after=after)


class TestCLICommandStructure:
    """Tests for CLI command structure (without executing GitHub API calls)."""

    def test_cli_has_all_commands(self) -> None:
        """CLI app has all required commands."""
        # Get command names from the app's internal command registry
        # Cyclopts stores commands in app._commands
        command_names = set()
        for cmd_name, cmd in app._commands.items():
            if not cmd_name.startswith("_"):
                command_names.add(cmd_name)
        expected = {
            "init",
            "new",
            "absorb",
            "triage",
            "attach",
            "detach",
            "move",
            "children",
            "tree",
            "next",
            "path",
            "close",
            "doctor",
        }
        assert expected.issubset(command_names)
        for retired in ("validate", "root", "work-unit", "add"):
            assert retired not in command_names

    def test_machine_output_flag_is_json_everywhere(self) -> None:
        """Every command exposing machine output uses --json, never --as-json."""
        from typing import get_type_hints

        from itree.cli import children, doctor, next, path

        for cmd in (children, doctor, next, path):
            hints = get_type_hints(cmd, include_extras=True)
            names = hints["as_json"].__metadata__[0].name
            assert "--json" in names
            assert not any("as-json" in n for n in names)

    def test_cli_help_generated_from_docstrings(self) -> None:
        """CLI help text is generated from command docstrings."""
        for cmd_name, cmd in app._commands.items():
            if not cmd_name.startswith("_"):
                assert cmd.help is not None
                assert len(cmd.help) > 0

    def test_repo_diagnostics_do_not_accept_explicit_root_selection(self) -> None:
        """Repository-level diagnostics discover the root from the issue tree."""
        assert "root" not in signature(doctor).parameters
        assert "root" not in signature(path).parameters

    def test_help_model_prints_packaged_workflows_verbatim(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`itree help model` output equals the packaged WORKFLOWS.md byte-for-byte."""
        import importlib.resources

        cli.help_model()
        out = capsys.readouterr().out
        expected = importlib.resources.files("itree").joinpath("WORKFLOWS.md").read_text(encoding="utf-8")
        assert out == expected

    def test_help_milestone_explains_the_direct_root_rule(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Command-specific help distinguishes a milestone ledger from an ordinary grouping child."""
        cli.help_milestone()
        out = capsys.readouterr().out
        assert "direct child of the root ledger" in out
        assert "Backlog is a sibling branch" in out

    def test_help_maintenance_ships_the_live_repair_contract(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Maintenance help exposes the required live reread, ledger, and handoff steps."""
        cli.help_maintenance()
        out = capsys.readouterr().out
        assert "Reread the live GitHub issue tree" in out
        assert "remediation ledger entry" in out
        assert "append-only maintenance ledger" in out
        assert "gh issue comment OWNER/REPO#ROOT" in out
        assert "Preserve the current substantive work unit" in out

    def test_move_cli_rejects_both_before_and_after(self) -> None:
        """CLI move command rejects both --before and --after flags."""
        result = subprocess.run(
            ["python3", "-m", "itree.cli", "move", "owner/repo#1", "owner/repo#2", "--before", "owner/repo#3", "--after", "owner/repo#4"],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parents[3]),
        )
        assert result.returncode != 0
        assert "either --before or --after" in result.stderr or "either --before or --after" in result.stdout


class TestLiveReadCommands:
    """Read-only command glue proven end to end against the disposable repo (#24).

    These exercise the full build_dag -> pure-helper -> render path against real
    GitHub, so they fail if the fetch boundary, traversal, or rendering breaks.
    The scratch tree's structural anchors are fixed: #3 root ledger ->
    #4 milestone ledger -> #5 the sole open work unit.
    """

    SCRATCH = "dzackgarza/itree-e2e-scratch"

    def test_next_names_the_open_work_unit(self, capsys: pytest.CaptureFixture[str]) -> None:
        cli.next(self.SCRATCH)
        assert "#5 Editor preview sync" in capsys.readouterr().out

    def test_doctor_reads_the_live_tree_without_structural_errors(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            doctor(self.SCRATCH)
        # No structural errors and a real fetch: exit 0 (ok) or 1 (warnings),
        # never 2 (E-code) or 3 (fetch/auth failure).
        assert exc.value.code in (0, 1)
        out = capsys.readouterr().out
        assert "#3 Ledger: dzackgarza/itree-e2e-scratch" in out
        assert "Next work unit: #5" in out
        assert "errors: 0" in out

    def test_tree_renders_the_live_hierarchy(self, capsys: pytest.CaptureFixture[str]) -> None:
        cli.tree(self.SCRATCH)
        out = capsys.readouterr().out
        assert "#3" in out and "#4" in out and "#5" in out
