"""Proportionality metrics: Q-code structure questions (#7).

Q findings are advisory. They render in the doctor "Structure questions:"
section and never affect the exit code; all severity data lives in
DIAGNOSTIC_CATALOG.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .models import DoctorReport, Finding, RepoDag
from .validate import DIAGNOSTIC_CATALOG

CONFIG_PATH = Path.home() / ".config" / "itree" / "config.toml"


class MetricsConfig(BaseModel):
    """Knobs for the Q-code predicates, from ~/.config/itree/config.toml."""

    model_config = ConfigDict(frozen=True)

    max_open_work_units: int = 8
    loc_per_work_unit: int = 400
    flat_children_ratio: float = 0.5
    flat_min_children: int = 6
    deferral_label: str = "deferred"
    decomposition_label: str = ""
    derived_state_labels: tuple[str, ...] = ()


class PresentCodeSize(BaseModel):
    kind: Literal["present"] = "present"
    total_loc: int


class AbsentCodeSize(BaseModel):
    kind: Literal["absent"] = "absent"
    reason: str


CodeSizeEvidence = PresentCodeSize | AbsentCodeSize


def load_config(path: Path = CONFIG_PATH) -> MetricsConfig:
    """Missing file yields the documented defaults; a malformed file fails loudly."""
    if not path.exists():
        return MetricsConfig()
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return MetricsConfig.model_validate(data)


def parse_scc_total(scc_json: str) -> int:
    """Sum code lines across languages from ``scc --format json`` output."""
    return sum(int(entry["Code"]) for entry in json.loads(scc_json))


def measure_code_size(slug: str, cwd: Path) -> CodeSizeEvidence:
    """Code size of a local checkout matching ``slug``, absent otherwise.

    Q002 is evaluated only when the working directory is a checkout of the
    repository under diagnosis; anything else is Absent with a visible reason,
    never a guessed size.
    """
    origin = subprocess.run(
        ["git", "-C", str(cwd), "config", "--get", "remote.origin.url"],
        capture_output=True,
        text=True,
        check=False,
    )
    if origin.returncode != 0 or slug not in origin.stdout:
        return AbsentCodeSize(reason=f"no local checkout of {slug} at {cwd}")
    if shutil.which("scc") is None:
        return AbsentCodeSize(reason="scc is not installed")
    scc = subprocess.run(
        ["scc", "--format", "json", str(cwd)],
        capture_output=True,
        text=True,
        check=True,
    )
    return PresentCodeSize(total_loc=parse_scc_total(scc.stdout))


def structure_questions(
    dag: RepoDag,
    report: DoctorReport,
    config: MetricsConfig,
    code_size: CodeSizeEvidence,
) -> list[Finding]:
    """Evaluate Q001/Q002/Q003 against a finished doctor report."""
    findings: list[Finding] = []
    open_work_units = report.metrics.open_work_units

    if open_work_units > config.max_open_work_units:
        findings.append(
            _q_finding(
                "Q001",
                [
                    f"{open_work_units} open work units exceed max_open_work_units={config.max_open_work_units}"
                ],
            )
        )

    if code_size.kind == "present":
        supported = max(1, code_size.total_loc // config.loc_per_work_unit)
        if open_work_units > supported:
            findings.append(
                _q_finding(
                    "Q002",
                    [
                        f"{open_work_units} open work units against {code_size.total_loc} LOC supports ~{supported} (loc_per_work_unit={config.loc_per_work_unit})"
                    ],
                )
            )

    if report.root.kind == "present":
        root_num = report.root.ref.number
        open_children = [c for c in dag.children_of[root_num] if dag.issues[c].is_open]
        open_reachable = (
            report.metrics.open_issues_reachable_from_root - 1
        )  # minus the root itself
        if (
            len(open_children) >= config.flat_min_children
            and open_reachable > 0
            and len(open_children) / open_reachable >= config.flat_children_ratio
        ):
            findings.append(
                _q_finding(
                    "Q003",
                    [
                        f"{len(open_children)} of {open_reachable} open issues hang directly off root #{root_num} (>= flat_children_ratio={config.flat_children_ratio})"
                    ],
                )
            )

    return findings


def _q_finding(code: str, evidence: list[str]) -> Finding:
    details = DIAGNOSTIC_CATALOG[code]
    return Finding(
        code=code,
        severity=details["severity"],
        title=details["title"],
        evidence=evidence,
        meaning=details["meaning"],
        remediation=details["remediation"],
    )
