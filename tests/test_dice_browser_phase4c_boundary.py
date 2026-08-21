"""Phase 4C boundary guard, extended through Phase 5: structural proof
that Next/Continue/Review/answer-question code exists nowhere in this
repo, and that Submit exists in exactly one place (dice_browser/
submission.py, Phase 5, itself gated and separately boundary-tested).
"""
from __future__ import annotations

from pathlib import Path

DICE_BROWSER_DIR = Path(__file__).parent.parent / "dice_browser"


def test_questions_module_is_detection_only():
    # Phase 4D-A: dice_browser/questions.py now exists (Review-screen /
    # NO_QUESTIONS_PRESENT detection), but it must never click, fill, or
    # select anything -- detection/extraction only, same discipline as
    # every other module in this repo.
    source = (DICE_BROWSER_DIR / "questions.py").read_text(encoding="utf-8")
    assert ".click()" not in source
    assert "set_input_files" not in source
    assert ".fill(" not in source
    assert ".select_option(" not in source
    assert ".check()" not in source


def test_submission_module_clicks_exactly_one_submit_button():
    # Phase 5: dice_browser/submission.py now exists and is the ONE
    # module in this repo permitted to click Submit -- gated behind
    # explicit preconditions (see test_phase5_boundary.py for the full
    # structural guard). It must click at most once, never click
    # Next/Continue/Review, and never answer a question itself.
    source = (DICE_BROWSER_DIR / "submission.py").read_text(encoding="utf-8")
    assert source.count(".click()") == 1
    lowered = source.lower()
    for forbidden in ("click_next", "click_review", "click_continue", "answer_question"):
        assert forbidden not in lowered


def test_no_next_review_submit_functions_outside_submission_module():
    # Every OTHER module in dice_browser/ must still never define
    # Next/Review/Submit/answer-question functions -- only submission.py
    # (Phase 5, explicitly authorized and itself boundary-tested) may
    # DEFINE the Submit click. Exemptions, and why:
    #   models.py       -- data-only (dataclasses/enums), docstrings
    #                       legitimately name submit_application() for
    #                       cross-reference documentation.
    #   wizard_navigation.py -- Phase 6, defines click_next() (Next only,
    #                       never Submit/Review) -- itself boundary-
    #                       tested in test_phase6_boundary.py.
    #   worker.py       -- Phase 6, orchestrates by CALLING the already-
    #                       gated submit_application()/click_next(); it
    #                       never defines a second click-Submit path
    #                       itself -- verified separately (zero of its
    #                       own .click() calls) in test_phase6_boundary.py.
    forbidden = ("click_review", "click_submit", "answer_question")
    exempt = {"submission.py", "models.py", "wizard_navigation.py", "worker.py"}
    for py_file in DICE_BROWSER_DIR.glob("*.py"):
        if py_file.name in exempt:
            continue
        source = py_file.read_text(encoding="utf-8").lower()
        for name in forbidden:
            assert name not in source, f"found forbidden function name {name!r} in {py_file}"
        # click_next/submit_application (bare names) are checked here too,
        # for every file that ISN'T one of the two Phase 6 modules that
        # legitimately reference them.
        for name in ("click_next", "submit_application"):
            assert name not in source, f"found forbidden reference {name!r} in {py_file}"


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
