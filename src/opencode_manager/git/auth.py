"""Isolated git env: clone the request URL as-is. No PAT rewrite."""

from __future__ import annotations

import base64
import os
import subprocess
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

from opencode_manager.log import get_logger

logger = get_logger()

_AUTH_NEEDLES = (
    "could not read username",
    "terminal prompts disabled",
    "authentication failed",
    "invalid username or password",
    "http basic",
    "authorization required",
    "access denied",
    "401",
    "403 unauthorized",
)

_job_creds: Dict[str, Tuple[str, str]] = {}


def _git_config_pairs(pairs: list[tuple[str, str]], env: Dict[str, str]) -> None:
    base = int(env.get("GIT_CONFIG_COUNT") or "0")
    for index, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{base + index}"] = key
        env[f"GIT_CONFIG_VALUE_{base + index}"] = value
    env["GIT_CONFIG_COUNT"] = str(base + len(pairs))


def uses_windows_stored_creds() -> bool:
    """True on Windows: GCM/wincred stored creds, or a login popup."""
    return os.name == "nt"


def host_from_repo_url(repo_url: str) -> str:
    return (urlparse(repo_url).hostname or repo_url or "").strip()


def is_git_auth_error(message: str) -> bool:
    text = (message or "").lower()
    if "could not resolve host" in text or "timed out" in text:
        return False
    return any(needle in text for needle in _AUTH_NEEDLES)


def remember_job_creds(job_id: str, username: str, password: str) -> None:
    if job_id and username:
        _job_creds[job_id] = (username, password)


def creds_for_job(job_id: Optional[str]) -> Optional[Tuple[str, str]]:
    if not job_id:
        return None
    return _job_creds.get(job_id)


def forget_job_creds(job_id: Optional[str]) -> None:
    if job_id:
        _job_creds.pop(job_id, None)


def isolated_git_env(*, username: str = "", password: str = "") -> Dict[str, str]:
    """Direct clone env. Windows: GCM, or Basic from a dialog. Linux: helper off."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Pointer files only. Do not download LFS blobs (smudge).
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    env.pop("DISPLAY", None)
    env.pop("SSH_ASKPASS", None)
    user = (username or "").strip()
    secret = password or ""
    if user and secret:
        # One-shot dialog creds. Do not put user:pass on argv.
        env["GIT_ASKPASS"] = ""
        basic = base64.b64encode(f"{user}:{secret}".encode("utf-8")).decode("ascii")
        _git_config_pairs(
            [
                ("credential.helper", ""),
                ("http.extraHeader", f"Authorization: Basic {basic}"),
            ],
            env,
        )
        return env
    if uses_windows_stored_creds():
        env.pop("GIT_ASKPASS", None)
        env["GCM_INTERACTIVE"] = "auto"
        env["GCM_MODAL_PROMPT"] = "true"
        _git_config_pairs([("credential.helper", "manager")], env)
        return env
    env["GIT_ASKPASS"] = ""
    _git_config_pairs([("credential.helper", "")], env)
    return env


def argv_helper_off() -> list[str]:
    """Never pass -c credential.helper= on Windows unless we already have dialog creds."""
    if uses_windows_stored_creds():
        return []
    return ["-c", "credential.helper="]


def prompt_windows_credentials(host: str) -> Optional[Tuple[str, str]]:
    """Modal Windows username/password dialog. None if cancelled or not Windows."""
    if not uses_windows_stored_creds():
        return None
    target = host or "git"
    logger.info("opening Windows credential dialog host=%s", target)
    script = (
        "$h = $env:OSM_GIT_HOST; "
        "$c = Get-Credential -Message ('Enter git username and password for ' + $h); "
        "if (-not $c) { exit 2 }; "
        "Write-Output $c.UserName; "
        "Write-Output $c.GetNetworkCredential().Password;"
    )
    env = os.environ.copy()
    env["OSM_GIT_HOST"] = target
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", script],
            capture_output=True,
            timeout=300,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("Windows credential dialog failed err=%s", type(exc).__name__)
        return None
    if result.returncode != 0:
        logger.info("Windows credential dialog cancelled or failed")
        return None
    lines = (result.stdout or b"").decode("utf-8", errors="replace").splitlines()
    if len(lines) < 2:
        logger.info("Windows credential dialog returned no username/password")
        return None
    user = lines[0].strip()
    password = "\n".join(lines[1:])
    if not user or password is None:
        return None
    logger.info("Windows credential dialog accepted user=%s", user)
    return user, password
