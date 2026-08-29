"""Classify clone URLs: GitLab vs Azure DevOps / TFS."""

from __future__ import annotations

from urllib.parse import urlparse


def classify_host(repo_url: str) -> str:
    parsed = urlparse(repo_url.strip())
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if any(part in path for part in ("/_git/", "/tfs/")):
        return "tfs"
    if host in {"dev.azure.com", "visualstudio.com"} or host.endswith(".visualstudio.com"):
        return "azure"
    if "dev.azure.com" in host:
        return "azure"
    return "gitlab"
