# AGENTS.md — OpenCode Session Manager

This file is binding for anyone implementing or changing this repo.
The long rationale lives in [PLAN.md](PLAN.md). If this file and the
plan disagree, **fix the plan** — do not silently pick a third design.

This is a small Windows/Linux worker between **n8n** and **OpenCode**.
It is not Yaver / virtual_developer. No Jira poller, no GitLab MR, no
Codex. The dashboard is **jobs-tab visualization only** (no writes).

## Intentional product choices

These look like bugs. They are not.

1. **The product of a job is text.** Callback `text` is the last
   assistant message (or an error). No git push, no MR, no returning
   a branch.
2. **Always delete the clone** when the job ends, success or fail.
   Next job for the same ticket re-clones to the **same path**.
3. **Chat vs disk drift is expected.** After delete, the tree is a
   clean remote checkout. Session history may talk about edits that
   are gone. Do not keep the workspace to make files match the
   session. Do not fail a job because the model “remembers” dirty
   files.
4. **One `opencode serve` per job**, unique localhost port. Not one
   shared serve. We kill **this** serve so Windows file locks / AV
   do not block delete. Other jobs’ serves stay up.
5. **`callback_url` is on the request**, not in settings. Do not
   guess it from `Host` / `Origin` / `Referer`.
6. **Dashboard is GET-only.** Jobs list + job detail (prompt, chat,
   logs, attempts). It never starts or stops work. History outlives
   the clone.
7. **Hang clock is “never started answering.”** Once this turn has a
   new assistant message (id ≠ the pre-POST baseline), that counts as
   progress for the rest of the wait. A frozen mid-generation is the
   attempt `timeout_in_seconds`, not hang. Hang is `busy` + not
   compacting + no assistant yet this turn + no new messages / compact
   markers.

## Hard rules

### API and jobs

- Inbound writes are `POST /jobs` and `DELETE /sessions`. Dashboard
  `/api/*` stays GET-only. n8n may use `n8n-callback.json` (one
  terminal POST to `callback_url`) or `n8n-poller.json` (omit
  `callback_url`, poll `GET /jobs/{job_id}`). Same OSM process.
- `POST /jobs` is only an ack (`202` / `409` / `400` / `503`). Never hold
  that socket for clone or OpenCode. `503` means the process is not
  accepting (`boot` not finished, or shutting down). No callback.
- `DELETE /sessions` is a **sync** admin op (not a job): forget an
  OpenCode `ses_*` for a ticket. Same envelope shape; `job_id` is
  empty. No `callback_url`, no queue, no history row. Body is
  `{ jira_id, session_id }`. Empty / `-1` / not `ses_*` → **400**.
  Live queued/running `jira_id` → **409** (include that job’s
  `job_id`). A delete already in flight for that ticket → **409**.
  OpenCode 2xx or 404 → **200** (idempotent). Serve boot / other
  OpenCode failure → **500**. Boot/shutdown → **503**. `POST /jobs`
  for a ticket whose session delete is in flight is also **409**.
- `callback_url` is optional. Omit or leave empty to **poll** instead
  of receiving a POST. If present it must be absolute `http`/`https`.
  `callback_allowed_hosts` of `[]`, `["*"]`, or `["all"]` accepts
  every host. A real host list is SSRF only (`*.example.com` ok).
- `GET /jobs/{job_id}` is the poller (same envelope as the callback,
  plus `live` and `status`). Unknown id → HTTP **404**. Live
  queued/running → HTTP **202**, envelope `status_code` **202**.
  Terminal → HTTP **200**, envelope `status_code` is `200` / `404` /
  `500` / `504`. Dashboard `/api/jobs/{id}` is unchanged.
