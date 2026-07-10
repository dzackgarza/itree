"""Golden tests for the account-wide scan (#9): fake per-repo graphs in,
health lines and worst-repo footer out. No network."""

from __future__ import annotations

import pytest

from itree import cli, validate
from itree.cli import scan
from itree.metrics import MetricsConfig
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


class TestScanHonorsConfiguredDeferralLabel:
    """The account-scan health path must apply the SAME configured deferral_label
    as doctor. Config is now read once at the CLI command boundary and threaded
    in as repo_health(dag, deferral_label=...); repo_health no longer reads
    config itself.

    A grouping carrying the configured label with no open descendants is an
    intentional shelf (I010), not a dead grouping (W030). A scan that ignored
    the configured label (used the "deferred" default while config set a custom
    one) would flag a custom-labeled shelf as W030 instead.
    """

    def _deferred_shelf_dag(self) -> RepoDag:
        # #2 is an open milestone ledger with no open descendants, labeled with
        # the *custom* deferral label. Default "deferred" would NOT suppress it.
        return _dag(
            "o/deferred",
            {
                1: _issue(1, "Ledger: o/deferred"),
                2: _issue(2, "Milestone: Future work", labels=("epic",)),
                3: _issue(3, "Real work unit", body=ACCEPTANCE),
            },
            {1: (2, 3)},
        )

    def _spy_on_report(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
        # Spy: wrap the REAL generate_doctor_report so its true findings still
        # drive RepoHealth, but the label it received and the codes it produced
        # are observable. repo_health condenses findings away, so the flip
        # between W030 and I010 is only visible on the real report.
        captured: dict[str, object] = {}
        real_report = validate.generate_doctor_report

        def capturing(dag: RepoDag, deferral_label: str = "deferred") -> object:
            report = real_report(dag, deferral_label=deferral_label)
            captured["label"] = deferral_label
            captured["codes"] = {f.code for f in report.findings}
            return report

        monkeypatch.setattr(validate, "generate_doctor_report", capturing)
        return captured

    def test_repo_health_forwards_explicit_label_flipping_w030_to_i010(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = self._spy_on_report(monkeypatch)

        repo_health(self._deferred_shelf_dag(), deferral_label="epic")

        # repo_health forwarded its explicit label to generate_doctor_report,
        # which then treated the "epic"-labeled shelf as an intentional shelf.
        assert captured["label"] == "epic"
        codes = captured["codes"]
        assert isinstance(codes, set)
        assert "I010" in codes and "W030" not in codes

    def test_scan_loads_config_once_and_applies_configured_label(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        # Real scan/health_of wiring: scan must read load_config once at the
        # command boundary and pass config.deferral_label into every repo_health
        # call. A scan that ignored config (default "deferred") would produce
        # W030, not I010, and the captured label would be "deferred".
        calls = {"count": 0}

        def counting_load_config(*a: object, **k: object) -> MetricsConfig:
            calls["count"] += 1
            return MetricsConfig(deferral_label="epic")

        monkeypatch.setattr(cli, "load_config", counting_load_config)
        monkeypatch.setattr(cli, "list_repos", lambda owner: (RepoRef(owner="o", repo="deferred"),))
        monkeypatch.setattr(cli, "build_dag", lambda ref, *a, **k: self._deferred_shelf_dag())
        captured = self._spy_on_report(monkeypatch)

        scan("o")

        assert calls["count"] == 1
        assert captured["label"] == "epic"
        codes = captured["codes"]
        assert isinstance(codes, set)
        assert "I010" in codes and "W030" not in codes

    def test_default_label_does_not_suppress_a_custom_labeled_shelf(self) -> None:
        # Negative control: with the default label the "epic"-labeled shelf is a
        # dead grouping (W030), proving the label is load-bearing and that
        # generate_doctor_report keeps its explicit deferral_label parameter.
        report = generate_doctor_report(self._deferred_shelf_dag(), deferral_label="deferred")
        codes = {f.code for f in report.findings}
        assert "W030" in codes and "I010" not in codes


class TestListRepos:
    def test_skips_zero_issue_and_issues_disabled_repos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import json
        from unittest.mock import MagicMock

        from itree import github

        payload = [
            {"name": "active", "issues": {"totalCount": 3}},
            {"name": "empty", "issues": {"totalCount": 0}},
            {"name": "issues-disabled", "issues": None},  # gh returns null here
        ]
        proc = MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")
        monkeypatch.setattr(github.subprocess, "run", lambda *a, **k: proc)

        refs = github.list_repos("o")
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


class TestScanCommand:
    def test_scan_renders_a_line_per_repo_and_isolates_fetch_errors(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        dags = {
            "o/clean": _dag(
                "o/clean",
                {1: _issue(1, "Ledger: o/clean"), 2: _issue(2, "WU", body=ACCEPTANCE)},
                {1: (2,)},
            ),
        }

        def fake_build_dag(repo_ref: RepoRef, *args: object, **kwargs: object) -> RepoDag:
            if repo_ref.slug == "o/gone":
                raise RuntimeError("gh api failed: Not Found")
            return dags[repo_ref.slug]

        monkeypatch.setattr(cli, "list_repos", lambda owner: (RepoRef(owner="o", repo="clean"), RepoRef(owner="o", repo="gone")))
        monkeypatch.setattr(cli, "build_dag", fake_build_dag)

        scan("o")
        out = capsys.readouterr().out
        assert "o/clean" in out
        assert "ERROR: gh api failed: Not Found" in out
        assert "o/gone" in out

    def test_scan_json_shape(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        import json

        dag = _dag("o/clean", {1: _issue(1, "Ledger: o/clean"), 2: _issue(2, "WU", body=ACCEPTANCE)}, {1: (2,)})
        monkeypatch.setattr(cli, "list_repos", lambda owner: (RepoRef(owner="o", repo="clean"),))
        monkeypatch.setattr(cli, "build_dag", lambda *a, **k: dag)

        scan("o", as_json=True)
        payload = json.loads(capsys.readouterr().out)
        assert payload["errors"] == []
        assert payload["repos"][0]["slug"] == "o/clean"
        assert payload["repos"][0]["root_status"] == "ok"
