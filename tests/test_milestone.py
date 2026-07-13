"""Contract proof for tree-safe GitHub milestone orchestration."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from itree import models
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


def _request(
    parent_number: int,
    work_numbers: tuple[int, ...],
) -> models.CreateMilestoneRequest:
    repo_ref = RepoRef(owner="owner", repo="repo")
    return models.CreateMilestoneRequest(
        repo_ref=repo_ref,
        title=models.MilestoneTitle.parse("release 2"),
        parent=IssueRef(repo_ref=repo_ref, number=parent_number),
        body="## Outcome\nShip release 2.",
        work_units=tuple(IssueRef(repo_ref=repo_ref, number=number) for number in work_numbers),
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
        ParentedPriorPlacement,
        ParentlessPriorPlacement,
        UnassignedPriorMilestone,
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
    parented, parentless = result.work_units
    assert parented.ref.number == 3
    assert parented.prior_placement == ParentedPriorPlacement(
        kind=WorkUnitPlacement.replace_parent,
        parent_number=1,
        position=1,
    )
    assert parentless.ref.number == 4
    assert parentless.prior_placement == ParentlessPriorPlacement(kind=WorkUnitPlacement.attach)
    assert parented.prior_milestone == UnassignedPriorMilestone(kind="unassigned")
    assert parentless.prior_milestone == UnassignedPriorMilestone(kind="unassigned")
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
        state=GithubMilestoneState.closed,
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


def test_request_rejects_cross_repository_references() -> None:
    """A write-capable request cannot contain a foreign parent or work unit."""
    repo_ref = RepoRef(owner="owner", repo="repo")
    foreign_ref = IssueRef.parse("other/repo#2")

    with pytest.raises(ValidationError):
        models.CreateMilestoneRequest(
            repo_ref=repo_ref,
            title=models.MilestoneTitle.parse("release 2"),
            parent=foreign_ref,
            body="",
            work_units=(),
        )


def test_effect_variants_require_their_target_shape() -> None:
    """Targeted and untargeted effects cannot be represented by the wrong model."""
    with pytest.raises(ValidationError):
        models.MilestoneEffect(kind=models.MilestoneEffectKind.attach_work_unit)
    with pytest.raises(ValidationError):
        models.WorkUnitMilestoneEffect(
            kind=models.MilestoneEffectKind.create_ledger,
            ref=IssueRef.parse("owner/repo#3"),
        )


def test_preflight_rejects_a_work_unit_as_parent() -> None:
    """Only an open grouping issue can own the new milestone ledger."""
    from itree.milestone import preflight_milestone

    result = preflight_milestone(_request(3, (4,)), _representative_dag(), ())

    assert isinstance(result, models.MilestonePreflightRejected)
    assert result.kind is models.MilestonePreflightErrorKind.parent_invalid
    assert result.references == ("owner/repo#3",)


@pytest.mark.xfail(strict=True, reason="itree#39: Backlog is incorrectly accepted as a milestone-ledger parent")
def test_preflight_rejects_backlog_parent_before_an_effect_plan_exists() -> None:
    """A release ledger and Backlog are sibling branches of the root ledger."""
    from itree.milestone import preflight_milestone

    dag = RepoDag(
        repo_ref=RepoRef(owner="owner", repo="repo"),
        issues={
            1: _issue(1, "Ledger: owner/repo"),
            2: _issue(2, "Backlog"),
            3: _issue(3, "Release work unit"),
        },
        children_of={1: (2, 3)},
    )

    result = preflight_milestone(_request(2, (3,)), dag, ())

    assert isinstance(result, models.MilestonePreflightRejected)
    assert result.kind is models.MilestonePreflightErrorKind.parent_invalid
    assert result.references == ("owner/repo#2", "owner/repo#1")


def test_preflight_rejects_exact_ledger_title_collision_in_any_issue_state() -> None:
    """A closed issue still reserves the exact derived ledger title."""
    from itree.milestone import preflight_milestone

    closed_ledger = GithubIssue(
        id=5,
        number=5,
        title="Milestone: release 2",
        state=IssueState.closed,
        html_url="https://github.com/owner/repo/issues/5",
        body="Historical release ledger.",
    )
    dag = RepoDag(
        repo_ref=RepoRef(owner="owner", repo="repo"),
        issues={
            1: _issue(1, "Ledger: owner/repo"),
            2: _issue(2, "Milestone: existing"),
            5: closed_ledger,
        },
        children_of={1: (2, 5)},
    )

    result = preflight_milestone(_request(2, ()), dag, ())

    assert isinstance(result, models.MilestonePreflightRejected)
    assert result.kind is models.MilestonePreflightErrorKind.ledger_title_collision
    assert result.references == ("owner/repo#5",)


def test_preflight_rejects_duplicate_work_unit_arguments() -> None:
    """Repeated CLI references cannot produce duplicate remote operations."""
    from itree.milestone import preflight_milestone

    result = preflight_milestone(
        _request(2, (3, 3)),
        _representative_dag(),
        (),
    )

    assert isinstance(result, models.MilestonePreflightRejected)
    assert result.kind is models.MilestonePreflightErrorKind.duplicate_work_unit
    assert result.references == ("owner/repo#3",)


def test_preflight_rejects_grouping_issue_as_work_unit() -> None:
    """A supplied grouping issue cannot be moved as a leaf work unit."""
    from itree.milestone import preflight_milestone

    result = preflight_milestone(
        _request(1, (2, 4)),
        _representative_dag(),
        (),
    )

    assert isinstance(result, models.MilestonePreflightRejected)
    assert result.kind is models.MilestonePreflightErrorKind.invalid_work_unit
    assert result.references == ("owner/repo#2",)


def test_preflight_rejects_closed_work_unit() -> None:
    """A closed issue cannot enter the mutation plan."""
    from itree.milestone import preflight_milestone

    closed_work = GithubIssue(
        id=3,
        number=3,
        title="Closed work unit",
        state=IssueState.closed,
        html_url="https://github.com/owner/repo/issues/3",
        body="## Acceptance Criteria\n- Historical only.",
    )
    dag = RepoDag(
        repo_ref=RepoRef(owner="owner", repo="repo"),
        issues={
            1: _issue(1, "Ledger: owner/repo"),
            2: _issue(2, "Milestone: existing"),
            3: closed_work,
        },
        children_of={1: (2, 3)},
    )

    result = preflight_milestone(_request(2, (3,)), dag, ())

    assert isinstance(result, models.MilestonePreflightRejected)
    assert result.kind is models.MilestonePreflightErrorKind.invalid_work_unit
    assert result.references == ("owner/repo#3",)


def test_preflight_rejects_nonleaf_work_unit() -> None:
    """A supplied work unit cannot own children in the existing tree."""
    from itree.milestone import preflight_milestone

    dag = RepoDag(
        repo_ref=RepoRef(owner="owner", repo="repo"),
        issues={
            1: _issue(1, "Ledger: owner/repo"),
            2: _issue(2, "Decomposed work unit"),
            3: _issue(3, "Milestone: target parent"),
            4: _issue(4, "Nested task"),
        },
        children_of={1: (2, 3), 2: (4,)},
    )

    result = preflight_milestone(_request(3, (2,)), dag, ())

    assert isinstance(result, models.MilestonePreflightRejected)
    assert result.kind is models.MilestonePreflightErrorKind.invalid_work_unit
    assert result.references == ("owner/repo#2",)


def test_preflight_rejects_unknown_work_unit() -> None:
    """An absent issue, including a PR absent from the issue graph, cannot be planned."""
    from itree.milestone import preflight_milestone

    dag = RepoDag(
        repo_ref=RepoRef(owner="owner", repo="repo"),
        issues={
            1: _issue(1, "Ledger: owner/repo"),
            2: _issue(2, "Milestone: existing"),
        },
        children_of={1: (2,)},
    )

    result = preflight_milestone(_request(2, (99,)), dag, ())

    assert isinstance(result, models.MilestonePreflightRejected)
    assert result.kind is models.MilestonePreflightErrorKind.invalid_work_unit
    assert result.references == ("owner/repo#99",)


def test_preflight_reports_cycle_risk_before_nonleaf_reclassification() -> None:
    """Moving an ancestor beneath its descendant is rejected as the specific cycle risk."""
    from itree.milestone import preflight_milestone

    dag = RepoDag(
        repo_ref=RepoRef(owner="owner", repo="repo"),
        issues={
            1: _issue(1, "Ledger: owner/repo"),
            2: _issue(2, "Ancestor work unit"),
            3: _issue(3, "Milestone: nested parent"),
        },
        children_of={1: (2,), 2: (3,)},
    )

    result = preflight_milestone(_request(3, (2,)), dag, ())

    assert isinstance(result, models.MilestonePreflightRejected)
    assert result.kind is models.MilestonePreflightErrorKind.cycle_risk
    assert result.references == ("owner/repo#2", "owner/repo#3")


def test_preflight_rejects_unselected_open_orphan() -> None:
    """Ambiguous open tree state blocks the first write."""
    from itree.milestone import preflight_milestone

    result = preflight_milestone(
        _request(2, (3,)),
        _representative_dag(),
        (),
    )

    assert isinstance(result, models.MilestonePreflightRejected)
    assert result.kind is models.MilestonePreflightErrorKind.repository_malformed
    assert result.references == ("owner/repo#4",)


def test_preflight_records_existing_parent_order_and_milestone() -> None:
    """Failure reporting retains every supplied unit's prior recovery state."""
    from itree.milestone import preflight_milestone

    assigned_work = GithubIssue(
        id=3,
        number=3,
        title="Parented work unit",
        state=IssueState.open,
        html_url="https://github.com/owner/repo/issues/3",
        body="## Acceptance Criteria\n- Preserve prior state.",
        milestone=models.Milestone(title="old release"),
    )
    dag = RepoDag(
        repo_ref=RepoRef(owner="owner", repo="repo"),
        issues={
            1: _issue(1, "Ledger: owner/repo"),
            2: _issue(2, "Milestone: existing"),
            3: assigned_work,
        },
        children_of={1: (2, 3)},
    )

    result = preflight_milestone(_request(2, (3,)), dag, ())

    assert isinstance(result, models.ValidatedMilestonePlan)
    assert result.work_units == (
        models.ExistingWorkUnit(
            ref=IssueRef.parse("owner/repo#3"),
            issue_id=3,
            prior_placement=models.ParentedPriorPlacement(
                kind=models.WorkUnitPlacement.replace_parent,
                parent_number=1,
                position=1,
            ),
            prior_milestone=models.AssignedPriorMilestone(
                kind="assigned",
                title="old release",
            ),
        ),
    )


