"""Phase 4C: resume detection and upload only. No question answering, no
Next/Review/Submit -- those modules don't exist anywhere in this repo.
Offline tests only (synthetic HTML + a dummy, non-personal test fixture
file); no live Dice needed for any of this.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from dice_browser.resume import (
    _find_cover_letter_file_options_button,
    _find_resume_file_options_button,
    _open_file_options_menu,
    _replace_resume_file,
    _upload_succeeded,
    detect_existing_resume,
    upload_resume,
)

FIXTURES = Path(__file__).parent / "fixtures"
DUMMY_RESUME = FIXTURES / "dummy_test_resume.txt"
MISSING_RESUME = FIXTURES / "does_not_exist.pdf"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture()
def page(browser):
    pg = browser.new_page()
    yield pg
    pg.close()


# ── missing-file precondition (checked before touching the page at all) ──


def test_upload_resume_missing_file_before_page_interaction(page):
    page.set_content("<html><body><input type='file' id='fsp-fileUpload'></body></html>")
    result = upload_resume(page, resume_path=MISSING_RESUME)
    assert result.uploaded is False
    assert result.reason == "RESUME_FILE_MISSING"


# ── existing-resume detection: TRUE / FALSE / UNKNOWN ────────────────────


def test_detect_existing_resume_true_when_replace_control_present(page):
    page.set_content('<html><body><button>Replace</button><input type="file" id="fsp-fileUpload"></body></html>')
    assert detect_existing_resume(page) is True


def test_detect_existing_resume_true_on_real_dice_wizard_shape(page):
    # Real live finding (Phase 4B.1 live closure, 2026-08-21): the actual
    # Dice apply-wizard resume step never says "Replace" -- it says
    # "Change". And a broad "Upload" text check is a false-negative trap:
    # the SAME page also has an unrelated "Upload your cover letter"
    # prompt for the separate, optional cover-letter field, which would
    # incorrectly flip existing-resume detection to False even though a
    # resume clearly is already on file.
    page.set_content(
        """
        <html><body>
        <p>Resume *</p>
        <p>resume.pdf</p>
        <p>Uploaded to profile on 8/21/2026</p>
        <button>Change</button>
        <p>Cover letter</p>
        <p>Optional</p>
        <p>Upload your cover letter</p>
        <input type="file">
        </body></html>
        """
    )
    assert detect_existing_resume(page) is True


def test_detect_existing_resume_true_when_resume_card_marker_present(page):
    page.set_content('<html><body><div class="current-resume-card">my_resume.pdf</div></body></html>')
    assert detect_existing_resume(page) is True


def test_detect_existing_resume_false_when_only_upload_prompt_present(page):
    page.set_content('<html><body><p>Upload your resume</p><input type="file" id="fsp-fileUpload"></body></html>')
    assert detect_existing_resume(page) is False


def test_detect_existing_resume_unknown_when_ambiguous(page):
    page.set_content("<html><body><p>Some unrelated application step.</p></body></html>")
    assert detect_existing_resume(page) is None


# ── upload flow ───────────────────────────────────────────────────────────


def test_upload_attempted_when_no_existing_resume(page):
    page.set_content(
        '<html><body><p>Upload your resume</p>'
        '<input type="file" id="fsp-fileUpload">'
        '</body></html>'
    )
    # No success evidence in this fixture -- expect UPLOAD_FAILED, but the
    # important assertion is that set_input_files was actually attempted
    # (file accepted without error) rather than skipped.
    result = upload_resume(page, resume_path=DUMMY_RESUME)
    assert result.existing_resume_detected is False
    assert result.uploaded is False
    assert result.reason.startswith("UPLOAD_FAILED")


_REAL_FILE_CARD_CLASS = "my-2 rounded-lg border border-gray-200 bg-white p-4 shadow-xs mb-6"

_WIZARD_WITH_FILE_OPTIONS_FIXTURE = f"""
<html><body>
<div class="{_REAL_FILE_CARD_CLASS}">
  <p>Resume *</p>
  <p>resume.pdf</p>
  <p>Uploaded to profile on 8/21/2026</p>
  <button aria-label="File options" id="btn-a">A</button>
