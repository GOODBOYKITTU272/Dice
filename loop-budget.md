# Loop Budget — ApplyWizz DicePilot

No scheduled or unattended runs exist for this project (no automation, no cron, no L3). Every session is human-initiated and human-approved per phase, so there is nothing to "exceed" automatically today. This file exists so the ceiling is decided in advance, before any future scheduling proposal, rather than improvised under pressure.

## Session limits (human-initiated work, current reality)

| Activity | Max per session | Notes |
|----------|-----------------|-------|
| Phases advanced without a STOP-for-review | 1 | Never advance past the phase explicitly approved in STATE.md |
| Files touched without listing them in the end-of-task report | 0 | Every file created/modified must appear in the session's own summary |
| Live Supabase migrations pushed without prior schema review | 0 | `supabase db push` only after the SQL has been reviewed and approved, every time |

## If scheduling or sub-agent automation is proposed later (V2+ consideration, not built)

| Loop | Max runs/day | Max tokens/day | Max sub-agent spawns/run |
|------|--------------|-----------------|--------------------------|
| (none defined — no scheduled loop exists) | — | — | — |

Do not fill this table in as a way to quietly turn on scheduling. A new loop only gets a budget row after a human explicitly decides to schedule it, with its own gate.yaml review.

## On budget exceed (if a scheduled loop is added later)

1. Pause all schedulers immediately.
2. Append an event to `loop-run-log.md`.
3. Escalate in `STATE.md`'s "High Priority" section — do not just log and continue.

## Kill switch

- There is currently nothing running to kill — no scheduler, no autonomous worker.
- Once the Dice worker exists (Phase 8+), its kill switch is: stop the worker process. It holds no state that requires a graceful shutdown sequence beyond whatever's already durable in Supabase (`applications`, `application_events`) — that's the point of Supabase being the source of truth instead of in-memory state.
- Resume only after a human clears the blocking condition and updates STATE.md.
