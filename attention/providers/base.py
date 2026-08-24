"""Phase 7.4: the one interface both Telegram and iMessage implement.
attention.service depends only on this -- never on a concrete provider
class -- so adding a third channel later never touches business logic."""
from __future__ import annotations

from typing import Protocol

from attention.models import NormalizedEvent


class AttentionProvider(Protocol):
    channel: str

    def send_job_offer(self, application: dict, job: dict) -> str:
        """Sends the "Found a match... [Apply] [Skip]" message. Returns
        the provider's own native message id (for the audit log only --
        never used for correlation, that's external_message_id on the
        NEXT inbound reply)."""
        ...

    def send_missing_question(self, application_id: str, question) -> str:
        """Sends exactly one question (never a batch)."""
        ...

    def send_answer_confirmation(self, application_id: str, question_id: str, raw_answer: str) -> str:
        """Sends "You answered: X [Confirm] [Edit]"."""
        ...

    def send_submission_success(self, application: dict, job: dict) -> str:
        ...

    def send_submission_failure(self, application: dict, job: dict, reason: str) -> str:
        ...

    def parse_inbound(self, raw_event: dict) -> NormalizedEvent:
        """Translates one provider-native inbound event (a Telegram
        update, an iMessage row) into a NormalizedEvent. Never touches
        Supabase, never decides what happens next -- that's entirely
        attention.service's job."""
        ...
