# AGENTS.md — OpenCode Session Manager

This file is binding for anyone implementing or changing this repo.
The long rationale lives in [PLAN.md](PLAN.md). If this file and the
plan disagree, **fix the plan** — do not silently pick a third design.

This is a small Windows/Linux worker between **n8n** and **OpenCode**.
It is not Yaver / virtual_developer. No Jira poller, no GitLab MR, no
Codex, no dashboard.

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

## Hard rules

### API and jobs

- Inbound HTTP is only an ack (`202` / `409` / `400`). Never hold the
  socket for clone or OpenCode.
- Lifecycle POSTs go to that job’s `callback_url`.
  - Over capacity: queued → in_progress → terminal (3).
  - Under capacity: in_progress → terminal (2).
  - `409` / inbound `400`: no callback.
- Dedup key is **`jira_id`**. Running or queued → `409`. Do not
  enqueue a second job for the same ticket.
- Capacity full + **other** tickets → queue. Persist the queue
  (including `callback_url`) so a process restart does not drop work.

### Git

- Clone with the **request PAT only**. Disable credential helpers
  (`GIT_TERMINAL_PROMPT=0`, empty `credential.helper`). PAT must not
  appear on `git` argv, in logs, or in callbacks.
- GitLab: `oauth2` + PAT. TFS / Azure DevOps: Basic `base64(":PAT")`
  (empty username). Detect from the URL. Never send GitLab auth to TFS.
- Reject `git@` / `ssh://`.
- `source_branch` must exist on the remote. Do not create a branch
  from `main`. Missing field on the body → inbound **400**. Missing
  on the remote → inbound **202**, worker checks, callback **404**.
  Never return a sync 404 for a missing remote ref.
- Clones live under `work_dir`. Defaults: Windows `C:\osm\.temp`,
  Linux `/var/lib/osm/.temp`. Folder name identity is **`jira_id` +
  repo + source branch**. Two tickets ⇒ two folders. Same ticket +
  same repo + same branch later ⇒ same folder.
- Mid-job retries: **do not delete** the clone. Delete only when the
  job is finished.

### OpenCode

- Start `opencode serve --hostname 127.0.0.1 --port <free>`. Never
  hardcode `4096`. Record `{pid, port, base_url, cwd}` on the job.
- Auto-approve via serve config (the meaning of `--auto`). Do not
  run `opencode --auto` as a one-shot CLI.
- Scope requests with `x-opencode-directory: <clone>`.
- OpenCode only. No Codex.

### Session id — two moments

| Moment | Unusable / rejected `ses_*` |
|---|---|
| **New job** (first serve of this request) | Create a **new** session. Log INFO. Do not fail the job. Return the id actually used. |
| **Mid-job hang retry** (we already had a live id, clone still on disk) | **Fail this attempt.** Do not open a blank session and continue. Counts against `retry_count`. |

Empty / non-`ses_*` / Codex UUID on a new job = create new, not an error.

### Prompts

The incoming `prompt` (`ORIGINAL`) is sent **once**, first turn of the
first attempt. Never send it again.

| When | Send |
|---|---|
| First turn | `ORIGINAL` |
| Model asked a live question | `UNATTENDED_NUDGE` (at most once per job) |
| Compact loop aborted, session idle | `COMPACT_LOOP_NUDGE` |
| Outer retry: hang / serve dead / HTTP | `HANG_RESUME` |
| Outer retry: incomplete, not compact | `INCOMPLETE_RESUME` |

Exact strings: PLAN.md §5.3. Do not invent a fifth resume prompt.

Never POST a user message while the session is `busy` / compacting.

### Compact vs hang

- Healthy compact can take minutes. **Wait.** That is not a retry.
- Do not send “Continue” during compact (races OpenCode’s loop).
- While status is `compacting` / `busy_compacting`, that **is**
  progress. Do **not** run the hang clock (compact may last minutes
  with no new markers).
- Hang watchdog: `busy` **and not compacting** **and** no new
  message / compact marker for `hang_timeout_seconds` → **outer
  retry**: abort → kill **this** serve → new serve, same path, same
  `session_id` → `HANG_RESUME`. Same if serve is dead.
- `retry_count` counts those restarts, not compact-wait.
- `timeout_in_seconds` is **one OpenCode attempt** (serve boot +
  session loop). Clone / cleanup / callbacks are outside it.
- Each outer retry **resets** that clock. `retry_count = 3` and
  timeout `1800` ⇒ up to **5400s** of OpenCode (`retry_count` is
  attempts, first included).
- Drive the turn with a **poll loop**. Do not block `/message` for
  the whole attempt budget.
- Attempt clock hits zero, or hang watchdog fires → abort, kill this
  serve, next attempt if any remain (`HANG_RESUME`). None left →
  delete clone, callback `504`.

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
On outer retry, stop after step 2 and start a new serve — do not
delete the clone.

### Logs

- Levels: DEBUG, INFO, WARNING, ERROR, CRITICAL.
- **App log** (whole process): `{project_root}/logs/app.log`.
- **Per-job logs**: named by **`jira_id`**. Windows
  `C:\osm\logs\{jira_id}.log`, Linux `/var/lib/osm/logs/{jira_id}.log`.
  Append across runs of the same ticket. `correlation_id` stays on
  the line, not in the filename.
- Tag every line with `correlation_id` and `jira_id` (contextvars).
- Never log the PAT or a URL that still has userinfo.
- Create `work_dir` and `job_log_dir` on startup if missing.

## Do not copy from virtual_developer

Jira poller, GitLab MR/`glab`, Codex, dashboard, settings PAT map,
“create `feature/KEY` from target”, shared long-lived serve, “never
kill serve”, keeping a dirty clone for reuse.

Do copy: PAT env isolation, process-tree kill, Windows hard delete,
compact-wait / one-nudge control loop, outer retry **shape**,
per-job log context.

## Before you change behaviour

If a change touches serve lifetime, session resume, clone path,
cleanup, or callback counts, update **PLAN.md and this file** in the
same change. Do not leave a third implicit design.
