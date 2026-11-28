from __future__ import annotations

import hashlib
import http.client
import http.server
import json
import os
import platform
import signal
import subprocess
import sys
import threading
import time
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
    _comment,
    _install,
    _installer_fixture,
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
    names = ("ITREE_HARNESS_LIVE_ACCEPTANCE", "ITREE_HARNESS_CAPTURE_PROVIDER")
    missing = tuple(name for name in names if os.environ.get(name) != "1")
    if missing:
        return AcceptanceDisabled(missing=missing)
    return LiveAcceptanceConfig(
        live_harness_enabled=True, provider_capture_enabled=True
    )


def _text_leaves(value: JsonValue) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _text_leaves(item)]
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in _text_leaves(item)]
    return []


def _json_documents(raw: str) -> list[JsonValue]:
    candidates: list[JsonValue] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            outer = TypeAdapter(JsonValue).validate_json(stripped)
        except ValueError:
            continue
        candidates.append(outer)
        for text in _text_leaves(outer):
            if not text.startswith(("{", "[")):
                continue
            try:
                candidates.append(TypeAdapter(JsonValue).validate_json(text))
            except ValueError:
                continue
    return candidates


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
    for document in _json_documents(path.read_text(errors="replace")):
        if not isinstance(document, dict) or "kind" not in document:
            continue
        kind = document["kind"]
        if kind == "prepared":
            observation = PreparedObservation.model_validate(document)
            prepared[observation.invocation_id] = observation
        elif kind == "chunk":
            observation = ChunkObservation.model_validate(document)
            chunks.setdefault(observation.invocation_id, []).append(observation)
        elif kind == "emission-receipt":
            receipts.append(ReceiptObservation.model_validate(document))
    return prepared, chunks, receipts


class ProviderRecorder(http.server.BaseHTTPRequestHandler):
    destination: Path
    upstream: str
    recorded: list[Path]
    lock: threading.Lock

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        with self.lock:
            sequence = len(self.recorded)
            target = self.destination / f"{sequence:06d}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            self.recorded.append(target)
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.casefold()
            not in {"host", "content-length", "connection", "proxy-connection"}
        }
        connection = http.client.HTTPSConnection(self.upstream, timeout=600)
        connection.request("POST", self.path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        self.send_response(response.status)
        for key, value in response.getheaders():
            if key.casefold() not in {
                "content-length",
                "connection",
                "transfer-encoding",
            }:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        connection.close()

    def log_message(self, format: str, *args: str) -> None:
        return


class ProxyServer:
    def __init__(self, run_directory: Path, upstream: str) -> None:
        recorder = type(
            "ConfiguredProviderRecorder",
            (ProviderRecorder,),
            {
                "destination": run_directory / "provider-input",
                "upstream": upstream,
                "recorded": [],
                "lock": threading.Lock(),
            },
        )
        self.recorder = recorder
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), recorder)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    @property
    def artifacts(self) -> tuple[Path, ...]:
        return tuple(self.recorder.recorded)

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()


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
    upstream = (
        "api.anthropic.com" if harness in ("claude", "opencode") else "api.openai.com"
    )
    proxy = ProxyServer(evidence, upstream)
    if harness in ("claude", "opencode"):
        environment["ANTHROPIC_BASE_URL"] = proxy.base_url
    else:
        environment["OPENAI_BASE_URL"] = proxy.base_url + "/v1"
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
        proxy.close()
        return NativeRun(harness, requested, native_record, proxy.artifacts)
    session = _session_id(harness, requested, initial_stdout)
    if session is None:
        proxy.close()
        return NativeRun(harness, None, native_record, proxy.artifacts)
    for label, number in (("A", fixture.small), ("B", fixture.large)):
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
            1_800_000,
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
            proxy.close()
            return NativeRun(harness, session, native_record, proxy.artifacts)
    changed = f"NATIVE-CHANGED-A-{time.time_ns()}-終\n"
    _comment(fixture, fixture.small, changed)
    prompt_name = (
        "/speckit-itree-work"
        if harness in ("claude", "codex")
        else "/speckit.itree.work"
    )
    outcome, stdout, stderr = external_watchdog(
        _native_commands(
            harness,
            installer.project,
            session,
            f"{prompt_name} {fixture.ref(fixture.small)}",
            False,
        ),
        installer.project,
        environment,
        1_800_000,
    )
    with native_record.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "operation": "changed-A",
                    "outcome": outcome.model_dump(mode="json"),
                    "stdout": stdout,
                    "stderr": stderr,
                }
            )
            + "\n"
        )
    native_record.open("a").write(
        json.dumps(
            {
                "elapsed_seconds": time.perf_counter() - started,
                "native_session": session,
            }
        )
        + "\n"
    )
    proxy.close()
    return NativeRun(harness, session, native_record, proxy.artifacts)


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
    run: NativeRun, code_revision: str, environment: LiveAcceptanceConfig
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
    complete_sources = len(invocations) == len(prepared) == 3 and len(prepared) > 0
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
    code = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True
    ).strip()
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
        install_outcome=None,
        processes=_watchdog_outcomes(run.output),
        evidence_level=level,
        acceptance=acceptance,
        manifest_sha256=Sha256("0" * 64),
    )
    write_sealed_manifest(root, manifest)
    return manifest


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
        manifest = _persist_run(run, revision, config)
        results[harness] = manifest.acceptance
    assert set(results) == {"claude", "codex", "opencode"}
    assert all(
        value in ("accepted", "emission-only", "unaccepted")
        for value in results.values()
    )