- When `callback_url` was sent: exactly one terminal callback (same
  envelope) goes to that URL, and only when the job is terminal
  (`200` / `404` / `500` / `504`).
  - Accepted job with a callback URL (inbound `202`, queued or
    started): 1 terminal callback.
  - Accepted job with no callback URL: **0** callbacks; poll
    `GET /jobs/{job_id}`.
  - `409` / inbound `400`: no callback.
  - Never POST `queued` or `in_progress`. n8n wait-node URLs fire once;
    the HTTP ack already says queued vs started.
  - Callback **HTTP** (n8n’s reply to that POST, not the envelope
    `status_code`): `2xx` is delivered — stop. Retry the same envelope
    on `404` / `408` / `429` / `5xx` / transport error
    (`callback_retry_count`, then log). Other `4xx` (`400`, `401`,
    `403`, `405`, `410`, `422`, …) are permanent — log and stop.
    Do not re-run OpenCode. n8n Wait `404` means the webhook is not
    armed yet, not that the callback was delivered.
- Dedup key is **`jira_id`**. Running or queued → `409`. Session
  delete in progress for that ticket → `409`. Do not enqueue a
  second job for the same ticket. A job that already finished in
  this process must not stay `409` because the last history write
  failed — overlay the terminal row so poll/`live_for_jira` see it
  done.
- Capacity full + **other** tickets → queue. Persist the queue
  (including `callback_url`) so a **running** process can dequeue
  after a slot frees. A process restart does **not** auto-run
  leftover work — see Boot and shutdown.

### Boot and shutdown

These are process-lifecycle rules. Do not mix them with hang retry.

- **While booting: do not start any job.** Do not dequeue. Do not
  clone. Do not start `opencode serve`. Do not resume a leftover
  running or queued job. Do not send callbacks for leftovers. Do
  not accept `POST /jobs` or `DELETE /sessions` until boot is finished.
- **Boot cleanup is process hygiene only.** Kill leftover
  `serve_pid` / `extra_pids` recorded on running or queued rows.
  Then reap orphans whose cwd/argv is `work_dir` (leftover serves,
  git, tools) on Linux `/proc` only — do not snapshot Win32_Process
  on Windows. Mark leftover running/queued
  rows **ERROR** in the job-history store so the dashboard can show
  them. They are not live, so those `jira_id`s are not `409`. Do
  **not** “handle” those jobs (no OpenCode, no terminal callback,
  no re-enqueue).
- **While shutting down:** stop accepting `POST /jobs` and
  `DELETE /sessions`. Force-kill
  every job’s process tree (git, **that** job’s serve, tool
  children). Mark every running **and** queued job **ERROR**. Send
  each its one terminal callback `500`. Then the normal job-end
  delete for each clone. Keep the history row for the dashboard.
  Never kill the manager until that is done.
- **If a request comes again:** after those jobs are ERROR (boot
  leftover or shutdown), a new `POST /jobs` for
  the same `jira_id` is a **new** job. That worker hard-deletes the
  leftover clone path first, then clones. Resume `session_id` only if
  the caller sent one and it is still valid. Do not recover the
  interrupted work on the next boot.

### Git

- **Direct clone** of the request `repo_url`. There is no `PAT` field
  and no oauth2 / extraHeader rewrite. `git clone <url> dest` only —
  no `--branch`, no `--single-branch`, no `git checkout`. The
  OpenCode agent checks out `source_branch`. Then scrub origin.
  Do **not** init or
  update git submodules. Do **not** download Git LFS blobs
  (`GIT_LFS_SKIP_SMUDGE=1`); leave pointer files on disk.
  `GIT_TERMINAL_PROMPT=0` so git never waits on a hidden console
  username prompt. On **Windows**: first try GCM (`manager`,
  `GCM_INTERACTIVE=auto`) for a stored cred or GCM popup. If git still
  fails with an auth error (`terminal prompts disabled`, 401, …), OSM
  opens a **Windows username/password dialog** (`Get-Credential`) and
  retries once with Basic (not on argv, not logged). On Linux, keep
  `credential.helper` empty (no dialog). Cancel / empty dialog → job
  **500**. A leftover inbound `PAT` key is ignored.
