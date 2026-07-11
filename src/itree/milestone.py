"""Pure preflight and ordered effects for GitHub milestone orchestration."""

from __future__ import annotations

from .github import GithubApi, GithubMutationError
from .models import (
    AssignedPriorMilestone,
    CreateMilestoneRequest,
    ExistingWorkUnit,
    GithubMilestone,
    MilestoneCreationResult,
    MilestoneCreationSucceeded,
    MilestoneEffect,
    MilestoneEffectKind,
    MilestoneExecutionProgress,
    MilestonePreflightErrorKind,
    MilestonePreflightRejected,
    ParentedPriorPlacement,
    ParentlessPriorPlacement,
    PlannedMilestoneEffect,
    RepoDag,
    UnassignedPriorMilestone,
    ValidatedMilestonePlan,
    WorkUnitMilestoneEffect,
    WorkUnitPlacement,
)
from .validate import is_grouping_issue, is_root_ledger

MilestonePreflightResult = ValidatedMilestonePlan | MilestonePreflightRejected


def _reject(
    kind: MilestonePreflightErrorKind,
    *references: str,
) -> MilestonePreflightRejected:
    return MilestonePreflightRejected(kind=kind, references=references)


def _reachable_from(dag: RepoDag, root_number: int) -> set[int]:
    reachable: set[int] = set()
    pending = [root_number]
    while pending:
        number = pending.pop()
        if number in reachable:
            continue
        reachable.add(number)
        pending.extend(dag.children_of[number])
    return reachable


def _is_ancestor(dag: RepoDag, ancestor: int, descendant: int) -> bool:
    current = descendant
    seen: set[int] = set()
    while current in dag.parent_of:
        if current in seen:
            return True
        seen.add(current)
        current = dag.parent_of[current]
        if current == ancestor:
            return True
    return False


def _malformed_structure_references(dag: RepoDag) -> tuple[str, ...]:
    """Identify edge states that cannot support deterministic orchestration."""
    parents_by_child: dict[int, list[int]] = {number: [] for number in dag.issues}
    duplicate_edges: list[int] = []
    incoming_counts = {number: 0 for number in dag.issues}
    for parent, children in dag.children_of.items():
        if len(children) != len(set(children)):
            duplicate_edges.append(parent)
        for child in children:
            parents_by_child[child].append(parent)
            incoming_counts[child] += 1

    multiple_parent_children = tuple(child for child, parents in parents_by_child.items() if len(parents) > 1)
    pending = [number for number, count in incoming_counts.items() if count == 0]
    removed: set[int] = set()
    while pending:
        number = pending.pop()
        if number in removed:
            continue
        removed.add(number)
        for child in dag.children_of[number]:
            incoming_counts[child] -= 1
            if incoming_counts[child] == 0:
                pending.append(child)
    cycle_numbers = tuple(number for number in dag.issues if number not in removed)

    malformed: list[int] = []
    for number in (*duplicate_edges, *multiple_parent_children, *cycle_numbers):
        if number not in malformed:
            malformed.append(number)
    return tuple(f"{dag.repo_ref.slug}#{number}" for number in malformed)


def _planned_effects(work_units: tuple[ExistingWorkUnit, ...]) -> tuple[PlannedMilestoneEffect, ...]:
    effects: list[PlannedMilestoneEffect] = [
        MilestoneEffect(kind=MilestoneEffectKind.create_milestone),
        MilestoneEffect(kind=MilestoneEffectKind.create_ledger),
        MilestoneEffect(kind=MilestoneEffectKind.attach_ledger),
        MilestoneEffect(kind=MilestoneEffectKind.assign_ledger),
    ]
    for work_unit in work_units:
        move_kind = MilestoneEffectKind.attach_work_unit if work_unit.placement is WorkUnitPlacement.attach else MilestoneEffectKind.replace_work_unit_parent
        effects.extend(
            (
                WorkUnitMilestoneEffect(kind=move_kind, ref=work_unit.ref),
                WorkUnitMilestoneEffect(kind=MilestoneEffectKind.assign_work_unit, ref=work_unit.ref),
            )
        )
    return tuple(effects)


