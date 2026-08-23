"""Phase 6.3 / 7.1: standalone worker daemon.

Polls Supabase for PENDING DicePilot runs and processes them against the
existing authenticated Chrome/CDP session. This is the ONLY process that
ever claims and executes a run -- Vercel (or local Flask; same code
either way) only ever writes a run PENDING and redirects to its progress
page.

Same architecture whether the browser it attaches to is on the operator's
own Mac or on a cloud VM: this process only ever CDP-attaches to an
already-running, already-authenticated Chrome -- it never launches or
owns that Chrome itself, and never automates login. DICEPILOT_CDP_URL
(default http://127.0.0.1:9333, i.e. same host) is the only thing that
changes between a local and a cloud deployment; on a cloud VM the
worker and the browser normally run on the SAME host (see
deploy/cloud-worker/), so 127.0.0.1 remains correct there too -- it just
no longer means "the user's Mac".

Reuses dice_browser.worker.run_worker_for_run() completely unchanged --
this module owns only the poll/claim/heartbeat/connect loop around it,
none of the actual application flow (live requalify, auth, Easy Apply,
resume, questions, Review, submit -- all Phase 6, untouched).
"""
from __future__ import annotations

import argparse
import os
import time
import uuid

from dice_browser.browser_provider import resolve_browser_provider
from dice_browser.session import BROWSER_PROFILE_DIR_ENV_VAR, clean_stale_singleton_locks

import run_registry
from dice_browser.worker import SubmissionPolicy, run_worker_for_run

DEFAULT_POLL_INTERVAL_SECONDS = 5
DEFAULT_AUTH_CHECK_INTERVAL_SECONDS = 60
CDP_URL_ENV_VAR = "DICEPILOT_CDP_URL"
DEFAULT_CDP_URL = "http://127.0.0.1:9333"
_DICE_HOME_URL = "https://www.dice.com/home-feed"


def default_cdp_url() -> str:
    return os.environ.get(CDP_URL_ENV_VAR, DEFAULT_CDP_URL)


def _connect(cdp_url: str):
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        ctx = browser.contexts[0]
        page = ctx.new_page()
    except Exception:
        playwright.stop()
        raise
    return playwright, page