- Reject `git@` / `ssh://`.
- `source_branch` must exist on the remote. Do not create a branch
  from `main`. Missing field or n8n placeholder `-1` on the body →
  inbound **400**. Missing on the remote → inbound **202**, worker
  checks, callback **404**. Never return a sync 404 for a missing
  remote ref. Job-end must not crash the manager (no process scan
  when the clone was never created; never PEB-read python/cmd).
- One settings root: `data_dir` (Windows `C:\osm`, Linux
  `/var/lib/osm`). Clones live under `{data_dir}/.temp`. Folder name is the **ticket id**
  (`jira_id`, Windows-safe: `[A-Za-z0-9][A-Za-z0-9._-]{0,79}`).
  `.`, `..`, slashes, and other characters are inbound **400**. The
  dest must be a strict child of `{data_dir}/.temp` — never the temp
  root itself. Dedup is one live job per ticket, so repo and branch
  are not part of the path. Two tickets ⇒ two folders. Same ticket
  later ⇒ same folder (hard-delete, then clone).
- **New job** (including after boot leftover ERROR): if that
  stable path already exists, sequential hard-delete it first, then
  clone. Do not `git clone` until that path is gone. If the leftover
  cannot be deleted, fail the job `500` (no OpenCode). Boot does
  **not** delete leftover trees (no job handling).
- Mid-job retries: **do not delete** the clone. Delete only when the
  job is finished.
- Job-end delete also runs when git fails before OpenCode starts
  (missing remote branch, clone error, git timeout). After kill,
  hard-delete the clone path again and log whether it is gone.
- Track every git child PID on `job.extra_pids` while it is live.
  `ls-remote` matches `refs/heads/{branch}` exactly. After clone,
  origin has no userinfo and keeps `host:port`. Scrub the stored
  `remote.origin.url` — never `git remote get-url` (rewrites can lie).

### OpenCode

- Start `opencode serve --hostname 127.0.0.1 --port <free>`. Never
  hardcode `4096`. Record `{pid, port, base_url, cwd}` on the job
  **immediately after Popen**, before the health wait.
- If `GET /global/health` is not 200 in time: kill **this** child,
  fail **this** attempt (`serve-dead`). Outer `retry_count` applies.
  Do not leave the process up.
- After health, wait until the directory instance answers
  (`GET /session`). `/global/health` is process-level; the first
  `x-opencode-directory` request creates the instance and can block
  while OpenCode bootstraps the clone. That wait is serve boot
  (counts toward `timeout_in_seconds`). Do not fail `POST /session`
  at 30s while the instance is still booting.
- Then check `GET /config/providers` (then `/provider`, including
  `connected`). If the request `model` is not on this serve, or the
  inventory is readable and empty: fail the **job** `500` immediately
  with the available ids. Do not POST a user message. Do not wait
  for the attempt clock. Do not spend remaining `retry_count`. A
  later OpenCode `ProviderModelNotFoundError` / "model not found" is
  the same **500**, not a hang or `504`.
- Do not run `opencode --auto` as a one-shot CLI. Do not enable
  permission auto-approve.
- Scope requests with `x-opencode-directory: <clone>`.
- Request `model` (`provider/id`) is required. Send it on every
  user message as `{ providerID, modelID }`. No settings default.
- Only two OpenCode agents on `agent_mode`: `planner` and
  `orchestrator`. n8n maps `working_mode` itself (`Plan` / `plan`
  → `planner`, `build` / `Build` → `orchestrator`, case-insensitive)
  and does not send `working_mode` to OSM. Anything else → inbound
  **400**.
- OpenCode only. No Codex.

### Session id — two moments

| Moment | Unusable / rejected `ses_*` |
|---|---|
| **No live `ses_*` yet** (including first serve died before create) | Create a **new** session. Log INFO. Do not fail the job. Return the id actually used. |
| **Mid-job hang retry** (we already had a live id, clone still on disk) | **Fail this attempt.** Do not open a blank session and continue. Counts against `retry_count`. |

Empty / non-`ses_*` / Codex UUID when we have no live id = create new, not an error.

### Prompts

The incoming `prompt` (`ORIGINAL`) is sent **once**, the first time a
serve can accept a user message. Key off **whether it was POSTed**,
not attempt number. Never send it again after that one POST.

