# Loop State — ApplyWizz DicePilot

Last run: 2026-08-21 (Phase 4F NEEDS_INPUT pause/resume built on the existing Phase 1 schema -- no migration, no second orchestration store. db/intervention_repository.py adds create_or_get_question_intervention() (idempotent, dedupes on application_id+question_id), resolve_question_intervention() (rejects invalid radio answers, preserves free text verbatim, resolves exactly once), and compute_application_readiness() (derives READY/RUNNING/NEEDS_INPUT/RESUMABLE/FAILED/SUBMITTED -- RESUMABLE is computed, never persisted, since the schema has no such status value). Live-verified end to end (NEEDS_INPUT -> resolved -> RESUMABLE) against the real linked Supabase project with disposable TEST- rows, cleaned up after. Local operator view added at /interventions (read + local-only resolve form, never touches Dice). Auto-answering: NOT BUILT. Submission: NOT BUILT.)

## V1 Delivery Board

| Phase | Status |
|---|---|
| Phase 1 — Database/Foundation | COMPLETE |
| Phase 2 — Discovery/Qualification | COMPLETE |
| Phase 3A — Safe JobSpy Integration | COMPLETE |
| Phase 3B — Qualification Validation | COMPLETE — identified correctness blockers |
| Phase 3C — C2C Correctness | COMPLETE |
| Phase 3D — LIKELY Policy | COMPLETE — LIKELY → HUMAN_REVIEW approved |
| Phase 4A — Playwright Reference Audit | COMPLETE |
| Phase 4B — Persistent Dice Browser | COMPLETE |
| Phase 4B.1 — Authenticated Session Bootstrap | COMPLETE — human login + CDP-attach, session persists while Chrome stays running |
| Phase 4C — Easy Apply Navigation + Resume | COMPLETE — Easy Apply + resume replacement both live-verified end to end |
| Phase 4D — Application Question Engine | EXTRACTION FOUNDATION COMPLETE — NO_QUESTIONS_PRESENT and Review-screen detection live-verified; RADIO/TEXTAREA live-observed + offline-replay verified; select/date/checkbox-as-question/multi-select not yet observed live; auto-answering not built |
| Phase 4E — Candidate Adapter | COMPLETE — normalization built and offline-verified; live fetch BLOCKED (APPLYWIZZ_API_BASE_URL/APPLYWIZZ_API_TOKEN not configured) |
| Phase 4F — NEEDS_INPUT / Pause-Resume | COMPLETE — live-verified end to end against real Supabase; auto-answering/submission not built |
| Phase 5 — Submission Verification | NOT STARTED |
| Phase 6 — Sequential Worker | NOT STARTED |
| Phase 7 — 20-Job End-to-End V1 | NOT STARTED |

## Repository Architecture — LOCKED

