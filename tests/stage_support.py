from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from pydantic import JsonValue

SLUG = "owner/repo"
ISSUE_NUMBER = 71
ISSUE_REF = f"{SLUG}#{ISSUE_NUMBER}"
ISSUE_NODE_ID = "I_stage_node_71"
COMMIT_SHA = "6f704600024a6e1a759b1d94c742262656a47930"
TASKS = """<!-- itree-task-document:v1:start -->
- [x] T001 id = "T001"
    order = 0
    prerequisites = []
    requirements = ["R71-10"]
    evidence = {"resolved":[{"kind":"documented_test","command":"uv run --locked pytest tests/test_stage_tasks.py","report":{"repository":"dzackgarza/itree","issue_number":71,"attempt_id":"A-T015","report_id":"v1-box-and-fields"}}]}
    goals = [{"id":"NS-002","rationale":"preserve a completed task for handoff"}]
    description = "Create v1 tasks in src/itree/stage.py"
    acceptance = "Its checkbox and fields persist."
- [ ] T003 id = "T003"
    order = 1
    prerequisites = ["T001", "OWNER/B-REPO#2:T001"]
    requirements = ["R71-10", "R71-03"]
    evidence = {"pending":"record the public task merge test receipt"}
    goals = [{"id":"NS-002","rationale":"preserve the task for a new session"}]
    description = "Implement exact task merge in src/itree/stage.py"
    acceptance = "Every field and opaque span survives byte for byte."
<!-- itree-task-document:v1:end -->
"""


def managed_body(*, plan: str = "Plan zero α\n", tasks: str = TASKS) -> str:
    return (
        "Préface unowned\r\n"
        "<!-- itree:original-contract:start -->\n"
        "Original Ω\n"
        "```itree-contract\n"
        'kind = "proof"\n'
        'evidence = "records"\n'
        "```\n"
        "<!-- itree:original-contract:end -->\n"
        "between original and requirements\n"
        "<!-- itree:requirements:start -->\n"
        "Requirement café\n"
        "<!-- itree:requirements:end -->\n"
        "<!-- itree:plan:start -->\n"
        f"{plan}"
        "```itree-contract\n"
        'kind = "audit"\n'
        'evidence = "narrows"\n'
        "```\n"
        "<!-- itree:plan:end -->\n"
        "unowned between plan and tasks\n"
        "<!-- itree:tasks:start -->\n"
        f"{tasks}"
        "<!-- itree:tasks:end -->"
    )


def writer_fixture_source() -> Path:
    return Path(__file__).parent / "fixtures" / "stage-writer" / "workflow.yml"


def example_state(body: str) -> dict[str, JsonValue]:
    workflow = writer_fixture_source().read_bytes()
    return {
        "repository": SLUG,
        "actor": "maintainer",
        "permissions": {
            "maintainer": "admin",
            "triager": "triage",
            "writer": "write",
            "reader": "read",
        },
        "issue": {
            "id": 910071,
            "node_id": ISSUE_NODE_ID,
            "number": ISSUE_NUMBER,
            "title": "Issue stage fixture",
            "state": "open",
            "html_url": f"https://github.com/{SLUG}/issues/{ISSUE_NUMBER}",
            "body": body,
        },
        "comments": [],
        "histories": {},
        "default_branch": "main",
        "writer": {
            "repository": SLUG,
            "default_branch": "main",
            "default_head_sha": COMMIT_SHA,
            "workflow_blob": COMMIT_SHA,
            "workflow_sha256": hashlib.sha256(workflow).hexdigest(),
        },
        "job_logs": [
            json.dumps(
                {
                    "kind": "operation_log_binding",
                    "operation_id": f"drain-{ISSUE_NODE_ID}",
                    "issue_node_id": ISSUE_NODE_ID,
                    "request_id": "drain",
                    "attempt_id": "drain",
                    "revision": COMMIT_SHA,
                    "workflow_path": ".github/workflows/itree-issue-writer.yml",
                    "workflow_sha256": hashlib.sha256(workflow).hexdigest(),
                },
                sort_keys=True,
            )
        ],
        "job": {
            "id": 201,
            "node_id": "J_writer",
            "name": "write",
            "run_id": 101,
            "run_attempt": 1,
            "head_sha": COMMIT_SHA,
            "head_branch": "main",
            "html_url": f"https://github.com/{SLUG}/actions/runs/101/job/201",
            "run_url": f"https://github.com/{SLUG}/actions/runs/101",
            "labels": ["ubuntu-latest"],
            "steps": [
                {
                    "name": "Record operation bindings",
                    "number": 6,
                    "status": "completed",
                    "conclusion": "success",
                }
            ],
        },
    }