| When | Send |
|---|---|
| First user message of this job (`ORIGINAL` not yet POSTed) | `ORIGINAL` |
| Model asked a live question | `UNATTENDED_NUDGE` (at most once per job) |
| Compact loop aborted, session idle | `COMPACT_LOOP_NUDGE` |
| Outer retry **after** `ORIGINAL` was POSTed: hang / serve dead / HTTP | `HANG_RESUME` |
| Outer retry: incomplete, not compact (**same serve**, do not kill) | `INCOMPLETE_RESUME` |

Exact strings: PLAN.md §5.3. Do not invent a fifth resume prompt.

Never POST a user message while the session is `busy` / compacting.

### Compact vs hang

- Healthy compact can take minutes. **Wait.** That is not a retry.
- Do not send “Continue” during compact (races OpenCode’s loop).
  OSM never POSTs `Continue if you have next steps, or stop and ask
  for clarification if you are unsure how to proceed.` That string
  is **not** one of the five prompts above.
- OpenCode (TUI and `opencode serve`, same `SessionPrompt.run`)
  injects that line itself after a successful **auto** compact:
  a synthetic user part (`synthetic: true`,
  `metadata.compaction_continue: true`). TUI “continues itself”
  because of this insert — the human does not type it. The
  dashboard may show it as a normal user bubble; snapshot drops
  those flags. Do not treat it as an OSM-posted prompt.
- Why OpenCode inserts it: `SessionPrompt.run` **exits** when the
  last assistant `finish` is not `tool-calls` and
  `lastUser.id < lastAssistant.id`. The compact summary is an
  assistant `finish=stop`. Without a newer user message the serve
  goes idle. OSM would then `assess_idle` that `stop` as **success**
  and end the job. Do not disable
  `experimental.compaction.autocontinue` (default `enabled: true`)
  and do not strip that synthetic turn.
- Compact-loop (~8) counts **new** compact markers **this wait**
  only. Markers already in the session when the turn started do not
  count (resumed `ses_*` / KAN-95).
- While status is `compacting` / `busy_compacting`, **or**
  `GET /session/:id` has `time.compacting`, that **is** progress.
  OpenCode `GET /session/status` is only `idle` / `retry` / `busy`
  — do not require a `compacting` status type. Do **not** run the
  hang clock (compact may last minutes with no new markers).
- Hang watchdog: `busy` **and not compacting** **and** no new
  message / compact marker **and no assistant yet this turn** for
  `hang_timeout_seconds` → **outer retry**: abort → kill **this**
  serve → new serve, same path. An assistant id that appeared after
  the POST is progress (intentional; see product choice 7).
  If `ORIGINAL` was already POSTed: same `session_id` → `HANG_RESUME`.
  If `ORIGINAL` was never POSTed: create if we have no live id, then
  send `ORIGINAL`. Same if serve is dead.
- Incomplete (session **idle**, last finish unfinished — `tool-calls`,
  `length` / max tokens, null, not a clean `stop` — not compact,
  not still-asking): **same serve**, POST `INCOMPLETE_RESUME`. Do
  **not** abort or kill serve. Counts against `retry_count`.
- Still asking after the one nudge, or compact-related leftover after
  inner handling: fail the job. No `INCOMPLETE_RESUME`.
- Clean `stop` + only leftover OpenCode todos: success (text product).
- `retry_count` counts serve restarts **and** same-serve incomplete
  resumes, not compact-wait.
- `timeout_in_seconds` is **one OpenCode attempt** (serve boot +
  session loop). Clone / cleanup / callbacks are outside it.
- Each outer retry **resets** that clock. `retry_count = 3` and
  timeout `1800` ⇒ up to **5400s** of OpenCode (`retry_count` is
  attempts, first included).
- Drive the turn with a **poll loop**. Do not block `/message` for
  the whole attempt budget.
- Attempt clock hits zero, or hang watchdog fires → abort, kill this
  serve, next attempt if any remain. Prompt is `HANG_RESUME` only if
  `ORIGINAL` was already POSTed; otherwise `ORIGINAL`.
