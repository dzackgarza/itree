"""Rails proofs: pure placement rendering + live GitHub boundary mutations.

The guard rails (file-don't-invent, work-units-are-leaves, absorb-verbatim,
traverse-don't-replan) are proven as real behavior. Refusals that never mutate
run against the disposable integration repo and assert exit code + message; the
placement menu is rendered from a constructed tree. Mutating rails create
proof-prefixed issues in the scratch repo, reread the live tree to confirm the
effect, then detach and close every created artifact on teardown.

Scratch anchors: #3 root ledger -> #4 milestone ledger -> #5 the open work
unit; #2 is a closed issue. These are never mutated.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from functools import partial
from types import TracebackType
from typing import Literal

import pytest

from itree import cli
from itree.github import GithubApi
from itree.milestone import preflight_milestone
from itree.models import CreateMilestoneRequest, GithubIssue, IssueCloseReason, IssueRef, IssueState, MilestoneTitle, RepoDag, RepoRef, TreeNode, ValidatedMilestonePlan

SCRATCH = RepoRef(owner="dzackgarza", repo="itree-e2e-scratch")
SLUG = SCRATCH.slug
LEDGER, MILESTONE, WORKUNIT, CLOSED = 3, 4, 5, 2


class FixtureCleanupFailure(RuntimeError):
    """A live fixture cleanup operation failed for one tracked issue."""

    def __init__(self, issue_number: int, operation: str) -> None:
        self.issue_number = issue_number
        self.operation = operation
        super().__init__(f"cleanup {operation} failed for issue #{issue_number}")


class CleanupOperation:
    """Attach an issue and operation to a cleanup failure without suppressing it."""

    def __init__(self, issue_number: int, operation: str, action: Callable[[], None]) -> None:
        self.issue_number = issue_number
        self.operation = operation
        self.action = action

    def __call__(self) -> None:
        with self:
            self.action()

    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        if exc_value is not None:
            raise FixtureCleanupFailure(self.issue_number, self.operation) from exc_value
        return False


class ScratchFixtures:
    """Creates proof-prefixed scratch issues and tears them down (detach+close)."""

    def __init__(self) -> None:
        self.api = GithubApi.from_repo_ref(SCRATCH)
        self._created: list[GithubIssue] = []

    def new_issue(self, title: str, body: str = "", parent: int | None = None) -> GithubIssue:
        issue = self.api.create_issue(f"proof: {title}", body)
        self._created.append(issue)
        if parent is not None:
            self.api.add_subissue(parent, issue.id)
        return issue

    def track(self, issue: GithubIssue) -> GithubIssue:
        """Track an issue created by the command under test for teardown."""
        self._created.append(issue)
        return issue

    def cleanup(self) -> None:
        # Detach from any live parent so closed proof issues never pollute the
        # anchor tree's edges, then close them (restore-or-close per #24).
        # ExitStack completes every callback even when an earlier one raises;
        # each wrapper retains the failed issue and cleanup operation.
        with ExitStack() as cleanup:
            for issue in reversed(self._created):
                cleanup.callback(CleanupOperation(issue.number, "close", partial(self._close_issue, issue)))
                cleanup.callback(CleanupOperation(issue.number, "detach", partial(self._detach_issue, issue)))

    def _detach_issue(self, issue: GithubIssue) -> None:
        parent = self.api.get_parent_number(issue.number)
        if parent is not None:
            self.api.remove_subissue(parent, issue.id)

    def _close_issue(self, issue: GithubIssue) -> None:
        if self.api.get_issue(issue.number).is_open:
            self.api.close_issue(issue.number, reason=IssueCloseReason.not_planned)


@pytest.fixture
def scratch() -> Iterator[ScratchFixtures]:
    fixtures = ScratchFixtures()
    try:
        yield fixtures
    finally:
        fixtures.cleanup()


def _issue(number: int, title: str, state: IssueState = IssueState.open, body: str | None = None) -> GithubIssue:
    return GithubIssue(
        id=number + 5000,
        number=number,
        title=title,
        state=state,
        html_url=f"https://github.com/t/t/issues/{number}",
        body=body,
    )


class TestPlacementMenuRendering:
    """Rail 1 renderer: print_placement_menu lists existing work first (pure)."""

    def test_menu_lists_work_units_groupings_and_exact_commands(self, capsys: pytest.CaptureFixture[str]) -> None:
        dag = RepoDag(
            repo_ref=RepoRef(owner="t", repo="t"),
            issues={
                1: _issue(1, "Ledger: t/t"),
                2: _issue(2, "Milestone: v1"),
                3: _issue(3, "Preview sync", body="## Acceptance Criteria\n- ok"),
            },
            children_of={1: (2,), 2: (3,)},
        )
        tree_node: TreeNode = dag.materialize_root(1)

        cli.print_placement_menu("t/t", "Some idea", tree_node)

        out = capsys.readouterr().out
        assert "Nothing was created" in out
        assert "#3 Preview sync" in out
        assert "#2 Milestone: v1" in out
        assert 'itree absorb --into t/t#3 --title "Some idea"' in out
        assert 'itree new t/t "Some idea" --under t/t#2' in out


class TestMilestonePlacementRendering:
    def test_placement_uses_the_root_ledger_when_a_lower_grouping_is_reachable(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The displayed no-write command names a parent accepted by mutation preflight."""
        repo = RepoRef(owner="t", repo="t")
        dag = RepoDag(
            repo_ref=repo,
            issues={
                1: _issue(1, "Backlog"),
                10: _issue(10, "Ledger: t/t"),
            },
            children_of={10: (1,)},
        )
        inquiry = cli.PlacementInquiry(repo_ref=repo, title=MilestoneTitle.parse("release 2"))

        cli.print_milestone_placement(inquiry, dag, "", None, ())

        command = next(line.strip() for line in capsys.readouterr().out.splitlines() if line.startswith("  itree milestone"))
        tokens = shlex.split(command)
        displayed_parent = IssueRef.parse(tokens[tokens.index("--under") + 1])
        result = preflight_milestone(
            CreateMilestoneRequest(
                repo_ref=repo,
                title=inquiry.title,
                parent=displayed_parent,
                body="",
            ),
            dag,
            (),
        )

        assert displayed_parent.number == 10
        assert isinstance(result, ValidatedMilestonePlan)
        assert result.parent_issue.number == 10


