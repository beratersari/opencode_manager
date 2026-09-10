"""Windows-safe queue key for an Azure DevOps pull request."""

from __future__ import annotations

import hashlib

from opencode_manager.workspace.identity import mr_key


def azure_project_num(project_id: str, repo_id: str) -> int:
    """Stable int so existing ``{project}-{iid}`` folders and cancel still work."""
    digest = hashlib.sha1(f"{project_id}:{repo_id}".encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def azure_mr_key(project_id: str, repo_id: str, pr_id: int) -> str:
    return mr_key(azure_project_num(project_id, repo_id), int(pr_id))