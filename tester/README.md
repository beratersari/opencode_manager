# OSM job tester

A **test-only** stand-in for n8n. It sends `POST /jobs` to OpenCode Session
Manager and listens for the one terminal callback.

It is not part of the manager. Stdlib only — no extra packages.

```
this app  --POST /jobs-->  OSM :4096
          <--ack 202------

OSM later --POST /callback-->  this app :8090
```

## Run

Manager must already be up on `:4096`. Clone uses `repo_url` as given.

From the repo root:

```bash
python3 tester/tester.py
```

Open http://127.0.0.1:8090

Optional:

```bash
python3 tester/tester.py --listen 127.0.0.1 --port 8090 --osm http://127.0.0.1:4096
```

`callback_url` is filled in as `http://<listen>:<port>/callback`. OSM must be
able to reach that address (same machine: `127.0.0.1` is fine).

## Reliability run (200 mixed jobs)

Measures inbound HTTP codes and terminal `status_code` (200 / 404 / 500 / 504).

```bash
# Embedded OSM, fake runner, real POST /jobs (fast)
python tester/reliability.py --count 200

# Running manager + real OpenCode (slow; unique jira_id per job)
python tester/reliability.py --count 200 --live --osm http://127.0.0.1:4096 --repo-url https://github.com/you/repo.git
```

Replies are also appended to `replies.jsonl` in this folder.
