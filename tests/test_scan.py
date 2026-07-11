"""Tests for the account-wide scan (#9).

Constructed per-repo DAGs go through the real health/render/orchestration
logic; the DAG builder is injected as a plain function so per-repo failure
isolation is proven without stubbing a production boundary. One live proof
exercises the real repo-listing boundary against the disposable repo.
"""

from __future__ import annotations

from itree import cli
from itree.cli import collect_repo_healths, scan_payload
from itree.github import issue_bearing_repos, list_repos
from itree.models import GithubIssue, IssueState, PresentReportRef, RepoDag, RepoHealth, RepoRef
from itree.render import render_scan
from itree.validate import generate_doctor_report, repo_health


def _issue(
    number: int,
    title: str,
    body: str | None = None,
    state: IssueState = IssueState.open,
    labels: tuple[str, ...] = (),
) -> GithubIssue:
    return GithubIssue(
        id=number,
        number=number,
        title=title,
        state=state,
        html_url=f"https://github.com/o/r/issues/{number}",
        body=body,
        labels=labels,
    )


def _dag(
    slug: str,
    issues: dict[int, GithubIssue],
    children_of: dict[int, tuple[int, ...]],
) -> RepoDag:
    owner, repo = slug.split("/")
    return RepoDag(repo_ref=RepoRef(owner=owner, repo=repo), issues=issues, children_of=children_of)


ACCEPTANCE = "## Acceptance Criteria\n- done when ok"


def _clean_dag(slug: str) -> RepoDag:
    return _dag(slug, {1: _issue(1, f"Ledger: {slug}"), 2: _issue(2, "WU", body=ACCEPTANCE)}, {1: (2,)})


class TestRepoHealth:
    def test_clean_tree_reports_ok_and_next_work_unit(self) -> None:
        dag = _dag(
            "o/clean",
            {
                1: _issue(1, "Ledger: o/clean"),
                2: _issue(2, "First work unit", body=ACCEPTANCE),
                3: _issue(3, "Second work unit", body=ACCEPTANCE),
                4: _issue(4, "Old thing", state=IssueState.closed),
            },
            {1: (2, 3)},
        )
        health = repo_health(dag)
        assert health == RepoHealth(
            slug="o/clean",
            open_issues=3,
            root_status="ok",
            error_count=0,
            next_work_unit=PresentReportRef(ref=cli.parse_ref("o/clean#2")),
        )

    def test_no_open_root_candidate_reports_e001(self) -> None:
        # The only open issue hangs off a closed parent, so no open parentless
        # root candidate exists at all -> E001.
        dag = _dag(
            "o/noroot",
            {1: _issue(1, "Ledger: o/noroot", state=IssueState.closed), 2: _issue(2, "WU", body=ACCEPTANCE)},
            {1: (2,)},
        )
        health = repo_health(dag)
        assert health.root_status == "E001"
        assert health.error_count >= 1
        assert health.next_work_unit.kind == "absent"

    def test_two_roots_reports_e002(self) -> None:
        dag = _dag(
            "o/tworoots",
            {1: _issue(1, "Ledger: A"), 2: _issue(2, "Ledger: B")},
            {},
        )
        assert repo_health(dag).root_status == "E002"

    def test_root_not_ledger_reports_e004(self) -> None:
        dag = _dag(
            "o/badroot",
            {1: _issue(1, "Not a ledger"), 2: _issue(2, "WU", body=ACCEPTANCE)},
            {1: (2,)},
        )
        assert repo_health(dag).root_status == "E004"


