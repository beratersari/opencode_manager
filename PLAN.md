# OpenCode Session Manager — Plan

A small Windows/Linux worker that sits between **n8n** and **OpenCode**.
n8n sends a job. This service clones the repo with the request PAT, runs
OpenCode in serve mode, drives the session until the task is finished, then
calls a target API with the result.

This is a smaller slice of [virtual_developer](https://github.com/beratersari/virtual_developer)
(Yaver). Copy clone/PAT isolation, force-kill, hard delete, the OpenCode
serve control loop, and the **jobs-tab dashboard look** (GET-only). Do
**not** copy Jira polling, GitLab MRs, Codex, or dashboard write
actions (cancel, delete, settings, schedules).

The n8n JSON in this repo is only a fragment of the old poll loop
(`prompt_async` → wait 60s → `GET /sessions`). That loop becomes **internal**.
Do not treat it as the public contract.

---

## 1. What I think you want

n8n today has to know too much: clone? session? is OpenCode still compacting?
retry? cleanup? file locks on Windows?

You want n8n to do this instead:

1. POST a job (repo, PAT, branch, prompt, model, optional session, Jira id, timeout, retries, **callback_url**).
2. Get an immediate ack (accepted / queued / already running / bad request).
3. Later receive **exactly one** callback on the **`callback_url` from that request** (the caller, usually an n8n Wait node). That POST is the terminal result — never a “queued” or “started” ping.
4. On success, the callback `text` is the last OpenCode assistant message.
   On failure, `text` is a real error a human can act on.

This service owns everything in the middle: concurrency, queue, clone, serve,
session resume, state machine, retries, process kill, disk cleanup, per-job logs.

It is a **session/job orchestrator**, not a coding platform. It does not push
branches or open MRs unless we add that later.

---

## 2. Decisions locked in this plan

These are the defaults I would implement. Change them before coding if they
are wrong.

| # | Topic | Decision |
|---|---|---|
| 1 | Product of a job | **Text only.** Last assistant output (or error). No git push, no MR. Clone is disposable. |
| 2 | How OpenCode runs | **One `opencode serve` process per job**, unique localhost port, auto-approve via config. **Not** `opencode --auto`. **Not** one shared serve for all jobs. Kill that serve when the job ends. See §3.2. |
| 3 | Cleanup vs resume | **Always delete the clone, then re-clone to the same stable path.** Identity is **`jira_id` + repo + source branch** (Windows-safe short name / digest). Ticket makes two jobs unique; repo+branch is part of the key. OpenCode sessions live in the global `opencode.db` keyed by `directory`. Same identity ⇒ old `session_id` should resolve. **No live `ses_*` yet** (inbound unusable, or first serve died before create) → create a new session (do not fail). **Mid-job hang retry** (we already had a live id, clone still on disk): same `ses_*` or that attempt fails — never invent a blank session. `ORIGINAL` only chooses the prompt: first user message until that POST succeeds; hang restart after that POST is `HANG_RESUME`. Workspace vs chat drift after delete is **intentional** (§3.3). Live e2e: `tests/test_session_resume_same_path_live_e2e.py`. |
| 4 | Sync vs async | Incoming HTTP is only an ack. The **per-request `callback_url`** gets **one terminal POST** (success or fail). Never `queued` / `in_progress`. No global target in settings. |
| 5 | Dedup key | **`jira_id`**. One live job (running or queued) per ticket. `session_id` is only for OpenCode resume. |
| 6 | Git auth | **Request PAT only.** No OS credential store, no settings PAT, no SSH. GitLab and TFS/Azure DevOps use different header schemes. |
| 7 | Source branch | Must exist on the remote. Missing field on the body → inbound **400**. Missing on the remote → inbound **202**, then callback **404**. Do not invent a branch from `main`. |
| 8 | Codex | Never. OpenCode only. |

If any row is wrong, the implementation will be wrong in a hard-to-undo way.
Especially rows 1 and 3.

---

## 3. Conflicts I already resolved (and why)

### 3.1 Serve vs `--auto`

You asked for both. They are different programs:

- `opencode serve` is an HTTP server. Create/resume session, POST a prompt,
  poll until idle. Matches n8n and virtual_developer.
- `opencode --auto` is a one-shot CLI. No stable session HTTP API.

`--auto` in this plan means **permission auto-approve** (the agent may run
tools without a human). That is an OpenCode config on the per-job serve,
not the CLI flag.

### 3.2 Shared serve vs per-job serve

**Decision: one `opencode serve` process per job.** This is the more
sensible shape for *this* app. virtual_developer’s shared serve is
cheaper, but it fights the cleanup rules we actually have.

#### Why per-job is the right default

The process that holds the clone open is `opencode serve` (cwd, file
watcher, tool children). We must **always** hard-delete the clone,
including when Windows / antivirus still has the tree locked, and we
must **force-kill** git, OpenCode, and anything else used for that job.

If serve is shared:

- Killing it aborts every other live session.
- So we cannot kill OpenCode.
- Then delete becomes “protect serve, retry, hope the lock goes away”
  — the hard virtual_developer problem.

If each job has its own serve, the end of a job is simple and isolated:

1. Abort **this** session (best effort).
2. Force-kill **this** serve and its descendants (`taskkill /F /T` /
   `SIGKILL` process group).
3. Sequential hard-delete of **this** clone, with retries.

No other job is affected. That is the whole point of per-job serve.

`max_concurrent_jobs` is the cap on how many of these serves exist at
once. That is how we pay for isolation without unbounded RAM.

#### Ports — there is no conflict

A clash only happens if every job binds the **same** port (the usual
`4096`). We will not do that.

| Process | Port |
|---|---|
| This manager (n8n → us) | One settings port (`listen_port`). Public/internal API. |
| Job A `opencode serve` | Ephemeral localhost, e.g. `127.0.0.1:45123` |
| Job B `opencode serve` | Ephemeral localhost, e.g. `127.0.0.1:45124` |

How we pick the job port:

- `opencode serve --port` defaults to **0** = OS assigns an unused port.
- We pre-bind `127.0.0.1:0`, read the assigned port, close, then start
  serve with `--hostname 127.0.0.1 --port <that>`.
- Record `{pid, port, cwd}` on the job. All HTTP to OpenCode for that
  job uses only that base URL.
- Those ports are never published. n8n never sees them.

The live e2e already does this (`_free_port()`). Two jobs at once
cannot steal each other’s port.

#### Lifecycle of one job’s serve

1. Clone is on disk at the stable path.
2. Allocate a free port. Start
   `opencode serve --hostname 127.0.0.1 --port <free>`
   with cwd = clone. Auto-approve is config on this process
   (the serve equivalent of `--auto`).
3. Wait until `GET /global/health` is 200, or fail this attempt
   (outer `retry_count` may apply).
4. Create or resume `session_id`. Send `x-opencode-directory: <clone>`.
5. Drive the session until done / timeout / retries exhausted.
6. Abort session. Force-kill **this** pid tree. Do not touch the
   manager. Do not touch another job’s `{pid, port}`.
7. Hard-delete the clone.

On manager crash: leftover serves are orphans. Startup **reaps
processes** (recorded live pids, or anything bound to `work_dir`).
It does **not** resume those jobs, dequeue the old queue, or send
callbacks. Mark leftover live/queued rows history **ERROR** (not
live, so not `409`) so a later `POST /jobs` for the same `jira_id`
is a new job. Do not accept `/jobs` until boot is finished. See §5.1.

#### What we track per job

- `serve_pid`
- `serve_port`
- `serve_base_url` (`http://127.0.0.1:<port>`)
- `clone_path`
- `session_id`
- extra child pids we spawned (git)

Kill uses this list first, then a workspace cwd/argv sweep as backup.

#### Disadvantages (accepted costs)

| Cost | What it means in practice |
|---|---|
| **RAM** | A live serve on this host was ~300–500 MB RSS. Two concurrent jobs ≈ 1 GB, four ≈ 2 GB, **just for OpenCode**. `max_concurrent_jobs` is the budget knob. |
| **Startup time** | Each attempt waits for serve to boot (a few seconds) before the first prompt. That time counts toward **that attempt’s** `timeout_in_seconds`. |
| **More processes** | We must track pid + port per job so we never kill the wrong tree. More processes can fail to start. |
| **Crash isolation cuts both ways** | A hung serve only kills one job. We also have more processes that can fail independently. |
| **Resume is not “same process”** | History lives in the global `opencode.db`, keyed by `directory`. The next job starts a **new** serve on the **same path** and resumes `ses_*`. The live e2e proved this works. We do not keep the old HTTP process. |

#### When a shared serve *would* be better

One long-lived serve (virtual_developer’s model) is simpler and cheaper
**if** we did not need to kill OpenCode and did not always delete the
workspace. Then sessions are just rows on one multiplexer, one port,
one health check.

That is a bad fit for *these* rules:

- always hard-delete the clone
- force-kill OpenCode
- survive “file locked / OpenCode still holding the tree / AV lock”

So we do **not** use a shared serve. If RAM later hurts, we lower
`max_concurrent_jobs`. We do not switch architecture to save memory
and then fail cleanup.

#### What we explicitly will not do

- Do not hardcode port `4096` (or any single port) for job serves.
- Do not leave a job’s serve running after the clone is deleted.
- Do not kill serve A while cleaning job B.
- Do not kill the manager process.
- Do not expose job-serve ports off localhost.

### 3.3 Cleanup vs `session_id` continue

OpenCode sessions live in the **global** `opencode.db` (not inside the
clone). The session row stores `directory`. Deleting the clone does
**not** delete the session.

Operator correction: if the next job clones back to the **same absolute
path**, OpenCode should accept the old `session_id` and keep history.

So:

- Always hard-delete the workspace when the job ends (success or fail).
- Name clones with a **stable** identity of **`jira_id` + repo +
  source branch** (short Windows-safe folder / digest of those three).
  Not a timestamp. `jira_id` is what makes two tickets different
  folders even when repo and branch match. Same three fields later ⇒
  the same path so OpenCode can find the session.
- Persist `session_id`. Next job for that ticket: re-clone to that path,
  start a new per-job serve, try the inbound id.
- **No live `ses_*` yet** (inbound empty / not `ses_*` / rejected, or
  the first serve died before create) → **create a new session**. That
  is not a job failure. Return the id that was actually used. First
  user message is `ORIGINAL`.
- **Mid-job hang retry** (we already had a live `ses_*`): resume that
  **same** id. If OpenCode rejects it, **this attempt fails**. Do not
  open a blank session and pretend it is a continue. Outer
  `retry_count` may try again; if the id is still dead after retries,
  fail the job. First user message on that resume is `HANG_RESUME`
  only if `ORIGINAL` was already POSTed; otherwise still `ORIGINAL`.
- **Workspace vs session drift is intentional.** After delete, the next
  clone is a clean remote tree. Chat history may mention files that
  are no longer dirty. Product is the last assistant **text**, not the
  working tree. Do not keep the clone to make files match the session.
- Live proof: `tests/test_session_resume_same_path_live_e2e.py`
  (real git clone, real `opencode serve`, real free model, no mocks).
  Passed 2026-08-27: delete clone, re-clone to the same path, `GET /session/{id}`
  → 200, first-turn history still present, second real model turn on the
  same `ses_*`.

### 3.4 Holding the HTTP request vs calling n8n back

A clone plus several OpenCode attempts can last a long time (clone is
uncapped by the request timeout; OpenCode can use
`retry_count * timeout_in_seconds`).
Do not hold the inbound HTTP connection.

n8n’s HTTP response is the ack. The “outgoing request” in your spec is
a **separate POST** to the `callback_url` that arrived on that same job.

### 3.5 “Already in progress” vs queue

Different cases:

- Capacity full, **other** tickets → queue (inbound **202**; one terminal callback later).
- Same `jira_id` already running or already queued → **reject now**.
  Do not enqueue a second job for the same ticket.

---

## 4. Public contract

### 4.1 Incoming (n8n → this service)

`POST /jobs` (name can change; one write endpoint is enough).

| Field | Required | Meaning |
|---|---|---|
| `repo_url` | yes | HTTPS clone URL. GitLab or TFS/Azure DevOps Git. SSH rejected. |
| `PAT` | yes | Used **only** for this job’s git. Never stored in settings, never logged. |
| `source_branch` | yes | Remote branch that must already exist. Start point of the clone. |
| `session_id` | no | If it is a live OpenCode `ses_*` we can resume, continue it. Else create new. |
| `prompt` | yes | User text sent as the session turn. |
| `model` | yes | OpenCode model name, `provider/id` (e.g. `opencode/hy3-free`). Sent on every user message for this job. Missing, empty, or not `provider/id` → **400**. No settings default. OpenCode rejects it later → fail the attempt (callback **500** if none remain). |
| `agent_mode` | yes | OpenCode agent name (`build`, `plan`, `general`, …). Unknown → 400. |
| `timeout_in_seconds` | yes | Wall clock for **one OpenCode attempt only** (serve boot + session loop). Not clone, not cleanup, not callbacks. Resets on every outer retry. |
| `retry_count` | yes | Max **OpenCode attempts** (first included). `3` with timeout `1800` ⇒ up to **5400 seconds** of OpenCode. Not compact-wait. Minimum `1`. |
| `jira_id` | yes | Dedup key. Ties the run to n8n/Jira. |
| `callback_url` | yes | Absolute `http(s)` URL we POST the **terminal** result to. This is the caller (n8n Wait-node URL), **not** a setting on this server. Missing or non-http(s) → **400**. |

Do **not** infer the target from `Host`, `Origin`, or `Referer`. n8n wait-node URLs are unique per execution (`…/webhook-waiting/<id>/…`) and fire **once** (the first POST resumes the Wait node). The site origin is not enough. The caller must send the exact URL it wants the result on.

Because that URL is one-shot, we never POST `queued` or `in_progress` to it. Queued vs started lives only on the inbound HTTP ack. The callback is the terminal result only.

Persist `callback_url` on the job record so a queued job still knows where to POST after dequeue **in this process**. A process restart does not send that callback — leftovers become history **ERROR** on boot (§5.1).

### 4.2 Immediate HTTP response

Always fast. Never wait for OpenCode.

| Situation | HTTP | Body idea |
|---|---|---|
| Same `jira_id` running or queued | **409** | Already in progress. Include the existing `job_id` and `session_id` if we have one. **No callback.** |
| Capacity full | **202** | Queued. `job_id`, `session_id` (incoming or empty). One terminal callback later. |
| Capacity free, accepted | **202** | Started. One terminal callback later. |
| Missing/invalid fields, SSH URL, unknown agent, bad `model` | **400** | Error text. No callback. |
| `source_branch` missing on remote | **202** then callback **404** | Check in the worker after accept (§6.3). Not a sync 400. |

Every HTTP body (and every callback) uses the same shape so n8n has one parser:

```json
{
  "text": "Job accepted and is now in progress.",
  "session_id": "ses_… or empty",
  "status_code": 202,
  "jira_id": "PROJ-123",
  "job_id": "job_…"
}
```

`job_id` is minted here (`job_…`). n8n does not send it. It is on
every log line and is the dashboard / history key. The **per-job log
file** is named by **`jira_id`**, not by `job_id`.

### 4.3 Callbacks (this service → request `callback_url`)

Same JSON. **One POST per accepted job**, when the job is finished. Never a
`queued` or `in_progress` callback — n8n wait-node URLs fire on the first
POST, and the inbound HTTP ack already said queued vs started.

| Phase | When | `status_code` | `text` |
|---|---|---|---|
| success | OpenCode finished | 200 | **Last assistant message** |
| failed | After retries / timeout / clone error | 4xx/5xx | Actionable error, no PAT |

`404` (missing remote branch), `500` (exhausted retries / crash), and `504`
(attempt timeout, no attempts left) are the failure codes.

Callback counts:

- Accepted job (inbound **202**, queued or started): **1** terminal callback.
- 409 / 400 on the inbound request: **0** callbacks.
- `status_code` **202** is inbound-only. It never appears on a callback.

If the target API is down, retry the callback a small fixed number of
times (e.g. 3) and then log. The job itself is already finished; do not
re-run OpenCode because n8n missed a webhook.

### 4.4 Status codes (shared vocabulary)

| Code | Meaning |
|---|---|
| 200 | OpenCode finished; `text` is the last assistant output |
| 202 | Inbound ack only: queued or started. Never a callback. |
| 400 | Bad request / unusable input |
| 404 | `source_branch` does not exist on the remote |
| 409 | This `jira_id` already has a live job |
| 500 | Failed after retries (serve crash, incomplete, unexpected) |
| 504 | An OpenCode attempt hit `timeout_in_seconds` and no attempts remain |

---

## 5. Runtime behaviour

```
n8n  --POST /jobs-->  manager
                         │
                         ├─ 409 already in progress (same jira_id)  — no callback
                         ├─ 202 queued
                         │        └─ later worker slot
                         └─ 202 started
                                │
                                ├─ detect GitLab vs TFS
                                ├─ clone with request PAT only
                                ├─ if source_branch missing on remote: cleanup + callback 404
                                ├─ start opencode serve --hostname 127.0.0.1 --port <free> (cwd = clone)
                                ├─ resume session_id or create
                                ├─ POST prompt, wait all states, outer retry
                                ├─ abort + force-kill tree
                                ├─ sequential hard-delete clone
                                └─ one callback 200 / 4xx / 5xx (text, session_id, status_code)
```

### 5.1 Concurrency and queue

- `max_concurrent_jobs` in the settings file.
- Running count = jobs that have a worker (clone/serve in flight).
- Queued jobs sit on disk so **this** process can dequeue when a slot
  frees. Persist `callback_url` on the row. A **process restart does
  not** auto-run leftover queued or running work.
- When a running job hits a terminal state, dequeue the next FIFO item
  and run the same pipeline. Do **not** send an `in_progress` callback.
- Same `jira_id` already running **or** queued → 409. Do not stack.

**Boot:** do not start any job. Do not listen for `POST /jobs` until
boot is finished. Reap orphan processes on `work_dir`. Leftover
running/queued rows become history **ERROR** (no callback, no
OpenCode). They stay visible on the dashboard. They are not live, so
that `jira_id` is not `409`.

**Shutdown:** stop accepting `/jobs`. Kill every job process tree.
Mark every running and queued job ERROR. Terminal callback `500` for
each. Then job-end delete. History rows stay (dashboard).

**Next request:** after ERROR (including boot leftover ERROR), a new POST
for that `jira_id` is a **new** job. That worker sequential-hard-deletes
the leftover clone path first (if present), then clones. Resume
`session_id` only if the caller sent one and it is still valid. Do not
recover interrupted work on the next boot. Boot itself does not delete
those trees.

After a job is **success/fail/timeout/ERROR**, a new POST for that
`jira_id` is a **new** job.

### 5.2 Timeout (OpenCode attempt only)

`timeout_in_seconds` is **not** a whole-job deadline. It applies only
to **one OpenCode attempt**:

`serve boot + create/resume session + inner loop (including healthy compact wait)`

It does **not** cover clone, `ls-remote`, kill, delete, or callbacks.
Those use their own settings limits if we need a safety cap
(`git_clone_timeout_seconds` in settings — not the request field).

Each outer retry **resets** the clock. Serve-restart path: a new serve
+ a new `timeout_in_seconds` window. Incomplete path: same serve + a
new `timeout_in_seconds` window.

Example (operator’s numbers): `retry_count = 3`, `timeout_in_seconds = 1800`

| Attempt | OpenCode budget |
|---|---|
| 1 | 1800s |
| 2 (after hang/timeout/error) | 1800s, fresh |
| 3 | 1800s, fresh |
| **Max OpenCode time** | **5400s** (not 5400 minutes) |

`retry_count` is the number of attempts, **first included**. `1` = one
try, no restart. `3` = up to three OpenCode runs. Values `< 1` are
treated as `1`.

When an attempt’s 1800s fires: abort, kill **this** serve, clone stays.
If attempts remain → new serve, same path, new 1800s. Prompt is
`HANG_RESUME` and the same `session_id` only if `ORIGINAL` was already
POSTed; otherwise create if needed and send `ORIGINAL`. If none remain
→ delete clone, callback `504`.

`hang_timeout_seconds` (settings, progress watchdog) can end an
attempt **early** when OpenCode is busy, **not compacting**, and
nothing new appears. While status is `compacting` / `busy_compacting`,
that **is** progress — do **not** run the hang clock (a healthy
compact is one long LLM call and may have no new markers for minutes).
Start the hang clock only when the session is `busy` and **not**
compacting. A hang still consumes one attempt and resets the next
window. This must not be the `/message` HTTP timeout (see §5.3).

### 5.3 Retry and hangs (two loops + serve restart)

OpenCode **does** hang. Compaction is the most reported cause. Public
issues include: stuck after compact / `models.dev` never returns
([#3053](https://github.com/anomalyco/opencode/issues/3053));
auto-compact loops until it stops answering
([#30680](https://github.com/anomalyco/opencode/issues/30680));
synthetic “Continue…” racing compact
([#15533](https://github.com/anomalyco/opencode/issues/15533));
`opencode serve` REST sessions left `busy` forever
([#6573](https://github.com/anomalyco/opencode/issues/6573));
compact request itself over the context limit
([#15849](https://github.com/anomalyco/opencode/issues/15849));
compact never succeeding ([#24249](https://github.com/anomalyco/opencode/issues/24249));
compact running in parallel with the next step
([#41358](https://github.com/anomalyco/opencode/issues/41358)).
`virtual_developer` already treats this as first-class (KAN-95:
compact-wait ate the full job timeout).

A **healthy compact** can take tens of seconds to several minutes.
That is a long summarize, not a deadlock. Killing serve on the first
`busy` / `compacting` would abort work that would have finished.

Copied in spirit from `agent_runner` + `opencode_serve`, **OpenCode only**.
No Codex paths.

During retries the **clone stays on disk**. We only delete it at the
end of the job (success or fail). Restarting serve mid-job is
kill-process + start a new serve on the **same path**, same `session_id`.

`retry_count` is max OpenCode attempts (first included). See §5.2.
Do **not** retry a clean finish.
Do **not** count a healthy compact-wait as a retry.
Do **not** share one timeout across all attempts.

#### Inner loop — not a retry

After a prompt is posted, stay on **this serve and this session**
until OpenCode is actually idle:

| Observation | Action |
|---|---|
| Compact / `Session auto-compacted` / `busy_compacting` | **Wait.** No new user message. No “Continue”. |
| `tool-calls` / unfinished finish **and the session is still busy** | Wait. |
| Compact recap that quotes “Shall I…?” | Still compact, not a live question. Wait. |
| Clarifying question (live, last turn stopped) | **One** unattended nudge (see prompts below), then wait. Never re-send the original prompt. |
| Compact-only loop (many new compact markers, no work turn; ~8 cycles) | Abort the in-flight turn. Wait until the **same** session is **idle**. Then one compact-loop nudge. Do **not** Continue while compact is still running. |
| Real last-turn `stop`, not premature (or the only leftover signal is OpenCode todos) | **Success.** Leave the loop. Product is last assistant text — leftover todos are not a delivery gate. |
| Still asking after the one unattended nudge | **Fail the job** (`500`). No second nudge. No `INCOMPLETE_RESUME`. |
| Compact-related leftover after wait / after `COMPACT_LOOP_NUDGE` already used | **Fail the job** (`500`). Do not send `INCOMPLETE_RESUME` (it races compact). |
| Session **idle**, last finish unfinished (`tool-calls` / null / not a clean `stop`), not compact, not a live question | **Incomplete.** Leave the inner loop. Do not wait for the hang clock. |

This is what “handle all states of OpenCode until it is done” means.
The n8n 60s poll is a crude version. Implement the real serve control
loop, not a blind sleep.

Hang detector **inside** the wait (still not a retry by itself):

- Drive the turn with a **poll loop** (start the prompt, then poll
  status + messages). Do not block on `/message` for the whole
  attempt budget — that hides hangs until the attempt is already
  dead (KAN-95). Use `prompt_async` if present; otherwise `/message`
  in the background plus a watchdog.
- Hang clock: session is `busy` **and not compacting** **and** no new
  message / compact marker for `hang_timeout_seconds` (settings,
  default ~180s, **not** `timeout_in_seconds`). That ends **this
  attempt** early.
- While status is `compacting` / `busy_compacting`, **reset / do not
  start** the hang clock. Compact-in-progress counts as progress.
- Serve process died or `/global/health` fails.
- This attempt’s `timeout_in_seconds` clock hits zero.

Those escalate to the **outer** loop as a **serve restart**.

Copied from virtual_developer’s `agent_runner` + `opencode_serve` shape
(error vs incomplete vs timeout), minus Codex, Jira categories, and
git/MR delivery gates. Incomplete is **not** a hang. The session is
already idle; killing serve is the hang/crash path only.

#### Outer loop — `retry_count`

Two different outer sequences. Do not mix them.

**A. Serve restart** — hang watchdog / serve dead / attempt clock /
transport / unexpected HTTP:

1. `POST /session/{id}/abort` (best effort).
2. Force-kill **this job’s** serve tree. Other jobs untouched.
   Clone is **not** deleted.
3. Backoff (`delay * 2^n`, capped).
4. Start a **new** serve on the **same clone path**, new free port.
5. Session: if we **already had a live `ses_*`**, resume that id.
   Do **not** create a new session. If resume fails, this **attempt**
   fails (log ERROR). If we **never had a live id** (boot / create
   died first), this is still §5.4 A — create a session. Do not
   silently start a blank session and pretend a mid-job continue
   happened.
6. POST `HANG_RESUME` only if `ORIGINAL` was already POSTed this
   job. If it was never POSTed, POST `ORIGINAL` (once). Never send
   `ORIGINAL` a second time.
7. Return to the inner loop. Fresh `timeout_in_seconds`.

**B. Same serve** — inner left with **incomplete** (idle, unfinished
finish, not compact, not still-asking):

1. Do **not** abort. Do **not** kill serve. Clone stays.
2. Backoff (`delay * 2^n`, capped).
3. POST `INCOMPLETE_RESUME` on this serve and this `session_id`.
4. Return to the inner loop. Fresh `timeout_in_seconds`.

This consumes one `retry_count` attempt (first included), same as a
serve restart. If no attempts remain → fail the job, then the normal
kill + delete + callback `500`.

If attempts are exhausted after a **serve-restart** path: fail the job,
then the normal kill + delete + callback `500` / `504` (`504` if the
last attempt was an attempt-timeout, `500` otherwise).

#### Which prompt is sent

The incoming `prompt` (`ORIGINAL`) is sent **once**, the first time a
serve can accept a user message. Key off **whether that POST happened**,
not attempt number. Every later user message is a short orchestrator
prompt. Replaying the task text after it is already in history
duplicates work, blows context, and can trigger compact again.

| When | Prompt id | What we send |
|---|---|---|
| First user message of this job (`ORIGINAL` not yet POSTed) | `ORIGINAL` | The request `prompt`, unchanged. Includes attempt 2+ if attempt 1 died in boot / health / session create. |
| Inner: model asked a live question | `UNATTENDED_NUDGE` | See text below. At most **once** per job. |
| Inner: compact loop aborted and session is idle | `COMPACT_LOOP_NUDGE` | See text below. At most **once** per wait stretch. |
| Outer A **after** `ORIGINAL` was POSTed: hang / HTTP budget / serve dead | `HANG_RESUME` | See text below. |
| Outer B: incomplete (same serve; idle unfinished finish, not compact) | `INCOMPLETE_RESUME` | See text below. |
| Outer A: other error after serve restart, `ORIGINAL` already POSTed | `HANG_RESUME` | Same as hang. One resume text for “something died, continue”. |

**Never send**

- `ORIGINAL` again after that one POST succeeded.
- Any user message while status is `busy` / compacting.
- A second `UNATTENDED_NUDGE` if the model asks again after the first
  (fail the job `500` — do not nag, do not hang-retry).

**`UNATTENDED_NUDGE`**

```
You are running unattended — there is no human in the loop and no one
will answer questions. Do not ask clarifying questions, confirmation,
or multiple-choice options. Choose the safest defaults consistent with
the repository and the original task. Finish all remaining work.
Do not restart from scratch.
```

**`COMPACT_LOOP_NUDGE`**

```
Auto-compact looped and was aborted. Stay in this session.
Do not start another compaction cycle. Finish remaining work from
the current files and conversation. Do not restart from scratch.
Do not ask clarifying questions.
```

**`HANG_RESUME`** (outer restart — this is the retry prompt)

```
The last turn stopped early (timeout, hang, or the OpenCode server was
restarted). Stay in this session. Do not start another compaction cycle.
Do not restart from scratch. Finish remaining work from the current
files and conversation. Do not ask clarifying questions.
```

**`INCOMPLETE_RESUME`**

```
Finish remaining todos and complete the original task in this session.
Do not restart from scratch. Do not ask clarifying questions.
```

Why not the original prompt on retry **after it was sent**: the session
already has that text in history (same `ses_*`, same path). A second
copy looks like a new task. The hang/resume text tells the model to
**continue**. If `ORIGINAL` never landed, there is no history to
continue — send the task.

### 5.4 Session id rules

Two different moments. Do not mix them.

**A. No live `ses_*` yet** (inbound id unused, or first serve died
before create)

- Valid inbound = OpenCode accepts it (`ses_*`) on this serve + this
  clone path.
- Empty, not `ses_*`, Codex UUID, or OpenCode rejects the id →
  **create a new session**. Log why at INFO. Do **not** fail the job.
- Return the id that was **actually** used (resumed or newly created).
- First user message is `ORIGINAL`.

**B. Mid-job hang / crash retry** (clone still on disk, we already had
a live `ses_*`)

- Must resume **that same** id on the new serve.
- If OpenCode rejects it → this attempt **fails** (ERROR). Count against
  `retry_count`. Never substitute a new session and keep going as if
  history were intact.
- After retries are exhausted, fail the job.
- First user message: `HANG_RESUME` if `ORIGINAL` was already POSTed;
  otherwise `ORIGINAL` (session was created, then the serve died
  before the task POST).

Always put the live id on every callback.

---

## 6. Git

### 6.1 PAT isolation (must)

virtual_developer already solved the “Windows GCM popped a prompt” class
of bugs. Copy that pattern, but the PAT comes from **the request**, not
from settings/`GITLAB_PAT`.

For every git child of a job:

- `GIT_TERMINAL_PROMPT=0`
- GCM / credential helper **off** (`credential.helper=`)
- no GUI askpass / no `DISPLAY`
- PAT **not** on `git` argv (so it does not appear in `ps`)
- rewrite via `GIT_CONFIG_*` insteadOf + askpass / `http.extraHeader`
- after clone, origin URL scrubbed of any userinfo
- PAT redacted from every log and every callback
- never fall back to the machine credential store if the PAT is wrong —
  fail with a clear “auth failed” error

SSH (`git@`, `ssh://`) is rejected at the API. A PAT cannot authenticate SSH.

### 6.2 GitLab vs TFS / Azure DevOps

Detect from `repo_url`. Do not send GitLab’s `oauth2:PAT` to TFS.

| Kind | How we tell | Auth |
|---|---|---|
| GitLab | default if not TFS-shaped | username `oauth2`, password = PAT (insteadOf + Basic `oauth2:PAT`) |
| Azure DevOps Services | `dev.azure.com`, `visualstudio.com` | Basic `base64(":PAT")` extraHeader |
| TFS / Azure DevOps Server | `/tfs/`, `/_git/`, typical on-prem hosts | same empty-user Basic; on-prem often **requires** empty username |

If a host is ambiguous, prefer TFS rules when the path contains `/_git/`
or `/tfs/`.

### 6.3 Clone steps

1. Classify host, build isolated git env from **this** PAT.
2. In the **worker** (after the inbound **202**), `git ls-remote --heads`
   (or clone then checkout) to prove `source_branch` exists. If it does
   not: cleanup anything already created, callback **404**, stop. Do
   not invent a branch from `main`.
3. Clone under `work_dir` into a short **stable** Windows-safe path
   whose identity is **`jira_id` + repo + source branch** (e.g.
   `{work_dir}/{ticket}_{digest12}` of those three).

   | OS | Default `work_dir` |
   |---|---|
   | Windows | `C:\osm\.temp` |
   | Linux | `/var/lib/osm/.temp` |

   No timestamp. Same three fields ⇒ same folder. Different `jira_id`
   ⇒ different folder even when repo and branch are identical. Same
   ticket with a different repo or branch ⇒ a different folder.

   **New job only:** if that path already exists (crash leftover,
   failed prior delete, same identity later), sequential hard-delete
   it first, then clone. Do **not** `git clone` into a non-empty dest.
   Mid-job outer retry: leave the existing tree. Boot does not delete
   leftover trees.
4. Checkout `source_branch` exactly. No “create from main”.
5. Submodules: only if present; same PAT env. Fail the job if they fail
   (do not let OpenCode run on a half tree).

**Locked:** missing remote branch is **202 + callback 404**. Existence
check needs the PAT and the network; it runs in the worker so the
inbound handler stays fast. n8n must treat 202 as “accepted, wait for
callback” — including this 404.

Empty / omitted `source_branch` on the JSON body is still inbound
**400** (bad request, no callback). That is a missing field, not a
missing remote ref.

No target branch. No push. No MR.

---

## 7. OpenCode serve (per job)

See §3.2 for why this is per job and how ports work.

1. Allocate a free local port (`bind(127.0.0.1, 0)`). Never reuse a
   fixed port like 4096 for more than one live job.
2. Start `opencode serve` with:
   - `--hostname 127.0.0.1 --port <free>`
   - working directory = clone
   - auto-approve / permission allow (serve equivalent of `--auto`)
   - store `{pid, port, base_url, cwd}` on the job immediately
3. Wait until `GET /global/health` is 200, or fail this attempt
   (outer retry may apply). Boot time counts toward **this attempt’s**
   `timeout_in_seconds`.
4. Create or resume session; set agent from `agent_mode`.
   Send `x-opencode-directory: <clone>`. On every user-message POST,
   send this job’s `model` as OpenCode `{ providerID, modelID }`
   (split on the first `/`). Do not pick a different model mid-job.
5. Start a prompt (poll loop, §5.3). First user message is `ORIGINAL`
   until that POST succeeds (even on a later attempt). After a
   hang/timeout **serve restart** where `ORIGINAL` already landed, use
   `HANG_RESUME`. After an idle incomplete (same serve) use
   `INCOMPLETE_RESUME`. Completeness uses status + message polls, not
   a single blocking `/message` for the whole attempt.
6. Inner loop until done, incomplete, or this attempt’s
   `timeout_in_seconds` hits zero (§5.3).
7. Capture **last assistant text** for the success callback.
8. Abort session. Force-kill **this** serve and every descendant.
   Do not touch other jobs’ `{pid, port}`. Do not touch the manager PID.
   On a **serve-restart** outer retry, leave the clone in place, start a
   new serve on the same path. Resume the same `session_id` and send
   `HANG_RESUME` only if `ORIGINAL` already landed; otherwise create if
   needed and send `ORIGINAL`. On an **incomplete** outer retry, keep
   this serve and send `INCOMPLETE_RESUME`. Delete the clone only when
   the job is finished.

Copy completeness helpers from `src/opencode_serve.py` and
`src/opencode_sessions.py` **selectively**. Strip Codex, Jira titles,
dashboard hooks. Do **not** copy “never kill the shared serve”.

---

## 8. Cleanup and process kill

Always run, including on failure and timeout. Order matters (this is
the virtual_developer lesson that avoided “Another git process seems
to be running” and Windows `nul` / AV locks):

1. Abort OpenCode session (best effort).
2. Force-kill this job’s process tree: git, serve, agent tools
   (`taskkill /F /T` on Windows, `SIGKILL` + process group on Linux).
   Never kill the manager. Never kill another job’s PIDs.
3. Kill leftover processes whose cwd/argv is this clone.
4. Kill holders of still-open files (Restart Manager on Windows; narrow
   `/proc` fd walk on Linux — do **not** scan all of `/proc` on WSL).
5. Drop stale `.git/*.lock` only when no holder remains.
6. Sequential hard delete with retries:
   - Windows: `rd /s /q \\?\…`, device-namespace `del` for reserved
     names (`nul`, `con`, …), chmod writable, retry/backoff
   - Linux: chmod + `rmtree`, retry
7. If remnants remain after retries, log ERROR with the path and still
   send the job’s terminal callback. Do not leave the job “running”
   because delete failed.

Copy `process_kill.py` + `temp_fs.py` ideas. Invert virtual_developer’s
“protect the shared serve” rule: **this job’s serve is supposed to die**.
Still protect the manager PID and every *other* job’s serve pid.

---

## 9. Logging

Levels: DEBUG, INFO, WARNING, ERROR, CRITICAL.

Two sinks, **two different roots**:

1. **App log** (whole process) — under the **project root**, not under
   `C:\osm` / `/var/lib/osm`:
   `{project_root}/logs/app.log`
2. **Per-job log** — one file per **Jira ticket**, outside the repo.
   Filename is the ticket id so operators can find it without the
   `job_id`. Sequential runs of the same ticket **append** to
   the same file (`job_id` on each line separates runs). Two
   jobs for the same ticket never run at once (409), so no concurrent
   writers.

| OS | Default job-log file |
|---|---|
| Windows | `C:\osm\logs\{jira_id}.log` |
| Linux | `/var/lib/osm/logs/{jira_id}.log` |

Create the directories on startup if they do not exist. Overridable
via `job_log_dir`.

Every line:

```
YYYY-MM-DD HH:MM:SS.mmm  LEVEL     [file:line]  function  [job_id=… jira_id=…]  message
```

Log: accept, queue position, clone start/end, branch check, serve start
and port, session create vs resume (and why resume failed), each OpenCode
state transition, each outer retry, kill counts, delete attempts, every
callback attempt and HTTP status.

Never log the PAT, never log a URL that still has userinfo.

Use contextvars so concurrent jobs do not mix ids (virtual_developer
`log_context.py`).

---

## 10. Settings file

Not env-only. A single file the operator can edit (YAML or TOML).

| Key | Role |
|---|---|
| `listen_host` / `listen_port` | Inbound API |
| `max_concurrent_jobs` | Worker cap |
| `callback_timeout_seconds` | Per callback HTTP timeout |
| `callback_retry_count` | If the caller URL is down |
| `callback_allowed_hosts` | Optional allow-list (SSRF). Empty = any http(s) URL from the request. |
| `work_dir` | Clone root. Default **Windows** `C:\osm\.temp`, **Linux** `/var/lib/osm/.temp` |
| `job_log_dir` | Per-job log root. Default **Windows** `C:\osm\logs`, **Linux** `/var/lib/osm/logs` |
| `log_level` | Minimum level for both sinks |
| `opencode_bin` | Path or name on PATH |
| `queue_path` | Persisted queue/job store |
| `job_store_dir` | Finished + live job history for the dashboard. Default **Windows** `C:\osm\jobs`, **Linux** `/var/lib/osm/jobs`. One JSON per `job_id`. Never write the PAT here. |
| `hang_timeout_seconds` | No-progress watchdog inside one attempt (default ~180). Runs only when `busy` **and not compacting**. Not the request timeout. |
| `git_clone_timeout_seconds` | Safety cap for clone / `ls-remote`. Request timeout does not apply to git. |

No GitLab PAT, no Jira token, no board id, no default model. Those belong to n8n.

---

## 11. What to copy from virtual_developer (and what not to)

Clone the repo for reference only. Do not import it as a dependency.

**Copy / slim down**

- `git_manager` PAT env: askpass, `GIT_CONFIG_*`, helper off, redact
- host classification we will **extend** for TFS
- `process_kill` tree kill, workspace reclaim, lock clearing
- `temp_fs.force_rmtree` Windows reserved names + `\\?\`
- `opencode_serve` + `opencode_sessions` completeness / compact wait /
  one nudge
- `agent_runner` outer retry shape (error vs incomplete vs timeout),
  minus Codex lock. Incomplete = same-serve finish-todos prompt, **not**
  a serve restart. Leftover todos-only after a clean `stop` is success
  here (no git/MR delivery gate).
- `log_context` + file logger layout
- short Windows-safe temp names
- jobs-tab SPA look: React + Vite + Tailwind + Geist, Jobs list + job
  detail tabs, chat transcript UI, `JobStore` one-JSON-per-job.
  **GET-only** — strip every write control.

**Do not copy**

- Jira poller, reporter, webhooks
- GitLab MR / `glab`
- Codex backend
- dashboard **writes** (cancel, delete, bulk-delete, settings PATCH, schedules, storage delete)
- Poll / Scheduled / Sessions / Storage / Settings / issue-detail tabs
- settings-level `GITLAB_HOST_PATS`
- “if source is main, create `feature/KEY` from target”
- shared long-lived `opencode serve` + “never kill serve”
- keeping a dirty clone for reuse (we delete every time; next job
  re-clones to the same `jira_id` + repo + branch path)

---

## 12. Suggested layout

Small Python service (same language as the reference, 3.11+).

```
opencode_manager/
  PLAN.md
  settings.yaml
  pyproject.toml
  src/opencode_manager/
    app.py              # HTTP server
    settings.py
    api.py              # POST /jobs
    models.py           # request/response
    queue.py            # persist + dispatch
    worker.py           # one job pipeline
    callback.py         # POST to target
    git/
      detect.py         # GitLab vs TFS
      clone.py          # PAT clone + branch check
      auth.py           # isolated env
    opencode/
      serve.py          # start/stop per-job serve
      session.py        # create/resume/prompt/assess
      retry.py          # outer retry
    cleanup/
      kill.py
      rmtree.py
    log.py
    log_context.py
    dashboard/
      api.py            # GET /api/jobs… + /ws only
      store.py          # job history JSON
      chat.py           # snapshot + live transcript
  web/                  # React + Vite + Tailwind SPA (jobs tab only)
  logs/                 # app log only: logs/app.log (project root)
# clones default:  Windows C:\osm\.temp   Linux /var/lib/osm/.temp
# job logs default: Windows C:\osm\logs    Linux /var/lib/osm/logs
# job history:      Windows C:\osm\jobs    Linux /var/lib/osm/jobs
```

Cross-platform: `os.name` branches only in kill + rmtree + a few git env
keys. Everything else is the same.

---

## 13. Implementation order

1. Settings + logger + `job_id` + per-job files.
2. HTTP `POST /jobs` + in-memory/disk queue + 409/202 rules + callbacks
   (fake worker first).
3. Git classify + PAT clone + missing-branch fail + scrub/redact.
4. Per-job serve boot + create/resume + prompt + last-assistant extract.
5. Inner completeness loop (compact / tools / one nudge).
6. Outer `retry_count`.
7. Per-attempt OpenCode timeout (resets on each retry).
8. Force-kill + sequential hard delete + retries.
9. Persist queue for in-process dequeue; boot does not auto-run leftovers;
    shutdown marks live jobs ERROR. Persist job-history rows for the dashboard.
10. Read-only jobs dashboard (SPA + GET APIs + live WS).
11. Tests: PAT never on argv / never in logs; GitLab vs TFS header;
    409 vs queue; one terminal callback (never queued/in_progress);
    missing branch; kill-then-delete; new job deletes leftover dest
    then clones;
    inbound invalid session_id creates a new one; hang-retry
    resume failure fails the attempt; idle incomplete uses
    INCOMPLETE_RESUME on the same serve; first-serve death before
    ORIGINAL still sends ORIGINAL on the next attempt;
    dashboard GET-only (writes 405); PAT never in job-history JSON;
    history survives restart; boot leftover is ERROR in history not 409.

---

## 14. Risks I would not hide

1. **Resume after delete depends on the clone path being identical.**
   That is the hypothesis the live e2e proves. If OpenCode ever keys
   sessions by a content hash or inode instead of the path string,
   we would have to keep the workspace or migrate the session row.
2. **Text-only + delete clone + resume session** means the next job’s
   chat may mention edits that are gone from disk. **Intentional.**
   The product is the last assistant message. Do not keep the workspace
   to stay consistent with history.
3. **On-prem TFS** auth is picky (empty username vs dummy user). We
   should try the documented empty-user Basic first, and make the
   fallback obvious in DEBUG logs (still no PAT).
4. **Per-job serve RAM.** ~300–500 MB RSS each on this host. Four
   concurrent jobs ≈ 2 GB for OpenCode alone. Cap with
   `max_concurrent_jobs`. Do not “fix” this by switching to a shared
   serve — that undoes kill + hard-delete.
5. **Missing remote branch is 202 + callback 404.** n8n must treat
   inbound 202 as “accepted, wait for callback”, including this case.
6. WSL + `/mnt/c` clones: full `/proc` walks can hang. Keep the narrow
   kill strategy from virtual_developer.

---

## 15. Still worth a yes/no from you

The table in §2 is the plan. These four are the ones that change the
shape if you disagree:

1. Text-only result — no push?
2. Per-job serve + auto-approve, not `opencode --auto`?
3. Always delete clone; re-clone to the same path and resume `session_id`?
4. Per-request `callback_url` (not a setting); HTTP is only the ack?

If those four stay yes, this file is the spec to implement.

---

## 16. Requirements (atomic)

Check these off during implementation. One box = one done thing. Do not
merge boxes. Non-goals at the bottom are also requirements (do **not**
build them).

### 16.1 Skeleton and settings

- [ ] Python 3.11+ package layout as in §12 (`src/opencode_manager/…`).
- [ ] Load settings from a YAML or TOML file (not env-only).
- [ ] Setting `listen_host` / `listen_port` for the inbound API.
- [ ] Setting `max_concurrent_jobs`.
- [ ] Setting `callback_timeout_seconds`.
- [ ] Setting `callback_retry_count`.
- [ ] Setting `callback_allowed_hosts` (empty = any http(s) URL).
- [ ] Setting `work_dir` default Windows `C:\osm\.temp`, Linux `/var/lib/osm/.temp`.
- [ ] Setting `job_log_dir` default Windows `C:\osm\logs`, Linux `/var/lib/osm/logs`.
- [ ] Setting `log_level` (DEBUG / INFO / WARNING / ERROR / CRITICAL).
- [ ] Setting `opencode_bin`.
- [ ] Setting `queue_path`.
- [ ] Setting `job_store_dir` default Windows `C:\osm\jobs`, Linux `/var/lib/osm/jobs`.
- [ ] Setting `hang_timeout_seconds` (default ~180).
- [ ] Setting `git_clone_timeout_seconds`.
- [ ] Create `work_dir`, `job_log_dir`, and `job_store_dir` on startup if missing.
- [ ] Create `{project_root}/logs/` on startup if missing.
- [ ] No GitLab PAT, Jira token, board id, or default model in settings.

### 16.2 Logging

- [ ] Log levels: DEBUG, INFO, WARNING, ERROR, CRITICAL.
- [ ] App sink: `{project_root}/logs/app.log` (whole process).
- [ ] Job sink: `{job_log_dir}/{jira_id}.log` (not `job_id`).
- [ ] Append to the same ticket file across sequential runs.
- [ ] Line format includes timestamp, level, file:line, function, `job_id`, `jira_id`, message.
- [ ] Bind `job_id` and `jira_id` with contextvars so concurrent jobs do not mix.
- [ ] Never write the PAT to any log.
- [ ] Never write a URL that still contains userinfo to any log.
- [ ] Redact the PAT from git stderr before logging.

### 16.3 Inbound HTTP

- [ ] `POST /jobs` accepts the JSON fields in §4.1.
- [ ] Inbound handler never waits for clone or OpenCode (ack only).
- [ ] Mint `job_id` on accept; n8n does not send it.
- [ ] Response/callback JSON always has `text`, `session_id`, `status_code`, `jira_id`, `job_id`.
- [ ] Missing required field → HTTP **400**, no callback.
- [ ] Empty / omitted `source_branch` on the body → HTTP **400**, no callback.
- [ ] `callback_url` missing or not `http`/`https` → HTTP **400**, no callback.
- [ ] Do not infer callback from `Host` / `Origin` / `Referer`.
- [ ] SSH `repo_url` (`git@`, `ssh://`) → HTTP **400**, no callback.
- [ ] Unknown `agent_mode` → HTTP **400**, no callback.
- [ ] Missing / empty / not `provider/id` `model` → HTTP **400**, no callback.
- [ ] `retry_count < 1` treated as `1`.
- [ ] Same `jira_id` already running → HTTP **409**, no callback.
- [ ] Same `jira_id` already queued → HTTP **409**, no callback.
- [ ] Capacity full, other ticket → HTTP **202** queued.
- [ ] Capacity free → HTTP **202** accepted.
- [ ] Never return a sync HTTP 404 for a missing remote branch.

### 16.4 Queue and concurrency

- [ ] Running count = jobs with a worker in flight (clone or serve).
- [ ] Persist queued jobs to `queue_path` so **this** process can dequeue when a slot frees.
- [ ] Persist `callback_url` (and the rest of the request needed to run) on the queue row.
- [ ] FIFO dequeue when a running job becomes terminal.
- [ ] On dequeue run the same pipeline; do **not** send an `in_progress` callback.
- [ ] After success / fail / timeout / ERROR, a new POST for that `jira_id` is a new job.
- [ ] While booting: do not start any job, do not dequeue, do not accept `POST /jobs`.
- [ ] On boot: reap orphan processes on `work_dir`; mark leftover running/queued as history ERROR (no callback, no OpenCode); they are not live (`409` off).
- [ ] On shutdown: stop accepting `/jobs`; kill every job tree; mark running and queued jobs ERROR; terminal callback `500` each; then job-end delete; keep history rows.
- [ ] A later `POST /jobs` for a boot leftover ERROR or shutdown-ERROR `jira_id` is a new job (not `409`).

### 16.5 Callbacks

- [ ] POST terminal JSON to the job’s `callback_url` only.
- [ ] Accepted job (queued or started): **1** terminal callback.
- [ ] Never POST `queued` or `in_progress` to `callback_url`.
- [ ] 409 / inbound 400: **0** callbacks.
- [ ] Callback `status_code` is never 202 (202 is inbound-only).
- [ ] Success callback: `status_code` 200, `text` = last assistant message.
- [ ] Failed callback: `status_code` 4xx/5xx, actionable `text`, no PAT.
- [ ] Missing remote branch: terminal callback `status_code` **404**.
- [ ] Last OpenCode attempt timed out and no attempts left: callback **504**.
- [ ] Other exhausted-retry / crash: callback **500**.
- [ ] Always put the live `session_id` on every callback.
- [ ] If callback HTTP fails, retry `callback_retry_count` times, then log; do not re-run OpenCode.
- [ ] Honor `callback_timeout_seconds` per callback POST.
- [ ] If `callback_allowed_hosts` is non-empty, reject callbacks to other hosts.

### 16.6 Git

- [ ] Classify host: GitLab vs Azure DevOps Services vs TFS / Azure DevOps Server.
- [ ] Ambiguous URL with `/_git/` or `/tfs/` uses TFS rules.
- [ ] GitLab auth: username `oauth2`, password = request PAT (`GIT_CONFIG_*` insteadOf + Basic).
- [ ] Azure DevOps / TFS auth: Basic `base64(":PAT")` extraHeader (empty username).
- [ ] Never send GitLab `oauth2:PAT` to a TFS / Azure DevOps host.
- [ ] Every git child: `GIT_TERMINAL_PROMPT=0`, credential helper off, no GUI askpass, no `DISPLAY`.
- [ ] PAT never appears on `git` argv.
- [ ] After clone, origin URL has no userinfo.
- [ ] Wrong PAT fails closed (no OS credential-store fallback).
- [ ] Worker `ls-remote` (or equivalent) after HTTP 202 to prove `source_branch` exists.
- [ ] Missing remote branch: cleanup anything created, callback 404, stop.
- [ ] Do not create a branch from `main` / `master` / target.
- [ ] Clone under `work_dir` to a stable path of **`jira_id` + repo + source branch** (short digest, no timestamp).
- [ ] New job: if that stable path exists, sequential hard-delete it first, then clone. Not on boot. Not on mid-job retry.
- [ ] Same three fields ⇒ same folder; different `jira_id` ⇒ different folder.
- [ ] Same ticket with a different repo or branch ⇒ a different folder.
- [ ] Checkout `source_branch` exactly.
- [ ] If `.gitmodules` exists, update submodules with the same PAT env; fail the job on submodule error.
- [ ] Honor `git_clone_timeout_seconds` for clone / `ls-remote` / submodules.
- [ ] Track git child PIDs on the job for kill.
- [ ] No push, no MR, no target branch.

### 16.7 OpenCode serve (per job)

- [ ] One `opencode serve` process per job. Not a shared serve.
- [ ] Do not run `opencode --auto` as a one-shot CLI.
- [ ] Auto-approve via that serve’s OpenCode config.
- [ ] Allocate a free localhost port (`bind(127.0.0.1, 0)`). Never hardcode 4096.
- [ ] Start `opencode serve --hostname 127.0.0.1 --port <free>` with cwd = clone.
- [ ] Record `{serve_pid, serve_port, serve_base_url, clone_path, session_id}` immediately.
- [ ] Job HTTP to OpenCode uses only that job’s `serve_base_url`.
- [ ] Do not publish job-serve ports off localhost.
- [ ] Wait for `GET /global/health` 200; fail this attempt if boot fails.
- [ ] Serve boot time counts toward **this attempt’s** `timeout_in_seconds`.
- [ ] Send `x-opencode-directory: <clone>` on OpenCode requests.
- [ ] Every user-message POST sends this job’s `model` as `{ providerID, modelID }` (split on first `/`). Same model for the whole job. No settings default.
- [ ] OpenCode only. No Codex code paths.

### 16.8 Session and prompts

- [ ] New job, usable inbound `ses_*`: resume it.
- [ ] No live `ses_*` yet (inbound empty / non-`ses_*` / Codex UUID / rejected, or first serve died before create): create a new session; do not fail the job; log INFO.
- [ ] Return the session id that was actually used.
- [ ] Mid-job hang retry: resume the **same** `ses_*` only.
- [ ] Mid-job resume rejected: this **attempt** fails (ERROR); do not create a blank session and continue.
- [ ] First user message of the job sends `ORIGINAL` (request `prompt`) once — including attempt 2+ if attempt 1 died before that POST.
- [ ] Never send `ORIGINAL` again after that POST succeeded.
- [ ] Serve-restart after `ORIGINAL` was POSTed: `HANG_RESUME`. Serve-restart before `ORIGINAL` was POSTed: `ORIGINAL`, not `HANG_RESUME`.
- [ ] Live clarifying question: send `UNATTENDED_NUDGE` at most once per job (exact text in §5.3).
- [ ] Second clarifying question after the nudge: fail the job (`500`); no second nudge; no `INCOMPLETE_RESUME`.
- [ ] Compact loop aborted and session idle: send `COMPACT_LOOP_NUDGE` at most once per wait stretch (exact text in §5.3).
- [ ] Outer hang / serve-dead / attempt-timeout restart **after** `ORIGINAL` was POSTed: send `HANG_RESUME` (exact text in §5.3).
- [ ] Outer incomplete (idle unfinished finish, not compact, not still-asking): send `INCOMPLETE_RESUME` on the **same** serve (exact text in §5.3). Do **not** kill serve.
- [ ] Never POST a user message while status is `busy` or compacting.
- [ ] Do not invent a fifth resume prompt.
- [ ] Success `text` is the last assistant message.

### 16.9 Inner loop (not a retry)

- [ ] Drive the turn with a poll loop (`prompt_async` or background `/message` + watchdog).
- [ ] Do not block on `/message` for the whole attempt budget.
- [ ] Compact / `busy_compacting` / `Session auto-compacted`: wait; no new user message; no “Continue”.
- [ ] `tool-calls` / unfinished finish **while busy**: wait.
- [ ] Compact recap that quotes “Shall I…?” is not a live question; wait.
- [ ] Compact-only loop (~8 new compact markers, no work turn): abort the turn; wait until **idle**; then one `COMPACT_LOOP_NUDGE`.
- [ ] Real last-turn `stop`, not premature (or only leftover todos): success; leave the loop.
- [ ] Still asking after the one unattended nudge: fail the job (`500`); no `INCOMPLETE_RESUME`.
- [ ] Compact-related leftover after wait / after `COMPACT_LOOP_NUDGE`: fail the job (`500`); no `INCOMPLETE_RESUME`.
- [ ] Session idle, last finish unfinished, not compact, not a live question: leave inner as **incomplete** (do not wait for hang).
- [ ] Healthy compact-wait does not consume `retry_count`.

### 16.10 Timeout, hang, outer retry

- [ ] `timeout_in_seconds` covers only one OpenCode attempt (boot + session loop).
- [ ] Clone, `ls-remote`, kill, delete, callbacks are outside `timeout_in_seconds`.
- [ ] Each outer retry resets the attempt clock (fresh `timeout_in_seconds`).
- [ ] `retry_count` is max attempts, first included (`3` × `1800` = 5400s OpenCode).
- [ ] Do not retry a clean finish.
- [ ] Hang clock runs only when status is `busy` **and not compacting** and no new message/marker for `hang_timeout_seconds`.
- [ ] While `compacting` / `busy_compacting`, do not start or run the hang clock.
- [ ] Serve death or `/global/health` fail ends this attempt.
- [ ] Attempt clock hitting zero ends this attempt.
- [ ] On serve-restart attempt end (hang / timeout / crash / transport): abort session (best effort).
- [ ] Then force-kill **this** serve tree; do **not** delete the clone.
- [ ] Backoff `delay * 2^n` (capped) before the next attempt.
- [ ] Next serve-restart attempt: new serve, same clone path, new free port; same `session_id` + `HANG_RESUME` if `ORIGINAL` was POSTed; create if needed + `ORIGINAL` if it was not; new timeout window.
- [ ] On incomplete (same serve): do **not** abort or kill serve; POST `INCOMPLETE_RESUME`; new timeout window; counts against `retry_count`.
- [ ] Attempts exhausted after attempt-timeout → cleanup, callback **504**.
- [ ] Attempts exhausted otherwise → cleanup, callback **500**.

### 16.11 Kill and cleanup

- [ ] Job-end order: abort session → kill this tree → kill leftover cwd/argv holders → kill file holders → drop stale git locks → sequential hard-delete with retries.
- [ ] Windows kill: `taskkill /F /T`. Linux kill: `SIGKILL` process group.
- [ ] Never kill the manager PID.
- [ ] Never kill another job’s serve PID.
- [ ] Windows delete: `rd /s /q \\?\…` plus reserved-name (`nul`, `con`, …) handling.
- [ ] Linux delete: chmod writable + rmtree, retry.
- [ ] Drop `.git/*.lock` only when no holder remains.
- [ ] Delete the clone on success **and** on failure / timeout / 404-branch.
- [ ] Do not delete the clone on a mid-job outer retry.
- [ ] If delete still leaves remnants: log ERROR, still send the terminal callback; do not leave the job “running”.
- [ ] Chat vs disk drift after delete+reclone is not a failure.

### 16.12 Tests

- [ ] PAT never appears on `git` argv.
- [ ] PAT never appears in logs or callbacks.
- [ ] GitLab vs TFS/Azure DevOps use different auth headers.
- [ ] 409 when the same `jira_id` is running or queued.
- [ ] Accepted job (capacity full or free) → exactly one terminal callback; 409/400 → 0; never a `queued` / `in_progress` POST.
- [ ] Missing / empty / not `provider/id` `model` → inbound **400**, no callback.
- [ ] Missing remote branch → inbound 202 + callback 404.
- [ ] Kill-then-hard-delete even when files are locked (retry).
- [ ] New job after leftover dest exists: hard-delete that path first, then clone (not `git clone` into non-empty).
- [ ] Inbound invalid `session_id` creates a new session (job succeeds path).
- [ ] Hang-retry resume failure fails the attempt (no blank session).
- [ ] If attempt 1 dies before `ORIGINAL` is POSTed, attempt 2 sends `ORIGINAL` (not `HANG_RESUME`).
- [ ] Idle incomplete (unfinished finish, not compact) sends `INCOMPLETE_RESUME` on the same serve; does not kill serve.
- [ ] Live e2e: delete clone, re-clone same path, same `ses_*` still works (`tests/test_session_resume_same_path_live_e2e.py`).

### 16.13 Non-goals (must not build)

- [ ] No Jira poller, reporter, or Jira webhook.
- [ ] No GitLab MR / `glab`.
- [ ] No Codex backend.
- [ ] No dashboard writes (no cancel / delete / settings / schedules / storage from the UI).
- [ ] No Poll, Scheduled, Sessions, Storage, Settings, or issue-detail tabs.
- [ ] No settings-level GitLab PAT map.
- [ ] No “create `feature/{KEY}` from target if source is main”.
- [ ] No shared long-lived `opencode serve`.
- [ ] No keeping a dirty clone for reuse.
- [ ] No git push.

---

## 17. Dashboard (visualization only)

Copy the **jobs tab** from virtual_developer’s `web/` SPA. Same tech
stack and the same look. It is **read-only**. n8n still owns
`POST /jobs`. The dashboard never starts, stops, retries, or deletes
a job.

virtual_developer pages we **do not** build: Poll, Scheduled,
Sessions, Storage, Settings, issue detail (`/tasks/:key`).

### 17.1 Product

- Same process and `listen_host` / `listen_port` as `POST /jobs`.
- SPA routes: `/` → `/jobs`, `/jobs`, `/jobs/:jobId`. Nothing else.
- Live running jobs and the queue are visible. Terminal jobs stay
  visible after clone delete and after process restart.
- 409 is only **running or queued**. History rows (including boot
  ERROR) do not 409.

### 17.2 Tech stack (lock to virtual_developer `web/`)

React 19, TypeScript, Vite, Tailwind 4 (`@tailwindcss/vite`),
react-router-dom 7, react-markdown + remark-gfm, Geist / Geist Mono
(`@fontsource-variable/geist*`), oxlint. Copy `web/src/index.css`
tokens, `PageHeader`, `StatusBadge`, `Tabs`, `MetaCard`, `PromptBlock`,
`MarkdownBody`, `LiveDot`, `Spinner`, job cards. Re-skin names from
Yaver to this service. Do not add a second UI kit.

Dev: Vite `:5173`, proxy `/api` and `/ws` to `listen_port`. Prod:
serve `web/dist` from the manager (same MIME workaround as VD on
Windows).

### 17.3 What the UI shows (and what it must not)

**Jobs list** (VD `JobsPage` / `JobsTable`, minus writes)

- Cards: `jira_id`, `job_id`, status badge, live dot, `agent_mode`,
  `model`, started_at, error preview.
- Filters: All / In flight / Queue / Error / Completed. No Cancelled
  tab (shutdown/boot leftovers are Error). No bulk-select, no Delete,
  no queue Cancel.
- Search by `jira_id`. Paginate like VD (page size 25).
- Queue filter is GET-only: `jira_id`, position, accepted_at. No PAT.

**Job detail** (VD `JobDetailPage` tabs, slimmed)

| Tab | Source |
|---|---|
| **Details** | History row: ids, status, live, `agent_mode`, `model`, `session_id`, redacted `repo_url`, `source_branch`, `clone_path`, serve pid/port if live, timeout / retry_count / attempt n of m, timestamps, elapsed, error, callback `status_code`, last assistant **text** (the product). **Attempts table** like VD `retry_attempts`: number, kind (hang / timeout / incomplete / serve-dead / create-fail), prompt id sent, error, `session_id`, time. |
| **Prompt** | Exact user messages we POSTed: `ORIGINAL` plus `UNATTENDED_NUDGE` / `COMPACT_LOOP_NUDGE` / `HANG_RESUME` / `INCOMPLETE_RESUME` (id, text, time). |
| **Transcript** | Chat UI from VD `JobChatTab` (user / assistant / tool / compact). Live job: this job’s serve. After serve is dead: persisted snapshot. Never require the clone to still exist. No Codex path. |
| **Logs** | `{job_log_dir}/{jira_id}.log` lines for this `job_id` only. |

No Stop / Delete / Report. Refresh + live WS only.

**Do not show:** PAT, URLs with userinfo, MR / commit / feature branch /
delivery, Codex, Jira description, workflow_type, worker backend
(always OpenCode).

### 17.4 GET API (dashboard → manager)

All under `/api`. **GET and WebSocket only.** POST / PATCH / DELETE
on `/api/*` → **405**. `POST /jobs` (n8n) is unchanged and is not
under `/api`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/meta` | app name, version, server_time |
| GET | `/api/jobs` | page, page_size, `jira_id` filter. `{ jobs, total, page, page_size, server_time }` |
| GET | `/api/jobs/{job_id}` | one job + `system_logs` (this `job_id` only) |
| GET | `/api/jobs/{job_id}/prompts` | prompt artifacts |
| GET | `/api/jobs/{job_id}/chat` | transcript (live serve or snapshot) |
| GET | `/api/jobs/{job_id}/logs` | job-log lines for this id |
| GET | `/api/queue` | queued rows, no PAT, no `callback_url` secrets required on the card |
| WS | `/ws` | live snapshot: running count, queue count, generation. No settings/poll payload. |

404 if the id is unknown. Never put PAT in a response.

### 17.5 Persistence (required or the UI is empty)

Clone delete and boot-no-resume stay. History is a **new** store.

- Accept → write a history row keyed by `job_id`.
- Update status, session, attempts, prompts, chat snapshot, result
  text, error, callback `status_code` as the worker runs.
- Terminal (200 / 404 / 500 / 504 / shutdown ERROR / boot ERROR):
  keep the row. Delete the clone as today.
- Chat snapshot: persist messages while polling and again at job
  end. After kill-serve the snapshot is the transcript.
- History JSON never contains the PAT. Queue file may still hold
  PAT for in-process dequeue only; dashboard DTOs strip it.
- Same ticket, later POST: **new** `job_id`, new row. List
  shows both. Per-job log file still appends by `jira_id`.

### 17.6 Requirements (atomic)

- [ ] `web/` Vite app: React 19 + TS + Tailwind 4 + react-router-dom + react-markdown + Geist, same tokens/components as virtual_developer `web/`.
- [ ] Routes `/`, `/jobs`, `/jobs/:jobId` only. `/` redirects to `/jobs`.
- [ ] Nav is Jobs only. No Poll / Scheduled / Sessions / Storage / Settings / issue pages.
- [ ] Prod: manager serves `web/dist` on the same `listen_port`. Dev: Vite proxies `/api` and `/ws`.
- [ ] Jobs list cards: `jira_id`, `job_id`, status, live, `agent_mode`, `model`, started_at, error preview.
- [ ] Filters: All / In flight / Queue / Error / Completed. Search by `jira_id`. Page size 25.
- [ ] No Delete, no bulk-select, no Stop, no queue Cancel, no Report.
- [ ] Job detail tabs: Details, Prompt, Transcript, Logs.
- [ ] Details shows the §17.3 meta fields, last assistant text, and the attempts table.
- [ ] Prompt tab lists every user message we POSTed (`ORIGINAL` + orchestrator ids) with exact text.
- [ ] Transcript copies VD `JobChatTab` (user / assistant / tool / compact). OpenCode only.
- [ ] Live transcript reads this job’s serve; finished jobs read the snapshot. Clone may be gone.
- [ ] Logs tab: `{job_log_dir}/{jira_id}.log` filtered by this `job_id`.
- [ ] History store under `job_store_dir`, one JSON per `job_id`. Create dir on startup.
- [ ] Write the history row on accept; update through the run; keep it after terminal and after clone delete.
- [ ] Persist attempt rows (kind, prompt id, error, session_id, time) on each outer retry.
- [ ] Persist a chat snapshot during the poll loop and at job end.
- [ ] History JSON and every `/api` body omit the PAT and any URL userinfo.
- [ ] `GET /api/meta`, `/api/jobs`, `/api/jobs/{id}`, `/api/jobs/{id}/prompts`, `/api/jobs/{id}/chat`, `/api/jobs/{id}/logs`, `/api/queue`.
- [ ] `WS /ws` pushes running/queue counts (no poll/settings payload).
- [ ] POST / PATCH / DELETE under `/api` → **405**.
- [ ] Boot leftover running/queued → history ERROR, no callback; not `409`.
- [ ] Shutdown ERROR rows stay in history after clone delete.
- [ ] New POST for the same `jira_id` after terminal → new `job_id` and a new list row.
- [ ] Tests: dashboard writes 405; PAT absent from history JSON and `/api` bodies; list/detail/chat/logs 200; history survives restart; boot leftover is ERROR in history and the next `POST /jobs` is not 409.
