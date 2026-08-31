# OpenCode Session Manager

Worker between your orchestrator (n8n or any HTTP client) and **OpenCode**.
You POST a job. OSM clones the branch, runs a per-job `opencode serve`, and
POSTs **one** terminal result to the `callback_url` you sent. The product is
**text** (the last assistant message). The dashboard at `/jobs` is read-only.

Design: [PLAN.md](PLAN.md). Rules: [AGENTS.md](AGENTS.md).

## How another system should use this

1. Expose an HTTPS (or HTTP) URL that accepts **one** JSON POST. That is your
   wait / webhook. Put it in `callback_url`. OSM does not guess it from
   `Host` / `Origin`.
2. `POST /jobs` and return immediately on the ack. Do **not** hold the
   socket for clone or OpenCode.
3. Treat inbound **202** as accepted (started or queued). Wait for the
   callback. Do not poll OSM to decide success.
4. Use a stable `jira_id` per ticket. A second POST for the same id while
   that job is running or queued is **409** and gets **no** callback.
5. After the job is terminal (callback arrived, or you know it failed), the
   same `jira_id` may be posted again as a **new** job.

A ready n8n sub-workflow is [n8nflow.json](n8nflow.json) (from
[n8ninitial.json](n8ninitial.json)). Only the OpenCode HTTP/poll path is
replaced. **strginfyInputText1**, **isTextExist1**, **Basic LLM Chain1**,
**OpenAI Chat Model1**, and **returnSuccess1** / **returnFail1** are
unchanged: OSM `text` still goes into that LLM, then the same return
shape. Import the flow and set `remoteIP` / `remotePort` (8080) on
**remoteComputerInfo1**. **buildOsmRequest** hardcodes `PAT` and `model`.
Do not poll OSM.

There is **no hardcoded webhook URL** in that file. n8n creates a **new
Wait resume URL for each run**. The Code node **buildOsmRequest** copies
it into the OSM body:

```javascript
callback_url: $execution.resumeUrl,
```

`$execution.resumeUrl` belongs to the Wait node **waitForOsmCallback**
(`resume: webhook`). Typical shape:

```text
https://<your-n8n-host>/webhook-waiting/<executionId>/<webhookId>
```

`webhookId` in the Wait node (`65a1bc19-ed88-4b03-9297-2bbddc83919b`) is
only n8n’s internal id, not the public URL.

Flow:

1. n8n starts the run.
2. **buildOsmRequest** sets `callback_url` to `$execution.resumeUrl`.
3. **sendRequestToAI1** `POST /jobs` and takes the ack only (`neverError`
   so **400** / **409** / **503** still return the envelope).
4. **ackIs202**: both **202** texts (`in progress` and `queued`) go to
   **waitForOsmCallback**. **400** / **409** / **503** go to
   **returnAckFail** with OSM `text` (no callback).
5. OSM later POSTs the one terminal JSON to the Wait URL.
6. **waitForOsmCallback** resumes with `{ text, session_id, status_code, jira_id, job_id }`.

After a test run, open **buildOsmRequest** → `osmBody.callback_url` to
see the real address. OSM must be able to reach that host (n8n’s public
URL or tunnel). `localhost` on the n8n machine is not reachable from OSM
unless they are the same host.

Inbound ack and callback share one body:

```json
{
  "text": "…",
  "session_id": "ses_… or empty",
  "status_code": 202,
  "jira_id": "PROJ-123",
  "job_id": "job_…"
}
```

There is never a `queued` or `in_progress` callback. `{…}` below is filled in.
`job_id` is empty on **400** / **503**. **409** returns the live job’s id.

### Ack (`POST /jobs` response)

