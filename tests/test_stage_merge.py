from __future__ import annotations

from itree.models import SectionReplacement
from itree.stage import BodyCandidate, MergeConflict, merge_stage_values, sha256_text
from itree.work import ManagedIssueLayout, SectionName, parse_issue_layout
from tests.stage_support import TASKS, managed_body


def _managed(body: str) -> ManagedIssueLayout:
    layout = parse_issue_layout(body)
    assert isinstance(layout, ManagedIssueLayout)
    return layout


def _replacement(section: str, base: str, proposed: str) -> SectionReplacement:
    assert section in {"requirements", "plan", "tasks"}
    return SectionReplacement(
        section=section,
        base_text=base,
        proposed_text=proposed,
        base_sha256=sha256_text(base),
        proposed_sha256=sha256_text(proposed),
    )


def _plan_text(content: str) -> str:
    return content + '```itree-contract\nkind = "audit"\nevidence = "narrows"\n```\n'


def _plan(layout: ManagedIssueLayout) -> str:
    return next(
        section.text for section in layout.sections if section.name == SectionName.plan
    )


def _tasks(layout: ManagedIssueLayout) -> str:
    return next(
        section.text for section in layout.sections if section.name == SectionName.tasks
    )


def test_stale_plan_merge_retains_the_late_task_edit() -> None:
    base = _managed(managed_body())
    live = _managed(managed_body(tasks=TASKS.replace("- [x] T001", "- [ ] T001")))
    proposal = _replacement("plan", _plan(base), _plan_text("Plan one β\n"))

    candidate = merge_stage_values(base, live, (proposal,))

    assert isinstance(candidate, BodyCandidate)
    merged = _managed(candidate.body)
    assert _plan(merged) == _plan_text("Plan one β\n")
    assert _tasks(merged) == _tasks(live)
    assert "Original Ω" in candidate.body
    assert candidate.before_fences == candidate.after_fences


def test_reversed_independent_submission_order_composes() -> None:
    base = _managed(managed_body())
    live = _managed(managed_body(plan="Plan one β\n"))
    task_value = _tasks(base).replace("- [x] T001", "- [ ] T001")
    proposal = _replacement("tasks", _tasks(base), task_value)

    candidate = merge_stage_values(base, live, (proposal,))

    assert isinstance(candidate, BodyCandidate)
    merged = _managed(candidate.body)
    assert _plan(merged) == _plan_text("Plan one β\n")
    assert _tasks(merged) == task_value


def test_same_section_divergence_is_a_typed_conflict() -> None:
    base = _managed(managed_body())
    live = _managed(managed_body(plan="Plan accepted first\n"))
    proposal = _replacement(
        "plan", _plan(base), _plan_text("Plan incompatible later\n")
    )

    conflict = merge_stage_values(base, live, (proposal,))

    assert isinstance(conflict, MergeConflict)
    assert conflict.section == "plan"
    assert conflict.retained_body == live.body
    assert "Plan accepted first" in conflict.retained_body
    assert "Plan incompatible later" not in conflict.retained_body


def test_noop_value_matches_current_live_even_when_its_base_is_stale() -> None:
    base = _managed(managed_body())
    live = _managed(managed_body(plan="Plan accepted first\n"))
    proposal = _replacement("plan", _plan(base), _plan_text("Plan accepted first\n"))

    candidate = merge_stage_values(base, live, (proposal,))

    assert isinstance(candidate, BodyCandidate)
    assert candidate.body == live.body
