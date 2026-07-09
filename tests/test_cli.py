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

    @pytest.mark.xfail(reason="#15: clean-tree doctor footer suggests --explain E010", strict=True)
    def test_clean_tree_omits_explain_suggestion(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        dag = self._dag(
            {
                1: self._issue(1, "Ledger: t/t"),
                2: self._issue(2, "Work unit", body="## Acceptance Criteria\n- ok"),
            },
            {1: (2,)},
        )
        monkeypatch.setattr(cli, "build_dag", lambda *args, **kwargs: dag)

        with pytest.raises(SystemExit) as exc:
            doctor("t/t")
        assert exc.value.code == 0
        assert "--explain" not in capsys.readouterr().out

    def test_error_tree_suggests_a_present_code(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        dag = self._dag(
            {
                1: self._issue(1, "Ledger: Root 1"),
                2: self._issue(2, "Ledger: Root 2"),
            },
            {},
        )
        monkeypatch.setattr(cli, "build_dag", lambda *args, **kwargs: dag)

        with pytest.raises(SystemExit) as exc:
            doctor("t/t")
        assert exc.value.code == 2
        out = capsys.readouterr().out
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
        for retired in ("validate", "root", "milestone", "work-unit", "add"):
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
