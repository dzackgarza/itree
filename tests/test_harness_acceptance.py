from __future__ import annotations

import hashlib
import json
import os
import platform
import signal
import subprocess
import sys
import threading
import time
import yaml
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

from itree.harness import (
    ArtifactDescriptor,
    EnvironmentIdentity,
    EvidenceManifest,
    HarnessIdentity,
    InstallOutcome,
    InvocationRecord,
    LiveAcceptanceConfig,
    ProviderComparisonRecord,
    write_sealed_manifest,
)
from itree.harness_work import (
    EvidenceLevel,
    InvocationId,
    PackageChunk,
    ProviderComparisonFailure,
    ProviderInputPayload,
    Sha256,
    WatchdogOutcome,
    WatchdogStatus,
    compare_source_chunks,
)
from tests.test_harness import (
    fixture,
    HarnessFixture,
    _install,
    _installer_fixture,
    _snapshot,
)

SOURCE = Path(__file__).resolve().parents[1]
UPSTREAM = "a4d571229e164e440f8950cac034607bae7b7518"


class AcceptanceDisabled(BaseModel):
    kind: str = "AcceptanceDisabled"
    missing: tuple[str, ...]


@dataclass(frozen=True)
class NativeRun:
    harness: str
    native_session: str | None
    output: Path
    provider_requests: tuple[Path, ...]


class PreparedObservation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: str
    invocation_id: str
    issue_ref: str
    package_sha256: str
    byte_count: int
    chunk_count: int
    process: dict[str, JsonValue]


class ChunkObservation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: str
    invocation_id: str
    index: int
    chunk_count: int
    byte_start: int
    byte_end: int
    sha256: str
    text: str


class ReceiptObservation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: str
    evidence_level: str
    invocation_id: str
    package_sha256: str
    byte_count: int
    chunk_count: int


def _opt_ins() -> LiveAcceptanceConfig | AcceptanceDisabled:
    names = ("ITREE_HARNESS_LIVE_ACCEPTANCE",)
    missing = tuple(name for name in names if os.environ.get(name) != "1")
    if missing:
        return AcceptanceDisabled(missing=missing)
    return LiveAcceptanceConfig(
        live_harness_enabled=True, provider_capture_enabled=False
    )


def _append_json_document(value: JsonValue, documents: list[JsonValue]) -> None:
    documents.append(value)
    if isinstance(value, dict):
        for child in value.values():
            _append_json_document(child, documents)
    elif isinstance(value, list):
        for child in value:
            _append_json_document(child, documents)
    elif isinstance(value, str) and value.startswith(("{", "[")):
        try:
            nested = json.loads(value)
        except ValueError:
            return
        _append_json_document(nested, documents)


def _json_documents(raw: str) -> list[JsonValue]:
    documents: list[JsonValue] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            outer = TypeAdapter(JsonValue).validate_json(stripped)
        except ValueError:
            continue
        _append_json_document(outer, documents)
    return documents


def _observations(
    path: Path,
) -> tuple[
    dict[str, PreparedObservation],
    dict[str, list[ChunkObservation]],
    list[ReceiptObservation],
]:
    prepared: dict[str, PreparedObservation] = {}
    chunks: dict[str, list[ChunkObservation]] = {}
    receipts: list[ReceiptObservation] = []
    for raw in path.read_text(errors="replace").splitlines():
        operation = json.loads(raw)
        if not isinstance(operation, dict) or not isinstance(
            operation.get("stdout"), str
        ):
            continue
        for event in operation["stdout"].splitlines():
            part = json.loads(event)["part"]
            state = part.get("state", {})
            payloads = [state.get("output")]
            content = state.get("metadata", {}).get("content", [])
            payloads.extend(
                item.get("text") for item in content if isinstance(item, dict)
            )
            for text in payloads:
                if not isinstance(text, str) or not text.startswith('{"kind"'):
                    continue
                document = json.loads(text)
                kind = document["kind"]
                if kind == "prepared":
                    observation = PreparedObservation.model_validate(document)
                    prepared[observation.invocation_id] = observation
                elif kind == "chunk":
                    observation = ChunkObservation.model_validate(document)
                    existing = chunks.setdefault(observation.invocation_id, [])
                    if observation.index not in {chunk.index for chunk in existing}:
                        existing.append(observation)
                elif kind == "emission-receipt":
                    observation = ReceiptObservation.model_validate(document)
                    if all(
                        receipt.invocation_id != observation.invocation_id
                        for receipt in receipts
                    ):
                        receipts.append(observation)
    return prepared, chunks, receipts


