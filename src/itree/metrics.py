"""Proportionality metrics: Q-code structure questions (#7).

Q findings are advisory. They render in the doctor "Structure questions:"
section and never affect the exit code; all severity data lives in
DIAGNOSTIC_CATALOG.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .models import DoctorReport, Finding, RepoDag

CONFIG_PATH = Path.home() / ".config" / "itree" / "config.toml"


class MetricsConfig(BaseModel):
    """Knobs for the Q-code predicates, from ~/.config/itree/config.toml."""

    model_config = ConfigDict(frozen=True)

    max_open_work_units: int = 8
    loc_per_work_unit: int = 400
    flat_children_ratio: float = 0.5
    flat_min_children: int = 6


class PresentCodeSize(BaseModel):
    kind: Literal["present"] = "present"
    total_loc: int


class AbsentCodeSize(BaseModel):
    kind: Literal["absent"] = "absent"
    reason: str


CodeSizeEvidence = PresentCodeSize | AbsentCodeSize


def load_config(path: Path = CONFIG_PATH) -> MetricsConfig:
    """Missing file yields the documented defaults; a malformed file fails loudly."""
    raise NotImplementedError("#7")


def parse_scc_total(scc_json: str) -> int:
    """Sum code lines across languages from ``scc --format json`` output."""
    raise NotImplementedError("#7")


def measure_code_size(slug: str, cwd: Path) -> CodeSizeEvidence:
    """Code size of a local checkout matching ``slug``, absent otherwise."""
    raise NotImplementedError("#7")


def structure_questions(
    dag: RepoDag,
    report: DoctorReport,
    config: MetricsConfig,
    code_size: CodeSizeEvidence,
) -> list[Finding]:
    """Evaluate Q001/Q002/Q003 against a finished doctor report."""
    raise NotImplementedError("#7")
