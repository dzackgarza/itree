"""Real-process proof for GitHub mutation outcome classification."""

import pytest

from itree.github import GithubApi, GithubIndeterminateError
from itree.models import (
    MilestoneEffect,
    MilestoneEffectKind,
    RepoRef,
)


def test_nonzero_gh_without_http_status_is_indeterminate() -> None:
    """A real gh failure without an HTTP response cannot prove API rejection."""
    api = GithubApi.from_repo_ref(RepoRef(owner="owner", repo="repo"))
    effect = MilestoneEffect(kind=MilestoneEffectKind.create_milestone)

    with pytest.raises(GithubIndeterminateError) as raised:
        api._run_mutation_command(
            "INVALID METHOD",
            "rate_limit",
            effect,
            raw_fields={},
            typed_fields={},
            timeout=30,
        )

    assert raised.value.outcome.kind == "indeterminate"
    assert raised.value.outcome.effect == effect
