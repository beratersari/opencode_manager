"""Authorization / PRIVATE-TOKEN / leftover argv must not persist secrets."""

from __future__ import annotations

import logging
from pathlib import Path

from opencode_manager.azure.auth import azure_basic_auth
from opencode_manager.cleanup import kill as kill_mod
from opencode_manager.log import clip, fmt_cmd, redact, setup_logging
from opencode_manager.workspace.gitops import isolated_git_env


_PROBE_PAT = "AZURE_PROBE_PAT_7a1b_DO_NOT_KEEP"


def test_redact_strips_basic_bearer_and_private_token() -> None:
    header = azure_basic_auth(_PROBE_PAT)
    blob = header.split()[-1]
    argv = (
        "git -c credential.helper= -c http.extraHeader=Authorization: "
        f"{header} -c core.askPass=/tmp/ask"
    )
    cleaned = redact(argv)
    assert _PROBE_PAT not in cleaned
    assert blob not in cleaned
    assert "Authorization: Basic ***" in cleaned

    bearer = redact("Authorization: Bearer sk-live-SUPER-SECRET")
    assert "sk-live-SUPER-SECRET" not in bearer
    assert "Authorization: Bearer ***" in bearer

    lower = redact("authorization: bearer abc.def")
    assert "abc.def" not in lower

    token = redact("PRIVATE-TOKEN: glpat-LEAKME")
    assert "glpat-LEAKME" not in token
    assert "PRIVATE-TOKEN: ***" in token


def test_fmt_cmd_and_clip_redact_authorization() -> None:
    header = azure_basic_auth(_PROBE_PAT)
    blob = header.split()[-1]
    line = fmt_cmd(["git", "-c", f"http.extraHeader=Authorization: {header}", "status"])
    assert blob not in line
    assert _PROBE_PAT not in line
    clipped = clip(f"Authorization: Basic {blob}", limit=80)
    assert blob not in clipped


def test_redact_leaves_plain_text_alone() -> None:
    assert redact("job finished status=success") == "job finished status=success"
    assert redact("") == ""
    assert redact("Authorization required") == "Authorization required"


def test_userinfo_redact_still_works() -> None:
    assert "***:***@" in redact("https://oauth2:tok@host/r.git")
    assert "tok" not in redact("https://oauth2:tok@host/r.git")
    assert "***@" in redact("https://PATONLY@host/r.git")
    assert ":***@" in redact("https://:colonpat@host/r.git")


def test_azure_git_argv_has_no_extraheader(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    env = isolated_git_env(_PROBE_PAT, auth_scheme="azure")
    assert env.get("GIT_CONFIG_KEY_0") != "http.extraHeader"
    assert _PROBE_PAT not in str(env)
    from opencode_manager.workspace import gitops as gitops_mod

    captured: dict = {}

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        captured["cmd"] = list(cmd)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(gitops_mod.subprocess, "run", fake_run)
    gitops_mod._run_git(["status"], env=env, timeout=1.0)
    joined = " ".join(str(p) for p in captured["cmd"])
    assert "extraHeader" not in joined
    assert "Authorization" not in joined
    assert _PROBE_PAT not in joined
    blob = azure_basic_auth(_PROBE_PAT).split()[-1]
    assert blob not in joined


def test_reap_leftover_log_is_stem_not_argv(tmp_path: Path, monkeypatch) -> None:
    header = azure_basic_auth(_PROBE_PAT)
    blob = header.split()[-1]
    argv = (
        "git -c credential.helper= -c http.extraHeader=Authorization: "
        f"{header} -c core.askPass=/tmp/ask"
    )
    root = tmp_path / "workspaces" / "1-2"
    root.mkdir(parents=True)
    app_log = tmp_path / "app.log"
    setup_logging(job_log_dir=tmp_path / "logs", app_log=app_log, level="INFO")
    logger = logging.getLogger("opencode_manager")
    logger.info(
        "reap leftover pid=%s cwd=%s argv_stem=%s root=%s",
        12345,
        str(root),
        kill_mod._image_stem(argv),
        str(root),
    )
    text = app_log.read_text(encoding="utf-8")
    assert _PROBE_PAT not in text
    assert blob not in text
    assert "argv_stem=git" in text
    assert "Authorization: Basic" not in text

    killed: list[int] = []
    monkeypatch.setattr(kill_mod, "iter_processes", lambda **k: [
        kill_mod.ProcInfo(pid=424243, cwd=str(root), argv=argv),
    ])
    monkeypatch.setattr(kill_mod, "may_kill", lambda pid: True)
    monkeypatch.setattr(kill_mod, "kill_pid", lambda pid: killed.append(pid))
    monkeypatch.setattr(kill_mod, "protected_pids", lambda: set())
    orig = kill_mod.os.name
    try:
        kill_mod.os.name = "posix"
        assert kill_mod.reap_path(root) >= 1
    finally:
        kill_mod.os.name = orig
    text2 = app_log.read_text(encoding="utf-8")
    assert blob not in text2
    assert _PROBE_PAT not in text2
    assert "argv_stem=git" in text2
    assert killed == [424243]
