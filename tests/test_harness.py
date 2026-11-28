from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from pydantic import JsonValue

SOURCE = Path(__file__).resolve().parents[1]
TYPE_FIXTURE = SOURCE / "tests" / "fixtures" / "harness_types" / "invalid.py"
SMALL_SIZE = 300 * 1024
LARGE_SIZE = 2_300_000
CHUNK_SIZE = 8192
SPEC_PIN = "a4d571229e164e440f8950cac034607bae7b7518"


@dataclass(frozen=True)
class HarnessFixture:
    root: Path
    repo: str
    small: int
    large: int
    invalid: int
    slow: int
    journal: Path

    def ref(self, number: int) -> str:
        return f"{self.repo}#{number}"

    def record(
        self, operation: str, result: subprocess.CompletedProcess[str], started: float
    ) -> None:
        with self.journal.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "operation": operation,
                        "exit": result.returncode,
                        "elapsed_seconds": time.perf_counter() - started,
                        "stdout_bytes": len(result.stdout.encode()),
                        "stderr_bytes": len(result.stderr.encode()),
                    }
                )
                + "\n"
            )


def _execute(
    fixture: HarnessFixture,
    operation: str,
    argv: list[str],
    *,
    cwd: Path | None = None,
    payload: str | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    started = time.perf_counter()
    result = subprocess.run(
        argv,
        cwd=cwd or fixture.root,
        input=payload,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    fixture.record(operation, result, started)
    assert result.returncode == 0, {
        "argv": argv,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    return result


def _write_issue(fixture: HarnessFixture, title: str, body: str) -> int:
    result = _execute(
        fixture,
        f"create:{title}",
        [
            "gh",
            "api",
            "-X",
            "POST",
            f"repos/{fixture.repo}/issues",
            "--input",
            "-",
        ],
        payload=json.dumps({"title": title, "body": body}),
    )
    return int(json.loads(result.stdout)["number"])


def _comment(fixture: HarnessFixture, number: int, body: str) -> None:
    _execute(
        fixture,
        f"comment:{number}",
        [
            "gh",
            "api",
            "-X",
            "POST",
            f"repos/{fixture.repo}/issues/{number}/comments",
            "--input",
            "-",
        ],
        payload=json.dumps({"body": body}),
    )


def managed_body(marker: str) -> str:
    return "\n".join(
        (
            "<!-- itree:original-contract:start -->",
            f"ORIGINAL-{marker}-α",
            "<!-- itree:original-contract:end -->",
            "<!-- itree:requirements:start -->",
            f"REQUIREMENTS-{marker}-β",
            "<!-- itree:requirements:end -->",
            "<!-- itree:plan:start -->",
            f"PLAN-{marker}-γ",
            "<!-- itree:plan:end -->",
            "<!-- itree:tasks:start -->",
            f"TASKS-{marker}-δ",
            "<!-- itree:tasks:end -->",
            "",
        )
    )


def text_block(label: str, index: int, size: int) -> str:
    unique = f"UNIQUE-{label}-{index:04d}-ε-中-🙂\n"
    return unique + (f"{label}-{index:04d}-content-λ\n" * (size // 20))[:size] + "\n"


@pytest.fixture(scope="session")
def fixture() -> Iterator[HarnessFixture]:
    parent = Path(tempfile.mkdtemp(prefix="itree-harness-", dir="/tmp/opencode"))
    journal = parent / "journal.jsonl"
    repo_name = f"itree-harness-{time.time_ns()}"
    repo = f"dzackgarza/{repo_name}"
    placeholder = HarnessFixture(parent, repo, 0, 0, 0, 0, journal)
    with ExitStack() as stack:
        stack.callback(
            lambda: (
                parent.exists()
                and subprocess.run(
                    ["trash", str(parent)],
                    check=False,
                    capture_output=True,
                    timeout=120,
                )
            )
        )
        checkout = parent / "fixture"
        checkout.mkdir()
        _execute(
            placeholder, "repo-create", ["gh", "repo", "create", repo_name, "--private"]
        )
        stack.callback(
            lambda: subprocess.run(
                ["gh", "repo", "delete", repo, "--yes"],
                check=False,
                capture_output=True,
                timeout=120,
            )
        )
        for relative, content in {
            "AGENTS.md": text_block("AGENTS", 0, 150_000),
            "CONTRIBUTING.md": "CONTRIBUTING-unique\n",
            ".specify/memory/constitution.md": "CONSTITUTION-unique NS-002 NS-003\n",
            "src/itree/WORKFLOWS.md": "WORKFLOWS-unique\n",
        }.items():
            target = checkout / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            payload = json.dumps(
                {
                    "message": f"Create {relative}",
                    "content": base64.b64encode(content.encode()).decode(),
                    "branch": "main",
                }
            )
            _execute(
                placeholder,
                f"contents:{relative}",
                [
                    "gh",
                    "api",
                    "-X",
                    "PUT",
                    f"repos/{repo}/contents/{relative}",
                    "--input",
                    "-",
                ],
                payload=payload,
            )
        small = _write_issue(
            placeholder, "Small managed package", managed_body("SMALL")
        )
        large = _write_issue(
            placeholder, "Large legacy package", "LARGE-ORIGINAL-αρχή\n"
        )
        invalid = _write_issue(
            placeholder,
            "Invalid managed package",
            "<!-- itree:original-contract:start -->\nINVALID-READABLE-PREFIX\n<!-- itree:original-contract:end -->\n",
        )
        slow = _write_issue(placeholder, "Slow source package", "SLOW-SOURCE-PENDING\n")
        for index in range(7):
            _comment(placeholder, small, text_block("SMALL-COMMENT", index, 38_000))
        for index in range(45):
            _comment(placeholder, large, text_block("LARGE-COMMENT", index, 48_000))
        value = HarnessFixture(parent, repo, small, large, invalid, slow, journal)
        started = time.perf_counter()
        yield value
        journal.open("a", encoding="utf-8").write(
            json.dumps({"case_seconds": time.perf_counter() - started}) + "\n"
        )


@pytest.fixture(scope="session")
def clean_wheel_target(fixture: HarnessFixture) -> Path:
    root = fixture.root / "clean-wheel-target"
    distribution = root / "dist"
    target = root / "venv"
    _execute(
        fixture,
        "wheel-build",
        ["uv", "build", "--out-dir", str(distribution)],
        cwd=SOURCE,
        timeout=600,
    )
    wheel = next(distribution.glob("itree-*.whl"))
    _execute(
        fixture,
        "wheel-venv",
        ["uv", "venv", "--python", sys.executable, str(target)],
        timeout=300,
    )
    _execute(
        fixture,
        "wheel-install",
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(target / "bin" / "python"),
            str(wheel),
            f"specify-cli @ git+https://github.com/github/spec-kit@{SPEC_PIN}",
        ],
        timeout=600,
    )
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        expected = {
            "itree/resources/speckit_itree/extension.yml",
            "itree/resources/speckit_itree/commands/work.md",
            "itree/resources/speckit_itree/commands/initialize.md",
            "itree/resources/speckit_itree/scripts/python/work_loader.py",
            "itree/resources/speckit_itree/scripts/python/initialize.py",
        }
        assert expected <= names
        launcher_info = archive.getinfo(
            "itree/resources/speckit_itree/scripts/python/work_loader.py"
        )
        assert launcher_info.external_attr >> 16 & 0o111
    return target


def test_rendered_wheel_uses_its_console_launcher_without_source_imports(
    fixture: HarnessFixture, clean_wheel_target: Path
) -> None:
    project = fixture.root / "unrelated-rendered-project"
    project.joinpath(".specify").mkdir(parents=True)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = ""
    environment["PATH"] = (
        f"{clean_wheel_target / 'bin'}{os.pathsep}{environment['PATH']}"
    )
    environment["ITREE_HARNESS_WORK_ROOT"] = str(fixture.root / "unrelated-state")
    specify = clean_wheel_target / "bin" / "specify"
    installed = subprocess.run(
        [str(specify), "integration", "install", "codex"],
        cwd=project,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    rendered = subprocess.run(
        [
            str(specify),
            "extension",
            "add",
            str(SOURCE / "src" / "itree" / "resources" / "speckit_itree"),
            "--dev",
        ],
        cwd=project,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
    )
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    module = subprocess.run(
        [
            str(clean_wheel_target / "bin" / "python"),
            "-c",
            "import itree; print(itree.__file__)",
        ],
        cwd=project,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert module.returncode == 0
    assert str(clean_wheel_target) in module.stdout
    assert str(SOURCE) not in module.stdout
    helper = (
        project
        / ".specify"
        / "extensions"
        / "itree"
        / "scripts"
        / "python"
        / "work_loader.py"
    )
    result = subprocess.run(
        [str(helper), "prepare", fixture.ref(fixture.small), "--deadline-ms", "180000"],
        cwd=project,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=240,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    prepared = json.loads(result.stdout)
    launcher = clean_wheel_target / "bin" / "itree"
    assert prepared["process"]["argv"] == [
        str(launcher),
        "work",
        fixture.ref(fixture.small),
    ]
    binding = json.loads(
        (
            fixture.root
            / "unrelated-state"
            / prepared["invocation_id"]
            / "distribution.json"
        ).read_text()
    )
    assert binding["launcher_abspath"] == str(launcher)
    assert binding["entrypoint_value"] == "itree.cli:app"
    for script in (
        next(clean_wheel_target.glob("lib/python*/site-packages/itree/cli.py")),
        next(clean_wheel_target.glob("lib/python*/site-packages/itree/__main__.py")),
    ):
        warmup = subprocess.run(
            ["uv", "run", "--script", str(script), "--help"],
            cwd=project,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=240,
        )
        assert warmup.returncode == 0, warmup.stdout + warmup.stderr
    commands = (
        [str(launcher), "work", fixture.ref(fixture.small)],
        [
            str(clean_wheel_target / "bin" / "python"),
            "-m",
            "itree",
            "work",
            fixture.ref(fixture.small),
        ],
        [
            "uv",
            "run",
            "--script",
            str(
                next(clean_wheel_target.glob("lib/python*/site-packages/itree/cli.py"))
            ),
            "work",
            fixture.ref(fixture.small),
        ],
        [
            "uv",
            "run",
            "--script",
            str(
                next(
                    clean_wheel_target.glob(
                        "lib/python*/site-packages/itree/__main__.py"
                    )
                )
            ),
            "work",
            fixture.ref(fixture.small),
        ],
    )
    outputs = [
        subprocess.run(
            command,
            cwd=project,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=240,
        )
        for command in commands
    ]
    assert all(result.returncode == 0 for result in outputs), [
        (result.stdout, result.stderr) for result in outputs
    ]
    assert len({result.stdout for result in outputs}) == 1
    assert len({result.stderr for result in outputs}) == 1
    empty = fixture.root / "module-only-venv"
    _execute(
        fixture,
        "module-only-venv",
        ["uv", "venv", "--python", sys.executable, str(empty)],
    )
    rejected = subprocess.run(
        [str(helper), "initialize"],
        cwd=project,
        env={
            **environment,
            "PATH": f"{empty / 'bin'}{os.pathsep}{environment['PATH']}",
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert rejected.returncode == 5
    assert json.loads(rejected.stderr)["kind"] == "distribution-failure"


def _invoke(
    fixture: HarnessFixture, state: Path, *arguments: str, timeout: int = 240
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["ITREE_HARNESS_WORK_ROOT"] = str(state)
    started = time.perf_counter()
    result = subprocess.run(
        [str(SOURCE / ".venv" / "bin" / "itree"), "harness-work", *arguments],
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
        env=environment,
    )
    fixture.record(f"public:{' '.join(arguments[:2])}", result, started)
    return result


def _prepare(fixture: HarnessFixture, state: Path, number: int) -> dict[str, JsonValue]:
    result = _invoke(
        fixture, state, "prepare", fixture.ref(number), "--deadline-ms", "180000"
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["kind"] == "prepared"
    assert value["issue_ref"] == fixture.ref(number)
    return value


def _one_chunk(
    fixture: HarnessFixture, state: Path, invocation: str, index: int
) -> dict[str, JsonValue]:
    result = _invoke(fixture, state, "next", invocation)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["kind"] == "chunk"
    assert value["index"] == index
    assert value["invocation_id"] == invocation
    assert 0 < len(value["text"].encode()) <= CHUNK_SIZE
    assert hashlib.sha256(value["text"].encode()).hexdigest() == value["sha256"]
    return value


def _all_chunks(
    fixture: HarnessFixture, state: Path, prepared: dict[str, JsonValue]
) -> bytes:
    count = int(prepared["chunk_count"])
    invocation = str(prepared["invocation_id"])
    chunks = [_one_chunk(fixture, state, invocation, index) for index in range(count)]
    assert chunks[0]["byte_start"] == 0
    for previous, current in zip(chunks, chunks[1:], strict=False):
        assert current["byte_start"] == previous["byte_end"]
    assert chunks[-1]["byte_end"] == prepared["byte_count"]
    return "".join(str(chunk["text"]) for chunk in chunks).encode()


def _finish(
    fixture: HarnessFixture, state: Path, invocation: str
) -> dict[str, JsonValue]:
    result = _invoke(fixture, state, "finish", invocation)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["kind"] == "emission-receipt"
    assert value["evidence_level"] == "emission-only"
    return value


def _direct_work(
    fixture: HarnessFixture, number: int
) -> subprocess.CompletedProcess[str]:
    started = time.perf_counter()
    result = subprocess.run(
        [str(SOURCE / ".venv" / "bin" / "itree"), "work", fixture.ref(number)],
        text=True,
        capture_output=True,
        check=False,
        timeout=240,
    )
    fixture.record(f"direct-work:{number}", result, started)
    return result


def test_explicit_ids_interleave_and_changed_live_content_is_fresh(
    fixture: HarnessFixture, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    original = _direct_work(fixture, fixture.small)
    first = _prepare(fixture, state, fixture.small)
    second = _prepare(fixture, state, fixture.large)
    assert first["invocation_id"] != second["invocation_id"]
    first_zero = _one_chunk(fixture, state, str(first["invocation_id"]), 0)
    second_zero = _one_chunk(fixture, state, str(second["invocation_id"]), 0)
    first_one = _one_chunk(fixture, state, str(first["invocation_id"]), 1)
    assert (
        first_zero["text"].encode()
        == original.stdout.encode()[: len(first_zero["text"].encode())]
    )
    assert first_one["byte_start"] == first_zero["byte_end"]
    assert second_zero["text"].encode() != first_zero["text"].encode()
    missing = _invoke(fixture, state, "next")
    unknown = _invoke(fixture, state, "next", "0" * 32)
    premature = _invoke(fixture, state, "finish", str(second["invocation_id"]))
    assert missing.returncode != 0
    assert unknown.returncode != 0
    assert premature.returncode != 0
    mutation = f"CHANGED-LIVE-A-{time.time_ns()}-終\n"
    _comment(fixture, fixture.small, mutation)
    changed = _prepare(fixture, state, fixture.small)
    assert changed["invocation_id"] != first["invocation_id"]
    assert changed["package_sha256"] != first["package_sha256"]
    remaining_first = b"".join(
        [first_zero["text"].encode(), first_one["text"].encode()]
        + [
            _one_chunk(fixture, state, str(first["invocation_id"]), index)[
                "text"
            ].encode()
            for index in range(2, int(first["chunk_count"]))
        ]
    )
    assert remaining_first == original.stdout.encode()
    receipt = _finish(fixture, state, str(first["invocation_id"]))
    assert receipt["package_sha256"] == first["package_sha256"]
    replayed_finish = _invoke(fixture, state, "finish", str(first["invocation_id"]))
    post_final = _invoke(fixture, state, "next", str(first["invocation_id"]))
    assert replayed_finish.returncode != 0
    assert post_final.returncode != 0
    assert not state.joinpath(str(first["invocation_id"])).exists()


def test_both_size_bands_reconstruct_every_byte(
    fixture: HarnessFixture, tmp_path: Path
) -> None:
    for number, minimum in ((fixture.small, SMALL_SIZE), (fixture.large, LARGE_SIZE)):
        expected = _direct_work(fixture, number)
        prepared = _prepare(fixture, tmp_path / "state", number)
        assert prepared["byte_count"] >= minimum
        streamed = _all_chunks(fixture, tmp_path / "state", prepared)
        assert streamed == expected.stdout.encode()
        assert hashlib.sha256(streamed).hexdigest() == prepared["package_sha256"]
        receipt = _finish(fixture, tmp_path / "state", str(prepared["invocation_id"]))
        assert receipt["byte_count"] == expected.stdout.encode().__len__()
        assert receipt["chunk_count"] == prepared["chunk_count"]


def test_read_and_usage_failures_preserve_public_process_semantics(
    fixture: HarnessFixture, tmp_path: Path
) -> None:
    expected = _direct_work(fixture, fixture.invalid)
    assert expected.returncode == 2
    partial = _invoke(
        fixture,
        tmp_path / "partial",
        "prepare",
        fixture.ref(fixture.invalid),
        "--deadline-ms",
        "180000",
    )
    assert partial.returncode == 2
    assert partial.stdout == expected.stdout
    assert "INVALID-READABLE-PREFIX" in partial.stdout
    missing = _invoke(
        fixture,
        tmp_path / "missing",
        "prepare",
        fixture.ref(999_999_999),
        "--deadline-ms",
        "180000",
    )
    direct_missing = _direct_work(fixture, 999_999_999)
    assert missing.returncode == 3
    assert direct_missing.returncode == 3
    assert missing.stdout == ""
    malformed = _invoke(
        fixture, tmp_path / "usage", "prepare", fixture.repo, "--deadline-ms", "180000"
    )
    assert malformed.returncode == 2
    assert malformed.stdout == ""


def test_prepare_emits_only_bounded_metadata_before_chunks(tmp_path: Path) -> None:
    state = tmp_path / "state"
    environment = {**os.environ, "ITREE_HARNESS_WORK_ROOT": str(state)}
    result = subprocess.run(
        [
            str(SOURCE / ".venv" / "bin" / "itree"),
            "harness-work",
            "prepare",
            "dzackgarza/itree#68",
            "--deadline-ms",
            "180000",
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=240,
    )
    assert result.returncode == 0, result.stderr
    invocation = json.loads(result.stdout)["invocation_id"]
    cleaned = subprocess.run(
        ["trash", str(state / invocation)], capture_output=True, check=False, timeout=30
    )
    assert cleaned.returncode == 0
    assert len(result.stdout.encode()) < 20_000
    assert "Work package complete" not in result.stdout


def test_invalid_deadline_is_a_usage_failure(tmp_path: Path) -> None:
    environment = {**os.environ, "ITREE_HARNESS_WORK_ROOT": str(tmp_path / "state")}
    result = subprocess.run(
        [
            str(SOURCE / ".venv" / "bin" / "itree"),
            "harness-work",
            "prepare",
            "dzackgarza/itree#68",
            "--deadline-ms",
            "0",
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["kind"] == "usage_failure"


def test_manifest_identity_must_match_requested_invocation(tmp_path: Path) -> None:
    state = tmp_path / "state"
    environment = {**os.environ, "ITREE_HARNESS_WORK_ROOT": str(state)}
    prepared = subprocess.run(
        [
            str(SOURCE / ".venv" / "bin" / "itree"),
            "harness-work",
            "prepare",
            "dzackgarza/itree#68",
            "--deadline-ms",
            "180000",
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=240,
    )
    assert prepared.returncode == 0, prepared.stderr
    actual = json.loads(prepared.stdout)["invocation_id"]
    requested = "b" * 32
    assert requested != actual
    state.joinpath(actual).rename(state / requested)

    result = subprocess.run(
        [
            str(SOURCE / ".venv" / "bin" / "itree"),
            "harness-work",
            "next",
            requested,
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode != 0
    assert result.stdout == ""


def test_modified_and_premature_streams_have_no_receipt(
    fixture: HarnessFixture, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    prepared = _prepare(fixture, state, fixture.small)
    invocation = str(prepared["invocation_id"])
    premature = _invoke(fixture, state, "finish", invocation)
    assert premature.returncode != 0
    chunk = state / invocation / "chunks" / "000000.md"
    chunk.write_text(" altered", encoding="utf-8")
    modified = _invoke(fixture, state, "next", invocation)
    assert modified.returncode != 0
    replay = _invoke(fixture, state, "next", invocation)
    assert replay.returncode != 0
    assert not state.joinpath(invocation).exists()


@dataclass(frozen=True)
class InstallerFixture:
    project: Path
    external: Path
    trace: Path


def _installer_fixture(root: Path) -> InstallerFixture:
    project = root / "project"
    external = root / "external"
    project.joinpath(".specify", "integrations").mkdir(parents=True)
    external.joinpath("skills", "user-existing").mkdir(parents=True)
    external.joinpath("skills", "user-existing", "SKILL.md").write_text(
        "USER-EXTERNAL-SKILL\n"
    )
    project.joinpath("AGENTS.md").write_text("PROJECT-INSTRUCTIONS\n")
    project.joinpath("CLAUDE.md").write_text("CLAUDE-INSTRUCTIONS\n")
    project.joinpath(".claude").mkdir()
    project.joinpath(".claude", "settings.local.json").write_text('{"local": true}\n')
    project.joinpath(".claude", "settings.json").write_text(
        json.dumps(
            {
                "custom": {"preserve": 7},
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "matcher": "user",
                            "hooks": [{"type": "command", "command": "user-hook"}],
                        }
                    ]
                },
            },
            indent=2,
        )
        + "\n"
    )
    project.joinpath(".codex").mkdir()
    project.joinpath(".codex", "config.toml").write_text(
        'model = "retained-model"\n[custom]\npreserve = true\n'
    )
    project.joinpath("opencode.json").write_text(
        json.dumps({"custom": "retained", "$schema": "custom-schema"}) + "\n"
    )
    integration = {
        "version": "1.4.4",
        "integration_state_schema": 1,
        "installed_integrations": ["none"],
        "integration_settings": {"none": {"script": "sh", "invoke_separator": "."}},
        "integration": "none",
        "default_integration": "none",
    }
    project.joinpath(".specify", "integration.json").write_text(
        json.dumps(integration, indent=2) + "\n"
    )
    project.joinpath(".specify", "init-options.json").write_text(
        json.dumps(
            {
                "ai": "none",
                "integration": "none",
                "script": "sh",
                "speckit_version": "1.4.4",
            }
        )
        + "\n"
    )
    project.joinpath(".agents").symlink_to(external, target_is_directory=True)
    return InstallerFixture(project, external, root / "install-trace.jsonl")


def _install(fixture: InstallerFixture) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["ITREE_HARNESS_INSTALL_TRACE"] = str(fixture.trace)
    return subprocess.run(
        [str(SOURCE / ".venv" / "bin" / "itree"), "harness"],
        cwd=fixture.project,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=300,
    )


def _snapshot(
    root: Path, ignored: tuple[Path, ...] = ()
) -> dict[str, tuple[str, int, str | None, int]]:
    values: dict[str, tuple[str, int, str | None, int]] = {}
    for path in sorted(root.rglob("*")):
        if any(path == excluded or excluded in path.parents for excluded in ignored):
            continue
        relative = str(path.relative_to(root))
        if path.is_symlink():
            values[relative] = ("symlink", 0, os.readlink(path), path.lstat().st_mode)
        elif path.is_file():
            values[relative] = (
                "file",
                path.stat().st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
                path.stat().st_mode,
            )
        else:
            values[relative] = ("directory", 0, None, path.stat().st_mode)
    return values


def _trace_operations(trace: Path) -> list[str]:
    return [json.loads(line)["operation"] for line in trace.read_text().splitlines()]


def test_real_linked_install_preserves_and_repeats_every_surface(
    tmp_path: Path,
) -> None:
    fixture = _installer_fixture(tmp_path)
    preserved = {
        "AGENTS.md": fixture.project.joinpath("AGENTS.md").read_bytes(),
        "CLAUDE.md": fixture.project.joinpath("CLAUDE.md").read_bytes(),
        ".claude/settings.local.json": fixture.project.joinpath(
            ".claude", "settings.local.json"
        ).read_bytes(),
    }
    first = _install(fixture)
    assert first.returncode == 0, first.stdout + first.stderr
    for harness, relative in {
        "claude": ".claude/skills/speckit-itree-work/SKILL.md",
        "codex": ".agents/skills/speckit-itree-work/SKILL.md",
        "opencode": ".opencode/commands/speckit.itree.work.md",
    }.items():
        path = fixture.project / relative
        assert path.is_file(), (harness, path)
        content = path.read_text()
        assert "$ARGUMENTS" in content
        assert "prepare" in content and "next" in content and "finish" in content
    assert fixture.external.joinpath(
        "skills", "speckit-itree-work", "SKILL.md"
    ).is_file()
    assert (
        fixture.external.joinpath("skills", "user-existing", "SKILL.md").read_text()
        == "USER-EXTERNAL-SKILL\n"
    )
    for relative in (
        ".codex/config.toml",
        ".specify/integration.json",
        ".specify/integrations/codex.manifest.json",
        ".specify/events.py",
        ".specify/extensions.yml",
        ".opencode/plugin/speckit-events.ts",
    ):
        assert fixture.project.joinpath(relative).is_file(), relative
    for relative, expected in preserved.items():
        assert fixture.project.joinpath(relative).read_bytes() == expected
    claude = json.loads(
        fixture.project.joinpath(".claude", "settings.json").read_text()
    )
    assert claude["custom"] == {"preserve": 7}
    assert claude["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] == "user-hook"
    codex = fixture.project.joinpath(".codex", "config.toml").read_text()
    assert 'model = "retained-model"' in codex
    assert "[custom]" in codex and "speckit" in codex
    opencode = json.loads(fixture.project.joinpath("opencode.json").read_text())
    assert opencode["custom"] == "retained"
    assert "speckit-events" in json.dumps(opencode)
    integration = json.loads(
        fixture.project.joinpath(".specify", "integration.json").read_text()
    )
    assert integration["default_integration"] == "none"
    assert {"none", "claude", "codex", "opencode"} <= set(
        integration["installed_integrations"]
    )
    assert not fixture.project.joinpath(".specify", "feature.json").exists()
    assert not fixture.project.joinpath(
        ".specify", "extensions", "agent-context"
    ).exists()
    assert not list(fixture.project.glob(".specify/presets/**"))
    operations = _trace_operations(fixture.trace)
    assert operations.index("register-itree") < operations.index("refresh-events")
    first_project = _snapshot(fixture.project, (fixture.trace,))
    first_external = _snapshot(fixture.external)
    second = _install(fixture)
    assert second.returncode == 0, second.stdout + second.stderr
    assert _snapshot(fixture.project, (fixture.trace,)) == first_project
    assert _snapshot(fixture.external) == first_external
    second_operations = _trace_operations(fixture.trace)
    assert second_operations.index(
        "register-itree", len(operations)
    ) < second_operations.index("refresh-events", len(operations))


def test_modified_owned_artifact_stops_before_any_write(tmp_path: Path) -> None:
    fixture = _installer_fixture(tmp_path)
    first = _install(fixture)
    assert first.returncode == 0, first.stdout + first.stderr
    command = fixture.project / ".claude" / "skills" / "speckit-itree-work" / "SKILL.md"
    command.write_text(command.read_text() + "LOCAL-MODIFICATION\n")
    before = _snapshot(fixture.project)
    external_before = _snapshot(fixture.external)
    result = _install(fixture)
    assert result.returncode != 0
    assert _snapshot(fixture.project) == before
    assert _snapshot(fixture.external) == external_before


def test_midwrite_collision_reports_actual_prefix_and_untouched_suffix(
    tmp_path: Path,
) -> None:
    fixture = _installer_fixture(tmp_path)
    collision = (
        fixture.project / ".specify" / "extensions" / "itree" / "commands" / "work.md"
    )

    def interfere() -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if fixture.trace.exists():
                lines = fixture.trace.read_text().splitlines()
                if any(
                    (
                        lambda record: (
                            record["operation"] == "register-itree"
                            and record["status"] == "started"
                        )
                    )(json.loads(line))
                    for line in lines
                ):
                    collision.parent.mkdir(parents=True, exist_ok=True)
                    collision.write_text("REAL-MIDWRITE-COLLISION\n")
                    return
            time.sleep(0.001)
        raise AssertionError(
            "registration did not start before the synchronization deadline"
        )

    thread = threading.Thread(target=interfere)
    thread.start()
    result = _install(fixture)
    thread.join(timeout=35)
    assert not thread.is_alive()
    assert result.returncode != 0
    outcome = json.loads(result.stdout)
    assert outcome["kind"] == "partial"
    effects = outcome["outcome"]
    assert [item["operation_id"] for item in effects["confirmed"]] == [
        "stage-codex-core",
        "install-claude",
        "install-opencode",
        "merge-codex-core",
        "select-codex",
    ]
    assert effects["failed"]["operation_id"] == "register-itree"
    assert [item["operation_id"] for item in effects["untouched"]] == [
        "refresh-events",
        "render-claude",
        "render-codex",
        "render-opencode",
        "restore-default",
        "confirm",
    ]
    assert collision.read_text() == "REAL-MIDWRITE-COLLISION\n"
    assert not fixture.project.joinpath(
        ".claude", "skills", "speckit-itree-work", "SKILL.md"
    ).exists()
    assert not fixture.project.joinpath(
        ".opencode", "commands", "speckit.itree.work.md"
    ).exists()
    assert not fixture.project.joinpath(".specify", "events.py").exists()


def test_unsafe_linked_target_fails_before_writes(tmp_path: Path) -> None:
    fixture = _installer_fixture(tmp_path)
    missing = tmp_path / "missing-target"
    fixture.project.joinpath(".agents").unlink()
    fixture.project.joinpath(".agents").symlink_to(missing, target_is_directory=True)
    before = _snapshot(fixture.project)
    external_before = _snapshot(fixture.external)
    result = _install(fixture)
    assert result.returncode == 2
    assert _snapshot(fixture.project) == before
    assert _snapshot(fixture.external) == external_before
    assert not missing.exists()
    assert not fixture.project.joinpath(".specify", "extensions.yml").exists()


class _SlowHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        time.sleep(15)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"SLOW-SOURCE-COMPLETE")


def test_real_slow_endpoint_is_terminated_and_reaped(
    fixture: HarnessFixture, tmp_path: Path
) -> None:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SlowHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    body = f"SLOW-ORIGINAL\nhttp://127.0.0.1:{server.server_port}/slow.txt\n"
    _execute(
        fixture,
        "slow-body",
        [
            "gh",
            "issue",
            "edit",
            str(fixture.slow),
            "--repo",
            fixture.repo,
            "--body",
            body,
        ],
    )
    result = _invoke(
        fixture,
        tmp_path / "state",
        "prepare",
        fixture.ref(fixture.slow),
        "--deadline-ms",
        "600",
    )
    from itree.harness_work import WatchdogStatus
    from tests.test_harness_acceptance import external_watchdog

    watchdog_outcome, watchdog_stdout, watchdog_stderr = external_watchdog(
        [
            str(
                SOURCE
                / "src"
                / "itree"
                / "resources"
                / "speckit_itree"
                / "scripts"
                / "python"
                / "work_loader.py"
            ),
            "prepare",
            fixture.ref(fixture.slow),
            "--deadline-ms",
            "180000",
        ],
        fixture.root,
        {**os.environ, "ITREE_HARNESS_WORK_ROOT": str(tmp_path / "external-state")},
        100,
    )
    tmp_path.joinpath("external-watchdog.json").write_text(
        json.dumps(
            {
                "outcome": watchdog_outcome.model_dump(mode="json"),
                "stdout": watchdog_stdout,
                "stderr": watchdog_stderr,
            }
        )
        + "\n"
    )
    server.shutdown()
    thread.join()
    server.server_close()
    assert watchdog_outcome.status == WatchdogStatus.forced_termination
    assert watchdog_outcome.reaped is True
    assert watchdog_outcome.remaining_children == ()
    assert watchdog_outcome.signals == (int(signal.SIGTERM), int(signal.SIGKILL))
    assert watchdog_outcome.external_elapsed_seconds > 0
    assert watchdog_stdout == ""
    assert result.returncode == 4
    process_files = list((tmp_path / "state").glob("*/process.json"))
    assert len(process_files) == 1
    process = json.loads(process_files[0].read_text())
    assert process["kind"] == "timed_out"
    assert process["cleanup"]["reaped"] is True
    assert process["cleanup"]["terminate_requested"] is True
    assert process["cleanup"]["final_returncode"] != 0
    assert process["wall_seconds"] > 0
    assert result.stdout == ""


def test_packaged_extension_has_one_shared_upstream_surface() -> None:
    resource = SOURCE / "src" / "itree" / "resources" / "speckit_itree"
    manifest = yaml.safe_load((resource / "extension.yml").read_text())
    assert manifest["schema_version"] == "1.0"
    assert manifest["extension"]["id"] == "itree"
    assert [command["name"] for command in manifest["provides"]["commands"]] == [
        "speckit.itree.work",
        "speckit.itree.initialize",
    ]
    assert list(manifest["events"]) == ["session_start"]
    assert "hooks" not in manifest
    assert not list(resource.rglob("*preset*"))
    work = (resource / "commands" / "work.md").read_text()
    assert "$ARGUMENTS" in work
    assert "{SCRIPT}" in work
    assert "prepare" in work and "next" in work and "finish" in work
    assert "emission-only" in work
    initialization = (resource / "scripts" / "python" / "initialize.py").read_text()
    assert "work_loader" not in initialization


def test_canonical_manifest_hash_omits_self_and_verifies_artifacts() -> None:
    from itree.harness import (
        EvidenceManifest,
        canonical_manifest_bytes,
        seal_manifest,
        verify_manifest_directory,
    )

    root = SOURCE / "tests" / "fixtures" / "harness_types" / "evidence_run"
    manifest = EvidenceManifest.model_validate_json(
        (root.parent / "golden_manifest.json").read_bytes()
    )
    expected = (root.parent / "canonical_manifest.json").read_bytes()

    assert canonical_manifest_bytes(manifest) == expected
    assert (
        hashlib.sha256(expected).hexdigest()
        == (root / "manifest.sha256").read_text().strip()
    )
    assert (
        seal_manifest(manifest).manifest_sha256.root
        == (root / "manifest.sha256").read_text().strip()
    )
    assert verify_manifest_directory(root) == manifest


def test_external_watchdog_kills_nested_process_group_and_reaps() -> None:
    from itree.harness_work import WatchdogStatus
    from tests.test_harness_acceptance import external_watchdog

    outcome, stdout, stderr = external_watchdog(
        [
            sys.executable,
            str(
                SOURCE / "tests" / "fixtures" / "harness_types" / "stubborn_process.py"
            ),
        ],
        SOURCE,
        os.environ.copy(),
        100,
    )

    assert outcome.status == WatchdogStatus.forced_termination, stdout + stderr
    assert int(signal.SIGTERM) in outcome.signals
    assert int(signal.SIGKILL) in outcome.signals
    assert outcome.reaped is True
    assert outcome.remaining_children == ()
    assert outcome.external_elapsed_seconds < 10
    assert outcome.leader_returncode != 0


def test_illegal_work_and_evidence_states_are_static_errors() -> None:
    result = subprocess.run(
        ["uv", "run", "--with", "mypy", "mypy", "--strict", str(TYPE_FIXTURE)],
        cwd=SOURCE,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    codes = [
        line.rsplit("[", maxsplit=1)[1].removesuffix("]")
        for line in result.stdout.splitlines()
        if TYPE_FIXTURE.name in line and ": error:" in line
    ]
    assert sorted(codes) == sorted(
        ["arg-type", "call-arg", "arg-type", "assignment", "call-arg"]
    ), result.stdout
