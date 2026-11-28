from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import JsonValue, TypeAdapter, ValidationError

from .github import GithubApi, GithubApiError
from .models import (
    ArtifactDraft,
    ArtifactIndexEntry,
    ArtifactKey,
    ArtifactKind,
    ArtifactPayload,
    ArtifactProposal,
    AttemptId,
    DeclaredOrder,
    EvidenceRef,
    FinalizedProposal,
    GithubIssue,
    GoalContribution,
    GoalId,
    IssueNodeId,
    IssueRef,
    LegacyIntakeProposal,
    LocalTaskRef,
    MissingPart,
    OpaqueSpan,
    PartDescriptor,
    PendingEvidence,
    PendingUpload,
    QualifiedTaskRef,
    RequestId,
    RequirementId,
    ResolvedEvidence,
    RestartedAttempt,
    SectionEditProposal,
    SectionReplacement,
    SessionAdmissionRequest,
    SessionReleaseRequest,
    Sha256,
    SourceSpan,
    StageModel,
    TaskDocument,
    TaskField,
    TaskFieldProposal,
    TaskId,
    TaskParseFailure,
    TaskPrerequisite,
    TaskRecord,
    UploadContent,
    UploadManifest,
    UploadPart,
    UploadRejected,
    UploadRejectionReason,
    WriterSealInvalidContext,
    UploadResolution,
)
from .work import (
    LegacyIssueLayout,
    ManagedIssueLayout,
    SectionName,
    ValidIssueLayout,
    contract_fence_inventory,
    parse_issue_layout,
)


class StageCommandResult(StageModel):
    exit_code: int
    lines: tuple[str, ...]


class ProjectionFailure(StageModel):
    reason: Literal[
        "invalid_layout",
        "ambiguous_boundary",
        "legacy_intake_required",
        "legacy_missing_final_newline",
        "missing_file",
        "unexpected_file_change",
        "contract_fence_change",
        "original_contract_target",
        "missing_node",
        "issue_unavailable",
        "wrong_repository",
        "node_mismatch",
        "wrong_attempt",
        "unsupported_task_wire",
    ]
    exit_code: Literal[2, 3, 4, 5]
    detail: str


class StageFailure(RuntimeError):
    def __init__(self, failure: ProjectionFailure) -> None:
        self.failure = failure
        super().__init__(failure.detail)


class NoBodyChange(StageModel):
    kind: Literal["no_body_change"] = "no_body_change"
    live_body_sha256: Sha256


class BodyCandidate(StageModel):
    kind: Literal["body_candidate"] = "body_candidate"
    body: str
    body_sha256: Sha256
    original_sha256: Sha256
    changed_sections: tuple[str, ...]
    before_fences: tuple[str, ...]
    after_fences: tuple[str, ...]


class MergeConflict(StageModel):
    kind: Literal["conflict"] = "conflict"
    section: Literal["original-contract", "requirements", "plan", "tasks"]
    task_id: TaskId | None = None
    field: TaskField | None = None
    retained_body: str
    competing_request_id: RequestId | None = None


class TaskWireFailure(StageModel):
    kind: Literal["task_parse_failure"] = "task_parse_failure"
    reason: TaskParseFailure
    detail: str
    task_id: TaskId | None = None
    source: str


class TaskFieldConflict(StageModel):
    kind: Literal["task_field_conflict"] = "task_field_conflict"
    task_id: TaskId | None
    field: TaskField
    retained_source: str


class ScratchMetadata(StageModel):
    version: Literal[1]
    issue_ref: IssueRef
    issue_node_id: IssueNodeId
    attempt_id: AttemptId
    layout: Literal["legacy", "managed"]
    base_body: str
    body_sha256: Sha256
    original_sha256: Sha256


def sha256_text(text: str) -> Sha256:
    return Sha256(hashlib.sha256(text.encode("utf-8")).hexdigest())


def _defect(failure: ProjectionFailure) -> StageCommandResult:
    return StageCommandResult(
        exit_code=failure.exit_code,
        lines=(f"reason={failure.reason}", f"detail={failure.detail}"),
    )


def _result(lines: tuple[str, ...], exit_code: int = 0) -> StageCommandResult:
    return StageCommandResult(exit_code=exit_code, lines=lines)


def require_issue_node(issue: GithubIssue) -> IssueNodeId:
    node = issue.node_id
    if node is None or not node:
        raise StageFailure(
            ProjectionFailure(
                reason="missing_node",
                exit_code=3,
                detail=f"issue {issue.repository_slug}#{issue.number} has no REST node_id",
            )
        )
    return node


def _layout_or_raise(body: str) -> ValidIssueLayout:
    layout = parse_issue_layout(body)
    if layout.kind == "invalid":
        first = layout.errors[0]
        raise StageFailure(
            ProjectionFailure(
                reason="invalid_layout",
                exit_code=2,
                detail=f"{first.name}:{first.line}:{first.reason}",
            )
        )
    return layout


def _read_layout(
    api: GithubApi, issue: IssueRef
) -> tuple[GithubIssue, ValidIssueLayout]:
    try:
        live = api.get_issue(issue.number)
    except GithubApiError as error:
        raise StageFailure(
            ProjectionFailure(
                reason="issue_unavailable", exit_code=3, detail=str(error)
            )
        ) from error
    assert live.repository_slug == issue.repo_ref.slug, (
        live.repository_slug,
        issue.repo_ref.slug,
    )
    require_issue_node(live)
    return live, _layout_or_raise(live.body or "")


def _section(layout: ManagedIssueLayout, name: SectionName) -> str:
    return next(section.text for section in layout.sections if section.name == name)


def _validate_replacement_text(text: str) -> None:
    if text and not text.endswith("\n"):
        raise StageFailure(
            ProjectionFailure(
                reason="ambiguous_boundary",
                exit_code=2,
                detail="non-empty replacement text must end in LF",
            )
        )