</div>
<div class="{_REAL_FILE_CARD_CLASS}">
  <p>Cover letter</p>
  <p>Optional</p>
  <p>test_resume.pdf</p>
  <p>New file</p>
  <button aria-label="File options" id="btn-b">B</button>
</div>
<div class="overflow-hidden text-foreground-primary inline-flex flex-col items-end justify-center gap-6 rounded-lg border border-cyan-300 bg-cyan-50 p-6 shadow-sm">
  <p>Go visible! Get noticed by recruiters, change your profile to visible.</p>
</div>
<div class="flex flex-col gap-6 overflow-hidden rounded-lg border bg-surface-primary p-6 shadow-sm">
  <p>Similar job: Some Other Role</p>
  <button aria-label="File options" id="btn-unrelated">Unrelated</button>
</div>
</body></html>
"""


def test_finds_resume_card_file_options_button_deterministically(page):
    page.set_content(_WIZARD_WITH_FILE_OPTIONS_FIXTURE)
    btn = _find_resume_file_options_button(page)
    assert btn is not None
    assert btn.get_attribute("id") == "btn-a"


def test_finds_cover_letter_card_file_options_button_deterministically(page):
    page.set_content(_WIZARD_WITH_FILE_OPTIONS_FIXTURE)
    btn = _find_cover_letter_file_options_button(page)
    assert btn is not None
    assert btn.get_attribute("id") == "btn-b"


def test_resume_and_cover_letter_buttons_are_different_elements(page):
    page.set_content(_WIZARD_WITH_FILE_OPTIONS_FIXTURE)
    resume_btn = _find_resume_file_options_button(page)
    cover_btn = _find_cover_letter_file_options_button(page)
    assert resume_btn.get_attribute("id") != cover_btn.get_attribute("id")


def test_card_scoping_not_confused_by_resume_substring_in_cover_letter_filename(page):
    # "Filenames containing the word 'resume' must not confuse section
    # detection" -- a Cover Letter filename containing "resume" as a
    # substring must not cause the Cover Letter card to be misidentified
    # as the Resume card.
    fixture = f"""
    <html><body>
    <div class="{_REAL_FILE_CARD_CLASS}">
      <p>Resume *</p>
      <p>resume.pdf</p>
      <p>Uploaded to profile on 8/21/2026</p>
      <button aria-label="File options" id="btn-a">A</button>
    </div>
    <div class="{_REAL_FILE_CARD_CLASS}">
      <p>Cover letter</p>
      <p>Optional</p>
      <p>my_resume_backup_cover.pdf</p>
      <p>New file</p>
      <button aria-label="File options" id="btn-b">B</button>
    </div>
    </body></html>
    """
    page.set_content(fixture)
    resume_btn = _find_resume_file_options_button(page)
    cover_btn = _find_cover_letter_file_options_button(page)
    assert resume_btn.get_attribute("id") == "btn-a"
    assert cover_btn.get_attribute("id") == "btn-b"


def test_card_scoping_ignores_unrelated_promotional_and_sidebar_cards(page):
    # Already covered implicitly by the main fixture (which includes both
    # an unrelated promo card and an unrelated sidebar card with its own
    # "File options" button), asserted explicitly here.
    page.set_content(_WIZARD_WITH_FILE_OPTIONS_FIXTURE)
    resume_btn = _find_resume_file_options_button(page)
    cover_btn = _find_cover_letter_file_options_button(page)
    assert resume_btn.get_attribute("id") not in (None, "btn-unrelated")
    assert cover_btn.get_attribute("id") not in (None, "btn-unrelated")
    assert resume_btn.get_attribute("id") == "btn-a"
    assert cover_btn.get_attribute("id") == "btn-b"


def test_upload_targets_resume_input_not_cover_letter_input(page):
    # Real live finding (Phase 4C.1 live upload, 2026-08-21): the actual
    # wizard has TWO file inputs -- Resume and Cover Letter -- and the
    # old input[type='file'].first blindly grabbed whichever came first
    # in DOM order, which turned out to be the Cover Letter one. The
    # V1 test resume landed as an optional cover letter, not the resume.
    # Fixed to scope strictly by DOM order relative to the "Cover letter"
    # landmark: only an input that appears BEFORE it counts as Resume.
    # Deliberately adversarial: the Cover Letter input is FIRST in raw
    # DOM/tag order (matching what actually happened live -- the
    # already-filled Resume slot didn't expose a plain reachable input
    # the same simple way, so a naive input[type='file'].first landed on
    # Cover Letter). The fix must key off position relative to the
    # "Cover letter" TEXT landmark, not raw input tag order.
    page.set_content(
        """
        <html><body>
        <input type="file" id="cover-letter-input">
        <p>Resume *</p>
        <input type="file" id="resume-input">
        <p>Cover letter</p>
        <p>Optional</p>
        </body></html>
        """
    )
    result = upload_resume(page, resume_path=DUMMY_RESUME)
    # Only the resume input should ever receive files -- verified by
    # checking which input actually has a file attached afterward.
    resume_has_file = page.locator("#resume-input").evaluate("e => e.files.length") > 0
    cover_letter_has_file = page.locator("#cover-letter-input").evaluate("e => e.files.length") > 0
    assert resume_has_file is True
    assert cover_letter_has_file is False


def test_upload_success_verification_scoped_to_resume_section_only(page):
    # Exact false-positive regression from the live incident: filename
    # evidence appears, but under Cover Letter, not Resume. Must not
    # count as resume-upload success.
    page.set_content(
        """
        <html><body>
        <p>Resume *</p>
        <p>resume.pdf</p>
        <p>Uploaded to profile on 8/21/2026</p>
        <p>Cover letter</p>
        <p>Optional</p>
        <p>test_resume.pdf</p>
        <p>New file</p>
        <input type="file">
        </body></html>
        """
    )
    assert _upload_succeeded(page, "test_resume.pdf") is False


def test_upload_success_verification_true_with_duplicate_cover_letter_text(page):
    # Real live finding (Phase 4C.1 corrected retry, 2026-08-21): the real
    # wizard page has TWO elements matching "Cover letter" text -- a
    # stepper/nav step label plus the field's own label -- and the nav one
    # sits earlier in raw DOM order than the actual Resume field content.
    # A naive "evidence must come before the first Cover letter match"
    # check collapses to false for every genuine Resume success. Card
    # scoping must not be fooled by this.
    page.set_content(
        f"""
        <html><body>
        <nav><p>Resume</p><p>Cover letter</p><p>Screening questions</p></nav>
        <div class="{_REAL_FILE_CARD_CLASS}">
          <p>Resume *</p>
          <p>test_resume.pdf</p>
          <p>New file</p>
        </div>
        <div class="{_REAL_FILE_CARD_CLASS}">
          <p>Cover letter</p>
          <p>Optional</p>
        </div>
        </body></html>
        """
    )
    assert _upload_succeeded(page, "test_resume.pdf") is True


def test_upload_success_verification_true_when_filename_under_resume(page):
    # Sanity counterpart: the SAME filename, but genuinely under Resume
    # (before the Cover Letter landmark), must count as success.
    page.set_content(
        """
        <html><body>
        <p>Resume *</p>
        <p>test_resume.pdf</p>
        <p>New file</p>
        <p>Cover letter</p>
        <p>Optional</p>
        </body></html>
        """
    )
    assert _upload_succeeded(page, "test_resume.pdf") is True


def test_upload_skips_change_click_when_file_input_already_usable(page):
    # Real live finding (Phase 4B.1/4C.1 live upload attempt, 2026-08-21):
    # the only "Change" text match on the real wizard page was an
    # unrelated, hidden "Go visible! Get noticed by recruiters..."
    # promotional card that happens to contain the word "change" in its
    # copy -- not the resume control at all. Clicking it hung waiting for
    # visibility and crashed the whole upload. The real file input is
    # already directly visible and usable with no reveal click needed, so
    # the fix must try the file input FIRST and only fall back to a
    # click if it isn't found -- never blindly click a "Change" match
    # before checking whether it's even needed.
    page.set_content(
        """
        <html><body>
        <div class="promo-card" style="display:none">
        Go visible! Get noticed by recruiters, make the most of your job
        search, and unlock more dashboard widgets when you change your
        profile to visible.
        </div>
        <p>resume.pdf</p>
        <p>Uploaded to profile on 8/21/2026</p>
        <input type="file">
        </body></html>
        """
    )
    result = upload_resume(page, resume_path=DUMMY_RESUME)
    assert result.existing_resume_detected is True
    # Must not hang/crash on the irrelevant hidden "Change" match -- the
    # file input was directly usable, so the upload should proceed.
    assert result.uploaded is False  # no success-evidence fixture here, but no crash
    assert result.reason.startswith("UPLOAD_FAILED")


def test_upload_clicks_replace_when_existing_resume_detected(page):
    page.set_content(
        """
        <html><body>
        <div class="current-resume-card">old_resume.pdf</div>
        <button id="replace-btn">Replace</button>
        <input type="file" id="fsp-fileUpload" style="display:none">
        <script>
        document.getElementById('replace-btn').addEventListener('click', () => {
            document.getElementById('fsp-fileUpload').style.display = 'block';
            window.__replaceClicked = true;
        });
        </script>
        </body></html>
        """
    )
    upload_resume(page, resume_path=DUMMY_RESUME)
    assert page.evaluate("window.__replaceClicked === true")


def test_upload_failed_when_no_success_evidence(page):
    page.set_content('<html><body><input type="file" id="fsp-fileUpload"></body></html>')
    result = upload_resume(page, resume_path=DUMMY_RESUME)
    assert result.uploaded is False
    assert result.reason.startswith("UPLOAD_FAILED")


def test_upload_succeeds_with_filename_evidence(page):
    page.set_content(
        """
        <html><body>
        <input type="file" id="fsp-fileUpload">
        <script>
        document.getElementById('fsp-fileUpload').addEventListener('change', () => {
            const el = document.createElement('div');
            el.textContent = document.getElementById('fsp-fileUpload').files[0].name;
            document.body.appendChild(el);
        });
        </script>
        </body></html>
        """
    )
    result = upload_resume(page, resume_path=DUMMY_RESUME)
    assert result.uploaded is True
    assert result.reason == "uploaded successfully"


def test_upload_no_file_input_found_returns_upload_failed(page):
    page.set_content("<html><body><p>No file input here.</p></body></html>")
    result = upload_resume(page, resume_path=DUMMY_RESUME)
    assert result.uploaded is False


# ── corrected Replace-via-menu flow (Phase 4C.1 corrected retry) ─────────
#
# Real live finding, 2026-08-21: once a resume is already on file, the
# real wizard exposes NO reachable <input type=file> at all -- only a
# "File options" button with a live aria-controls relationship to the menu
# it opens (React Aria's standard trigger/popup pattern). These fixtures
# deliberately have no <input type=file> anywhere in the initial markup
# (matching that live reality) -- each variant's Replace item creates one
# dynamically, exactly as the real page's own JS must be doing.


def _menu_wizard_fixture(resume_replace_behavior: str) -> str:
    """Two file cards, each with its own File-options button wired to its
    own menu via aria-controls, each menu containing Replace and Delete
    menuitems. `resume_replace_behavior` controls what clicking Resume's
    Replace item does: "chooser" fires a real native file chooser (a
    freshly created, momentarily-clicked hidden input); "reveal_input"
    instead creates an already-visible Resume-scoped input with no chooser
    event; "noop" does nothing at all."""
    if resume_replace_behavior == "chooser":
        resume_replace_onclick = (
            "(function(){var inp=document.createElement('input');inp.type='file';"
            "inp.style.display='none';document.getElementById('resume-card').appendChild(inp);"
            "inp.addEventListener('change',function(e){"
            "document.getElementById('resume-filename').textContent=e.target.files[0].name;});"
            "inp.click();})()"
        )
    elif resume_replace_behavior == "reveal_input":
        resume_replace_onclick = (
            "(function(){var inp=document.createElement('input');inp.type='file';"
            "document.getElementById('resume-card').appendChild(inp);"
            "inp.addEventListener('change',function(e){"
            "document.getElementById('resume-filename').textContent=e.target.files[0].name;});})()"
        )
    else:
        resume_replace_onclick = ""

    return f"""