def preflight_milestone(
    request: CreateMilestoneRequest,
    dag: RepoDag,
    milestones: tuple[GithubMilestone, ...],
) -> MilestonePreflightResult:
    """Validate the complete remote snapshot and construct the only write plan."""
    if dag.repo_ref != request.repo_ref:
        return _reject(
            MilestonePreflightErrorKind.repository_malformed,
            dag.repo_ref.slug,
            request.repo_ref.slug,
        )

    malformed_references = _malformed_structure_references(dag)
    if malformed_references:
        return _reject(
            MilestonePreflightErrorKind.repository_malformed,
            *malformed_references,
        )

    matching_milestones = tuple(milestone for milestone in milestones if milestone.title.value == request.title.value)
    if matching_milestones:
        return _reject(
            MilestonePreflightErrorKind.milestone_title_collision,
            *(f"milestone:{milestone.number}" for milestone in matching_milestones),
        )

    matching_ledgers = tuple(issue for issue in dag.issues.values() if issue.title == request.title.ledger_title)
    if matching_ledgers:
        return _reject(
            MilestonePreflightErrorKind.ledger_title_collision,
            *(f"{request.repo_ref.slug}#{issue.number}" for issue in matching_ledgers),
        )

    parent = dag.issues.get(request.parent.number)
    if parent is None or not parent.is_open or not is_grouping_issue(parent.title):
        return _reject(MilestonePreflightErrorKind.parent_invalid, request.parent.slug)

    root_numbers = tuple(number for number, issue in dag.issues.items() if number not in dag.parent_of and issue.is_open and is_root_ledger(issue.title))
    if len(root_numbers) != 1:
        return _reject(
            MilestonePreflightErrorKind.repository_malformed,
            *(f"{request.repo_ref.slug}#{number}" for number in root_numbers),
        )

    reachable = _reachable_from(dag, root_numbers[0])
    if request.parent.number not in reachable:
        return _reject(MilestonePreflightErrorKind.parent_invalid, request.parent.slug)

    requested_numbers = tuple(ref.number for ref in request.work_units)
    if len(set(requested_numbers)) != len(requested_numbers):
        duplicates = tuple(number for number in requested_numbers if requested_numbers.count(number) > 1)
        return _reject(
            MilestonePreflightErrorKind.duplicate_work_unit,
            *(f"{request.repo_ref.slug}#{number}" for number in dict.fromkeys(duplicates)),
        )

    selected_numbers = set(requested_numbers)
    unrelated_open_orphans = tuple(number for number, issue in dag.issues.items() if issue.is_open and number not in reachable and number not in selected_numbers)
    if unrelated_open_orphans:
        return _reject(
            MilestonePreflightErrorKind.repository_malformed,
            *(f"{request.repo_ref.slug}#{number}" for number in unrelated_open_orphans),
        )

    work_units: list[ExistingWorkUnit] = []
    for ref in request.work_units:
        issue = dag.issues.get(ref.number)
        if issue is None or not issue.is_open or issue.is_pull_request:
            return _reject(MilestonePreflightErrorKind.invalid_work_unit, ref.slug)

        prior_parent = dag.parent_of.get(ref.number)
        if ref.number not in reachable and prior_parent is not None:
            return _reject(MilestonePreflightErrorKind.repository_malformed, ref.slug)
        if _is_ancestor(dag, ref.number, request.parent.number):
            return _reject(MilestonePreflightErrorKind.cycle_risk, ref.slug, request.parent.slug)
        if is_grouping_issue(issue.title) or dag.children_of[ref.number]:
            return _reject(MilestonePreflightErrorKind.invalid_work_unit, ref.slug)

        prior_placement = (
            ParentlessPriorPlacement(kind=WorkUnitPlacement.attach)
            if prior_parent is None
            else ParentedPriorPlacement(
                kind=WorkUnitPlacement.replace_parent,
                parent_number=prior_parent,
                position=dag.children_of[prior_parent].index(ref.number),
            )
        )
        prior_milestone = UnassignedPriorMilestone(kind="unassigned") if issue.milestone is None else AssignedPriorMilestone(kind="assigned", title=issue.milestone.title)
        work_units.append(
            ExistingWorkUnit(
                ref=ref,
                issue_id=issue.id,
                prior_placement=prior_placement,
                prior_milestone=prior_milestone,
            )
        )

    typed_work_units = tuple(work_units)
    return ValidatedMilestonePlan(
        request=request,
        parent_issue=parent,
        work_units=typed_work_units,
        effects=_planned_effects(typed_work_units),
    )