- None left after **`timeout_in_seconds`**: delete clone, callback
  **`504`**.
- None left after hang / serve death / incomplete / other: delete
  clone, callback **`500`**. `504` is only the attempt clock.

### Kill and cleanup

**Never kill OSM.** Every kill (`taskkill`, `SIGKILL`, `killpg`) goes
through `kill_pid` / `may_kill`. Refuse this process, every ancestor
(cmd / `start-backend.bat` / console), and PID ≤ 4. This is not
path-specific: job-end, hang retry, serve-dead, git timeout, boot
reap, shutdown, file holders. A leftover or holder that is OSM is
left alone; delete the clone anyway.

Order, always, on the **job-end** path:

1. Abort session (best effort).
2. Force-kill this job’s tree: git, **this** serve, tool children
   (`taskkill /F /T` / `SIGKILL` process group).
3. Kill leftovers whose cwd/argv is this clone; then file holders.
   On Windows do **not** snapshot every process (`Get-CimInstance
   Win32_Process` via PowerShell). That scan, after a successful job
   whose clone still exists, is killed by EDR (`Backend exited`).
   `_windows_process_rows` must not spawn PowerShell. `reap_path` and
   the leftover cwd/argv walk are Linux `/proc` only. Windows
   leftovers are Restart Manager file holders only. Never
   PEB-walk python/cmd/powershell or every PID. Restart Manager
   calls must set ctypes argtypes. The RmStartSession key buffer is
   `CCH_RM_SESSION_KEY+1` WCHARs (33), not 32. Query RM in a **child
   process** so a `rstrtmgr` access violation cannot kill OSM. Job-end
   kills the recorded tree, then `rd`. RM runs only if the clone is
   still there. If that child dies (nonzero / timeout) and the folder
   remains, spawn **one** more child (max two). Do not retry when the
   helper exits 0. Do not call `path_has_holders` after RM on Windows.
   Dashboard `/ws` uses live slot/queue counts — it must
   not `list_all()` every tick. Never `taskkill` the manager PID,
   its parent console, or PID ≤ 4. Do not reap a drive root. A
   holder-stop / delete / boot / shutdown exception must not skip
   clone delete, leave boot unfinished, or take down the process.
   `boot()` always ends ready to accept jobs.
4. Drop stale `.git/*.lock` only when no holder remains.
5. Sequential hard-delete with retries (Windows `rd /s /q \\?\…` +
   reserved names; Linux chmod + rmtree).

Never kill the manager. Never kill another job’s serve.
On a **serve-restart** outer retry (hang / timeout / serve dead),
stop after step 2 and start a new serve — do not delete the clone.
On an **incomplete** outer retry, do not enter this kill path at all.

### Logs

- Levels: DEBUG, INFO, WARNING, ERROR, CRITICAL.
- **App log** (whole process): `{data_dir}/logs/app.log`.
- **Crash log**: `{data_dir}/logs/crash.log` (uncaught exceptions,
  signals, faulthandler, clean vs abrupt exit). If Windows security
  kills the process, Python cannot write; the start script appends
  the exit code to `{project}/logs/wrapper-exit.log`.
- **Per-job logs**: `{data_dir}/logs/{jira_id}_{job_id}_{YYYYMMDD}_{HHMMSS}.log`
  (accept time, UTC). One file per job. `job_id` and `jira_id`
  also stay on each line.
- Tag every line with `job_id` and `jira_id` (contextvars).
- Never log a URL that still has userinfo. Redact
  `user:pass@`, Azure-style `user@`, and `:pass@`.
  Inbound `POST /jobs` logs the redacted `repo_url` only. A leftover
  `PAT` key is not logged.
- Create `{data_dir}` layout on startup if missing: `.temp`,
  `.serve`, `logs`, `jobs`, `queue.json`.

### Offline install

