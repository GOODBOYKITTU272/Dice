"""Phase 4C: resume detection and upload only. No question answering, no
Next/Review/Submit -- those modules don't exist anywhere in this repo.

Uses one fixed V1 test resume, not the (not-yet-built, Phase 4E)
Candidate Adapter. The actual resume file lives outside git entirely --
see resume_path_from_env() -- and must be verified gitignored before any
real file is ever placed there.
"""
from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import Page

from dice_browser.models import ResumeUploadResult

RESUME_PATH_ENV_VAR = "DICEPILOT_TEST_RESUME_PATH"
DEFAULT_RESUME_PATH = Path(__file__).resolve().parent.parent / ".runtime" / "resume" / "test_resume.pdf"


def resume_path_from_env() -> Path:
    configured = os.environ.get(RESUME_PATH_ENV_VAR)
    return Path(configured) if configured else DEFAULT_RESUME_PATH


def detect_existing_resume(page: Page) -> bool | None:
    """TRUE / FALSE / UNKNOWN (None) -- conservative. Exact live selector
    is unverified (Phase 4B.1 auth prerequisite deferred, see STATE.md);
    returns None whenever the page doesn't show a clear signal either
    way, rather than guessing."""
    has_replace = page.get_by_text("Replace", exact=False).count() > 0
    has_marker = page.locator("[data-testid*='resume'], [class*='resume-card'], [class*='current-resume']").count() > 0
    if has_replace or has_marker:
        return True

    has_upload_prompt = page.get_by_text("Upload", exact=False).count() > 0
    if has_upload_prompt:
        return False

    return None


def upload_resume(page: Page, resume_path: Path | None = None) -> ResumeUploadResult:
    path = resume_path if resume_path is not None else resume_path_from_env()
    if not path.exists():
        return ResumeUploadResult(uploaded=False, existing_resume_detected=None, reason="RESUME_FILE_MISSING")

    existing = detect_existing_resume(page)

    if existing:
        replace_control = page.get_by_text("Replace", exact=False).first
        if replace_control.count() > 0:
            replace_control.click()

    file_input = page.locator("input#fsp-fileUpload")
    if file_input.count() == 0:
        file_input = page.locator("input[type='file']").first
    if file_input.count() == 0:
        return ResumeUploadResult(
            uploaded=False, existing_resume_detected=existing, reason="UPLOAD_FAILED: no file input found"
        )

    file_input.set_input_files(str(path))
    try:
        page.wait_for_timeout(1500)
    except Exception:
        pass

    if _upload_succeeded(page, path.name):
        return ResumeUploadResult(uploaded=True, existing_resume_detected=existing, reason="uploaded successfully")

    return ResumeUploadResult(
        uploaded=False, existing_resume_detected=existing, reason="UPLOAD_FAILED: no success evidence"
    )


def _upload_succeeded(page: Page, filename: str) -> bool:
    # Exact production locator needs live-DOM confirmation once the
    # Phase 4B.1 auth prerequisite is completed (see STATE.md). This
    # conservative fallback checks for the uploaded filename appearing
    # anywhere, or a generic upload-complete signal -- never returns True
    # without a concrete signal.
    if page.get_by_text(filename, exact=False).count() > 0:
        return True
    return page.locator("[class*='upload-complete'], [data-testid*='upload-success']").count() > 0