<html><body>
<div class="{_REAL_FILE_CARD_CLASS}" id="resume-card">
  <p>Resume *</p>
  <p id="resume-filename">resume.pdf</p>
  <p>Uploaded to profile on 8/21/2026</p>
  <button aria-label="File options" id="btn-resume" aria-controls="menu-resume" aria-haspopup="true" aria-expanded="false"
    onclick="document.getElementById('menu-resume').hidden=false; this.setAttribute('aria-expanded','true');">Resume options</button>
</div>
<div id="menu-resume" role="menu" hidden>
  <div role="menuitem" onclick="{resume_replace_onclick}">Replace</div>
  <div role="menuitem" onclick="window.__deleteClickedResume=true;">Delete</div>
</div>
<div class="{_REAL_FILE_CARD_CLASS}" id="cover-card">
  <p>Cover letter</p>
  <p id="cover-filename">Optional</p>
  <button aria-label="File options" id="btn-cover" aria-controls="menu-cover" aria-haspopup="true" aria-expanded="false"
    onclick="document.getElementById('menu-cover').hidden=false; this.setAttribute('aria-expanded','true');">Cover options</button>
</div>
<div id="menu-cover" role="menu" hidden>
  <div role="menuitem" onclick="document.getElementById('cover-filename').textContent='SHOULD_NOT_BE_USED.pdf';">Replace</div>
  <div role="menuitem" onclick="window.__deleteClickedCover=true;">Delete</div>
