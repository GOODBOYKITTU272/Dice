"""Phase 7.4: the five domain actions and the shape a provider normalizes
its own transport into. Deliberately minimal -- no channel-specific
fields here; anything a provider can't express (e.g. iMessage has no
structured callback_data) is just left None and resolved by
attention.service against V1's single configured candidate."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AttentionAction(str, Enum):
    APPLY = "APPLY"
    SKIP = "SKIP"
    CONFIRM = "CONFIRM"
    EDIT = "EDIT"
    ANSWER = "ANSWER"


class MessageType(str, Enum):
    JOB_OFFER = "JOB_OFFER"
    MISSING_QUESTION = "MISSING_QUESTION"
    ANSWER_CONFIRMATION = "ANSWER_CONFIRMATION"
    SUBMISSION_SUCCESS = "SUBMISSION_SUCCESS"
    SUBMISSION_FAILURE = "SUBMISSION_FAILURE"
    APPLY_ACK = "APPLY_ACK"
    SKIP_ACK = "SKIP_ACK"
    ANSWER_ACCEPTED = "ANSWER_ACCEPTED"
    READY_TO_SUBMIT = "READY_TO_SUBMIT"


@dataclass(frozen=True)
class NormalizedEvent:
    """What a provider's parse_inbound() returns. external_message_id is
    the provider's own native id (Telegram update_id, iMessage row id)
    -- the sole key inbound idempotency is keyed on. application_id/
    question_id are populated only when the transport carries them
    explicitly (Telegram inline-button callback_data does; iMessage's
    plain-text replies don't) -- attention.service resolves either to
    "the current pending one for the single configured candidate" when
    left None, rather than each provider reimplementing that lookup."""

    channel: str
    external_message_id: str
    action: AttentionAction
    application_id: str | None = None
    question_id: str | None = None
    raw_text: str | None = None
