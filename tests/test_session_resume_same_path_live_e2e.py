"""LIVE e2e: cleanup + re-clone to the same path resumes the OpenCode session.

Hypothesis (see PLAN.md §3.3)
-----------------------------
OpenCode sessions live in the global ``opencode.db``, keyed by ``directory``.
If we:

1. clone a real repo to path P
2. start a real ``opencode serve``, create a session, send a real free-model turn
3. kill serve and hard-delete P
4. clone the same repo back to the **same** path P
5. start a new serve

then the old ``ses_*`` id must still resolve, history must still be there,
and a second real model turn on that id must succeed.

No mocks. No fakes. Real git, real folder, real ``opencode`` binary, real
free model.

Run::

    python3 -m pytest tests/test_session_resume_same_path_live_e2e.py -v -s
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest


SOURCE_BRANCH = "e2e-source"
# Live-probed 2026-08-27: hy3 / mimo / muse-spark reply. nemotron-* 404/502 on Zen.
PREFERRED_FREE_MODELS = (
    "opencode/hy3-free",
    "opencode/mimo-v2.5-free",
    "opencode/muse-spark-1.2-contributor-free",
)
SERVE_BOOT_TIMEOUT = 90.0
MODEL_TURN_TIMEOUT = 300.0


def _opencode_bin() -> Optional[str]:
    return shutil.which("opencode")


def _git_bin() -> Optional[str]:
    return shutil.which("git")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run(cmd: List[str], *, cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "command failed ({code}): {cmd}\nstdout:\n{out}\nstderr:\n{err}".format(
                code=result.returncode,
                cmd=" ".join(cmd),
                out=result.stdout[-2000:],
                err=result.stderr[-2000:],
            )
        )
    return result


def _git(*args: str, cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    git = _git_bin()
    if not git:
        raise RuntimeError("git is not on PATH")
    return _run([git, *args], cwd=cwd)


def _rmtree(path: Path) -> None:
    def _onerror(func, name, _exc):  # noqa: ARG001
        try:
            os.chmod(name, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            func(name)
        except OSError:
            pass

    if path.exists():
        shutil.rmtree(path, onerror=_onerror)
    if path.exists():
        raise OSError("hard delete left remnants at {0}".format(path))


def _seed_origin(origin: Path) -> None:
    """Create a real git repo with a named source branch (file:// remote)."""
    origin.mkdir(parents=True)
    # git 2.25 (this host) has no `init -b`; create then rename the first branch.
    _git("init", cwd=origin)
    _git("config", "user.email", "e2e@opencode-manager.test", cwd=origin)
    _git("config", "user.name", "opencode-manager-e2e", cwd=origin)
    _git("config", "init.defaultBranch", "main", cwd=origin)
    (origin / "README.md").write_text(
        "# opencode-manager live e2e fixture\n\n"
        "This file exists so the clone is a real project, not an empty dir.\n",
        encoding="utf-8",
    )
    _git("add", "README.md", cwd=origin)
    _git("commit", "-m", "initial commit", cwd=origin)
    _git("checkout", "-b", SOURCE_BRANCH, cwd=origin)
    (origin / "NOTE.txt").write_text(
        "source-branch marker for the live session-resume e2e\n",
        encoding="utf-8",
    )
    _git("add", "NOTE.txt", cwd=origin)
    _git("commit", "-m", "source branch marker", cwd=origin)


def _clone_to(origin: Path, dest: Path) -> None:
    if dest.exists():
        raise RuntimeError("clone dest already exists: {0}".format(dest))
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(
        "clone",
        "--branch",
        SOURCE_BRANCH,
        "--single-branch",
        str(origin),
        str(dest),
    )
    head = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=dest).stdout.strip()
    if head != SOURCE_BRANCH:
        raise RuntimeError("expected HEAD={0}, got {1}".format(SOURCE_BRANCH, head))


