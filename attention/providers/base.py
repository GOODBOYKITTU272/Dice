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

    def send_apply_ack(self, application_id: str) -> str:
        """Sent once, only after Apply actually changed application
        state (never on a duplicate/replayed Apply)."""
        ...

    def send_skip_ack(self, application_id: str) -> str:
        """Sent once, only after Skip actually changed application
        state (never on a duplicate/replayed Skip)."""
        ...

    def send_answer_accepted(self, application_id: str, question_id: str) -> str:
        """"Got it" -- sent after a Confirm that resolves a question but
        leaves more open ones. Legitimately repeats once per confirmed
        question on the same application (like MISSING_QUESTION)."""
        ...

    def send_ready_to_submit(self, application_id: str) -> str:
        """Sent once, only when the final open question is confirmed AND
        the associated run's policy is AUTHORIZED_AUTONOMOUS."""
        ...

    def send_reconnect_required(self, application_id: str) -> str:
        """Phase 8D. Sent once per application+channel when a genuine
        (post-retry) AUTH_REQUIRED interrupts an already-authorized
        application -- never for a job that was merely held pre-offer
        (that one silently never gets an Apply/Skip card at all, no
        message needed). Never mentions cookies/Browserless/Railway."""
        ...

    def send_reconnect_success(self, application: dict, job: dict) -> str:
        """Phase 8D. Sent once reconnect_dice() positively re-verifies
        Dice ACTIVE and resumes this specific interrupted application --
        never a new Apply card, never a second authorization."""
        ...

    def parse_inbound(self, raw_event: dict) -> NormalizedEvent:
        """Translates one provider-native inbound event (a Telegram
        update, an iMessage row) into a NormalizedEvent. Never touches
        Supabase, never decides what happens next -- that's entirely
        attention.service's job."""
        ...
