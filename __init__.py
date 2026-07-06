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
from .validate import TreeViolation, validate_dag, validate_tree

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
    "TreeViolation",
    "build_dag",
    "validate_dag",
    "validate_tree",
]