def replace_sections(
    layout: ManagedIssueLayout, replacements: tuple[SectionReplacement, ...]
) -> BodyCandidate | NoBodyChange:
    if not replacements:
        return NoBodyChange(live_body_sha256=sha256_text(layout.body))
    by_name = {section.name.value: section for section in layout.sections}
    changed: list[str] = []
    for replacement in replacements:
        section = by_name[replacement.section]
        assert replacement.base_text == section.text, replacement.section
        assert replacement.base_sha256 == sha256_text(section.text), replacement.section
        assert replacement.proposed_sha256 == sha256_text(replacement.proposed_text), (
            replacement.section
        )
        _validate_replacement_text(replacement.proposed_text)
    sorted_replacements = sorted(
        replacements, key=lambda item: by_name[item.section].byte_start
    )
    body_bytes = layout.body.encode("utf-8")
    cursor = 0
    candidate_bytes = bytearray()
    for replacement in sorted_replacements:
        section = by_name[replacement.section]
        candidate_bytes.extend(body_bytes[cursor : section.byte_start])
        candidate_bytes.extend(replacement.proposed_text.encode("utf-8"))
        cursor = section.byte_end
        if replacement.base_text != replacement.proposed_text:
            changed.append(replacement.section)
    candidate_bytes.extend(body_bytes[cursor:])
    candidate = candidate_bytes.decode("utf-8")
    if candidate == layout.body:
        return NoBodyChange(live_body_sha256=sha256_text(layout.body))
    reparsed = _layout_or_raise(candidate)
    assert isinstance(reparsed, ManagedIssueLayout), reparsed
    original_before = _section(layout, SectionName.original_contract)
    original_after = _section(reparsed, SectionName.original_contract)
    assert original_before == original_after, "original contract changed"
    before_fences = tuple(fence.text for fence in contract_fence_inventory(layout.body))
    after_fences = tuple(fence.text for fence in contract_fence_inventory(candidate))
    if before_fences != after_fences:
        raise StageFailure(
            ProjectionFailure(
                reason="contract_fence_change",
                exit_code=4,
                detail="every top-level itree-contract fence must survive byte for byte",
            )
        )
    return BodyCandidate(
        body=candidate,
        body_sha256=sha256_text(candidate),
        original_sha256=sha256_text(original_after),
        changed_sections=tuple(changed),
        before_fences=before_fences,
        after_fences=after_fences,
    )


TASK_DOCUMENT_START = "<!-- itree-task-document:v1:start -->\n"
TASK_DOCUMENT_END = "<!-- itree-task-document:v1:end -->"
TASK_FIRST_LINE = re.compile(
    r"^- \[(?P<state>[ x])\] (?P<visible>T[0-9]+) id = (?P<raw>[^\r\n]+)\n$"
)
TASK_CONTINUATION_LINE = re.compile(r"^    (?P<name>[a-z]+) = (?P<raw>[^\r\n]+)\n$")
TASK_CONTINUATION_NAMES = (
    "order",
    "prerequisites",
    "requirements",
    "evidence",
    "goals",
    "description",
    "acceptance",
)
EVIDENCE_ADAPTER: TypeAdapter[EvidenceRef] = TypeAdapter(EvidenceRef)
JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def _task_failure(
    source: str,
    reason: TaskParseFailure,
    detail: str,
    task_id: TaskId | None = None,
) -> TaskWireFailure:
    return TaskWireFailure(reason=reason, detail=detail, task_id=task_id, source=source)


def _json_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _json_value(raw: str) -> JsonValue:
    assert "\n" not in raw and "\r" not in raw, raw
    return JSON_VALUE_ADAPTER.validate_python(
        json.loads(raw, object_pairs_hook=_json_object)
    )


def _span(source: str, start: int, end: int) -> SourceSpan:
    return SourceSpan(
        character_start=start,
        character_end=end,
        byte_start=len(source[:start].encode("utf-8")),
        byte_end=len(source[:end].encode("utf-8")),
    )


def _prerequisites(
    source: str, raw: str
) -> tuple[TaskPrerequisite, ...] | TaskParseFailure:
    value = _json_value(raw)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return "unsupported_task_wire"
    prerequisites: list[TaskPrerequisite] = []
    for item in value:
        assert isinstance(item, str)
        if re.fullmatch(r"T[0-9]+", item):
            prerequisites.append(LocalTaskRef(task_id=TaskId(item)))
            continue
        qualified = re.fullmatch(
            r"(?P<issue>[^/\s]+/[^/\s]+#[1-9][0-9]*):(?P<task>T[0-9]+)", item
        )
        if qualified is None:
            return "malformed_ref"
        prerequisites.append(
            QualifiedTaskRef(
                issue_ref=IssueRef.parse(qualified["issue"]),
                task_id=TaskId(qualified["task"]),
            )
        )
    return tuple(prerequisites)


def _requirements(raw: str) -> tuple[RequirementId, ...] | TaskParseFailure:
    value = _json_value(raw)
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        return "unsupported_task_wire"
    return tuple(RequirementId(item) for item in value if isinstance(item, str))


def _evidence(
    source: str, raw: str, task_id: TaskId
) -> PendingEvidence | ResolvedEvidence | TaskWireFailure:
    value = _json_value(raw)
    if not isinstance(value, dict) or len(value) != 1:
        return _task_failure(
            source,
            "unsupported_task_wire",
            "evidence expects exactly pending or resolved",
            task_id,
        )
    if "pending" in value:
        pending = value["pending"]
        if not isinstance(pending, str) or not pending:
            return _task_failure(
                source,
                "unsupported_task_wire",
                "pending evidence must be nonempty text",
                task_id,
            )
        return PendingEvidence(pending=pending)
    resolved = value.get("resolved")
    if not isinstance(resolved, list) or not resolved:
        return _task_failure(
            source,
            "unsupported_task_wire",
            "resolved evidence must be a nonempty array",
            task_id,
        )
    references: list[EvidenceRef] = []
    for reference in resolved:
        if not isinstance(reference, dict):
            return _task_failure(
                source,
                "unsupported_task_wire",
                "evidence reference must be an object",
                task_id,
            )
        kind = reference.get("kind")
        if kind not in ("issue_comment", "commit", "pull_request", "documented_test"):
            return _task_failure(
                source,
                "unknown_evidence_kind",
                f"unknown evidence kind: {kind!r}",
                task_id,
            )
        try:
            references.append(EVIDENCE_ADAPTER.validate_python(reference, strict=True))
        except ValidationError as error:
            return _task_failure(source, "unsupported_task_wire", str(error), task_id)
    return ResolvedEvidence(resolved=tuple(references))


def _goals(
    source: str, raw: str, task_id: TaskId
) -> tuple[GoalContribution, ...] | TaskWireFailure:
    value = _json_value(raw)
    if not isinstance(value, list):
        return _task_failure(
            source, "unsupported_task_wire", "goals must be an array", task_id
        )
    goals: list[GoalContribution] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"id", "rationale"}:
            return _task_failure(
                source,
                "unsupported_task_wire",
                "each goal requires exactly id and rationale",
                task_id,
            )
        identifier = item["id"]
        rationale = item["rationale"]
        if (
            not isinstance(identifier, str)
            or not identifier
            or not isinstance(rationale, str)
            or not rationale
        ):
            return _task_failure(
                source,
                "unsupported_task_wire",
                "goal id and rationale must be nonempty",
                task_id,
            )
        goals.append(GoalContribution(id=GoalId(identifier), rationale=rationale))
    return tuple(goals)


