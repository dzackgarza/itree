from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tests.stage_support import ISSUE_REF, StageFixture


def test_materialize_uses_the_selected_issue_in_private_scratch(tmp_path: Path) -> None:
    fixture = StageFixture(tmp_path / "fixture")
    decoy_root = tmp_path / "decoy-checkout"
    decoy_plan = decoy_root / ".itree" / "work" / "999" / "plan.md"
    decoy_plan.parent.mkdir(parents=True)
    decoy_plan.write_text("DECOY-PLAN", encoding="utf-8")
    pointer = decoy_root / ".specify" / "feature.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text('{"feature":"999"}', encoding="utf-8")

    feature, values = fixture.materialize("A-materialize")

    body = fixture.issue_body()
    original = body.split("<!-- itree:original-contract:start -->\n", 1)[1].split(
        "<!-- itree:original-contract:end -->", 1
    )[0]
    assert values["issue_ref"] == ISSUE_REF
    assert (
        values["original_contract_sha256"]
        == hashlib.sha256(original.encode()).hexdigest()
    )
    assert values["body_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert values["layout"] == "managed"
    assert feature.joinpath("original-contract.md").read_text() == original
    assert (
        feature.joinpath("plan.md").read_text()
        == 'Plan zero α\n```itree-contract\nkind = "audit"\nevidence = "narrows"\n```\n'
    )
    assert "DECOY" not in feature.joinpath("tasks.md").read_text()
    assert pointer.read_text() == '{"feature":"999"}'
    assert decoy_plan.read_text() == "DECOY-PLAN"


def test_no_change_managed_round_trip_never_patches(tmp_path: Path) -> None:
    fixture = StageFixture(tmp_path / "fixture")
    feature, _ = fixture.materialize("A-no-change")
    before = fixture.issue_body()

    result = fixture.run(
        "stage",
        "write-back",
        ISSUE_REF,
        "--attempt",
        "A-no-change",
        "--feature-directory",
        str(feature),
        writer=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "no_body_write=true" in result.stdout
    assert "stage_complete=true" in result.stdout
    assert "scratch_cleanup=removed" in result.stdout
    assert fixture.issue_body() == before
    assert fixture.patches() == []
    assert not feature.exists()


@pytest.mark.parametrize("legacy", ["legacy body\n", "legacy without final newline"])
def test_no_change_legacy_round_trip_preserves_zero_markers(
    tmp_path: Path, legacy: str
) -> None:
    fixture = StageFixture(tmp_path / "fixture", legacy)
    feature, values = fixture.materialize("A-legacy-no-change")
    original_inputs = {
        name: feature.joinpath(name).read_bytes()
        for name in ("spec.md", "plan.md", "tasks.md")
    }

    result = fixture.run(
        "stage",
        "write-back",
        ISSUE_REF,
        "--attempt",
        "A-legacy-no-change",
        "--feature-directory",
        str(feature),
        writer=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert values["layout"] == "legacy"
    assert original_inputs == {"spec.md": b"", "plan.md": b"", "tasks.md": b""}
    assert not feature.exists()
    assert fixture.issue_body() == legacy
    assert fixture.patches() == []


def test_only_the_changed_owned_ranges_move(tmp_path: Path) -> None:
    fixture = StageFixture(tmp_path / "fixture")
    feature, _ = fixture.materialize("A-ranges")
    original = fixture.issue_body()
    plan = 'Plan one β\n```itree-contract\nkind = "audit"\nevidence = "narrows"\n```\n'
    feature.joinpath("plan.md").write_text(plan, encoding="utf-8")
    feature.joinpath("tasks.md").write_text("Tasks changed δ\n", encoding="utf-8")

    result = fixture.run(
        "stage",
        "write-back",
        ISSUE_REF,
        "--attempt",
        "A-ranges",
        "--feature-directory",
        str(feature),
        writer=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    updated = fixture.issue_body()
    assert (
        updated.split("<!-- itree:original-contract:start -->", 1)[1].split(
            "<!-- itree:original-contract:end -->", 1
        )[0]
        == original.split("<!-- itree:original-contract:start -->", 1)[1].split(
            "<!-- itree:original-contract:end -->", 1
        )[0]
    )
    assert updated.startswith("Préface unowned\r\n")
    assert "between original and requirements\n" in updated
    assert "Plan zero α" not in updated
    assert "Plan one β\n" in updated
    assert "Tasks changed δ\n" in updated
    assert updated.endswith("<!-- itree:tasks:end -->")
    assert len(fixture.patches()) == 1


def test_original_scratch_change_is_rejected_before_any_proposal(
    tmp_path: Path,
) -> None:
    fixture = StageFixture(tmp_path / "fixture")
    feature, _ = fixture.materialize("A-original-target")
    before = fixture.issue_body()
    feature.joinpath("original-contract.md").chmod(0o600)
    feature.joinpath("original-contract.md").write_text("tampered\n")

    result = fixture.run(
        "stage",
        "write-back",
        ISSUE_REF,
        "--attempt",
        "A-original-target",
        "--feature-directory",
        str(feature),
        writer=True,
    )

    assert result.returncode == 2
    assert "original_contract_target" in result.stdout
    assert feature.exists()
    assert fixture.issue_body() == before
    assert fixture.patches() == []
    assert fixture.state["comments"] == []


def test_contract_fence_removal_is_rejected_without_normalizing_other_ranges(
    tmp_path: Path,
) -> None:
    fixture = StageFixture(tmp_path / "fixture")
    feature, _ = fixture.materialize("A-fence")
    before = fixture.issue_body()
    feature.joinpath("plan.md").write_text("Plan without its declaration\n")

    result = fixture.run(
        "stage",
        "write-back",
        ISSUE_REF,
        "--attempt",
        "A-fence",
        "--feature-directory",
        str(feature),
        writer=True,
    )

    assert result.returncode == 4
    assert "contract_fence_change" in result.stdout
    assert fixture.issue_body() == before
    assert fixture.patches() == []


def test_legacy_intake_is_atomic_and_requires_an_explicit_flag(
    tmp_path: Path,
) -> None:
    legacy = "Legacy objective\nwith two lines\n"
    fixture = StageFixture(tmp_path / "fixture", legacy)
    feature, _ = fixture.materialize("A-intake")
    feature.joinpath("spec.md").write_text("Requirements one\n")
    feature.joinpath("plan.md").write_text("Plan one\n")

    rejected = fixture.run(
        "stage",
        "write-back",
        ISSUE_REF,
        "--attempt",
        "A-intake",
        "--feature-directory",
        str(feature),
        writer=True,
    )
    assert rejected.returncode == 2
    assert "legacy_intake_required" in rejected.stdout
    assert fixture.issue_body() == legacy

    accepted = fixture.run(
        "stage",
        "write-back",
        ISSUE_REF,
        "--attempt",
        "A-intake",
        "--feature-directory",
        str(feature),
        "--managed-intake",
        writer=True,
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    updated = fixture.issue_body()
    assert updated.startswith("<!-- itree:original-contract:start -->\n" + legacy)
    assert (
        updated.index("itree:original-contract:start")
        < updated.index("itree:requirements:start")
        < updated.index("itree:plan:start")
        < updated.index("itree:tasks:start")
    )
    assert len(fixture.patches()) == 1


def test_legacy_intake_without_final_newline_has_no_candidate(tmp_path: Path) -> None:
    fixture = StageFixture(tmp_path / "fixture", "Legacy without final newline")
    feature, _ = fixture.materialize("A-no-final-newline")
    feature.joinpath("plan.md").write_text("Cannot be represented\n")

    result = fixture.run(
        "stage",
        "write-back",
        ISSUE_REF,
        "--attempt",
        "A-no-final-newline",
        "--feature-directory",
        str(feature),
        "--managed-intake",
        writer=True,
    )

    assert result.returncode == 2
    assert "legacy_missing_final_newline" in result.stdout
    assert fixture.issue_body() == "Legacy without final newline"
    assert fixture.patches() == []


def test_missing_node_id_fails_without_creating_scratch(tmp_path: Path) -> None:
    fixture = StageFixture(tmp_path / "fixture")
    issue = fixture.state["issue"]
    assert isinstance(issue, dict)
    issue.pop("node_id")
    fixture.save()

    result = fixture.run("stage", "materialize", ISSUE_REF, "--attempt", "A-node")

    assert result.returncode == 3
    assert "missing_node" in result.stdout
    assert fixture.patches() == []
    assert fixture.state["comments"] == []
