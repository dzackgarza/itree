"""itree: Deterministic traversal layer over GitHub sub-issue trees."""

from .cli import app
from .github import GithubApi
from .models import (
    AttachRequest,
    DetachRequest,
    GithubIssue,
    IssueCloseReason,
    IssueRef,
    IssueState,
    MoveRequest,
    RepoDag,
    RepoRef,
    TreeNode,
)
from .traversal import build_dag
from .validate import DIAGNOSTIC_CATALOG, generate_doctor_report

__all__ = [
    "app",
    "GithubApi",
    "IssueRef",
    "RepoRef",
    "RepoDag",
    "GithubIssue",
    "TreeNode",
    "IssueState",
    "IssueCloseReason",
    "AttachRequest",
    "DetachRequest",
    "MoveRequest",
    "DIAGNOSTIC_CATALOG",
    "build_dag",
    "generate_doctor_report",
]