class StageFixture:
    def __init__(self, root: Path, body: str | None = None) -> None:
        self.root = root
        self.bindir = root / "bin"
        self.bindir.mkdir(parents=True)
        source = Path(__file__).parent / "fixtures" / "stage-writer" / "gh"
        executable = self.bindir / "gh"
        shutil.copy(source, executable)
        executable.chmod(0o755)
        self.state_path = root / "state.json"
        self.log_path = root / "calls.jsonl"
        self.log_path.write_text("", encoding="utf-8")
        self.state = example_state(managed_body() if body is None else body)
        self.save()

    def save(self) -> None:
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")

    def load(self) -> None:
        self.state = json.loads(self.state_path.read_text())

    def _environment(
        self, writer: bool, environment: dict[str, str] | None
    ) -> dict[str, str]:
        values = dict(os.environ)
        values.update(
            {
                "PATH": f"{self.bindir}{os.pathsep}{os.environ['PATH']}",
                "ITREE_FIXTURE_STATE": str(self.state_path),
                "ITREE_FIXTURE_LOG": str(self.log_path),
                "ITREE_WORKFLOW_SOURCE": str(writer_fixture_source()),
                "ITREE_STAGE_FIXTURE_ROOT": str(self.root / "writer"),
            }
        )
        if writer:
            values["ITREE_STAGE_WRITER_MODE"] = "fixture"
            values["ITREE_STAGE_RUN_ID"] = "101"
            values["ITREE_STAGE_RUN_ATTEMPT"] = "1"
            values["ITREE_STAGE_JOB_NAME"] = "write"
        if environment is not None:
            values.update(environment)
        return values

    def run(
        self,
        *arguments: str,
        writer: bool = False,
        environment: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "itree", *arguments],
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
            env=self._environment(writer, environment),
            cwd=cwd,
        )

    def spawn(
        self,
        *arguments: str,
        writer: bool = False,
        environment: dict[str, str] | None = None,
    ) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [sys.executable, "-m", "itree", *arguments],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._environment(writer, environment),
        )

    def calls(self) -> list[dict[str, JsonValue]]:
        self.log_path.read_text(encoding="utf-8")
        return [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
        ]

    def materialize(self, attempt: str) -> tuple[Path, dict[str, str]]:
        result = self.run("stage", "materialize", ISSUE_REF, "--attempt", attempt)
        assert result.returncode == 0, result.stdout + result.stderr
        values = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
        assert values["issue_node_id"] == ISSUE_NODE_ID
        assert values["SPECIFY_FEATURE_NO_PERSIST"] == "1"
        feature = Path(values["SPECIFY_FEATURE_DIRECTORY"])
        assert feature.is_absolute()
        assert feature.stat().st_mode & 0o777 == 0o700
        assert not feature.is_relative_to(self.root)
        assert not feature.is_relative_to(Path.cwd())
        return feature, values

    def issue_body(self) -> str:
        self.load()
        issue = self.state["issue"]
        assert isinstance(issue, dict)
        body = issue["body"]
        assert isinstance(body, str)
        return body

    def patches(self) -> list[dict[str, JsonValue]]:
        return [call for call in self.calls() if "PATCH" in call["argv"]]
