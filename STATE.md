# Loop State — ApplyWizz DicePilot

Last run: 2026-08-21 (Phase 1 security fix applied and verified — COMPLETE)

## Current Phase

Phase 1 — COMPLETE

## Next Phase

Phase 2 — Pending Human Approval

## Last Verified Result

- Project ref: `pkuqcnvtweukgurisczw`
- Migrations applied (in order):
  1. `20260820175616_dicepilot_foundation.sql` — schema, RLS, atomic claim function
  2. `20260820183454_restrict_claim_rpc_to_service_role.sql` — closed the RPC-permission finding (explicit per-role `REVOKE EXECUTE ... FROM public, anon, authenticated` + `GRANT ... TO service_role`, since the original `REVOKE ... FROM PUBLIC` alone didn't remove the per-role grants this project's default privileges had already handed to `anon`/`authenticated` at function-creation time)
- Live schema structure (tables, UNIQUE/CHECK/FK/ON DELETE, indexes, triggers, RLS-enabled, table-level anon/authenticated access): all PASS.
- Live RPC privileges on `claim_next_queued_application(uuid, text)`, verified via fresh `supabase db dump` (not just "ran without error"): `PUBLIC` — no EXECUTE; `anon` — no EXECUTE; `authenticated` — no EXECUTE; `service_role` — EXECUTE granted; `SECURITY DEFINER` — yes; `search_path` — pinned to `public`. All PASS.
- Test suite against live schema: **25 passed, 0 failed, 0 skipped** (14 unit + 11 integration; all 12 required queue-behavior cases individually asserted, including a real concurrent-threads claim race). Same count as before the fix — expected, since the worker/tests use `service_role`, which retained EXECUTE throughout; only `anon`/`authenticated` access changed.
- Test rows cleaned up after every run; all 5 tables confirmed empty.

## V1 Scope

- one internal candidate
- one Dice browser profile
- up to 20 jobs
- US Dice only
- Contract / Third Party
- true C2C classification (not inferred from Contract/Third Party alone)
- Easy Apply only
- sequential applications (one active application per candidate at a time)

## Source of Truth

- 01 — PRD (`01_ApplyWizz_DicePilot_PRD.pdf`)
- 02 — TRD (`02_ApplyWizz_DicePilot_TRD.pdf`)
- 03 — App Flow (`03_ApplyWizz_DicePilot_App_Flow.pdf`)
- 04 — UI/UX Brief (`04_ApplyWizz_DicePilot_UI_UX_Brief.pdf`)
- 05 — Backend Schema (`05_ApplyWizz_DicePilot_Backend_Schema.pdf`)
- 06 — Implementation Plan (`06_ApplyWizz_DicePilot_Implementation_Plan.pdf`)

These six documents are the product spec. Reconcile against the actual repo before assuming a technical statement in them is still accurate — code and schema are ground truth for what exists today; the docs are ground truth for what V1 is supposed to become.

## Infrastructure

- DicePilot Supabase project: `pkuqcnvtweukgurisczw` (separate from the Indeed Supabase project)
- Migrations: `20260820175616_dicepilot_foundation.sql`, `20260820183454_restrict_claim_rpc_to_service_role.sql` — both **applied to the live project**, both verified
- Repository layer: `db/supabase_client.py`, `db/application_repository.py` — implemented, 25/25 tests passing (unit + live integration)
- Dedicated Dice worker: not started
- Existing Indeed system (`main.py`, `app.py`, `scraper_engine.py`, `templates/index.html`, `scrape_*.py`): untouched, must remain untouched
- Playwright: not yet installed
- Candidate-details API integration: not yet built (client doesn't exist anywhere in this repo yet)

## Current Approved Task

None — waiting for Phase 2 approval.

## Do Not Build Yet

- Playwright application engine
- resume tailoring
- Telegram
- recruiter email
- multi-candidate execution
- billing
- external ATS automation

## Blocking Questions / High Priority

None open. (Resolved 2026-08-21: the `claim_next_queued_application()` RPC-permission gap — `anon`/`authenticated` retaining EXECUTE despite `REVOKE ... FROM PUBLIC` — was closed by `20260820183454_restrict_claim_rpc_to_service_role.sql`, which revokes from each role by name. Verified live via fresh `supabase db dump`: no `GRANT` line remains for `anon` or `authenticated` on this function.)

## Watch List

- Whether `NEEDS_INPUT` (APPLICATION_LEVEL) should still allow the worker to advance to the next queued job — resolved for the schema (it does, per V1 Decision 2/4), but not yet exercised against real Dice question flows.
- `FAILED_RETRYABLE → QUEUED` is manual-only (`requeue_failed_application`) — no operator UI trigger exists yet; confirm whether Phase 2+ needs one before it becomes a real gap.

## Recent Noise (ignored this run)

- Stray browser tab hitting a Google CAPTCHA page — unrelated to DicePilot, closed, not a project signal.

---
Run log: see `loop-run-log.md`
