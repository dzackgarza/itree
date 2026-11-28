from __future__ import annotations

from pathlib import Path

from .models import AttemptId, IssueNodeId, IssueRef, StageModel


class StageCommandResult(StageModel):
    exit_code: int
    lines: tuple[str, ...]


def _pending(issue: IssueRef, attempt: AttemptId) -> StageCommandResult:
    return StageCommandResult(
        exit_code=5,
        lines=(
            f"issue_ref={issue.slug}",
            f"attempt_id={attempt}",
            "stage_complete=false",
            f"recovery=itree stage resume {issue.slug} --attempt {attempt}",
        ),
    )


def stage_materialize(issue: IssueRef, attempt: AttemptId) -> StageCommandResult:
    return _pending(issue, attempt)


def stage_write_back(
    issue: IssueRef,
    attempt: AttemptId,
    feature_directory: Path,
    managed_intake: bool,
) -> StageCommandResult:
    assert feature_directory.is_absolute(), feature_directory
    assert isinstance(managed_intake, bool), managed_intake
    return _pending(issue, attempt)


def stage_resume(issue: IssueRef, attempt: AttemptId) -> StageCommandResult:
    return _pending(issue, attempt)


def stage_drain(
    issue: IssueRef, expected_node: IssueNodeId | None
) -> StageCommandResult:
    assert expected_node is None or len(expected_node) > 0, expected_node
    return StageCommandResult(
        exit_code=5,
        lines=(f"issue_ref={issue.slug}", "stage_complete=false"),
    )
