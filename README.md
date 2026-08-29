# OpenCode Session Manager

Windows/Linux worker between **n8n** and **OpenCode**. It clones a repo with the request PAT, runs a per-job `opencode serve`, and POSTs results to the request `callback_url`. A read-only jobs dashboard (same stack as virtual_developer `web/`) shows history, logs, prompts, and chat.

Design is in [PLAN.md](PLAN.md). Binding implementation rules are in [AGENTS.md](AGENTS.md).

```bash
python3.12 -m pip install -e ".[dev]"
cd web && npm install && npm run build
opencode-manager
```

Unit tests: `python3.12 -m pytest tests -m "not live"`.
Live tests (real git + real `opencode`): `python3.12 -m pytest tests -m live`.

A test-only sender/listener (fake n8n Wait node) lives in [`tester/`](tester/):

```bash
python3 tester/tester.py
```

Then open http://127.0.0.1:8090.
