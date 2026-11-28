from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from itree.models import (
    ArtifactDraft,
    ArtifactProposal,
    ActorLogin,
    FinalizedProposal,
    IssueRef,
    PendingUpload,
    RestartedAttempt,
    SectionEditProposal,
    SectionReplacement,
    SessionAdmissionRequest,
    SessionReleaseRequest,
    StageModel,
    UploadContent,
    UploadRejected,
)
from itree.stage import (
    artifact_upload_contents,
    build_upload_manifest,
    finalize_upload,
    make_upload_parts,
    regenerate_upload_part,
    section_upload_contents,
    sha256_text,
)
from itree.stage_writer import stage_comment_body
from itree.work import ManagedIssueLayout, SectionName, parse_issue_layout
from tests.stage_support import (
    ISSUE_NODE_ID,
    ISSUE_NUMBER,
    ISSUE_REF,
    SLUG,
    StageFixture,
    managed_body,
)


def _layout(body: str) -> ManagedIssueLayout:
    layout = parse_issue_layout(body)
    assert isinstance(layout, ManagedIssueLayout)
    return layout


def _section_proposal() -> SectionEditProposal:
    body = managed_body()
    layout = _layout(body)
    base = next(
        section.text for section in layout.sections if section.name == SectionName.plan
    )
    proposed = base.replace("Plan zero α", "Plan finalized β")
    return SectionEditProposal(
        version=1,
        request_kind="sections",
        issue_node_id=ISSUE_NODE_ID,
        issue_ref=IssueRef.parse(ISSUE_REF),
        request_id="A-multipart",
        attempt_id="attempt-multipart",
        session_id="session-multipart",
        base_body_sha256=sha256_text(body),
        base_body=body,
        changes=(
            SectionReplacement(
                section="plan",
                base_text=base,
                proposed_text=proposed,
                base_sha256=sha256_text(base),
                proposed_sha256=sha256_text(proposed),
            ),
        ),
    )


def test_every_missing_ordinal_stays_pending_and_only_total_parts_finalize() -> None:
    proposal = _section_proposal()
    contents = section_upload_contents(proposal)
    manifest = build_upload_manifest(proposal, contents)
    parts = make_upload_parts(manifest, contents)

    for absent in range(len(parts)):
        partial = parts[:absent] + parts[absent + 1 :]
        pending = finalize_upload(manifest, partial)
        assert isinstance(pending, PendingUpload)
        assert [missing.ordinal for missing in pending.missing_parts] == [
            parts[absent].ordinal
        ]
        assert pending.missing_parts[0].role == parts[absent].role

    complete = finalize_upload(manifest, parts)
    assert isinstance(complete, FinalizedProposal)
    assert complete.proposal == proposal.model_dump(mode="json")
    assert complete.logical_payload_sha256 == manifest.finalized_proposal_sha256


def test_regeneration_requires_exact_digest_and_never_confirms_a_replacement() -> None:
    proposal = _section_proposal()
    contents = section_upload_contents(proposal)
    manifest = build_upload_manifest(proposal, contents)
    parts = make_upload_parts(manifest, contents)
    absent = parts[-1]
    partial = parts[:-1]

    unauthenticated = regenerate_upload_part(
        manifest, partial, absent, authenticated=False
    )
    assert isinstance(unauthenticated, RestartedAttempt)
    assert unauthenticated.prior.missing_parts[0].role == absent.role

    substituted = make_upload_parts(
        manifest,
        (
            UploadContent(
                role=absent.role,
                ordinal=absent.ordinal,
                payload=absent.payload + "different",
            ),
        ),
    )[0]
    rejected = regenerate_upload_part(
        manifest, partial, substituted, authenticated=True
    )
    assert isinstance(rejected, RestartedAttempt)

    exact = regenerate_upload_part(manifest, partial, absent, authenticated=True)
    assert isinstance(exact, FinalizedProposal)


