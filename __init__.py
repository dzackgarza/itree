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
    RepoRef,
    TreeNode,
)
from .traversal import get_descendants_preorder, get_direct_children, materialize, next_leaf
from .validate import TreeViolation, full_validate, validate_tree

__all__ = [
    "app",
    "GithubApi",
    "IssueRef",
    "RepoRef",
    "GithubIssue",
    "TreeNode",
    "IssueState",
    "IssueCloseReason",
    "AttachRequest",
    "DetachRequest",
    "MoveRequest",
    "TreeViolation",
    "materialize",
    "next_leaf",
    "get_direct_children",
    "get_descendants_preorder",
    "validate_tree",
    "full_validate",
]
