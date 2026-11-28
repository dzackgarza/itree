from __future__ import annotations

from abc import abstractmethod
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from .harness_work import EvidenceLevel, InvocationId, Sha256, WatchdogOutcome
from .models import CommitSha

IntegrationKey = Literal["claude", "codex", "opencode"]
ArtifactKind = Literal[
    "source-chunk", "emission-record", "provider-input", "install-record", "process-log"
]


class InstallOperation(BaseModel):
    model_config = ConfigDict(frozen=True)

    ordinal: int = Field(ge=0)
    operation_id: str = Field(min_length=1)
    destination: Path
    expected_sha256: Sha256 | None


class InstallPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    integration_key: IntegrationKey
    operations: tuple[InstallOperation, ...]

    @model_validator(mode="after")
    def ordered(self) -> Self:
        assert tuple(item.ordinal for item in self.operations) == tuple(
            range(len(self.operations))
        ), {"operations": self.operations, "required": "contiguous operation ordinals"}
        return self


class EffectStatus(StrEnum):
    confirmed = "confirmed"
    failed = "failed"
    untouched = "untouched"


class EffectResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    ordinal: int = Field(ge=0)
    operation_id: str = Field(min_length=1)
    status: EffectStatus
    artifact: Path
    diagnostic: str | None


class InstallOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    confirmed: tuple[EffectResult, ...]
    failed: EffectResult | None
    untouched: tuple[EffectResult, ...]

    @model_validator(mode="after")
    def valid_partition(self) -> Self:
        assert all(item.status == EffectStatus.confirmed for item in self.confirmed)
        assert self.failed is None or self.failed.status == EffectStatus.failed
        assert all(item.status == EffectStatus.untouched for item in self.untouched)
        return self


class AcceptedInstall(BaseModel):
    kind: Literal["accepted"] = "accepted"
    outcome: InstallOutcome

    @model_validator(mode="after")
    def complete(self) -> Self:
        assert self.outcome.failed is None, {"outcome": self.outcome}
        return self


class PartialInstall(BaseModel):
    kind: Literal["partial"] = "partial"
    outcome: InstallOutcome

    @model_validator(mode="after")
    def incomplete(self) -> Self:
        assert self.outcome.failed is not None, {"outcome": self.outcome}
        return self


class PreflightConflict(BaseModel):
    kind: Literal["preflight-conflict"] = "preflight-conflict"
    outcome: InstallOutcome


class FileSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: Path
    exists: bool
    is_file: bool
    is_symlink: bool
    byte_count: int = Field(ge=0)
    sha256: Sha256 | None
    modified_ns: int | None


class LinkedSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    link: Path
    target: Path
    files: tuple[FileSnapshot, ...]


class SyncPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    project: LinkedSnapshot
    operations: tuple[InstallOperation, ...]


class SpecifyPort(Protocol):
    @abstractmethod
    def run(self, arguments: tuple[str, ...], cwd: Path) -> int: ...


class FsPort(Protocol):
    @abstractmethod
    def snapshot(self, paths: tuple[Path, ...]) -> LinkedSnapshot: ...

    @abstractmethod
    def apply(self, plan: SyncPlan) -> InstallOutcome: ...


class ArtifactDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True)

    invocation_id: InvocationId
    kind: ArtifactKind
    ordinal: int = Field(ge=0)
    path: Path
    media_type: str = Field(min_length=1)
    byte_count: int = Field(ge=0)
    sha256: Sha256


class HarnessIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: IntegrationKey
    executable: Path
    cli_version: str = Field(min_length=1)
    model_provider: str = Field(min_length=1)
    native_session_ids: tuple[str, ...]


class EnvironmentIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)

    python: str
    node: str
    operating_system: str
    uv: str
    pythonpath: str | None
    live_harness_enabled: Literal[True]
    provider_capture_enabled: Literal[True]


class InvocationRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    invocation_id: InvocationId
    issue_ref: str
    package_sha256: Sha256
    package_bytes: int = Field(gt=0)
    first_chunk_index: Literal[0]
    last_chunk_index: int = Field(ge=0)
    distribution_launcher: Path
    process_outcome: str
    wall_seconds: float = Field(ge=0)


class ProviderComparisonRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    invocation_id: InvocationId
    session_id: str
    status: Literal[
        "matched", "missing", "reordered", "truncated", "spilled", "compacted"
    ]
    chunk_index: int | None = Field(default=None, ge=0)


class EvidenceManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1]
    run_id: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    code_revision: CommitSha
    upstream_revision: CommitSha
    harness: HarnessIdentity
    environment: EnvironmentIdentity
    invocations: tuple[InvocationRecord, ...]
    artifacts: tuple[ArtifactDescriptor, ...]
    comparisons: tuple[ProviderComparisonRecord, ...]
    install_outcome: InstallOutcome | None
    processes: tuple[WatchdogOutcome, ...]
    evidence_level: EvidenceLevel
    acceptance: Literal["accepted", "emission-only", "unaccepted"]
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def acceptance_has_evidence(self) -> Self:
        provider_artifacts = tuple(
            item for item in self.artifacts if item.kind == "provider-input"
        )
        if self.evidence_level == EvidenceLevel.model_context_verified:
            assert provider_artifacts, {"evidence_level": self.evidence_level}
            assert self.acceptance == "accepted", {"acceptance": self.acceptance}
        else:
            assert self.acceptance != "accepted", {
                "acceptance": self.acceptance,
                "evidence_level": self.evidence_level,
            }
        return self


class LiveAcceptanceConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    live_harness_enabled: Literal[True]
    provider_capture_enabled: Literal[True]


class AcceptanceDisabled(BaseModel):
    kind: Literal["AcceptanceDisabled"] = "AcceptanceDisabled"
    missing: tuple[
        Literal["ITREE_HARNESS_LIVE_ACCEPTANCE", "ITREE_HARNESS_CAPTURE_PROVIDER"], ...
    ]


class EvidenceStore(Protocol):
    @abstractmethod
    def write(self, run_id: str, manifest: EvidenceManifest) -> Path: ...

    @abstractmethod
    def verify(self, commit: str, run_id: str) -> EvidenceManifest: ...


class ProviderInputPort(Protocol):
    @abstractmethod
    def capture(self, session_id: str) -> tuple[bytes, ...]: ...


JsonDocument = dict[str, JsonValue]
