#!/usr/bin/env python3
"""POST many different jobs at OSM and measure inbound + terminal error rates.

Default: 200 jobs, mixed difficulty. Unique jira_id per job (no 409).

  python tester/reliability.py --count 200
  python tester/reliability.py --count 200 --osm http://127.0.0.1:4096 --live

--live talks to a running manager (real clone + OpenCode). Without --live
the script starts an embedded OSM and a fake runner so the HTTP path is real.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Recipe:
    name: str
    difficulty: str
    inbound: str  # valid | invalid
    agent_mode: str
    prompt: str
    timeout_in_seconds: int
    retry_count: int
    repo_url: str = "https://gitlab.example/g/r.git"
    model: str = "opencode/mimo-v2.5-free"


def catalog() -> List[Recipe]:
    easy = (
        "Do not use tools. Reply with exactly this token on its own line "
        "and nothing else: {token}"
    )
    medium = (
        "List only the file names in the repository root. One name per line. "
        "No extra commentary. Token {token}"
    )
    hard = (
        "Read README.md if it exists. Write a one-sentence summary, then "
        "repeat this token on the last line: {token}"
    )
    planner = (
        "Plan only. Do not edit files. List at most three steps to inspect "
        "this repo. End with token {token}"
    )
    return [
        Recipe("easy_echo", "easy", "valid", "orchestrator", easy, 90, 1),
        Recipe("medium_list", "medium", "valid", "orchestrator", medium, 180, 2),
        Recipe("hard_summary", "hard", "valid", "orchestrator", hard, 300, 2),
        Recipe("planner_steps", "planner", "valid", "planner", planner, 180, 1),
        Recipe(
            "invalid_ssh",
            "invalid",
            "invalid",
            "orchestrator",
            easy,
            30,
            1,
            repo_url="git@gitlab.example:g/r.git",
        ),
        Recipe(
            "invalid_agent",
            "invalid",
            "invalid",
            "wizard",
            easy,
            30,
            1,
        ),
        Recipe(
            "invalid_model",
            "invalid",
            "invalid",
            "orchestrator",
            easy,
            30,
            1,
            model="not-a-provider-id",
        ),
    ]


def pick_recipes(count: int) -> List[Recipe]:
    """Weighted mix: mostly valid work, some inbound 400s."""
    items = catalog()
    by_name = {r.name: r for r in items}
    order = (
        ["easy_echo"] * 8
        + ["medium_list"] * 5
        + ["hard_summary"] * 3
        + ["planner_steps"] * 2
        + ["invalid_ssh", "invalid_agent", "invalid_model"]
    )
    out: List[Recipe] = []
    for i in range(count):
        out.append(by_name[order[i % len(order)]])
    return out


def job_body(recipe: Recipe, *, index: int, run_id: str, repo_url: str) -> Dict[str, Any]:
    token = f"REL-{run_id}-{index:04d}"
    repo = repo_url if recipe.inbound == "valid" else recipe.repo_url
    return {
        "repo_url": repo,
        "source_branch": "main",
        "prompt": recipe.prompt.format(token=token),
        "model": recipe.model,
        "agent_mode": recipe.agent_mode,
        "timeout_in_seconds": recipe.timeout_in_seconds,
        "retry_count": recipe.retry_count,
        "jira_id": f"REL{run_id}{index:04d}"[:80],
        "callback_url": "",
    }


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def http_json(method: str, url: str, body: Optional[dict] = None, *, timeout: float = 20.0) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"text": raw}
        return exc.code, parsed
    except URLError as exc:
        return 0, {"text": str(exc.reason)}


@dataclass
class Row:
    index: int
    jira_id: str
    difficulty: str
    inbound_http: int
    job_id: str = ""
    terminal_code: Optional[int] = None
    status: str = ""
    text_preview: str = ""
    error: str = ""


@dataclass
class Report:
    run_id: str
    count: int
    inbound: Counter = field(default_factory=Counter)
    terminal: Counter = field(default_factory=Counter)
    by_difficulty: Dict[str, Counter] = field(default_factory=dict)
    rows: List[Row] = field(default_factory=list)
    started: str = ""
    finished: str = ""
    elapsed_s: float = 0.0

    def add_inbound(self, row: Row) -> None:
        self.inbound[str(row.inbound_http)] += 1
        bucket = self.by_difficulty.setdefault(row.difficulty, Counter())
        bucket[f"in_{row.inbound_http}"] += 1
        self.rows.append(row)

    def add_terminal(self, row: Row) -> None:
        key = str(row.terminal_code if row.terminal_code is not None else "none")
        self.terminal[key] += 1
        self.by_difficulty.setdefault(row.difficulty, Counter())[f"term_{key}"] += 1

    def accepted(self) -> int:
        return int(self.inbound.get("202", 0))

    def inbound_error_rate(self) -> float:
        bad = sum(n for code, n in self.inbound.items() if code not in {"202", "200"})
        return bad / self.count if self.count else 0.0

    def job_error_rate(self) -> float:
        done = sum(self.terminal.values())
        bad = sum(n for code, n in self.terminal.items() if code not in {"200"})
        return bad / done if done else 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "count": self.count,
            "started": self.started,
            "finished": self.finished,
            "elapsed_s": round(self.elapsed_s, 3),
            "inbound": dict(self.inbound),
            "terminal": dict(self.terminal),
            "accepted": self.accepted(),
            "inbound_error_rate": round(self.inbound_error_rate(), 4),
            "job_error_rate": round(self.job_error_rate(), 4),
            "by_difficulty": {k: dict(v) for k, v in sorted(self.by_difficulty.items())},
        }


def poll_job(osm: str, job_id: str, *, timeout: float) -> Dict[str, Any]:
    deadline = time.time() + timeout
    last: Dict[str, Any] = {}
    while time.time() < deadline:
        code, body = http_json("GET", f"{osm.rstrip('/')}/jobs/{job_id}")
        if isinstance(body, dict):
            last = body
            if code == 404:
                return {"status_code": 404, "status": "missing", "text": body.get("text") or ""}
            if not body.get("live") and body.get("status_code") not in (None, 202):
                return body
        time.sleep(0.25)
    return last or {"status_code": None, "status": "poll_timeout", "text": "poll timeout"}


def post_jobs(
    osm: str,
    recipes: List[Recipe],
    *,
    run_id: str,
    repo_url: str,
    poll: bool,
    poll_timeout: float,
) -> Report:
    report = Report(run_id=run_id, count=len(recipes), started=_now())
    t0 = time.time()
    pending: List[Row] = []
    for i, recipe in enumerate(recipes, start=1):
        body = job_body(recipe, index=i, run_id=run_id, repo_url=repo_url)
        code, ack = http_json("POST", f"{osm.rstrip('/')}/jobs", body)
        row = Row(
            index=i,
            jira_id=str(body["jira_id"]),
            difficulty=recipe.difficulty,
            inbound_http=code,
            job_id=str((ack or {}).get("job_id") or ""),
            error=str((ack or {}).get("text") or "")[:200],
        )
        report.add_inbound(row)
        if code == 202 and row.job_id:
            pending.append(row)
    if poll:
        for row in pending:
            got = poll_job(osm, row.job_id, timeout=poll_timeout)
            row.terminal_code = got.get("status_code")
            row.status = str(got.get("status") or "")
            row.text_preview = str(got.get("text") or "")[:160]
            report.add_terminal(row)
    report.finished = _now()
    report.elapsed_s = time.time() - t0
    return report


def print_report(report: Report) -> None:
    data = report.as_dict()
    print(json.dumps(data, indent=2))
    print()
    print(
        f"sent={report.count} accepted={report.accepted()} "
        f"inbound_error_rate={data['inbound_error_rate']:.1%} "
        f"job_error_rate={data['job_error_rate']:.1%} "
        f"elapsed={report.elapsed_s:.1f}s"
    )


def start_embedded(repo_ready: Path) -> tuple[str, threading.Thread]:
    sys.path.insert(0, str(ROOT / "src"))
    import uvicorn

    from opencode_manager.app import create_app
    from opencode_manager.models import JobRecord
    from opencode_manager.settings import Settings
    from opencode_manager.worker import Terminal

    class FastRunner:
        def run(self, job: JobRecord, *, should_stop) -> Terminal:  # noqa: ARG002
            job.session_id = job.session_id or "ses_rel"
            if "hard" in (job.jira_id or "") and int(job.jira_id[-1]) in {7, 8}:
                return Terminal(500, f"injected hard fail {job.jira_id}")
            job.text = f"ok {job.jira_id}"
            return Terminal(200, job.text)

    data = repo_ready / "rel-data"
    settings = Settings(
        listen_host="127.0.0.1",
        listen_port=0,
        max_concurrent_jobs=4,
        callback_timeout_seconds=5.0,
        callback_retry_count=1,
        work_dir=data / "work",
        job_log_dir=data / "joblogs",
        job_store_dir=data / "jobs",
        queue_path=data / "queue.json",
        log_level="WARNING",
        hang_timeout_seconds=30.0,
        git_clone_timeout_seconds=30.0,
        project_root=ROOT,
    )
    settings.ensure_dirs()
    app = create_app(settings, runner=FastRunner())
    sock = __import__("socket").socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    def _run() -> None:
        server.run()

    thread = threading.Thread(target=_run, name="osm-reliability", daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        code, _ = http_json("GET", f"{base}/api/meta", timeout=1.0)
        if code == 200:
            return base, thread
        time.sleep(0.1)
    raise SystemExit("embedded OSM did not become ready")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="POST many OSM jobs and measure error rates")
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--osm", default="")
    parser.add_argument("--live", action="store_true", help="Use a running OSM (real OpenCode)")
    parser.add_argument("--repo-url", default="https://gitlab.example/g/r.git")
    parser.add_argument("--poll-timeout", type=float, default=600.0)
    parser.add_argument("--no-poll", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    if args.count < 1:
        print("--count must be >= 1", file=sys.stderr)
        return 2
    run_id = datetime.now(timezone.utc).strftime("%H%M%S")
    recipes = pick_recipes(args.count)
    if args.live:
        osm = (args.osm or "http://127.0.0.1:4096").rstrip("/")
        code, meta = http_json("GET", f"{osm}/api/meta", timeout=5.0)
        if code != 200:
            print(f"OSM not reachable at {osm}: HTTP {code} {meta}", file=sys.stderr)
            return 1
        print(f"live OSM {osm} meta={meta}")
        report = post_jobs(
            osm,
            recipes,
            run_id=run_id,
            repo_url=args.repo_url,
            poll=not args.no_poll,
            poll_timeout=args.poll_timeout,
        )
    else:
        osm, _thread = start_embedded(Path.cwd())
        print(f"embedded OSM {osm}")
        report = post_jobs(
            osm,
            recipes,
            run_id=run_id,
            repo_url=args.repo_url,
            poll=not args.no_poll,
            poll_timeout=min(args.poll_timeout, 60.0),
        )
    print_report(report)
    if args.out:
        Path(args.out).write_text(json.dumps(report.as_dict(), indent=2) + "\n", encoding="utf-8")
    return 0 if report.accepted() > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
