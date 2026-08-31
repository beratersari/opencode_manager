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

Manager must already be up on `:4096`. `PAT` is optional on the form
(public HTTPS repos). Leave it blank when the remote does not need auth.

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

Replies are also appended to `replies.jsonl` in this folder.
