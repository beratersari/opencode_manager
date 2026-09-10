"""Runtime review settings. Dashboard may change model, agent, and timeout."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Iterable

from opencode_manager.review_config import ReviewConfig
from opencode_manager.review_log import get_logger, log_fail, log_ok

logger = get_logger("settings")

SETTINGS_NAME = "settings.json"
KNOWN_MODELS = ("opencode/big-pickle",)
KNOWN_AGENTS = ("code-reviewer",)
MIN_TIMEOUT = 1
MAX_TIMEOUT = 86400

_lock = threading.Lock()


class SettingsError(ValueError):
    pass


def settings_path(cfg: ReviewConfig) -> Path:
    return Path(cfg.data_dir) / SETTINGS_NAME


def normalize_model(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        raise SettingsError("model is required")
    provider, sep, name = text.partition("/")
    if not sep or not provider or not name.strip():
        raise SettingsError("model must be provider/id")
    return text


def normalize_agent(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        raise SettingsError("agent is required")
    if "/" in text or " " in text:
        raise SettingsError("agent must be a single OpenCode agent name")
    return text


def normalize_timeout(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise SettingsError("timeout must be an integer") from exc
    if value < MIN_TIMEOUT or value > MAX_TIMEOUT:
        raise SettingsError(f"timeout must be between {MIN_TIMEOUT} and {MAX_TIMEOUT} seconds")
    return value


def remember_yaml_defaults(cfg: ReviewConfig) -> None:
    if not (getattr(cfg, "opencode_model_env", "") or "").strip():
        cfg.opencode_model_env = (cfg.opencode_model or "").strip()
    if not getattr(cfg, "opencode_timeout_env", 0):
        cfg.opencode_timeout_env = int(cfg.opencode_timeout or 1800)
    if not (getattr(cfg, "opencode_agent_env", "") or "").strip():
        cfg.opencode_agent_env = (cfg.opencode_agent or "").strip()


def load_overrides(cfg: ReviewConfig) -> dict[str, Any]:
    path = settings_path(cfg)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log_fail(logger, "settings load", path=str(path), err=exc)
        return {}
    return data if isinstance(data, dict) else {}


def apply_runtime_settings(cfg: ReviewConfig) -> None:
    remember_yaml_defaults(cfg)
    data = load_overrides(cfg)
    if not data:
        return
    raw_model = data.get("review_model") or data.get("opencode_model")
    if raw_model is not None and str(raw_model).strip():
        try:
            cfg.opencode_model = normalize_model(raw_model)
        except SettingsError as exc:
            log_fail(logger, "settings model", err=exc)
    raw_timeout = data.get("review_timeout_seconds")
    if raw_timeout is None:
        raw_timeout = data.get("opencode_timeout")
    if raw_timeout is not None and str(raw_timeout).strip() != "":
        try:
            cfg.opencode_timeout = normalize_timeout(raw_timeout)
        except SettingsError as exc:
            log_fail(logger, "settings timeout", err=exc)
    raw_agent = data.get("review_agent") or data.get("opencode_agent")
    if raw_agent is not None and str(raw_agent).strip():
        try:
            cfg.opencode_agent = normalize_agent(raw_agent)
        except SettingsError as exc:
            log_fail(logger, "settings agent", err=exc)
    log_ok(
        logger,
        "settings applied",
        model=cfg.opencode_model,
        agent=cfg.opencode_agent,
        timeout=cfg.opencode_timeout,
    )


def save_runtime_settings(cfg: ReviewConfig, *, model: str, timeout: int, agent: str) -> None:
    remember_yaml_defaults(cfg)
    model = normalize_model(model)
    timeout = normalize_timeout(timeout)
    agent = normalize_agent(agent)
    path = settings_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "review_model": model,
        "review_timeout_seconds": timeout,
        "review_agent": agent,
    }
    tmp = path.with_name(path.name + ".tmp")
    with _lock:
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        cfg.opencode_model = model
        cfg.opencode_timeout = timeout
        cfg.opencode_agent = agent
    log_ok(logger, "settings saved", model=model, agent=agent, timeout=timeout)


def suggested_models(cfg: ReviewConfig, extra: Iterable[str] = ()) -> list[str]:
    seen: list[str] = []
    for raw in (cfg.opencode_model, getattr(cfg, "opencode_model_env", ""), *KNOWN_MODELS, *extra):
        text = str(raw or "").strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def suggested_agents(cfg: ReviewConfig, extra: Iterable[str] = ()) -> list[str]:
    seen: list[str] = []
    for raw in (cfg.opencode_agent, getattr(cfg, "opencode_agent_env", ""), *KNOWN_AGENTS, *extra):
        text = str(raw or "").strip()
        if text and text not in seen:
            seen.append(text)
    return seen
