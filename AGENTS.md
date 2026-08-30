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

- Inbound HTTP is only an ack (`202` / `409` / `400` / `503`). Never hold
  the socket for clone or OpenCode. `503` means the process is not
  accepting (`boot` not finished, or shutting down). No callback.
- Exactly one POST goes to that job’s `callback_url`, and only when
  the job is terminal (`200` / `404` / `500` / `504`).
  - Accepted job (inbound `202`, queued or started): 1 terminal callback.
  - `409` / inbound `400`: no callback.
  - Never POST `queued` or `in_progress`. n8n wait-node URLs fire once;
    the HTTP ack already says queued vs started.
- Dedup key is **`jira_id`**. Running or queued → `409`. Do not
  enqueue a second job for the same ticket.
- Capacity full + **other** tickets → queue. Persist the queue
  (including `callback_url`) so a **running** process can dequeue
  after a slot frees. A process restart does **not** auto-run
  leftover work — see Boot and shutdown.

### Boot and shutdown

These are process-lifecycle rules. Do not mix them with hang retry.

- **While booting: do not start any job.** Do not dequeue. Do not
  clone. Do not start `opencode serve`. Do not resume a leftover
  running or queued job. Do not send callbacks for leftovers. Do
  not accept `POST /jobs` until boot is finished.
- **Boot cleanup is process hygiene only.** Kill leftover
  `serve_pid` / `extra_pids` recorded on running or queued rows.
  Then reap orphans whose cwd/argv is `work_dir` (leftover serves,
  git, tools) on Windows and Linux. Mark leftover running/queued
  rows **ERROR** in the job-history store so the dashboard can show
  them. They are not live, so those `jira_id`s are not `409`. Do
  **not** “handle” those jobs (no OpenCode, no terminal callback,
  no re-enqueue).
- **While shutting down:** stop accepting `POST /jobs`. Force-kill
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

- Clone with the **request PAT** when the caller sent one. `PAT` is
  optional: omit or leave empty for a **public** HTTPS repo. Disable
  credential helpers (`GIT_TERMINAL_PROMPT=0`, empty
  `credential.helper`). PAT must not appear on `git` argv, in logs,
  or in callbacks. No PAT ⇒ no auth header (do not fall back to the
  OS credential store). Private remotes without a PAT fail in the
  worker (callback **500**), not as inbound **400**.
- GitLab: `oauth2` + PAT. TFS / Azure DevOps: Basic `base64(":PAT")`
  (empty username). Detect from the URL. Never send GitLab auth to TFS.
- Reject `git@` / `ssh://`.
- `source_branch` must exist on the remote. Do not create a branch
  from `main`. Missing field on the body → inbound **400**. Missing
  on the remote → inbound **202**, worker checks, callback **404**.
  Never return a sync 404 for a missing remote ref.
- One settings root: `data_dir` (Windows `C:\osm`, Linux
  `/var/lib/osm`). Clones live under `{data_dir}/.temp`. Folder name is the **ticket id**
  (`jira_id`, Windows-safe). Dedup is one live job per ticket, so
  repo and branch are not part of the path. Two tickets ⇒ two
  folders. Same ticket later ⇒ same folder (hard-delete, then clone).
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
  `remote.origin.url` — never `git remote get-url` under the PAT env
  (`insteadOf` rewrites get-url to `oauth2:PAT@host` even when disk is
  already clean).

### OpenCode

- Start `opencode serve --hostname 127.0.0.1 --port <free>`. Never
  hardcode `4096`. Record `{pid, port, base_url, cwd}` on the job
  **immediately after Popen**, before the health wait.
- If `GET /global/health` is not 200 in time: kill **this** child,
  fail **this** attempt (`serve-dead`). Outer `retry_count` applies.
  Do not leave the process up.
- After health, check `GET /config/providers` (then `/provider`).
  If the request `model` is not on this serve: fail the **job**
  `500` immediately with the available ids. Do not POST a user
  message. Do not hang-wait. Do not spend remaining `retry_count`.
- Do not run `opencode --auto` as a one-shot CLI. Do not enable
  permission auto-approve.
- Scope requests with `x-opencode-directory: <clone>`.
- Request `model` (`provider/id`) is required. Send it on every
  user message as `{ providerID, modelID }`. No settings default.
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
- While status is `compacting` / `busy_compacting`, that **is**
  progress. Do **not** run the hang clock (compact may last minutes
  with no new markers).
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

