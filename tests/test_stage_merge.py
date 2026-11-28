from __future__ import annotations

import json
import time
from pathlib import Path

from itree.models import SectionReplacement
from itree.stage import BodyCandidate, MergeConflict, merge_stage_values, sha256_text
from itree.work import ManagedIssueLayout, SectionName, parse_issue_layout
from tests.stage_support import (
    ISSUE_REF,
    TASKS,
    StageFixture,
    managed_body,
)


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


def _wait_for_path(path: Path) -> None:
    deadline = time.monotonic() + 30
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), path


def test_after_final_read_barrier_preserves_both_independent_writes(
    tmp_path: Path,
) -> None:
    fixture = StageFixture(tmp_path / "fixture")
    root = fixture.root / "writer"
    feature_a, _ = fixture.materialize("A-barrier")
    plan = (
        "Plan one after barrier β\n"
        "```itree-contract\n"
        'kind = "audit"\n'
        'evidence = "narrows"\n'
        "```\n"
    )
    feature_a.joinpath("plan.md").write_text(plan, encoding="utf-8")
    feature_b, _ = fixture.materialize("B-barrier")
    feature_b.joinpath("tasks.md").write_text(
        TASKS.replace("- [ ] T003", "- [x] T003"), encoding="utf-8"
    )
    process_a = fixture.spawn(
        "stage",
        "write-back",
        ISSUE_REF,
        "--attempt",
        "A-barrier",
        "--feature-directory",
        str(feature_a),
        writer=True,
        environment={"ITREE_STAGE_BARRIER_REQUEST": "request-A-barrier"},
    )
    _wait_for_path(root / "checkpoint.json")
    process_b = fixture.spawn(
        "stage",
        "write-back",
        ISSUE_REF,
        "--attempt",
        "B-barrier",
        "--feature-directory",
        str(feature_b),
        writer=True,
    )
    deadline = time.monotonic() + 30
    fixture.load()
    while len(fixture.state["comments"]) < 4 and time.monotonic() < deadline:
        time.sleep(0.02)
        fixture.load()
    assert len(fixture.state["comments"]) >= 4
    assert fixture.patches() == []
    assert process_a.poll() is None
    assert process_b.poll() is None
    checkpoint = json.loads(root.joinpath("checkpoint.json").read_text())
    assert checkpoint["position"] == "after_final_get_before_intent_and_patch"

    root.joinpath("release.json").write_text("release-A")
    _wait_for_path(root / "release.json")
    stdout_a, stderr_a = process_a.communicate(timeout=60)
    stdout_b, stderr_b = process_b.communicate(timeout=60)

    assert process_a.returncode == 0, stdout_a + stderr_a
    assert process_b.returncode == 0, stdout_b + stderr_b
    final = fixture.issue_body()
    assert "Plan one after barrier β" in final
    assert "- [x] T003" in final
    assert "Original Ω" in final
    confirmed = [
        comment
        for comment in fixture.state["comments"]
        if isinstance(comment, dict)
        and comment["body"].startswith("```itree-stage-receipt")
        and '"status":"confirmed"' in comment["body"]
    ]
    assert len(confirmed) == 2
    assert len(fixture.patches()) == 2


def test_barrier_is_default_off_for_an_ordinary_write(tmp_path: Path) -> None:
    fixture = StageFixture(tmp_path / "fixture")
    feature, _ = fixture.materialize("A-production-off")

    result = fixture.run(
        "stage",
        "write-back",
        ISSUE_REF,
        "--attempt",
        "A-production-off",
        "--feature-directory",
        str(feature),
        writer=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (fixture.root / "writer" / "checkpoint.json").exists()
    assert not (fixture.root / "writer" / "controller-trace.json").exists()