def _parse_task_item(
    lines: tuple[str, ...], source: str, position: int
) -> TaskRecord | TaskWireFailure:
    first = TASK_FIRST_LINE.fullmatch(lines[0])
    if first is None or len(lines) != 8:
        return _task_failure(
            source,
            "unsupported_task_wire",
            "expected one complete eight-line task item",
        )
    try:
        identifier_value = _json_value(first["raw"])
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        return _task_failure(source, "unsupported_task_wire", str(error))
    if not isinstance(identifier_value, str) or not re.fullmatch(
        r"T[0-9]+", identifier_value
    ):
        return _task_failure(source, "missing_id", "task id must be a JSON T... string")
    task_id = TaskId(identifier_value)
    if first["visible"] != task_id:
        return _task_failure(
            source, "unsupported_task_wire", "visible and JSON task IDs differ", task_id
        )
    spans: dict[str, SourceSpan] = {
        "id": _span(source, first.start("raw"), first.end("raw"))
    }
    values: dict[str, str] = {"id": first["raw"]}
    for index, expected_name in enumerate(TASK_CONTINUATION_NAMES, start=1):
        match = TASK_CONTINUATION_LINE.fullmatch(lines[index])
        if match is None or match["name"] != expected_name:
            return _task_failure(
                source,
                "unsupported_task_wire",
                f"expected {expected_name} at member {index + 1}",
                task_id,
            )
        line_start = sum(len(line) for line in lines[:index])
        spans[expected_name] = _span(
            source,
            line_start + match.start("raw"),
            line_start + match.end("raw"),
        )
        values[expected_name] = match["raw"]
    try:
        order_value = _json_value(values["order"])
        prerequisites = _prerequisites(source, values["prerequisites"])
        requirements = _requirements(values["requirements"])
        evidence = _evidence(source, values["evidence"], task_id)
        goals = _goals(source, values["goals"], task_id)
        description = _json_value(values["description"])
        acceptance = _json_value(values["acceptance"])
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        return _task_failure(source, "unsupported_task_wire", str(error), task_id)
    if isinstance(prerequisites, str):
        return _task_failure(
            source, prerequisites, "invalid prerequisite list", task_id
        )
    if isinstance(requirements, str):
        return _task_failure(source, requirements, "invalid requirement list", task_id)
    if isinstance(evidence, TaskWireFailure):
        return evidence
    if isinstance(goals, TaskWireFailure):
        return goals
    if (
        not isinstance(order_value, int)
        or isinstance(order_value, bool)
        or order_value < 0
    ):
        return _task_failure(
            source, "invalid_order", "order must be a nonnegative JSON integer", task_id
        )
    if order_value != position:
        return _task_failure(
            source,
            "invalid_order",
            f"order {order_value} does not equal item position {position}",
            task_id,
        )
    if not isinstance(description, str) or not isinstance(acceptance, str):
        return _task_failure(
            source,
            "unsupported_task_wire",
            "description and acceptance must be JSON strings",
            task_id,
        )
    return TaskRecord(
        id=task_id,
        order=DeclaredOrder(order_value),
        state="complete" if first["state"] == "x" else "unfinished",
        prerequisites=prerequisites,
        requirements=requirements,
        evidence=evidence,
        goals=goals,
        description=description,
        acceptance=acceptance,
        source=source,
        field_spans=spans,
        state_span=_span(source, 3, 4),
    )


def parse_task_document(source: str) -> TaskDocument | TaskWireFailure:
    if TASK_DOCUMENT_START not in source and TASK_DOCUMENT_END not in source:
        return TaskDocument(
            source=source,
            wrapped=False,
            tasks=(),
            opaque=(OpaqueSpan(source=source, span=_span(source, 0, len(source))),),
        )
    if source.count(TASK_DOCUMENT_START) != 1 or source.count(TASK_DOCUMENT_END) != 1:
        return _task_failure(
            source, "ambiguous_span", "expected exactly one v1 document wrapper"
        )
    start = source.index(TASK_DOCUMENT_START)
    end = source.index(TASK_DOCUMENT_END)
    if end < start:
        return _task_failure(source, "ambiguous_span", "v1 end precedes start")
    inner_start = start + len(TASK_DOCUMENT_START)
    inner = source[inner_start:end]
    lines = tuple(inner.splitlines(keepends=True))
    tasks: list[TaskRecord] = []
    cursor = 0
    while cursor < len(lines):
        line = lines[cursor]
        if line.startswith("- ["):
            if cursor + 8 > len(lines):
                return _task_failure(
                    source, "unsupported_task_wire", "task item is truncated"
                )
            item_source = "".join(lines[cursor : cursor + 8])
            record = _parse_task_item(
                lines[cursor : cursor + 8], item_source, len(tasks)
            )
            if isinstance(record, TaskWireFailure):
                return record
            tasks.append(record)
            cursor += 8
            continue
        cursor += 1
    seen: set[TaskId] = set()
    for task in tasks:
        if task.id in seen:
            return _task_failure(
                source, "duplicate_id", f"duplicate task ID {task.id}", task.id
            )
        seen.add(task.id)
    returned = TaskDocument(source=source, wrapped=True, tasks=tuple(tasks), opaque=())
    opaque: list[OpaqueSpan] = []
    cursor = 0
    for task in tasks:
        position = source.index(task.source, cursor)
        if position > cursor:
            opaque.append(
                OpaqueSpan(
                    source=source[cursor:position], span=_span(source, cursor, position)
                )
            )
        cursor = position + len(task.source)
    if cursor < len(source):
        opaque.append(
            OpaqueSpan(source=source[cursor:], span=_span(source, cursor, len(source)))
        )
    return returned.model_copy(update={"opaque": tuple(opaque)})


def _task_slots(document: TaskDocument) -> tuple[tuple[str, ...], tuple[str, ...]]:
    gaps: list[str] = []
    cursor = 0
    for task in document.tasks:
        position = document.source.index(task.source, cursor)
        gaps.append(document.source[cursor:position])
        cursor = position + len(task.source)
    gaps.append(document.source[cursor:])
    return tuple(gaps), tuple(task.source for task in document.tasks)


def _raw_field(task: TaskRecord, field: TaskField) -> str:
    if field == TaskField.state:
        span = task.state_span
    else:
        span = task.field_spans[field.value]
    return task.source[span.character_start : span.character_end]


def _splice_task_field(task: TaskRecord, field: TaskField, raw_value: str) -> str:
    if field == TaskField.state:
        assert raw_value in {" ", "x"}, raw_value
        span = task.state_span
    else:
        span = task.field_spans[field.value]
    return (
        task.source[: span.character_start]
        + raw_value
        + task.source[span.character_end :]
    )


def replace_task_field(
    document: TaskDocument, task_id: TaskId, field: TaskField, raw_value: str
) -> TaskDocument | TaskWireFailure:
    if not document.wrapped:
        return _task_failure(
            document.source,
            "unsupported_task_wire",
            "opaque legacy tasks expose no field spans",
            task_id,
        )
    if field == TaskField.opaque:
        return _task_failure(
            document.source,
            "unsupported_field",
            "opaque is not directly editable",
            task_id,
        )
    task = next((task for task in document.tasks if task.id == task_id), None)
    if task is None:
        return _task_failure(
            document.source, "missing_id", f"no task {task_id}", task_id
        )
    updated_task = _splice_task_field(task, field, raw_value)
    updated_source = document.source.replace(task.source, updated_task, 1)
    return parse_task_document(updated_source)