| HTTP | `text` | When | Callback? |
|---|---|---|---|
| **202** | `Job accepted and is now in progress.` | Slot free; worker started | Yes — one terminal POST later |
| **202** | `Job accepted and queued.` | Capacity full; other ticket | Yes — one terminal POST later |
| **409** | `jira_id {jira_id} already has a live job` | Same ticket running or queued | No |
| **400** | `missing required field: {name}` | Missing/empty `repo_url`, `source_branch`, `prompt`, `model`, `agent_mode`, `timeout_in_seconds`, `retry_count`, `jira_id`, or `callback_url` | No |
| **400** | `SSH repo_url is rejected` | `git@` or `ssh://` | No |
| **400** | `repo_url must be http(s) or file` | Other scheme | No |
| **400** | `model must be provider/id` | Not `provider/id` | No |
| **400** | `unknown agent_mode: {agent}` | Not `build` / `plan` / `general` / `explore` | No |
| **400** | `callback_url must be an absolute http(s) URL` | Missing scheme or host | No |
| **400** | `callback_url host is not allowed` | Host not in `callback_allowed_hosts` (ignored when that list is `[]` / `*` / `all`) | No |
| **400** | `timeout_in_seconds and retry_count must be integers` | Non-integer | No |
| **400** | `timeout_in_seconds must be >= 1` | Zero or negative | No |
| **503** | `manager is not accepting jobs` | Booting or shutting down | No |

### Callback (one POST to `callback_url`)

Only after inbound **202**. `status_code` on this POST is never 202.

| `status_code` | `text` | When |
|---|---|---|
| **200** | Last assistant message | OpenCode finished |
| **404** | `source_branch '{branch}' does not exist on the remote` | Branch missing on the remote |
| **500** | `manager shutting down` | Shutdown, or the job was stopped mid-flight |
| **500** | `could not remove leftover clone at {path}` | Stable clone path could not be deleted before clone |
| **500** | `git failed: {error}` | Clone / `ls-remote` failed (never includes a PAT) |
| **500** | `model '{provider/id}' is not available on this OpenCode serve. Available: {sample}.` | Model missing from `GET /config/providers` |
| **500** | `model still asking after UNATTENDED_NUDGE` | Still asking after the one nudge |
| **500** | `compact leftover after COMPACT_LOOP_NUDGE` | Compact leftover after the compact nudge |
| **500** | `attempt {n} ended: hang` | Hang watchdog; no attempts left |
| **500** | `attempt {n} ended: serve-dead` | Serve died; no attempts left |
| **500** | `attempt {n} ended: incomplete` | Incomplete resume exhausted |
| **500** | `serve boot failed: {error}` | Last attempt could not start `opencode serve` |
| **500** | `serve health failed` | Last attempt’s serve never became healthy |
| **500** | `session create failed: {error}` | No live `ses_*` yet; create failed on last attempt |
| **500** | `resume rejected: {error}` | Mid-job resume failed (live id already bound) |
| **500** | `resume rejected; will not open a blank session` | Mid-job hang retry would have created a new session |
| **500** | `session busy; refusing to POST a user message` | Last attempt refused to POST while busy |
| **500** | `user message POST failed: {error}` | Last attempt’s `/message` failed |
| **500** | `worker crashed: {error}` | Uncaught worker exception |
| **500** | `pipeline failed: {error}` | Uncaught pipeline exception |
| **504** | `attempt {n} ended: timeout` | Attempt clock hit zero; no attempts left |

Boot leftovers (`process restarted; leftover job was not resumed`) are history-only **ERROR** rows. They are **not** POSTed.

### `POST /jobs` body

| Field | Required | Notes |
|---|---|---|
| `repo_url` | yes | HTTPS (or `file://`). No `git@` / `ssh://`. |
| `PAT` | no | Only for a private remote. Omit or `""` for public HTTPS. |
| `source_branch` | yes | Must already exist on the remote. |
| `prompt` | yes | Sent once, as the first user message. |
| `model` | yes | `provider/id`, e.g. `opencode/mimo-v2.5-free`. |
| `agent_mode` | yes | `build`, `plan`, `general`, or `explore`. |
| `timeout_in_seconds` | yes | One OpenCode attempt (not clone / cleanup). |
| `retry_count` | yes | Max attempts, first included. Minimum `1`. |
| `jira_id` | yes | Dedup key. Folder name for the clone. |
| `callback_url` | yes | Absolute `http(s)` URL for the one terminal POST. Default settings accept any host (`callback_allowed_hosts: ["*"]`). |
| `session_id` | no | Resume this `ses_*` if it is still valid. |

## curl

Start a job (private GitLab / Azure DevOps: send `PAT`. Public: omit it or use `""`):

