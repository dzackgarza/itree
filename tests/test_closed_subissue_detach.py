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
        subprocess.run(
            [
                "gh",
                "issue",
                "edit",
                str(PARENT_NUMBER),
                "--repo",
                SCRATCH.slug,
                "--remove-sub-issue",
                str(child.number),
            ],
            check=True,
        )
        assert api.get_parent_number(child.number) is None
