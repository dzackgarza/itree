from __future__ import annotations

import csv
import hashlib
import importlib.metadata
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from abc import abstractmethod
from datetime import UTC, datetime
from enum import StrEnum
from importlib.metadata import Distribution, PackageNotFoundError
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self, assert_never

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from .models import IssueRef

Sha256Text = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
MonotonicSeconds = Annotated[float, Field(ge=0)]
DeadlineMilliseconds = Annotated[int, Field(gt=0)]
CHUNK_BYTES = 8192


class InvocationId(RootModel[str]):
    model_config = ConfigDict(frozen=True)

    root: str = Field(pattern=r"^[0-9a-f]{32}$")

    @classmethod
    def generate(cls) -> Self:
        import secrets

        return cls(secrets.token_hex(16))


class Sha256(RootModel[str]):
    model_config = ConfigDict(frozen=True)

    root: Sha256Text

    @classmethod
    def of(cls, content: bytes) -> Self:
        return cls(hashlib.sha256(content).hexdigest())


class ItreeDistribution(BaseModel):
    model_config = ConfigDict(frozen=True)

    distribution_name: Literal["itree"]
    version: str = Field(min_length=1)
    entrypoint_group: Literal["console_scripts"]
    entrypoint_name: Literal["itree"]
    entrypoint_value: Literal["itree.cli:app"]
    record_launcher_relpath: Path
    launcher_abspath: Path
    launcher_sha256: Sha256

    @model_validator(mode="after")
    def validate_launcher(self) -> Self:
        assert self.launcher_abspath.is_absolute(), {
            "launcher_abspath": self.launcher_abspath,
            "required": "absolute RECORD launcher path",
        }
        assert not self.record_launcher_relpath.is_absolute(), {
            "record_launcher_relpath": self.record_launcher_relpath,
            "required": "relative RECORD path",
        }
        return self