def _current_effect(
    progress: MilestoneExecutionProgress,
    kind: MilestoneEffectKind,
) -> PlannedMilestoneEffect:
    effect = progress.current
    assert effect.kind is kind, f"milestone executor reached an effect out of order; expected={kind}; found={effect}; confirmed={progress.confirmed}; fix the execution plan"
    return effect


def _current_work_unit_effect(
    progress: MilestoneExecutionProgress,
    kind: MilestoneEffectKind,
    work_unit: ExistingWorkUnit,
) -> WorkUnitMilestoneEffect:
    effect = _current_effect(progress, kind)
    assert isinstance(effect, WorkUnitMilestoneEffect), (
        f"work-unit effect must carry its issue reference; expected_ref={work_unit.ref.slug}; found={effect}; fix milestone preflight"
    )
    assert effect.ref == work_unit.ref, f"work-unit effect target must follow CLI order; expected={work_unit.ref.slug}; found={effect.ref.slug}; fix milestone preflight"
    return effect


def execute_milestone(
    plan: ValidatedMilestonePlan,
    api: GithubApi,
) -> MilestoneCreationResult:
    """Execute one validated plan in order and stop at the first typed failure."""
    progress = MilestoneExecutionProgress.start(plan.effects, plan.work_units)
    try:
        effect = _current_effect(progress, MilestoneEffectKind.create_milestone)
        milestone = api.create_planned_milestone(plan.request.title, effect)
        progress = progress.confirm(effect)

        effect = _current_effect(progress, MilestoneEffectKind.create_ledger)
        ledger = api.create_planned_issue(
            plan.request.title.ledger_title,
            plan.request.body,
            effect,
        )
        progress = progress.confirm(effect)

        effect = _current_effect(progress, MilestoneEffectKind.attach_ledger)
        api.attach_planned_subissue(plan.request.parent.number, ledger.id, effect)
        progress = progress.confirm(effect)

        effect = _current_effect(progress, MilestoneEffectKind.assign_ledger)
        assigned_ledger = api.assign_planned_issue_milestone(
            ledger.number,
            milestone,
            effect,
        )
        if not plan.work_units:
            return MilestoneCreationSucceeded(
                milestone=milestone,
                ledger=assigned_ledger,
                work_units=plan.work_units,
            )
        progress = progress.confirm(effect)

        for index, work_unit in enumerate(plan.work_units):
            move_kind = MilestoneEffectKind.attach_work_unit if work_unit.placement is WorkUnitPlacement.attach else MilestoneEffectKind.replace_work_unit_parent
            effect = _current_work_unit_effect(progress, move_kind, work_unit)
            if work_unit.placement is WorkUnitPlacement.attach:
                api.attach_planned_subissue(ledger.number, work_unit.issue_id, effect)
            else:
                api.replace_planned_parent(ledger.number, work_unit.issue_id, effect)
            progress = progress.confirm(effect)

            effect = _current_work_unit_effect(
                progress,
                MilestoneEffectKind.assign_work_unit,
                work_unit,
            )
            api.assign_planned_issue_milestone(
                work_unit.ref.number,
                milestone,
                effect,
            )
            if index == len(plan.work_units) - 1:
                return MilestoneCreationSucceeded(
                    milestone=milestone,
                    ledger=assigned_ledger,
                    work_units=plan.work_units,
                )
            progress = progress.confirm(effect)
    except GithubMutationError as error:
        return progress.stop(error.outcome)

    raise AssertionError(f"milestone execution exhausted without success or typed failure; plan={plan}; progress={progress}; fix execute_milestone")
