from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
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
    server.shutdown()
    thread.join()
    server.server_close()
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
