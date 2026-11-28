from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

import fcntl
import pytest
from itree.models import FixtureState
from pydantic import BaseModel, ConfigDict, TypeAdapter


def _worker(
    subject: pytest.Config
    | pytest.FixtureRequest
    | pytest.Session
    | pytest.Item
    | pytest.TestReport,
) -> bool:
    if isinstance(subject, pytest.TestReport):
        return os.environ.get("PYTEST_XDIST_WORKER") is not None
    if isinstance(subject, pytest.Session):
        return hasattr(subject.config, "workerinput")
    if isinstance(subject, pytest.Config):
        return hasattr(subject, "workerinput")
    return hasattr(subject.config, "workerinput")


def _controller_root(
    _config: pytest.Config
    | pytest.FixtureRequest
    | pytest.Session
    | pytest.Item
    | pytest.TestReport,
) -> Path:
    configured = os.environ.get("ITREE_WORK_CONTROLLER_ROOT")
    assert configured is not None, "the parent must provide one controller namespace"
    root = Path(configured)
    assert root.is_absolute(), root
    return root


def _harness_only_selection(config: pytest.Config) -> bool:
    harness_files = {"test_harness.py", "test_harness_acceptance.py"}
    selected = [argument.split("::", maxsplit=1)[0] for argument in config.args]
    return bool(selected) and all(
        Path(argument).name in harness_files for argument in selected
    )


def pytest_configure(config: pytest.Config) -> None:
    if _worker(config):
        root = _controller_root(config)
        owner = json.loads(root.joinpath("owner.json").read_text())
        assert root.is_dir() and owner["root"] == str(root), owner
        return
    configured = os.environ.get("ITREE_WORK_CONTROLLER_ROOT")
    if configured is not None:
        root = Path(configured)
        root.mkdir(parents=True, exist_ok=False)
    else:
        root = Path(
            tempfile.mkdtemp(prefix="itree-work-controller-", dir="/tmp/opencode")
        )
    os.environ["ITREE_WORK_CONTROLLER_ROOT"] = str(root)
    root.joinpath("owner.json").write_text(
        json.dumps({"root": str(root), "pid": os.getpid()}, sort_keys=True)
    )
    if _harness_only_selection(config):
        return
    if os.environ.get("ITREE_WORK_FIXTURE_STATE") is None:
        root.joinpath("creation-started").write_text(str(time.time()))
        root.joinpath("creation-started-monotonic").write_text(str(time.perf_counter()))
        started = time.perf_counter()
        parent = Path(tempfile.mkdtemp(prefix="itree-work-test-", dir="/tmp/opencode"))
        result = subprocess.run(
            [
                sys.executable,
                "tests/fixtures/work-package/setup.py",
                "create",
                "--root",
                str(parent / "acceptance"),
                "--output",
                str(parent / "state.json"),
            ],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
            check=False,
            timeout=1200,
        )
        root.joinpath("setup.stdout.log").write_text(result.stdout)
        root.joinpath("setup.stderr.log").write_text(result.stderr)
        root.joinpath("setup-returncode").write_text(str(result.returncode))
        assert result.returncode == 0, result.stdout + result.stderr
        assert (parent / "state.json").exists(), parent
        root.joinpath("owns-state").write_text(str(parent))
        root.joinpath("fixture-operation-started").write_text(str(time.time()))
        root.joinpath("setup-seconds").write_text(str(time.perf_counter() - started))
        os.environ["ITREE_WORK_FIXTURE_STATE"] = str(parent / "state.json")
        os.environ["ITREE_TEST_STARTED_AT"] = root.joinpath(
            "creation-started"
        ).read_text()
        os.environ["ITREE_TEST_SETUP_SECONDS"] = root.joinpath(
            "setup-seconds"
        ).read_text()