def reorder_task_document(
    document: TaskDocument, task_ids: tuple[TaskId, ...]
) -> TaskDocument | TaskWireFailure:
    if not document.wrapped:
        return _task_failure(
            document.source,
            "unsupported_task_wire",
            "opaque legacy tasks cannot be reordered semantically",
        )
    by_id = {task.id: task for task in document.tasks}
    if len(task_ids) != len(by_id) or set(task_ids) != set(by_id):
        return _task_failure(
            document.source, "missing_id", "reorder must name every task exactly once"
        )
    gaps, original_items = _task_slots(document)
    rebuilt = gaps[0]
    for index, task_id in enumerate(task_ids):
        task = by_id[task_id]
        rebuilt += (
            _splice_task_field(task, TaskField.order, str(index)) + gaps[index + 1]
        )
    assert len(original_items) == len(task_ids), (original_items, task_ids)
    return parse_task_document(rebuilt)


def _three_way(base: str, live: str, proposed: str) -> str | None:
    if live == proposed or base == proposed:
        return live
    if base == live:
        return proposed
    return None


def merge_task_documents(
    base: TaskDocument, live: TaskDocument, proposed: TaskDocument
) -> TaskDocument | TaskFieldConflict:
    if not base.wrapped or not live.wrapped or not proposed.wrapped:
        value = _three_way(base.source, live.source, proposed.source)
        if value is not None:
            parsed = parse_task_document(value)
            if isinstance(parsed, TaskWireFailure):
                return TaskFieldConflict(
                    task_id=None, field=TaskField.opaque, retained_source=live.source
                )
            return parsed
        task_id = base.tasks[0].id if base.tasks else None
        return TaskFieldConflict(
            task_id=task_id, field=TaskField.opaque, retained_source=live.source
        )
    base_ids = tuple(task.id for task in base.tasks)
    live_ids = tuple(task.id for task in live.tasks)
    proposed_ids = tuple(task.id for task in proposed.tasks)
    if (
        len(set(base_ids)) != len(base_ids)
        or len(set(live_ids)) != len(live_ids)
        or len(set(proposed_ids)) != len(proposed_ids)
    ):
        return TaskFieldConflict(
            task_id=None, field=TaskField.order, retained_source=live.source
        )
    if set(base_ids) != set(live_ids) or set(base_ids) != set(proposed_ids):
        if base_ids == proposed_ids:
            return live
        if base_ids == live_ids:
            return proposed
        return TaskFieldConflict(
            task_id=None, field=TaskField.order, retained_source=live.source
        )
    base_by_id = {task.id: task for task in base.tasks}
    live_by_id = {task.id: task for task in live.tasks}
    proposed_by_id = {task.id: task for task in proposed.tasks}
    merged = live
    for task_id in live_ids:
        for field in (
            TaskField.state,
            TaskField.order,
            TaskField.prerequisites,
            TaskField.requirements,
            TaskField.evidence,
            TaskField.goals,
            TaskField.description,
            TaskField.acceptance,
        ):
            selected = _three_way(
                _raw_field(base_by_id[task_id], field),
                _raw_field(live_by_id[task_id], field),
                _raw_field(proposed_by_id[task_id], field),
            )
            if selected is None:
                return TaskFieldConflict(
                    task_id=task_id, field=field, retained_source=live.source
                )
            if selected != _raw_field(live_by_id[task_id], field):
                changed = replace_task_field(merged, task_id, field, selected)
                if isinstance(changed, TaskWireFailure):
                    return TaskFieldConflict(
                        task_id=task_id, field=field, retained_source=live.source
                    )
                merged = changed
    selected_order = _three_way(
        ",".join(base_ids), ",".join(live_ids), ",".join(proposed_ids)
    )
    if selected_order is None:
        return TaskFieldConflict(
            task_id=None, field=TaskField.order, retained_source=live.source
        )
    desired_ids: tuple[TaskId, ...] = (
        tuple(TaskId(part) for part in selected_order.split(","))
        if selected_order
        else ()
    )
    if desired_ids != tuple(task.id for task in merged.tasks):
        reordered = reorder_task_document(merged, desired_ids)
        if isinstance(reordered, TaskWireFailure):
            return TaskFieldConflict(
                task_id=None, field=TaskField.order, retained_source=live.source
            )
        merged = reordered
    gaps_base, _ = _task_slots(base)
    gaps_live, _ = _task_slots(live)
    gaps_proposed, _ = _task_slots(proposed)
    if len(gaps_base) == len(gaps_live) == len(gaps_proposed):
        selected_gaps: list[str] = []
        for values in zip(gaps_base, gaps_live, gaps_proposed, strict=True):
            selected = _three_way(*values)
            if selected is None:
                return TaskFieldConflict(
                    task_id=None,
                    field=TaskField.opaque,
                    retained_source=live.source,
                )
            selected_gaps.append(selected)
        _, merged_items = _task_slots(merged)
        rebuilt = selected_gaps[0]
        for index, item in enumerate(merged_items):
            rebuilt += item + selected_gaps[index + 1]
        parsed = parse_task_document(rebuilt)
        if isinstance(parsed, TaskWireFailure):
            return TaskFieldConflict(
                task_id=None,
                field=TaskField.opaque,
                retained_source=live.source,
            )
        return parsed
    return merged


def merge_stage_values(
    base: ManagedIssueLayout,
    live: ManagedIssueLayout,
    replacements: tuple[SectionReplacement, ...],
) -> BodyCandidate | MergeConflict:
    original_base = _section(base, SectionName.original_contract)
    original_live = _section(live, SectionName.original_contract)
    if original_base != original_live:
        return MergeConflict(
            section="original-contract",
            retained_body=live.body,
        )
    accepted: list[SectionReplacement] = []
    for replacement in replacements:
        section = SectionName(replacement.section)
        base_value = _section(base, section)
        live_value = _section(live, section)
        assert replacement.base_text == base_value, (
            replacement.base_text,
            base_value,
            replacement.section,
        )
        proposed = replacement.proposed_text
        if live_value == proposed or replacement.base_text == proposed:
            selected = live_value
        elif base_value == live_value:
            selected = proposed
        else:
            return MergeConflict(
                section=replacement.section,
                retained_body=live.body,
            )
        if selected != live_value:
            accepted.append(
                SectionReplacement(
                    section=replacement.section,
                    base_text=live_value,
                    proposed_text=selected,
                    base_sha256=sha256_text(live_value),
                    proposed_sha256=sha256_text(selected),
                )
            )
    projected = replace_sections(live, tuple(accepted))
    if isinstance(projected, NoBodyChange):
        return BodyCandidate(
            body=live.body,
            body_sha256=sha256_text(live.body),
            original_sha256=sha256_text(original_live),
            changed_sections=(),
            before_fences=tuple(
                fence.text for fence in contract_fence_inventory(live.body)
            ),
            after_fences=tuple(
                fence.text for fence in contract_fence_inventory(live.body)
            ),
        )
    return projected