class TestScratchFixtureCleanup:
    def test_cleanup_continues_after_a_real_github_failure(self) -> None:
        """A failed issue cleanup is contextualized while later tracked issues are still cleaned."""
        fixtures = ScratchFixtures()
        missing = GithubIssue(
            id=999_999_999,
            number=999_999_999,
            title="proof: missing cleanup boundary",
            state=IssueState.open,
            html_url="https://github.com/dzackgarza/itree-e2e-scratch/issues/999999999",
        )
        fixtures.track(missing)
        created = fixtures.new_issue("cleanup continues after GitHub failure")

        with pytest.raises(FixtureCleanupFailure) as error:
            fixtures.cleanup()

        assert error.value.issue_number == missing.number
        assert error.value.operation == "close"
        assert fixtures.api.get_parent_number(created.number) is None
        assert fixtures.api.get_issue(created.number).state is IssueState.closed


class TestNewRefusals:
    """Rails 1 & 2 as live command behavior; none of these mutate the repo."""

    def test_bare_new_creates_nothing_and_prints_menu(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.new(SLUG, "proof: never created")
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "Nothing was created" in out
        # Rail-1 lists the live work unit and grouping targets.
        assert f"#{WORKUNIT} Editor preview sync" in out

    def test_new_under_work_unit_refuses(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.new(SLUG, "proof: forbidden child", under=f"{SLUG}#{WORKUNIT}")
        assert exc.value.code == 2
        assert "work units are leaves" in capsys.readouterr().out

    def test_new_under_closed_parent_refuses(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.new(SLUG, "proof: late work", under=f"{SLUG}#{CLOSED}")
        assert exc.value.code == 2
        assert "closed" in capsys.readouterr().out


class TestNewCreatesUnderGrouping:
    def test_new_under_grouping_creates_and_attaches(self, scratch: ScratchFixtures, capsys: pytest.CaptureFixture[str]) -> None:
        cli.new(SLUG, "child under milestone", under=f"{SLUG}#{MILESTONE}", body="## Acceptance Criteria\n- ok")

        out = capsys.readouterr().out.strip()
        assert out.startswith(f"{SLUG}#")
        created_number = int(out.rsplit("#", 1)[1])
        scratch.track(scratch.api.get_issue(created_number))

        # Reread live state: the new issue really hangs under the milestone.
        assert scratch.api.get_parent_number(created_number) == MILESTONE


class TestAttachDetach:
    def test_attach_then_detach_moves_real_parent_edge(self, scratch: ScratchFixtures) -> None:
        child = scratch.new_issue("attachable")
        assert scratch.api.get_parent_number(child.number) is None

        cli.attach(f"{SLUG}#{MILESTONE}", f"{SLUG}#{child.number}")
        assert scratch.api.get_parent_number(child.number) == MILESTONE

        cli.detach(f"{SLUG}#{MILESTONE}", f"{SLUG}#{child.number}")
        assert scratch.api.get_parent_number(child.number) is None


class TestMove:
    def test_move_reparents_under_the_new_grouping(self, scratch: ScratchFixtures) -> None:
        child = scratch.new_issue("movable", parent=MILESTONE)
        assert scratch.api.get_parent_number(child.number) == MILESTONE

        cli.move(f"{SLUG}#{child.number}", under=f"{SLUG}#{LEDGER}")
        assert scratch.api.get_parent_number(child.number) == LEDGER


class TestAbsorb:
    SRC_BODY = "Exact original body.\n\n- detail A\n- detail B\n"

    def test_absorb_appends_source_verbatim_and_closes_source(self, scratch: ScratchFixtures, capsys: pytest.CaptureFixture[str]) -> None:
        target = scratch.new_issue("absorb target", body="Target body.")
        source = scratch.new_issue("absorb source", body=self.SRC_BODY)

        cli.absorb(f"{SLUG}#{source.number}", into=f"{SLUG}#{target.number}")

        out = capsys.readouterr().out
        assert f"Absorbed {SLUG}#{source.number} -> {SLUG}#{target.number}" in out

        # Reread live: target body carries the source body verbatim; source closed.
        merged = scratch.api.get_issue(target.number)
        assert merged.body is not None
        assert merged.body.startswith("Target body.")
        assert merged.body.endswith(self.SRC_BODY)
        assert f"## Absorbed: proof: absorb source (#{source.number})" in merged.body
        assert scratch.api.get_issue(source.number).state == IssueState.closed

    def test_absorb_into_grouping_refuses(self, capsys: pytest.CaptureFixture[str]) -> None:
        # #4 is a live milestone grouping; absorbing into it is refused pre-mutation.
        with pytest.raises(SystemExit) as exc:
            cli.absorb(f"{SLUG}#{WORKUNIT}", into=f"{SLUG}#{MILESTONE}")
        assert exc.value.code == 2
        assert "grouping issue" in capsys.readouterr().out

    def test_absorb_cross_repo_refuses(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.absorb("other/repo#7", into=f"{SLUG}#{WORKUNIT}")
        assert exc.value.code == 1
        assert "same repository" in capsys.readouterr().out


class TestTriage:
    def test_triage_surfaces_a_live_orphan(self, scratch: ScratchFixtures, capsys: pytest.CaptureFixture[str]) -> None:
        # A parentless open issue is an orphan: unreachable from the root ledger.
        orphan = scratch.new_issue("floating orphan", body="Orphan body.")

        # Explicit target surfaces this specific orphan regardless of others.
        cli.triage(f"{SLUG}#{orphan.number}")

        out = capsys.readouterr().out
        assert f"#{orphan.number} proof: floating orphan" in out
        assert f"itree close {SLUG}#{orphan.number} --reason not_planned" in out
