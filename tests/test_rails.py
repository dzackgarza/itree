"""Call-sequence tests for the rails commands: new, absorb, triage.

These prove the guard rails as observable API behavior: new-without-
placement creates nothing, work units stay leaves, absorb preserves the
source body byte-for-byte, and triage surfaces exactly one orphan.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from itree import cli
from itree.github import GithubApi
from itree.models import GithubIssue, IssueCloseReason, IssueState, RepoDag, RepoRef


def _issue(
    number: int,
    title: str,
    state: IssueState = IssueState.open,
    body: str | None = None,
) -> GithubIssue:
    return GithubIssue(
        id=number + 5000,
        number=number,
        title=title,
        state=state,
        html_url=f"https://github.com/t/t/issues/{number}",
        body=body,
    )


def _dag(issues: dict[int, GithubIssue], children_of: dict[int, tuple[int, ...]]) -> RepoDag:
    return RepoDag(repo_ref=RepoRef(owner="t", repo="t"), issues=issues, children_of=children_of)


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch cli.GithubApi so every constructor path yields one mock instance."""
    instance = MagicMock(spec=GithubApi)
    cls = MagicMock()
    cls.from_issue_ref.return_value = instance
    cls.from_repo_ref.return_value = instance
    monkeypatch.setattr(cli, "GithubApi", cls)
    return instance


def _patch_dag(monkeypatch: pytest.MonkeyPatch, dag: RepoDag) -> None:
    monkeypatch.setattr(cli, "build_dag", lambda *args, **kwargs: dag)


