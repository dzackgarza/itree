from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SETUP = Path(__file__).parent / "fixtures" / "work-package" / "setup.py"
_SPEC = importlib.util.spec_from_file_location("work_package_setup", _SETUP)
assert _SPEC is not None and _SPEC.loader is not None
setup = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(setup)


@pytest.mark.parametrize(
    "operation", ["createPullRequest", "createGitTag", "createRef"]
)
def test_content_object_pacing_counts_every_graphql_creation(operation: str) -> None:
    fields = " ".join(
        f'r{index:03d}:{operation}(input:{{repositoryId:"R"}}){{ref{{id}}}}'
        for index in range(5)
    )

    units = setup._content_units(
        ["gh", "api", "graphql", "-f", f"query=mutation WorkPackage {{{fields}}}"]
    )

    assert units == 5