Order, always, on the **job-end** path:

1. Abort session (best effort).
2. Force-kill this job’s tree: git, **this** serve, tool children
   (`taskkill /F /T` / `SIGKILL` process group).
3. Kill leftovers whose cwd/argv is this clone; then file holders.
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
- **Per-job logs**: `{data_dir}/logs/{jira_id}_{job_id}_{YYYYMMDD}_{HHMMSS}.log`
  (accept time, UTC). One file per job. `job_id` and `jira_id`
  also stay on each line.
- Tag every line with `job_id` and `jira_id` (contextvars).
- Never log the PAT or a URL that still has userinfo. Redact
  `user:pass@`, Azure-style `user@` (PAT as username), and `:pass@`.
  Inbound `POST /jobs` logs the redacted `repo_url` only — never the
  `PAT` field.
- Create `{data_dir}` layout on startup if missing: `.temp`,
  `.serve`, `logs`, `jobs`, `queue.json`.

### Offline install

- Target installers never hit the network.
  - `install.bat` / `install.sh` — manager only. Create `.venv` with
    the bundled interpreter (`vendor/python/windows/python.exe` or
    `vendor/python/linux/bin/python3`), then install from
    `vendor/python-wheels` and check `web/dist`. Do not use a
    system Python. Recreate `.venv` from scratch each install.
  - `install-opencode.bat` / `install-opencode.sh` — OpenCode CLI
    only. Install root is `<user>/.opencode` (Windows:
    `%USERPROFILE%\.opencode`, not AppData). Detect that folder,
    delete it, then copy `vendor/bin` from scratch. Run the helper
    with the bundled Python, not PATH.
- CI (`packaging/build_dist.py`, workflow **Offline Distribution**)
  produces **one** zip for Windows and Linux: bundled CPython for
  both, matching wheels, `vendor/bin/opencode.exe` +
  `vendor/bin/opencode`, and `web/dist`.
- No npm on the target. `start-frontend` is the Python SPA proxy
  (`dashboard.frontend_proxy`), not Vite.
- Do not vendor Git, Codex, `glab`, portable Node, or
  oh-my-opencode.

### Dashboard (GET only)

- Jobs tab only (`/jobs`, `/jobs/:jobId`). Same tech stack and look
  as virtual_developer `web/` (React + Vite + Tailwind + Geist).
- Visualization only. The UI and `/api/*` never POST / PATCH / DELETE.
  No cancel, delete, settings, schedules, or storage actions.
  **Report issue** on job detail is a local zip download (note +
  job JSON + prompts + chat + OSM job log + OpenCode serve log).
  The note is not stored. `GET /api/jobs/:id/logs?limit=0` is the
  whole manager log; `GET /api/jobs/:id/serve-log` is
  `{data_dir}/.serve/{job_id}.log` (redacted). Default logs
  `limit=2000` stays for the Logs tab.
- Persist a history row per `job_id` (no PAT). Keep it after
  clone delete and after boot leftover ERROR. 409 is only running or
  queued.
- Job detail: meta + attempts, prompts we POSTed, chat transcript
  (live serve or snapshot), per-job log lines for that
  `job_id`, then that job’s OpenCode serve log. Chat must work after the clone is gone. After the serve
  is dead, `/api/jobs/:id/chat` is **this job’s snapshot** — do not
  replace it from global `opencode.db` by `session_id` (later jobs
  reuse the same `ses_*` / clone path). Filling missing tool
  `output` on snapshot message ids is ok; never append later turns.
  Navigating to an unknown id must not keep the previous job on screen.
- Jobs list filters (All / In flight / Error / Completed) run on
  the server (`GET /api/jobs?filter=` + `jira_id` + page) so page
  25 is the filtered set. Queue is `GET /api/queue?jira_id=`.

## Do not copy from virtual_developer

Jira poller, GitLab MR/`glab`, Codex, dashboard **writes**, Poll /
Scheduled / Sessions / Storage / Settings tabs, settings PAT map,
“create `feature/KEY` from target”, shared long-lived serve, “never
kill serve”, keeping a dirty clone for reuse.

Do copy: PAT env isolation, process-tree kill, Windows hard delete,
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
