"""Phase 5 boundary guard: structural proof that submission.py can click
Submit at most once per call and never contains a retry/loop path around
that click -- the same "lock it in structurally" discipline as every
earlier phase's boundary tests.
"""
from __future__ import annotations

from pathlib import Path

DICE_BROWSER_DIR = Path(__file__).parent.parent / "dice_browser"
DB_DIR = Path(__file__).parent.parent / "db"

SUBMISSION_SOURCE = (DICE_BROWSER_DIR / "submission.py").read_text(encoding="utf-8")
SUBMISSION_REPO_SOURCE = (DB_DIR / "submission_repository.py").read_text(encoding="utf-8")


def test_submission_module_clicks_submit_at_most_once():
    # Exactly one .click() call anywhere in the module -- the Submit
    # button click itself. No second click target, no retry loop.
    assert SUBMISSION_SOURCE.count(".click()") == 1


def test_no_next_review_continue_click_functions():
    lowered = SUBMISSION_SOURCE.lower()
    for forbidden in ("click_next", "click_review", "click_continue"):
        assert forbidden not in lowered


def test_no_automatic_retry_loop_around_submit():
    # The bounded confirmation-poll loop (while True: ... deadline check
    # ... return) is legitimate -- it polls for EVIDENCE after the one
    # click already happened, it never clicks Submit again. What's
    # actually forbidden is anything that would re-click or re-attempt
    # the submission itself.
    lowered = SUBMISSION_SOURCE.lower()
    for forbidden in ("for attempt", "retry_count", "max_retries", "for _ in range", "while not result"):
        assert forbidden not in lowered, f"found forbidden retry pattern {forbidden!r}"
    assert SUBMISSION_SOURCE.count("submit_button.first.click()") == 1


def test_submission_module_never_touches_supabase():
    for forbidden in ("import supabase", "get_supabase_client", "db.application_repository", "db.intervention_repository"):
        assert forbidden not in SUBMISSION_SOURCE.lower()


def test_submission_repository_never_touches_dice_browser_pages():
    lowered = SUBMISSION_REPO_SOURCE.lower()
    for forbidden in ("playwright", ".click()", "page.", "dice_browser.session", "dice_browser.questions"):
        assert forbidden not in lowered
