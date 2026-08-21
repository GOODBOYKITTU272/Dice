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


class QuestionExtractionStatus(str, Enum):
    """Result of dice_browser.questions.extract_questions(). NO_QUESTIONS_PRESENT
    is the one real live-verified branch so far (Data Engineer @ Stefanini,
    2026-08-21): a Review screen with zero supported question controls.
    QUESTIONS_PRESENT means candidate controls were found but not yet
    classified -- no live evidence of real question shapes exists yet, so
    this phase deliberately stops at "something is here" rather than
    guessing what it is. UNKNOWN_SCREEN means the page isn't a recognized
    Review screen at all -- never treated as "no questions" by omission."""

    NO_QUESTIONS_PRESENT = "NO_QUESTIONS_PRESENT"
    QUESTIONS_PRESENT = "QUESTIONS_PRESENT"
    UNKNOWN_SCREEN = "UNKNOWN_SCREEN"


class FieldType(str, Enum):
    """Deliberately minimal -- TEXT/RADIO/SELECT/etc. are not added until
    a real live question of that shape has actually been observed. Every
    candidate control found so far is UNSUPPORTED by construction."""

    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class QuestionField:
    """One candidate question control found on a Review/question screen.
    No live-verified question exists yet, so this intentionally carries
    only structural facts (id, type, visibility) -- no prompt-extraction
    or answer-classification logic exists until a real question shape is
    observed to build it against."""

    question_id: str
    field_type: FieldType


@dataclass(frozen=True)
class QuestionExtractionResult:
    status: QuestionExtractionStatus
    questions: tuple[QuestionField, ...]