@pytest.mark.parametrize("defect", ["duplicate", "reordered", "digest", "extra"])
def test_invalid_part_sets_cannot_finalize(defect: str) -> None:
    proposal = _section_proposal()
    contents = section_upload_contents(proposal)
    manifest = build_upload_manifest(proposal, contents)
    parts = make_upload_parts(manifest, contents)
    candidates = {
        "duplicate": (parts[0], *parts),
        "reordered": (parts[1], parts[0], *parts[2:]),
        "digest": (
            parts[0],
            parts[1].model_copy(update={"payload": "altered"}),
            *parts[2:],
        ),
        "extra": (
            make_upload_parts(
                manifest, (UploadContent(role="extra", ordinal=9, payload="unknown"),)
            )[0],
            *parts,
        ),
    }

    result = finalize_upload(manifest, candidates[defect])

    assert isinstance(result, UploadRejected)
    assert (
        result.reason
        == {
            "duplicate": "duplicate_part",
            "reordered": "wrong_order",
            "digest": "digest_mismatch",
            "extra": "unexpected_part",
        }[defect]
    )


def test_every_artifact_family_round_trips_by_declared_key_and_content() -> None:
    body = managed_body()
    families = (
        ("research", "research"),
        ("data-model", "data-model"),
        ("contract", "contracts/stage-cli"),
        ("quickstart", "quickstart"),
        ("clarification", "clarification-1"),
        ("finding", "finding-1"),
        ("evidence", "evidence-1"),
        ("checkpoint", "checkpoint-1"),
        ("plan-history", "plan-history-1"),
        ("task-history", "task-history-1"),
    )
    proposal = ArtifactProposal(
        version=1,
        request_kind="artifacts",
        issue_node_id=ISSUE_NODE_ID,
        issue_ref=IssueRef.parse(ISSUE_REF),
        request_id="A-artifact-families",
        attempt_id="attempt-artifacts",
        session_id="session-artifacts",
        base_body_sha256=sha256_text(body),
        base_body=body,
        artifacts=tuple(
            ArtifactDraft(
                key=key, kind=kind, content=f"Exact {kind} content for {key}\n"
            )
            for kind, key in families
        ),
    )
    manifest = build_upload_manifest(proposal, artifact_upload_contents(proposal))
    parts = make_upload_parts(manifest, artifact_upload_contents(proposal))

    for expected in proposal.artifacts:
        missing = make_upload_parts(
            manifest,
            (
                UploadContent(
                    role=f"artifact:{expected.kind}:{expected.key}",
                    ordinal=0,
                    payload=expected.content,
                ),
            ),
        )
        retained = tuple(
            part
            for part in parts
            if part.role != f"artifact:{expected.kind}:{expected.key}"
        )
        missing_index = next(
            index
            for index, part in enumerate(parts)
            if part.role == f"artifact:{expected.kind}:{expected.key}"
        )
        partial = retained[:missing_index] + missing + retained[missing_index:]
        absent = finalize_upload(
            manifest,
            tuple(
                part
                for part in parts
                if part.role != f"artifact:{expected.kind}:{expected.key}"
            ),
        )
        assert isinstance(absent, PendingUpload)
        assert (
            absent.missing_parts[0].role == f"artifact:{expected.kind}:{expected.key}"
        )
        with_newest = finalize_upload(manifest, partial)
        assert isinstance(with_newest, FinalizedProposal)

    completed = finalize_upload(manifest, parts)
    assert isinstance(completed, FinalizedProposal)
    response = json.loads(json.dumps(completed.proposal))
    assert [
        (item["kind"], item["key"], item["content"]) for item in response["artifacts"]
    ] == [(item.kind, item.key, item.content) for item in proposal.artifacts]


def test_session_upload_parts_cannot_synthesize_an_admission_before_record_exists() -> (
    None
):
    session = SessionAdmissionRequest(
        version=1,
        request_kind="session_admit",
        issue_node_id=ISSUE_NODE_ID,
        issue_ref=IssueRef.parse(ISSUE_REF),
        request_id="session-multipart",
        attempt_id="attempt-multipart",
        session_id="session-multipart",
        entry_body_sha256=sha256_text(managed_body()),
        enrolled_actors=(ActorLogin("maintainer"),),
    )
    contents = (
        UploadContent(
            role="session",
            ordinal=0,
            payload=json.dumps(
                session.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ),
        ),
    )
    manifest = build_upload_manifest(session, contents)
    parts = make_upload_parts(manifest, contents)

    pending = finalize_upload(manifest, ())
    assert isinstance(pending, PendingUpload)
    assert pending.missing_parts[0].role == "session"
    complete = finalize_upload(manifest, parts)
    assert isinstance(complete, FinalizedProposal)
    assert complete.proposal == session.model_dump(mode="json")


