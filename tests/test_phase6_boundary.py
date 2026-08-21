"""Phase 6 boundary guard: structural proof that the worker never gains
a second, unguarded way to click Submit or Review, and that Next-clicking
lives in exactly one place. Same "lock it in structurally" discipline as
every earlier phase's boundary tests.
"""
from __future__ import annotations

from pathlib import Path

DICE_BROWSER_DIR = Path(__file__).parent.parent / "dice_browser"
DB_DIR = Path(__file__).parent.parent / "db"
DICE_DIR = Path(__file__).parent.parent / "dice"

WORKER_SOURCE = (DICE_BROWSER_DIR / "worker.py").read_text(encoding="utf-8")
WIZARD_NAV_SOURCE = (DICE_BROWSER_DIR / "wizard_navigation.py").read_text(encoding="utf-8")
SUBMISSION_SOURCE = (DICE_BROWSER_DIR / "submission.py").read_text(encoding="utf-8")


def test_wizard_navigation_clicks_next_exactly_once():
    assert WIZARD_NAV_SOURCE.count(".click()") == 1


def test_wizard_navigation_never_clicks_submit_or_review():
    # Quoted string literals only (matching resume.py's own boundary
    # test pattern) -- code that SEARCHES for "Submit"/"Review" as
    # button text is what's forbidden, not prose in a docstring
    # explaining that this module doesn't do that.
    lowered = WIZARD_NAV_SOURCE.lower()
    for forbidden in ("submit", "review", "click_review", "click_submit"):
        assert f'"{forbidden}"' not in lowered and f"'{forbidden}'" not in lowered, (
            f"found forbidden string literal {forbidden!r} in wizard_navigation.py"
        )


def test_worker_defines_zero_clicks_of_its_own():
    # worker.py orchestrates by CALLING submit_application()/click_next()
    # -- the actual clicking happens inside those already-gated
    # functions. worker.py itself must never contain a raw .click() call.
    assert ".click()" not in WORKER_SOURCE


def test_worker_never_defines_review_or_submit_click_functions():
    lowered = WORKER_SOURCE.lower()
    for forbidden in ("def click_submit", "def click_review", "def click_next"):
        assert forbidden not in lowered


def test_submission_module_click_count_unchanged_by_phase_6():
    # Locks in that Phase 6 didn't quietly add a second Submit-click path
    # inside submission.py itself.
    assert SUBMISSION_SOURCE.count(".click()") == 1


def test_worker_never_touches_supabase_client_directly():
    # All DB access must go through db.application_repository /
    # db.intervention_repository / db.submission_repository -- worker.py
    # never calls get_supabase_client() itself.
    assert "get_supabase_client" not in WORKER_SOURCE


def test_worker_run_loop_has_no_unbounded_retry():
    # The bounded question-walking loop (while True: ... return on every
    # branch -- reaches Review, hits a blocker, or a click fails) is
    # legitimate and not what this guards against. What's forbidden is
    # retrying the SAME failed action (e.g. re-clicking Submit) rather
    # than reporting the classification once, as Phase 5 already locked
    # in for submission.py itself.
    lowered = WORKER_SOURCE.lower()
    for forbidden in ("retry_count", "max_retries", "for attempt", "while not result"):
        assert forbidden not in lowered, f"found forbidden pattern {forbidden!r} in worker.py"


def test_answer_resolution_never_maps_sensitive_fields():
    source = (DICE_DIR / "answer_resolution.py").read_text(encoding="utf-8")
    lowered = source.lower()
    for forbidden in ('"visa_type"', "'visa_type'", '"work_authorized"', "'work_authorized'", '"requires_sponsorship"', "'requires_sponsorship'"):
        assert forbidden not in lowered, f"found forbidden sensitive-field mapping {forbidden!r}"
