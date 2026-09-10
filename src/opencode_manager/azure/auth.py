"""Azure DevOps Server accepts PAT as a Basic password. IIS rejects an empty username."""

from __future__ import annotations

import base64


def azure_basic_user(username: str = "") -> str:
    return (username or "").strip() or "pat"


def azure_basic_auth(token: str, username: str = "") -> str:
    raw = f"{azure_basic_user(username)}:{(token or '')}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")