- Target installers never hit the network.
  - `install.bat` / `install.sh` — manager only. Create `.venv` with
    the bundled interpreter (`vendor/python/windows/python.exe`,
    `vendor/python/linux/bin/python3`, or
    `vendor/python/darwin-arm64|darwin-x64/bin/python3`), then install from
    `vendor/python-wheels` and check `web/dist`. Do not use a
    system Python. Recreate `.venv` from scratch each install.
    Each OS zip ships `settings.local.yaml` (`packaging/settings.local.windows.yaml`
    → `data_dir: C:\osm`, `packaging/settings.local.linux.yaml` →
    `data_dir: /var/lib/osm`). The combined Windows+Linux zip ships both
    templates; `install.bat` / `install.sh` copy the matching one if
    `settings.local.yaml` is missing. On Linux, if `/var/lib/osm` is not
    writable and the overlay is absent **or still names** `/var/lib/osm`,
    `install.sh` rewrites it to `$XDG_DATA_HOME/osm` or
    `~/.local/share/osm`. A custom `data_dir` in the overlay is left
    alone. The code default stays `/var/lib/osm` on Linux and `C:\osm`
    on Windows. Do not require root for `./install.sh` then `./start.sh`.
    Restore `+x` on the zip-root launchers (some extractors drop Unix
    modes).
  - `install-opencode.bat` / `install-opencode.sh` — OpenCode CLI
    only. Install root is `<user>/.opencode` (Windows:
    `%USERPROFILE%\.opencode`, not AppData). Detect that folder,
    delete it, then copy `vendor/bin` from scratch. Run the helper
    with the bundled Python, not PATH.
- CI (`packaging/build_dist.py`, workflow **Offline Distribution**)
  produces four zips: `*-windows-x64.zip`, `*-linux-x64.zip`,
  `*-darwin.zip`, and `*-windows-linux.zip` (Windows + Linux). Each
  has that OS’s bundled CPython, matching wheels, OpenCode CLI, and
  `web/dist`. Do not ship `agents/` in the zip. Native wheels
  (`PyYAML`, `pydantic-core`) must exist for that OS. Do not
  `pip download --platform win_amd64` of `uvicorn[standard]` as-is:
  host markers still require `uvloop` (no Windows wheel) and the
  whole Windows set is skipped.
- Additive single-file exe (`packaging/build_exe.py`, same workflow,
  native Windows + Linux jobs): `opencode-manager-<ver>-windows-x64.exe`
  and `opencode-manager-<ver>-linux-x64`. The artifact is that one
  file. It starts API + SPA (`:4096`) and the `:5173` proxy in-process
  (`opencode_manager.standalone`). It does **not** replace the zip, does
  **not** change `start.bat` / `start.sh`, and does **not** vendor Git
  or OpenCode (PATH, plus `~/.opencode/bin` prepended). Build on that
  OS — no cross-compile. `agents/` is not in the exe. The exe artifact
  zip is the binary plus `settings.local.yaml` (Windows `C:\osm`, Linux
  `/var/lib/osm`). Keep both in the same folder. Windows does **not**
  fall back to `%LOCALAPPDATA%\osm`. If Linux `/var/lib/osm` is not
  writable, the exe falls back to `$XDG_DATA_HOME/osm` or
  `~/.local/share/osm`.
- No npm on the target. `start-frontend` is the Python SPA proxy
  (`dashboard.frontend_proxy`), not Vite.
- Do not vendor Git, Codex, `glab`, portable Node, or
  oh-my-opencode.

### Dashboard (GET only)

- Jobs tab only (`/jobs`, `/jobs/:jobId`). Same tech stack and look
  as virtual_developer `web/` (React + Vite + Tailwind + Geist).
- Visualization only. The UI and `/api/*` never POST / PATCH / DELETE.
  No cancel, delete, settings, schedules, or storage actions.
  **Report issue** is in the sidebar (pick a job or general) and
  on job detail. Client-built zip from GET data. The note is not
  stored. Job zip: note, meta, runtime, safe settings, queue,
  app.log, crash.log, wrapper-exit.log, recent OpenCode CLI logs,
  job record, parameters, attempts, prompts, chat (json+md), OSM
  job log, this job's OpenCode serve log, clone/git explanation
  (no live clone scan — the tree is deleted at job end).
  `GET /api/report-context` is process extras (redacted, capped).
  `GET /api/jobs/:id/logs?limit=0` is the whole manager log;
  `GET /api/jobs/:id/serve-log` is `{data_dir}/.serve/{job_id}.log`
  (redacted). Default logs `limit=2000` stays for the Logs tab.
