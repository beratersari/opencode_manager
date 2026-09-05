# OpenCode Session Manager

Worker between your orchestrator (n8n or any HTTP client) and **OpenCode**.
You POST a job. OSM clones the branch, runs a per-job `opencode serve`, and
POSTs **one** terminal result to the `callback_url` you sent. The product is
**text** (the last assistant message). The dashboard at `/jobs` is read-only.

Design: [PLAN.md](PLAN.md). Rules: [AGENTS.md](AGENTS.md). Türkçe mimari / dosya rehberi: [README.tr.md](README.tr.md).

## How another system should use this

1. Expose an HTTPS (or HTTP) URL that accepts **one** JSON POST. That is your
   wait / webhook. Put it in `callback_url`. OSM does not guess it from
   `Host` / `Origin`.
2. `POST /jobs` and return immediately on the ack. Do **not** hold the
   socket for clone or OpenCode.
3. Treat inbound **202** as accepted (started or queued). Either wait
   for the callback **or** omit `callback_url` and poll
   `GET /jobs/{job_id}` until HTTP 200. **400 / 409 / 503 get no
   callback** — do not enter a Wait node. The shipped n8n flows send
   those acks to **returnAckFail** immediately (`is_success: false`).
4. Use a stable `jira_id` per ticket. A second POST for the same id while
   that job is running or queued is **409** and gets **no** callback.
5. After the job is terminal (callback arrived, or you know it failed), the
   same `jira_id` may be posted again as a **new** job.

Two ready n8n sub-workflows (same OSM, pick one):

| File | How it waits |
|---|---|
| [n8n-callback.json](n8n-callback.json) | Sends `callback_url` (`$execution.resumeUrl`). **waitForOsmCallback** is a Wait webhook. |
| [n8n-poller.json](n8n-poller.json) | Omits `callback_url`. See the poller status table below. |

Both keep **strginfyInputText1**, **isTextExist1**, **Basic LLM Chain1**,
**OpenAI Chat Model1**, and **returnSuccess1** / **returnFail1**. Import
and set `remoteIP` / `remotePort` (4096) on **remoteComputerInfo1**.
That node also has `timeout`, `retry_count`, and (poller only)
`poll_interval` / `poll_max_seconds`. **buildOsmRequest** hardcodes
`model`.

**n8n-callback** has **no hardcoded webhook URL**. n8n creates a new
Wait resume URL each run. **buildOsmRequest** copies it into the OSM
body:

```javascript
callback_url: $execution.resumeUrl,

**n8n-poller status handling** (re-import the JSON after changing this):

| OSM reply | What the poller does |
|---|---|
| POST **202** + `job_*` | Sleep `poll_interval`, then `GET /jobs/{id}` |
| POST **400** / **409** / **503** | **returnAckFail** immediately (no poll) |
| GET **202** / `live: true` | Keep polling |
| GET HTTP **5xx** with no terminal body | `retry_blip` — poll again until `poll_max_seconds` |
| GET envelope **200** | Success path (`isTextExist1` → LLM) |
| GET envelope **404** / **500** / **504** | **returnAckFail** (missing branch / error / timeout). Does **not** keep polling. |
| GET **404** unknown id | **returnAckFail** |
| DELETE /sessions **200** | `is_success: true` |
| DELETE **400** / **409** / **500** / **503** | `is_success: false`, keep inbound `session_id` |
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
   **waitForOsmCallback** (callback flow) or the GET `/jobs/{id}` loop
   (poller). **400** / **409** / **503** go to
   **returnAckFail** with OSM `text` (no callback).
5. OSM later POSTs the one terminal JSON to the Wait URL.
6. **normalizeCallback** unwraps the Wait `{ body }` wrapper.
7. **isCallback200**: `status_code === 200` goes to **isTextExist1**
   (then the LLM). **404** / **500** / **504** and Wait timeout (still
   the **202** ack) go to **returnAckFail** with OSM `text`.

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
| **400** | `missing required field: {name}` | Missing/empty `repo_url`, `prompt`, `model`, `agent_mode`, `timeout_in_seconds`, `retry_count`, or `jira_id` | No |
| **400** | `SSH repo_url is rejected` | `git@` or `ssh://` | No |
| **400** | `repo_url must be http(s) or file` | Other scheme | No |
| **400** | `model must be provider/id` | Not `provider/id` | No |
| **400** | `unknown agent_mode: {agent}` | Not `planner` or `orchestrator` | No |
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
| **500** | `git failed: {error}` | Clone / `ls-remote` failed (userinfo redacted) |
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
| `repo_url` | yes | HTTPS (or `file://`). Cloned as given. No `git@` / `ssh://`. |
| `source_branch` | no | If sent, must already exist on the remote. Omit / `""` / `-1` skips `ls-remote` and clones the default HEAD. OSM does not check it out; the OpenCode agent does. |
| `prompt` | yes | Sent once, as the first user message. |
| `model` | yes | `provider/id`, e.g. `opencode/mimo-v2.5-free`. |
| `agent_mode` | yes | `planner` or `orchestrator`. n8n maps `working_mode` before POST. |
| `timeout_in_seconds` | yes | One OpenCode attempt (not clone / cleanup). |
| `retry_count` | yes | Max attempts, first included. Minimum `1`. |
| `jira_id` | yes | Dedup key. Folder name for the clone. |
| `callback_url` | no | Absolute `http(s)` URL for the one terminal POST. Omit or `""` to poll `GET /jobs/{job_id}` instead. Default settings accept any host (`callback_allowed_hosts: ["*"]`). |
| `session_id` | no | Resume this `ses_*` if it is still valid. |

