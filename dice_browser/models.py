"""Phase 4B: browser/session state models.

Foundation states only. No uploading/answering/reviewing/submitting states
belong here yet — those arrive with the phases that build them.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class BrowserState(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    STARTING = "STARTING"
    ACTIVE = "ACTIVE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    NEEDS_INPUT = "NEEDS_INPUT"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


class ChallengeType(str, Enum):
    OTP = "OTP"
    CAPTCHA = "CAPTCHA"
    SECURITY_CHECK = "SECURITY_CHECK"
    UNKNOWN = "UNKNOWN"


@dataclass
class NavigationResult:
    """What opening one already-discovered job URL found. already_applied
    is None (not False) when we can't tell — e.g. not authenticated, so
    Dice has no per-account "applied" state to show us at all; None means
    unknown, never a guessed False."""

    canonical_url: str
    page_title: str
    browser_state: BrowserState
    authenticated: bool
    already_applied: bool | None
    easy_apply_visible: bool | None
    challenge_type: ChallengeType | None
    evidence: str


@dataclass
class EasyApplyOpenResult:
    """Result of dice_browser.easy_apply.open_easy_apply() -- the one
    function in this codebase permitted to navigate into
    /job-applications/..., gated behind three preconditions. reason is
    one of: AUTH_REQUIRED, ALREADY_APPLIED, UNKNOWN_APPLIED_STATE,
    NOT_EASY_APPLY, CLICK_FAILED (optionally with a detail suffix), or
    "OPENED" on success."""

    opened: bool
    current_url: str
    page_title: str
    reason: str


@dataclass
class ResumeUploadResult:
    """Result of dice_browser.resume.upload_resume(). reason is one of:
    RESUME_FILE_MISSING, UPLOAD_FAILED (with a detail suffix), or
    "uploaded successfully" on success. existing_resume_detected is
    True/False/None (unknown) -- see detect_existing_resume()."""

    uploaded: bool
    existing_resume_detected: bool | None
    reason: str
