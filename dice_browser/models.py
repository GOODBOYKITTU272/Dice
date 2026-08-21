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
    """Result of dice_browser.questions.extract_questions(). Two live
    branches verified 2026-08-21: NO_QUESTIONS_PRESENT (Data Engineer @
    Stefanini and others -- a Review screen with zero question controls)
    and QUESTIONS_PRESENT (Java Developer @ Yashnee Tech Solutions -- a
    dedicated "Application Questions" step with real radiogroup/textarea
    controls). UNKNOWN_SCREEN means the page isn't a recognized Review or
    Questions screen at all -- never treated as "no questions" by
    omission."""

    NO_QUESTIONS_PRESENT = "NO_QUESTIONS_PRESENT"
    QUESTIONS_PRESENT = "QUESTIONS_PRESENT"
    UNKNOWN_SCREEN = "UNKNOWN_SCREEN"


class FieldType(str, Enum):
    """Deliberately minimal -- SELECT/DATE/CHECKBOX/MULTI_SELECT/etc. are
    not added until a real live question of that shape has actually been
    observed. RADIO and TEXTAREA are live-verified (Java Developer @
    Yashnee Tech Solutions, 2026-08-21, job 3f63223a-1dc9-4af9-914c-4ed01e625d44).
    Anything else found is UNSUPPORTED by construction, never guessed
    into one of these two."""

    RADIO = "RADIO"
    TEXTAREA = "TEXTAREA"
    UNSUPPORTED = "UNSUPPORTED"


class RequiredState(str, Enum):
    """Tri-state, not bool. Live evidence (2026-08-21, same job as above):
    neither observed question exposes a `required`/`aria-required`
    attribute or a visible required marker -- Dice apparently enforces
    required-ness only via click-time validation, which is unobservable
    from static DOM inspection alone. UNKNOWN must never be silently
    treated as OPTIONAL."""

    REQUIRED = "REQUIRED"
    OPTIONAL = "OPTIONAL"
    UNKNOWN = "UNKNOWN"


class QuestionStatus(str, Enum):
    NEEDS_INPUT = "NEEDS_INPUT"
    UNSUPPORTED = "UNSUPPORTED"
    ALREADY_ANSWERED = "ALREADY_ANSWERED"


@dataclass(frozen=True)
class QuestionField:
    """One question control found on a Review/question screen.

    question_id prefers the DOM `name` attribute (live-verified to be a
    stable, UUID-shaped Dice question identifier) -- never a React-Aria
    generated `id` (live-verified to be per-render, not durable). Falls
    back to a hash of the resolved prompt (still content-stable across
    reloads), and only as a last resort an explicitly-unstable positional
    placeholder -- see dice_browser.questions._question_id()."""

    question_id: str
    prompt: str | None
    field_type: FieldType
    required_state: RequiredState
    options: tuple[str, ...] | None
    current_value: str | None
    helper: str | None
    status: QuestionStatus


@dataclass(frozen=True)
class QuestionExtractionResult:
    status: QuestionExtractionStatus
    questions: tuple[QuestionField, ...]