</div>
</body></html>
"""


def test_resume_file_options_menu_opens_via_aria_controls(page):
    page.set_content(_menu_wizard_fixture("chooser"))
    button = _find_resume_file_options_button(page)
    menu = _open_file_options_menu(page, button)
    assert menu is not None
    assert menu.get_attribute("id") == "menu-resume"


def test_open_menu_skips_click_when_already_open(page):
    # Real live finding, 2026-08-21: a prior read-only observation had
    # already opened the menu (aria-expanded="true"); clicking an
    # already-open trigger toggles it shut and hangs waiting for a stable
    # click target. If aria-controls already resolves to a visible menu,
    # it must be returned directly -- the trigger button must not be
    # clicked again.
    html = _menu_wizard_fixture("chooser").replace(
        'aria-expanded="false"\n    onclick="document.getElementById(\'menu-resume\').hidden=false; '
        "this.setAttribute('aria-expanded','true');\">Resume options</button>",
        "aria-expanded=\"true\" onclick=\"window.__buttonClickedAgain=true;\">Resume options</button>",
    ).replace('<div id="menu-resume" role="menu" hidden>', '<div id="menu-resume" role="menu">')
    page.set_content(html)
    button = _find_resume_file_options_button(page)
    menu = _open_file_options_menu(page, button)
    assert menu is not None
    assert menu.get_attribute("id") == "menu-resume"
    assert page.evaluate("window.__buttonClickedAgain") is not True


def test_resume_menu_contains_replace_and_delete_items(page):
    page.set_content(_menu_wizard_fixture("chooser"))
    menu = _open_file_options_menu(page, _find_resume_file_options_button(page))
    assert menu.get_by_role("menuitem", name="Replace", exact=True).count() == 1
    assert menu.get_by_role("menuitem", name="Delete", exact=True).count() == 1


def test_replace_click_handles_native_file_chooser(page):
    page.set_content(_menu_wizard_fixture("chooser"))
    result = _replace_resume_file(page, DUMMY_RESUME, file_chooser_timeout_ms=1000)
    assert result.uploaded is True
    assert page.evaluate("window.__deleteClickedResume") is not True
    assert page.locator("#resume-filename").inner_text() == DUMMY_RESUME.name


def test_replace_click_handles_fallback_file_input_reveal(page):
    page.set_content(_menu_wizard_fixture("reveal_input"))
    result = _replace_resume_file(page, DUMMY_RESUME, file_chooser_timeout_ms=300)
    assert result.uploaded is True
    assert page.locator("#resume-filename").inner_text() == DUMMY_RESUME.name


def test_replace_click_stops_safely_when_no_file_mechanism_appears(page):
    page.set_content(_menu_wizard_fixture("noop"))
    result = _replace_resume_file(page, DUMMY_RESUME, file_chooser_timeout_ms=300)
    assert result.uploaded is False
    assert result.reason == "UPLOAD_FAILED: no file chooser or resume-scoped input appeared after Replace"


def test_replace_selection_ignores_page_wide_replace_text(page):
    html = _menu_wizard_fixture("chooser").replace(
        "<body>",
        '<body><button onclick="window.__unrelatedReplaceClicked=true;">Replace profile photo</button>',
    )
    page.set_content(html)
    result = _replace_resume_file(page, DUMMY_RESUME, file_chooser_timeout_ms=1000)
    assert result.uploaded is True
    assert page.evaluate("window.__unrelatedReplaceClicked") is not True


def test_resume_replace_menu_never_uses_cover_letter_menu(page):
    page.set_content(_menu_wizard_fixture("chooser"))
    result = _replace_resume_file(page, DUMMY_RESUME, file_chooser_timeout_ms=1000)
    assert result.uploaded is True
    assert page.locator("#cover-filename").inner_text() == "Optional"
    assert page.evaluate("window.__deleteClickedCover") is not True


def test_replace_resume_file_end_to_end_success_scoped_to_resume_card(page):
    page.set_content(_menu_wizard_fixture("chooser"))
    result = upload_resume(page, resume_path=DUMMY_RESUME)
    assert result.uploaded is True
    assert result.existing_resume_detected is True
    assert page.locator("#resume-filename").inner_text() == DUMMY_RESUME.name
    assert page.locator("#cover-filename").inner_text() == "Optional"


def test_replace_resume_file_rejects_success_evidence_under_cover_letter(page):
    # Simulates the exact class of bug this whole feature exists to catch:
    # even if a Replace click somehow wrote evidence under Cover Letter
    # instead of Resume, success must not be reported.
    html = _menu_wizard_fixture("chooser").replace(
        "document.getElementById('resume-filename')", "document.getElementById('cover-filename')"
    )
    page.set_content(html)
    result = _replace_resume_file(page, DUMMY_RESUME, file_chooser_timeout_ms=1000)
    assert result.uploaded is False
    assert result.reason == "UPLOAD_FAILED: no success evidence in Resume card after Replace"


def test_resume_menu_open_fails_safely_when_aria_controls_target_is_ambiguous(page):
    # An "unexpected second menu" sharing the id the button claims to
    # control -- must refuse deterministically rather than guess which one
    # is real.
    html = _menu_wizard_fixture("chooser").replace(
        '<div id="menu-cover" role="menu" hidden>',
        '<div id="menu-resume" role="menu" hidden><div role="menuitem">Decoy Replace</div></div>'
        '<div id="menu-cover" role="menu" hidden>',
    )
    page.set_content(html)
    menu = _open_file_options_menu(page, _find_resume_file_options_button(page))
    assert menu is None