class DistributionFailure(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal[
        "missing_distribution",
        "missing_entrypoint",
        "missing_launcher_record",
        "ambiguous_launcher",
        "invalid_launcher",
    ]
    diagnostic: str = Field(min_length=1)


DistributionResolution = ItreeDistribution | DistributionFailure


class ProcessBase(BaseModel):
    model_config = ConfigDict(frozen=True)

    argv: tuple[str, ...] = Field(min_length=1)
    started_utc: str = Field(min_length=1)
    started_monotonic: float
    wall_seconds: MonotonicSeconds
    cpu_seconds: MonotonicSeconds


class Spawned(ProcessBase):
    kind: Literal["spawned"] = "spawned"
    process_id: int = Field(gt=0)


class Exited(ProcessBase):
    kind: Literal["exited"] = "exited"
    returncode: int
    stdout: bytes
    stderr: bytes


class CleanupOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    terminate_requested: bool
    terminate_wait_seconds: MonotonicSeconds
    kill_required: bool
    kill_wait_seconds: MonotonicSeconds | None
    reaped: Literal[True]
    final_returncode: int


class TimedOut(ProcessBase):
    kind: Literal["timed_out"] = "timed_out"
    deadline_ms: DeadlineMilliseconds
    partial_stdout: bytes
    partial_stderr: bytes
    cleanup: CleanupOutcome


class Interrupted(ProcessBase):
    kind: Literal["interrupted"] = "interrupted"
    signal_number: int
    partial_stdout: bytes
    partial_stderr: bytes
    cleanup: CleanupOutcome


class SpawnFailed(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["spawn_failed"] = "spawn_failed"
    argv: tuple[str, ...] = Field(min_length=1)
    error_kind: str = Field(min_length=1)
    diagnostic: str = Field(min_length=1)
    elapsed_seconds: MonotonicSeconds


ProcessOutcome = Spawned | Exited | TimedOut | Interrupted | SpawnFailed
TerminalProcessOutcome = Exited | TimedOut | Interrupted | SpawnFailed
StoredProcessOutcome = Annotated[
    Exited | TimedOut | Interrupted | SpawnFailed, Field(discriminator="kind")
]


class PackageSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    issue_ref: IssueRef
    distribution: ItreeDistribution
    markdown: bytes = Field(min_length=1)
    stdout: bytes
    stderr: bytes
    outcome: StoredProcessOutcome
    package_sha256: Sha256


class PackageChunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    invocation_id: InvocationId
    index: int = Field(ge=0)
    byte_start: int = Field(ge=0)
    byte_end: int = Field(gt=0)
    text: str = Field(min_length=1)
    digest: Sha256

    @model_validator(mode="after")
    def validate_chunk(self) -> Self:
        encoded = self.text.encode("utf-8")
        assert len(encoded) <= CHUNK_BYTES, {"index": self.index, "bytes": len(encoded)}
        assert self.byte_end - self.byte_start == len(encoded), {
            "index": self.index,
            "range": (self.byte_start, self.byte_end),
            "bytes": len(encoded),
        }
        return self


class PackageKind(StrEnum):
    full = "full"
    partial = "partial"


class PendingPackage(BaseModel):
    model_config = ConfigDict(frozen=True)

    invocation_id: InvocationId
    source: PackageSource
    chunks: tuple[PackageChunk, ...] = Field(min_length=1)


class PendingFull(PendingPackage):
    kind: Literal[PackageKind.full] = PackageKind.full


class PendingPartial(PendingPackage):
    kind: Literal[PackageKind.partial] = PackageKind.partial


StoredPreparation = Annotated[PendingFull | PendingPartial, Field(discriminator="kind")]


class StreamFailure(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal[
        "unknown_invocation",
        "replayed_invocation",
        "premature_finish",
        "partial_package",
        "modified_chunk",
        "missing_chunk",
        "reordered_chunk",
        "post_final_chunk",
        "integrity_failure",
        "cleanup_failure",
    ]
    invocation_id: InvocationId | None
    index: int | None = Field(default=None, ge=0)
    diagnostic: str | None


class EmittedPackage(BaseModel):
    model_config = ConfigDict(frozen=True)

    pending: PendingFull
    next_index: int


class EmissionReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["emission-receipt"] = "emission-receipt"
    evidence_level: Literal["emission-only"] = "emission-only"
    invocation_id: InvocationId
    issue_ref: IssueRef
    byte_count: int = Field(gt=0)
    chunk_count: int = Field(gt=0)
    package_sha256: Sha256
    elapsed_seconds: MonotonicSeconds


class UsageFailure(BaseModel):
    kind: Literal["usage_failure"] = "usage_failure"
    diagnostic: str = Field(min_length=1)


class ReadFailure(BaseModel):
    kind: Literal["read_failure"] = "read_failure"
    outcome: StoredProcessOutcome


class BoundaryFailure(BaseModel):
    kind: Literal["boundary_failure"] = "boundary_failure"
    diagnostic: str = Field(min_length=1)


Preparation = (
    PendingFull
    | PendingPartial
    | UsageFailure
    | ReadFailure
    | BoundaryFailure
    | TimedOut
    | Interrupted
    | SpawnFailed
)


class CommandResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    exit_code: int
    stdout: bytes
    stderr: bytes


class Cursor(BaseModel):
    model_config = ConfigDict(frozen=True)

    next_index: int = Field(ge=0)


class WatchdogStatus(StrEnum):
    exited = "exited"
    forced_termination = "forced-termination"
    survivor_error = "survivor-error"


class WatchdogOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: WatchdogStatus
    native_deadline_ms: DeadlineMilliseconds
    external_elapsed_seconds: MonotonicSeconds
    signals: tuple[int, ...]
    terminate_wait_seconds: MonotonicSeconds | None
    kill_wait_seconds: MonotonicSeconds | None
    reaped: bool
    leader_returncode: int | None
    remaining_children: tuple[int, ...]


class EvidenceLevel(StrEnum):
    emission_only = "emission-only"
    model_context_verified = "model-context-verified"
    unaccepted = "unaccepted"


class ProviderInputPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str = Field(min_length=1)
    body: bytes = Field(min_length=1)


class ModelInputMatch(BaseModel):
    kind: Literal["match"] = "match"
    session_id: str
    chunk_count: int = Field(gt=0)


class ProviderComparisonFailure(BaseModel):
    kind: Literal["missing", "reordered", "truncated", "spilled", "compacted"]
    session_id: str
    chunk_index: int = Field(ge=0)


ProviderComparison = ModelInputMatch | ProviderComparisonFailure


class TrashPort(Protocol):
    @abstractmethod
    def trash(self, path: Path) -> None: ...


class ChunkStore(Protocol):
    @abstractmethod
    def next_chunk(
        self, invocation_id: InvocationId
    ) -> PackageChunk | StreamFailure: ...

    @abstractmethod
    def complete(
        self, invocation_id: InvocationId
    ) -> EmittedPackage | StreamFailure: ...


def resolve_distribution() -> DistributionResolution:
    try:
        distribution = Distribution.from_name("itree")
    except PackageNotFoundError as error:
        return DistributionFailure(kind="missing_distribution", diagnostic=str(error))
    entrypoints = [
        entry
        for entry in distribution.entry_points
        if entry.group == "console_scripts"
        and entry.name == "itree"
        and entry.value == "itree.cli:app"
    ]
    if len(entrypoints) != 1:
        return DistributionFailure(
            kind="missing_entrypoint",
            diagnostic=f"matching console entrypoints={len(entrypoints)}",
        )
    record = distribution.read_text("RECORD")
    if record is None:
        return DistributionFailure(
            kind="missing_launcher_record", diagnostic="RECORD is absent"
        )
    candidates = [
        row[0]
        for row in csv.reader(record.splitlines())
        if row and Path(row[0]).name == "itree"
    ]
    if len(candidates) != 1:
        return DistributionFailure(
            kind="ambiguous_launcher",
            diagnostic=f"RECORD launcher candidates={candidates}",
        )
    relative = Path(candidates[0])
    launcher = Path(str(distribution.locate_file(relative))).resolve()
    if not launcher.is_file():
        return DistributionFailure(
            kind="invalid_launcher", diagnostic=f"launcher={launcher} is not a file"
        )
    return ItreeDistribution(
        distribution_name="itree",
        version=distribution.version,
        entrypoint_group="console_scripts",
        entrypoint_name="itree",
        entrypoint_value="itree.cli:app",
        record_launcher_relpath=relative,
        launcher_abspath=launcher,
        launcher_sha256=Sha256.of(launcher.read_bytes()),
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _discard_process_group(process: subprocess.Popen[bytes]) -> CleanupOutcome:
    terminate_started = time.perf_counter()
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.communicate(timeout=1)
        terminate_seconds = time.perf_counter() - terminate_started
        cleanup = CleanupOutcome(
            terminate_requested=True,
            terminate_wait_seconds=terminate_seconds,
            kill_required=False,
            kill_wait_seconds=None,
            reaped=True,
            final_returncode=int(process.returncode),
        )
        return cleanup
    except subprocess.TimeoutExpired:
        terminate_seconds = time.perf_counter() - terminate_started
        kill_started = time.perf_counter()
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        return CleanupOutcome(
            terminate_requested=True,
            terminate_wait_seconds=terminate_seconds,
            kill_required=True,
            kill_wait_seconds=time.perf_counter() - kill_started,
            reaped=True,
            final_returncode=int(process.returncode),
        )


def _children_cpu_seconds() -> float:
    usage = os.times()
    return usage.children_user + usage.children_system


def supervise(
    argv: tuple[str, ...], deadline_ms: DeadlineMilliseconds
) -> TerminalProcessOutcome:
    started_wall = time.perf_counter()
    started_cpu = _children_cpu_seconds()
    started_utc = _utc_now()
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        return SpawnFailed(
            argv=argv,
            error_kind=type(error).__name__,
            diagnostic=str(error),
            elapsed_seconds=time.perf_counter() - started_wall,
        )
    try:
        stdout, stderr = process.communicate(timeout=deadline_ms / 1000)
        return Exited(
            argv=argv,
            started_utc=started_utc,
            started_monotonic=started_wall,
            wall_seconds=time.perf_counter() - started_wall,
            cpu_seconds=_children_cpu_seconds() - started_cpu,
            returncode=int(process.returncode),
            stdout=stdout,
            stderr=stderr,
        )
    except subprocess.TimeoutExpired:
        cleanup = _discard_process_group(process)
        stdout, stderr = process.communicate()
        return TimedOut(
            argv=argv,
            started_utc=started_utc,
            started_monotonic=started_wall,
            wall_seconds=time.perf_counter() - started_wall,
            cpu_seconds=_children_cpu_seconds() - started_cpu,
            deadline_ms=deadline_ms,
            partial_stdout=stdout,
            partial_stderr=stderr,
            cleanup=cleanup,
        )
    except KeyboardInterrupt:
        cleanup = _discard_process_group(process)
        stdout, stderr = process.communicate()
        return Interrupted(
            argv=argv,
            started_utc=started_utc,
            started_monotonic=started_wall,
            wall_seconds=time.perf_counter() - started_wall,
            cpu_seconds=_children_cpu_seconds() - started_cpu,
            signal_number=int(signal.SIGINT),
            partial_stdout=stdout,
            partial_stderr=stderr,
            cleanup=cleanup,
        )


def split_package(
    invocation_id: InvocationId, content: bytes
) -> tuple[PackageChunk, ...]:
    text = content.decode("utf-8")
    chunks: list[PackageChunk] = []
    byte_start = 0
    character_start = 0
    while character_start < len(text):
        remaining = text[character_start:].encode("utf-8")
        chunk_text = remaining[:CHUNK_BYTES].decode("utf-8", errors="ignore")
        assert chunk_text, {
            "character_start": character_start,
            "required": "UTF-8 prefix",
        }
        encoded = chunk_text.encode("utf-8")
        byte_end = byte_start + len(encoded)
        chunks.append(
            PackageChunk(
                invocation_id=invocation_id,
                index=len(chunks),
                byte_start=byte_start,
                byte_end=byte_end,
                text=chunk_text,
                digest=Sha256.of(encoded),
            )
        )
        character_start += len(chunk_text)
        byte_start = byte_end
    assert b"".join(chunk.text.encode() for chunk in chunks) == content
    assert tuple(chunk.index for chunk in chunks) == tuple(range(len(chunks)))
    return tuple(chunks)


def _invocation_root() -> Path:
    configured = os.environ.get("ITREE_HARNESS_WORK_ROOT")
    if configured is not None:
        root = Path(configured).absolute()
    else:
        root = Path(tempfile.gettempdir()) / "itree-harness-work"
    root.mkdir(parents=True, exist_ok=True)
    assert root.is_dir(), {"root": root, "required": "directory"}
    return root


def _atomic_bytes(path: Path, content: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def _write_json(path: Path, model: BaseModel) -> None:
    _atomic_bytes(path, (model.model_dump_json() + "\n").encode())


def _read_manifest(directory: Path) -> PendingFull | PendingPartial | StreamFailure:
    adapter: TypeAdapter[PendingFull | PendingPartial] = TypeAdapter(StoredPreparation)
    try:
        return adapter.validate_json((directory / "manifest.json").read_bytes())
    except (OSError, ValidationError, ValueError) as error:
        return StreamFailure(
            kind="integrity_failure",
            invocation_id=None,
            diagnostic=str(error),
        )


def _read_cursor(directory: Path) -> Cursor | StreamFailure:
    try:
        return Cursor.model_validate_json((directory / "cursor.json").read_bytes())
    except (OSError, ValidationError, ValueError) as error:
        return StreamFailure(
            kind="integrity_failure",
            invocation_id=None,
            diagnostic=str(error),
        )


def _verified_chunk(
    preparation: PendingFull | PendingPartial, directory: Path, index: int
) -> PackageChunk | StreamFailure:
    if index >= len(preparation.chunks):
        return StreamFailure(
            kind="post_final_chunk",
            invocation_id=preparation.invocation_id,
            index=index,
            diagnostic=None,
        )
    expected = preparation.chunks[index]
    path = directory / "chunks" / f"{index:06d}.md"
    try:
        content = path.read_bytes()
    except OSError as error:
        return StreamFailure(
            kind="missing_chunk",
            invocation_id=preparation.invocation_id,
            index=index,
            diagnostic=str(error),
        )
    if content != expected.text.encode():
        return StreamFailure(
            kind="modified_chunk",
            invocation_id=preparation.invocation_id,
            index=index,
            diagnostic=None,
        )
    if Sha256.of(content) != expected.digest:
        return StreamFailure(
            kind="integrity_failure",
            invocation_id=preparation.invocation_id,
            index=index,
            diagnostic=None,
        )
    return expected


def _trash(directory: Path) -> str | None:
    result = subprocess.run(
        ["trash", str(directory)], capture_output=True, check=False, timeout=120
    )
    if result.returncode != 0:
        return result.stderr.decode("utf-8", errors="replace")
    if directory.exists():
        return f"scratch remains: {directory}"
    return None


def prepare_invocation(
    issue_ref: IssueRef,
    distribution: ItreeDistribution,
    deadline_ms: DeadlineMilliseconds,
) -> CommandResult:
    invocation_id = InvocationId.generate()
    directory = _invocation_root() / invocation_id.root
    directory.mkdir()
    (directory / "chunks").mkdir()
    outcome = supervise(
        (str(distribution.launcher_abspath), "work", issue_ref.slug), deadline_ms
    )
    _write_json(directory / "process.json", outcome)
    _write_json(directory / "distribution.json", distribution)
    started_epoch = time.time()
    _atomic_bytes(directory / "started-epoch.txt", f"{started_epoch}\n".encode())
    if isinstance(outcome, (TimedOut, Interrupted, SpawnFailed)):
        return CommandResult(
            exit_code=4,
            stdout=b"",
            stderr=(outcome.model_dump_json() + "\n").encode(),
        )
    assert isinstance(outcome, Exited), {"outcome": outcome}
    if outcome.returncode == 3:
        return CommandResult(exit_code=3, stdout=b"", stderr=outcome.stderr)
    if outcome.returncode not in (0, 2):
        return CommandResult(exit_code=5, stdout=b"", stderr=outcome.stderr)
    if not outcome.stdout:
        failure = BoundaryFailure(diagnostic="empty successful package output")
        return CommandResult(
            exit_code=5, stdout=b"", stderr=(failure.model_dump_json() + "\n").encode()
        )
    package_kind = PackageKind.full if outcome.returncode == 0 else PackageKind.partial
    if package_kind == PackageKind.full:
        lines = outcome.stdout.splitlines(keepends=True)
        if len(lines) < 2 or lines[1] != b"Work package complete\n":
            failure = BoundaryFailure(diagnostic="missing complete package marker")
            return CommandResult(
                exit_code=5,
                stdout=b"",
                stderr=(failure.model_dump_json() + "\n").encode(),
            )
    source = PackageSource(
        issue_ref=issue_ref,
        distribution=distribution,
        markdown=outcome.stdout,
        stdout=outcome.stdout,
        stderr=outcome.stderr,
        outcome=outcome,
        package_sha256=Sha256.of(outcome.stdout),
    )
    chunks = split_package(invocation_id, outcome.stdout)
    _atomic_bytes(directory / "source.md", outcome.stdout)
    for chunk in chunks:
        _atomic_bytes(
            directory / "chunks" / f"{chunk.index:06d}.md", chunk.text.encode()
        )
    common = dict(invocation_id=invocation_id, source=source, chunks=chunks)
    preparation: PendingFull | PendingPartial
    if package_kind == PackageKind.full:
        preparation = PendingFull.model_validate(common)
    else:
        preparation = PendingPartial.model_validate(common)
    _write_json(directory / "manifest.json", preparation)
    _write_json(directory / "cursor.json", Cursor(next_index=0))
    if isinstance(preparation, PendingPartial):
        return CommandResult(exit_code=2, stdout=outcome.stdout, stderr=outcome.stderr)
    output = {
        "kind": "prepared",
        "invocation_id": invocation_id.root,
        "issue_ref": issue_ref.slug,
        "package_kind": preparation.kind.value,
        "package_sha256": source.package_sha256.root,
        "byte_count": len(source.markdown),
        "chunk_count": len(chunks),
        "process": outcome.model_dump(mode="json"),
    }
    return CommandResult(
        exit_code=0,
        stdout=(json.dumps(output, ensure_ascii=False) + "\n").encode(),
        stderr=b"",
    )


def prepare_command(issue: str, deadline_ms: int) -> CommandResult:
    if deadline_ms <= 0:
        failure = UsageFailure(diagnostic="deadline_ms must be positive")
        return CommandResult(
            exit_code=2, stdout=b"", stderr=(failure.model_dump_json() + "\n").encode()
        )
    try:
        issue_ref = IssueRef.parse(issue)
    except ValueError as error:
        failure = UsageFailure(diagnostic=str(error))
        return CommandResult(
            exit_code=2, stdout=b"", stderr=(failure.model_dump_json() + "\n").encode()
        )
    resolution = resolve_distribution()
    if isinstance(resolution, DistributionFailure):
        return CommandResult(
            exit_code=5,
            stdout=b"",
            stderr=(resolution.model_dump_json() + "\n").encode(),
        )
    return prepare_invocation(issue_ref, resolution, deadline_ms)


def _failure_result(failure: StreamFailure, exit_code: int = 2) -> CommandResult:
    return CommandResult(
        exit_code=exit_code,
        stdout=b"",
        stderr=(failure.model_dump_json() + "\n").encode(),
    )


def next_command(raw_invocation_id: str) -> CommandResult:
    try:
        invocation_id = InvocationId.model_validate(raw_invocation_id)
    except ValidationError as error:
        return CommandResult(
            exit_code=2,
            stdout=b"",
            stderr=(
                UsageFailure(diagnostic=str(error)).model_dump_json() + "\n"
            ).encode(),
        )
    directory = _invocation_root() / invocation_id.root
    if not directory.is_dir():
        return _failure_result(
            StreamFailure(
                kind="unknown_invocation", invocation_id=invocation_id, diagnostic=None
            )
        )
    preparation = _read_manifest(directory)
    if isinstance(preparation, StreamFailure):
        return _failure_result(preparation)
    if preparation.invocation_id != invocation_id:
        return _failure_result(
            StreamFailure(
                kind="integrity_failure",
                invocation_id=invocation_id,
                diagnostic="directory and manifest invocation identities differ",
            )
        )
    cursor = _read_cursor(directory)
    if isinstance(cursor, StreamFailure):
        return _failure_result(cursor)
    chunk = _verified_chunk(preparation, directory, cursor.next_index)
    if isinstance(chunk, StreamFailure):
        cleanup_error = _trash(directory)
        if cleanup_error is not None:
            return _failure_result(
                StreamFailure(
                    kind="cleanup_failure",
                    invocation_id=invocation_id,
                    diagnostic=cleanup_error,
                ),
                exit_code=5,
            )
        failure = chunk.model_copy(update={"invocation_id": invocation_id})
        return _failure_result(failure)
    _write_json(directory / "cursor.json", Cursor(next_index=cursor.next_index + 1))
    output = {
        "kind": "chunk",
        "invocation_id": invocation_id.root,
        "issue_ref": preparation.source.issue_ref.slug,
        "package_kind": preparation.kind.value,
        "index": chunk.index,
        "chunk_count": len(preparation.chunks),
        "byte_start": chunk.byte_start,
        "byte_end": chunk.byte_end,
        "byte_count": len(preparation.source.markdown),
        "package_sha256": preparation.source.package_sha256.root,
        "sha256": chunk.digest.root,
        "text": chunk.text,
    }
    return CommandResult(
        exit_code=0,
        stdout=(json.dumps(output, ensure_ascii=False) + "\n").encode(),
        stderr=b"",
    )


def finish_command(raw_invocation_id: str) -> CommandResult:
    try:
        invocation_id = InvocationId.model_validate(raw_invocation_id)
    except ValidationError as error:
        return CommandResult(
            exit_code=2,
            stdout=b"",
            stderr=(
                UsageFailure(diagnostic=str(error)).model_dump_json() + "\n"
            ).encode(),
        )
    directory = _invocation_root() / invocation_id.root
    if not directory.is_dir():
        return _failure_result(
            StreamFailure(
                kind="replayed_invocation", invocation_id=invocation_id, diagnostic=None
            )
        )
    preparation = _read_manifest(directory)
    if isinstance(preparation, StreamFailure):
        return _failure_result(preparation)
    if preparation.invocation_id != invocation_id:
        return _failure_result(
            StreamFailure(
                kind="integrity_failure",
                invocation_id=invocation_id,
                diagnostic="directory and manifest invocation identities differ",
            )
        )
    if isinstance(preparation, PendingPartial):
        return _failure_result(
            StreamFailure(
                kind="partial_package", invocation_id=invocation_id, diagnostic=None
            )
        )
    cursor = _read_cursor(directory)
    if isinstance(cursor, StreamFailure):
        return _failure_result(cursor)
    if cursor.next_index != len(preparation.chunks):
        return _failure_result(
            StreamFailure(
                kind="premature_finish",
                invocation_id=invocation_id,
                index=cursor.next_index,
                diagnostic=None,
            )
        )
    for index in range(len(preparation.chunks)):
        verified = _verified_chunk(preparation, directory, index)
        if isinstance(verified, StreamFailure):
            return _failure_result(verified)
    try:
        source_bytes = (directory / "source.md").read_bytes()
    except OSError as error:
        return _failure_result(
            StreamFailure(
                kind="missing_chunk",
                invocation_id=invocation_id,
                diagnostic=str(error),
            )
        )
    if Sha256.of(source_bytes) != preparation.source.package_sha256:
        return _failure_result(
            StreamFailure(
                kind="integrity_failure", invocation_id=invocation_id, diagnostic=None
            )
        )
    started_epoch = float((directory / "started-epoch.txt").read_text().strip())
    receipt = EmissionReceipt(
        invocation_id=invocation_id,
        issue_ref=preparation.source.issue_ref,
        byte_count=len(preparation.source.markdown),
        chunk_count=len(preparation.chunks),
        package_sha256=preparation.source.package_sha256,
        elapsed_seconds=time.time() - started_epoch,
    )
    cleanup_error = _trash(directory)
    if cleanup_error is not None:
        return _failure_result(
            StreamFailure(
                kind="cleanup_failure",
                invocation_id=invocation_id,
                diagnostic=cleanup_error,
            ),
            exit_code=5,
        )
    return CommandResult(
        exit_code=0,
        stdout=(receipt.model_dump_json() + "\n").encode(),
        stderr=b"",
    )


def initialize_command() -> CommandResult:
    resolution = resolve_distribution()
    if isinstance(resolution, DistributionFailure):
        return CommandResult(
            exit_code=5,
            stdout=b"",
            stderr=(resolution.model_dump_json() + "\n").encode(),
        )
    github = shutil.which("gh")
    assert github is not None, {"required": "authenticated gh on PATH"}
    output = {
        "kind": "initialized",
        "distribution": resolution.model_dump(mode="json"),
        "gh": github,
        "python": sys.version.split()[0],
    }
    return CommandResult(
        exit_code=0,
        stdout=(json.dumps(output) + "\n").encode(),
        stderr=b"",
    )


class HarnessWorkBoundary(Protocol):
    @abstractmethod
    def prepare(
        self, issue_ref: IssueRef, deadline_ms: DeadlineMilliseconds
    ) -> tuple[InvocationId, Preparation]: ...

    @abstractmethod
    def next(self, invocation_id: InvocationId) -> PackageChunk | StreamFailure: ...

    @abstractmethod
    def finish(self, emitted: EmittedPackage) -> EmissionReceipt | StreamFailure: ...


def _json_text_leaves(value: JsonValue) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _json_text_leaves(item)]
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in _json_text_leaves(item)]
    return []


def compare_source_chunks(
    chunks: tuple[PackageChunk, ...], payload: ProviderInputPayload
) -> ProviderComparison:
    try:
        document: JsonValue = TypeAdapter(JsonValue).validate_json(payload.body)
    except ValidationError:
        return ProviderComparisonFailure(
            kind="missing", session_id=payload.session_id, chunk_index=0
        )
    leaves = _json_text_leaves(document)
    positions: list[tuple[int, int]] = []
    for index, chunk in enumerate(chunks):
        locations = [
            (leaf_index, leaf.find(chunk.text))
            for leaf_index, leaf in enumerate(leaves)
            if chunk.text in leaf
        ]
        if not locations:
            rendered = "\n".join(leaves).casefold()
            failure_kind: Literal["missing", "truncated", "spilled", "compacted"]
            if "compact" in rendered:
                failure_kind = "compacted"
            elif "spill" in rendered:
                failure_kind = "spilled"
            elif "truncat" in rendered:
                failure_kind = "truncated"
            else:
                failure_kind = "missing"
            return ProviderComparisonFailure(
                kind=failure_kind, session_id=payload.session_id, chunk_index=index
            )
        positions.append(locations[0])
    if positions != sorted(positions):
        return ProviderComparisonFailure(
            kind="reordered", session_id=payload.session_id, chunk_index=0
        )
    return ModelInputMatch(session_id=payload.session_id, chunk_count=len(chunks))


def model_context_verified(
    chunks: tuple[PackageChunk, ...], payload: ProviderInputPayload
) -> ModelInputMatch | ProviderComparisonFailure:
    return compare_source_chunks(chunks, payload)


def exhaust_preparation(value: Preparation) -> None:
    match value:
        case (
            PendingFull()
            | PendingPartial()
            | UsageFailure()
            | ReadFailure()
            | BoundaryFailure()
            | TimedOut()
            | Interrupted()
            | SpawnFailed()
        ):
            return
        case _:
            assert_never(value)


EvidenceMetadata = dict[str, JsonValue]
