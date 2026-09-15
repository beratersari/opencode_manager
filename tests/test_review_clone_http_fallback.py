"""Real git HTTP clone: PAT 401, then system credentials succeed or both fail."""

from __future__ import annotations

import base64
import shutil
import subprocess
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from opencode_manager.workspace import gitops as gitops_mod
from opencode_manager.workspace.gitops import GitError, clone_repo


def _git(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    cmd = [shutil.which("git") or "git", *args]
    result = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)


def _bare_http_repo(tmp: Path) -> Path:
    src = tmp / "src"
    src.mkdir()
    _git("init", cwd=src)
    _git("config", "user.email", "e2e@opencode-manager.test", cwd=src)
    _git("config", "user.name", "opencode-manager-e2e", cwd=src)
    (src / "README.md").write_text("review-clone-fallback\n", encoding="utf-8")
    _git("add", "README.md", cwd=src)
    _git("commit", "-m", "seed", cwd=src)
    bare = tmp / "repo.git"
    _git("clone", "--bare", str(src), str(bare))
    _git("--git-dir", str(bare), "update-server-info")
    return bare


def _serve(root: Path, *, reject_oauth2: bool, required: tuple[str, str] | None):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            return

        def _auth_ok(self) -> bool:
            header = self.headers.get("Authorization") or ""
            if header.lower().startswith("basic "):
                raw = base64.b64decode(header.split(None, 1)[1]).decode("utf-8")
                user, _, password = raw.partition(":")
                if reject_oauth2 and user == "oauth2":
                    return False
                if required is not None:
                    return (user, password) == required
                return True
            return required is None

        def do_GET(self) -> None:  # noqa: N802
            if not self._auth_ok():
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="git"')
                self.end_headers()
                return
            super().do_GET()

        def do_HEAD(self) -> None:  # noqa: N802
            if not self._auth_ok():
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="git"')
                self.end_headers()
                return
            super().do_HEAD()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    return httpd, f"http://{host}:{port}/repo.git"


@pytest.fixture
def no_gcm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the Linux-style helper path so tests never open a GCM dialog."""
    monkeypatch.setattr(gitops_mod, "uses_windows_stored_creds", lambda: False)


def test_http_clone_bad_pat_then_anonymous_system(tmp_path: Path, no_gcm: None) -> None:
    root = tmp_path / "www"
    root.mkdir()
    bare = _bare_http_repo(tmp_path)
    shutil.move(str(bare), str(root / "repo.git"))
    httpd, url = _serve(root, reject_oauth2=True, required=None)
    try:
        dest = tmp_path / "ws"
        clone_repo(url, dest, "bad-pat", timeout=30)
        assert (dest / "README.md").read_text(encoding="utf-8") == "review-clone-fallback\n"
        origin = subprocess.run(
            [shutil.which("git") or "git", "remote", "get-url", "origin"],
            cwd=dest,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "bad-pat" not in (origin.stdout or "")
        assert "oauth2:" not in (origin.stdout or "")
    finally:
        httpd.shutdown()


def test_http_clone_bad_pat_then_store_helper(tmp_path: Path, no_gcm: None, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    (home / ".gitconfig").write_text("[credential]\n\thelper = store\n", encoding="utf-8")

    root = tmp_path / "www"
    root.mkdir()
    bare = _bare_http_repo(tmp_path)
    shutil.move(str(bare), str(root / "repo.git"))
    httpd, url = _serve(root, reject_oauth2=True, required=("bot", "s3cret"))
    (home / ".git-credentials").write_text(f"{url.replace('http://', 'http://bot:s3cret@')}\n", encoding="utf-8")
    try:
        dest = tmp_path / "ws"
        clone_repo(url, dest, "bad-pat", timeout=30)
        assert (dest / "README.md").is_file()
    finally:
        httpd.shutdown()


def test_http_clone_bad_pat_and_no_system_creds_fails(tmp_path: Path, no_gcm: None) -> None:
    root = tmp_path / "www"
    root.mkdir()
    bare = _bare_http_repo(tmp_path)
    shutil.move(str(bare), str(root / "repo.git"))
    httpd, url = _serve(root, reject_oauth2=True, required=("bot", "s3cret"))
    try:
        dest = tmp_path / "ws"
        with pytest.raises(GitError, match="PAT and system credentials"):
            clone_repo(url, dest, "bad-pat", timeout=30)
        assert not dest.exists()
    finally:
        httpd.shutdown()
