"""Golden tests for the ASCII tree renderer — pure functions, no API."""

from __future__ import annotations

from itree.models import GithubIssue, IssueState, TreeNode
from itree.render import render_tree, shape_summary


def _issue(number: int, title: str, state: IssueState = IssueState.open) -> GithubIssue:
    return GithubIssue(
        id=number,
        number=number,
        title=title,
        state=state,
        html_url=f"https://github.com/t/t/issues/{number}",
    )


def _leaf(number: int, title: str, state: IssueState = IssueState.open) -> TreeNode:
    return TreeNode(issue=_issue(number, title, state), children=())


def _small_tree() -> TreeNode:
    return TreeNode(
        issue=_issue(1, "Ledger: t/t"),
        children=(
            TreeNode(
                issue=_issue(2, "Milestone: v1"),
                children=(_leaf(3, "Preview sync"), _leaf(4, "Export proof")),
            ),
            _leaf(5, "Old thing", state=IssueState.closed),
        ),
    )


def test_render_small_tree_golden() -> None:
    out = render_tree(_small_tree(), next_number=3)
    assert out == (
        "#1 Ledger: t/t  [root]\n"
        "└── #2 Milestone: v1  [grouping]\n"
        "    ├── #3 Preview sync  [WU]  <- next\n"
        "    └── #4 Export proof  [WU]\n"
        "\n"
        "(1 closed issue hidden; --all to show)\n"
        "shape: 4 open | 2 groupings | 2 work units | depth 3 | root fan-out 1 | next #3"
    )


def test_render_all_shows_closed() -> None:
    out = render_tree(_small_tree(), next_number=3, show_closed=True)
    assert "#5 Old thing  [closed]" in out
    assert "hidden" not in out


def test_render_flat_tree_truncates_children() -> None:
    """>10 children: first 6 shown, remainder collapsed with numbers visible."""
    kids = tuple(_leaf(n, f"Work {n}") for n in range(2, 14))  # 12 children
    root = TreeNode(issue=_issue(1, "Ledger: flat"), children=kids)
    out = render_tree(root, next_number=2)
    assert "├── #7 Work 7  [WU]" in out
    assert "#8 Work 8" not in out.split("... ")[0]
    assert "└── ... 6 more: #8 #9 #10 #11 #12 #13" in out
    assert "root fan-out 12" in out


def test_render_marks_work_unit_with_children_e015() -> None:
    root = TreeNode(
        issue=_issue(1, "Ledger: t/t"),
        children=(
            TreeNode(
                issue=_issue(2, "Stray parent work unit"),
                children=(_leaf(3, "Task child"),),
            ),
        ),
    )
    out = render_tree(root)
    assert "#2 Stray parent work unit  [WU]  !E015: has child issues" in out


def test_render_marks_duplicate_e013_once() -> None:
    dup = _leaf(9, "Shared child")
    root = TreeNode(
        issue=_issue(1, "Ledger: t/t"),
        children=(
            TreeNode(issue=_issue(2, "Milestone: a"), children=(dup,)),
            TreeNode(issue=_issue(3, "Milestone: b"), children=(dup,)),
        ),
    )
    out = render_tree(root)
    assert out.count("#9 Shared child") == 2
    assert out.count("!E013: duplicate") == 1


def test_render_deep_tree_prefixes() -> None:
    root = TreeNode(
        issue=_issue(1, "Ledger: deep"),
        children=(
            TreeNode(
                issue=_issue(2, "Milestone: a"),
                children=(TreeNode(issue=_issue(3, "Backlog: inner"), children=(_leaf(4, "Deep work"),)),),
            ),
            _leaf(5, "Sibling work"),
        ),
    )
    out = render_tree(root, next_number=4)
    assert "├── #2 Milestone: a  [grouping]" in out
    assert "│   └── #3 Backlog: inner  [grouping]" in out
    assert "│       └── #4 Deep work  [WU]  <- next" in out
    assert "└── #5 Sibling work  [WU]" in out


def test_shape_summary_no_next() -> None:
    root = _leaf(1, "Ledger: t/t")
    assert shape_summary(root, None) == "shape: 1 open | 1 groupings | 0 work units | depth 1 | root fan-out 0 | next none"
