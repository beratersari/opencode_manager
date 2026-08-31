"""Create/resume sessions, post prompts, poll until idle, assess the turn."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from opencode_manager.log import clip, get_logger, log_fail, log_http
from opencode_manager.models import parse_model

logger = get_logger()


def known_model_ids_from_payload(data: Any) -> List[str]:
    """Extract ``provider/model`` ids from GET /config/providers or /provider."""
    out: List[str] = []

    def _add(provider: str, model: str) -> None:
        prov = (provider or "").strip()
        mid = (model or "").strip()
        if not mid:
            return
        if "/" in mid and not prov:
            out.append(mid)
            return
        if prov:
            out.append(f"{prov}/{mid}")
        else:
            out.append(mid)

    def _walk_provider(pid: str, body: Any) -> None:
        if not isinstance(body, dict):
            return
        models = body.get("models")
        if isinstance(models, dict):
            for mid in models:
                if isinstance(mid, str):
                    _add(pid, mid)
        elif isinstance(models, list):
            for item in models:
                if isinstance(item, str):
                    _add(pid, item)
                elif isinstance(item, dict):
                    _add(pid, str(item.get("id") or item.get("modelID") or ""))

    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict):
                pid = str(row.get("id") or row.get("providerID") or "")
                _walk_provider(pid, row)
        return out

    if not isinstance(data, dict):
        return out

    providers = data.get("providers")
    if providers is None:
        providers = data.get("all")
    if isinstance(providers, dict):
        for pid, body in providers.items():
            _walk_provider(str(pid), body)
    elif isinstance(providers, list):
        for row in providers:
            if not isinstance(row, dict):
                continue
            pid = str(row.get("id") or row.get("providerID") or "")
            _walk_provider(pid, row)

    default = data.get("default")
    if isinstance(default, dict):
        for pid, mid in default.items():
            if isinstance(mid, str) and mid.strip():
                _add(str(pid), mid)
    return out


def model_is_known(requested: str, known: Sequence[str]) -> bool:
    """True when ``requested`` is in the serve inventory (or inventory is empty)."""
    req = (requested or "").strip()
    if not req:
        return True
    bag = {(k or "").strip() for k in known if (k or "").strip()}
    if not bag:
        return True
    if req in bag:
        return True
    tail = req.split("/", 1)[-1]
    tails = {k.split("/", 1)[-1] for k in bag}
    if "/" not in req:
        return tail in tails
    return tail in bag


def unknown_model_message(requested: str, known: Sequence[str]) -> str:
    sample = ", ".join(list(known)[:12])
    more = f" (+{len(known) - 12} more)" if len(known) > 12 else ""
    extra = f" Available: {sample}{more}." if sample else ""
    return f"model {requested!r} is not available on this OpenCode serve.{extra}"

QUESTION_HINTS = (
    "shall i",
    "do you want",
    "would you like",
    "which option",
    "please confirm",
    "can you confirm",
    "what should i",
)


def session_is_busy(status: Any, session_id: Optional[str]) -> bool:
    if not isinstance(status, dict) or not session_id:
        return False

    def _row_busy(row: Any) -> bool:
        if not isinstance(row, dict):
            return False
        kind = str(
            row.get("type") or row.get("status") or row.get("state") or row.get("phase") or ""
        ).strip().lower()
        if kind in {
            "busy",
            "busy_compacting",
            "compacting",
            "retry",
            "running",
            "in_progress",
            "in-progress",
            "active",
            "working",
            "processing",
            "message",
        }:
            return True
        if row.get("busy") is True or row.get("running") is True:
            return True
        if "compact" in kind:
            return True
        return False

    def _row_kind(row: Any) -> str:
        if not isinstance(row, dict):
            return ""
        return str(row.get("type") or row.get("status") or row.get("state") or "").lower()

    row = status.get(session_id)
    if row is None and isinstance(status.get("data"), dict):
        row = status["data"].get(session_id)
    if _row_busy(row):
        return True
    if _row_busy(status):
        return True
    for key in ("data", "sessions", "items", "status"):
        lst = status.get(key)
        if not isinstance(lst, list):
            continue
        for item in lst:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("id") or item.get("sessionID") or item.get("session_id") or "")
            if sid == session_id and _row_busy(item):
                return True
    return False


def session_is_compacting(
    status: Any,
    session_id: Optional[str],
    *,
    session_info: Any = None,
) -> bool:
    """True when this session is in OpenCode compact.

    ``GET /session/status`` is only ``idle`` | ``retry`` | ``busy``. Compact
    is ``Session.time.compacting`` on ``GET /session/:id``, not a status type.
    """
    if not session_id:
        return False
    if isinstance(status, dict):

        def _compact(row: Any) -> bool:
            if not isinstance(row, dict):
                return False
            kind = str(row.get("type") or row.get("status") or row.get("state") or "").lower()
            return "compact" in kind

        if _compact(status.get(session_id)) or _compact(status):
            return True
    if isinstance(session_info, dict):
        inner = session_info.get("time") if isinstance(session_info.get("time"), dict) else session_info
        if isinstance(inner, dict) and inner.get("compacting"):
            return True
    return False


def _unwrap_session(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("id"), str):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        inner = payload["data"]
        if isinstance(inner.get("id"), str):
            return inner
    raise RuntimeError(f"unexpected session payload: {payload!r}")


def _coerce_messages(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("data", "messages", "items"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [row for row in inner if isinstance(row, dict)]
    return []


def _info(message: Dict[str, Any]) -> Dict[str, Any]:
    info = message.get("info")
    return info if isinstance(info, dict) else message


def turn_has_new_assistant(messages: List[Dict[str, Any]], baseline_id: str) -> bool:
    got = last_assistant_id(messages)
    return bool(got and got != (baseline_id or ""))


def last_assistant_id(messages: List[Dict[str, Any]]) -> str:
    for message in reversed(messages):
        info = _info(message)
        if (info.get("role") or message.get("role")) != "assistant":
            continue
        return str(info.get("id") or message.get("id") or "")
    return ""


def last_assistant_text(messages: List[Dict[str, Any]]) -> str:
    texts: List[str] = []
    for message in messages:
        info = _info(message)
        if (info.get("role") or message.get("role")) != "assistant":
            continue
        parts = message.get("parts") or info.get("parts") or []
        if not isinstance(parts, list):
            continue
        chunk: List[str] = []
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                chunk.append(str(part["text"]))
        if chunk:
            texts.append("\n".join(chunk))
    return texts[-1] if texts else ""


def _last_finish(messages: List[Dict[str, Any]]) -> Tuple[str, str]:
    if not messages:
        return "", ""
    last = messages[-1]
    info = _info(last)
    role = str(info.get("role") or last.get("role") or "")
    finish = str(info.get("finish") or last.get("finish") or "")
    return role, finish.lower()


def looks_like_question(text: str) -> bool:
    lowered = (text or "").lower()
    if "?" in lowered and any(hint in lowered for hint in QUESTION_HINTS):
        return True
    if lowered.strip().endswith("?") and len(lowered) < 800:
        return True
    return False


def compact_marker_count(messages: List[Dict[str, Any]]) -> int:
    count = 0
    for message in messages:
        blob = str(message).lower()
        if "compact" in blob or "session auto-compacted" in blob:
            count += 1
    return count


def assess_idle(messages: List[Dict[str, Any]]) -> str:
    """Return success | question | incomplete | compact_leftover."""
    if not messages:
        return "incomplete"
    role, finish = _last_finish(messages)
    text = last_assistant_text(messages)
    last = messages[-1]
    blob = str(last).lower()
    if "compact" in blob and role != "assistant":
        return "compact_leftover"
    if role == "assistant" and looks_like_question(text):
        return "question"
    # Only a clean last-turn `stop` is success. Idle + unfinished finish
    # (tool-calls, length / max tokens, null, user last, …) is incomplete.
    if finish == "stop":
        return "success"
    return "incomplete"


class OpenCodeClient:
    def __init__(self, base_url: str, directory: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.directory = directory
        self.headers = {"x-opencode-directory": directory}
        self.http = httpx.Client(base_url=self.base_url, verify=False, timeout=30.0)

    def close(self) -> None:
        self.http.close()

    def health(self) -> bool:
        try:
            response = self.http.get("/global/health", headers=self.headers, timeout=5.0)
            return response.status_code == 200
        except Exception:
            return False

    def list_known_models(self) -> List[str]:
        """Model ids this serve will accept (``provider/id``). Empty if unknown."""
        ids: List[str] = []
        for path in ("/config/providers", "/provider"):
            try:
                response = self.http.get(path, headers=self.headers, timeout=15.0)
            except Exception:
                continue
            if response.status_code >= 400:
                continue
            try:
                ids.extend(known_model_ids_from_payload(response.json()))
            except Exception:
                continue
            if ids:
                break
        seen = set()
        out: List[str] = []
        for mid in ids:
            if mid in seen:
                continue
            seen.add(mid)
            out.append(mid)
        return out

    def session_payload(self, session_id: str) -> Dict[str, Any]:
        """GET /session/:id body. Empty on error. No per-poll log line."""
        if not session_id:
            return {}
        try:
            response = self.http.get(f"/session/{session_id}", headers=self.headers, timeout=10.0)
        except Exception:
            return {}
        if response.status_code != 200:
            return {}
        try:
            data = response.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def get_session(self, session_id: str) -> httpx.Response:
        path = f"/session/{session_id}"
        try:
            response = self.http.get(path, headers=self.headers, timeout=15.0)
        except Exception as exc:  # noqa: BLE001
            log_http(logger, "GET", path, err=exc, ok=False)
            raise
        log_http(logger, "GET", path, status=response.status_code, body=response.text if response.status_code >= 400 else None)
        return response

    def create_session(self, title: str) -> str:
        logger.info("POST /session title=%s", clip(title, 120))
        try:
            response = self.http.post("/session", json={"title": title}, headers=self.headers, timeout=30.0)
        except Exception as exc:  # noqa: BLE001
            log_http(logger, "POST", "/session", err=exc, ok=False)
            raise
        if response.status_code >= 400:
            log_http(logger, "POST", "/session", status=response.status_code, body=response.text)
            response.raise_for_status()
        sid = str(_unwrap_session(response.json())["id"])
        log_http(logger, "POST", "/session", status=response.status_code)
        return sid

    def resume_or_create(self, inbound: Optional[str], title: str) -> Tuple[str, bool]:
        """Return (session_id, created_new)."""
        if inbound and inbound.startswith("ses_"):
            got = self.get_session(inbound)
            logger.info("GET /session/%s -> %s", inbound, got.status_code)
            if got.status_code == 200:
                return inbound, False
            logger.info("inbound session_id rejected (%s); creating new", got.status_code)
        elif inbound:
            logger.info("inbound session_id is not ses_*; creating new")
        else:
            logger.info("no inbound session_id; creating new")
        sid = self.create_session(title)
        logger.info("POST /session created %s", sid)
        return sid, True

    def status(self) -> Dict[str, Any]:
        try:
            response = self.http.get("/session/status", headers=self.headers, timeout=10.0)
            if response.status_code == 200:
                data = response.json()
                return data if isinstance(data, dict) else {}
        except Exception as exc:  # noqa: BLE001
            logger.debug("status poll failed: %s", exc)
        return {}

    def list_messages(self, session_id: str) -> List[Dict[str, Any]]:
        path = f"/session/{session_id}/message"
        try:
            response = self.http.get(
                path,
                params={"limit": 400},
                headers=self.headers,
                timeout=30.0,
            )
        except Exception as exc:  # noqa: BLE001
            log_http(logger, "GET", path, params={"limit": 400}, err=exc, ok=False)
            raise
        if response.status_code >= 400:
            log_http(logger, "GET", path, status=response.status_code, body=response.text, params={"limit": 400})
            response.raise_for_status()
        return _coerce_messages(response.json())

    def post_message(
        self,
        session_id: str,
        text: str,
        *,
        model: str,
        agent: str,
    ) -> None:
        import threading

        provider, model_id = parse_model(model)
        body = {
            "agent": agent,
            "parts": [{"type": "text", "text": text}],
            "model": {"providerID": provider, "modelID": model_id},
        }
        try:
            response = self.http.post(
                f"/session/{session_id}/prompt_async",
                json=body,
                headers=self.headers,
                timeout=20.0,
            )
            if response.status_code < 400:
                log_http(logger, "POST", f"/session/{session_id}/prompt_async", status=response.status_code)
                logger.info(
                    "user message accepted via prompt_async HTTP %s model=%s/%s agent=%s chars=%s",
                    response.status_code,
                    provider,
                    model_id,
                    agent,
                    len(text),
                )
                return
            log_http(
                logger,
                "POST",
                f"/session/{session_id}/prompt_async",
                status=response.status_code,
                body=response.text,
            )
            logger.info("prompt_async -> HTTP %s; falling back to /message", response.status_code)
        except Exception as exc:  # noqa: BLE001
            log_http(logger, "POST", f"/session/{session_id}/prompt_async", err=exc, ok=False)
            logger.info("prompt_async failed (%s); falling back to /message", exc)

        fail: list[str] = []

        def _send() -> None:
            timeout = httpx.Timeout(connect=15.0, read=600.0, write=30.0, pool=15.0)
            try:
                with httpx.Client(base_url=self.base_url, verify=False, timeout=timeout) as http:
                    response = http.post(
                        f"/session/{session_id}/message",
                        json=body,
                        headers=self.headers,
                    )
                    if response.status_code >= 400:
                        fail.append(f"HTTP {response.status_code} {clip(response.text, 200)}")
                        log_http(
                            logger,
                            "POST",
                            f"/session/{session_id}/message",
                            status=response.status_code,
                            body=response.text,
                        )
                    else:
                        log_http(logger, "POST", f"/session/{session_id}/message", status=response.status_code)
                        logger.info("user message accepted via /message HTTP %s", response.status_code)
            except Exception as exc:  # noqa: BLE001
                fail.append(str(exc))
                log_http(logger, "POST", f"/session/{session_id}/message", err=exc, ok=False)

        thread = threading.Thread(target=_send, name="osm-message", daemon=True)
        thread.start()
        deadline = time.time() + 45.0
        while time.time() < deadline:
            if fail:
                raise RuntimeError(fail[0])
            if session_is_busy(self.status(), session_id):
                logger.info("user message accepted (session went busy)")
                return
            try:
                messages = self.list_messages(session_id)
            except Exception:
                messages = []
            for message in reversed(messages):
                info = _info(message)
                if (info.get("role") or message.get("role")) != "user":
                    continue
                parts = message.get("parts") or info.get("parts") or []
                blob = " ".join(
                    str(p.get("text") or "")
                    for p in parts
                    if isinstance(p, dict)
                )
                if text[:80] and text[:80] in blob:
                    logger.info("user message accepted (seen in session history)")
                    return
                break
            if not thread.is_alive() and not fail:
                return
            time.sleep(0.3)
        if fail:
            raise RuntimeError(fail[0])
        raise RuntimeError("user message POST was not accepted by OpenCode")

    def abort(self, session_id: str) -> None:
        path = f"/session/{session_id}/abort"
        try:
            response = self.http.post(path, headers=self.headers, timeout=15.0)
            log_http(logger, "POST", path, status=response.status_code, body=response.text if response.status_code >= 400 else None)
        except Exception as exc:  # noqa: BLE001
            log_fail(logger, "abort session failed", session_id=session_id, err=exc)


def _part_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        inner = value.get("text") or value.get("value") or value.get("content")
        if isinstance(inner, str):
            return inner
        try:
            return str(inner) if inner is not None else ""
        except Exception:
            return ""
    return str(value)


def _snapshot_part(part: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten an OpenCode part. Tool results live under ``state``."""
    kind = str(part.get("type") or "text").lower()
    if kind in {"thinking"}:
        kind = "reasoning"
    if kind in {"compact"}:
        kind = "compaction"
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    text = _part_text(part.get("text") or part.get("content") or state.get("text"))
    output = _part_text(state.get("output") or part.get("output") or state.get("error"))
    status = str(state.get("status") or part.get("status") or "")
    tool = str(part.get("tool") or part.get("name") or state.get("title") or "")
    inp = state.get("input") if state.get("input") is not None else part.get("input")
    item: Dict[str, Any] = {
        "id": str(part.get("id") or ""),
        "type": kind,
        "text": text,
        "tool": tool,
        "status": status,
        "output": output,
    }
    if isinstance(inp, dict):
        item["input"] = inp
    elif inp is not None and str(inp):
        item["input"] = {"value": str(inp)}
    if kind == "compaction" and not text:
        item["text"] = "Session compacted"
    return item


def snapshot_chat(messages: List[Dict[str, Any]], session_id: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for index, message in enumerate(messages):
        info = _info(message)
        role = str(info.get("role") or message.get("role") or "unknown")
        mid = str(info.get("id") or message.get("id") or f"msg_{index}")
        parts_in = message.get("parts") or info.get("parts") or []
        parts: List[Dict[str, Any]] = []
        if isinstance(parts_in, list):
            for part in parts_in:
                if isinstance(part, dict):
                    parts.append(_snapshot_part(part))
        out.append(
            {
                "id": mid,
                "session_id": session_id,
                "role": role,
                "finish": info.get("finish"),
                "created_at": info.get("time") or info.get("created_at"),
                "parts": parts,
            }
        )
    return out