def _stat_rows() -> list[tuple[int, int, int, str]]:
    output = subprocess.check_output(["ps", "-eo", "pid=,ppid=,pgid=,stat="], text=True)
    rows: list[tuple[int, int, int, str]] = []
    for line in output.splitlines():
        pid, parent, group, state = line.split()
        rows.append((int(pid), int(parent), int(group), state))
    return rows


def _descendant_groups(root: int) -> set[int]:
    rows = _stat_rows()
    children: dict[int, list[int]] = {}
    for pid, parent, group, state in rows:
        children.setdefault(parent, []).append(pid)
    found = {root}
    pending = [root]
    while pending:
        current = pending.pop()
        descendants = children.get(current, [])
        found.update(descendants)
        pending.extend(descendants)
    return {group for pid, parent, group, state in rows if pid in found}


def _remaining_children(root: int) -> tuple[int, ...]:
    descendants = {
        pid
        for pid, parent, group, state in _stat_rows()
        if _is_descendant(pid, root) and state != "Z"
    }
    return tuple(sorted(descendants))


def _is_descendant(candidate: int, root: int) -> bool:
    if candidate == root:
        return True
    children: dict[int, list[int]] = {}
    for pid, parent, group, state in _stat_rows():
        children.setdefault(parent, []).append(pid)
    pending = list(children.get(root, []))
    while pending:
        current = pending.pop()
        if current == candidate:
            return True
        pending.extend(children.get(current, []))
    return False


