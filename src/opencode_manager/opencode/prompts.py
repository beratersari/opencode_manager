"""Exact orchestrator prompt strings (PLAN §5.3)."""

UNATTENDED_NUDGE = """You are running unattended — there is no human in the loop and no one
will answer questions. Do not ask clarifying questions, confirmation,
or multiple-choice options. Choose the safest defaults consistent with
the repository and the original task. Finish all remaining work.
Do not restart from scratch."""

COMPACT_LOOP_NUDGE = """Auto-compact looped and was aborted. Stay in this session.
Do not start another compaction cycle. Finish remaining work from
the current files and conversation. Do not restart from scratch.
Do not ask clarifying questions."""

HANG_RESUME = """The last turn stopped early (timeout, hang, or the OpenCode server was
restarted). Stay in this session. Do not start another compaction cycle.
Do not restart from scratch. Finish remaining work from the current
files and conversation. Do not ask clarifying questions."""

INCOMPLETE_RESUME = """Finish remaining todos and complete the original task in this session.
Do not restart from scratch. Do not ask clarifying questions."""
