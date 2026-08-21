"""Phase 4C boundary guard: structural proof that no question-answering,
Next/Review, or Submit code exists anywhere in this repo yet. The "stop"
after resume upload is structural (no such module exists), not a runtime
check that could be bypassed -- this test locks that in.
"""
from __future__ import annotations

from pathlib import Path

DICE_BROWSER_DIR = Path(__file__).parent.parent / "dice_browser"


def test_no_question_answering_module_exists():
    assert not (DICE_BROWSER_DIR / "questions.py").exists()


def test_no_submission_module_exists():
    assert not (DICE_BROWSER_DIR / "submission.py").exists()


def test_no_next_review_submit_functions_anywhere_in_dice_browser():
    forbidden = ("click_next", "click_review", "click_submit", "answer_question", "submit_application")
    for py_file in DICE_BROWSER_DIR.glob("*.py"):
        source = py_file.read_text(encoding="utf-8").lower()
        for name in forbidden:
            assert name not in source, f"found forbidden function name {name!r} in {py_file}"


def test_easy_apply_module_never_navigates_past_the_wizard_landing():
    # Static check: dice_browser/easy_apply.py may click exactly the
    # Easy Apply entry link -- it must not contain any second .click()
    # target implying a Next/Review/Submit step.
    source = (DICE_BROWSER_DIR / "easy_apply.py").read_text(encoding="utf-8")
    assert source.count(".click()") == 1


def test_resume_module_never_clicks_anything_but_change_or_replace():
    # resume.py may click exactly four things: the existing-resume swap
    # control (labeled "Change" on real Dice, "Replace" kept as a fallback
    # for a different UI variant), a File-options menu trigger button, and
    # the "Replace" menuitem inside that menu (twice: the direct click and
    # the one wrapped in expect_file_chooser) -- never Next/Continue/Submit.
    source = (DICE_BROWSER_DIR / "resume.py").read_text(encoding="utf-8")
    assert source.count(".click()") == 4
    assert "change" in source.lower()
    assert "replace" in source.lower()
    for forbidden in ("next", "continue", "submit", "review"):
        assert f'"{forbidden}"' not in source.lower() and f"'{forbidden}'" not in source.lower()