def external_watchdog(
    arguments: list[str], cwd: Path, environment: dict[str, str], deadline_ms: int
) -> tuple[WatchdogOutcome, str, str]:
    started = time.perf_counter()
    process = subprocess.Popen(
        arguments,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    output: dict[str, bytes] = {"stdout": b"", "stderr": b""}
    stdout_thread = threading.Thread(
        target=lambda: output.__setitem__("stdout", process.stdout.read())
    )
    stderr_thread = threading.Thread(
        target=lambda: output.__setitem__("stderr", process.stderr.read())
    )
    stdout_thread.start()
    stderr_thread.start()
    deadline = started + deadline_ms / 1000
    while process.poll() is None and time.perf_counter() < deadline:
        time.sleep(0.01)
    signals: list[int] = []
    terminate_wait: float | None = None
    kill_wait: float | None = None
    if process.poll() is None:
        groups = _descendant_groups(process.pid)
        for group in groups:
            os.killpg(group, signal.SIGTERM)
        signals.append(int(signal.SIGTERM))
        terminate_started = time.perf_counter()
        while process.poll() is None and time.perf_counter() - terminate_started < 0.5:
            time.sleep(0.01)
        terminate_wait = time.perf_counter() - terminate_started
    if process.poll() is None:
        groups = _descendant_groups(process.pid)
        for group in groups:
            os.killpg(group, signal.SIGKILL)
        signals.append(int(signal.SIGKILL))
        kill_started = time.perf_counter()
        returncode = process.wait()
        kill_wait = time.perf_counter() - kill_started
    else:
        returncode = process.wait()
    stdout_thread.join()
    stderr_thread.join()
    remaining = _remaining_children(process.pid)
    status = (
        WatchdogStatus.exited
        if not signals
        else WatchdogStatus.survivor_error
        if remaining
        else WatchdogStatus.forced_termination
    )
    outcome = WatchdogOutcome(
        status=status,
        native_deadline_ms=deadline_ms,
        external_elapsed_seconds=time.perf_counter() - started,
        signals=tuple(signals),
        terminate_wait_seconds=terminate_wait,
        kill_wait_seconds=kill_wait,
        reaped=process.poll() is not None,
        leader_returncode=returncode,
        remaining_children=remaining,
    )
    return (
        outcome,
        output["stdout"].decode(errors="replace"),
        output["stderr"].decode(errors="replace"),
    )


def _native_commands(
    harness: str, project: Path, session: str, argument: str, initial: bool
) -> list[str]:
    if harness == "claude":
        base = [
            "claude",
            "-p",
            "--permission-mode",
            "bypassPermissions",
            "--output-format",
            "stream-json",
            "--verbose",
            "--effort",
            "low",
        ]
        base.extend(["--session-id", session] if initial else ["--resume", session])
        base.append(argument)
        return base
    if harness == "codex":
        if initial:
            return [
                "codex",
                "exec",
                "--json",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                "-C",
                str(project),
                argument,
            ]
        return [
            "codex",
            "exec",
            "resume",
            session,
            "--json",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            argument,
        ]
    if harness == "opencode":
        base = ["opencode", "run", "--format", "json", "--auto", "--standalone"]
        if not initial:
            base.extend(["--session", session])
        base.append(argument)
        return base
    raise AssertionError({"harness": harness, "required": "supported native command"})


def _session_id(harness: str, requested: str, raw: str) -> str | None:
    if harness == "claude":
        return requested
    for document in _json_documents(raw):
        if not isinstance(document, dict):
            continue
        if harness == "codex" and document.get("type") == "thread.started":
            return str(document["thread_id"])
        if harness == "opencode":
            for key in ("sessionID", "sessionId", "session_id"):
                if key in document:
                    return str(document[key])
            info = document.get("info")
            if (
                isinstance(info, dict)
                and "id" in info
                and str(info["id"]).startswith("ses")
            ):
                return str(info["id"])
    return None


def _run_native(
    fixture: HarnessFixture, installer: InstallerFixture, harness: str, evidence: Path
) -> NativeRun:
    evidence.mkdir(parents=True)
    requested = str(uuid.uuid4())
    environment = os.environ.copy()
    native_record = evidence / "emission" / "native.jsonl"
    native_record.parent.mkdir(parents=True, exist_ok=True)
    session = requested
    initial_prompt = "Reply OK and do not load any issue or perform other work."
    started = time.perf_counter()
    initial_outcome, initial_stdout, initial_stderr = external_watchdog(
        _native_commands(harness, installer.project, session, initial_prompt, True),
        installer.project,
        environment,
        180_000,
    )
    native_record.write_text(
        json.dumps(
            {
                "operation": "bare",
                "outcome": initial_outcome.model_dump(mode="json"),
                "stdout": initial_stdout,
                "stderr": initial_stderr,
            }
        )
        + "\n"
    )
    if (
        initial_outcome.status != WatchdogStatus.exited
        or initial_outcome.leader_returncode != 0
    ):
        return NativeRun(harness, requested, native_record, ())
    session = _session_id(harness, requested, initial_stdout)
    if session is None:
        return NativeRun(harness, None, native_record, ())
    for label, number in (("A", fixture.small),):
        prompt_name = (
            "/speckit-itree-work"
            if harness in ("claude", "codex")
            else "/speckit.itree.work"
        )
        argument = f"{prompt_name} {fixture.ref(number)}"
        outcome, stdout, stderr = external_watchdog(
            _native_commands(harness, installer.project, session, argument, False),
            installer.project,
            environment,
            180_000,
        )
        with native_record.open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "operation": label,
                        "outcome": outcome.model_dump(mode="json"),
                        "stdout": stdout,
                        "stderr": stderr,
                    }
                )
                + "\n"
            )
        if outcome.status != WatchdogStatus.exited or outcome.leader_returncode != 0:
            return NativeRun(harness, session, native_record, ())
    native_record.open("a").write(
        json.dumps(
            {
                "elapsed_seconds": time.perf_counter() - started,
                "native_session": session,
            }
        )
        + "\n"
    )
    return NativeRun(harness, session, native_record, ())


