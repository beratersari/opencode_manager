from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Optional

import httpx

from opencode_manager.review_log import get_logger, log_fail, log_ok
from opencode_manager.review.prompt import HANG_RESUME

logger = get_logger("session")

_REVIEW_SUMMARY = re.compile(r"^###\s+Summary\b", re.IGNORECASE | re.MULTILINE)
_REVIEW_TITLE = re.compile(r"^####\s+\d+\.\s+`", re.MULTILINE)
_FINDINGS_TAG = "opencoderman-findings"


class OpenCodeError(RuntimeError):
    def __init__(self, message: str, *, timeout: bool = False, status_code: int = 0) -> None:
        super().__init__(message)
        self.timeout = timeout
        self.status_code = status_code


def parse_model(model: str) -> tuple[str, str]:
    provider, _, name = (model or "").strip().partition("/")
    return provider, name


def _role(message: dict[str, Any]) -> str:
    info = message.get("info") if isinstance(message.get("info"), dict) else message
    if not isinstance(info, dict):
        info = message
    return str(info.get("role") or message.get("role") or "").lower()


def _text_parts(message: dict[str, Any]) -> list[str]:
    info = message.get("info") if isinstance(message.get("info"), dict) else message
    parts = message.get("parts") or (info.get("parts") if isinstance(info, dict) else None) or []
    chunk: list[str] = []
    if not isinstance(parts, list):
        return chunk
    for part in parts:
        if not isinstance(part, dict):
            continue
        kind = str(part.get("type") or "text").lower()
        if kind not in {"text", "output", ""}:
            continue
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        text = part.get("text") or part.get("content") or state.get("text") or ""
        if text:
            chunk.append(str(text))
    return chunk


def last_assistant_text(messages: list[dict[str, Any]]) -> str:
    texts: list[str] = []
    for message in messages:
        if not isinstance(message, dict) or _role(message) != "assistant":
            continue
        chunk = _text_parts(message)
        if chunk:
            texts = chunk
    return "\n".join(texts).strip()


def looks_like_review(text: str) -> bool:
    """True when the body is a review, not a trailing wrap-up."""
    body = text or ""
    if _FINDINGS_TAG in body:
        return True
    if _REVIEW_SUMMARY.search(body) or _REVIEW_TITLE.search(body):
        return True
    return False


def turn_assistant_text(
    messages: list[dict[str, Any]],
    *,
    prefer_review: bool = True,
) -> str:
    """Assistant markdown from this job turn, not the whole ``ses_*``.

    A later wrap-up ("the review is complete") must not replace the
    review that already landed in this turn. Hang-resume user messages
    are not a new turn — they continue the prompt already posted.
    Does not walk back into a previous job's messages.
    """
    if not messages:
        return ""
    last_user = -1
    last_any_user = -1
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or _role(message) != "user":
            continue
        last_any_user = index
        if "\n".join(_text_parts(message)).strip() == HANG_RESUME.strip():
            continue
        last_user = index
    if last_user < 0:
        last_user = last_any_user
    if last_user < 0:
        return last_assistant_text(messages)
    texts = [
        "\n".join(_text_parts(message)).strip()
        for message in messages[last_user + 1 :]
        if isinstance(message, dict) and _role(message) == "assistant" and _text_parts(message)
    ]
    texts = [item for item in texts if item]
    if not texts:
        return ""
    if prefer_review:
        reviews = [item for item in texts if looks_like_review(item)]
        if reviews:
            return reviews[-1]
    return texts[-1]


def session_activity(messages: list[dict[str, Any]]) -> tuple[int, int, bool]:
    """Message count, part count, and whether structured output is present."""
    parts_n = 0
    has_structured = False
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        info = message.get("info") if isinstance(message.get("info"), dict) else message
        if isinstance(info, dict) and (info.get("structured_output") or info.get("structuredOutput")):
            has_structured = True
        parts = message.get("parts") or (info.get("parts") if isinstance(info, dict) else None) or []
        if not isinstance(parts, list):
            continue
        parts_n += len(parts)
        for part in parts:
            if not isinstance(part, dict):
                continue
            tool = str(part.get("tool") or part.get("name") or part.get("type") or "").lower()
            if "structured" in tool:
                has_structured = True
    return (len(messages or []), parts_n, has_structured)