def build_legacy_intake(
    layout: LegacyIssueLayout,
    requirements: str,
    plan: str,
    tasks: str,
) -> BodyCandidate:
    if not layout.body.endswith("\n"):
        raise StageFailure(
            ProjectionFailure(
                reason="legacy_missing_final_newline",
                exit_code=2,
                detail="managed intake cannot preserve a legacy original without final LF",
            )
        )
    for text in (requirements, plan, tasks):
        _validate_replacement_text(text)
    candidate = (
        "<!-- itree:original-contract:start -->\n"
        f"{layout.body}"
        "<!-- itree:original-contract:end -->\n"
        "<!-- itree:requirements:start -->\n"
        f"{requirements}"
        "<!-- itree:requirements:end -->\n"
        "<!-- itree:plan:start -->\n"
        f"{plan}"
        "<!-- itree:plan:end -->\n"
        "<!-- itree:tasks:start -->\n"
        f"{tasks}"
        "<!-- itree:tasks:end -->"
    )
    reparsed = _layout_or_raise(candidate)
    assert isinstance(reparsed, ManagedIssueLayout), reparsed
    assert _section(reparsed, SectionName.original_contract) == layout.body
    changed = tuple(
        section.value
        for section, current in zip(
            (SectionName.requirements, SectionName.plan, SectionName.tasks),
            (requirements, plan, tasks),
            strict=True,
        )
        if current
    )
    return BodyCandidate(
        body=candidate,
        body_sha256=sha256_text(candidate),
        original_sha256=sha256_text(layout.body),
        changed_sections=changed,
        before_fences=tuple(f.text for f in contract_fence_inventory(layout.body)),
        after_fences=tuple(f.text for f in contract_fence_inventory(candidate)),
    )


def _original_text(layout: ValidIssueLayout) -> str:
    if isinstance(layout, LegacyIssueLayout):
        return layout.original_contract
    return _section(layout, SectionName.original_contract)


def collect_artifact_drafts(directory: Path) -> tuple[ArtifactDraft, ...]:
    drafts: list[ArtifactDraft] = []
    single_files = (
        ("research.md", "research", "research"),
        ("data-model.md", "data-model", "data-model"),
        ("quickstart.md", "quickstart", "quickstart"),
    )
    for filename, kind, key in single_files:
        path = directory / filename
        if path.is_file():
            drafts.append(
                ArtifactDraft(
                    key=ArtifactKey(key),
                    kind=_artifact_kind(kind),
                    content=path.read_text(),
                )
            )
    contract_dir = directory / "contracts"
    if contract_dir.is_dir():
        for path in sorted(contract_dir.glob("*.md")):
            drafts.append(
                ArtifactDraft(
                    key=ArtifactKey(f"contracts/{path.stem}"),
                    kind=_artifact_kind("contract"),
                    content=path.read_text(),
                )
            )
    families = (
        ("clarifications", "clarification"),
        ("findings", "finding"),
        ("evidence", "evidence"),
        ("checkpoints", "checkpoint"),
        ("plan-history", "plan-history"),
        ("task-history", "task-history"),
    )
    for folder, kind in families:
        parent = directory / folder
        if not parent.is_dir():
            continue
        for path in sorted(parent.glob("*.md")):
            drafts.append(
                ArtifactDraft(
                    key=ArtifactKey(path.stem),
                    kind=_artifact_kind(kind),
                    content=path.read_text(),
                )
            )
    return tuple(drafts)


def _materialize_supporting_artifacts(
    directory: Path, plan_text: str, api: GithubApi, issue: IssueRef
) -> None:
    if "itree-stage-artifact-index" not in plan_text:
        return
    from .stage_writer import (
        _decode_stage_comment,
        _job_authority,
        _binding_for,
        _raw_history,
        validate_writer_seal,
    )

    matched = re.search(r"```itree-stage-artifact-index\n[^\n]+\n```\n", plan_text)
    if matched is None:
        raise StageFailure(
            ProjectionFailure(
                reason="ambiguous_boundary",
                exit_code=2,
                detail="artifact index is not one deterministic fence",
            )
        )
    _, raw_entries = _decode_stage_comment(matched.group(0))
    entries = TypeAdapter(tuple[ArtifactIndexEntry, ...]).validate_json(
        matched.group(0).splitlines()[1], strict=True
    )
    for entry in entries:
        native = api.get_comment(entry.comment_id)
        history = _raw_history(api, native)
        if history is None:
            raise StageFailure(
                ProjectionFailure(
                    reason="issue_unavailable",
                    exit_code=3,
                    detail=f"history unavailable for artifact {entry.key}",
                )
            )
        try:
            _, raw_payload = _decode_stage_comment(native.body)
            payload = ArtifactPayload.model_validate_json(
                canonical_payload_bytes_for_artifacts(raw_payload), strict=True
            )
        except (ValueError, TypeError, ValidationError) as error:
            raise StageFailure(
                ProjectionFailure(
                    reason="unexpected_file_change",
                    exit_code=3,
                    detail=f"edited or mismatched artifact {entry.key}",
                )
            ) from error
        expected_url = (
            f"https://github.com/{issue.repo_ref.slug}/issues/{issue.number}"
            f"#issuecomment-{entry.comment_id}"
        )
        intact = (
            native.node_id == entry.comment_node_id
            and native.html_url == expected_url == entry.url
            and history.created_at == history.updated_at
            and history.is_zero_edit()
            and payload.key == entry.key
            and payload.kind == entry.kind
            and payload.attempt_id == entry.attempt_id
            and sha256_text(payload.content) == entry.content_sha256
        )
        if not intact:
            raise StageFailure(
                ProjectionFailure(
                    reason="unexpected_file_change",
                    exit_code=3,
                    detail=f"edited or mismatched artifact {entry.key}",
                )
            )
        if isinstance(
            validate_writer_seal(
                payload.writer_seal,
                _job_authority(api, payload.writer_seal, _binding_for(payload)),
            ),
            WriterSealInvalidContext,
        ):
            raise StageFailure(
                ProjectionFailure(
                    reason="issue_unavailable",
                    exit_code=3,
                    detail=f"invalid writer seal for artifact {entry.key}",
                )
            )
        relative = {
            "research": Path("research.md"),
            "data-model": Path("data-model.md"),
            "quickstart": Path("quickstart.md"),
            "clarification": Path("clarifications") / f"{entry.key}.md",
            "finding": Path("findings") / f"{entry.key}.md",
            "evidence": Path("evidence") / f"{entry.key}.md",
            "checkpoint": Path("checkpoints") / f"{entry.key}.md",
            "plan-history": Path("plan-history") / f"{entry.key}.md",
            "task-history": Path("task-history") / f"{entry.key}.md",
        }.get(entry.kind, Path(f"{entry.key}.md"))
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload.content, encoding="utf-8", newline="")


