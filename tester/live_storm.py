#!/usr/bin/env python3
"""200 valid live OSM jobs: real public repos, mixed prompts, follow-up resumes.

Measures inbound + terminal rates and whether a resumed ses_* ever ships
the previous job's text (issue 4).

  python tester/live_storm.py --osm http://127.0.0.1:4097 --count 200
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPOS = (
    "https://github.com/octocat/Hello-World.git",
    "https://github.com/octocat/Spoon-Knife.git",
    "https://github.com/mathiasbynens/small.git",
    "https://github.com/github/gitignore.git",
    "https://github.com/jlevy/the-art-of-command-line.git",
)

PROMPTS = (
    "Do not use tools. Reply with exactly this token on its own line and nothing else: {token}",
    "Do not edit files. List only the file names in the repository root, one per line. Last line must be {token}",
    "Do not edit files. In one short sentence say what this repository is. Last line must be {token}",
    "Plan only. Do not edit files. List at most three steps to inspect this repo. Last line must be {token}",
    "Do not edit files. Answer yes or no: does README.md exist at the repo root? Last line must be {token}",
    "Do not edit files. Reply with a single integer: how many entries are in the repo root? Last line must be {token}",
    "Do not edit files. Name the license if you can see one, else say none. Last line must be {token}",
    "Do not edit files. One word: what language is this repo mostly? Last line must be {token}",
)

FOLLOWUPS = (
    "Do not use tools. Do not repeat your previous answer. Reply with exactly this new token on its own line: {token}",
    "Follow-up only. One sentence about the repo root, different from last time. Last line must be {token}",
    "Plan only. Do not edit. Give one next inspection step. Last line must be {token}",
    "Do not edit files. Reply with only {token}",
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def http_json(method: str, url: str, body: Optional[dict] = None, *, timeout: float = 30.0) -> tuple[int, Any]:
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


def poll_once(osm: str, job_id: str) -> Dict[str, Any]:
    code, body = http_json("GET", f"{osm}/jobs/{job_id}", timeout=20.0)
    if not isinstance(body, dict):
        return {"live": True, "status_code": 202, "text": str(body)}
    if code == 404:
        return {"status_code": 404, "status": "missing", "text": body.get("text") or "", "session_id": "", "live": False}
    return body


def make_first(index: int, run_id: str, model: str, retry_count: int) -> Dict[str, Any]:
    token = f"ST1-{run_id}-{index:04d}"
    repo = REPOS[index % len(REPOS)]
    prompt = PROMPTS[index % len(PROMPTS)].format(token=token)
    agent = "planner" if index % 5 == 4 else "orchestrator"
    return {
        "repo_url": repo,
        "source_branch": "",
        "prompt": prompt,
        "model": model,
        "agent_mode": agent,
        "timeout_in_seconds": 240,
        "retry_count": retry_count,
        "jira_id": f"ST{run_id}{index:04d}",
        "callback_url": "",
        "session_id": "-1",
        "_token": token,
        "_kind": "first",
    }


def make_follow(index: int, run_id: str, model: str, prev: Dict[str, Any], retry_count: int) -> Dict[str, Any]:
    token = f"ST2-{run_id}-{index:04d}"
    prompt = FOLLOWUPS[index % len(FOLLOWUPS)].format(token=token)
    return {
        "repo_url": prev["repo_url"],
        "source_branch": "",
        "prompt": prompt,
        "model": model,
        "agent_mode": prev.get("agent_mode") or "orchestrator",
        "timeout_in_seconds": 240,
        "retry_count": retry_count,
        "jira_id": prev["jira_id"],
        "callback_url": "",
        "session_id": prev.get("session_id") or "-1",
        "_token": token,
        "_kind": "follow",
        "_prev_text": prev.get("text") or "",
        "_prev_token": prev.get("token") or "",
    }


def post_one(osm: str, body: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
    wire = {k: v for k, v in body.items() if not k.startswith("_")}
    return http_json("POST", f"{osm}/jobs", wire, timeout=30.0)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--osm", default="http://127.0.0.1:4097")
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--followups", type=int, default=80)
    parser.add_argument("--model", default="opencode/mimo-v2.5-free")
    parser.add_argument("--poll-timeout", type=float, default=900.0)
    parser.add_argument("--retry-count", type=int, default=2)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    if args.count < 1:
        return 2
    follow_n = max(0, min(args.followups, args.count - 1))
    first_n = args.count - follow_n
    osm = args.osm.rstrip("/")
    code, meta = http_json("GET", f"{osm}/api/meta", timeout=5.0)
    if code != 200:
        print(f"OSM not reachable at {osm}: HTTP {code} {meta}", file=sys.stderr)
        return 1
    run_id = datetime.now(timezone.utc).strftime("%H%M%S")
    print(
        f"storm start osm={osm} meta={meta} first={first_n} followups={follow_n} "
        f"model={args.model} retry_count={args.retry_count}"
    )

    t0 = time.time()
    inbound = Counter()
    terminal = Counter()
    rows: List[Dict[str, Any]] = []
    leak = 0
    follow_ok = 0
    follow_fail = 0

    first_bodies = [
        make_first(i, run_id, args.model, args.retry_count) for i in range(1, first_n + 1)
    ]
    live: List[Dict[str, Any]] = []
    finished_first: List[Dict[str, Any]] = []

    for body in first_bodies:
        code, ack = post_one(osm, body)
        inbound[str(code)] += 1
        row = {
            "kind": "first",
            "jira_id": body["jira_id"],
            "repo": body["repo_url"],
            "token": body["_token"],
            "inbound": code,
            "job_id": str((ack or {}).get("job_id") or ""),
            "ack_text": str((ack or {}).get("text") or "")[:160],
        }
        rows.append(row)
        if code == 202 and row["job_id"]:
            live.append({**row, "body": body, "t_submit": time.time()})
        print(f"POST first {body['jira_id']} HTTP {code} job={row['job_id']}", flush=True)

    follows_left = follow_n
    while live or (follows_left > 0 and finished_first):
        still: List[Dict[str, Any]] = []
        for item in live:
            age = time.time() - item["t_submit"]
            got = poll_once(osm, item["job_id"])
            if got.get("live") or got.get("status_code") in (None, 202):
                if age > args.poll_timeout:
                    item["terminal"] = None
                    item["status"] = "poll_timeout"
                    item["text"] = "poll timeout"
                    terminal["poll_timeout"] += 1
                    rows_item = next(r for r in rows if r.get("job_id") == item["job_id"])
                    rows_item.update({k: item[k] for k in ("terminal", "status", "text") if k in item})
                    print(f"TIMEOUT {item['jira_id']} {item['job_id']}", flush=True)
                else:
                    still.append(item)
                continue
            item["terminal"] = got.get("status_code")
            item["status"] = str(got.get("status") or "")
            item["text"] = str(got.get("text") or "")
            item["session_id"] = str(got.get("session_id") or "")
            item["elapsed"] = round(time.time() - item["t_submit"], 2)
            terminal[str(item["terminal"])] += 1
            rec = next(r for r in rows if r.get("job_id") == item["job_id"])
            rec.update(
                {
                    "terminal": item["terminal"],
                    "status": item["status"],
                    "text": item["text"][:240],
                    "session_id": item["session_id"],
                    "elapsed_s": item["elapsed"],
                }
            )
            print(
                f"DONE {item['kind']} {item['jira_id']} term={item['terminal']} "
                f"{item['elapsed']}s ses={item['session_id'][:16]}",
                flush=True,
            )
            if item["kind"] == "first" and item["terminal"] == 200:
                finished_first.append(item)
            if item["kind"] == "follow":
                prev_text = item.get("prev_text") or ""
                prev_token = item.get("prev_token") or ""
                text = item["text"]
                leaked = bool(prev_text) and text.strip() == prev_text.strip()
                leaked = leaked or (bool(prev_token) and prev_token in text and item.get("token") not in text)
                rec["leak_previous"] = leaked
                if leaked:
                    leak += 1
                    print(f"LEAK {item['jira_id']} previous text reused", flush=True)
                if item["terminal"] == 200 and not leaked:
                    follow_ok += 1
                else:
                    follow_fail += 1
        live = still

        while follows_left > 0 and finished_first:
            prev = finished_first.pop(0)
            follows_left -= 1
            body = make_follow(1000 + follows_left, run_id, args.model, {
                "repo_url": prev["body"]["repo_url"],
                "jira_id": prev["jira_id"],
                "session_id": prev.get("session_id") or "-1",
                "agent_mode": prev["body"].get("agent_mode"),
                "text": prev.get("text") or "",
                "token": prev.get("token") or "",
            }, args.retry_count)
            code, ack = post_one(osm, body)
            inbound[str(code)] += 1
            row = {
                "kind": "follow",
                "jira_id": body["jira_id"],
                "repo": body["repo_url"],
                "token": body["_token"],
                "inbound": code,
                "job_id": str((ack or {}).get("job_id") or ""),
                "ack_text": str((ack or {}).get("text") or "")[:160],
                "prev_job_id": prev["job_id"],
            }
            rows.append(row)
            print(f"POST follow {body['jira_id']} HTTP {code} job={row['job_id']} ses={body['session_id'][:20]}", flush=True)
            if code == 202 and row["job_id"]:
                live.append({
                    **row,
                    "body": body,
                    "t_submit": time.time(),
                    "kind": "follow",
                    "prev_text": body.get("_prev_text") or "",
                    "prev_token": body.get("_prev_token") or "",
                    "token": body["_token"],
                })
            else:
                follow_fail += 1

        if live:
            time.sleep(1.0)

    elapsed = time.time() - t0
    done = sum(terminal.values())
    job_err = sum(n for k, n in terminal.items() if k != "200")
    report = {
        "run_id": run_id,
        "osm": osm,
        "model": args.model,
        "retry_count": args.retry_count,
        "count": args.count,
        "first_n": first_n,
        "follow_n": follow_n,
        "started": _now(),
        "elapsed_s": round(elapsed, 2),
        "inbound": dict(inbound),
        "terminal": dict(terminal),
        "accepted": inbound.get("202", 0),
        "inbound_error_rate": round(
            sum(n for k, n in inbound.items() if k != "202") / max(1, sum(inbound.values())), 4
        ),
        "job_error_rate": round(job_err / max(1, done), 4),
        "follow_ok": follow_ok,
        "follow_fail": follow_fail,
        "previous_text_leaks": leak,
        "mean_elapsed_s": round(
            sum(r.get("elapsed_s") or 0 for r in rows) / max(1, len([r for r in rows if r.get("elapsed_s")])),
            2,
        ),
        "repos": list(REPOS),
    }
    print(json.dumps(report, indent=2))
    print(
        f"sent={sum(inbound.values())} accepted={inbound.get('202', 0)} "
        f"job_error_rate={report['job_error_rate']:.1%} leaks={leak} "
        f"elapsed={elapsed:.1f}s",
        flush=True,
    )
    out = args.out or str(Path(__file__).with_name(f"live_storm_{run_id}.json"))
    Path(out).write_text(json.dumps({"summary": report, "rows": rows}, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}", flush=True)
    return 0 if leak == 0 and inbound.get("202", 0) > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
