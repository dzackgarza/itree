"""Tests for CLI commands and orchestration functions."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from itree.cli import (
    app,
    parse_ref,
    parse_repo,
)
from itree.models import AttachRequest, IssueRef, MoveRequest, RepoRef


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
            "add",
            "attach",
            "detach",
            "move",
            "children",
            "tree",
            "next",
            "path",
            "validate",
            "close",
        }
        assert expected.issubset(command_names)

    def test_cli_help_generated_from_docstrings(self) -> None:
        """CLI help text is generated from command docstrings."""
        for cmd_name, cmd in app._commands.items():
            if not cmd_name.startswith("_"):
                assert cmd.help is not None
                assert len(cmd.help) > 0

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