def _check_browser_and_auth(cdp_url: str) -> str:
    """One connect / navigate / classify / disconnect cycle -- returns
    ONLINE, BROWSER_DISCONNECTED, AUTH_REQUIRED, or SECURITY_CHALLENGE.
    Used for the periodic idle-poll status the frontend shows; never
    called mid-run (a claimed run's own live requalification is what
    matters there, and this project's own established rule is never to
    poll a live Dice page more than necessary)."""
    try:
        playwright, page = _connect(cdp_url)
    except Exception:
        return "BROWSER_DISCONNECTED"
    try:
        from dice_browser.models import BrowserState
        from dice_browser.session import classify_authentication, detect_challenge

        page.goto(_DICE_HOME_URL, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        if detect_challenge(page) is not None:
            return "SECURITY_CHALLENGE"
        if classify_authentication(page) != BrowserState.ACTIVE:
            return "AUTH_REQUIRED"
        return "ONLINE"
    finally:
        playwright.stop()


def run_daemon(
    worker_id: str,
    cdp_url: str | None = None,
    resume_path: str | None = None,
    submission_policy_override: SubmissionPolicy | None = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    auth_check_interval: float = DEFAULT_AUTH_CHECK_INTERVAL_SECONDS,
    max_iterations: int | None = None,
) -> None:
    """Heartbeat, then claim-and-process, forever. `max_iterations`
    bounds the loop for tests -- production callers (main()) never pass
    it, so the daemon really does run until killed.

    Submission policy belongs to the RUN, not to this process: each
    claimed run's own persisted submission_policy is what actually gets
    used, since one long-running daemon may claim many runs over its
    lifetime with different policies. submission_policy_override exists
    only as an explicit development/debug escape hatch -- normal
    production operation should never pass it.

    While idle (no PENDING run), heartbeat status is refreshed from a
    real browser/auth check at most once per auth_check_interval seconds
    (not every poll -- a full page load every few seconds would be
    wasteful and pointless) so the frontend's ONLINE / BROWSER_
    DISCONNECTED / AUTH_REQUIRED / SECURITY_CHALLENGE display stays
    truthful even when nothing is actively running."""
    cdp_url = cdp_url or default_cdp_url()
    last_status = "ONLINE"
    last_check_at = 0.0
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1

        run = run_registry.claim_next_pending_run(worker_id)
        if run is None:
            now = time.monotonic()
            if now - last_check_at >= auth_check_interval:
                last_status = _check_browser_and_auth(cdp_url)
                last_check_at = now
            run_registry.write_heartbeat(worker_id, status=last_status)
            time.sleep(poll_interval)
            continue

        policy = submission_policy_override or SubmissionPolicy(run["submission_policy"])

        try:
            playwright, page = _connect(cdp_url)
        except Exception:
            last_status = "BROWSER_DISCONNECTED"
            last_check_at = time.monotonic()
            run_registry.write_heartbeat(worker_id, status=last_status)
            # Never silently consume a claimed run when the browser
            # isn't reachable -- hand it back to PENDING so this (or a
            # later) daemon start can pick it up once Chrome is back.
            run_registry.update_run_status(run["id"], "PENDING")
            time.sleep(poll_interval)
            continue

        last_status = "ONLINE"
        last_check_at = time.monotonic()
        run_registry.write_heartbeat(worker_id, status=last_status)
        try:
            run_worker_for_run(page, run["id"], worker_id, submission_policy=policy, resume_path=resume_path)
        finally:
            playwright.stop()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dice_browser.worker_daemon",
        description=(
            "Persistent DicePilot worker: polls Supabase for PENDING runs and processes "
            "them one at a time against the already-authenticated Chrome/CDP session "
            "at DICEPILOT_CDP_URL (default http://127.0.0.1:9333 -- same host as the "
            "worker, whether that's a local Mac or a cloud VM)."
        ),
    )
    parser.add_argument("--cdp-url", default=None, help="Overrides DICEPILOT_CDP_URL for this invocation.")
    parser.add_argument("--resume-path", default=None)
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS)
    parser.add_argument("--auth-check-interval", type=float, default=DEFAULT_AUTH_CHECK_INTERVAL_SECONDS)
    parser.add_argument(
        "--submission-policy",
        choices=[p.value for p in SubmissionPolicy],
        default=None,
        help=(
            "Development/debug override only -- applies to every run this invocation claims, "
            "ignoring each run's own stored policy. Normal operation should omit this entirely; "
            "each run's persisted submission_policy (set when it was created) is used by default."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    worker_id = f"worker-{uuid.uuid4()}"
    cdp_url = args.cdp_url or default_cdp_url()
    override = SubmissionPolicy(args.submission_policy) if args.submission_policy else None

    print("Reconciling any state left behind by a previous crashed worker...")
    app_recovery = run_registry.recover_stale_applications()
    run_recovery = run_registry.recover_orphaned_runs()
    if app_recovery["requeued"] or app_recovery["needs_verification"] or run_recovery:
        print(f"  requeued (safe, pre-submit): {app_recovery['requeued']}")
        print(f"  needs manual verification (possibly post-submit): {app_recovery['needs_verification']}")
        print(f"  runs handed back to PENDING: {run_recovery}")
    else:
        print("  nothing to recover.")

    provider = resolve_browser_provider()
    if provider == "steel":
        profile_dir = os.environ.get(BROWSER_PROFILE_DIR_ENV_VAR)
        if profile_dir:
            removed = clean_stale_singleton_locks(profile_dir)
            if removed:
                print(f"Cleared stale Chrome lock artifacts from a previous container instance: {removed}")

    print(f"DicePilot worker daemon starting -- worker_id={worker_id}, browser_provider={provider}, cdp_url={cdp_url}")
    if override:
        print(f"WARNING: --submission-policy {override.value} overrides every claimed run's own stored policy (debug only)")
    run_daemon(
        worker_id,
        cdp_url=cdp_url,
        resume_path=args.resume_path,
        submission_policy_override=override,
        poll_interval=args.poll_interval,
        auth_check_interval=args.auth_check_interval,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
