"""Contract proof for tree-safe GitHub milestone orchestration."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from itree.models import GithubIssue, IssueRef, IssueState, RepoDag, RepoRef

REPO_ROOT = Path(__file__).resolve().parents[1]


def _issue(number: int, title: str) -> GithubIssue:
    return GithubIssue(
        id=number,
        number=number,
        title=title,
        state=IssueState.open,
        html_url=f"https://github.com/owner/repo/issues/{number}",
        body="## Acceptance Criteria\n- Preserve the owned state transition.",
    )


def _representative_dag() -> RepoDag:
    """Root, grouping parent, parented leaf, and parentless leaf."""
    return RepoDag(
        repo_ref=RepoRef(owner="owner", repo="repo"),
        issues={
            1: _issue(1, "Ledger: owner/repo"),
            2: _issue(2, "Milestone: existing"),
            3: _issue(3, "Parented work unit"),
            4: _issue(4, "Parentless work unit"),
        },
        children_of={1: (2, 3)},
    )


def test_cli_help_publishes_milestone_command() -> None:
    """The public CLI advertises the orchestration entrypoint."""
    result = subprocess.run(
        [sys.executable, "-m", "itree.cli", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "milestone  Create a GitHub Milestone and its issue-tree ledger." in result.stdout


def test_milestone_title_is_one_normalized_domain_value() -> None:
    """The milestone and ledger titles derive from one validated value."""
    from itree.models import MilestoneTitle

    title = MilestoneTitle.parse("  release 2  ")

    assert title.value == "release 2"
    assert title.ledger_title == "Milestone: release 2"
    with pytest.raises(ValidationError):
        MilestoneTitle.model_validate("   ")


def test_preflight_plans_parented_replace_and_parentless_attach_in_order() -> None:
    """One typed preflight decides all writes before effects begin."""
    from itree.milestone import preflight_milestone

    from itree.models import (
        CreateMilestoneRequest,
        MilestoneEffectKind,
        MilestoneTitle,
        ValidatedMilestonePlan,
        WorkUnitPlacement,
    )

    repo_ref = RepoRef(owner="owner", repo="repo")
    request = CreateMilestoneRequest(
        repo_ref=repo_ref,
        title=MilestoneTitle.parse("release 2"),
        parent=IssueRef(repo_ref=repo_ref, number=2),
        body="## Outcome\nShip release 2.",
        work_units=(
            IssueRef(repo_ref=repo_ref, number=3),
            IssueRef(repo_ref=repo_ref, number=4),
        ),
    )

    result = preflight_milestone(request, _representative_dag(), ())

    assert isinstance(result, ValidatedMilestonePlan)
    assert tuple((unit.ref.number, unit.placement, unit.prior_parent_number, unit.prior_position) for unit in result.work_units) == (
        (3, WorkUnitPlacement.replace_parent, 1, 1),
        (4, WorkUnitPlacement.attach, None, None),
    )
    assert tuple(effect.kind for effect in result.effects) == (
        MilestoneEffectKind.create_milestone,
        MilestoneEffectKind.create_ledger,
        MilestoneEffectKind.attach_ledger,
        MilestoneEffectKind.assign_ledger,
        MilestoneEffectKind.replace_work_unit_parent,
        MilestoneEffectKind.assign_work_unit,
        MilestoneEffectKind.attach_work_unit,
        MilestoneEffectKind.assign_work_unit,
    )


def test_preflight_rejects_title_collision_before_effects() -> None:
    """A remote title collision yields a typed rejection with no write plan."""
    from itree.milestone import preflight_milestone

    from itree.models import (
        CreateMilestoneRequest,
        GithubMilestone,
        GithubMilestoneState,
        MilestonePreflightErrorKind,
        MilestonePreflightRejected,
        MilestoneTitle,
    )

    repo_ref = RepoRef(owner="owner", repo="repo")
    title = MilestoneTitle.parse("release 2")
    request = CreateMilestoneRequest(
        repo_ref=repo_ref,
        title=title,
        parent=IssueRef(repo_ref=repo_ref, number=2),
        body="",
        work_units=(),
    )
    existing = GithubMilestone(
        number=7,
        title=title,
        state=GithubMilestoneState.open,
        html_url="https://github.com/owner/repo/milestone/7",
    )

    result = preflight_milestone(request, _representative_dag(), (existing,))

    assert isinstance(result, MilestonePreflightRejected)
    assert result.kind is MilestonePreflightErrorKind.milestone_title_collision
    assert result.references == ("milestone:7",)


def test_indeterminate_effect_preserves_confirmed_and_untouched_operations() -> None:
    """A lost mutation response stops the plan and exposes its untouched suffix."""
    from itree.models import (
        GithubIndeterminateOperation,
        MilestoneCreationFailed,
        MilestoneEffect,
        MilestoneEffectKind,
        MilestoneExecutionProgress,
    )

    effects = tuple(
        MilestoneEffect(kind=kind)
        for kind in (
            MilestoneEffectKind.create_milestone,
            MilestoneEffectKind.create_ledger,
            MilestoneEffectKind.attach_ledger,
            MilestoneEffectKind.assign_ledger,
        )
    )
    progress = MilestoneExecutionProgress.start(effects)
    progress = progress.confirm(effects[0])
    progress = progress.confirm(effects[1])

    failure = progress.stop(
        GithubIndeterminateOperation(
            effect=effects[2],
            detail="response unavailable after gh invocation",
        )
    )

    assert isinstance(failure, MilestoneCreationFailed)
    assert failure.progress.confirmed == effects[:2]
    assert failure.outcome.effect == effects[2]
    assert failure.progress.untouched == effects[3:]
