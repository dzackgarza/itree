#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import os
import sys
from importlib.metadata import Distribution, PackageNotFoundError
from pathlib import Path


def main() -> None:
    try:
        distribution = Distribution.from_name("itree")
        entrypoints = [
            entry
            for entry in distribution.entry_points
            if entry.group == "console_scripts"
            and entry.name == "itree"
            and entry.value == "itree.cli:app"
        ]
        assert len(entrypoints) == 1, {"matching_entrypoints": entrypoints}
        record = distribution.read_text("RECORD")
        assert record is not None, {"required": "installed itree RECORD"}
        candidates = [
            row[0]
            for row in csv.reader(record.splitlines())
            if row and Path(row[0]).name == "itree"
        ]
        assert len(candidates) == 1, {"launcher_candidates": candidates}
        launcher = Path(distribution.locate_file(candidates[0])).resolve()
        assert launcher.is_file(), {"launcher": launcher}
    except (PackageNotFoundError, OSError) as error:
        sys.stderr.write(
            json.dumps({"kind": "distribution-failure", "diagnostic": str(error)})
            + "\n"
        )
        raise SystemExit(5)
    os.execv(launcher, [str(launcher), "harness-work", "initialize"])


if __name__ == "__main__":
    main()