class TestDeferralLabelIsLoadBearing:
    """The configured deferral_label flips a custom-labeled empty shelf between a
    dead-grouping warning (W030) and an intentional-shelf info (I010), at the
    real generate_doctor_report seam that doctor and scan thread it into.

    Proving it at that seam is the observable contract: repo_health condenses
    W030/I010 away (both are non-error), so the flip has no effect further up.
    """

    def _deferred_shelf_dag(self) -> RepoDag:
        # #2 is an open milestone ledger with no open descendants, labeled with
        # a *custom* label. The default "deferred" would NOT suppress it.
        return _dag(
            "o/deferred",
            {
                1: _issue(1, "Ledger: o/deferred"),
                2: _issue(2, "Milestone: Future work", labels=("epic",)),
                3: _issue(3, "Real work unit", body=ACCEPTANCE),
            },
            {1: (2, 3)},
        )

    def test_configured_label_suppresses_the_matching_shelf(self) -> None:
        report = generate_doctor_report(self._deferred_shelf_dag(), deferral_label="epic")
        codes = {f.code for f in report.findings}
        assert "I010" in codes and "W030" not in codes

    def test_default_label_does_not_suppress_a_custom_labeled_shelf(self) -> None:
        # Negative control: with the default label the "epic"-labeled shelf is a
        # dead grouping (W030), proving the label is load-bearing.
        report = generate_doctor_report(self._deferred_shelf_dag(), deferral_label="deferred")
        codes = {f.code for f in report.findings}
        assert "W030" in codes and "I010" not in codes


class TestIssueBearingRepos:
    def test_skips_zero_issue_and_issues_disabled_repos(self) -> None:
        # Real gh `repo list --json name,issues` payload shape; None means a
        # repo with issues disabled, which has no open issues to scan.
        payload: list[dict] = [
            {"name": "active", "issues": {"totalCount": 3}},
            {"name": "empty", "issues": {"totalCount": 0}},
            {"name": "issues-disabled", "issues": None},
        ]
        refs = issue_bearing_repos("o", payload)
        assert [r.slug for r in refs] == ["o/active"]


class TestRenderScan:
    def test_lines_and_worst_footer(self) -> None:
        healths = [
            repo_health(
                _dag(
                    "o/clean",
                    {1: _issue(1, "Ledger: o/clean"), 2: _issue(2, "WU", body=ACCEPTANCE)},
                    {1: (2,)},
                )
            ),
            repo_health(_dag("o/broken", {1: _issue(1, "Ledger: A"), 2: _issue(2, "Ledger: B")}, {})),
        ]
        out = render_scan(healths, fetch_errors=[("o/gone", "gh api failed: Not Found")])

        assert "o/clean" in out and "root ok" in out and "next #2" in out
        assert "root E002" in out
        assert "o/gone   ERROR: gh api failed: Not Found" in out or "ERROR: gh api failed: Not Found" in out
        # Both the ambiguous-root repo and the fetch-error repo are worst.
        assert "Worst repos (2)" in out
        assert "  o/broken" in out
        assert "  o/gone" in out

    def test_all_clean_footer(self) -> None:
        health = repo_health(_dag("o/clean", {1: _issue(1, "Ledger: o/clean"), 2: _issue(2, "WU", body=ACCEPTANCE)}, {1: (2,)}))
        out = render_scan([health], fetch_errors=[])
        assert "All scanned repos have a clean root and no errors." in out
        assert "Worst repos" not in out


class TestScanOrchestration:
    """collect_repo_healths runs each repo through real repo_health with an
    injected builder, isolating per-repo failures. The builder is a plain
    function returning real DAGs (or raising), never a mock."""

    def test_per_repo_fetch_failure_is_isolated_not_fatal(self) -> None:
        clean = RepoRef(owner="o", repo="clean")
        gone = RepoRef(owner="o", repo="gone")

        def build(ref: RepoRef) -> RepoDag:
            if ref.slug == "o/gone":
                raise RuntimeError("gh api failed: Not Found")
            return _clean_dag(ref.slug)

        healths, errors = collect_repo_healths((clean, gone), build, deferral_label="deferred")

        assert [h.slug for h in healths] == ["o/clean"]
        assert errors == [("o/gone", "gh api failed: Not Found")]

    def test_json_payload_shape(self) -> None:
        clean = RepoRef(owner="o", repo="clean")
        healths, errors = collect_repo_healths((clean,), lambda ref: _clean_dag(ref.slug), deferral_label="deferred")
        payload = scan_payload(healths, errors)
        assert payload["errors"] == []
        assert payload["repos"][0]["slug"] == "o/clean"
        assert payload["repos"][0]["root_status"] == "ok"


def test_live_list_repos_includes_the_disposable_repo() -> None:
    """The real gh repo-listing boundary surfaces the integration repo."""
    refs = list_repos("dzackgarza")
    assert RepoRef(owner="dzackgarza", repo="itree-e2e-scratch") in refs
