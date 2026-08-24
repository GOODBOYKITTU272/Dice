"""Phase 7.4: iMessage adapter -- secondary V1 channel. No native
clickable buttons (unlike Telegram) -- every prompt asks for a plain-text
reply ("Reply: APPLY or SKIP"), normalized to the exact same five domain
actions Telegram uses. No business logic here (see attention/__init__.py).

Two OS-level primitives, both isolated behind small functions so
parse_inbound() (the actual normalization logic) stays unit-testable
with synthetic dicts and never needs a real Messages.app/chat.db:
  - sending: `osascript` driving Messages.app (real send, real side
    effect -- requires macOS Automation permission for Messages.app,
    granted by the user, once).
  - receiving: reading ~/Library/Messages/chat.db (the local Mac's own
    Messages database) for new rows from the configured contact --
    read-only, never writes to chat.db.

IMESSAGE_CONTACT (phone number or email, whichever the candidate's
iMessage account uses) is read from the environment only -- never
hardcoded, never committed.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from attention.formatting import job_metadata_line
from attention.models import AttentionAction, NormalizedEvent

_CONTACT_ENV_VAR = "IMESSAGE_CONTACT"
_CHAT_DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"
_OSASCRIPT_TIMEOUT_SECONDS = 15


class IMessageNotConfiguredError(RuntimeError):
    pass


def _contact() -> str:
    contact = os.environ.get(_CONTACT_ENV_VAR)
    if not contact:
        raise IMessageNotConfiguredError(f"{_CONTACT_ENV_VAR} is not configured")
    return contact


def _send_via_messages_app(text: str, contact: str) -> None:
    # AppleScript string literals only support \" and \\ escaping -- no
    # other characters in `text` need escaping for the "send ... to
    # buddy" form, since the whole string is passed as one atomic
    # osascript -e argument, never shell-interpolated.
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    script = f'tell application "Messages" to send "{escaped}" to buddy "{contact}" of (service 1 whose service type is iMessage)'
    subprocess.run(["osascript", "-e", script], check=True, timeout=_OSASCRIPT_TIMEOUT_SECONDS, capture_output=True)


def read_new_messages(contact: str, since_rowid: int = 0) -> list[dict[str, Any]]:
    """Read-only query against the local chat.db for incoming (not sent
    by this Mac) text messages from `contact` newer than since_rowid.
    Returns plain dicts -- never a raw sqlite3.Row -- so parse_inbound()
    doesn't need a real database to be tested."""
    if not _CHAT_DB_PATH.exists():
        return []
    conn = sqlite3.connect(f"file:{_CHAT_DB_PATH}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select message.ROWID as rowid, message.text as text
            from message
            join handle on message.handle_id = handle.ROWID
            where handle.id = ?
              and message.is_from_me = 0
              and message.ROWID > ?
            order by message.ROWID
            """,
            (contact, since_rowid),
        ).fetchall()
        return [{"rowid": row["rowid"], "text": row["text"]} for row in rows]
    finally:
        conn.close()


class IMessageProvider:
    channel = "IMESSAGE"

    def __init__(self, contact: str | None = None):
        """contact, when given, overrides IMESSAGE_CONTACT for every send
        from this instance -- same reasoning as TelegramProvider's
        chat_id override: lets attention.service send to a candidate's
        real bound contact (candidate_attention_channels) rather than
        the env var being the permanent identity source."""
        self._contact_override = contact

    def _send(self, text: str) -> str:
        contact = self._contact_override or _contact()
        _send_via_messages_app(text, contact)
        # chat.db assigns the ROWID asynchronously (Messages.app owns the
        # write); there is no synchronous "message id" osascript hands
        # back, so the audit log records the send attempt without one --
        # matches send_missing_question's own free-text path, and never
        # affects inbound dedupe (that's keyed on INBOUND rowids only).
        return ""

    def send_job_offer(self, application: dict, job: dict) -> str:
        meta = job_metadata_line(job)
        meta_suffix = f"\n{meta}" if meta else ""
        text = (
            f"Found a match:\n\n{job['title']} — {job.get('company_name') or 'Unknown Company'}{meta_suffix}\n\n"
            "Reply:\nAPPLY\nor\nSKIP"
        )
        return self._send(text)

    def send_apply_ack(self, application_id: str) -> str:
        return self._send("Checking the application...")

    def send_skip_ack(self, application_id: str) -> str:
        return self._send("Skipped 👍\n\nI won't apply to this job.")

    def send_answer_accepted(self, application_id: str, question_id: str) -> str:
        return self._send("Got it ✅")

    def send_ready_to_submit(self, application_id: str) -> str:
        return self._send("✅ I now have everything needed.\n\nSubmitting your application now...")

    def send_missing_question(self, application_id: str, question: dict) -> str:
        prompt = question.get("question_text") or "I need one answer before I can continue."
        options = (question.get("options") or {}).get("choices")
        reply_hint = "\n".join(options) if options else "your answer"
        text = f"{prompt}\n\nReply:\n{reply_hint}"
        return self._send(text)

    def send_answer_confirmation(self, application_id: str, question_id: str, raw_answer: str) -> str:
        text = f"You answered:\n{raw_answer}\n\nReply:\nCONFIRM\nor\nEDIT"
        return self._send(text)

    def send_submission_success(self, application: dict, job: dict) -> str:
        meta = job_metadata_line(job)
        meta_suffix = f"\n{meta}" if meta else ""
        text = f"Applied successfully ✅\n\n{job['title']}{meta_suffix}\n\nYour application was submitted."
        return self._send(text)

    def send_submission_failure(self, application: dict, job: dict, reason: str) -> str:
        text = f"Couldn't complete this application.\n\n{job['title']}\n{job.get('company_name') or ''}\n\n{reason}".rstrip()
        return self._send(text)

    def parse_inbound(self, raw_event: dict) -> NormalizedEvent:
        """raw_event is one row already read via read_new_messages() --
        {"rowid": int, "text": str}. external_message_id is chat.db's own
        ROWID (a real, monotonic, per-database-unique primary key --
        exactly what inbound dedupe needs), never re-derived or guessed."""
        external_message_id = str(raw_event["rowid"])
        text = (raw_event.get("text") or "").strip()
        upper = text.upper()
        if upper in ("APPLY", "SKIP", "CONFIRM", "EDIT"):
            return NormalizedEvent(channel=self.channel, external_message_id=external_message_id, action=AttentionAction(upper))
        return NormalizedEvent(channel=self.channel, external_message_id=external_message_id, action=AttentionAction.ANSWER, raw_text=text)
