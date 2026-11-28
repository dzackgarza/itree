from __future__ import annotations

import json

import pytest
from pydantic import JsonValue

from itree.models import (
    DocumentedTestEvidence,
    PendingEvidence,
    QualifiedTaskRef,
    ResolvedEvidence,
    TaskField,
    TaskRecord,
)
from itree.stage import (
    TaskDocument,
    TaskFieldConflict,
    TaskWireFailure,
    merge_task_documents,
    parse_task_document,
    replace_task_field,
    reorder_task_document,
)
from tests.stage_support import TASKS


def _document(text: str = TASKS) -> TaskDocument:
    parsed = parse_task_document(text)
    assert isinstance(parsed, TaskDocument), parsed
    return parsed


def _field_raw(task: TaskRecord, field: TaskField) -> str:
    span = (
        task.state_span if field == TaskField.state else task.field_spans[field.value]
    )
    return task.source[span.character_start : span.character_end]


def _replace_once(source: str, old: str, new: str) -> str:
    assert source.count(old) == 1, old
    return source.replace(old, new, 1)


def test_native_checkbox_and_every_semantic_span_round_trip() -> None:
    document = _document()
    first, second = document.tasks

    assert first.id == "T001"
    assert first.state == "complete"
    assert second.id == "T003"
    assert second.state == "unfinished"
    assert second.prerequisites[0].task_id == "T001"
    qualified = second.prerequisites[1]
    assert isinstance(qualified, QualifiedTaskRef)
    assert qualified.issue_ref.slug == "OWNER/B-REPO#2"
    assert qualified.task_id == "T001"
    assert second.requirements == ("R71-10", "R71-03")
    assert isinstance(first.evidence, ResolvedEvidence)
    assert isinstance(first.evidence.resolved[0], DocumentedTestEvidence)
    assert isinstance(second.evidence, PendingEvidence)
    assert second.goals[0].rationale == "preserve the task for a new session"
    assert first.state_span.byte_end - first.state_span.byte_start == 1
    assert first.source.encode()[3:4] == b"x"
    assert second.source.encode()[3:4] == b" "
    assert parse_task_document(document.source) == document


@pytest.mark.parametrize(
    ("field", "raw_value"),
    [
        (TaskField.state, " "),
        (TaskField.prerequisites, '["T003"]'),
        (TaskField.requirements, '["R71-03", "R71-11"]'),
        (
            TaskField.evidence,
            '{"resolved":[{"kind":"commit","repository":"dzackgarza/itree","sha":"6f704600024a6e1a759b1d94c742262656a47930","url":"https://github.com/dzackgarza/itree/commit/6f704600024a6e1a759b1d94c742262656a47930"}]}',
        ),
        (
            TaskField.goals,
            '[{"id":"NS-003","rationale":"retain independently checkable proof"}]',
        ),
        (TaskField.description, '"Changed description"'),
        (TaskField.acceptance, '"Changed acceptance"'),
        (TaskField.order, "0"),
    ],
)
def test_each_declared_field_edit_splices_only_its_raw_span(
    field: TaskField, raw_value: str
) -> None:
    document = _document()
    task = document.tasks[0]

    updated = replace_task_field(document, task.id, field, raw_value)

    assert isinstance(updated, TaskDocument)
    reparsed = _document(updated.source)
    assert reparsed.tasks[0].id == task.id
    for other_field in TaskField:
        if other_field in {TaskField.opaque, field}:
            continue
        assert _field_raw(reparsed.tasks[0], other_field) == _field_raw(
            task, other_field
        )
    before = document.source.encode()
    after = updated.source.encode()
    if field == TaskField.state:
        differing = [
            index for index in range(len(before)) if before[index] != after[index]
        ]
        assert len(differing) == 1
        assert differing[0] == document.source.encode().index(task.source.encode()) + 3


def test_disjoint_state_and_evidence_writes_preserve_all_other_fields() -> None:
    base = _document()
    live = _document(
        _replace_once(
            TASKS,
            '{"pending":"record the public task merge test receipt"}',
            '{"pending":"record the independently persisted test receipt"}',
        )
    )
    proposed = _document(_replace_once(TASKS, "- [ ] T003", "- [x] T003"))

    merged = merge_task_documents(base, live, proposed)

    assert isinstance(merged, TaskDocument)
    third = merged.tasks[1]
    assert third.state == "complete"
    assert third.evidence == live.tasks[1].evidence
    assert third.prerequisites == live.tasks[1].prerequisites
    assert third.goals == live.tasks[1].goals
    assert merged.opaque == live.opaque


def test_reorder_moves_whole_items_and_changes_only_order_values() -> None:
    document = _document()

    reordered = reorder_task_document(document, ("T003", "T001"))

    assert isinstance(reordered, TaskDocument)
    assert [task.id for task in reordered.tasks] == ["T003", "T001"]
    assert [task.order for task in reordered.tasks] == [0, 1]
    assert reordered.tasks[0].state == "unfinished"
    assert reordered.tasks[0].evidence == document.tasks[1].evidence
    assert reordered.tasks[1].evidence == document.tasks[0].evidence
    assert reordered.opaque == document.opaque