def canonical_payload_bytes_for_artifacts(payload: JsonValue) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _write_materialization(
    issue: IssueRef,
    api: GithubApi,
    live: GithubIssue,
    layout: ValidIssueLayout,
    attempt: AttemptId,
) -> StageCommandResult:
    directory = Path(tempfile.mkdtemp(prefix="itree-stage-", dir="/tmp/opencode"))
    directory.chmod(0o700)
    original = _original_text(layout)
    original_path = directory / "original-contract.md"
    original_path.write_text(original, encoding="utf-8", newline="")
    original_path.chmod(0o400)
    sections = (
        ("spec.md", SectionName.requirements),
        ("plan.md", SectionName.plan),
        ("tasks.md", SectionName.tasks),
    )
    for filename, name in sections:
        text = "" if isinstance(layout, LegacyIssueLayout) else _section(layout, name)
        directory.joinpath(filename).write_text(text, encoding="utf-8", newline="")
    plan_text = (
        ""
        if isinstance(layout, LegacyIssueLayout)
        else _section(layout, SectionName.plan)
    )
    _materialize_supporting_artifacts(directory, plan_text, api, issue)
    metadata = ScratchMetadata(
        version=1,
        issue_ref=issue,
        issue_node_id=require_issue_node(live),
        attempt_id=attempt,
        layout=layout.kind,
        base_body=layout.body,
        body_sha256=sha256_text(layout.body),
        original_sha256=sha256_text(original),
    )
    directory.joinpath(".itree-stage.json").write_text(
        metadata.model_dump_json(), encoding="utf-8"
    )
    return _result(
        (
            f"issue_ref={issue.slug}",
            f"issue_node_id={require_issue_node(live)}",
            f"attempt_id={attempt}",
            f"SPECIFY_FEATURE_DIRECTORY={directory}",
            "SPECIFY_FEATURE_NO_PERSIST=1",
            f"original_contract_sha256={metadata.original_sha256}",
            f"body_sha256={metadata.body_sha256}",
            f"layout={layout.kind}",
        )
    )


def stage_materialize(issue: IssueRef, attempt: AttemptId) -> StageCommandResult:
    try:
        api = GithubApi.from_issue_ref(issue)
        live, layout = _read_layout(api, issue)
        return _write_materialization(issue, api, live, layout, attempt)
    except StageFailure as error:
        return _defect(error.failure)


def _read_metadata(
    issue: IssueRef, attempt: AttemptId, directory: Path
) -> ScratchMetadata:
    try:
        metadata = ScratchMetadata.model_validate_json(
            directory.joinpath(".itree-stage.json").read_text(encoding="utf-8")
        )
    except (OSError, ValidationError) as error:
        raise StageFailure(
            ProjectionFailure(reason="missing_file", exit_code=2, detail=str(error))
        ) from error
    if metadata.issue_ref != issue or metadata.attempt_id != attempt:
        raise StageFailure(
            ProjectionFailure(
                reason="wrong_attempt",
                exit_code=2,
                detail=f"scratch belongs to {metadata.issue_ref.slug}:{metadata.attempt_id}",
            )
        )
    return metadata


def _read_scratch(directory: Path, filename: str) -> str:
    try:
        return directory.joinpath(filename).read_text(encoding="utf-8")
    except OSError as error:
        raise StageFailure(
            ProjectionFailure(reason="missing_file", exit_code=2, detail=str(error))
        ) from error


def _cleanup(directory: Path) -> str:
    shutil.rmtree(directory)
    return "removed"


def cleanup_stage_scratch(directory: Path) -> str:
    return _cleanup(directory)


def stage_write_back(
    issue: IssueRef,
    attempt: AttemptId,
    feature_directory: Path,
    managed_intake: bool,
) -> StageCommandResult:
    try:
        metadata = _read_metadata(issue, attempt, feature_directory)
        original = _read_scratch(feature_directory, "original-contract.md")
        base_layout = _layout_or_raise(metadata.base_body)
        if (
            original != _original_text(base_layout)
            or sha256_text(original) != metadata.original_sha256
        ):
            raise StageFailure(
                ProjectionFailure(
                    reason="original_contract_target",
                    exit_code=2,
                    detail="original-contract.md differs from its materialized bytes",
                )
            )
        requirements = _read_scratch(feature_directory, "spec.md")
        plan = _read_scratch(feature_directory, "plan.md")
        tasks = _read_scratch(feature_directory, "tasks.md")
        if isinstance(base_layout, LegacyIssueLayout):
            changed = any((requirements, plan, tasks))
            if not changed:
                candidate = BodyCandidate(
                    body=base_layout.body,
                    body_sha256=metadata.body_sha256,
                    original_sha256=metadata.original_sha256,
                    changed_sections=(),
                    before_fences=tuple(
                        fence.text
                        for fence in contract_fence_inventory(base_layout.body)
                    ),
                    after_fences=tuple(
                        fence.text
                        for fence in contract_fence_inventory(base_layout.body)
                    ),
                )
                from .stage_writer import apply_stage_candidate

                return apply_stage_candidate(
                    issue, attempt, feature_directory, metadata, candidate
                )
            if not managed_intake:
                raise StageFailure(
                    ProjectionFailure(
                        reason="legacy_intake_required",
                        exit_code=2,
                        detail="changed legacy input requires --managed-intake",
                    )
                )
            candidate = build_legacy_intake(base_layout, requirements, plan, tasks)
        else:
            replacement_names: tuple[Literal["requirements", "plan", "tasks"], ...] = (
                "requirements",
                "plan",
                "tasks",
            )
            replacements: list[SectionReplacement] = []
            for name, proposed in zip(
                replacement_names, (requirements, plan, tasks), strict=True
            ):
                base = _section(base_layout, SectionName(name))
                replacements.append(
                    SectionReplacement(
                        section=name,
                        base_text=base,
                        proposed_text=proposed,
                        base_sha256=sha256_text(base),
                        proposed_sha256=sha256_text(proposed),
                    )
                )
            projected = replace_sections(base_layout, tuple(replacements))
            if isinstance(projected, NoBodyChange):
                candidate = BodyCandidate(
                    body=base_layout.body,
                    body_sha256=metadata.body_sha256,
                    original_sha256=metadata.original_sha256,
                    changed_sections=(),
                    before_fences=tuple(
                        fence.text
                        for fence in contract_fence_inventory(base_layout.body)
                    ),
                    after_fences=tuple(
                        fence.text
                        for fence in contract_fence_inventory(base_layout.body)
                    ),
                )
            else:
                candidate = projected
        from .stage_writer import apply_stage_candidate

        return apply_stage_candidate(
            issue, attempt, feature_directory, metadata, candidate
        )
    except StageFailure as error:
        return _defect(error.failure)


