# OpenCode Session Manager

Windows/Linux worker between **n8n** and **OpenCode**. It clones a repo with the request PAT, runs a per-job `opencode serve`, and POSTs results to the request `callback_url`. A read-only jobs dashboard (same stack as virtual_developer `web/`) shows history, logs, prompts, and chat.

Design is in [PLAN.md](PLAN.md). Binding implementation rules are in [AGENTS.md](AGENTS.md).
