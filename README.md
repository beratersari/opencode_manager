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

| Inbound | Meaning | Callback? |
|---|---|---|
| **202** | Accepted (started or queued) | Yes — one terminal POST later |
| **409** | That `jira_id` is already live | No |
| **400** | Bad body (missing field, SSH URL, bad model, …) | No |
| **503** | Manager booting or shutting down | No |

| Callback `status_code` | Meaning | `text` |
|---|---|---|
| **200** | Finished | Last assistant message |
| **404** | `source_branch` missing on the remote | Error |
| **500** | Failed after retries | Error (never a PAT) |
| **504** | Attempt clock ran out | Error |

There is never a `queued` or `in_progress` callback.

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
| `callback_url` | yes | Absolute `http(s)` URL for the one terminal POST. |
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

Needs Python 3.11+, `git`, and the **offline zip** (or a source tree after
`packaging/build_dist.py --in-place`). Set **one** `data_dir` (clones in
`.temp/`, logs in `logs/`, history in `jobs/`). Default listen is
`0.0.0.0:8080` in `settings.yaml`.

### Offline zip (no network on the target)

CI workflow **Offline Distribution** builds **one** zip (Windows + Linux) with:

- `vendor/python-wheels` — pip packages for the manager
- `vendor/bin/opencode.exe` and `vendor/bin/opencode` — OpenCode CLI (Windows + Linux)
- `web/dist` — prebuilt GET-only dashboard (no Node on the target)

Extract it. Install a supported Python (see `vendor/SUPPORTED_PYTHON.txt`)
and Git. Then:

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

`install.bat` / `install.sh` install the manager only (venv + wheels +
prebuilt SPA). They never hit PyPI or npm. OpenCode is a separate
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
