"""Live regression proof for detaching closed GitHub sub-issues."""

from __future__ import annotations

import subprocess
from uuid import uuid4

from itree import cli
from itree.github import GithubApi
from itree.models import IssueCloseReason, RepoRef

SCRATCH = RepoRef(owner="dzackgarza", repo="itree-e2e-scratch")
PARENT_NUMBER = 4


def test_detach_removes_a_closed_direct_subissue_and_restores_the_fixture() -> None:
    """The CLI must detach a closed child through GitHub's real boundary."""
    api = GithubApi.from_repo_ref(SCRATCH)
    child = api.create_issue(f"proof: #26 closed-detach {uuid4().hex}", "Closed direct-subissue proof.")
    api.add_subissue(PARENT_NUMBER, child.id)
    api.close_issue(child.number, reason=IssueCloseReason.not_planned)

    try:
        cli.detach(f"{SCRATCH.slug}#{PARENT_NUMBER}", f"{SCRATCH.slug}#{child.number}")
        assert api.get_parent_number(child.number) is None
    finally:
        parent_number = api.get_parent_number(child.number)
        if parent_number is not None:
            subprocess.run(
                [
                    "gh",
                    "issue",
                    "edit",
                    str(parent_number),
                    "--repo",
                    SCRATCH.slug,
                    "--remove-sub-issue",
                    str(child.number),
                ],
                check=True,
            )
        assert api.get_parent_number(child.number) is None


def test_absorb_detaches_a_closed_direct_subissue_and_restores_target_body() -> None:
    """Absorb must detach a closed source through the same adapter boundary."""
    api = GithubApi.from_repo_ref(SCRATCH)
    target = api.get_issue(5)
    original_target_body = target.body or ""
    source = api.create_issue(f"proof: #26 absorb-closed {uuid4().hex}", "Closed source proof body.")

    try:
        api.add_subissue(PARENT_NUMBER, source.id)
        api.close_issue(source.number, reason=IssueCloseReason.not_planned)
        cli.absorb(f"{SCRATCH.slug}#{source.number}", into=f"{SCRATCH.slug}#{target.number}")

        reread_target = api.get_issue(target.number)
        reread_source = api.get_issue(source.number)
        assert "Closed source proof body." in (reread_target.body or "")
        assert not reread_source.is_open
        assert api.get_parent_number(source.number) is None
    finally:
        parent_number = api.get_parent_number(source.number)
        if parent_number is not None:
            subprocess.run(
                [
                    "gh",
                    "issue",
                    "edit",
                    str(parent_number),
                    "--repo",
                    SCRATCH.slug,
                    "--remove-sub-issue",
                    str(source.number),
                ],
                check=True,
            )
        current_source = api.get_issue(source.number)
        if current_source.is_open:
            api.close_issue(source.number, reason=IssueCloseReason.not_planned)
        api.update_issue_body(target.number, original_target_body)
        assert api.get_parent_number(source.number) is None
        assert (api.get_issue(target.number).body or "") == original_target_body
