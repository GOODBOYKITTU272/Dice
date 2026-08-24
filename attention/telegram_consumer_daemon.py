"""Phase 7.8: standalone, always-on Telegram consumer.

The missing piece that let every inbound Apply/Skip/Confirm/Edit tap in
this project's live testing only ever get processed when a human
manually ran a one-off poll script -- there was no continuously-running
process at all. Long-polls Telegram via TelegramProvider.fetch_updates()'s
own timeout parameter (a real server-side long-poll, not a sleep loop)
and dispatches every update through the existing, unmodified
attention.consumer.poll_telegram_once() -- no new business logic here,
this module only owns "keep calling it forever, survive transient
errors", exactly mirroring dice_browser.worker_daemon's own relationship
to dice_browser.worker.

MUST be run as a standalone process (`python -m
attention.telegram_consumer_daemon`), same as dice_browser.worker_daemon
-- see deploy/cloud-worker/README.md for the systemd unit that runs it
continuously in production.
"""
from __future__ import annotations

import sys
import time

from attention.consumer import poll_telegram_once
from attention.providers.telegram import TelegramNotConfiguredError, TelegramProvider

DEFAULT_LONG_POLL_TIMEOUT_SECONDS = 25
DEFAULT_ERROR_BACKOFF_SECONDS = 5


def check_startup_readiness() -> dict[str, bool]:
    """Fail loudly at startup rather than silently sitting there unable
    to do anything -- same principle as dice_browser.worker_daemon's own
    readiness check (Phase 7.7)."""
    results: dict[str, bool] = {}

    try:
        TelegramProvider().fetch_updates(offset=None, timeout=0)
        results["Telegram"] = True
    except TelegramNotConfiguredError:
        results["Telegram"] = False
    except Exception:
        # Any other failure (network, Telegram API error) still means
        # "not ready right now" -- this is a startup check, not a retry.
        results["Telegram"] = False

    try:
        from db.supabase_client import get_supabase_client

        get_supabase_client().table("attention_events").select("id").limit(1).execute()
        results["Supabase"] = True
    except Exception:
        results["Supabase"] = False

    return results


def run_consumer_daemon(
    max_iterations: int | None = None,
    long_poll_timeout: int = DEFAULT_LONG_POLL_TIMEOUT_SECONDS,
    error_backoff_seconds: float = DEFAULT_ERROR_BACKOFF_SECONDS,
) -> None:
    """Long-polls forever (or `max_iterations` times, for tests). Never
    crashes the whole process over one bad poll cycle -- logs and backs
    off, then keeps going, matching worker_daemon's own resilience
    philosophy: a single bad Telegram API call must never take down the
    one process every real Apply/Skip/Confirm/Edit tap depends on."""
    provider = TelegramProvider()
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        try:
            results = poll_telegram_once(provider, timeout=long_poll_timeout)
            if results:
                print(f"Processed {len(results)} Telegram update(s): {results}")
        except Exception as exc:  # noqa: BLE001 - one bad poll cycle must never crash the whole consumer
            print(f"Telegram poll failed, backing off {error_backoff_seconds}s: {exc}")
            time.sleep(error_backoff_seconds)


def main(argv: list[str] | None = None) -> int:
    print("Checking startup configuration...")
    readiness = check_startup_readiness()
    for name, passed in readiness.items():
        print(f"  {name:<20} {'PASS' if passed else 'FAIL'}")
    if not all(readiness.values()):
        print("Refusing to start: mandatory configuration is missing (see FAIL above). Fix it and restart.")
        return 1

    print("DicePilot Telegram consumer starting -- long-polling for Apply/Skip/Confirm/Edit taps...")
    run_consumer_daemon()
    return 0


if __name__ == "__main__":
    sys.exit(main())
