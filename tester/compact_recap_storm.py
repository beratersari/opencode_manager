#!/usr/bin/env python3
"""Live storm: long OSM jobs against a fat local git tree.

Goal: see whether an OpenCode 1.18 compact recap (summary/compaction,
finish still None) ERROR's the job, or OSM waits (assess=pending).

  python tester/compact_recap_storm.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from opencode_manager.app import create_app
from opencode_manager.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
RUN = Path(os.environ.get("OSM_COMPACT_STORM_DIR") or (ROOT / ".temp" / "compact-recap-storm"))
N_FILES = int(os.environ.get("OSM_COMPACT_STORM_FILES") or "80")
MODEL = os.environ.get("OSM_COMPACT_STORM_MODEL") or ""
MAX_JOBS = int(os.environ.get("OSM_COMPACT_STORM_CONCURRENT") or "3")


def _git(*args: str, cwd: Path | None = None) -> None:
    exe = shutil.which("git") or "git"
    result = subprocess.run([exe, *args], cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)


def _free_model() -> str:
    if MODEL:
        return MODEL
    bin_path = shutil.which("opencode") or str(Path.home() / ".opencode" / "bin" / "opencode.exe")
    result = subprocess.run([bin_path, "models"], capture_output=True, text=True, timeout=90)
    inventory = [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
    preferred = (
        "opencode/mimo-v2.5-free",
        "opencode/ling-3.0-flash-free",
        "opencode/deepseek-v4-flash-free",
        "opencode/laguna-s-2.1-free",
    )
    for item in preferred:
        if item in inventory:
            return item
    free = [m for m in inventory if "free" in m.lower() and "/" in m]
    if not free:
        raise SystemExit("no free OpenCode model")
    return free[0]


def _seed_origin(origin: Path) -> None:
    if origin.exists():
        shutil.rmtree(origin)
    origin.mkdir(parents=True)
    _git("init", cwd=origin)
    _git("config", "user.email", "storm@opencode-manager.test", cwd=origin)
    _git("config", "user.name", "compact-storm", cwd=origin)
    _git("config", "init.defaultBranch", "main", cwd=origin)
    (origin / "README.md").write_text(
        "# compact-storm fixture\n\nSTORM-MARKER lives in many files.\n",
        encoding="utf-8",
    )
    for i in range(N_FILES):
        chunk = f"STORM-MARKER file-{i:03d}\n" + ("lorem compact " * 40 + f" {i}\n") * 12
        sub = origin / ("mod_a" if i % 2 == 0 else "mod_b") / f"doc_{i:03d}.txt"
        sub.parent.mkdir(parents=True, exist_ok=True)
        sub.write_text(chunk, encoding="utf-8")
    _git("add", ".", cwd=origin)
    _git("commit", "-m", "fat tree", cwd=origin)


PROMPTS = (
    (
        "CMP-READ",
        "orchestrator",
        "Read every file under this repository (skip .git). For each file write its path "
        "and a one-line summary of what you actually read. Then write an 800-word overview "
        "of the whole tree. Do not skip files. Keep going until every file is covered. "
        "Mention STORM-MARKER if you see it.",
    ),
    (
        "CMP-SEARCH",
        "orchestrator",
        "Search the entire repository for STORM-MARKER. Report every occurrence with file "
        "and a short quote. Then create NOTES.md that quotes at least 20 distinct files "
        "you read. Be thorough; do not stop after the first directory.",
    ),
    (
        "CMP-REVIEW",
        "orchestrator",
        "Security-style review: read all files. List hardcoded strings, paths, and repeated "
        "tokens. Produce a markdown findings list citing real paths. Skimming is not enough.",
    ),
    (
        "CMP-PLAN",
        "planner",
        "After reading the full tree, produce a 10-step plan to split this repo into packages. "
        "Cite file paths from actual reads. Do not invent files. Plan only; do not edit.",
    ),
    (
        "CMP-CATALOG",
        "orchestrator",
        "Create CATALOG.md with a table of all files, the first 80 characters of each file "
        "you read, and a note that STORM-MARKER is present or not. Then add a short section "
        "to README.md linking the catalog.",
    ),
    (
        "CMP-BATCH",
        "orchestrator",
        "Read files in batches of 10 until none remain. After each batch write BATCH-n.md "
        "listing those paths. When done write FINAL.md synthesizing all batches. Do not stop "
        "until every non-git file is in some batch.",
    ),
)


def _settings(run: Path) -> Settings:
    data = run / "data"
    settings = Settings(
        listen_host="127.0.0.1",
        listen_port=0,
        max_concurrent_jobs=MAX_JOBS,
        callback_timeout_seconds=5.0,
        callback_retry_count=1,
        data_dir=data,
        work_dir=data / ".temp",
        job_log_dir=data / "logs",
        job_store_dir=data / "jobs",
        queue_path=data / "queue.json",
        log_level="INFO",
        hang_timeout_seconds=180.0,
        git_clone_timeout_seconds=180.0,
        project_root=ROOT,
        opencode_bin=shutil.which("opencode") or "opencode",
        dashboard_user="",
        dashboard_password="",
        dashboard_token="",
    )
    settings.ensure_dirs()
    dist = ROOT / "web" / "dist"
    if not (settings.project_root / "web" / "dist" / "index.html").is_file() and dist.is_file():
        pass
    return settings


def _recap_flags(messages: list) -> dict[str, Any]:
    recaps = 0
    finish_none = 0
    summary_true = 0
    agent_compaction = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        info = msg.get("info") if isinstance(msg.get("info"), dict) else {}
        role = str(info.get("role") or msg.get("role") or "")
        if role != "assistant":
            continue
        finish = info.get("finish") if info.get("finish") is not None else msg.get("finish")
        summary = info.get("summary") if "summary" in info else msg.get("summary")
        mode = str(info.get("mode") or msg.get("mode") or "").lower()
        agent = str(info.get("agent") or msg.get("agent") or "").lower()
        parts = msg.get("parts") or info.get("parts") or []
        compact_part = False
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, dict) and str(part.get("type") or "").lower() in {
                    "compact",
                    "compaction",
                }:
                    compact_part = True
        is_recap = (
            summary is True
            or mode == "compaction"
            or agent in {"compaction", "summarize", "summary"}
            or compact_part
        )
        if is_recap:
            recaps += 1
            if summary is True:
                summary_true += 1
            if agent in {"compaction", "summarize", "summary"} or mode == "compaction":
                agent_compaction += 1
            if finish is None or str(finish).strip() == "":
                finish_none += 1
    return {
        "recaps": recaps,
        "recap_finish_none": finish_none,
        "summary_true": summary_true,
        "agent_compaction": agent_compaction,
    }


def _scan_log(path: Path) -> dict[str, int]:
    counts = {
        "assess_pending": 0,
        "assess_incomplete": 0,
        "assess_success": 0,
        "pending_finish_none": 0,
        "leave_incomplete": 0,
        "leave_hang": 0,
        "leave_success": 0,
        "compact_marker": 0,
    }
    if not path.is_file():
        return counts
    text = path.read_text(encoding="utf-8", errors="replace")
    counts["assess_pending"] = text.count("assess=pending")
    counts["assess_incomplete"] = text.count("assess=incomplete")
    counts["assess_success"] = text.count("assess=success")
    counts["leave_incomplete"] = text.count("inner leave kind=incomplete")
    counts["leave_hang"] = text.count("inner leave kind=hang")
    counts["leave_success"] = text.count("inner leave kind=success")
    counts["compact_marker"] = text.lower().count("compact")
    for line in text.splitlines():
        if "assess=pending" in line and "last_finish=(none)" in line:
            counts["pending_finish_none"] += 1
    return counts


def main() -> int:
    oc = shutil.which("opencode") or str(Path.home() / ".opencode" / "bin" / "opencode.exe")
    oc_dir = str(Path(oc).parent)
    os.environ["PATH"] = oc_dir + os.pathsep + os.environ.get("PATH", "")
    if shutil.which("opencode") is None:
        raise SystemExit("opencode not on PATH")

    RUN.mkdir(parents=True, exist_ok=True)
    origin = RUN / "origin"
    _seed_origin(origin)
    model = _free_model()
    repo = origin.resolve().as_uri()
    settings = _settings(RUN)
    app = create_app(settings)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="osm-compact-storm", daemon=True)
    thread.start()
    deadline = time.time() + 20
    while time.time() < deadline and not server.started:
        time.sleep(0.05)
    if not server.started:
        raise SystemExit("uvicorn did not start")
    port = int(server.servers[0].sockets[0].getsockname()[1])
    base = f"http://127.0.0.1:{port}"
    report_path = RUN / "report.json"
    print(f"OSM {base} model={model} files={N_FILES} run={RUN}", flush=True)

    jobs: list[dict[str, Any]] = []
    with httpx.Client(base_url=base, timeout=30.0) as client:
        for jira_id, agent, prompt in PROMPTS:
            body = {
                "repo_url": repo,
                "prompt": prompt,
                "model": model,
                "agent_mode": agent,
                "timeout_in_seconds": 1800,
                "retry_count": 2,
                "jira_id": jira_id,
            }
            res = client.post("/jobs", json=body)
            if res.status_code != 202:
                print(f"POST {jira_id} -> {res.status_code} {res.text[:300]}", flush=True)
                continue
            payload = res.json()
            jobs.append(
                {
                    "jira_id": jira_id,
                    "job_id": payload.get("job_id"),
                    "agent": agent,
                    "status_code": None,
                    "status": "queued",
                    "text": "",
                    "session_id": "",
                }
            )
            print(f"accepted {jira_id} {payload.get('job_id')}", flush=True)

        t0 = time.time()
        while True:
            live = 0
            for row in jobs:
                if row["status_code"] in {200, 404, 500, 504}:
                    continue
                poll = client.get(f"/jobs/{row['job_id']}")
                body = poll.json() if poll.headers.get("content-type", "").startswith("application/json") else {}
                row["status"] = body.get("status") or row["status"]
                row["session_id"] = body.get("session_id") or row["session_id"]
                if poll.status_code in {200, 404} or (
                    isinstance(body, dict) and body.get("live") is False and body.get("status_code")
                ):
                    row["status_code"] = int(body.get("status_code") or poll.status_code)
                    row["text"] = (body.get("text") or "")[:500]
                    row["status"] = body.get("status") or row["status"]
                    print(
                        f"terminal {row['jira_id']} http={poll.status_code} "
                        f"env={row['status_code']} status={row['status']}",
                        flush=True,
                    )
                else:
                    live += 1
            if live == 0:
                break
            if time.time() - t0 > 7200:
                print("FAILED: storm wall clock 7200s", flush=True)
                break
            time.sleep(15)

        for row in jobs:
            job_id = row["job_id"]
            chat = client.get(f"/api/jobs/{job_id}/chat")
            messages = []
            if chat.status_code == 200:
                payload = chat.json()
                messages = payload.get("messages") or payload.get("chat") or []
                if isinstance(payload.get("snapshot"), list):
                    messages = payload["snapshot"]
            row["chat"] = _recap_flags(messages if isinstance(messages, list) else [])
            logs = settings.job_log_dir
            assert logs is not None
            matches = sorted(logs.glob(f"{row['jira_id']}_{job_id}_*.log"))
            app_log = settings.app_log_path
            row["log"] = _scan_log(matches[-1] if matches else (app_log or logs / "app.log"))
            row["log_file"] = str(matches[-1]) if matches else ""

    recap_jobs = [r for r in jobs if r.get("chat", {}).get("recaps")]
    recap_none = [r for r in recap_jobs if r.get("chat", {}).get("recap_finish_none")]
    failed = [r for r in jobs if r.get("status_code") not in {200, None}]
    recap_then_500 = [
        r
        for r in jobs
        if r.get("status_code") == 500
        and (
            r.get("chat", {}).get("recap_finish_none")
            or r.get("log", {}).get("pending_finish_none")
            or (r.get("log", {}).get("assess_pending") and r.get("log", {}).get("leave_incomplete"))
        )
        and r.get("log", {}).get("leave_incomplete")
        and not r.get("log", {}).get("leave_success")
    ]
    report = {
        "model": model,
        "base": base,
        "n_files": N_FILES,
        "jobs": [{k: v for k, v in r.items() if k != "text"} | {"text_preview": r.get("text", "")[:240]} for r in jobs],
        "recap_jobs": [r["jira_id"] for r in recap_jobs],
        "recap_finish_none": [r["jira_id"] for r in recap_none],
        "failed": [{"jira_id": r["jira_id"], "status_code": r["status_code"]} for r in failed],
        "yaver_bug_shape_then_500": [r["jira_id"] for r in recap_then_500],
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("recap_jobs", "recap_finish_none", "failed", "yaver_bug_shape_then_500")}, indent=2), flush=True)

    server.should_exit = True
    thread.join(timeout=20)

    if recap_then_500:
        print("FAILED: compact recap finish=None followed by incomplete/500", flush=True)
        return 1
    print("DONE: storm finished", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
