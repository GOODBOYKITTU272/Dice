"""Phase 4F boundary guard: structural proof that the NEEDS_INPUT
pause/resume layer never touches Dice, never stores secrets, and never
guesses an answer -- the same "lock it in structurally" discipline as
Phase 4C/4D's boundary tests.
"""
from __future__ import annotations

from pathlib import Path

DB_DIR = Path(__file__).parent.parent / "db"
INTERVENTION_SOURCE = (DB_DIR / "intervention_repository.py").read_text(encoding="utf-8")


# 17. no cookies/tokens stored
def test_no_cookie_or_token_handling_in_intervention_repository():
    lowered = INTERVENTION_SOURCE.lower()
    for forbidden in ("cookie", "session_token", "auth_token", "password", "browser_state"):
        assert forbidden not in lowered, f"found forbidden term {forbidden!r} in intervention_repository.py"


# 18. no guessing fallback
def test_no_default_or_guessed_answer_fallback():
    lowered = INTERVENTION_SOURCE.lower()
    for forbidden in ('"na"', "'na'", "default_answer", "fallback_answer", "guess"):
        assert forbidden not in lowered, f"found forbidden term {forbidden!r} in intervention_repository.py"


# 22. no Submit/Review click anywhere in this phase
def test_no_browser_mutation_or_submission_code():
    for forbidden in (".click()", "set_input_files", ".fill(", ".select_option(", ".check()", "playwright"):
        assert forbidden.lower() not in INTERVENTION_SOURCE.lower(), (
            f"found forbidden term {forbidden!r} in intervention_repository.py"
        )
    for forbidden in ("click_next", "click_review", "click_submit", "submit_application"):
        assert forbidden not in INTERVENTION_SOURCE.lower()