def _snapshot_part(part: dict[str, Any]) -> dict[str, Any]:
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    raw_input = part.get("input") if isinstance(part.get("input"), dict) else None
    if raw_input is None and isinstance(state.get("input"), dict):
        raw_input = state["input"]
    output = part.get("output")
    if output is None:
        output = state.get("output")
    row: dict[str, Any] = {
        "id": str(part.get("id") or ""),
        "type": str(part.get("type") or "text"),
        "text": str(part.get("text") or state.get("text") or ""),
        "tool": str(part.get("tool") or part.get("name") or ""),
        "status": str(part.get("status") or state.get("status") or ""),
        "output": "" if output is None else str(output),
    }
    if raw_input:
        row["input"] = raw_input
    return row


def snapshot_chat(messages: list[dict[str, Any]], session_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        info = message.get("info") if isinstance(message.get("info"), dict) else message
        role = str(info.get("role") or message.get("role") or "unknown")
        mid = str(info.get("id") or message.get("id") or f"msg_{index}")
        parts_in = message.get("parts") or info.get("parts") or []
        parts: list[dict[str, Any]] = []
        if isinstance(parts_in, list):
            for part in parts_in:
                if isinstance(part, dict):
                    parts.append(_snapshot_part(part))
        structured = None
        if isinstance(info, dict):
            structured = info.get("structured_output") or info.get("structuredOutput")
        if structured is None:
            structured = message.get("structured_output") or message.get("structuredOutput")
        if structured is not None:
            blob = structured if isinstance(structured, str) else json.dumps(structured)
            parts.append({"type": "structured_output", "text": blob, "tool": "StructuredOutput"})
        created = info.get("time") if isinstance(info, dict) else None
        if created is None:
            created = message.get("time") or message.get("created_at")
        row = {"id": mid, "session_id": session_id, "role": role, "parts": parts}
        if created is not None:
            row["created_at"] = created
        out.append(row)
    return out


def fetch_live_chat(base_url: str, directory: str, session_id: str) -> list[dict[str, Any]]:
    """Pull the current session transcript from a running opencode serve."""
    client = OpenCodeClient(base_url, directory)
    try:
        return snapshot_chat(client.list_messages(session_id), session_id)
    finally:
        client.close()


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

    def get_session(self, session_id: str) -> httpx.Response:
        return self.http.get(f"/session/{session_id}", headers=self.headers, timeout=15.0)

    def create_session(self, title: str) -> str:
        try:
            response = self.http.post("/session", json={"title": title}, headers=self.headers, timeout=60.0)
            response.raise_for_status()
        except Exception as exc:
            log_fail(logger, "opencode create session", title=title, err=exc)
            raise
        data = response.json()
        if isinstance(data, dict) and isinstance(data.get("id"), str):
            log_ok(logger, "opencode create session", session=data["id"], http=response.status_code)
            return data["id"]
        inner = data.get("session") if isinstance(data, dict) else None
        if isinstance(inner, dict) and inner.get("id"):
            sid = str(inner["id"])
            log_ok(logger, "opencode create session", session=sid, http=response.status_code)
            return sid
        log_fail(logger, "opencode create session", title=title, reason="no id")
        raise OpenCodeError("session create returned no id")

    def resume_or_create(self, inbound: Optional[str], title: str) -> tuple[str, bool]:
        if inbound and inbound.startswith("ses_"):
            try:
                got = self.get_session(inbound)
                if got.status_code == 200:
                    log_ok(logger, "opencode resume session", session=inbound, http=200)
                    return inbound, False
                log_fail(logger, "opencode resume session", session=inbound, http=got.status_code)
                logger.info("session %s rejected (%s); creating new", inbound, got.status_code)
            except Exception as exc:  # noqa: BLE001
                log_fail(logger, "opencode resume session", session=inbound, err=exc)
                logger.info("session resume failed (%s); creating new", exc)
        sid = self.create_session(title)
        return sid, True

    def list_messages(self, session_id: str) -> list[dict[str, Any]]:
        response = self.http.get(
            f"/session/{session_id}/message",
            params={"limit": 400},
            headers=self.headers,
            timeout=30.0,
        )
        if response.status_code == 400:
            response = self.http.get(
                f"/session/{session_id}/message",
                headers=self.headers,
                timeout=30.0,
            )
        if response.status_code == 400:
            log_fail(logger, "opencode list messages", session=session_id, http=400)
            raise OpenCodeError(
                "messages unreadable",
                status_code=400,
            )
        try:
            response.raise_for_status()
        except Exception as exc:
            log_fail(logger, "opencode list messages", session=session_id, http=response.status_code, err=exc)
            raise
        data = response.json()
        if isinstance(data, list):
            return [m for m in data if isinstance(m, dict)]
        if isinstance(data, dict):
            for key in ("messages", "data", "items"):
                if isinstance(data.get(key), list):
                    return [m for m in data[key] if isinstance(m, dict)]
        return []

    def status(self) -> Any:
        try:
            response = self.http.get("/session/status", headers=self.headers, timeout=10.0)
            if response.status_code == 200:
                return response.json()
        except Exception:
            return None
        return None

    def session_busy(self, session_id: str) -> bool:
        status = self.status()
        if not isinstance(status, dict):
            return False
        for value in status.values() if not isinstance(status.get(session_id), dict) else [status.get(session_id)]:
            row = value if isinstance(value, dict) else {}
            kind = str(row.get("type") or row.get("status") or row.get("state") or "").lower()
            if kind in {"busy", "retry", "running", "in_progress"}:
                sid = str(row.get("id") or row.get("sessionID") or row.get("session_id") or "")
                if not sid or sid == session_id:
                    return True
        if session_id in status and isinstance(status[session_id], dict):
            kind = str(status[session_id].get("type") or status[session_id].get("status") or "").lower()
            return kind in {"busy", "retry", "running", "in_progress"}
        return False

    def post_message(self, session_id: str, text: str, *, model: str, agent: str) -> None:
        provider, model_id = parse_model(model)
        body: dict[str, Any] = {
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
                log_ok(logger, "opencode prompt", session=session_id, via="prompt_async", http=response.status_code, agent=agent, model=model)
                return
            logger.info("prompt_async HTTP %s; falling back to /message", response.status_code)
        except Exception as exc:  # noqa: BLE001
            logger.info("prompt_async failed (%s); falling back to /message", exc)
        response = self.http.post(
            f"/session/{session_id}/message",
            json=body,
            headers=self.headers,
            timeout=httpx.Timeout(connect=15.0, read=600.0, write=30.0, pool=15.0),
        )
        if response.status_code >= 400:
            log_fail(
                logger,
                "opencode prompt",
                session=session_id,
                via="message",
                http=response.status_code,
                body=(response.text or "")[:200],
            )
            raise OpenCodeError(
                f"user message POST failed: HTTP {response.status_code} {response.text[:200]}",
                status_code=response.status_code,
            )
        log_ok(logger, "opencode prompt", session=session_id, via="message", http=response.status_code, agent=agent, model=model)

    def abort(self, session_id: str) -> None:
        if not session_id.startswith("ses_"):
            return
        try:
            response = self.http.post(f"/session/{session_id}/abort", headers=self.headers, timeout=15.0)
            log_ok(logger, "opencode abort", session=session_id, http=response.status_code)
        except Exception as exc:  # noqa: BLE001
            log_fail(logger, "opencode abort", session=session_id, err=exc)

    def wait_idle(
        self,
        session_id: str,
        *,
        timeout: float,
        hang_timeout: float,
        should_stop: Optional[Callable[[], bool]] = None,
        idle_settle: float = 8.0,
    ) -> str:
        deadline = time.time() + timeout
        last_change = time.time()
        last_token: tuple[int, int, int, int] | None = None
        saw_busy = False
        baseline_turn: str | None = None
        settle = max(0.0, float(idle_settle))
        text = ""
        while time.time() < deadline:
            if should_stop and should_stop():
                log_ok(logger, "opencode wait cancelled", session=session_id)
                raise OpenCodeError("cancelled")
            if not self.health():
                log_fail(logger, "opencode wait", session=session_id, reason="serve-dead")
                raise OpenCodeError("serve-dead")
            try:
                messages = self.list_messages(session_id)
            except Exception:
                messages = []
            text = turn_assistant_text(messages, prefer_review=False)
            msg_n, parts_n, _has_structured = session_activity(messages)
            token = (msg_n, parts_n, len(text), len(messages))
            if token != last_token:
                last_token = token
                last_change = time.time()
            busy = self.session_busy(session_id)
            if busy:
                saw_busy = True
                last_change = time.time()
            if baseline_turn is None:
                baseline_turn = text
                last_change = time.time()
                time.sleep(min(1.0, max(0.05, settle if settle else 0.2)))
                continue
            grew = bool(text) and text != baseline_turn
            if not busy and text and (saw_busy or grew or time.time() - last_change > settle):
                log_ok(
                    logger,
                    "opencode idle",
                    session=session_id,
                    chars=len(text),
                    saw_busy=saw_busy,
                    grew=grew,
                )
                return text
            if time.time() - last_change > hang_timeout:
                log_fail(logger, "opencode wait", session=session_id, reason="hang", chars=len(text), messages=msg_n)
                raise OpenCodeError("hang")
            time.sleep(min(1.0, max(0.05, settle if settle else 0.2)))
        log_fail(logger, "opencode wait", session=session_id, reason="timeout", chars=len(text) if last_token else 0)
        raise OpenCodeError("timeout", timeout=True)