def test_unusable_mutation_response_is_indeterminate() -> None:
    """A successful process with an unusable body cannot be called a rejection."""
    from itree.github import GithubApi, GithubIndeterminateError

    effect = models.MilestoneEffect(
        kind=models.MilestoneEffectKind.create_ledger,
    )

    with pytest.raises(GithubIndeterminateError) as raised:
        GithubApi._parse_issue_mutation("not-json", effect)

    assert raised.value.outcome.kind == "indeterminate"
    assert raised.value.outcome.effect == effect


def test_placement_guidance_reconstructs_arguments_from_concrete_dag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The guidance renderer names existing targets and reconstructs the invocation."""
    from itree.cli import print_milestone_placement

    inquiry = models.PlacementInquiry(
        repo_ref=RepoRef(owner="owner", repo="repo"),
        title=models.MilestoneTitle.parse("release 2"),
    )

    print_milestone_placement(
        inquiry,
        _representative_dag(),
        "Ship release 2.",
        None,
        ("owner/repo#3", "owner/repo#4"),
    )

    output = capsys.readouterr().out
    assert "Nothing was created. --under is required before milestone mutation." in output
    assert "#2 Milestone: existing" in output
    assert "#1 Ledger: owner/repo" in output
    assert ("itree milestone owner/repo 'release 2' --under 'owner/repo#1' --body 'Ship release 2.' --issues 'owner/repo#3' 'owner/repo#4'") in output


def test_failure_rendering_preserves_prior_state_and_progress(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The nonzero report separates confirmed, current, untouched, and prior state."""
    from itree.cli import print_milestone_failure

    work_unit = models.ExistingWorkUnit(
        ref=IssueRef.parse("owner/repo#3"),
        issue_id=3,
        prior_placement=models.ParentedPriorPlacement(
            kind=models.WorkUnitPlacement.replace_parent,
            parent_number=1,
            position=2,
        ),
        prior_milestone=models.AssignedPriorMilestone(
            kind="assigned",
            title="old release",
        ),
    )
    effects: tuple[models.PlannedMilestoneEffect, ...] = (
        models.MilestoneEffect(kind=models.MilestoneEffectKind.create_milestone),
        models.MilestoneEffect(kind=models.MilestoneEffectKind.create_ledger),
        models.WorkUnitMilestoneEffect(
            kind=models.MilestoneEffectKind.replace_work_unit_parent,
            ref=work_unit.ref,
        ),
        models.WorkUnitMilestoneEffect(
            kind=models.MilestoneEffectKind.assign_work_unit,
            ref=work_unit.ref,
        ),
    )
    progress = models.MilestoneExecutionProgress.start(effects, (work_unit,))
    progress = progress.confirm(effects[0])
    progress = progress.confirm(effects[1])
    failure = progress.stop(
        models.GithubIndeterminateOperation(
            effect=effects[2],
            detail="response unavailable after invocation",
        )
    )

    print_milestone_failure(failure)

    output = capsys.readouterr().out
    assert "outcome=indeterminate" in output
    assert "confirmed complete:\n    create_milestone\n    create_ledger" in output
    assert "current operation:\n    replace_work_unit_parent owner/repo#3" in output
    assert "confirmed not attempted:\n    assign_work_unit owner/repo#3" in output
    assert "owner/repo#3 parent=#1 position=2 milestone=assigned title='old release'" in output
    assert "Recovery: reread the live GitHub milestone, issue tree, and assignments" in output