def _descriptor(
    invocation: InvocationId,
    kind: str,
    ordinal: int,
    path: Path,
    root: Path,
    media: str,
) -> ArtifactDescriptor:
    content = path.read_bytes()
    return ArtifactDescriptor(
        invocation_id=invocation,
        kind=kind,
        ordinal=ordinal,
        path=path.relative_to(root),
        media_type=media,
        byte_count=len(content),
        sha256=Sha256.of(content),
    )


def _model_identity(run: NativeRun) -> str:
    for request in reversed(run.provider_requests):
        for document in _json_documents(request.read_text()):
            if isinstance(document, dict) and isinstance(document.get("model"), str):
                return str(document["model"])
    return "unavailable"


def _watchdog_outcomes(path: Path) -> tuple[WatchdogOutcome, ...]:
    outcomes: list[WatchdogOutcome] = []
    for document in _json_documents(path.read_text(errors="replace")):
        if isinstance(document, dict) and "outcome" in document:
            outcomes.append(WatchdogOutcome.model_validate(document["outcome"]))
    return tuple(outcomes)


def _persist_run(
    run: NativeRun,
    code_revision: str,
    environment: LiveAcceptanceConfig,
    install_outcome: InstallOutcome | None = None,
) -> EvidenceManifest:
    root = run.output.parents[1]
    prepared, chunk_records, receipts = _observations(run.output)
    artifacts: list[ArtifactDescriptor] = [
        _descriptor(
            InvocationId(next(iter(prepared))) if prepared else InvocationId.generate(),
            "emission-record",
            0,
            run.output,
            root,
            "application/x-ndjson",
        )
    ]
    invocations: list[InvocationRecord] = []
    comparisons = []
    for invocation_text, observation in prepared.items():
        invocation = InvocationId(invocation_text)
        chunks = sorted(
            chunk_records.get(invocation_text, []), key=lambda item: item.index
        )
        complete = len(chunks) == observation.chunk_count and [
            item.index for item in chunks
        ] == list(range(observation.chunk_count))
        receipt = next(
            (item for item in receipts if item.invocation_id == invocation_text), None
        )
        if complete:
            texts = [item.text for item in chunks]
            content = "".join(texts).encode()
            complete = (
                len(content) == observation.byte_count
                and hashlib.sha256(content).hexdigest() == observation.package_sha256
                and receipt is not None
            )
        if complete:
            for chunk in chunks:
                target = (
                    root / "source-chunks" / invocation_text / f"{chunk.index:06d}.md"
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(chunk.text)
                artifacts.append(
                    _descriptor(
                        invocation,
                        "source-chunk",
                        chunk.index,
                        target,
                        root,
                        "text/markdown",
                    )
                )
            invocations.append(
                InvocationRecord(
                    invocation_id=invocation,
                    issue_ref=observation.issue_ref,
                    package_sha256=Sha256(observation.package_sha256),
                    package_bytes=observation.byte_count,
                    first_chunk_index=0,
                    last_chunk_index=observation.chunk_count - 1,
                    distribution_launcher=Path(str(observation.process["argv"][0])),
                    process_outcome=str(observation.process["kind"]),
                    wall_seconds=float(observation.process["wall_seconds"]),
                )
            )
        session = run.native_session
        requests = run.provider_requests if session is not None else ()
        for request in requests:
            package_chunks = tuple(
                PackageChunk(
                    invocation_id=invocation,
                    index=chunk.index,
                    byte_start=chunk.byte_start,
                    byte_end=chunk.byte_end,
                    text=chunk.text,
                    digest=Sha256(chunk.sha256),
                )
                for chunk in chunks
            )
            comparison = (
                compare_source_chunks(
                    package_chunks,
                    ProviderInputPayload(session_id=session, body=request.read_bytes()),
                )
                if package_chunks
                else None
            )
            if comparison is not None:
                comparisons.append(
                    ProviderComparisonRecord(
                        invocation_id=invocation,
                        session_id=session,
                        status="matched"
                        if comparison.kind == "match"
                        else comparison.kind,
                        chunk_index=comparison.chunk_index
                        if isinstance(comparison, ProviderComparisonFailure)
                        else None,
                    )
                )
    provider_artifacts = [
        _descriptor(
            InvocationId.generate(),
            "provider-input",
            ordinal,
            request,
            root,
            "application/json",
        )
        for ordinal, request in enumerate(run.provider_requests)
    ]
    artifacts.extend(provider_artifacts)
    install_owner = (
        InvocationId(next(iter(prepared))) if prepared else InvocationId.generate()
    )
    for index, path in enumerate(sorted((root / "install").glob("*.json"))):
        artifacts.append(
            _descriptor(
                install_owner,
                "install-record",
                index,
                path,
                root,
                "application/json",
            )
        )
    complete_sources = len(invocations) == len(prepared) == 1
    payload_matched = (
        bool(provider_artifacts)
        and comparisons
        and all(item.status == "matched" for item in comparisons)
    )
    level = (
        EvidenceLevel.model_context_verified
        if complete_sources and payload_matched
        else EvidenceLevel.emission_only
        if complete_sources
        else EvidenceLevel.unaccepted
    )
    acceptance = (
        "accepted"
        if level == EvidenceLevel.model_context_verified
        else "emission-only"
        if level == EvidenceLevel.emission_only
        else "unaccepted"
    )
    manifest = EvidenceManifest(
        schema_version=1,
        run_id=root.name,
        code_revision=code_revision,
        upstream_revision=UPSTREAM,
        harness=HarnessIdentity(
            name=run.harness,
            executable=Path(run.harness),
            cli_version=subprocess.check_output(
                [run.harness, "--version"], text=True
            ).strip(),
            model_provider=_model_identity(run),
            native_session_ids=(run.native_session,)
            if run.native_session is not None
            else (),
        ),
        environment=EnvironmentIdentity(
            python=sys.version.split()[0],
            node=subprocess.check_output(["node", "--version"], text=True).strip(),
            operating_system=platform.platform(),
            uv=subprocess.check_output(["uv", "--version"], text=True).strip(),
            pythonpath=os.environ.get("PYTHONPATH"),
            live_harness_enabled=environment.live_harness_enabled,
            provider_capture_enabled=environment.provider_capture_enabled,
        ),
        invocations=tuple(invocations),
        artifacts=tuple(artifacts),
        comparisons=tuple(comparisons),
        install_outcome=install_outcome,
        processes=_watchdog_outcomes(run.output),
        evidence_level=level,
        acceptance=acceptance,
        manifest_sha256=Sha256("0" * 64),
    )
    write_sealed_manifest(root, manifest)
    return manifest


def _installation_lifecycle(
    installer: InstallerFixture, evidence: Path, harness: str
) -> InstallOutcome:
    update_output = evidence / "install" / "update-and-remove.json"
    update_output.parent.mkdir(parents=True, exist_ok=True)
    extension = SOURCE / "src" / "itree" / "resources" / "speckit_itree"
    before_project = _snapshot(installer.project)
    before_external = _snapshot(installer.external)
    updated = subprocess.run(
        [
            str(SOURCE / ".venv" / "bin" / "specify"),
            "extension",
            "add",
            str(extension),
            "--dev",
            "--force",
        ],
        cwd=installer.project,
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
    )
    assert updated.returncode == 0, updated.stdout + updated.stderr
    refreshed = _install(installer)
    assert refreshed.returncode == 0, refreshed.stdout + refreshed.stderr
    refreshed_outcome = json.loads(refreshed.stdout)["outcome"]
    updated_registry = (
        installer.project / ".specify" / "extensions" / ".registry"
    ).read_text()
    removed = subprocess.run(
        [
            str(SOURCE / ".venv" / "bin" / "specify"),
            "extension",
            "remove",
            "itree",
            "--force",
        ],
        cwd=installer.project,
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
    )
    assert removed.returncode == 0, removed.stdout + removed.stderr
    registry_after = json.loads(
        (installer.project / ".specify" / "extensions" / ".registry").read_text()
    )
    assert "itree" not in registry_after["extensions"]
    installed_after = yaml.safe_load(
        (installer.project / ".specify" / "extensions.yml").read_text()
    )["installed"]
    assert "itree" not in installed_after
    removed_paths = (
        ".specify/extensions/itree",
        ".claude/skills/speckit-itree-work",
        ".claude/skills/speckit-itree-initialize",
        ".agents/skills/speckit-itree-work",
        ".agents/skills/speckit-itree-initialize",
        ".opencode/commands/speckit.itree.work.md",
        ".opencode/commands/speckit.itree.initialize.md",
    )
    for relative in removed_paths:
        assert not (installer.project / relative).exists(), relative
    assert (
        installer.project.joinpath("AGENTS.md").read_text() == "PROJECT-INSTRUCTIONS\n"
    )
    assert (
        installer.project.joinpath("CLAUDE.md").read_text() == "CLAUDE-INSTRUCTIONS\n"
    )
    assert (
        installer.project.joinpath(".claude", "settings.local.json").read_text()
        == '{"local": true}\n'
    )
    assert installer.project.joinpath(".agents").is_symlink()
    assert os.readlink(installer.project / ".agents") == str(installer.external)
    after_project = _snapshot(installer.project)
    after_external = _snapshot(installer.external)
    preserved_before = {
        path: value
        for path, value in before_external.items()
        if value[0] in {"file", "symlink"}
    }
    preserved_after = {
        path: value
        for path, value in after_external.items()
        if value[0] in {"file", "symlink"}
    }
    removed_external = tuple(
        source.removeprefix(".agents/")
        for source in removed_paths
        if source.startswith(".agents/")
    )
    for relative in removed_external:
        preserved_before = {
            path: value
            for path, value in preserved_before.items()
            if path != relative and not path.startswith(relative + "/")
        }
        preserved_after = {
            path: value
            for path, value in preserved_after.items()
            if path != relative and not path.startswith(relative + "/")
        }
    assert preserved_before == preserved_after, {
        "external_differences": {
            path: (preserved_before.get(path), preserved_after.get(path))
            for path in sorted(set(preserved_before) | set(preserved_after))
            if preserved_before.get(path) != preserved_after.get(path)
        }
    }
    evidence_record = {
        "harness": harness,
        "before_project": before_project,
        "before_external": before_external,
        "update": {
            "exit": updated.returncode,
            "stdout": updated.stdout,
            "stderr": updated.stderr,
        },
        "refresh": {"exit": refreshed.returncode, "outcome": refreshed_outcome},
        "registry_after_refresh": json.loads(updated_registry),
        "remove": {
            "exit": removed.returncode,
            "stdout": removed.stdout,
            "stderr": removed.stderr,
        },
        "registry_after_remove": registry_after,
        "after_project": after_project,
        "after_external": after_external,
        "preserved_external_unrelated": preserved_after,
        "removed_entries_satisfied": True,
    }
    update_output.write_text(
        json.dumps(evidence_record, indent=2, sort_keys=True) + "\n"
    )
    return InstallOutcome.model_validate(refreshed_outcome)


def test_real_native_harness_acceptance(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    config = _opt_ins()
    if isinstance(config, AcceptanceDisabled):
        assert config.missing
        return
    fixture: HarnessFixture = request.getfixturevalue("fixture")
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True
    ).strip()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    results: dict[str, str] = {}
    for harness in ("claude", "codex", "opencode"):
        installer = _installer_fixture(tmp_path / harness)
        installed = _install(installer)
        assert installed.returncode == 0, installed.stdout + installed.stderr
        run_id = f"{stamp}-{uuid.uuid4().hex[:8]}-{harness}"
        destination = SOURCE / "evidence" / "harness" / run_id
        destination.parent.mkdir(parents=True, exist_ok=True)
        run = _run_native(fixture, installer, harness, destination)
        install_outcome = _installation_lifecycle(installer, destination, harness)
        manifest = _persist_run(run, revision, config, install_outcome)
        results[harness] = manifest.acceptance
    assert set(results) == {"claude", "codex", "opencode"}
    assert all(
        value in ("accepted", "emission-only", "unaccepted")
        for value in results.values()
    )
