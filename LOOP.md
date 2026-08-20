# Loop Configuration — ApplyWizz DicePilot

L1/L2 report-and-approve process. No autonomous (L3) behavior, no scheduling, no auto-fix, no auto-merge — see gate.yaml.

## Required session sequence

Every Claude Code session working on DicePilot must, in order:

1. Read `STATE.md` first.
2. Read the relevant DicePilot source-of-truth document(s) listed in `STATE.md` (01 PRD … 06 Implementation Plan) for the task at hand — don't assume a remembered detail is still correct.
3. Inspect `git status` / `git diff` before editing anything.
4. Work only on the task listed under "Current Approved Task" in `STATE.md`. If that's `None`, stop and ask — don't infer the next phase.
5. Preserve existing Indeed behavior — `main.py`, `app.py`, `scraper_engine.py`, `templates/index.html`, `scrape_*.py` are out of scope unless a shared-config change is explicitly required, and even then it must be explained before being made.
6. Never hardcode secrets. Supabase credentials are `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY`, read from environment only (see `.env.example`).
7. Never bypass CAPTCHA, OTP, device verification, or any other security challenge. Pause (`NEEDS_INPUT`, `SESSION_LEVEL`) instead — see `gate.yaml` and the TRD's security constraints.
8. Run the relevant tests before claiming a task complete. "I wrote it" is not "it passes."
9. Update `STATE.md` with verified results and any blockers before ending the session.
10. Never advance to the next phase automatically, even if it looks trivial from here.
11. Stop and wait for explicit human approval after each phase — this file does not authorize self-continuation.

## Human Gates

- No code is pushed to the live Supabase project without the schema/migration being reviewed and explicitly approved first (see the Phase 1 review exchange for the expected format: full SQL + a point-by-point walkthrough).
- No auto-fix, no auto-merge — see `gate.yaml`'s `autoMergeAllowlist` (docs/tests only) and `maxFiles` cap.
- Every phase ends with an explicit STOP for review; the next phase's scope is only ever what the human just approved, not what seems like a logical next step.
- Sensitive/legal/immigration/demographic/attestation answers are never guessed by any Dice worker code, present or future — unknown or ambiguous questions become `NEEDS_INPUT`, full stop.
- An application is never marked `SUBMITTED` on the strength of a click — only after a positively verified Dice success state.

## Worktree Isolation

- Product-code changes (once Dice scraping/Playwright/worker code begins) should be made in an isolated git worktree, not directly against whatever branch this session started on — keeps DicePilot work reviewable as a coherent diff and keeps a bad experiment from touching the working tree the Indeed system depends on.
- Repository-layer and schema work so far (Phase 1) was done directly in the working tree since it's additive-only (new files, one two-line addition to `requirements.txt`) and nothing destructive was at risk; worktree isolation becomes load-bearing once Phase 2+ starts writing/modifying application logic that could conflict with concurrent Indeed work.

## Connectors (MCP)

- Not used for DicePilot today. If a GitHub/Supabase MCP connector is introduced later, scope it to read + comment/query only until L2 trust is established — no write/execute connectors without a separate explicit decision.

## Budget

- See `loop-budget.md` for caps and the kill switch. No scheduled/unattended runs exist for this project — every session is human-initiated, so "budget" here means per-session discipline (don't silently balloon scope), not a cron cap.
- Sub-agent spawns: only for research/exploration within an already-approved task, never to self-extend scope into an unapproved phase.

## Links

- Source of truth: the six DicePilot PDFs listed in `STATE.md`.
- Schema: `supabase/migrations/20260820175616_dicepilot_foundation.sql`
- Repository layer: `db/supabase_client.py`, `db/application_repository.py`
- Escalation / human review: any `NEEDS_INPUT`, `SESSION_LEVEL` intervention, or ambiguous instruction stops for human input rather than guessing — this is the project's stop-and-ask policy, not just a Dice runtime behavior.