**Two-repo architecture**, decided explicitly and permanently for V1 (supersedes the TRD's original single-repo language — implementation had already progressed materially in the separate repo by the time this was reconsidered, and the safer deployment boundary was judged to matter more than matching the original doc):

- **Indeed-Scraper** (`https://github.com/Tech-Applywizz-git-account/Indeed-Scraper`, local: `/Users/ramakrishnachanda/Desktop/Indeed-Scraper`) — existing production Indeed system. Out of scope for all DicePilot work. Contains stale untracked Phase-1-era DicePilot files (never committed there) — intentionally left alone until this Dice repo is safely committed and verified; cleanup is a separate future task.
- **Dice** (`https://github.com/GOODBOYKITTU272/Dice`, local: `/Users/ramakrishnachanda/Desktop/Dice` — this repo) — standalone ApplyWizz DicePilot service. All DicePilot code, tests, and docs live here from now on.

## Current Phase

Phase 4F NEEDS_INPUT / Pause-Resume — **COMPLETE**. `db/intervention_repository.py` gives Phase 4D's `NEEDS_INPUT`/`SENSITIVE_NEEDS_INPUT`/`UNSUPPORTED` questions a safe stop-and-record path, built entirely on the existing Phase 1 schema (`applications`, `application_events`, `interventions`) — no migration, no second orchestration store. Live-verified end to end (`NEEDS_INPUT` → resolved → `RESUMABLE`) against the real linked Supabase project.

## Next Phase

Not yet approved. Phase 5 (submission verification) and the rest of Phase 4D (real answer-resolution wiring — Candidate Adapter → Question Engine → Intervention, currently three separate, unconnected pieces) remain explicitly **not started**. Auto-answering has still never been implemented anywhere in this repo.

## Phase 4F — NEEDS_INPUT / Pause-Resume Foundation (2026-08-21)

**Schema audit** (per explicit instruction to audit before building): the existing Phase 1 schema already covers nearly everything this phase needs. `applications.status` already has a `NEEDS_INPUT` value; `interventions` already has `application_id`, `type`, `intervention_scope`, `question_text`, `options` (jsonb), `status`, `answer` (jsonb), `answered_by`, `created_at`, `resolved_at`; `db/application_repository.py` already had working `create_intervention()`/`resolve_intervention()`/`update_application_status()`/`add_event()` from Phase 1. Two genuine gaps found:
1. No `RESUMABLE` value exists in `applications.status`'s CHECK constraint. Rather than migrate the live schema for one derived state, `compute_application_readiness()` computes `RESUMABLE` at **read time** (`NEEDS_INPUT` status + zero remaining `OPEN` interventions) without ever writing it to the database — the stored `status` stays `NEEDS_INPUT` until a future worker (not built) explicitly transitions it to `PROCESSING` to actually resume.
2. `question_id`/`field_type`/`reason`/`sensitivity` have no dedicated typed columns on `interventions`. Rather than add columns, these are packed into the existing `options` jsonb column as a structured wrapper (`{"question_id", "field_type", "reason", "sensitivity", "choices"}`), and `answer_source` reuses the existing `answered_by` column. `candidate_id`/`dice_job_id` are deliberately **not** duplicated onto interventions — they already live on the parent `applications` row, reachable via the existing `application_id` FK.

Net result: **zero schema migration**, full reuse of Phase 1's repository layer.

**`db/intervention_repository.py`** (new): `create_or_get_question_intervention()` is idempotent by check-before-insert on `(application_id, question_id)` — a worker restart re-encountering the same blocking question recovers the existing row rather than duplicating it. A real bug found via TDD: the underlying `create_intervention()` always tries to transition the application to `NEEDS_INPUT`, which fails (`NEEDS_INPUT` → `NEEDS_INPUT` isn't a modeled transition) when a *second*, different question blocks an application that's already `NEEDS_INPUT` from a first one — fixed by inserting the row directly (skipping the redundant transition) when the application is already `NEEDS_INPUT`. `resolve_question_intervention()` rejects an answer not in the intervention's recorded `RADIO` choices, preserves free text verbatim (no trimming, no rewriting), and raises rather than silently re-resolving an already-`ANSWERED` intervention. Always `APPLICATION_LEVEL` scope — a question blocks only its own application, never the whole candidate/browser session (unchanged Phase 1 semantics for `SESSION_LEVEL`).

**Local operator UI**: `/interventions` (new route) lists open interventions — job/company, question prompt, reason, sensitivity badge, a local-only resolve form (select for `RADIO` choices, free text otherwise). Resolving here only calls `resolve_question_intervention()` — it never opens a Dice page, never fills or clicks anything there. Capped to the 20 most recent open interventions: the linked Supabase project has accumulated **70 `OPEN` interventions from past test sessions that were never cleaned up** (a real, pre-existing gap, not something this phase caused — recorded below, not fixed now), and the page's per-row job/application lookups made an uncapped view impractically slow (35s) against that backlog.

**Live validation**: full `NEEDS_INPUT` → resolved → `RESUMABLE` cycle run against the real linked Supabase project (`test_intervention_repository_integration.py`), using a disposable `TEST-`-prefixed job/application, cleaned up immediately after each run (verified empty). Confirmed the restart-recovery/dedupe behavior against real Postgres, not just the in-memory fake client. No Dice.com interaction anywhere in this phase.

**Tests**: 245 baseline → **273 passed, 0 failed, 0 skipped** (21 in `test_intervention_repository.py`, 3 in `test_phase4f_boundary.py`, 1 live integration test, 3 in `test_local_app.py`). TDD: implementation moved aside first, confirmed `ModuleNotFoundError` (red for the right reason); restored, then one real behavioral bug (the `NEEDS_INPUT`→`NEEDS_INPUT` transition failure above) was caught by the tests themselves and fixed.

Decision gate: **PHASE 4F: COMPLETE.**

## Backlog / Risk Note — Accumulated Stale Test Rows in Live Supabase (2026-08-21)

Not solved this session, recorded only. The linked Supabase project (`pkuqcnvtweukgurisczw`) has **631 `TEST-`-prefixed `dice_jobs` rows and 70 `OPEN` interventions** accumulated from past integration-test runs across earlier phases that were never cleaned up, despite `STATE.md`'s own Phase 1 notes claiming "test rows cleaned up after every run." Only the exact rows this session's own tests created were cleaned up here (verified empty afterward) — the pre-existing backlog was left alone as out of scope for this phase. Worth a dedicated cleanup pass before this project needs the `interventions`/`dice_jobs` tables to reflect only real data (e.g. before Phase 6's sequential worker starts reading from them).

## Phase 4E — Candidate Adapter (2026-08-21)

**Audit** (Part 1): no candidate-details API client exists anywhere in either repo (`Dice` or `Indeed-Scraper`) — confirmed via repo-wide grep, matching the prior `STATE.md` note "client doesn't exist anywhere in this repo yet." The contract is documented, not implemented: `02_ApplyWizz_DicePilot_TRD.pdf` section 7 ("Candidate Adapter Rules") gives the field-mapping table and section 12 names the two required env vars (`APPLYWIZZ_API_BASE_URL`, `APPLYWIZZ_API_TOKEN`); `05_...Backend_Schema.pdf` section 14 gives the response shape: `GET candidate by candidate_id -> client + additional_information payload`. Neither document specifies the exact HTTP path — `dice/candidate_adapter.py` defaults to `{base_url}/candidates/{candidate_id}`, flagged in its own docstring as an inference, not a confirmed contract.

**Two real documentation gaps found, neither guessed shut**: the TRD's mapping table has **no source row for `location` at all** — `CandidateProfile.location` is always `None` until a source field is defined. `contact_email` is documented only as "Defined product policy... not guessed," with no named source field — mapped from `client.email` as the most consistent read of the already-named `client.*` identity fields (`client.id`, `client.full_name`, `client.visa_type`), flagged in the module docstring as an inference pending an explicit product decision.

**`dice/models.py`**: `CandidateProfile` (14 canonical fields, all `Optional` except `candidate_id`) and `CandidateFetchResult` (`status` + `profile` + non-sensitive `error` string) added, following this file's existing plain-`@dataclass`/string-status convention.

**`dice/candidate_adapter.py`** (new): `fetch_candidate(candidate_id)` — explicit outcomes `SUCCESS`/`NOT_FOUND`/`AUTH_ERROR`/`UPSTREAM_ERROR`/`INVALID_RESPONSE`, never a generic `None`. `normalize_candidate_payload(payload)` is pure (no HTTP), independently tested. Every field-level normalizer (`_clean_str`/`_clean_bool`/`_clean_number`) treats missing, null, and malformed source values identically: `None`, never coerced into `False`/`0`/`""`. `requires_sponsorship` follows the TRD's two-tier fallback (`additional_information.require_future_sponsorship`, then `client.sponsorship`) — the only field with a documented fallback. `resolve_candidate_field(candidate, field_name)` defines (not wires in) the future Phase 4D consumption contract, and explicitly refuses to resolve `visa_type`/`work_authorized`/`requires_sponsorship` — those must keep routing through Phase 4D's sensitive/`NEEDS_INPUT` policy. No `willing_to_relocate` → "willing to work onsite" mapping, no invented `desired_salary` field — both explicitly out of scope per instruction.

**Live validation**: **BLOCKED**. `APPLYWIZZ_API_BASE_URL` and `APPLYWIZZ_API_TOKEN` are not set anywhere in this environment (shell, `.env`, or `.env.example` before this session) — confirmed by direct inspection, not assumed. Added both to `.env.example` (names only, no values) so the requirement is documented. No live candidate fetch was attempted with fabricated credentials.

**Tests**: 216 baseline → **245 passed, 0 failed, 0 skipped** (29 new in `test_candidate_adapter.py`). TDD: implementation moved aside, tests confirmed `ModuleNotFoundError` (red for the right reason), implementation restored, full new suite passed on first run.

Decision gate: **PHASE 4E: COMPLETE (offline). Live fetch validation deferred until `APPLYWIZZ_API_BASE_URL`/`APPLYWIZZ_API_TOKEN` are configured.**

## Backlog / Risk Note — Discovery Data Can Drift Before Application Time (2026-08-21)

Not solved this session, recorded only. While searching for a live questions page (Phase 4D live revalidation), a stored CONFIRMED + Easy Apply candidate — **Senior Software Engineer (JavaScript, MongoDB) @ TalentFish LLC** (`173695bb-b7db-427e-b1a9-7b7e8ba0cd20`) — turned out live to be **`AUTH_REQUIRED`** (confirmed after a wait and a page reload, while other tabs in the same authenticated session stayed `ACTIVE`), and its visible content now reads **"Contract W2" / "Apply Now"** rather than the Third-Party/Easy-Apply shape it had when discovered ("Updated 22 hours ago" per the live page). Stored discovery/qualification data can go stale between discovery time and application time. **Not addressed now** — but the future sequential worker (Phase 6) must perform a live pre-application eligibility recheck immediately before any mutating action, not trust stored `is_easy_apply`/`c2c_status` alone.

## Phase 4D-B/C/D — Real Screening-Question Discovery, Extraction, and Closure (2026-08-21)

**Closure fix (Phase 4D-D)**: live revalidation surfaced a real Review-vs-Questions false positive: `is_questions_screen()` matched on the bare substring "Application Questions" anywhere in body text, which also appears on Yashnee's own **Step 3 Review** screen as a completed-step summary line ("Application Questions \* / Completed"). Fixed with TDD (regression fixture reproduces the real Step 3 body text verbatim) so **Review detection now wins over any incidental summary text it contains** — `is_questions_screen()` returns `False` whenever `is_review_screen()` is `True`, full stop, never a page-wide text match alone. Live-reconfirmed against the real, still-open Yashnee tab: `is_review_screen()` → `True`, `is_questions_screen()` → `False`, `extract_questions()` → `NO_QUESTIONS_PRESENT`. Stefanini confirmed untouched throughout.

**Job search**: using the existing discovery/qualification pipeline, checked 4 CONFIRMED Easy Apply jobs before finding one with real questions — QUANTUM TECHNOLOGIES (Full Stack Java Developer) and MSYS Inc. ×2 (SAP R2R Consultant, SAP Concur Consultant) all turned out to be the same no-question 2-step shape as Stefanini. **Java Developer with 8+ experience @ Yashnee Tech Solutions Corporation** (`3f63223a-1dc9-4af9-914c-4ed01e625d44`) has a genuine **3-step** wizard: Resume & Cover Letter → **Application Questions** → Review. Transparency note: the three no-question jobs were opened via Easy Apply during the search and left sitting at their Review screens, untouched, Submit never clicked — a real (if harmless) mutation each, flagged at the time.

**Two real questions captured field-for-field** (read-only DOM inspection, no answers entered): a `role="radiogroup"` ("Are you able and willing to regularly come into the office to work?", Yes/No, radios sharing `name="c59c9cd9-8441-4610-8e13-2621ae1669c2"`, prompt/helper linked via `aria-labelledby`/`aria-describedby`) and a `<textarea>` ("What is your expected rate or salary?", `name="96824b6c-c489-4500-9dcc-d82847b7b1b3"`, prompt via `aria-labelledby`, helper text visually adjacent but **not** ARIA-linked). Neither exposes `required`/`aria-required`/a visible marker anywhere — the planned `required: bool` field was wrong; replaced with a `RequiredState` tri-state (`REQUIRED`/`OPTIONAL`/`UNKNOWN`), both questions `UNKNOWN`. No `<fieldset>`s exist on the page — radio grouping happens via shared `name` only. The React-Aria-generated `id`s (`react-aria...`) are per-render, not durable; the `name` UUIDs are the real stable identifiers.

**`dice_browser/questions.py`**: `is_review_screen()` generalized from a literal "Step 2 of 2" to any "Step X of Y" indicator, since Yashnee's wizard proved step counts vary — confirmed correct later the same session when Yashnee's own Step 3 (also a Review screen) matched it. New `is_questions_screen()` detects the "Application Questions" heading + step indicator. `extract_questions()` groups same-`name` radio inputs into one `RADIO` question, extracts `TEXTAREA` questions directly, and classifies everything else (select, native checkbox, custom combobox, plain text input) `UNSUPPORTED` — never guessed into a supported type. `_question_id()` prefers `name`, falls back to a hash of the resolved prompt, and only as a last resort an explicitly-unstable positional placeholder — never the generated `id`. Both observed questions classify `NEEDS_INPUT` (no trusted candidate field exists for either — explicitly not mapped to `willing_to_relocate`, and salary is explicitly never guessed/defaulted/derived from the job's posted range). Still zero `.click()`/`.fill()`/`set_input_files`/`.select_option()`/`.check()` calls anywhere in the module.

**Live validation, partial**: `is_questions_screen()` confirmed `True` against the real live Yashnee Step 2 page. A fresh full `extract_questions()` re-run against that same live page (expecting `QUESTIONS_PRESENT`, count 2) could not be completed — by the time the check ran, the tab had already advanced to **Step 3 of 3** on its own, outside anything this session did (no `.click()` was ever issued against that tab after the read-only audit). Confirmed via direct inspection: the application was **not submitted** (no confirmation/success text anywhere; Submit button present, visible, not clicked) — Step 3 is itself a Review screen showing "Application Questions \* — Completed." The generalized `is_review_screen()` correctly matched this real "Step 3 of 3" page, and `extract_questions()` correctly returned `NO_QUESTIONS_PRESENT` there (no fillable controls on the review step) — real, if incidental, live confirmation of that generalization. The RADIO/TEXTAREA extraction logic itself is built directly from the exact live DOM data captured earlier the same session (real `aria-labelledby`/`aria-describedby` ids, real `name` UUIDs, real prompt/helper text) and passes 23 offline tests reproducing that exact shape — but a live re-run of `extract_questions()` specifically returning `QUESTIONS_PRESENT`/2 against a *running* Dice page did not occur this session. Documented as a real gap, not glossed over.

**Tests**: 192 baseline → 215 passed (23 new) → **216 passed, 0 failed, 0 skipped** after the closure fix (1 more).

**Final truthful status, per explicit decision to accept live-observed-plus-offline-replay evidence as sufficient rather than keep mutating more applications to reproduce an already-observed page**:
- NO_QUESTIONS_PRESENT: **LIVE VERIFIED**
- Review-screen detection: **LIVE VERIFIED**
- RADIO extraction: **LIVE OBSERVED + OFFLINE REPLAY VERIFIED**
- TEXTAREA extraction: **LIVE OBSERVED + OFFLINE REPLAY VERIFIED**
- NEEDS_INPUT classification: **VERIFIED**
- Fresh post-implementation live question-step replay: **NOT COMPLETED / NON-BLOCKING** (the one observed live questions page moved to Review on its own before a post-fix replay could run)
- SELECT / CHECKBOX-as-question / DATE / MULTI_SELECT: **NOT LIVE VERIFIED** — intentional; unknown control types fail safely (`UNSUPPORTED`) rather than block Phase 4E
- Auto-answering: **NOT BUILT**

Decision gate: **PHASE 4D EXTRACTION FOUNDATION: COMPLETE.**

## Phase 4D-A — Review-Screen / NO_QUESTIONS_PRESENT Detection (2026-08-21)

**Real live finding locked in**: job `469efdf8-e321-46a1-9346-70870d020736` (Data Engineer, Stefanini) has **no custom employer screening questions**. Its Step 2 of 2 is a read-only "Review your application" screen (Resume, Cover Letter, Work Authorization, Current Location, Dice Job Match Score™, Profile Visibility summaries, Back/Submit buttons) with exactly one control on the whole page — an unlabeled, hidden checkbox unrelated to any question. Confirmed directly: neither "Work Authorization" nor "Current Location" has an edit button, link, or input/select/textarea in its container — both are pulled from the candidate's existing Dice profile and shown for confirmation, not asked fresh per application.

**`dice_browser/questions.py`** (new): `is_review_screen(page)` requires all three live-verified signals together ("Step 2 of 2", "Review your application" heading, a Submit button) — no single signal trusted alone. `extract_questions(page)` returns `NO_QUESTIONS_PRESENT` only when the page is a recognized review screen AND zero *visible* candidate controls exist (hidden controls, like the real page's stray checkbox, are filtered out before counting); returns `QUESTIONS_PRESENT` (with minimal, unclassified `QuestionField` entries — no prompt/answer classification logic exists yet, since no real question shape has been observed live) when any visible control is found, even one this module can't yet classify; returns `UNKNOWN_SCREEN` for anything that isn't a recognized review screen, rather than ever defaulting to "no questions" by omission. Zero `.click()`/`set_input_files`/`.fill()`/`.select_option()`/`.check()` calls anywhere in the module — locked in by a boundary test, mirroring Phase 4C's `easy_apply.py`/`resume.py` guards.

**Deliberately not built yet, per explicit instruction**: prompt extraction, field-type classification (TEXT/RADIO/SELECT/etc.), answer resolution, candidate-field mapping, sensitive-field policy. All of that needs a real live question screen to build against — this job doesn't have one.

**Live validation**: `is_review_screen()` → `True`; `extract_questions()` → `NO_QUESTIONS_PRESENT`, 0 questions, against the real live Step 2 page. No click issued during validation; page state unchanged before/after.

**Tests**: 181 baseline → **192 passed, 0 failed, 0 skipped** (11 new in `test_dice_browser_questions.py`; `test_dice_browser_phase4c_boundary.py`'s `test_no_question_answering_module_exists` replaced with `test_questions_module_is_detection_only`, since a question-detection module now legitimately exists).

Decision gate: **PHASE 4D-A PASS. PHASE 4D OVERALL: IN PROGRESS** (blocked on finding a job with real screening questions before the rest of the engine can be built against live evidence).

## Phase 4C.1 — Corrected Resume Replacement (2026-08-21)

**Context**: the first live upload attempt (below, "Phase 4B.1 / 4C — Live Closure") used a naive `input[type='file'].first`, which landed on the Cover Letter field instead of Resume — self-reported immediately, not discovered by the user. Root cause and fix:

1. **DOM-order scoping first fix**: `_find_resume_file_input()`/`_upload_succeeded()` were scoped by DOM position relative to "Resume \*"/"Cover letter" text landmarks. This fixed the wrong-field write, but live re-investigation found the real wizard exposes **no reachable `<input type=file>` at all** once a resume is already on file — only a `button[aria-label="File options"]` menu trigger.
2. **Menu-based Replace flow**: `_open_file_options_menu()` uses the button's `aria-controls` attribute — a live-verified React Aria trigger/popup relationship — to resolve the exact menu it opens, never a page-wide `role=menu` search (the page has unrelated nav-dropdown menus, and Cover Letter has its own separate File-options menu right next to Resume's). `_replace_resume_file()` selects exactly the `role="menuitem"` with text "Replace", scoped strictly to that one menu (never Delete, never a page-wide "Replace" match), then hands the file to whichever mechanism Replace triggers — a native file chooser (`page.expect_file_chooser()`) or a newly-revealed Resume-scoped input, falling back safely (no mutation) if neither appears.
3. **Live retry #1 failed safely, no mutation**: an earlier read-only diagnostic this session had already left the menu open (`aria-expanded="true"`); clicking the trigger again tried to toggle it shut and timed out waiting for a stable click target. Fixed: `_open_file_options_menu()` now checks whether its `aria-controls` target is already visible before clicking, returning it directly if so.
4. **Success verification false negative, fixed**: the live retry's actual upload succeeded (Resume card correctly showed `test_resume.pdf`), but `_upload_succeeded()`'s DOM-order marker check reported failure — the real page has **two** "Cover letter" text matches (a stepper/nav step label plus the field's own label), and `.first` picked the nav one, which sits earlier in raw DOM order than the genuine Resume evidence, collapsing the before/after window to nothing. Fixed by scoping success to containment within the Resume **card** (`_find_file_card()`, same structural selector already used for the File-options buttons) instead of DOM-order landmarks — sidesteps duplicate-text fragility entirely. DOM-order marker logic kept as a fallback for pages without the card structure (covers the existing offline fixtures).

**Live validation, end to end, on the same application** (`469efdf8-e321-46a1-9346-70870d020736`, Data Engineer, Stefanini — re-confirmed unchanged before every mutating step): Resume card now genuinely shows `test_resume.pdf` / "New file" (verified via card-scoped, read-only re-check after the fix). Cover Letter card's earlier mistaken `test_resume.pdf` attachment is **left untouched**, per explicit instruction not to clean it up without separate authorization. No Next/Continue/Review/Submit was ever clicked; no application was submitted; no second application was created. Chrome left running throughout, never quit.

**Tests**: 169 baseline → **181 passed, 0 failed, 0 skipped** (12 new: menu-scoped Replace selection, native-file-chooser handling, fallback-input handling, safe-stop when neither appears, page-wide/Cover-Letter-menu isolation, card-scoped success verification incl. the duplicate-"Cover letter"-text regression, already-open-menu regression).

Decision gate: **PHASE 4C: COMPLETE.**

## Phase 4B.1 / 4C — Live Closure (2026-08-21)

**Recovered auth architecture** (supersedes the earlier "deferred, human prerequisite" framing): a `launch_persistent_context()` + full-quit cycle cannot carry Dice's session across a restart — diagnosed live via direct Cookies-DB and Local-Storage inspection (names/hosts only, no values ever read): the account's session material simply never persists to disk, consistent with a true browser-session-lifetime cookie that Chrome deletes on full exit by design. The compliant fix that actually works: launch a normal (non-Playwright) Chrome process against the dedicated profile with a localhost-only `--remote-debugging-port`, let the human log in directly in that real, unautomated browser, then have Playwright **attach** via `chromium.connect_over_cdp()` **without ever quitting Chrome**. No stealth, no fingerprint spoofing, no automation-flag hiding — Google's OAuth block (confirmed persistent across the bundled Chrome-for-Testing build and a real installed Chrome binary, in earlier session attempts) never triggers here because Playwright is never present during the login step at all.

**New standing architecture**: `AUTHENTICATION MODEL` — human logs in via a normal dedicated Chrome process. `RUNTIME MODEL` — that Chrome process stays running (never quit). `AUTOMATION MODEL` — Playwright attaches via localhost CDP for the duration of the session. This replaces "authentication persists across full browser restart" as the requirement; the real requirement is "session continuity while the dedicated Chrome process remains running."

**Three real selector corrections, all found live and fixed with TDD** (same discipline as Phase 4B's `apply-button-wc` finding):
1. `session.py`'s positive-auth signal — neither of the original guesses (`dashboard/logout`, "Sign Out") ever appears on real Dice pages. The real, stable, non-personal signal is `nav[aria-label="Account"]` (present consistently across page types; the `/dashboard/profiles` "My Profile" link only appears in one nav variant and was kept as a secondary check, not the primary one).
2. `session.py`'s CAPTCHA detector — a bare `"captcha" in text` substring match is a guaranteed false positive on any page using Google's ubiquitous invisible reCAPTCHA v3 badge, because "reCAPTCHA" lowercased contains "captcha" as a substring. This produced a real false CAPTCHA flag on an ordinary, fully authenticated job page during this session. Fixed to require either the classic visible `.g-recaptcha` widget, a *visible* (not hidden) captcha-sourced iframe, or an action-oriented phrase ("complete the captcha", etc.) — never a bare mention of the word.
3. `easy_apply.py`'s wizard-open evidence — the guessed `[class*='apply-wizard']` DOM landmark never appears on the real wizard page. The real signals are the exact page title (`"Apply | Dice.com"`) and the visible `"You're Applying for"` heading. Additionally, a single immediate evidence check raced ahead of what appears to be a client-side SPA route transition — replaced with a short, bounded poll (up to 6s) rather than one snapshot in time.
4. `resume.py`'s existing-resume detection — the real Dice wizard's control is labeled "Change", never "Replace" (kept as a fallback). A bare "Upload" text check was a false-negative trap: the same wizard step shows an unrelated "Upload your cover letter" prompt for the separate, optional cover-letter field, which would have incorrectly flagged a job with an existing resume as having none.

**Live validation, end to end, on one real qualified job** (`469efdf8-e321-46a1-9346-70870d020736`, Data Engineer, Stefanini): authenticated=True (real DOM evidence) → known-job navigation confirmed already_applied=False, easy_apply_visible=True → Easy Apply clicked exactly once → wizard confirmed opened at `.../job-applications/469efdf8.../wizard`, title "Apply | Dice.com", "You're Applying for / Data Engineer @ Stefanini... Step 1 of 2 / Resume & Cover Letter" → existing resume (`resume.pdf`, uploaded 8/21/2026) correctly detected. **Stopped there** — no `DICEPILOT_TEST_RESUME_PATH` was configured this session, so no upload was attempted (no fabricated resume file was created under time pressure); no Next/Continue/Review/Submit was ever clicked (no code exists for any of them). Chrome was deliberately left running, not quit, per the new architecture.

**Tests**: 153 baseline → **160 passed, 0 failed, 0 skipped** (7 new regression tests, all capturing real live-observed DOM shapes).

Decision gate: **PHASE 4B.1: PASS. PHASE 4C LIVE VALIDATION: PARTIAL PASS** (Easy Apply fully verified; resume upload verified up to but not including the actual file transfer, blocked by the test-resume prerequisite, not a defect).

## Phase 4C — Easy Apply Entry + Resume Upload (2026-08-21, original implementation)

**Scope**: Easy Apply precondition gate, opening the wizard, resume detection/upload. Nothing past that — no question-answering or Next/Review/Submit module exists anywhere in this repo (verified by a dedicated structural test, not just by omission).

**`dice_browser/easy_apply.py`** (new): `open_easy_apply(page, nav_result)` refuses before any click unless all three preconditions hold — `authenticated is True`, `already_applied is False` (not `True`, not `None`), `easy_apply_visible is True`. `already_applied=None` maps to `UNKNOWN_APPLIED_STATE`, never assumed safe. Re-verifies the live apply link's href at click time (not just trusting the possibly-stale `nav_result` flag) — rejects a stale/non-wizard link even if `nav_result.easy_apply_visible` said otherwise. Requires **both** a URL match (`job-applications` + `wizard` in the resulting URL) **and** a DOM landmark before reporting `OPENED` — a bare URL match with no corroborating content is deliberately not trusted alone. This is the only module in the codebase permitted to navigate into `/job-applications/...`; discovery code (`dice/search.py`, `dice/job_parser.py`, `dice/discovery.py`, `dice_browser/navigator.py`) remains unmodified and still forbidden from that path.

**`dice_browser/resume.py`** (new): `detect_existing_resume()` returns `True`/`False`/`None` (unknown, never guessed). `upload_resume()` checks the configured file exists *before* touching the page at all (`RESUME_FILE_MISSING`), clicks "Replace" only when an existing resume is positively detected, uses `set_input_files()`, and requires positive DOM evidence (uploaded filename or an upload-complete signal) before reporting success — never trusts "no exception was thrown." Resume path: `DICEPILOT_TEST_RESUME_PATH` env var, defaulting to `.runtime/resume/test_resume.pdf` — outside git entirely, same protection as the browser profile.

**Known limitation, not glossed over**: the exact DOM selectors for "wizard opened" and "upload succeeded" are placeholders pending live-DOM confirmation — same caveat pattern as `apply-button-wc` in Phase 4B, which turned out to be stale. They're written conservatively (requiring corroborating evidence, never a bare click-succeeded assumption), so a wrong guess fails toward `CLICK_FAILED`/`UPLOAD_FAILED`, never a false positive.

**Tests**: 129 baseline → **153 passed, 0 failed, 0 skipped** (24 new: 9 `test_dice_browser_easy_apply.py`, 10 `test_dice_browser_resume.py`, 5 `test_dice_browser_phase4c_boundary.py` — structural proof no question/Next/Review/Submit code exists anywhere).

**Live validation**: checked once (per this phase's explicit policy — no retry) whether an authenticated session already exists in the persistent profile. It doesn't — `classify_authentication()` returned `AUTH_REQUIRED`. Per the Phase 4B.1 product decision, this is not something Phase 4C's code attempts to fix; live validation of the Easy Apply open and resume upload is **BLOCKED BY AUTH PREREQUISITE**, not attempted, not faked.

Decision gate: **PHASE 4C IMPLEMENTATION: PASS. PHASE 4C LIVE VALIDATION: BLOCKED** (auth prerequisite, not a code defect).

## Authentication Bootstrap — PRODUCT DECISION (Phase 4B.1, 2026-08-21)

**Authentication bootstrap is a human/external prerequisite. The browser worker does not own first-time login, and never will.**

Required behavior, locked for all future phases:
- If an authenticated Dice session already exists in the profile → continue.
- If the session is logged out → `AUTH_REQUIRED` / `NEEDS_INPUT`.
- If Dice or Google shows OTP/CAPTCHA/security verification → `NEEDS_INPUT`.
- Never bypass or automate any of those gates, under any circumstance.

**Why**: Google's own OAuth sign-in actively detects and blocks automation-controlled browsers ("This browser or app may not be secure") — confirmed live, and confirmed to persist even across a real installed Chrome binary (`channel="chrome"`), because the detection keys on the DevTools/automation protocol connection itself, not the browser executable. Defeating that would require hiding the automation signal — stealth/fingerprint work explicitly prohibited by this project's rules regardless of who requests it. A compliant workaround (Option A: one-time login via a genuinely separate, non-Playwright-launched Chrome process pointed at the same profile directory) was investigated and is architecturally sound, but real-world execution kept getting derailed by an unrelated macOS/Chrome quirk (a second `--user-data-dir` launch silently gets absorbed into an already-running Chrome instance instead of starting a fresh process) rather than by anything wrong with the approach itself. Given the time already spent, the product decision is to stop treating this as a blocking dependency of the browser engine and instead treat "getting one authenticated session into the profile" as an operational task separate from building the rest of the system.

## Phase 4B.1 — Authenticated Session Bootstrap (2026-08-21)

**What is proven** (all verified live against real, unauthenticated Dice pages, unaffected by the deferred item below):
- Persistent browser foundation works — profile survives a full close/relaunch cycle.
- `AUTH_REQUIRED` detection works, confirmed against real logged-out Dice pages.
- Security-challenge detection works (offline, synthetic fixtures).
- Safe known-job navigation works — real jobs opened read-only, matching Phase 3B ground truth.
- Easy Apply presence inspection works (corrected mid-phase — see Phase 4B's `apply-button-wc` finding).
- Zero application-initiation requests across every live check this session.

**What is deferred, explicitly, not silently**:
- A live, positively-verified *authenticated* Dice session in the dedicated profile.
- Live proof that an authenticated session persists across a Playwright process restart.

Both require a human to complete a real login outside of Playwright's control (see product decision above) — this is an operational/credentials-access task, not an engineering task, and **does not block Phase 4C**, whose code is being written to require authentication as a precondition (`AUTH_REQUIRED`/`NEEDS_INPUT`) rather than to attempt to establish it.

**Durable code additions this session** (tri-state auth classification, TDD): `classify_authentication()` added to `dice_browser/session.py` — distinguishes `ACTIVE` (positive signal, no conflict), `AUTH_REQUIRED` (negative signal, no conflict), and `NEEDS_INPUT` (signals conflict, or neither present — ambiguous, never guessed either way). `dice_browser/navigator.py`'s `open_job()` now uses this instead of the old plain bool, so an ambiguous page correctly returns `NEEDS_INPUT` rather than being silently treated as logged-out. `dice_browser/session_bootstrap.py` (new): a manual, human-operated login-wait tool — launches the persistent profile headed, never touches the page until a local signal file appears (no DOM polling of the live Dice page while a human is mid-login), then inspects once. Supports `channel="chrome"` (a real installed Chrome, not the bundled Chrome-for-Testing build) as a legitimate, documented Playwright option — not a fingerprint/stealth trick.

Tests: 124 baseline → **129 passed, 0 failed, 0 skipped** (5 new: 4 tri-state classification tests, 1 navigator-level ambiguous-auth test).

Decision gate: **DEFERRED, not blocking.** See Phase 4C plan.

## Phase 4A — Playwright Reference Audit (2026-08-21)

Read-only audit of 3 public Dice automation repos (`KrishnaYalamarthi/Dice-Automation`, `AndrewKassab/Dice-AI`, `svrohith9/dice_jobs_ai_automation`) across all 28 requested capabilities. Key findings: none of the three persist authenticated browser state, none genuinely verify submission success, none handle OTP/security challenges, none handle checkbox/select questions. `svrohith9`'s repo has a live, functional LLM-auto-answer pathway that feeds legally-sensitive candidate data (visa/sponsorship/disability/veteran status) into an LLM prompt with no human review — marked DO NOT USE. Playwright recommended over Selenium (auto-waiting locators, native tracing, ergonomic persistent-context API — directly relevant to our hardest gaps). Proposed `dice_browser/` module map reviewed and approved; full capability-by-capability decision matrix and executive packet delivered in-session (not persisted as a separate file — this summary plus the module map below is the durable record).

## Phase 4B — Persistent Dice Browser Foundation (2026-08-21)

**Scope**: browser/session foundation only — `dice_browser/models.py`, `session.py`, `navigator.py`. No resume upload, no question answering, no Next/Review/Submit flow, no submission verification, no candidate API, no worker/orchestration. Those remain later phases.

**Dependency**: `playwright==1.62.0` added to `requirements.txt`; `chromium` browser installed via `playwright install chromium`.

**`session.py`**: `launch_persistent_session()`/`close_persistent_session()` wrap Playwright's `launch_persistent_context()` against a profile directory under `.runtime/browser_profiles/<profile_id>/` (gitignored — verified with `git check-ignore` before any real use). `ProfileLock` is a local pidfile guard (stale locks from dead PIDs are correctly reclaimed) — single-machine V1 scope, not distributed locking. `detect_challenge()` recognizes OTP/CAPTCHA/security-check phrasing and always returns a `ChallengeType`, never attempts to solve one. `is_authenticated()`'s **negative** path (login form, `/dashboard/login`, "Login" link) is confirmed against real live Dice pages; its **positive** path (an account/logout signal) is implemented but **not yet verified against a real authenticated session** — no Dice credentials exist anywhere in this project. Documented as a known limitation, not glossed over.

**`navigator.py`**: `open_job()` opens one already-discovered `canonical_url` (validated to be a `www.dice.com/job-detail/...` URL, explicitly rejecting any `/job-applications/...` path before ever navigating), never runs Dice's own search UI. Returns a `NavigationResult` with `already_applied=None` (not a guessed `False`) whenever not authenticated, since Dice can't show a per-account "applied" state to a logged-out visitor.

**Live-validation finding, corrected same session**: the Phase 4A reference locator `apply-button-wc` (independently used by all 3 audited repos) does **not** appear anywhere in current live Dice markup — confirmed by direct page inspection. `_detect_easy_apply()` was corrected to primarily use the Apply link's own href (`.../job-applications/{id}/wizard` vs `.../start-apply`), the signal already proven 20/20 in Phase 3B live validation, keeping `apply-button-wc` only as a secondary/fallback check. This is a real example of the "Dice's DOM can change without notice" risk flagged (as hypothetical) in the Phase 4A report — now confirmed true, caught by the mandatory live-validation step before claiming done.

**Tests**: 99 baseline → **124 passed, 0 failed, 0 skipped** (25 new: 12 in `test_dice_browser_session.py`, 13 in `test_dice_browser_navigator.py`). Offline tests use `page.set_content()` against synthetic HTML (real Chromium, no live Dice needed) per the phase's TDD requirement.

**Live validation** (unauthenticated only — see credentials note above): launched a real persistent context, opened two real known jobs (`469efdf8-...`, confirmed Easy Apply live in Phase 3B → `easy_apply_visible=True`; `5c2d489c-...`, confirmed not Easy Apply → `easy_apply_visible=False`) — both matched Phase 3B ground truth exactly after the correction above. Profile-in-use guard correctly rejected a second concurrent acquire with a clear PID-based error. Profile closed cleanly, persisted to disk, and was successfully reopened in a second process launch with the same result on re-check — proving genuine restart persistence. 171 real network requests captured across the session; **0** touched `/job-applications/` or `start-apply`.

**Not proven this phase** (explicitly, not silently): reusing a genuinely *authenticated* Dice session, and the positive-authentication-signal code path — both require real Dice credentials, which don't exist anywhere in this project and which I will never enter myself even if provided (credential entry is outside what I'll do regardless of instruction). This is the clearest concrete next dependency before Phase 4C.

**Local UI**: `local_app/templates/index.html`/`app.py` gained two static status boards (V1 Delivery Board, Phase 4B Browser Foundation Status) — server-rendered from a plain Python list in `app.py`, no live polling, no new controls, verified via Flask test client (200, all expected content present). Not coupled to the browser process — consistent with the original "Playwright must never run as a Flask background thread" constraint.

Decision gate: **PHASE 4B PASS. READY FOR PHASE 4C PLANNING** (not executed — awaiting approval; blocked on a real Dice credentials source regardless).

## V1 Qualification Policy — LOCKED (Phase 3D, 2026-08-21)

Approved decision: **LIKELY → HUMAN_REVIEW.**

Evidence: Phase 3B found 35/39 (89.7%, post-Phase-3C-fix) of CURRENT-qualified jobs depend on LIKELY, yet 0/15 manually reviewed LIKELY jobs had explicit description-level C2C confirmation — LIKELY is Dice's own "Third Party" structural categorization only, never a confirmed positive. AUTO was rejected: right after a phase that proved even CONFIRMED (explicit positive evidence) wasn't infallible, stacking unattended trust on a strictly weaker signal was the wrong sequencing. EXCLUDE was rejected: it discards a legitimate structural signal and leaves too few candidates (4 STRICT-qualified) to meaningfully pursue "up to 20 applications."

Policy meaning, for whenever application execution exists (not yet built):
- **CONFIRMED + Easy Apply** → eligible for automatic application
- **LIKELY + Easy Apply** → requires explicit human approval before application (not auto-applied, not silently discarded)
- **NOT_C2C** → excluded
- **UNKNOWN** → excluded from automatic application; may be reviewed separately if ever needed

This is a recorded product decision, not a new application-level state. It does **not** reuse or reinterpret `NEEDS_INPUT` — that's an `applications`-table status for an in-flight application hitting an unanswerable question (see `supabase/migrations/20260820175616_dicepilot_foundation.sql`, `LOOP.md`), a different state machine than discovery-time qualification. No code exists yet that acts on CONFIRMED vs. LIKELY differently (no application worker), so `dice/qualification.py`'s `is_qualified` boolean is intentionally unchanged — it still correctly means "discovery-time funnel+evidence+Easy-Apply eligible," not "eligible for unattended application." Adding a CONFIRMED/LIKELY branch there now would encode a policy with no consumer yet. When Phase 4E (Candidate Adapter) / Phase 6 (Sequential Worker) are built, this table is what they must implement — that is the "minimal policy/documentation change" scope for Phase 3D.

## Phase 3B — Qualification Reliability Study (2026-08-21)

Live batch: 120 unique Dice jobs across 4 roles (Software Engineer, Java Developer, Data Engineer, SAP Consultant, 30 each). Funnel: Contract/ThirdParty=120, C2C Confirmed=6, Likely=35, Unknown=76, NOT_C2C=3, Easy Apply=104. **CURRENT qualified=40, STRICT qualified=5. 35/40 (87.5%) of CURRENT-qualified depended on LIKELY.**

Manual validation (33 jobs: all 6 CONFIRMED, 15/35 LIKELY, all 3 NOT_C2C, 9/76 UNKNOWN): CONFIRMED precision 4/6 (66.7%) — **2 proven false positives**, both from negative C2C evidence the classifier failed to detect. Easy Apply: 10 positive + 10 negative manually checked against live Dice pages (apply-link href: `/wizard` = genuine vs `/start-apply` = external) — 0 false positives, 0 false negatives.

**Two bugs found and reported, not fixed in 3B** (measurement-only phase, no production changes):
- Bug 1 — `dice/c2c_classifier.py`'s negative-evidence list only recognized a handful of literal phrases ("no c2c", "w2 only", etc.), missing common refusal phrasings ("not accepting C2C", "No 3rd Party Subcontractors Permitted"). Jobs `5c2d489c-327d-4a69-8fd3-95b46c004d68` and `173695bb-b7db-427e-b1a9-7b7e8ba0cd20` misclassified CONFIRMED.
- Bug 2 — upstream `jobspy_enhanced.dice.util.clean_description()` strips HTML tags with no replacement whitespace, so adjacent block tags glue words together (`No C2C</p><p>Primary` → `No C2CPrimary`), defeating `\b`-boundary regexes. Job `660fc20a-4e64-4b01-9e10-45232b853c72` misclassified UNKNOWN instead of NOT_C2C.

Decision gate: **NOT READY FOR PLAYWRIGHT.**

## Phase 3C — C2C Correctness Hardening (2026-08-21)

TDD throughout: failing tests written first for both bugs, confirmed failing for the right reason, then the smallest fix, then full suite.

**Bug 1 fix** (`dice/c2c_classifier.py`): extended `_NEGATIVE_PATTERNS` with 6 new bounded refusal-verb frames (not a proximity/keyword-pile rule) — "not accepting/accept X", "cannot/unable to accept X", "X not accepted/allowed/permitted", "no [3rd-party/outside/external] X permitted", plus direct "no 3rd party"/"no subcontractors". Every frame is anchored on an explicit accept/allow/permit verb, never on require/need, specifically so "we do not require prior C2C experience" can't be misread as a refusal — verified by 3 dedicated overmatching-guard tests plus 3 legitimate-positive-preservation tests.

**Bug 2 fix** (`dice/upstream_adapter.py::clean_description()`): now runs `BeautifulSoup(raw, "html.parser").get_text(separator=" ")` before handing off to upstream's cleaner, so every tag boundary gets a real space; upstream's own unicode-unescape/html-unescape/whitespace-collapse behavior is otherwise unchanged (its own tag-strip regex becomes a no-op since no tags remain by then). Live re-validation surfaced a second-order finding: real Dice HTML sometimes marks ordinal suffixes as `3<sup>rd</sup> Party`, which the whitespace fix correctly turns into "3 rd Party" — `dice/c2c_classifier.py`'s "3rd party" target pattern was widened to `3\s?rd` to tolerate that.

**Real failure replay** (fresh live re-fetch, not stale stored text): all 3 known jobs now correctly return NOT_C2C.

**Reviewed-sample replay** (33-job Phase 3B manual sample, using stored descriptions): exactly 2 changed (the 2 known false positives, CONFIRMED → NOT_C2C); LIKELY (15) and UNKNOWN (9) unchanged — no new false positives or false negatives introduced anywhere in the manually-verified ground truth. **CONFIRMED false positives remaining: 0.** CONFIRMED precision in the reviewed sample: 66.7% → 100%.

**Full-120-job funnel replay** (stored descriptions — Bug 2's benefit only shows on fresh HTML, separately proven via the live re-fetch above): CONFIRMED 6→4, NOT_C2C 3→5, LIKELY unchanged at 35, UNKNOWN unchanged at 76. CURRENT qualified 40→39, STRICT qualified 5→4 (job `173695bb` dropped out of both pools — it was a false positive occupying one of the 5 STRICT slots).

**Tests**: 84 baseline → **99 passed, 0 failed, 0 skipped** (15 new: 10 in `test_c2c_classifier.py`, 5 in `test_upstream_adapter.py`).

**LIKELY policy**: untouched, as scoped. 35/39 (89.7%) of CURRENT-qualified still depends on LIKELY — see the Phase 3D executive review packet.

Decision gate: **PHASE 3C PASS. READY FOR PHASE 3D EXECUTIVE REVIEW** (not executed — awaiting approval).

## Phase 3A — Safe JobSpy Dice Integration (2026-08-21)

**Dependency**: `jobspy-enhanced-scraper==1.3.7`, pinned in `requirements.txt`. Verified before installing: PyPI's published wheel is byte-identical (SHA-256 match) to the audited GitHub HEAD; re-confirmed after installing by hashing the actually-installed file. No drift since the earlier audit.

**Safety boundary**: `dice/upstream_adapter.py` imports **only** free-standing functions from `jobspy_enhanced.dice.util` (`extract_from_next_data`, `clean_description`, `extract_salary_from_description`, `extract_salary_from_json`, `extract_experience_from_description`). **Never imports or calls `jobspy_enhanced.dice.Dice`** — confirmed by reading `Dice._fetch_job_details()`: every successful parse path unconditionally calls `_apply_w2_c2c_and_link()`, which both infers Easy Apply from URL absence and makes a live GET to `/job-applications/{id}/start-apply`. There's no way to use the `Dice` class without also triggering that, so it's avoided entirely — function-level imports only, each independently audited.

**Adopted from upstream**: `__NEXT_DATA__` parsing as an additional first-try tier (falls through safely to our existing JSON-LD parse if absent or incomplete), `clean_description()` (handles unicode-escaped description text our own stripper didn't), salary and experience text extraction (new capability, stored in `dice_jobs.raw_metadata` — no schema migration, that column already existed for exactly this).

**Explicitly not adopted**: the `Dice` class itself, `scrape_jobs()`, `_apply_w2_c2c_and_link`, `_resolve_apply_redirect`, all apply-URL extraction, `extract_apply_type_from_page`, the entire W2/C2C bucket engine, skills extraction, company-field extraction, the HTML-fallback parsing tier.

**Unchanged, per explicit lock**: `dice/c2c_classifier.py`, `dice/easy_apply_detector.py`, `dice/qualification.py` — zero lines touched. Contract/Third Party search-level filtering in `dice/search.py` — unchanged (confirmed upstream still never sends `filters.employmentType`).

**Tests**: 84 passed, 0 failed, 0 skipped (65 baseline + 19 new `test_upstream_adapter.py`), including two static-analysis tests (via `ast`, not naive text search) proving no DicePilot code imports the `Dice` class or references any `job-applications`/`start-apply` URL pattern in actual code (not docstrings), plus a runtime test that runs a full discovery pass against mocked responses and asserts every `requests.get` call target is either the search page or `/job-detail/` — never `/job-applications/`.

**Live validation** (51 real Dice jobs, 3 roles — Software Engineer, Java Developer, SAP Consultant, ~17 each): Discovered=51, Contract/ThirdParty=51 (search filter working), C2C Confirmed=3, C2C Likely=13, C2C Unknown=34, NOT_C2C=1, Easy Apply verified=41, **Qualified=16**. `salary_text`/`experience_text` populated for 22/51 jobs (upstream's `__NEXT_DATA__` tier was not reachable on any of the 51 — every job fell through to the JSON-LD path, which still successfully extracts salary/experience from description text; the resilience fallback worked exactly as designed even though tier 1 never fired in this sample).

5 targeted spot-checks against **fresh, independent re-fetches** (not the cached validation data) — 3 C2C classifications (1 CONFIRMED, 1 UNKNOWN, 1 NOT_C2C) and 2 Easy Apply signals (1 true, 1 false) — all matched with zero drift. The NOT_C2C case is a good real-world confirmation of the negative-override rule: live description text was *"MUST be worked on a W2 Only. No C2C eligibility..."*, which contains the substring "C2C" (from "No C2C") but correctly classifies NOT_C2C, not CONFIRMED.

**V1 "20 qualifying jobs" question — honest answer**: this specific 51-job sample produced 16 QUALIFIED, just under 20. The ~31% qualification rate strongly suggests a modestly larger batch (e.g. 4 roles × 20 jobs) would comfortably clear 20 — but that hasn't been run yet, so it's a projection, not a confirmed result. Worth an explicit decision on whether to run that larger batch before treating "up to 20 qualifying jobs" as proven.

51 real discovered jobs are now in `dice_jobs` (not cleaned up — genuine discovery output, not test data, same policy as the earlier 5 from your own visual tests).

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