def _start_serve(port: int, cwd: Path, log_path: Path) -> subprocess.Popen:
    bin_path = _opencode_bin()
    if not bin_path:
        raise RuntimeError("opencode binary not on PATH")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_f = open(log_path, "w", encoding="utf-8")
    env = dict(os.environ)
    env["OPENCODE_SERVER_PASSWORD"] = ""
    proc = subprocess.Popen(
        [
            bin_path,
            "serve",
            "--pure",
            "--port",
            str(port),
            "--hostname",
            "127.0.0.1",
            "--print-logs",
            "--log-level",
            "INFO",
        ],
        cwd=str(cwd),
        stdout=log_f,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    proc._om_log_f = log_f  # type: ignore[attr-defined]
    return proc


def _stop_serve(proc: Optional[subprocess.Popen]) -> None:
    if proc is None:
        return
    pid = getattr(proc, "pid", None)
    if pid:
        try:
            os.killpg(int(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(int(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
    log_f = getattr(proc, "_om_log_f", None)
    if log_f is not None:
        try:
            log_f.close()
        except Exception:
            pass


def _wait_health(base: str, directory: str, timeout: float = SERVE_BOOT_TIMEOUT) -> Dict[str, Any]:
    deadline = time.time() + timeout
    last_err: Optional[Exception] = None
    headers = {"x-opencode-directory": directory}
    with httpx.Client(verify=False, timeout=5.0) as client:
        while time.time() < deadline:
            try:
                response = client.get(base.rstrip("/") + "/global/health", headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    if isinstance(data, dict):
                        return data
            except Exception as exc:
                last_err = exc
            time.sleep(0.4)
    raise TimeoutError("serve health not ready at {0}: {1}".format(base, last_err))


def _unwrap_session(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("id"), str):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        inner = payload["data"]
        if isinstance(inner.get("id"), str):
            return inner
    raise RuntimeError("unexpected session payload: {0!r}".format(payload))


def _collect_model_ids(payload: Any) -> List[str]:
    found: List[str] = []

    def _add(provider: str, model: str) -> None:
        provider = (provider or "").strip()
        model = (model or "").strip()
        if not model:
            return
        if "/" in model:
            found.append(model)
        elif provider:
            found.append("{0}/{1}".format(provider, model))
        else:
            found.append(model)

    if isinstance(payload, list):
        for row in payload:
            if not isinstance(row, dict):
                continue
            provider = str(row.get("id") or row.get("providerID") or row.get("provider") or "")
            models = row.get("models")
            if isinstance(models, dict):
                for mid, meta in models.items():
                    if isinstance(meta, dict):
                        _add(provider, str(meta.get("id") or mid))
                    else:
                        _add(provider, str(mid))
            elif isinstance(models, list):
                for item in models:
                    if isinstance(item, dict):
                        _add(provider, str(item.get("id") or item.get("modelID") or ""))
                    else:
                        _add(provider, str(item))
            elif row.get("id") and "/" in str(row.get("id")):
                found.append(str(row.get("id")))
        return found

    if not isinstance(payload, dict):
        return found

    for key in ("all", "connected", "providers", "data"):
        inner = payload.get(key)
        if inner is not None:
            found.extend(_collect_model_ids(inner))

    model = payload.get("model")
    if isinstance(model, str) and "/" in model:
        found.append(model)
    return found


def _cli_free_models() -> List[str]:
    bin_path = _opencode_bin()
    if not bin_path:
        return []
    try:
        result = subprocess.run(
            [bin_path, "models"],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except Exception:
        return []
    out: List[str] = []
    for line in (result.stdout or "").splitlines():
        name = line.strip()
        if name and "free" in name.lower():
            out.append(name)
    return out


def _pick_free_model(client: httpx.Client, headers: Dict[str, str]) -> Tuple[str, str]:
    inventory: List[str] = []
    for path in ("/config/providers", "/provider", "/config"):
        try:
            response = client.get(path, headers=headers)
        except Exception:
            continue
        if response.status_code >= 400:
            continue
        try:
            inventory.extend(_collect_model_ids(response.json()))
        except Exception:
            continue

    inventory.extend(_cli_free_models())
    # unique, keep order
    seen = set()
    models: List[str] = []
    for item in inventory:
        if item in seen:
            continue
        seen.add(item)
        models.append(item)

    free = [m for m in models if "free" in m.lower()]
    for preferred in PREFERRED_FREE_MODELS:
        if preferred in free or preferred in models:
            provider, model = preferred.split("/", 1)
            return provider, model
    if free:
        if "/" not in free[0]:
            raise RuntimeError("free model has no provider: {0}".format(free[0]))
        provider, model = free[0].split("/", 1)
        return provider, model
    raise RuntimeError(
        "no real free model found from live serve or `opencode models`. "
        "inventory={0!r}".format(models[:30])
    )


def _assistant_texts(payload: Any) -> List[str]:
    texts: List[str] = []
    if not isinstance(payload, dict):
        return texts
    parts = payload.get("parts") or []
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text") or ""
                if text:
                    texts.append(str(text))
    return texts


def _turn_error(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for blob in (payload, payload.get("info") if isinstance(payload.get("info"), dict) else None):
        if not isinstance(blob, dict):
            continue
        err = blob.get("error")
        if not err:
            continue
        if isinstance(err, dict):
            data = err.get("data") if isinstance(err.get("data"), dict) else {}
            msg = data.get("message") or err.get("message") or err.get("name")
            return str(msg or err)
        return str(err)
    return None


def _require_live_reply(payload: Any, *, label: str) -> List[str]:
    err = _turn_error(payload)
    if err:
        raise AssertionError("{0} model turn failed: {1}".format(label, err))
    texts = _assistant_texts(payload)
    if not texts:
        raise AssertionError(
            "{0} model turn returned no assistant text: {1!r}".format(label, payload)
        )
    return texts


def _message_user_texts(messages: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for message in messages:
        info = message.get("info") if isinstance(message.get("info"), dict) else message
        role = (info or {}).get("role") or message.get("role")
        if role != "user":
            continue
        parts = message.get("parts") or []
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text") or ""
                    if text:
                        out.append(str(text))
    return out


def _coerce_message_list(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("data", "messages", "items"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [row for row in inner if isinstance(row, dict)]
    return []


class LiveServe:
    """Thin real HTTP client for one ``opencode serve`` process."""

    def __init__(self, base: str, directory: str):
        self.base = base.rstrip("/")
        self.directory = directory
        self.headers = {"x-opencode-directory": directory}
        self.http = httpx.Client(base_url=self.base, verify=False, timeout=30.0)

    def close(self) -> None:
        self.http.close()

    def create_session(self, title: str) -> Dict[str, Any]:
        response = self.http.post("/session", json={"title": title}, headers=self.headers)
        response.raise_for_status()
        return _unwrap_session(response.json())

    def get_session(self, session_id: str) -> httpx.Response:
        return self.http.get("/session/{0}".format(session_id), headers=self.headers)

    def list_messages(self, session_id: str) -> List[Dict[str, Any]]:
        response = self.http.get(
            "/session/{0}/message".format(session_id),
            params={"limit": 200},
            headers=self.headers,
        )
        response.raise_for_status()
        return _coerce_message_list(response.json())

    def send_message(
        self,
        session_id: str,
        text: str,
        *,
        provider_id: str,
        model_id: str,
    ) -> Dict[str, Any]:
        body = {
            "agent": "build",
            "parts": [{"type": "text", "text": text}],
            "model": {"providerID": provider_id, "modelID": model_id},
        }
        response = self.http.post(
            "/session/{0}/message".format(session_id),
            json=body,
            headers=self.headers,
            timeout=httpx.Timeout(
                connect=15.0,
                read=float(MODEL_TURN_TIMEOUT),
                write=30.0,
                pool=15.0,
            ),
        )
        response.raise_for_status()
        return response.json() if response.content else {}

    def abort(self, session_id: str) -> None:
        try:
            self.http.post(
                "/session/{0}/abort".format(session_id),
                headers=self.headers,
                timeout=15.0,
            )
        except Exception:
            pass

    def pick_free_model(self) -> Tuple[str, str]:
        return _pick_free_model(self.http, self.headers)


@pytest.mark.live
def test_reclone_same_path_resumes_opencode_session(tmp_path: Path) -> None:
    """Delete the clone, clone again to the same path, resume the same ses_*."""
    if not _opencode_bin():
        pytest.fail("opencode binary not found on PATH — this live test cannot skip")
    if not _git_bin():
        pytest.fail("git binary not found on PATH — this live test cannot skip")

    origin = tmp_path / "origin"
    clone_path = (tmp_path / "workspace").resolve()
    marker = "OMGR-SAMEPATH-{0}".format(uuid.uuid4().hex[:8].upper())
    first_prompt = (
        "Do not use tools. Reply with exactly this token on its own line and "
        "nothing else: {0}".format(marker)
    )
    second_prompt = (
        "Do not use tools. Repeat the exact token from the previous user "
        "message in this same session. One line only."
    )

    print("\n[e2e] clone_path={0}".format(clone_path), flush=True)
    print("[e2e] marker={0}".format(marker), flush=True)

    _seed_origin(origin)
    _clone_to(origin, clone_path)
    assert clone_path.is_dir(), clone_path
    assert (clone_path / "NOTE.txt").is_file()

    port1 = _free_port()
    base1 = "http://127.0.0.1:{0}".format(port1)
    proc1: Optional[subprocess.Popen] = None
    serve1: Optional[LiveServe] = None
    session_id = ""
    provider_id = ""
    model_id = ""

    try:
        proc1 = _start_serve(port1, clone_path, tmp_path / "serve-1.log")
        health1 = _wait_health(base1, str(clone_path))
        print("[e2e] serve1 health={0}".format(health1), flush=True)
        assert health1.get("healthy") is True, health1

        serve1 = LiveServe(base1, str(clone_path))
        provider_id, model_id = serve1.pick_free_model()
        print("[e2e] free model={0}/{1}".format(provider_id, model_id), flush=True)
        assert "free" in model_id.lower() or "free" in provider_id.lower(), (
            "refusing to use a non-free model: {0}/{1}".format(provider_id, model_id)
        )

        created = serve1.create_session("opencode-manager same-path resume e2e")
        session_id = created["id"]
        print("[e2e] created session_id={0}".format(session_id), flush=True)
        assert session_id.startswith("ses_"), created

        first = serve1.send_message(
            session_id,
            first_prompt,
            provider_id=provider_id,
            model_id=model_id,
        )
        first_texts = _require_live_reply(first, label="first")
        print("[e2e] first assistant snip={0!r}".format(first_texts[0][:200]), flush=True)

        messages_before = serve1.list_messages(session_id)
        user_texts_before = _message_user_texts(messages_before)
        assert any(marker in text for text in user_texts_before), (
            "first turn did not persist the marker in session history: {0!r}".format(
                user_texts_before
            )
        )
        serve1.abort(session_id)
    finally:
        if serve1 is not None:
            serve1.close()
        _stop_serve(proc1)

    print("[e2e] deleting clone at {0}".format(clone_path))
    _rmtree(clone_path)
    assert not clone_path.exists(), "clone still on disk after cleanup"

    print("[e2e] re-cloning to the same path")
    _clone_to(origin, clone_path)
    assert clone_path.resolve() == clone_path
    assert (clone_path / "NOTE.txt").is_file()

    port2 = _free_port()
    base2 = "http://127.0.0.1:{0}".format(port2)
    proc2: Optional[subprocess.Popen] = None
    serve2: Optional[LiveServe] = None
    try:
        proc2 = _start_serve(port2, clone_path, tmp_path / "serve-2.log")
        health2 = _wait_health(base2, str(clone_path))
        print("[e2e] serve2 health={0}".format(health2))
        assert health2.get("healthy") is True, health2

        serve2 = LiveServe(base2, str(clone_path))
        got = serve2.get_session(session_id)
        print("[e2e] GET /session/{0} -> {1}".format(session_id, got.status_code))
        assert got.status_code == 200, (
            "old session id was not accepted after re-clone to the same path: "
            "status={0} body={1}".format(got.status_code, got.text[:800])
        )
        resumed = _unwrap_session(got.json())
        assert resumed.get("id") == session_id, resumed
        resumed_dir = resumed.get("directory") or ""
        if resumed_dir:
            assert Path(str(resumed_dir)).resolve() == clone_path, (
                "session directory {0!r} != clone path {1}".format(resumed_dir, clone_path)
            )

        messages_after = serve2.list_messages(session_id)
        user_texts_after = _message_user_texts(messages_after)
        assert any(marker in text for text in user_texts_after), (
            "history lost after cleanup+reclone. session={0} users={1!r}".format(
                session_id, user_texts_after
            )
        )

        second = serve2.send_message(
            session_id,
            second_prompt,
            provider_id=provider_id,
            model_id=model_id,
        )
        second_texts = _require_live_reply(second, label="resume")
        print("[e2e] second assistant snip={0!r}".format(second_texts[0][:200]), flush=True)

        # Same id must still be the one we talk to — we never POST /session again.
        got_again = serve2.get_session(session_id)
        assert got_again.status_code == 200, got_again.text[:500]
        print(
            json.dumps(
                {
                    "session_id": session_id,
                    "clone_path": str(clone_path),
                    "model": "{0}/{1}".format(provider_id, model_id),
                    "history_kept_marker": True,
                    "resumed": True,
                },
                indent=2,
            )
        )
        serve2.abort(session_id)
    finally:
        if serve2 is not None:
            serve2.close()
        _stop_serve(proc2)
        if clone_path.exists():
            try:
                _rmtree(clone_path)
            except OSError:
                pass