- Persist a history row per `job_id`. Keep it after
  clone delete and after boot leftover ERROR. 409 is only running or
  queued. Job JSON is parsed with `json.loads` + `model_validate`
  (not `model_validate_json`). Rows over 50 MB are skipped.
  `list_all` may reuse an in-memory copy for `CACHE_TTL_SECONDS`
  (3s); `save` drops that cache before the write. Live `/ws` ticks
  use `Manager.live_counts()` (running slots + queue length), not
  `list_all()`.
- Job detail: meta + attempts, prompts we POSTed, chat transcript
  (live serve or snapshot), per-job log lines for that
  `job_id`, then that job’s OpenCode serve log. Chat must work after
  the clone is gone. Live `/chat` calls OpenCode only when
  `session_id` is `ses_*`. Empty / `-1` / other placeholders are no
  session — do not GET `/session/-1/message`. After the serve is
  dead, `/api/jobs/:id/chat` is **this job’s snapshot** — do not
  replace it from global `opencode.db` by `session_id` (later jobs
  reuse the same `ses_*` / clone path). Filling missing tool
  `output` on snapshot message ids is ok; never append later turns.
  Navigating to an unknown id must not keep the previous job on screen.
- Jobs list filters (All / In flight / Error / Completed) run on
  the server (`GET /api/jobs?filter=` + `jira_id` + page) so page
  25 is the filtered set. Queue is `GET /api/queue?jira_id=`.

## Do not copy from virtual_developer

Jira poller, GitLab MR/`glab`, Codex, dashboard **writes**, Poll /
Scheduled / Sessions / Storage / Settings tabs, settings PAT map
  (this product has no PAT field),
“create `feature/KEY` from target”, shared long-lived serve, “never
kill serve”, keeping a dirty clone for reuse.

Do copy: git no-console-prompt env (Windows GCM stored cred or popup), process-tree kill, Windows hard delete,
compact-wait / one-nudge control loop, outer retry **shape**,
per-job log context, jobs-tab SPA look (GET-only).

## Fixed regressions

Closed defects live in [agents/fixed-conditions.md](agents/fixed-conditions.md)
and `tests/test_fixed_conditions.py`. Do not re-report those as open
bugs. If you change one of those paths, update the tests in the same
change.

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/) for
every commit on **this** repo:

```text
<type>(<optional-scope>): <short explanation>
```

| Type | Use for |
|---|---|
| `feat` | New capability (API, worker, dashboard) |
| `fix` | Bug fix |
| `test` | Tests only |
| `docs` | Documentation only (`AGENTS.md`, `PLAN.md`, `agents/`) |
| `refactor` | Internal restructure, no behaviour change |
| `perf` | Performance |
| `chore` | Tooling, deps, non-user-facing maintenance |

Scopes are optional. Prefer: `api`, `worker`, `git`, `serve`,
`session`, `queue`, `cleanup`, `dashboard`, `web`, `settings`.

Subject: imperative, ~72 characters, no trailing period. Body is
optional; use it when the why is not obvious.

```text
fix(git): delete clone after GitError
test(serve): retry health timeout as serve-dead
docs(agents): add commit message convention
```

Do not use: `WIP`, `update stuff`, `bug fix`, `final`, or default
merge titles (`Merge branch …`, `Merged from …`).

This repo does not open product MRs. Do not use the target-clone
form `[PROJ-123] type: …` here.

## Before you change behaviour

If a change touches serve lifetime, session resume, clone path,
cleanup, callback counts, or the dashboard GET contract, update
**PLAN.md and this file** in the same change. Do not leave a third
implicit design.