def stage_resume(issue: IssueRef, attempt: AttemptId) -> StageCommandResult:
    from .stage_writer import resume_stage

    return resume_stage(issue, attempt)


def stage_drain(
    issue: IssueRef, expected_node: IssueNodeId | None
) -> StageCommandResult:
    from .stage_writer import drain_stage

    return drain_stage(issue, expected_node)


UploadProposal = (
    SectionEditProposal
    | LegacyIntakeProposal
    | TaskFieldProposal
    | ArtifactProposal
    | SessionAdmissionRequest
    | SessionReleaseRequest
)


def _model_payload(model: StageModel) -> JsonValue:
    return JSON_VALUE_ADAPTER.validate_python(model.model_dump(mode="json"))


def _canonical_upload_digest(payload: JsonValue) -> Sha256:
    return Sha256(
        hashlib.sha256(
            json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()
    )


def section_upload_contents(
    proposal: SectionEditProposal,
) -> tuple[UploadContent, ...]:
    contents = [UploadContent(role="base_body", ordinal=0, payload=proposal.base_body)]
    for replacement in proposal.changes:
        contents.extend(
            (
                UploadContent(
                    role=f"base:{replacement.section}",
                    ordinal=0,
                    payload=replacement.base_text,
                ),
                UploadContent(
                    role=f"proposed:{replacement.section}",
                    ordinal=0,
                    payload=replacement.proposed_text,
                ),
            )
        )
    return tuple(contents)


def task_upload_contents(
    proposal: TaskFieldProposal,
) -> tuple[UploadContent, ...]:
    return (
        UploadContent(role="base_body", ordinal=0, payload=proposal.base_body),
        UploadContent(
            role="full_task_base_sha256",
            ordinal=0,
            payload=proposal.full_task_base_sha256,
        ),
        UploadContent(role="base_value", ordinal=0, payload=proposal.base_value),
        UploadContent(
            role="proposed_value", ordinal=0, payload=proposal.proposed_value
        ),
    )


def legacy_upload_contents(
    proposal: LegacyIntakeProposal,
) -> tuple[UploadContent, ...]:
    return (
        UploadContent(role="base_body", ordinal=0, payload=proposal.legacy_base),
        UploadContent(role="requirements", ordinal=0, payload=proposal.requirements),
        UploadContent(role="plan", ordinal=0, payload=proposal.plan),
        UploadContent(role="tasks", ordinal=0, payload=proposal.tasks),
    )


def artifact_upload_contents(
    proposal: ArtifactProposal,
) -> tuple[UploadContent, ...]:
    return (
        UploadContent(role="base_body", ordinal=0, payload=proposal.base_body),
        *(
            UploadContent(
                role=f"artifact:{artifact.kind}:{artifact.key}",
                ordinal=0,
                payload=artifact.content,
            )
            for artifact in proposal.artifacts
        ),
    )


def build_upload_manifest(
    proposal: UploadProposal, contents: tuple[UploadContent, ...]
) -> UploadManifest:
    targets: tuple[str, ...]
    if isinstance(proposal, SectionEditProposal):
        targets = tuple(replacement.section for replacement in proposal.changes)
    elif isinstance(proposal, TaskFieldProposal):
        targets = (f"task:{proposal.task_id}:{proposal.field.value}",)
    elif isinstance(proposal, LegacyIntakeProposal):
        targets = ("requirements", "plan", "tasks")
    elif isinstance(proposal, ArtifactProposal):
        targets = tuple(
            f"artifact:{artifact.kind}:{artifact.key}"
            for artifact in proposal.artifacts
        )
    else:
        targets = (proposal.request_kind,)
    parts = tuple(
        PartDescriptor(
            role=content.role,
            ordinal=content.ordinal,
            payload_sha256=sha256_text(content.payload),
        )
        for content in contents
    )
    base_roles = tuple(
        content.role
        for content in contents
        if content.role in {"base_body", "base_value", "full_task_base_sha256"}
        or content.role.startswith("base:")
    )
    proposed_roles = tuple(
        content.role for content in contents if content.role not in base_roles
    )
    return UploadManifest(
        version=1,
        issue_node_id=proposal.issue_node_id,
        issue_ref=proposal.issue_ref,
        request_id=proposal.request_id,
        attempt_id=proposal.attempt_id,
        session_id=proposal.session_id,
        base_body_sha256=(
            proposal.entry_body_sha256
            if isinstance(proposal, SessionAdmissionRequest)
            else None
            if isinstance(proposal, SessionReleaseRequest)
            else proposal.base_body_sha256
        ),
        request_kind=proposal.request_kind,
        targets=targets,
        base_roles=base_roles,
        proposed_roles=proposed_roles,
        expected_part_count=len(parts),
        parts=parts,
        finalized_proposal_sha256=_canonical_upload_digest(_model_payload(proposal)),
    )


def _upload_part(manifest: UploadManifest, content: UploadContent) -> UploadPart:
    return UploadPart(
        version=1,
        issue_node_id=manifest.issue_node_id,
        issue_ref=manifest.issue_ref,
        request_id=manifest.request_id,
        attempt_id=manifest.attempt_id,
        manifest_request_id=manifest.request_id,
        manifest_payload_sha256=_canonical_upload_digest(_model_payload(manifest)),
        role=content.role,
        ordinal=content.ordinal,
        payload=content.payload,
        payload_sha256=sha256_text(content.payload),
    )


def make_upload_parts(
    manifest: UploadManifest, contents: tuple[UploadContent, ...]
) -> tuple[UploadPart, ...]:
    return tuple(_upload_part(manifest, content) for content in contents)


def _upload_rejection(reason: UploadRejectionReason) -> UploadRejected:
    return UploadRejected(reason=reason)


STAGE_SECTION_FIELDS: dict[str, Literal["requirements", "plan", "tasks"]] = {
    "requirements": "requirements",
    "plan": "plan",
    "tasks": "tasks",
}


def _required_body_sha(manifest: UploadManifest) -> Sha256:
    digest = manifest.base_body_sha256
    assert digest is not None, manifest.request_kind
    return digest


def _artifact_kind(value: str) -> ArtifactKind:
    match value:
        case "research" | "data-model" | "contract" | "quickstart":
            return value
        case "clarification" | "finding" | "evidence" | "checkpoint":
            return value
        case "plan-history" | "task-history":
            return value
    raise AssertionError(value)


def _complete_proposal(
    manifest: UploadManifest, contents: dict[str, str]
) -> UploadProposal:
    assert manifest.base_body_sha256 is not None or manifest.request_kind in {
        "session_admit",
        "session_release",
    }
    if manifest.request_kind == "sections":
        base = _layout_or_raise(contents["base_body"])
        assert isinstance(base, ManagedIssueLayout)
        replacements: list[SectionReplacement] = []
        for section in manifest.targets:
            assert section in {"requirements", "plan", "tasks"}, section
            base_text = _section(base, SectionName(section))
            proposed = contents[f"proposed:{section}"]
            replacements.append(
                SectionReplacement(
                    section=STAGE_SECTION_FIELDS[section],
                    base_text=base_text,
                    proposed_text=proposed,
                    base_sha256=sha256_text(base_text),
                    proposed_sha256=sha256_text(proposed),
                )
            )
        return SectionEditProposal(
            version=1,
            request_kind="sections",
            issue_node_id=manifest.issue_node_id,
            issue_ref=manifest.issue_ref,
            request_id=manifest.request_id,
            attempt_id=manifest.attempt_id,
            session_id=manifest.session_id,
            base_body_sha256=_required_body_sha(manifest),
            base_body=contents["base_body"],
            changes=tuple(replacements),
        )
    if manifest.request_kind == "task_field":
        task_id, field = manifest.targets[0].split(":", 2)[1:]
        return TaskFieldProposal(
            version=1,
            request_kind="task_field",
            issue_node_id=manifest.issue_node_id,
            issue_ref=manifest.issue_ref,
            request_id=manifest.request_id,
            attempt_id=manifest.attempt_id,
            session_id=manifest.session_id,
            base_body_sha256=_required_body_sha(manifest),
            base_body=contents["base_body"],
            task_id=TaskId(task_id),
            field=TaskField(field),
            full_task_base_sha256=Sha256(contents["full_task_base_sha256"]),
            base_value=contents["base_value"],
            proposed_value=contents["proposed_value"],
        )
    if manifest.request_kind == "legacy_intake":
        return LegacyIntakeProposal(
            version=1,
            request_kind="legacy_intake",
            issue_node_id=manifest.issue_node_id,
            issue_ref=manifest.issue_ref,
            request_id=manifest.request_id,
            attempt_id=manifest.attempt_id,
            session_id=manifest.session_id,
            base_body_sha256=_required_body_sha(manifest),
            legacy_base=contents["base_body"],
            requirements=contents["requirements"],
            plan=contents["plan"],
            tasks=contents["tasks"],
        )
    if manifest.request_kind == "artifacts":
        drafts: list[ArtifactDraft] = []
        for target in manifest.targets:
            _, kind, key = target.split(":", 2)
            drafts.append(
                ArtifactDraft(
                    key=ArtifactKey(key),
                    kind=_artifact_kind(kind),
                    content=contents[target],
                )
            )
        return ArtifactProposal(
            version=1,
            request_kind="artifacts",
            issue_node_id=manifest.issue_node_id,
            issue_ref=manifest.issue_ref,
            request_id=manifest.request_id,
            attempt_id=manifest.attempt_id,
            session_id=manifest.session_id,
            base_body_sha256=_required_body_sha(manifest),
            base_body=contents["base_body"],
            artifacts=tuple(drafts),
        )
    payload = json.loads(contents["session"])
    if manifest.request_kind == "session_admit":
        return SessionAdmissionRequest.model_validate_json(
            contents["session"], strict=True
        )
    assert manifest.request_kind == "session_release", manifest.request_kind
    return SessionReleaseRequest.model_validate_json(contents["session"], strict=True)


def finalize_upload(
    manifest: UploadManifest, parts: tuple[UploadPart, ...]
) -> UploadResolution:
    descriptors = {(part.role, part.ordinal) for part in manifest.parts}
    seen: set[tuple[str, int]] = set()
    expected_order = [
        (descriptor.role, descriptor.ordinal) for descriptor in manifest.parts
    ]
    actual_order = [(part.role, part.ordinal) for part in parts]
    if len(set(actual_order)) != len(actual_order):
        return _upload_rejection("duplicate_part")
    if any(identity not in descriptors for identity in actual_order):
        return _upload_rejection("unexpected_part")
    if actual_order != [
        identity for identity in expected_order if identity in actual_order
    ]:
        return _upload_rejection("wrong_order")
    by_identity: dict[tuple[str, int], UploadPart] = {}
    for part in parts:
        identity = (part.role, part.ordinal)
        if identity in seen:
            return _upload_rejection("duplicate_part")
        seen.add(identity)
        if identity not in descriptors:
            return _upload_rejection("unexpected_part")
        if part.manifest_request_id != manifest.request_id:
            return _upload_rejection("manifest_mismatch")
        if part.manifest_payload_sha256 != _canonical_upload_digest(
            _model_payload(manifest)
        ):
            return _upload_rejection("manifest_mismatch")
        expected_digest = next(
            descriptor.payload_sha256
            for descriptor in manifest.parts
            if (descriptor.role, descriptor.ordinal) == identity
        )
        if (
            part.payload_sha256 != expected_digest
            or sha256_text(part.payload) != expected_digest
        ):
            return _upload_rejection("digest_mismatch")
        by_identity[identity] = part
    missing = tuple(
        MissingPart(
            role=descriptor.role,
            ordinal=descriptor.ordinal,
            expected_payload_sha256=descriptor.payload_sha256,
        )
        for descriptor in manifest.parts
        if (descriptor.role, descriptor.ordinal) not in by_identity
    )
    if missing:
        return PendingUpload(
            validated_manifest=manifest,
            present_sealed_parts=parts,
            missing_parts=missing,
        )
    contents = {
        descriptor.role: by_identity[(descriptor.role, descriptor.ordinal)].payload
        for descriptor in manifest.parts
    }
    try:
        proposal = _complete_proposal(manifest, contents)
    except AssertionError, KeyError, ValueError, ValidationError, StageFailure:
        return _upload_rejection("manifest_mismatch")
    digest = _canonical_upload_digest(_model_payload(proposal))
    if digest != manifest.finalized_proposal_sha256:
        return _upload_rejection("digest_mismatch")
    return FinalizedProposal(
        manifest=manifest,
        parts=parts,
        logical_payload_sha256=digest,
        proposal=_model_payload(proposal),
    )


LivePartReconstructor = Callable[[UploadManifest], tuple[UploadContent, ...]]


def regenerate_upload_part(
    manifest: UploadManifest,
    present: tuple[UploadPart, ...],
    reconstruct: LivePartReconstructor,
) -> UploadResolution | RestartedAttempt:
    pending = finalize_upload(manifest, present)
    if not isinstance(pending, PendingUpload):
        return pending
    reconstructed = reconstruct(manifest)
    if not reconstructed:
        return RestartedAttempt(
            prior=pending,
            required_new_attempt=AttemptId(f"{manifest.attempt_id}-new"),
        )
    expected = {missing.role for missing in pending.missing_parts}
    if {part.role for part in reconstructed} != expected:
        return RestartedAttempt(
            prior=pending,
            required_new_attempt=AttemptId(f"{manifest.attempt_id}-new"),
        )
    regenerated = make_upload_parts(manifest, reconstructed)
    resumed = finalize_upload(manifest, present + regenerated)
    if isinstance(resumed, UploadRejected):
        return RestartedAttempt(
            prior=pending,
            required_new_attempt=AttemptId(f"{manifest.attempt_id}-new"),
        )
    return resumed
