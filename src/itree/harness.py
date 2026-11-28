from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from abc import abstractmethod
from datetime import UTC, datetime
from enum import StrEnum
from importlib.metadata import Distribution, PackageNotFoundError
from pathlib import Path
from typing import Literal, Protocol, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    model_validator,
)
import yaml

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


def canonical_manifest_bytes(manifest: EvidenceManifest) -> bytes:
    document: dict[str, JsonValue] = manifest.model_dump(mode="json")
    document.pop("manifest_sha256")
    return (
        json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def seal_manifest(manifest: EvidenceManifest) -> EvidenceManifest:
    digest = hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()
    return manifest.model_copy(update={"manifest_sha256": Sha256(digest)})


def write_sealed_manifest(run_directory: Path, manifest: EvidenceManifest) -> Path:
    sealed = seal_manifest(manifest)
    destination = run_directory / "manifest.json"
    destination.write_bytes((sealed.model_dump_json(indent=2) + "\n").encode("utf-8"))
    sidecar = run_directory / "manifest.sha256"
    sidecar.write_text(sealed.manifest_sha256.root + "\n", encoding="ascii")
    canonical = sealed.model_dump(mode="json")
    canonical.pop("manifest_sha256")
    expected = (
        json.dumps(canonical, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    assert hashlib.sha256(expected).hexdigest() == sidecar.read_text().strip(), {
        "manifest": destination,
        "sidecar": sidecar,
    }
    return destination


def verify_manifest_directory(run_directory: Path) -> EvidenceManifest:
    manifest = EvidenceManifest.model_validate_json(
        (run_directory / "manifest.json").read_bytes()
    )
    assert manifest.run_id == run_directory.name, {"run_directory": run_directory}
    sidecar = (run_directory / "manifest.sha256").read_text(encoding="ascii")
    assert sidecar == manifest.manifest_sha256.root + "\n", {"sidecar": sidecar}
    assert (
        hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()
        == manifest.manifest_sha256.root
    ), {"manifest": run_directory / "manifest.json"}
    recorded = {descriptor.path for descriptor in manifest.artifacts}
    assert len(recorded) == len(manifest.artifacts), {
        "duplicate_artifact_paths": recorded
    }
    for descriptor in manifest.artifacts:
        relative = descriptor.path
        assert not relative.is_absolute() and ".." not in relative.parts, {
            "path": relative
        }
        content = (run_directory / relative).read_bytes()
        assert len(content) == descriptor.byte_count, {
            "path": relative,
            "bytes": len(content),
        }
        assert Sha256.of(content) == descriptor.sha256, {"path": relative}
    actual = {
        path.relative_to(run_directory)
        for path in run_directory.rglob("*")
        if path.is_file() and path.name not in {"manifest.json", "manifest.sha256"}
    }
    assert actual == recorded, {
        "unrecorded": actual - recorded,
        "missing": recorded - actual,
    }
    return manifest


def verify_manifest_commit(
    commit: str, run_id: str, repository: Path
) -> EvidenceManifest:
    prefix = f"evidence/harness/{run_id}/"
    result = subprocess.run(
        ["git", "show", f"{commit}:{prefix}manifest.json"],
        cwd=repository,
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, {
        "commit": commit,
        "path": prefix,
        "stderr": result.stderr,
    }
    manifest = EvidenceManifest.model_validate_json(result.stdout)
    sidecar = subprocess.run(
        ["git", "show", f"{commit}:{prefix}manifest.sha256"],
        cwd=repository,
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert sidecar.returncode == 0
    assert sidecar.stdout.decode("ascii") == manifest.manifest_sha256.root + "\n"
    assert (
        hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()
        == manifest.manifest_sha256.root
    )
    listing = subprocess.run(
        [
            "git",
            "ls-tree",
            "-r",
            "--name-only",
            commit,
            "--",
            f"evidence/harness/{run_id}",
        ],
        cwd=repository,
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert listing.returncode == 0
    actual = {
        Path(name).relative_to(prefix) for name in listing.stdout.decode().splitlines()
    }
    recorded = {descriptor.path for descriptor in manifest.artifacts}
    assert len(recorded) == len(manifest.artifacts)
    assert actual - recorded == {Path("manifest.json"), Path("manifest.sha256")}
    assert recorded - actual == set()
    for descriptor in manifest.artifacts:
        content = subprocess.run(
            ["git", "show", f"{commit}:{prefix}{descriptor.path.as_posix()}"],
            cwd=repository,
            capture_output=True,
            check=False,
            timeout=120,
        )
        assert content.returncode == 0, {"commit": commit, "path": descriptor.path}
        assert len(content.stdout) == descriptor.byte_count
        assert Sha256.of(content.stdout) == descriptor.sha256
    return manifest


SPECIFY_REVISION = "a4d571229e164e440f8950cac034607bae7b7518"
INSTALL_OPERATIONS = (
    "stage-codex-core",
    "install-claude",
    "install-opencode",
    "merge-codex-core",
    "select-codex",
    "register-itree",
    "refresh-events",
    "render-claude",
    "render-codex",
    "render-opencode",
    "restore-default",
    "confirm",
)
PREFLIGHT_OPERATIONS = ("preflight", *INSTALL_OPERATIONS)
OWNED_COMMANDS = (
    ".claude/skills/speckit-itree-work/SKILL.md",
    ".claude/skills/speckit-itree-initialize/SKILL.md",
    ".agents/skills/speckit-itree-work/SKILL.md",
    ".agents/skills/speckit-itree-initialize/SKILL.md",
    ".opencode/commands/speckit.itree.work.md",
    ".opencode/commands/speckit.itree.initialize.md",
    ".opencode/plugin/speckit-events.ts",
    ".specify/events.py",
    ".specify/integrations/codex.manifest.json",
    ".specify/integration.json",
    ".specify/extensions.yml",
    ".specify/extensions/.registry",
    ".claude/settings.json",
    ".codex/config.toml",
    "opencode.json",
)


class IntegrationState(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: str
    integration_state_schema: int
    installed_integrations: list[str]
    integration_settings: dict[str, JsonValue]
    integration: str | None
    default_integration: str | None


class OwnedArtifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    sha256: Sha256


class HarnessInstallState(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1]
    upstream_revision: CommitSha
    artifacts: tuple[OwnedArtifact, ...]


class InstallBoundaryResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    exit_code: int
    outcome: AcceptedInstall | PartialInstall | PreflightConflict
    staging: Path | None


class InstallTracer:
    def __init__(self, configured: str | None) -> None:
        self.path = Path(configured) if configured is not None else None

    def record(self, operation: str, status: str) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "operation": operation,
                        "status": status,
                        "utc": datetime.now(UTC).isoformat(),
                    }
                )
                + "\n"
            )


def _json(path: Path) -> JsonValue:
    return TypeAdapter(JsonValue).validate_json(path.read_bytes())


def _write_json_file(path: Path, value: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.model_dump_json() + "\n", encoding="utf-8")


def _specify_launcher() -> Path:
    try:
        distribution = Distribution.from_name("specify-cli")
    except PackageNotFoundError as error:
        raise AssertionError(
            {"required": "specify-cli distribution", "error": str(error)}
        )
    upstream = distribution.read_text("direct_url.json")
    assert upstream is not None, {"required": "specify-cli direct_url.json"}
    upstream_document = _json_from_bytes(upstream.encode())
    assert isinstance(upstream_document, dict), upstream_document
    vcs_info = upstream_document["vcs_info"]
    assert isinstance(vcs_info, dict), vcs_info
    assert vcs_info["commit_id"] == SPECIFY_REVISION, {
        "required": SPECIFY_REVISION,
        "actual": vcs_info["commit_id"],
    }
    entrypoints = [
        entry
        for entry in distribution.entry_points
        if entry.group == "console_scripts"
        and entry.name == "specify"
        and entry.value == "specify_cli:main"
    ]
    assert len(entrypoints) == 1, {"entrypoints": entrypoints}
    record = distribution.read_text("RECORD")
    assert record is not None, {"required": "specify-cli RECORD"}
    candidates = [
        row[0]
        for row in csv.reader(record.splitlines())
        if row and Path(row[0]).name == "specify"
    ]
    assert len(candidates) == 1, {"launcher_candidates": candidates}
    launcher = Path(str(distribution.locate_file(candidates[0]))).resolve()
    assert launcher.is_file(), {"launcher": launcher}
    return launcher


def _json_from_bytes(content: bytes) -> JsonValue:
    return TypeAdapter(JsonValue).validate_json(content)


def _run_checked(arguments: tuple[str, ...], cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            arguments,
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
            timeout=300,
        )
    except subprocess.TimeoutExpired as error:
        return str(error)
    combined = result.stdout + result.stderr
    if result.returncode != 0:
        return combined
    if "warning" in combined.casefold() or "⚠" in combined:
        return combined
    return None


def _fresh_effects(
    operations: tuple[str, ...],
    confirmed_names: tuple[str, ...],
    failed: tuple[str, str] | None,
    project: Path,
) -> InstallOutcome:
    confirmed = tuple(
        EffectResult(
            ordinal=operations.index(name),
            operation_id=name,
            status=EffectStatus.confirmed,
            artifact=project,
            diagnostic=None,
        )
        for name in confirmed_names
    )
    failed_effect = None
    untouched_names = operations
    if failed is not None:
        name, diagnostic = failed
        failed_effect = EffectResult(
            ordinal=operations.index(name),
            operation_id=name,
            status=EffectStatus.failed,
            artifact=project,
            diagnostic=diagnostic,
        )
        untouched_names = operations[operations.index(name) + 1 :]
    else:
        untouched_names = ()
    untouched = tuple(
        EffectResult(
            ordinal=operations.index(name),
            operation_id=name,
            status=EffectStatus.untouched,
            artifact=project,
            diagnostic=None,
        )
        for name in untouched_names
    )
    return InstallOutcome(
        confirmed=confirmed, failed=failed_effect, untouched=untouched
    )


def _read_state(project: Path) -> IntegrationState:
    return IntegrationState.model_validate(
        _json(project / ".specify" / "integration.json")
    )


def _write_state(project: Path, state: IntegrationState) -> None:
    path = project / ".specify" / "integration.json"
    path.write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8")


def _sync_codex_core(staging: Path, project: Path) -> str | None:
    real_state = _read_state(project)
    for integration in ("codex", "opencode"):
        staged_root = staging / integration
        staged_state = _read_state(staged_root)
        real_state.installed_integrations = list(
            dict.fromkeys([*real_state.installed_integrations, integration])
        )
        real_state.integration_settings[integration] = (
            staged_state.integration_settings[integration]
        )
        _write_state(project, real_state)
        if integration == "codex":
            continue
        source = (
            staged_root / ".specify" / "integrations" / f"{integration}.manifest.json"
        )
        destination = (
            project / ".specify" / "integrations" / f"{integration}.manifest.json"
        )
        if destination.exists() and destination.read_bytes() == source.read_bytes():
            continue
        if destination.exists():
            return f"{integration} core collision: {destination}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    return None


def _collision(project: Path) -> str | None:
    agents = project / ".agents"
    if agents.is_symlink() and not agents.resolve().is_dir():
        return f"unsafe .agents target: {agents.resolve()}"
    if agents.exists() and not agents.is_dir():
        return f".agents is not a directory: {agents}"
    state_path = project / ".specify" / "itree-harness-state.json"
    if not state_path.exists():
        for relative in OWNED_COMMANDS[:6]:
            if (project / relative).exists():
                return f"unowned generated command collision: {project / relative}"
        if (project / ".specify" / "extensions" / "itree").exists():
            return f"unowned extension collision: {project / '.specify' / 'extensions' / 'itree'}"
    return None


def _source_extension() -> Path:
    return Path(__file__).resolve().parent / "resources" / "speckit_itree"


def _resource(name: str) -> Path:
    return Path(__file__).resolve().parent / "resources" / name


def _yaml_value(path: Path) -> JsonValue:
    return TypeAdapter(JsonValue).validate_python(yaml.safe_load(path.read_text()))


def _merge_extension_registry(staging: Path, project: Path) -> None:
    source_path = staging / "codex" / ".specify" / "extensions.yml"
    destination = project / ".specify" / "extensions.yml"
    if not destination.exists():
        destination.write_bytes(source_path.read_bytes())
        return
    staged = _yaml_value(source_path)
    current = _yaml_value(destination)
    assert isinstance(staged, dict) and isinstance(current, dict), {
        "staged": staged,
        "current": current,
    }
    staged_installed = staged["installed"]
    current_installed = current["installed"]
    assert isinstance(staged_installed, list) and isinstance(current_installed, list), {
        "staged": staged_installed,
        "current": current_installed,
    }
    current["installed"] = list(dict.fromkeys([*current_installed, *staged_installed]))
    destination.write_text(yaml.safe_dump(current, sort_keys=False), encoding="utf-8")


def _register_linked(launcher: Path, staging: Path, project: Path) -> str | None:
    failure = _run_checked(
        (str(launcher), "extension", "add", str(_source_extension()), "--dev"),
        staging / "codex",
    )
    if failure is not None:
        return failure
    _merge_extension_registry(staging, project)
    staged_registry = staging / "codex" / ".specify" / "extensions" / ".registry"
    real_registry = project / ".specify" / "extensions" / ".registry"
    if not real_registry.exists():
        real_registry.parent.mkdir(parents=True, exist_ok=True)
        real_registry.write_bytes(staged_registry.read_bytes())
    else:
        staged_metadata = _json(staged_registry)
        current_metadata = _json(real_registry)
        assert isinstance(staged_metadata, dict) and isinstance(
            current_metadata, dict
        ), {
            "staged_metadata": staged_metadata,
            "current_metadata": current_metadata,
        }
        staged_extensions = staged_metadata["extensions"]
        current_extensions = current_metadata["extensions"]
        assert isinstance(staged_extensions, dict) and isinstance(
            current_extensions, dict
        ), {
            "staged_extensions": staged_extensions,
            "current_extensions": current_extensions,
        }
        current_metadata["extensions"] = {**current_extensions, **staged_extensions}
        real_registry.write_text(json.dumps(current_metadata, indent=2) + "\n")
    staged_extension = staging / "codex" / ".specify" / "extensions" / "itree"
    real_extension = project / ".specify" / "extensions" / "itree"
    for source in sorted(staged_extension.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(staged_extension)
        destination = real_extension / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.read_bytes() != source.read_bytes():
                return f"extension content collision: {destination}"
            continue
        destination.write_bytes(source.read_bytes())
    for name in ("speckit-itree-work", "speckit-itree-initialize"):
        source = staging / "codex" / ".agents" / "skills" / name / "SKILL.md"
        destination = project / ".agents" / "skills" / name / "SKILL.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.read_bytes() != source.read_bytes():
                return f"linked skill collision: {destination}"
            continue
        destination.write_bytes(source.read_bytes())
    return None


def _operation(
    name: str,
    launcher: Path,
    project: Path,
    staging: Path,
    previous_default: str | None,
) -> str | None:
    match name:
        case "stage-codex-core":
            codex_staging = staging / "codex"
            codex_staging.joinpath(".specify").mkdir(parents=True)
            return _run_checked(
                (str(launcher), "integration", "install", "codex"), codex_staging
            )
        case "install-claude":
            return _run_checked(
                (str(launcher), "integration", "install", "claude", "--force"), project
            )
        case "install-opencode":
            opencode_staging = staging / "opencode"
            opencode_staging.joinpath(".specify").mkdir(parents=True)
            return _run_checked(
                (str(launcher), "integration", "install", "opencode", "--force"),
                opencode_staging,
            )
        case "merge-codex-core":
            return _sync_codex_core(staging, project)
        case "select-codex":
            failure = _run_checked(
                (str(launcher), "integration", "use", "codex"), project
            )
            if failure is not None:
                return failure
            source = (
                staging / "codex" / ".specify" / "integrations" / "codex.manifest.json"
            )
            destination = project / ".specify" / "integrations" / "codex.manifest.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
            return None
        case "register-itree":
            return _register_linked(launcher, staging, project)
        case "refresh-events":
            return _run_checked(
                (sys.executable, str(_resource("refresh_events.py"))), project
            )
        case "render-claude":
            return _run_checked(
                (str(launcher), "integration", "use", "claude"), project
            )
        case "render-codex":
            command = project / ".agents" / "skills" / "speckit-itree-work" / "SKILL.md"
            if not command.is_file():
                return f"missing registered Codex command: {command}"
            return None
        case "render-opencode":
            return _run_checked(
                (str(launcher), "integration", "use", "opencode"), project
            )
        case "restore-default":
            if previous_default is None:
                return None
            return _run_checked(
                (str(launcher), "integration", "use", previous_default), project
            )
        case "confirm":
            missing = [
                relative
                for relative in OWNED_COMMANDS
                if not (project / relative).is_file()
            ]
            if missing:
                return f"missing generated artifacts: {missing}"
            return None
        case _:
            raise AssertionError({"unknown_operation": name})


def _owned_artifacts(project: Path) -> tuple[OwnedArtifact, ...]:
    artifacts = [
        OwnedArtifact(
            path=relative, sha256=Sha256.of((project / relative).read_bytes())
        )
        for relative in OWNED_COMMANDS
    ]
    extension = project / ".specify" / "extensions" / "itree"
    artifacts.extend(
        OwnedArtifact(
            path=path.relative_to(project).as_posix(),
            sha256=Sha256.of(path.read_bytes()),
        )
        for path in sorted(extension.rglob("*"))
        if path.is_file()
    )
    return tuple(artifacts)


def _idempotent(
    project: Path, tracer: InstallTracer
) -> AcceptedInstall | PreflightConflict | None:
    state_path = project / ".specify" / "itree-harness-state.json"
    if not state_path.exists():
        return None
    try:
        state = HarnessInstallState.model_validate(_json(state_path))
    except (ValidationError, ValueError) as error:
        outcome = _fresh_effects(
            PREFLIGHT_OPERATIONS, (), ("preflight", str(error)), project
        )
        tracer.record("preflight", "failed")
        return PreflightConflict(outcome=outcome)
    expected = {artifact.path: artifact.sha256 for artifact in state.artifacts}
    required = {artifact.path for artifact in _owned_artifacts(project)}
    if required != set(expected):
        diagnostic = f"installed artifact set differs: actual={sorted(required)} recorded={sorted(expected)}"
        outcome = _fresh_effects(
            PREFLIGHT_OPERATIONS, (), ("preflight", diagnostic), project
        )
        tracer.record("preflight", "failed")
        return PreflightConflict(outcome=outcome)
    for relative, digest in expected.items():
        if Sha256.of((project / relative).read_bytes()) != digest:
            diagnostic = f"modified owned artifact: {project / relative}"
            outcome = _fresh_effects(
                PREFLIGHT_OPERATIONS, (), ("preflight", diagnostic), project
            )
            tracer.record("preflight", "failed")
            return PreflightConflict(outcome=outcome)
    for operation in INSTALL_OPERATIONS:
        tracer.record(operation, "unchanged")
    outcome = _fresh_effects(INSTALL_OPERATIONS, INSTALL_OPERATIONS, None, project)
    return AcceptedInstall(outcome=outcome)


def install_harness(project: Path | None = None) -> InstallBoundaryResult:
    root = (project or Path.cwd()).resolve()
    tracer = InstallTracer(os.environ.get("ITREE_HARNESS_INSTALL_TRACE"))
    collision = _collision(root)
    if collision is not None:
        tracer.record("preflight", "failed")
        outcome = _fresh_effects(
            PREFLIGHT_OPERATIONS, (), ("preflight", collision), root
        )
        return InstallBoundaryResult(
            exit_code=2, outcome=PreflightConflict(outcome=outcome), staging=None
        )
    tracer.record("preflight", "confirmed")
    idempotent = _idempotent(root, tracer)
    if isinstance(idempotent, PreflightConflict):
        return InstallBoundaryResult(exit_code=2, outcome=idempotent, staging=None)
    if isinstance(idempotent, AcceptedInstall):
        return InstallBoundaryResult(exit_code=0, outcome=idempotent, staging=None)
    launcher = _specify_launcher()
    previous_default = _read_state(root).default_integration
    staging = Path(
        tempfile.mkdtemp(prefix="itree-harness-install-", dir="/tmp/opencode")
    )
    confirmed: list[str] = []
    for name in INSTALL_OPERATIONS:
        tracer.record(name, "started")
        try:
            failure = _operation(name, launcher, root, staging, previous_default)
        except OSError as error:
            failure = str(error)
        if failure is not None:
            tracer.record(name, "failed")
            outcome = _fresh_effects(
                INSTALL_OPERATIONS, tuple(confirmed), (name, failure), root
            )
            return InstallBoundaryResult(
                exit_code=3,
                outcome=PartialInstall(outcome=outcome),
                staging=staging,
            )
        confirmed.append(name)
        tracer.record(name, "confirmed")
    state = HarnessInstallState(
        schema_version=1,
        upstream_revision=SPECIFY_REVISION,
        artifacts=_owned_artifacts(root),
    )
    _write_json_file(root / ".specify" / "itree-harness-state.json", state)
    cleaned = subprocess.run(
        ["trash", str(staging)], capture_output=True, check=False, timeout=120
    )
    assert cleaned.returncode == 0 and not staging.exists(), {
        "staging": staging,
        "cleanup_stderr": cleaned.stderr.decode(errors="replace"),
    }
    outcome = _fresh_effects(INSTALL_OPERATIONS, tuple(confirmed), None, root)
    return InstallBoundaryResult(
        exit_code=0,
        outcome=AcceptedInstall(outcome=outcome),
        staging=None,
    )