def _post_record(fixture: StageFixture, fence: str, record: StageModel) -> None:
    result = subprocess.run(
        [
            str(fixture.bindir / "gh"),
            "api",
            "-X",
            "POST",
            f"repos/{SLUG}/issues/{ISSUE_NUMBER}/comments",
            "--input",
            "-",
        ],
        input=json.dumps(
            {"body": stage_comment_body(fence, record.model_dump(mode="json"))}
        ),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        env=fixture._environment(False, None),
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_drain_stops_on_every_missing_part_and_recovers_from_github_only(
    tmp_path: Path,
) -> None:
    fixture = StageFixture(tmp_path / "fixture")
    feature, _ = fixture.materialize("A-upload")
    admitted = fixture.run(
        "stage",
        "write-back",
        ISSUE_REF,
        "--attempt",
        "A-upload",
        "--feature-directory",
        str(feature),
        writer=True,
    )
    assert admitted.returncode == 0, admitted.stdout
    proposal = _section_proposal().model_copy(
        update={"attempt_id": "A-upload", "session_id": "session-A-upload"}
    )
    contents = section_upload_contents(proposal)
    manifest = build_upload_manifest(proposal, contents)
    parts = make_upload_parts(manifest, contents)
    _post_record(fixture, "itree-stage-upload-manifest", manifest)

    pending = fixture.run("stage", "drain", ISSUE_REF, writer=True)
    assert pending.returncode == 5
    assert "pending_upload" in pending.stdout
    assert len(fixture.patches()) == 0
    assert not any(
        '"status":"intended"' in comment["body"]
        for comment in fixture.state["comments"]
    )

    for index, part in enumerate(parts):
        _post_record(fixture, "itree-stage-upload-part", part)
        if index + 1 < len(parts):
            partial = fixture.run("stage", "drain", ISSUE_REF, writer=True)
            assert partial.returncode == 5
            assert "pending_upload" in partial.stdout
            assert fixture.patches() == []

    completed = fixture.run("stage", "drain", ISSUE_REF, writer=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert len(fixture.patches()) == 1
    assert "Plan finalized β" in fixture.issue_body()
    assert not (tmp_path / "unused-scratch").exists()

    replay = fixture.run("stage", "drain", ISSUE_REF, writer=True)
    assert replay.returncode == 0, replay.stdout + replay.stderr
    assert len(fixture.patches()) == 1

    fixture.load()
    removed = next(
        comment
        for comment in fixture.state["comments"]
        if comment["body"]
        == stage_comment_body(
            "itree-stage-upload-part", parts[-1].model_dump(mode="json")
        )
    )
    fixture.state["comments"].remove(removed)
    fixture.save()
    incomplete = fixture.run("stage", "drain", ISSUE_REF, writer=True)
    assert incomplete.returncode == 5
    assert "pending_upload" in incomplete.stdout
    assert len(fixture.patches()) == 1


def test_artifact_families_publish_link_materialize_and_invalidate_on_edit(
    tmp_path: Path,
) -> None:
    fixture = StageFixture(tmp_path / "fixture")
    feature, _ = fixture.materialize("A-publish-artifacts")
    files = {
        "research.md": "Exact research\n",
        "data-model.md": "Exact model\n",
        "quickstart.md": "Exact quickstart\n",
        "contracts/stage-cli.md": "Exact contract\n",
        "clarifications/c-1.md": "Exact clarification\n",
        "findings/f-1.md": "Exact finding\n",
        "evidence/e-1.md": "Exact evidence\n",
        "checkpoints/cp-1.md": "Exact checkpoint\n",
        "plan-history/p-1.md": "Exact plan history\n",
        "task-history/t-1.md": "Exact task history\n",
    }
    for relative, content in files.items():
        destination = feature / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)
    result = fixture.run(
        "stage",
        "write-back",
        ISSUE_REF,
        "--attempt",
        "A-publish-artifacts",
        "--feature-directory",
        str(feature),
        writer=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    fixture.load()
    artifact_comments = [
        comment
        for comment in fixture.state["comments"]
        if comment["body"].startswith("```itree-stage-artifact\n")
    ]
    assert len(artifact_comments) == len(files)
    for comment in artifact_comments:
        payload = json.loads(comment["body"].splitlines()[1])
        assert payload["payload_sha256"] == sha256_text(payload["content"])
        assert payload["writer_seal"]["repository"] == SLUG
        assert (
            payload["writer_seal"]["workflow_path"]
            == ".github/workflows/itree-issue-writer.yml"
        )
        assert payload["writer_seal"]["effective_token_permissions"] == {
            "contents": "read",
            "issues": "write",
            "actions": "read",
        }
        history = fixture.state["histories"][comment["node_id"]]
        assert history["createdAt"] == history["updatedAt"]
        assert history["createdAt"] == history["updatedAt"]
        assert (
            f"https://github.com/{SLUG}/issues/{ISSUE_NUMBER}#issuecomment-{comment['id']}"
            == comment["html_url"]
        )
    assert "itree-stage-artifact-index" in fixture.issue_body()

    fresh, values = fixture.materialize("A-fresh-artifacts")
    assert values["SPECIFY_FEATURE_NO_PERSIST"] == "1"
    for relative, expected in files.items():
        assert fresh.joinpath(relative).read_text() == expected

    fixture.load()
    edited = artifact_comments[0]
    current = next(
        comment
        for comment in fixture.state["comments"]
        if comment["id"] == edited["id"]
    )
    current["body"] = current["body"] + "late-edit"
    current["updated_at"] = "2026-11-28T02:00:00Z"
    history = fixture.state["histories"][current["node_id"]]
    history.update(
        {
            "updatedAt": current["updated_at"],
            "lastEditedAt": current["updated_at"],
            "editor": {"login": "other"},
            "userContentEdits": {"totalCount": 1, "nodes": [{"id": "late-edit"}]},
        }
    )
    fixture.save()
    before = fixture.issue_body()
    patches_before_block = len(fixture.patches())
    blocked = fixture.run(
        "stage", "materialize", ISSUE_REF, "--attempt", "A-edited-artifact"
    )
    assert blocked.returncode == 3
    assert "edited or mismatched artifact" in blocked.stdout
    assert fixture.issue_body() == before
    assert len(fixture.patches()) == patches_before_block


def test_artifact_manifest_recovers_and_indexes_only_after_final_part(
    tmp_path: Path,
) -> None:
    fixture = StageFixture(tmp_path / "fixture")
    feature, _ = fixture.materialize("A-multipart")
    first = fixture.run(
        "stage",
        "write-back",
        ISSUE_REF,
        "--attempt",
        "A-multipart",
        "--feature-directory",
        str(feature),
        writer=True,
    )
    assert first.returncode == 0, first.stdout + first.stderr
    body = fixture.issue_body()
    proposal = ArtifactProposal(
        version=1,
        request_kind="artifacts",
        issue_node_id=ISSUE_NODE_ID,
        issue_ref=IssueRef.parse(ISSUE_REF),
        request_id="artifacts-multipart",
        attempt_id="A-multipart",
        session_id="session-A-multipart",
        base_body_sha256=sha256_text(body),
        base_body=body,
        artifacts=(
            ArtifactDraft(
                key="multipart-research",
                kind="research",
                content="Multipart research\n",
            ),
            ArtifactDraft(
                key="multipart-evidence",
                kind="evidence",
                content="Multipart evidence\n",
            ),
        ),
    )
    contents = artifact_upload_contents(proposal)
    manifest = build_upload_manifest(proposal, contents)
    parts = make_upload_parts(manifest, contents)
    _post_record(fixture, "itree-stage-upload-manifest", manifest)

    pending = fixture.run("stage", "drain", ISSUE_REF, writer=True)
    assert pending.returncode == 5
    assert "pending_upload" in pending.stdout
    assert fixture.patches() == []

    for part in parts[:-1]:
        _post_record(fixture, "itree-stage-upload-part", part)
    still_pending = fixture.run("stage", "drain", ISSUE_REF, writer=True)
    assert still_pending.returncode == 5
    assert "pending_upload" in still_pending.stdout
    assert fixture.patches() == []

    _post_record(fixture, "itree-stage-upload-part", parts[-1])
    completed = fixture.run("stage", "drain", ISSUE_REF, writer=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert len(fixture.patches()) == 1
    assert "itree-stage-artifact-index" in fixture.issue_body()
    assert "multipart-research" in fixture.issue_body()
    assert "multipart-evidence" in fixture.issue_body()
    replay = fixture.run("stage", "drain", ISSUE_REF, writer=True)
    assert replay.returncode == 0, replay.stdout + replay.stderr
    assert len(fixture.patches()) == 1


def test_rejected_only_upload_queue_replays_and_releases(tmp_path: Path) -> None:
    fixture = StageFixture(tmp_path / "fixture")
    feature, _ = fixture.materialize("A-multipart")
    first = fixture.run(
        "stage",
        "write-back",
        ISSUE_REF,
        "--attempt",
        "A-multipart",
        "--feature-directory",
        str(feature),
        writer=True,
    )
    assert first.returncode == 0, first.stdout + first.stderr
    body = fixture.issue_body()
    proposal = ArtifactProposal(
        version=1,
        request_kind="artifacts",
        issue_node_id=ISSUE_NODE_ID,
        issue_ref=IssueRef.parse(ISSUE_REF),
        request_id="wrong-order-artifacts",
        attempt_id="A-multipart",
        session_id="session-A-multipart",
        base_body_sha256=sha256_text(body),
        base_body=body,
        artifacts=(
            ArtifactDraft(
                key="ordered-research", kind="research", content="Ordered research\n"
            ),
            ArtifactDraft(
                key="ordered-evidence", kind="evidence", content="Ordered evidence\n"
            ),
        ),
    )
    contents = artifact_upload_contents(proposal)
    manifest = build_upload_manifest(proposal, contents)
    ordered_parts = make_upload_parts(manifest, contents)
    wrong_order = (ordered_parts[1], ordered_parts[0], *ordered_parts[2:])
    _post_record(fixture, "itree-stage-upload-manifest", manifest)
    for part in wrong_order:
        _post_record(fixture, "itree-stage-upload-part", part)

    rejected = fixture.run("stage", "drain", ISSUE_REF, writer=True)
    assert rejected.returncode == 4
    assert "wrong_order" in rejected.stdout
    assert "rejected=wrong-order-artifacts" in rejected.stdout
    assert fixture.patches() == []
    fixture.load()
    rejected_records = [
        comment
        for comment in fixture.state["comments"]
        if comment["body"].startswith("```itree-stage-receipt")
        and '"status":"rejected"' in comment["body"]
        and "wrong-order-artifacts" in comment["body"]
    ]
    assert len(rejected_records) == 1
    receipt = rejected_records[0]
    payload = json.loads(receipt["body"].splitlines()[1])
    assert payload["rejection_reason"] == "conflict"

    replay = fixture.run("stage", "drain", ISSUE_REF, writer=True)
    assert replay.returncode == 0, replay.stdout + replay.stderr
    assert "wrong_order" not in replay.stdout
    fixture.load()
    assert (
        len(
            [
                comment
                for comment in fixture.state["comments"]
                if comment["body"] == receipt["body"]
            ]
        )
        == 1
    )

    expected_id = max(comment["id"] for comment in fixture.state["comments"]) + 1
    release = SessionReleaseRequest(
        version=1,
        request_kind="session_release",
        issue_node_id=ISSUE_NODE_ID,
        issue_ref=IssueRef.parse(ISSUE_REF),
        request_id="release-after-rejected",
        attempt_id="A-multipart",
        session_id="session-A-multipart",
        expected_last_scanned_comment_id=expected_id,
    )
    _post_record(fixture, "itree-stage-edit", release)
    released = fixture.run("stage", "drain", ISSUE_REF, writer=True)
    assert released.returncode == 0, released.stdout + released.stderr
    assert "session_released=session-A-multipart" in released.stdout
