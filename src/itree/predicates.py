"""Shared predicates for issue classification.

This module breaks the circular dependency between ``validate.py`` and
``audit.py`` by providing grouping/acceptance predicates in a leaf module
that both can import without creating a cycle.
"""

from __future__ import annotations

import re


def parse_milestone_ledger_name(title: str) -> str | None:
    m = re.match(r"(?i)^milestone:\s*(?P<name>.+)$", title)
    if m:
        return m.group("name").strip()
    return None


def is_backlog_ledger(title: str) -> bool:
    return title.lower().startswith("backlog")


def is_root_ledger(title: str) -> bool:
    return title.lower().startswith("ledger:")


def is_roadmap_issue(title: str) -> bool:
    return title.lower().startswith("roadmap:")


def is_phase_issue(title: str) -> bool:
    return title.lower().startswith("phase:")


def is_grouping_issue(title: str) -> bool:
    return is_root_ledger(title) or parse_milestone_ledger_name(title) is not None or is_backlog_ledger(title) or is_roadmap_issue(title) or is_phase_issue(title)


def lacks_acceptance_criteria(body: str | None) -> bool:
    if not body:
        return True
    body_lower = body.lower()
    return not ("done when" in body_lower or "done criteria" in body_lower or "acceptance criteria" in body_lower or "acceptance" in body_lower)
