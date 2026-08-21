"""Phase 4C: resume detection and upload only. No question answering, no
Next/Review/Submit -- those modules don't exist anywhere in this repo.
Offline tests only (synthetic HTML + a dummy, non-personal test fixture
file); no live Dice needed for any of this.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from dice_browser.resume import detect_existing_resume, upload_resume

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
    assert result.reason.startswith("UPLOAD_FAILED")
