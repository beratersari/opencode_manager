"""Real checks of the Windows git retry, /api/auth probe, and Linux overlay rewrite.

No mocks. Git is a real git. HTTP is a real uvicorn listener. The install
rewrite is the awk in scripts/osm-lib.sh, run by the real awk binary.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import uvicorn

from opencode_manager.app import create_app
from opencode_manager.git.auth import isolated_git_env, is_git_auth_error
from opencode_manager.git.clone import (
    GitError,
    _clone_dest_from_args,
    _fetch_into_existing_dest,
    clone_repo,
)
from opencode_manager.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
WSL_DISTRO = "Ubuntu-24.04"


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)
    return (result.stdout or "").strip()


def _seed_origin(root: Path) -> Path:
    """main is default HEAD. develop exists but is not checked out."""
    root.mkdir(parents=True)
    _git("init", cwd=root)
    _git("config", "user.email", "t@t.test", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    _git("checkout", "-B", "main", cwd=root)
    (root / "README.md").write_text("on-main\n", encoding="utf-8")
    _git("add", "README.md", cwd=root)
    _git("commit", "-m", "main", cwd=root)
    _git("checkout", "-b", "develop", cwd=root)
    (root / "NOTE.txt").write_text("on-develop\n", encoding="utf-8")
    _git("add", "NOTE.txt", cwd=root)
    _git("commit", "-m", "develop", cwd=root)
    _git("checkout", "main", cwd=root)
    return root


def _file_url(path: Path) -> str:
    return path.resolve().as_uri()


def _settings(tmp: Path) -> Settings:
    settings = Settings(
        listen_host="127.0.0.1",
        listen_port=0,
        max_concurrent_jobs=1,
        callback_timeout_seconds=2.0,
        callback_retry_count=1,
        data_dir=tmp / "data",
        work_dir=tmp / "work",
        job_log_dir=tmp / "joblogs",
        job_store_dir=tmp / "jobs",
        queue_path=tmp / "queue.json",
        log_level="INFO",
        hang_timeout_seconds=15.0,
        git_clone_timeout_seconds=60.0,
        project_root=tmp,
        dashboard_user="admin",
        dashboard_password="admin",
        dashboard_token="change_me",
        gitlab_webhook_secret="tank",
    )
    settings.ensure_dirs()
    dist = tmp / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html>SPA</html>", encoding="utf-8")
    return settings


class _Live:
    def __init__(self, settings: Settings) -> None:
        self.app = create_app(settings)
        self.config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=0,
            log_level="error",
            lifespan="on",
        )
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, name="osm-claim-http", daemon=True)
        self.thread.start()
        deadline = time.time() + 8
        while time.time() < deadline and not self.server.started:
            time.sleep(0.02)
        if not self.server.started:
            raise RuntimeError("uvicorn did not start")
        sock = self.server.servers[0].sockets[0]
        self.port = int(sock.getsockname()[1])
        self.base = f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=12)


def _wsl_available() -> bool:
    wsl = shutil.which("wsl")
    if not wsl:
        return False
    try:
        listed = subprocess.run(
            [wsl, "-l", "-q"],
            capture_output=True,
            text=True,
            errors="replace",
        )
    except OSError:
        return False
    blob = ((listed.stdout or "") + (listed.stderr or "")).replace("\x00", "")
    return WSL_DISTRO in blob


def _can_run_bash() -> bool:
    if os.name != "nt":
        return shutil.which("bash") is not None and shutil.which("awk") is not None
    return _wsl_available()


def _bash_path(path: Path) -> str:
    if os.name == "nt":
        return _win_to_wsl(path)
    return str(path.resolve())


def _run_bash_file(script: Path) -> subprocess.CompletedProcess[str]:
    if os.name == "nt":
        return _wsl("bash", _win_to_wsl(script))
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"bash {script} rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


def _win_to_wsl(path: Path) -> str:
    text = str(path.resolve()).replace("\\", "/")
    if len(text) >= 2 and text[1] == ":":
        return f"/mnt/{text[0].lower()}{text[2:]}"
    return text


def _wsl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = ["wsl", "-d", WSL_DISTRO, "--", *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"wsl {' '.join(args)!r} rc={result.returncode}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


def _lf_copy(src: Path, dest: Path) -> Path:
    """Linux bash cannot source a CRLF checkout of osm-lib.sh."""
    dest.write_text(src.read_text(encoding="utf-8").replace("\r\n", "\n"), encoding="utf-8", newline="\n")
    return dest


def _osm_awk_program() -> str:
    lib = (ROOT / "scripts" / "osm-lib.sh").read_text(encoding="utf-8")
    match = re.search(
        r"awk -v d=\"\$fallback\"\s+'([^']+)'\s+\"\$local_yaml\"",
        lib,
    )
    assert match, "osm_ensure_linux_data_dir awk program missing from osm-lib.sh"
    return match.group(1)


def test_kan401_dest_exists_stderr_is_not_git_auth(tmp_path: Path) -> None:
    src = _seed_origin(tmp_path / "src")
    dest = tmp_path / "KAN-401"
    dest.mkdir()
    (dest / "stale").write_text("leftover", encoding="utf-8")
    with pytest.raises(GitError) as caught:
        clone_repo(_file_url(src), dest, timeout=30.0)
    err = str(caught.value)
    assert "already exists" in err.lower()
    assert "KAN-401" in err
    assert is_git_auth_error(err) is False


def test_git_clone_refuses_nonempty_dest_allows_empty(tmp_path: Path) -> None:
    src = _seed_origin(tmp_path / "src")
    url = _file_url(src)
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "stale").write_text("x", encoding="utf-8")
    bad = subprocess.run(
        ["git", "clone", url, str(nonempty)],
        capture_output=True,
        text=True,
    )
    assert bad.returncode != 0
    assert "already exists" in (bad.stderr or "").lower()
    assert is_git_auth_error(bad.stderr or "") is False

    empty = tmp_path / "empty"
    empty.mkdir()
    good = subprocess.run(
        ["git", "clone", url, str(empty)],
        capture_output=True,
        text=True,
    )
    assert good.returncode == 0, good.stderr
    assert (empty / "README.md").read_text(encoding="utf-8") == "on-main\n"
    assert not (empty / "NOTE.txt").exists()


def test_fetch_into_existing_git_dest_uses_origin_head_not_source_branch(
    tmp_path: Path,
) -> None:
    src = _seed_origin(tmp_path / "src")
    dest = tmp_path / "AUD-9"
    dest.mkdir()
    _git("init", cwd=dest)
    _git("remote", "add", "origin", _file_url(src), cwd=dest)
    marker = dest / "do-not-delete.txt"
    marker.write_text("keep", encoding="utf-8")

    env = isolated_git_env()
    result = _fetch_into_existing_dest(
        dest,
        origin_url=_file_url(src),
        env=env,
        timeout=30.0,
    )
    assert result.returncode == 0
    assert dest.exists()
    assert marker.exists()
    assert (dest / "README.md").read_text(encoding="utf-8") == "on-main\n"
    assert not (dest / "NOTE.txt").exists()
    head = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=dest)
    assert head in {"HEAD", "main"}
    assert _git("rev-parse", "HEAD", cwd=dest) == _git("rev-parse", "main", cwd=src)


def test_fetch_into_existing_dest_does_not_keep_develop_checkout(tmp_path: Path) -> None:
    src = _seed_origin(tmp_path / "src")
    dest = tmp_path / "work" / "TICKET-9"
    clone_repo(_file_url(src), dest, "develop", timeout=30.0)
    _git("checkout", "develop", cwd=dest)
    assert (dest / "NOTE.txt").is_file()

    _fetch_into_existing_dest(
        dest,
        origin_url=_file_url(src),
        env=isolated_git_env(),
        timeout=30.0,
    )
    assert dest.exists()
    assert (dest / "README.md").is_file()
    assert not (dest / "NOTE.txt").exists()


def test_fetch_into_empty_non_git_dest_fails(tmp_path: Path) -> None:
    dest = tmp_path / "empty-leftover"
    dest.mkdir()
    with pytest.raises(GitError, match="not a git repository"):
        _fetch_into_existing_dest(
            dest,
            origin_url="https://example.test/r.git",
            env=isolated_git_env(),
            timeout=10.0,
        )
    assert dest.exists()


def test_clone_dest_from_args_reads_last_clone_arg(tmp_path: Path) -> None:
    dest = tmp_path / "KAN-401"
    assert _clone_dest_from_args(["clone", "https://example.test/r.git", str(dest)]) == dest
    assert _clone_dest_from_args(["ls-remote", "--heads", "https://example.test/r.git"]) is None
    assert _clone_dest_from_args(["clone", "https://example.test/r.git"]) is None


def test_shipped_overlay_makes_unauthenticated_meta_401_auth_200(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    live = _Live(settings)
    try:
        meta_err = None
        try:
            urllib.request.urlopen(f"{live.base}/api/meta", timeout=5)
        except urllib.error.HTTPError as exc:
            meta_err = exc
        assert meta_err is not None
        assert meta_err.code == 401

        with urllib.request.urlopen(f"{live.base}/api/auth", timeout=5) as res:
            body = res.read().decode("utf-8")
            assert res.status == 200
        compact = body.replace(" ", "")
        assert '"required":true' in compact
        assert '"authenticated":false' in compact
    finally:
        live.close()


def test_start_script_probes_urlopen_and_powershell_match_auth_not_meta(
    tmp_path: Path,
) -> None:
    """curl -sf / urllib / Invoke-WebRequest fail on 401. /api/auth is 200."""
    settings = _settings(tmp_path)
    live = _Live(settings)
    try:
        meta = f"{live.base}/api/meta"
        auth = f"{live.base}/api/auth"

        urlopen_meta = subprocess.run(
            [
                sys.executable,
                "-c",
                f"import urllib.request; urllib.request.urlopen({meta!r}, timeout=5)",
            ],
            capture_output=True,
            text=True,
        )
        urlopen_auth = subprocess.run(
            [
                sys.executable,
                "-c",
                f"import urllib.request; urllib.request.urlopen({auth!r}, timeout=5)",
            ],
            capture_output=True,
            text=True,
        )
        assert urlopen_meta.returncode != 0, urlopen_meta.stderr
        assert urlopen_auth.returncode == 0, urlopen_auth.stderr

        if os.name == "nt":
            ps_meta = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"try {{ Invoke-WebRequest -Uri '{meta}' -UseBasicParsing -TimeoutSec 5 | Out-Null; exit 0 }} catch {{ exit 1 }}",
                ],
                capture_output=True,
                text=True,
            )
            ps_auth = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"try {{ Invoke-WebRequest -Uri '{auth}' -UseBasicParsing -TimeoutSec 5 | Out-Null; exit 0 }} catch {{ exit 1 }}",
                ],
                capture_output=True,
                text=True,
            )
            assert ps_meta.returncode != 0, ps_meta.stderr
            assert ps_auth.returncode == 0, ps_auth.stderr

        curl = shutil.which("curl")
        if curl:
            curl_meta = subprocess.run(
                [curl, "-sf", "--max-time", "5", meta],
                capture_output=True,
                text=True,
            )
            curl_auth = subprocess.run(
                [curl, "-sf", "--max-time", "5", auth],
                capture_output=True,
                text=True,
            )
            assert curl_meta.returncode != 0
            assert curl_auth.returncode == 0, curl_auth.stderr
    finally:
        live.close()


@pytest.mark.skipif(not _can_run_bash(), reason="needs bash+awk (Linux or WSL Ubuntu-24.04)")
def test_osm_awk_rewrites_only_data_dir_keeps_shipped_keys(tmp_path: Path) -> None:
    overlay = tmp_path / "settings.local.yaml"
    overlay.write_text(
        (ROOT / "packaging" / "settings.local.linux.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
        newline="\n",
    )
    fallback = "/home/tester/.local/share/osm"
    prog = _osm_awk_program()
    awk_file = tmp_path / "rewrite.awk"
    awk_file.write_text(prog, encoding="utf-8", newline="\n")
    script = tmp_path / "rewrite.sh"
    script.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        f"overlay={_bash_path(overlay)!r}\n"
        f"fallback={fallback!r}\n"
        f"awkfile={_bash_path(awk_file)!r}\n"
        "tmp=\"$overlay.tmp\"\n"
        "awk -v d=\"$fallback\" -f \"$awkfile\" \"$overlay\" > \"$tmp\" && mv \"$tmp\" \"$overlay\"\n",
        encoding="utf-8",
        newline="\n",
    )
    _run_bash_file(script)
    text = overlay.read_text(encoding="utf-8")
    assert f"data_dir: {fallback}" in text
    assert "data_dir: /var/lib/osm" not in text
    assert "dashboard_token: change_me" in text
    assert "dashboard_password: admin" in text
    assert "dashboard_user: admin" in text
    assert "gitlab_webhook_secret: tank" in text
    assert "max_concurrent_n8n_jobs: 2" in text
    assert "max_concurrent_reviews: 2" in text
    assert "review_agent: code-reviewer" in text


@pytest.mark.skipif(not _can_run_bash(), reason="needs bash (Linux or WSL Ubuntu-24.04)")
def test_osm_custom_data_dir_is_left_alone(tmp_path: Path) -> None:
    root = tmp_path / "payload"
    root.mkdir()
    overlay = root / "settings.local.yaml"
    overlay.write_text(
        "data_dir: /home/berat/custom-osm\n"
        "dashboard_token: change_me\n"
        "gitlab_webhook_secret: tank\n",
        encoding="utf-8",
        newline="\n",
    )
    before = overlay.read_text(encoding="utf-8")
    script = tmp_path / "run.sh"
    lib = _bash_path(_lf_copy(ROOT / "scripts" / "osm-lib.sh", tmp_path / "osm-lib.sh"))
    payload = _bash_path(root)
    script.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        f". {lib!r}\n"
        f"osm_ensure_linux_data_dir {payload!r}\n",
        encoding="utf-8",
        newline="\n",
    )
    out = _run_bash_file(script)
    assert "left as-is" in (out.stdout or "")
    assert overlay.read_text(encoding="utf-8") == before


@pytest.mark.skipif(not _can_run_bash(), reason="needs bash (Linux or WSL Ubuntu-24.04)")
def test_osm_linux_default_detector_and_missing_overlay_keys(tmp_path: Path) -> None:
    lib = _bash_path(_lf_copy(ROOT / "scripts" / "osm-lib.sh", tmp_path / "osm-lib.sh"))
    shipped = tmp_path / "shipped.yaml"
    shipped.write_text(
        (ROOT / "packaging" / "settings.local.linux.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
        newline="\n",
    )
    custom = tmp_path / "custom.yaml"
    custom.write_text("data_dir: /tmp/other-osm\n", encoding="utf-8", newline="\n")
    check = tmp_path / "check.sh"
    check.write_text(
        "#!/bin/bash\n"
        "set -uo pipefail\n"
        f". {lib!r}\n"
        f"osm_local_yaml_is_linux_default {_bash_path(shipped)!r}\n"
        "echo shipped:$?\n"
        f"osm_local_yaml_is_linux_default {_bash_path(custom)!r}\n"
        "echo custom:$?\n",
        encoding="utf-8",
        newline="\n",
    )
    out = _run_bash_file(check)
    text = out.stdout or ""
    assert "shipped:0" in text
    assert "custom:1" in text

    lib_text = (ROOT / "scripts" / "osm-lib.sh").read_text(encoding="utf-8")
    marker = "osm_ensure_linux_data_dir()"
    assert marker in lib_text
    fn = lib_text.split(marker, 1)[1]
    heredoc = fn.split("<<EOF", 1)[1].split("EOF", 1)[0]
    assert "dashboard_token: change_me" in heredoc
    assert "gitlab_webhook_secret: tank" in heredoc
    assert "dashboard_password: admin" in heredoc
    assert "max_concurrent_n8n_jobs: 2" in heredoc
