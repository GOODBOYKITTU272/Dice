# Loop State — ApplyWizz DicePilot

Last run: 2026-08-21 (Phase 2 durability check — fresh verification, committed)

## Repository Architecture — LOCKED

**Two-repo architecture**, decided explicitly and permanently for V1 (supersedes the TRD's original single-repo language — implementation had already progressed materially in the separate repo by the time this was reconsidered, and the safer deployment boundary was judged to matter more than matching the original doc):

- **Indeed-Scraper** (`https://github.com/Tech-Applywizz-git-account/Indeed-Scraper`, local: `/Users/ramakrishnachanda/Desktop/Indeed-Scraper`) — existing production Indeed system. Out of scope for all DicePilot work. Contains stale untracked Phase-1-era DicePilot files (never committed there) — intentionally left alone until this Dice repo is safely committed and verified; cleanup is a separate future task.
- **Dice** (`https://github.com/GOODBOYKITTU272/Dice`, local: `/Users/ramakrishnachanda/Desktop/Dice` — this repo) — standalone ApplyWizz DicePilot service. All DicePilot code, tests, and docs live here from now on.

## Current Phase

Phase 2 — COMPLETE (discovery + qualification pipeline, fresh-verified and committed)

## Next Phase

Audit current Phase 2 pipeline against jobspy-enhanced-scraper 1.3.7 and integrate only the safe discovery portions — **not started, pending separate approval**.

## Last Verified Result

- Project ref: `pkuqcnvtweukgurisczw`
- Migrations applied (in order):
  1. `20260820175616_dicepilot_foundation.sql` — schema, RLS, atomic claim function
  2. `20260820183454_restrict_claim_rpc_to_service_role.sql` — closed the RPC-permission finding (explicit per-role `REVOKE EXECUTE ... FROM public, anon, authenticated` + `GRANT ... TO service_role`, since the original `REVOKE ... FROM PUBLIC` alone didn't remove the per-role grants this project's default privileges had already handed to `anon`/`authenticated` at function-creation time)
- Live schema structure (tables, UNIQUE/CHECK/FK/ON DELETE, indexes, triggers, RLS-enabled, table-level anon/authenticated access): all PASS.
- Live RPC privileges on `claim_next_queued_application(uuid, text)`, verified via fresh `supabase db dump` (not just "ran without error"): `PUBLIC` — no EXECUTE; `anon` — no EXECUTE; `authenticated` — no EXECUTE; `service_role` — EXECUTE granted; `SECURITY DEFINER` — yes; `search_path` — pinned to `public`. All PASS.
- Test suite against live schema: **25 passed, 0 failed, 0 skipped** (14 unit + 11 integration; all 12 required queue-behavior cases individually asserted, including a real concurrent-threads claim race). Same count as before the fix — expected, since the worker/tests use `service_role`, which retained EXECUTE throughout; only `anon`/`authenticated` access changed.
- Test rows cleaned up after every run; all 5 tables confirmed empty.

### Phase 2 (implementation complete, awaiting your visual review — not marked Phase 2 COMPLETE per your instruction)

- Full test suite: **47 passed, 0 failed, 0 skipped** (the original 25 Phase 1 tests unchanged + 22 new Phase 2 tests: C2C classifier, Easy Apply detector, search/detail parsing incl. duplicate-guid dedup).
- I ran the actual local UI myself end-to-end before handing this back (role="Software Engineer", max_results=5) to verify it works, not just that tests pass: 5 real Dice jobs discovered, classified, and saved to `dice_jobs` (visible now — I only removed pytest's own `TEST-*` rows, the 5 real discovery rows are left for you to inspect). Terminal progress logs, UI table, and Supabase rows all matched.
- **Found and fixed a real bug during that verification**: the job-detail-page Easy Apply cross-check was reading the "Easy Apply" badge from *unrelated recommended-job cards* elsewhere on the same page (Dice's detail page renders a "similar jobs" widget), producing false positives — every job showed Easy Apply=true. Removed that check entirely; Easy Apply now comes only from the search-results-card badge, which is correctly scoped (confirmed by direct DOM inspection) and produced a realistic 4/5 mixed result on rerun. Documented in `dice/job_parser.py`.
- Discovery uses plain HTTP (no Playwright): Dice's job-search JSON API returned 403 (needs an internal key, deliberately not pursued), but the public search-results and job-detail pages are server-rendered HTML with a clean `data-testid`-based DOM and a standard schema.org JobPosting JSON-LD block — both legitimate, unauthenticated, publicly-served content.
- No `dice_jobs` schema change was needed — `c2c_reason` doubles as the "C2C Evidence" UI column, `easy_apply_evidence` (jsonb) already existed for exactly this purpose, and "Discovery Status" is a runtime/UI concept (SAVED/ERROR) rather than a stored column.

### Phase 2 — Visual Review Correction Iteration (2026-08-21, still awaiting your second visual review — not marked complete)

You personally ran the first visual test (Role=Software Engineer, max=5) and independently re-verified the Dexian DISYS job (`d103b516-...`) against the live Dice page, a fresh run of the production parser, and the stored Supabase row — all three agreed. That earlier "Easy Apply: False" observation was from an unreliable ad-hoc manual check done *before* any code existed, not from the production parser; **the production Easy Apply logic (search-card-badge-only) is unchanged and preserved**, per your explicit instruction.

Two real issues found in your visual review, both fixed:

1. **"Contract/Third Party" counter root cause**: `local_app/templates/index.html` line 200 (`if (job.is_third_party) thirdParty++;`) — the counter was labeled "Contract/Third Party" but its code only ever counted `is_third_party`. It never checked for Contract at all, hence 0 despite all 5 jobs being Contract. Fixed by splitting into two honest, separate counters (Contract, Third Party) backed by a new `dice/qualification.py::is_contract()` — Contract is never conflated with Third Party in either direction.
2. **Discovered/Stored/Qualified conflation**: "Saved=5" was accurate as a storage fact but could be misread as "approved for application." Added `dice/qualification.py::qualify_job()` — a deterministic, non-LLM function computed at read time from fields Phase 1 already has (`employment_type`, `is_third_party`, `c2c_status`, `is_easy_apply`). **No database migration** — nothing needed one; QUALIFIED is recomputed from existing columns every time, never persisted, so it can't drift out of sync with them.

Self-verified against the 5 real stored rows from the earlier run (not re-run through the UI — that's reserved for the human): computed counters exactly match the expected table — Discovered=5, Contract=5, ThirdParty=0, Confirmed=0, Likely=0, Unknown=4, NotC2C=1, EasyApply=4, Stored=5, **Qualified=0**. Per-row reasons match too, including Randstad Digital correctly showing both "C2C unknown" and "Not Easy Apply".

Full suite: **65 passed, 0 failed, 0 skipped** (47 baseline + 18 new `test_qualification.py` tests). No regressions.

### Phase 2 durability check (2026-08-21) — COMPLETE, committed

Independent re-verification, done specifically because "don't rely on the historical test result":
- Secret scan: clean — `.env` confirmed gitignored, not in `git status`; no secret-shaped strings (Supabase URL/key patterns, tokens, passwords) found in any file staged for commit.
- Code re-review: `dice/discovery.py`, `dice/search.py`, `dice/job_parser.py`, `dice/c2c_classifier.py`, `dice/easy_apply_detector.py`, `dice/qualification.py` re-read line-by-line; confirmed no application-initiation URLs (`/job-applications/`, `/start-apply`) are ever called, no candidate data is referenced, no apply-submission logic exists.
- Fresh test run (not the cached historical number): **65 passed, 0 failed, 0 skipped**, immediately followed by cleanup of only the `TEST-`-prefixed rows the integration tests create — the 5 real discovery rows from your own visual tests were left untouched.
- Import/runtime check: every Phase 2 module (`dice.discovery`, `dice.search`, `dice.job_parser`, `dice.qualification`, `dice.c2c_classifier`, `dice.easy_apply_detector`) imports cleanly; `local_app.app` constructs and registers its 3 routes without error.
- **Minor non-blocking finding**: `dice/easy_apply_detector.py`'s module docstring still describes a "two independent readings, cross-checked" design — stale, since the fix removed the detail-page signal entirely and `discovery.py` only ever calls it with the search-card argument. Functionally correct, comment is outdated. Left as-is per this session's read-only/no-refactor scope; worth a one-line docstring fix next time that file is touched for a real reason.
- jobspy-enhanced-scraper: audit completed (see prior session) — integration **not started**. Playwright: **not started**.

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
- Dice discovery: implemented (`dice/search.py`, `dice/job_parser.py`, `dice/c2c_classifier.py`, `dice/easy_apply_detector.py`, `dice/discovery.py`, `dice/models.py`, `dice/qualification.py`) — plain HTTP, no browser
- Local operator UI: implemented (`local_app/app.py`, `local_app/templates/index.html`) — new, not a copy of the old Indeed dashboard
- Dedicated Dice application/worker (submits applications): not started — out of scope for Phase 2
- Existing Indeed system (`main.py`, `app.py`, `scraper_engine.py`, `templates/index.html`, `scrape_*.py`): untouched, must remain untouched (and lives in a separate repo entirely — this repo has no Indeed code)
- Playwright: not yet installed
- Candidate-details API integration: not yet built (client doesn't exist anywhere in this repo yet)

## Current Approved Task

None — Phase 2 is complete and committed. Waiting for approval to begin the next phase: auditing the current Phase 2 pipeline against jobspy-enhanced-scraper 1.3.7 and integrating only the safe discovery portions (search/pagination patterns, job detail parsing fallbacks, description/salary/experience extraction, canonical Dice UUID identity). C2C classification, Easy Apply verification, and the qualification gate remain ours, unchanged, per the completed audit's recommendation — not to be replaced by upstream logic.

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
- Easy Apply detection is now single-signal (search-card badge only) after removing the buggy detail-page check. This is honest and correctly scoped, but it means there's no independent cross-check anymore — worth revisiting if a future phase needs stronger confidence before an actual apply attempt (Phase 2 is detection-only, so this wasn't gating anything real yet).
- Dice's job-search JSON API (`job-search-api.svc.dhigroupinc.com`) returned 403 without an internal API key — deliberately not pursued (would mean reverse-engineering an access-control token). HTML discovery works fine and is what's implemented; flagging in case a future phase considers the API route again.
- QUALIFIED is a Phase 2 discovery-time judgment only — it does not enqueue an application, and Phase 2 has no application worker to consume it yet. Worth deciding, before any future phase acts on it, whether "qualified" should also require a fresher re-check (e.g. re-confirm Easy Apply is still true) immediately before an actual apply attempt, since discovery and application could happen at different times.
- Considered and declined DeepSeek Harness for DicePilot (per your explicit decision) — deterministic discovery + Supabase state + eventual Playwright doesn't need a general-purpose agent runtime. No dependency, no code from it exists anywhere in this repo.

## Recent Noise (ignored this run)

- Stray browser tab hitting a Google CAPTCHA page — unrelated to DicePilot, closed, not a project signal.

---
Run log: see `loop-run-log.md`
