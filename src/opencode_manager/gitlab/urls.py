"""Derive the GitLab API root and clone URL from a webhook payload."""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote, urlparse, urlunparse


def gitlab_api_root(url: str, path_with_namespace: str = "") -> str:
    """Host (+ optional /gitlab prefix) from a project or MR URL.

    ``https://gitlab.example/group/repo.git`` and
    ``https://gitlab.example/group/repo/-/merge_requests/1`` both become
    ``https://gitlab.example``. A relative GitLab at
    ``https://host/gitlab/group/repo`` keeps ``/gitlab`` when
    ``path_with_namespace`` is ``group/repo``.
    """
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    path = unquote(parsed.path or "").rstrip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    if "/-/" in path:
        path = path.split("/-/", 1)[0].rstrip("/")
    ns = unquote(str(path_with_namespace or "").strip()).strip("/")
    if ns:
        lower = path.lower()
        suffix = "/" + ns.lower()
        if lower.endswith(suffix):
            path = path[: -len(ns)].rstrip("/")
        elif lower == ns.lower():
            path = ""
    else:
        path = ""
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", "")).rstrip("/")


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def gitlab_http_url(payload: dict[str, Any]) -> str:
    """Clone URL from the hook (``project.http_url_to_repo``)."""
    if not isinstance(payload, dict):
        return ""
    blobs = (
        _as_dict(payload.get("project")),
        _as_dict(payload.get("repository")),
        _as_dict(_as_dict(payload.get("object_attributes")).get("source")),
        _as_dict(_as_dict(payload.get("merge_request")).get("source")),
    )
    for blob in blobs:
        for key in ("http_url_to_repo", "git_http_url", "git_http_url_to_repo"):
            text = str(blob.get(key) or "").strip()
            if text.lower().startswith("http"):
                return text
    return ""