class TestNew:
    def test_bare_new_creates_nothing_and_prints_menu(self, api: MagicMock, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        dag = _dag(
            {
                1: _issue(1, "Ledger: t/t"),
                2: _issue(2, "Milestone: v1"),
                3: _issue(3, "Preview sync", body="## Acceptance Criteria\n- ok"),
            },
            {1: (2,), 2: (3,)},
        )
        _patch_dag(monkeypatch, dag)

        with pytest.raises(SystemExit) as exc:
            cli.new("t/t", "Some idea")
        assert exc.value.code == 1

        out = capsys.readouterr().out
        assert "Nothing was created" in out
        assert "#3 Preview sync" in out
        assert "#2 Milestone: v1" in out
        assert 'itree absorb --into t/t#3 --title "Some idea"' in out
        assert 'itree new t/t "Some idea" --under t/t#2' in out
        api.create_issue.assert_not_called()
        api.add_subissue.assert_not_called()

    def test_new_under_work_unit_refuses(self, api: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
        api.get_issue.return_value = _issue(3, "Preview sync")

        with pytest.raises(SystemExit) as exc:
            cli.new("t/t", "Sub-task", under="t/t#3")
        assert exc.value.code == 2

        out = capsys.readouterr().out
        assert "work units are leaves" in out
        assert "itree absorb --into t/t#3" in out
        api.create_issue.assert_not_called()

    def test_new_under_closed_parent_refuses(self, api: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
        api.get_issue.return_value = _issue(2, "Milestone: v1", state=IssueState.closed)

        with pytest.raises(SystemExit) as exc:
            cli.new("t/t", "Late work", under="t/t#2")
        assert exc.value.code == 2
        assert "closed" in capsys.readouterr().out
        api.create_issue.assert_not_called()

    def test_new_under_grouping_creates_and_attaches(self, api: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
        api.get_issue.return_value = _issue(2, "Milestone: v1")
        api.create_issue.return_value = _issue(9, "Export proof")

        cli.new("t/t", "Export proof", under="t/t#2", body="## Acceptance Criteria\n- ok")

        api.create_issue.assert_called_once_with("Export proof", "## Acceptance Criteria\n- ok")
        api.add_subissue.assert_called_once_with(2, 9 + 5000)
        assert "t/t#9" in capsys.readouterr().out

    def test_new_parent_as_first_arg_behaves_as_under(self, api: MagicMock) -> None:
        api.get_issue.return_value = _issue(2, "Milestone: v1")
        api.create_issue.return_value = _issue(9, "Export proof")

        cli.new("t/t#2", "Export proof")

        api.add_subissue.assert_called_once_with(2, 9 + 5000)


class TestAbsorb:
    SRC_BODY = "Exact original body.\n\n- detail A\n- detail B\n"

    def _dag_with_source(self) -> RepoDag:
        return _dag(
            {
                1: _issue(1, "Ledger: t/t"),
                2: _issue(2, "Milestone: v1"),
                3: _issue(3, "Preview sync", body="Target body."),
                7: _issue(7, "Small orphan fix", body=self.SRC_BODY),
            },
            {1: (2, 7), 2: (3,)},
        )

    def test_absorb_existing_issue_full_sequence(self, api: MagicMock, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        _patch_dag(monkeypatch, self._dag_with_source())
        api.get_issue.return_value = _issue(3, "Preview sync", body="Target body.")

        cli.absorb("t/t#7", into="t/t#3")

        new_body = api.update_issue_body.call_args.args[1]
        assert new_body.startswith("Target body.")
        assert "## Absorbed: Small orphan fix (#7)" in new_body
        assert new_body.endswith(self.SRC_BODY)  # byte-for-byte, no summarization
        api.add_comment.assert_called_once()
        assert "Absorbed into #3" in api.add_comment.call_args.args[1]
        api.remove_subissue.assert_called_once_with(1, 7 + 5000)
        api.close_issue.assert_called_once_with(7, reason=IssueCloseReason.duplicate)
        out = capsys.readouterr().out
        assert "Absorbed t/t#7 -> t/t#3" in out
        assert "Next: itree doctor t/t" in out

    def test_absorb_parentless_source_skips_detach(self, api: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        dag = _dag(
            {
                1: _issue(1, "Ledger: t/t"),
                3: _issue(3, "Preview sync", body="Target body."),
                7: _issue(7, "Floating orphan", body=self.SRC_BODY),
            },
            {1: (3,)},
        )
        _patch_dag(monkeypatch, dag)
        api.get_issue.return_value = _issue(3, "Preview sync", body="Target body.")

        cli.absorb("t/t#7", into="t/t#3")

        api.remove_subissue.assert_not_called()
        api.close_issue.assert_called_once_with(7, reason=IssueCloseReason.duplicate)

    def test_absorb_into_grouping_refuses(self, api: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
        api.get_issue.return_value = _issue(2, "Milestone: v1")

        with pytest.raises(SystemExit) as exc:
            cli.absorb("t/t#7", into="t/t#2")
        assert exc.value.code == 2
        assert "grouping issue" in capsys.readouterr().out
        api.update_issue_body.assert_not_called()

    def test_absorb_unfiled_content_appends_only(self, api: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
        api.get_issue.return_value = _issue(3, "Preview sync", body="Target body.")

        cli.absorb(into="t/t#3", title="Tiny follow-up", body="One-liner detail.")

        new_body = api.update_issue_body.call_args.args[1]
        assert "## Absorbed: Tiny follow-up" in new_body
        assert new_body.endswith("One-liner detail.")
        api.close_issue.assert_not_called()
        api.add_comment.assert_not_called()
        assert "Absorbed new content -> t/t#3" in capsys.readouterr().out

    def test_absorb_cross_repo_refuses(self, api: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
        api.get_issue.return_value = _issue(3, "Preview sync", body="Target body.")

        with pytest.raises(SystemExit) as exc:
            cli.absorb("other/repo#7", into="t/t#3")
        assert exc.value.code == 1
        assert "same repository" in capsys.readouterr().out


class TestTriage:
    def _dag_with_orphans(self) -> RepoDag:
        return _dag(
            {
                1: _issue(1, "Ledger: t/t"),
                2: _issue(2, "Milestone: v1"),
                3: _issue(3, "Preview sync", body="## Acceptance Criteria\n- ok"),
                9: _issue(9, "Orphan nine", body="Nine's body"),
                5: _issue(5, "Orphan five", body="Five's body"),
            },
            {1: (2,), 2: (3,)},
        )

    def test_triage_picks_lowest_orphan_and_counts_rest(self, api: MagicMock, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        _patch_dag(monkeypatch, self._dag_with_orphans())

        cli.triage("t/t")

        out = capsys.readouterr().out
        assert "Orphan 1 of 2: #5 Orphan five" in out
        assert "Five's body" in out
        assert "itree absorb t/t#5 --into t/t#3" in out
        assert "itree move t/t#5 --under t/t#2" in out
        assert "itree close t/t#5 --reason not_planned" in out
        assert "1 orphan remain after this one" in out

    def test_triage_explicit_target(self, api: MagicMock, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        _patch_dag(monkeypatch, self._dag_with_orphans())

        cli.triage("t/t#9")

        out = capsys.readouterr().out
        assert "#9 Orphan nine" in out

    def test_triage_non_orphan_target_errors(self, api: MagicMock, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        _patch_dag(monkeypatch, self._dag_with_orphans())

        with pytest.raises(SystemExit) as exc:
            cli.triage("t/t#3")
        assert exc.value.code == 1
        assert "not an orphan" in capsys.readouterr().out

    def test_triage_clean_repo(self, api: MagicMock, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        dag = _dag(
            {
                1: _issue(1, "Ledger: t/t"),
                3: _issue(3, "Preview sync", body="## Acceptance Criteria\n- ok"),
            },
            {1: (3,)},
        )
        _patch_dag(monkeypatch, dag)

        cli.triage("t/t")

        out = capsys.readouterr().out
        assert "No orphans" in out
        assert "itree doctor t/t" in out

    def test_triage_prefers_ledger_titled_root_among_candidates(self, api: MagicMock, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """With several parentless issues, the Ledger-titled one anchors triage."""
        dag = _dag(
            {
                4: _issue(4, "Stray early issue"),
                8: _issue(8, "Ledger: t/t"),
                9: _issue(9, "Work under ledger", body="## Acceptance Criteria\n- ok"),
            },
            {8: (9,)},
        )
        _patch_dag(monkeypatch, dag)

        cli.triage("t/t")

        out = capsys.readouterr().out
        assert "Orphan 1 of 1: #4 Stray early issue" in out