def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    root = _controller_root(config)
    readonly = sum(item.get_closest_marker("serial") is None for item in items)
    serial = len(items) - readonly
    worker = os.environ.get("PYTEST_XDIST_WORKER", "controller")
    record = {
        "worker": worker,
        "pid": os.getpid(),
        "root": str(root),
        "expected_cases": len(items),
        "expected_readonly": readonly,
        "expected_serial": serial,
    }
    with _exclusive(root / "collection.lock"):
        root.joinpath("collection").mkdir(exist_ok=True)
        root.joinpath("collection", f"{worker}.json").write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n"
        )
        root.joinpath("expected-readonly").write_text(str(readonly))
        root.joinpath("expected-serial").write_text(str(serial))
        root.joinpath("expected-cases").write_text(str(len(items)))


@contextmanager
def _exclusive(path: Path) -> Iterator[IO[bytes]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield handle
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _emit(root: Path, kind: str, identity: str) -> None:
    destination = (
        root / kind / (hashlib.sha256(identity.encode()).hexdigest() + ".json")
    )
    with _exclusive(destination.with_suffix(".lock")) as _:
        destination.write_text(identity)


def _record(root: Path, kind: str) -> int:
    directory = root / kind
    return len(tuple(directory.glob("*.json"))) if directory.exists() else 0


def _stamp(root: Path, name: str, value: str) -> None:
    destination = root / name
    with _exclusive(destination.with_suffix(".lock")) as _:
        if not destination.exists():
            destination.write_text(value)


def _finished(report: pytest.TestReport) -> None:
    if report.when != "call":
        return
    root = _controller_root(report)
    _emit(root, "workers", os.environ["PYTEST_XDIST_WORKER"])
    category = "serial" if "serial" in report.keywords else "readonly"
    _emit(root, f"done-{category}", report.nodeid)
    _emit(root, f"outcome-{category}-{report.outcome}", report.nodeid)
    if report.outcome != "passed":
        _emit(root, "failures", report.nodeid)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if _worker(report):
        _finished(report)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, None, None]:
    if _worker(item.config):
        root: Path = _controller_root(item.config)
        _stamp(root, "case-started", str(time.time()))
        worker = os.environ["PYTEST_XDIST_WORKER"]
        readiness = root / "worker-ready" / f"{worker}.json"
        with _exclusive(readiness.with_suffix(".lock")):
            readiness.write_text(
                json.dumps(
                    {
                        "worker": worker,
                        "pid": os.getpid(),
                        "root": str(root),
                    },
                    sort_keys=True,
                )
            )
        expected_workers = int(os.environ["PYTEST_XDIST_WORKER_COUNT"])
        started = time.perf_counter()
        while _record(root, "worker-ready") != expected_workers:
            if time.perf_counter() - started > 120:
                root.joinpath("worker-start-timeout").write_text(item.nodeid)
                raise RuntimeError(
                    "real worker readiness failed before case execution: "
                    f"{_record(root, 'worker-ready')}/{expected_workers}"
                )
            time.sleep(0.025)
        _stamp(root, "workers-ready", str(time.time()))
    if _worker(item.config) and item.get_closest_marker("serial"):
        expected = int(root.joinpath("expected-readonly").read_text())
        if _record(root, "failures"):
            raise RuntimeError(
                f"read-only case failed before serialized work {item.nodeid}; "
                "the fixture is preserved"
            )
        with _exclusive(root / "serial.lock") as _:
            started = time.perf_counter()
            while _record(root, "done-readonly") != expected:
                if _record(root, "failures"):
                    raise RuntimeError(
                        f"read-only case failed before serialized work {item.nodeid}; "
                        "the fixture is preserved"
                    )
                if time.perf_counter() - started > 600:
                    root.joinpath("serial-timeout").write_text(item.nodeid)
                    raise RuntimeError(
                        f"read-only cases did not finish for {item.nodeid}: "
                        f"{_record(root, 'done-readonly')}/{expected}"
                    )
                time.sleep(0.025)
    yield


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not _worker(session):
        root: Path = _controller_root(session.config)
        if not root.joinpath("owns-state").exists():
            root.joinpath("cleanup-blocked").write_text(
                "No controller-owned complete fixture; no automatic cleanup."
            )
            return
        configured = os.environ.get("ITREE_WORK_FIXTURE_STATE")
        assert configured is not None
        state = FixtureState.model_validate_json(Path(configured).read_text())
        expected_cases = int(root.joinpath("expected-cases").read_text())
        expected_serial = int(root.joinpath("expected-serial").read_text())
        actual_serial = _record(root, "done-serial")
        actual_readonly = _record(root, "done-readonly")
        expected_readonly = int(root.joinpath("expected-readonly").read_text())
        expected_workers = int(session.config.option.numprocesses or 0)
        case_checks = {
            "exitstatus": exitstatus,
            "expected_cases": expected_cases,
            "expected_serial": expected_serial,
            "actual_serial": actual_serial,
            "expected_readonly": expected_readonly,
            "actual_readonly": actual_readonly,
            "workers": _record(root, "workers"),
            "expected_workers": expected_workers,
            "ready_workers": _record(root, "worker-ready"),
            "controller_root": str(root),
            "journal": str(_journal_path(state.root)),
        }
        root.joinpath("verification.json").write_text(json.dumps(case_checks, indent=2))
        if (
            exitstatus != 0
            or actual_serial + actual_readonly != expected_cases
            or actual_serial != expected_serial
            or actual_readonly != expected_readonly
            or _record(root, "workers") != expected_workers
            or _record(root, "worker-ready") != expected_workers
            or _record(root, "failures") != 0
        ):
            _emit(root, "incomplete-cleanup", json.dumps(case_checks))
            return
        case_started = float(root.joinpath("case-started").read_text())
        case_finished = time.time()
        case_logs = {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted((state.root / "logs").iterdir())
            if path.is_file()
        }
        root.joinpath("case-logs.json").write_text(
            json.dumps(case_logs, indent=2, sort_keys=True)
        )
        live_verification = _verify_live_fixture(state)
        root.joinpath("live-verification.json").write_text(
            json.dumps(live_verification, indent=2, sort_keys=True) + "\n"
        )
        deletion_started = time.perf_counter()
        deletion = subprocess.run(
            ["gh", "repo", "delete", state.repo, "--yes"],
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
        remote_delete_seconds = time.perf_counter() - deletion_started
        root.joinpath("remote-delete.json").write_text(
            json.dumps(
                {
                    "exit": deletion.returncode,
                    "stdout": deletion.stdout,
                    "stderr": deletion.stderr,
                    "elapsed_seconds": remote_delete_seconds,
                },
                indent=2,
                sort_keys=True,
            )
        )
        assert deletion.returncode == 0, deletion.stderr
        absence_started = time.perf_counter()
        absent = subprocess.run(
            ["gh", "repo", "view", state.repo],
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
        absence_seconds = time.perf_counter() - absence_started
        assert absent.returncode != 0
        scratch_started = time.perf_counter()
        trashed = subprocess.run(
            ["trash", str(state.root.parent)],
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
        scratch_seconds = time.perf_counter() - scratch_started
        assert trashed.returncode == 0, trashed.stderr
        assert not state.root.parent.exists()
        finished = time.perf_counter()
        origin = float(root.joinpath("creation-started-monotonic").read_text())
        timings = {
            "setup_seconds": state.setup_elapsed_seconds,
            "case_seconds": case_finished - case_started,
            "cleanup_seconds": finished - deletion_started,
            "total_seconds": finished - origin,
            "setup_returncode": 0,
            "remote_delete_seconds": remote_delete_seconds,
            "remote_delete_exit": deletion.returncode,
            "absence_verification_seconds": absence_seconds,
            "remote_absence_exit": absent.returncode,
            "scratch_trash_seconds": scratch_seconds,
            "scratch_trash_exit": trashed.returncode,
            "raw_record": str(root / "raw-timings.json"),
            "case_logs": case_logs,
        }
        root.joinpath("raw-timings.json").write_text(
            json.dumps(timings, indent=2, sort_keys=True)
        )
        final = {
            **timings,
            "case_logs": str(root / "case-logs.json"),
            "journal": str(_journal_path(state.root)),
            "live_objects_verified": True,
            "live_verification": live_verification,
            "families": _family_costs(state.root),
        }
        total_seconds = finished - origin
        timings["total_seconds"] = total_seconds
        root.joinpath("teardown-timings.json").write_text(
            json.dumps(final, indent=2, sort_keys=True)
        )
        assert total_seconds >= 0, final
        assert total_seconds < 300, final


class _LiveRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _LivePageInfo(_LiveRecord):
    hasNextPage: bool
    endCursor: str | None


class _LiveComment(_LiveRecord):
    id: str
    databaseId: int
    body: str
    url: str


class _LiveComments(_LiveRecord):
    totalCount: int
    nodes: list[_LiveComment]
    pageInfo: _LivePageInfo


class _LiveParent(_LiveRecord):
    number: int


class _LiveIssue(_LiveRecord):
    id: str
    number: int
    body: str
    parent: _LiveParent | None
    comments: _LiveComments


class _LiveCommentPage(_LiveRecord):
    comments: _LiveComments


class _LiveIssues(_LiveRecord):
    totalCount: int
    nodes: list[_LiveIssue]


class _LivePullRequest(_LiveRecord):
    id: str
    number: int


class _LivePullRequests(_LiveRecord):
    totalCount: int
    nodes: list[_LivePullRequest]


class _LiveRefTarget(_LiveRecord):
    oid: str


class _LiveRef(_LiveRecord):
    id: str
    name: str
    prefix: str
    target: _LiveRefTarget


class _LiveRefs(_LiveRecord):
    totalCount: int
    nodes: list[_LiveRef]


class _LiveRepository(_LiveRecord):
    issues: _LiveIssues
    pullRequests: _LivePullRequests
    refs: _LiveRefs


class _LiveRepositoryPage(_LiveRecord):
    issue: _LiveCommentPage


class _LiveData(_LiveRecord):
    repository: _LiveRepository


class _LiveDataPage(_LiveRecord):
    repository: _LiveRepositoryPage


class _LiveResponse(_LiveRecord):
    data: _LiveData


class _LiveResponsePage(_LiveRecord):
    data: _LiveDataPage


def _graphql(query: str) -> str:
    result = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}"],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def _verify_live_fixture(state: FixtureState) -> dict[str, bool | int]:
    rows = [
        json.loads(line) for line in _journal_path(state.root).read_text().splitlines()
    ]
    expected_issues = {
        str(obj["id"])
        for row in rows
        if row["family"] == "createIssue"
        for obj in row["result"]["objects"].values()
    }
    assert len(expected_issues) == 15
    expected_pr = {
        str(obj["id"])
        for row in rows
        if row["family"] == "createPullRequest"
        for obj in row["result"]["objects"].values()
    }
    assert len(expected_pr) == 1
    expected_comments = {
        str(obj["id"]): str(obj["body"])
        for row in rows
        if row["family"] == "addComment"
        for obj in row["result"]["objects"].values()
    }
    assert len(expected_comments) == 119
    fresh = json.loads(state.root.joinpath("T019-new-comment.json").read_text())
    expected_comments[fresh["node_id"]] = fresh["body"]
    expected_refs = {
        str(obj["id"])
        for row in rows
        if row["family"] == "createRef"
        for obj in row["result"]["objects"].values()
    }
    assert len(expected_refs) == 11
    expected_tags = {
        str(obj["id"])
        for row in rows
        if row["family"] == "createGitTag"
        for obj in row["result"]["objects"].values()
    }
    assert len(expected_tags) == 3
    owner, name = state.repo.split("/", 1)
    query = """query VerifyFixture {
      repository(owner:%s,name:%s) {
        issues(first:100) {
          totalCount
          nodes {
            id number body
            parent { number }
            comments(first:100) {
              totalCount
              nodes { id databaseId body url }
              pageInfo { hasNextPage endCursor }
            }
          }
        }
        pullRequests(first:10) { totalCount nodes { id number } }
        refs(first:100,refPrefix:"refs/") {
          totalCount
          nodes { id name prefix target { oid } }
        }
      }
    }""" % (json.dumps(owner), json.dumps(name))
    repository = (
        TypeAdapter(_LiveResponse).validate_json(_graphql(query)).data.repository
    )
    issues = repository.issues.nodes
    assert repository.issues.totalCount == 15
    assert {issue.id for issue in issues} == expected_issues
    assert repository.pullRequests.totalCount == 1
    assert {obj.id for obj in repository.pullRequests.nodes} == expected_pr
    for issue_number, body in (
        (state.selected, state.selected_body),
        (state.roadmap, state.roadmap_body),
    ):
        issue = next(obj for obj in issues if obj.number == issue_number)
        assert issue.body == body, issue
    parents = {
        issue.number: issue.parent.number
        for issue in issues
        if issue.parent is not None
    }
    assert parents == {state.selected: state.roadmap, state.cycle_a: state.cycle_b}
    comments: dict[str, _LiveComment] = {}
    issue_nodes: dict[int, list[_LiveComment]] = {}
    for issue in issues:
        page = issue.comments
        nodes = list(page.nodes)
        while page.pageInfo.hasNextPage:
            cursor = json.dumps(page.pageInfo.endCursor)
            continuation = (
                TypeAdapter(_LiveResponsePage)
                .validate_json(
                    _graphql(
                        """query MoreComments {
                      repository(owner:%s,name:%s) {
                        issue(number:%d) {
                          comments(first:100,after:%s) {
                            totalCount
                            nodes { id databaseId body url }
                            pageInfo { hasNextPage endCursor }
                          }
                        }
                      }
                    }"""
                        % (json.dumps(owner), json.dumps(name), issue.number, cursor)
                    )
                )
                .data.repository.issue
            )
            page = continuation.comments
            nodes.extend(page.nodes)
        assert page.totalCount == len(nodes)
        issue_nodes[issue.number] = nodes
        for comment in nodes:
            assert comment.id not in comments, comment
            comments[comment.id] = comment
    assert set(comments) == set(expected_comments)
    assert all(
        comments[identifier].body == body
        for identifier, body in expected_comments.items()
    )
    ordered = issue_nodes[state.pagination]
    assert [comment.body for comment in ordered] == state.pagination_comments
    assert [comment.databaseId for comment in ordered] == state.pagination_ids
    required_refs = {obj.id: obj for obj in repository.refs.nodes}
    assert set(expected_refs) <= set(required_refs)
    annotated = {
        obj.target.oid
        for obj in required_refs.values()
        if obj.target.oid in expected_tags
    }
    assert annotated == expected_tags
    return {
        "verified": True,
        "issues": 15,
        "pull_requests": 1,
        "initial_comments": 119,
        "mutation_comments": len(expected_comments) - 119,
        "comments": len(comments),
        "ordered_comments": len(ordered),
        "parent_links": len(parents),
        "required_refs": len(expected_refs),
        "annotated_tags": len(annotated),
    }


def _family_costs(root: Path) -> dict[str, dict[str, int | float]]:
    families: dict[str, dict[str, int | float]] = {}
    for line in _journal_path(root).read_text().splitlines():
        record = json.loads(line)
        family = families.setdefault(
            record["family"],
            {"requests": 0, "objects": 0, "api_seconds": 0.0, "pacing_seconds": 0.0},
        )
        family["requests"] += 1
        family["objects"] += record["object_count"]
        family["api_seconds"] += record["api_seconds"]
        family["pacing_seconds"] += record["pacing_seconds"]
    return families


def _journal_path(root: Path) -> Path:
    parent = root.parent
    if root.name != "acceptance":
        return root / "family-journal.jsonl"
    return parent.parent / f"{parent.name}-family-journal.jsonl"