def test_opaque_legacy_tasks_allow_sections_but_reject_field_edits() -> None:
    document = _document("- [ ] T001 plain upstream text\n")

    assert not document.wrapped
    assert document.tasks == ()
    assert document.opaque[0].source == "- [ ] T001 plain upstream text\n"
    rejected = replace_task_field(document, "T001", TaskField.state, "x")

    assert isinstance(rejected, TaskWireFailure)
    assert rejected.reason == "unsupported_task_wire"


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        (_replace_once(TASKS, "- [x] T001", "- [X] T001"), "unsupported_task_wire"),
        (
            _replace_once(
                TASKS, "    order = 0\n", '    order = 0\n    state = "complete"\n'
            ),
            "unsupported_task_wire",
        ),
        (
            _replace_once(TASKS, "- [x] T001 id", "- [x] [ ] T001 id"),
            "unsupported_task_wire",
        ),
        (
            _replace_once(TASKS, "- [x] T001 id", "- [x] T999 id"),
            "unsupported_task_wire",
        ),
        (
            _replace_once(
                TASKS, '    acceptance = "Its checkbox and fields persist."\n', ""
            ),
            "unsupported_task_wire",
        ),
        (
            _replace_once(
                TASKS,
                "    prerequisites = []\n    requirements",
                '    requirements = ["R71-10"]\n    prerequisites = []\n    evidence',
            ),
            "unsupported_task_wire",
        ),
        (
            _replace_once(TASKS, '"kind":"documented_test"', '"kind":"tests"'),
            "unknown_evidence_kind",
        ),
        (
            _replace_once(
                TASKS,
                ',"report":{"repository":"dzackgarza/itree","issue_number":71,"attempt_id":"A-T015","report_id":"v1-box-and-fields"}',
                "",
            ),
            "unsupported_task_wire",
        ),
        (
            _replace_once(TASKS, '"issue_number":71', '"issue_number":"71"'),
            "unsupported_task_wire",
        ),
        (
            _replace_once(
                TASKS,
                '    prerequisites = ["T001", "OWNER/B-REPO#2:T001"]',
                '    prerequisites = ["#2:T001"]',
            ),
            "malformed_ref",
        ),
        (_replace_once(TASKS, "    order = 0\n", "    order = 1\n"), "invalid_order"),
        (
            _replace_once(
                TASKS,
                '"rationale":"preserve a completed task for handoff"',
                '"rationale":""',
            ),
            "unsupported_task_wire",
        ),
        (
            _replace_once(
                TASKS,
                '    acceptance = "Its checkbox and fields persist."',
                '    acceptance = "two\nlines"',
            ),
            "unsupported_task_wire",
        ),
    ],
)
def test_invalid_task_wire_has_one_closed_failure(source: str, reason: str) -> None:
    parsed = parse_task_document(source)

    assert isinstance(parsed, TaskWireFailure)
    assert parsed.reason == reason


def test_pending_and_resolved_evidence_cannot_be_combined() -> None:
    evidence = json.loads(TASKS.split("    evidence = ", 1)[1].splitlines()[0])
    assert isinstance(evidence, dict)
    evidence["pending"] = "inconsistent"
    source = _replace_once(
        TASKS,
        TASKS.split("    evidence = ", 1)[1].splitlines()[0],
        json.dumps(evidence, separators=(",", ":")),
    )

    parsed = parse_task_document(source)

    assert isinstance(parsed, TaskWireFailure)
    assert parsed.reason == "unsupported_task_wire"


def test_duplicate_task_ids_are_rejected() -> None:
    first = TASKS.split("- [ ] T003", 1)[0].split(
        "<!-- itree-task-document:v1:start -->\n", 1
    )[1]
    duplicate = (
        "<!-- itree-task-document:v1:start -->\n"
        + first
        + first.replace("    order = 0\n", "    order = 1\n")
        + "<!-- itree-task-document:v1:end -->\n"
    )

    parsed = parse_task_document(duplicate)

    assert isinstance(parsed, TaskWireFailure)
    assert parsed.reason == "duplicate_id"


def test_same_field_divergence_is_not_a_set_union() -> None:
    base = _document()
    live = _document(
        _replace_once(TASKS, '["T001", "OWNER/B-REPO#2:T001"]', '["T003"]')
    )
    proposed = _document(
        _replace_once(TASKS, '["T001", "OWNER/B-REPO#2:T001"]', '["T001"]')
    )

    conflict = merge_task_documents(base, live, proposed)

    assert isinstance(conflict, TaskFieldConflict)
    assert conflict.task_id == "T003"
    assert conflict.field == TaskField.prerequisites
    assert conflict.retained_source == live.source