## curl

Start a job:

```bash
curl -sS -X POST http://127.0.0.1:4096/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "repo_url": "https://gitlab.example.com/group/repo.git",
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

Poll instead of a callback (omit `callback_url`):

```bash
# 202 + job_id
JOB=$(curl -sS -X POST http://127.0.0.1:4096/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "repo_url": "https://gitlab.example.com/group/repo.git",
    "source_branch": "main",
    "prompt": "Add tests for the login handler.",
    "model": "opencode/mimo-v2.5-free",
    "agent_mode": "build",
    "timeout_in_seconds": 1800,
    "retry_count": 2,
    "jira_id": "PROJ-123"
  }' | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")

# loop until HTTP 200 (envelope status_code is 200/404/500/504)
curl -sS -w '\nHTTP %{http_code}\n' http://127.0.0.1:4096/jobs/$JOB
```

Optional resume:

```bash
curl -sS -X POST http://127.0.0.1:4096/jobs \
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
curl -sS 'http://127.0.0.1:4096/api/jobs?filter=active'
curl -sS 'http://127.0.0.1:4096/api/jobs?jira_id=PROJ-123'
curl -sS 'http://127.0.0.1:4096/api/jobs/job_xxxxxxxx'
curl -sS 'http://127.0.0.1:4096/api/jobs/job_xxxxxxxx/chat'
curl -sS 'http://127.0.0.1:4096/api/jobs/job_xxxxxxxx/logs'
curl -sS 'http://127.0.0.1:4096/api/report-context'
```

## Run

Needs `git` and the **offline zip** (or a source tree after
`packaging/build_dist.py --in-place`). Python is **in the zip** — the
installer does not use a system interpreter. Set **one** `data_dir`
(clones in `.temp/`, logs in `logs/`, history in `jobs/`). Default
listen is `0.0.0.0:4096` in `settings.yaml`.

### Offline zip (no network on the target)

CI workflow **Offline Distribution** builds **four** payloads. A GitHub
Actions artifact download is one zip of the folder (`install.bat` /
`vendor/` at the top). Tag **Releases** attach the four `.zip` files
directly. Download the one for your OS (or the combined Windows+Linux zip):

- `opencode-manager-<version>-windows-x64.zip`
- `opencode-manager-<version>-linux-x64.zip`
- `opencode-manager-<version>-darwin.zip` (Apple Silicon + Intel)
- `opencode-manager-<version>-windows-linux.zip` (Windows + Linux together)

Each zip has the matching bundled CPython, wheels, OpenCode CLI,
`web/dist`, and `settings.local.yaml` (Windows `C:\osm`, Linux
`/var/lib/osm`). The combined zip has both templates; install copies
the one for your OS. `agents/` is not shipped. Extract it. Install
Git. Then:

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
and check the prebuilt SPA. They never hit PyPI or npm. On Linux, if
`/var/lib/osm` is not writable, `install.sh` writes `settings.local.yaml`
to `$XDG_DATA_HOME/osm` or `~/.local/share/osm` so `./start.sh` works
without root. OpenCode is a separate
installer: `install-opencode.*` deletes `<user>/.opencode` (Windows:
`%USERPROFILE%\.opencode`) and copies `vendor/bin` from scratch.
`start-frontend` is a Python SPA proxy on `:5173` (not Vite). The manager
also serves the same SPA at http://127.0.0.1:4096/jobs.

### Single-file exe (Windows + Linux)

A separate CI artifact is the executable plus `settings.local.yaml`
(no `install.bat`). Keep both in the same folder. It starts both the
manager (`:4096`, API + SPA) and the `:5173` proxy. `start.bat` /
`start.sh` are unchanged.

- `opencode-manager-<version>-windows-x64.exe` + `settings.local.yaml` (`C:\osm`)
- `opencode-manager-<version>-linux-x64` + `settings.local.yaml` (`/var/lib/osm`)

Git and OpenCode stay on PATH (the exe prepends `~/.opencode/bin`).
Windows always uses `C:\osm` unless you change the overlay. On Linux,
if `/var/lib/osm` is not writable, the exe falls back to
`~/.local/share/osm`.

Build on the target OS (no cross-compile), after `web/dist` exists:

```bash
pip install -e ".[exe]"
python packaging/build_exe.py
```

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
curl -sS http://127.0.0.1:4096/api/meta
```

Dashboard: http://127.0.0.1:4096/jobs

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
