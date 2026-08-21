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
    """TRUE / FALSE / UNKNOWN (None) -- conservative; returns None
    whenever the page doesn't show a clear signal either way, rather than
    guessing.

    Verified live (Phase 4B.1 CDP-attach closure, 2026-08-21) against the
    real Dice apply wizard's resume step: the control is labeled "Change",
    never "Replace" (that original guess was stale). "Uploaded to profile"
    is the real confirmation phrase shown next to an already-on-file
    resume. A bare "Upload" text check is a false-negative trap -- the
    same wizard step also shows an unrelated "Upload your cover letter"
    prompt for the separate, optional cover-letter field; only a
    resume-specific upload prompt counts as a negative signal."""
    has_change = page.get_by_text("Change", exact=False).count() > 0
    has_uploaded_marker = page.get_by_text("Uploaded to profile", exact=False).count() > 0
    has_marker = page.locator("[data-testid*='resume'], [class*='resume-card'], [class*='current-resume']").count() > 0
    has_replace = page.get_by_text("Replace", exact=False).count() > 0  # fallback for a different Dice UI variant
    if has_change or has_uploaded_marker or has_marker or has_replace:
        return True

    has_resume_upload_prompt = page.get_by_text("Upload your resume", exact=False).count() > 0
    if has_resume_upload_prompt:
        return False

    return None


def upload_resume(page: Page, resume_path: Path | None = None) -> ResumeUploadResult:
    path = resume_path if resume_path is not None else resume_path_from_env()
    if not path.exists():
        return ResumeUploadResult(uploaded=False, existing_resume_detected=None, reason="RESUME_FILE_MISSING")

    existing = detect_existing_resume(page)

    if existing:
        # "Change" verified live (Phase 4B.1); "Replace" kept as a
        # fallback in case a different Dice UI variant uses that wording.
        change_control = page.get_by_text("Change", exact=False).first
        if change_control.count() > 0:
            change_control.click()
        else:
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
