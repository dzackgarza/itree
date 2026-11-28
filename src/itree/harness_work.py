from __future__ import annotations

from abc import abstractmethod
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel, model_validator

from .models import IssueRef

Sha256Text = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
MonotonicSeconds = Annotated[float, Field(ge=0)]
PositiveDuration = Annotated[float, Field(gt=0)]
ChunkByteCount = Annotated[int, Field(gt=0, le=8192)]
DeadlineMilliseconds = Annotated[int, Field(gt=0)]


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


class PackageSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    issue_ref: IssueRef
    distribution: ItreeDistribution
    markdown: bytes = Field(min_length=1)
    stdout: bytes
    stderr: bytes
    outcome: TerminalProcessOutcome
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
        assert len(encoded) <= 8192, {"index": self.index, "bytes": len(encoded)}
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
        "integrity_failure",
    ]
    invocation_id: InvocationId | None
    index: int | None = Field(default=None, ge=0)


class EmittedPackage(BaseModel):
    model_config = ConfigDict(frozen=True)

    pending: PendingFull
    next_index: int


class EmissionReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

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
    outcome: TerminalProcessOutcome


class BoundaryFailure(BaseModel):
    kind: Literal["boundary_failure"] = "boundary_failure"
    diagnostic: str = Field(min_length=1)


Preparation = (
    PendingFull | PendingPartial | UsageFailure | ReadFailure | BoundaryFailure
)


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


def prepare_issue(
    issue_ref: IssueRef,
    distribution: ItreeDistribution,
    deadline_ms: DeadlineMilliseconds,
) -> tuple[InvocationId, Preparation]:
    raise NotImplementedError


def next_chunk(
    invocation_id: InvocationId, store: ChunkStore
) -> PackageChunk | StreamFailure:
    return store.next_chunk(invocation_id)


def finish_emission(
    emitted: EmittedPackage,
    store: ChunkStore,
    trash: TrashPort,
    elapsed_seconds: MonotonicSeconds,
) -> EmissionReceipt | StreamFailure:
    raise NotImplementedError


def compare_source_chunks(
    chunks: tuple[PackageChunk, ...], payload: ProviderInputPayload
) -> ProviderComparison:
    raise NotImplementedError


def model_context_verified(
    chunks: tuple[PackageChunk, ...], payload: ProviderInputPayload
) -> ModelInputMatch | ProviderComparisonFailure:
    raise NotImplementedError


class HarnessWorkBoundary(Protocol):
    @abstractmethod
    def prepare(
        self, issue_ref: IssueRef, deadline_ms: DeadlineMilliseconds
    ) -> tuple[InvocationId, Preparation]: ...

    @abstractmethod
    def next(self, invocation_id: InvocationId) -> PackageChunk | StreamFailure: ...

    @abstractmethod
    def finish(self, emitted: EmittedPackage) -> EmissionReceipt | StreamFailure: ...


EvidenceMetadata = dict[str, JsonValue]
