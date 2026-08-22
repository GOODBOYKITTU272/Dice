"""Phase 6.3: standalone worker daemon.

Polls Supabase for PENDING DicePilot runs and processes them against the
existing authenticated Chrome/CDP session. This is the ONLY process that
ever claims and executes a run -- Vercel (or local Flask; same code
either way) only ever writes a run PENDING and redirects to its progress
page.

MUST be started manually (`python -m dice_browser.worker_daemon`), on
the Mac that has the dedicated, already-authenticated Dice Chrome open
(same requirement as dice_browser.worker, whose CDP-attach pattern this
reuses verbatim). Never launched by a Flask request, on Vercel or
locally.

Reuses dice_browser.worker.run_worker_for_run() completely unchanged --
this module owns only the poll/claim/heartbeat/connect loop around it,
none of the actual application flow (live requalify, auth, Easy Apply,
resume, questions, Review, submit -- all Phase 6, untouched).
"""
from __future__ import annotations

import argparse
import time
import uuid

import run_registry
from dice_browser.worker import SubmissionPolicy, run_worker_for_run

DEFAULT_POLL_INTERVAL_SECONDS = 5


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


def run_daemon(
    worker_id: str,
    cdp_url: str = "http://127.0.0.1:9333",
    resume_path: str | None = None,
    submission_policy_override: SubmissionPolicy | None = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
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
    production operation should never pass it, and doing so applies the
    same override to every run this invocation ever claims (a deliberate
    choice for a debug session, never a substitute for a run's own
    stored policy)."""
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1

        run = run_registry.claim_next_pending_run(worker_id)
        if run is None:
            run_registry.write_heartbeat(worker_id, status="ONLINE")
            time.sleep(poll_interval)
            continue

        policy = submission_policy_override or SubmissionPolicy(run["submission_policy"])

        try:
            playwright, page = _connect(cdp_url)
        except Exception:
            run_registry.write_heartbeat(worker_id, status="BROWSER_DISCONNECTED")
            # Never silently consume a claimed run when the browser
            # isn't reachable -- hand it back to PENDING so this (or a
            # later) daemon start can pick it up once Chrome is back.
            run_registry.update_run_status(run["id"], "PENDING")
            time.sleep(poll_interval)
            continue

        run_registry.write_heartbeat(worker_id, status="ONLINE")
        try:
            run_worker_for_run(page, run["id"], worker_id, submission_policy=policy, resume_path=resume_path)
        finally:
            playwright.stop()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dice_browser.worker_daemon",
        description=(
            "Persistent DicePilot worker: polls Supabase for PENDING runs and processes "
            "them one at a time against the dedicated, already-authenticated Chrome/CDP "
            "session. Run this yourself, in your own terminal."
        ),
    )
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9333")
    parser.add_argument("--resume-path", default=None)
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS)
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
    override = SubmissionPolicy(args.submission_policy) if args.submission_policy else None
    print(f"DicePilot worker daemon starting -- worker_id={worker_id}, cdp_url={args.cdp_url}")
    if override:
        print(f"WARNING: --submission-policy {override.value} overrides every claimed run's own stored policy (debug only)")
    run_daemon(
        worker_id,
        cdp_url=args.cdp_url,
        resume_path=args.resume_path,
        submission_policy_override=override,
        poll_interval=args.poll_interval,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