```bash
curl -sS -X POST http://127.0.0.1:8080/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "repo_url": "https://gitlab.example.com/group/repo.git",
    "PAT": "",
    "source_branch": "main",
    "prompt": "Add tests for the login handler. Do not ask questions.",
    "model": "opencode/mimo-v2.5-free",
    "agent_mode": "build",
    "timeout_in_seconds": 1800,
    "retry_count": 2,
    "jira_id": "PROJ-123",
    "callback_url": "https://n8n.example.com/webhook-waiting/abc123"
  }'
```

Optional resume:

```bash
curl -sS -X POST http://127.0.0.1:8080/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "repo_url": "https://github.com/example/repo.git",
    "source_branch": "develop",
    "session_id": "ses_xxxxxxxx",
    "prompt": "Continue the previous work.",
    "model": "opencode/mimo-v2.5-free",
    "agent_mode": "build",
    "timeout_in_seconds": 1800,
    "retry_count": 2,
    "jira_id": "PROJ-123",
    "callback_url": "https://n8n.example.com/webhook-waiting/abc123"
  }'
```

Watch only (GET). The dashboard never starts or stops work:

```bash
curl -sS 'http://127.0.0.1:8080/api/jobs?filter=active'
curl -sS 'http://127.0.0.1:8080/api/jobs?jira_id=PROJ-123'
curl -sS 'http://127.0.0.1:8080/api/jobs/job_xxxxxxxx'
curl -sS 'http://127.0.0.1:8080/api/jobs/job_xxxxxxxx/chat'
curl -sS 'http://127.0.0.1:8080/api/jobs/job_xxxxxxxx/logs'
```

## Run

Needs `git` and the **offline zip** (or a source tree after
`packaging/build_dist.py --in-place`). Python is **in the zip** — the
installer does not use a system interpreter. Set **one** `data_dir`
(clones in `.temp/`, logs in `logs/`, history in `jobs/`). Default
listen is `0.0.0.0:8080` in `settings.yaml`.

### Offline zip (no network on the target)

CI workflow **Offline Distribution** builds **four** payloads. A GitHub
Actions artifact download is one zip of the folder (`install.bat` /
`vendor/` at the top). Tag **Releases** attach the four `.zip` files
directly. Download the one for your OS (or the combined Windows+Linux zip):

- `opencode-manager-<version>-windows-x64.zip`
- `opencode-manager-<version>-linux-x64.zip`
- `opencode-manager-<version>-darwin.zip` (Apple Silicon + Intel)
- `opencode-manager-<version>-windows-linux.zip` (Windows + Linux together)

Each zip has the matching bundled CPython, wheels, OpenCode CLI, and
`web/dist`. `agents/` is not shipped. Extract it. Install Git. Then:

```bash
# Windows
install.bat
install-opencode.bat
start.bat

# Linux
./install.sh
./install-opencode.sh
./start.sh
```

`install.bat` / `install.sh` install the manager only: they create
`.venv` with the bundled `python.exe` / `python3`, then install wheels
and check the prebuilt SPA. They never hit PyPI or npm. OpenCode is a separate
installer: `install-opencode.*` deletes `<user>/.opencode` (Windows:
`%USERPROFILE%\.opencode`) and copies `vendor/bin` from scratch.
`start-frontend` is a Python SPA proxy on `:5173` (not Vite). The manager
also serves the same SPA at http://127.0.0.1:8080/jobs.

### Build the vendor payload (needs network, once)

On a machine that can reach GitHub / PyPI / npm:

```bash
python3 packaging/build_dist.py --in-place   # fills vendor/ + web/dist in this repo
python3 packaging/build_dist.py              # writes dist/opencode-manager-*.zip
```

### From source (developers)

```bash
python3.12 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"
cd web && npm install && npm run build && cd ..
opencode-manager
```

Check it is up:

```bash
curl -sS http://127.0.0.1:8080/api/meta
```

Dashboard: http://127.0.0.1:8080/jobs

Local fake n8n Wait node (manager must already be running):

```bash
python3 tester/tester.py
# form: http://127.0.0.1:8090
# callback OSM will POST to: http://127.0.0.1:8090/callback
```

```bash
python3.12 -m pytest tests -m "not live"
python3.12 -m pytest tests -m live
```
