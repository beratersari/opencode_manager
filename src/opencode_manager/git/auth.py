"""Isolated git env: PAT never on argv."""

from __future__ import annotations

import base64
import os
from typing import Dict
from urllib.parse import urlparse

from opencode_manager.git.detect import classify_host


def _git_config_pairs(pairs: list[tuple[str, str]], env: Dict[str, str]) -> None:
    base = int(env.get("GIT_CONFIG_COUNT") or "0")
    for index, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{base + index}"] = key
        env[f"GIT_CONFIG_VALUE_{base + index}"] = value
    env["GIT_CONFIG_COUNT"] = str(base + len(pairs))


def isolated_git_env(repo_url: str, pat: str) -> Dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env.pop("DISPLAY", None)
    env.pop("SSH_ASKPASS", None)
    kind = classify_host(repo_url)
    parsed = urlparse(repo_url)
    host = parsed.hostname or ""
    scheme = parsed.scheme or "https"
    if not host or scheme == "file":
        _git_config_pairs([("credential.helper", "")], env)
        return env
    pairs: list[tuple[str, str]] = [("credential.helper", "")]
    token = (pat or "").strip()
    if not token:
        # Public HTTPS: no auth header. Helpers stay off so we never
        # fall back to the machine credential store.
        _git_config_pairs(pairs, env)
        return env
    if kind == "gitlab":
        basic = base64.b64encode(f"oauth2:{token}".encode("utf-8")).decode("ascii")
        src = f"{scheme}://{host}/"
        pairs.append((f"url.{scheme}://oauth2:{token}@{host}/.insteadOf", src))
        pairs.append(("http.extraHeader", f"Authorization: Basic {basic}"))
    else:
        basic = base64.b64encode(f":{token}".encode("utf-8")).decode("ascii")
        pairs.append(("http.extraHeader", f"Authorization: Basic {basic}"))
    _git_config_pairs(pairs, env)
    return env


def argv_helper_off() -> list[str]:
    return ["-c", "credential.helper="]
