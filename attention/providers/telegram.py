"""Phase 7.4: Telegram Bot API adapter -- primary V1 channel. Sends via
inline keyboard buttons where the action set is bounded (Apply/Skip,
Confirm/Edit, a RADIO/SELECT question's own choices); a free-text
question just asks for a plain reply, handled identically to how
iMessage's plain-text answers work.

No business logic here (see attention/__init__.py) -- parse_inbound()
only translates a raw Telegram Update dict into a NormalizedEvent;
attention.service decides what happens next. callback_data deliberately
never embeds application_id/question_id (Telegram's own 64-byte limit,
and V1 is single-candidate/one-pending-thing-at-a-time by design anyway)
-- attention.service's _resolve_pending_application_id() resolves the
same way it does for iMessage's unstructured replies.

TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are read from the environment only
-- never hardcoded, never committed. This module makes real HTTP calls
only when send_*()/parse_inbound() are actually invoked; importing it
does not require the token to be configured.
"""
from __future__ import annotations

import os

import requests

from attention.models import AttentionAction, NormalizedEvent

_API_BASE = "https://api.telegram.org"
_TOKEN_ENV_VAR = "TELEGRAM_BOT_TOKEN"
_CHAT_ID_ENV_VAR = "TELEGRAM_CHAT_ID"
_DEFAULT_TIMEOUT_SECONDS = 10.0


class TelegramNotConfiguredError(RuntimeError):
    pass


def _bot_token() -> str:
    token = os.environ.get(_TOKEN_ENV_VAR)
    if not token:
        raise TelegramNotConfiguredError(f"{_TOKEN_ENV_VAR} is not configured")
    return token


def _chat_id() -> str:
    chat_id = os.environ.get(_CHAT_ID_ENV_VAR)
    if not chat_id:
        raise TelegramNotConfiguredError(f"{_CHAT_ID_ENV_VAR} is not configured")
    return chat_id


class TelegramProvider:
    channel = "TELEGRAM"

    def _send(self, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> str:
        payload: dict = {"chat_id": _chat_id(), "text": text}
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in buttons]
            }
        resp = requests.post(f"{_API_BASE}/bot{_bot_token()}/sendMessage", json=payload, timeout=_DEFAULT_TIMEOUT_SECONDS)
        resp.raise_for_status()
        return str(resp.json()["result"]["message_id"])

    def send_job_offer(self, application: dict, job: dict) -> str:
        text = f"Found a match\n\n{job['title']} — {job.get('company_name') or 'Unknown Company'}\nC2C • Easy Apply\n\nApplication check complete."
        return self._send(text, buttons=[[("Apply", "APPLY"), ("Skip", "SKIP")]])

    def send_missing_question(self, application_id: str, question: dict) -> str:
        options = (question.get("options") or {}).get("choices")
        prompt = question.get("question_text") or "I need one answer before I can continue."
        text = f"I need one answer before I can continue:\n\n{prompt}"
        if options:
            buttons = [[(choice, f"ANSWER:{choice}") for choice in options]]
            return self._send(text, buttons=buttons)
        return self._send(text + "\n\n(Reply with your answer)")

    def send_answer_confirmation(self, application_id: str, question_id: str, raw_answer: str) -> str:
        text = f"You answered:\n\n{raw_answer}"
        return self._send(text, buttons=[[("Confirm", "CONFIRM"), ("Edit", "EDIT")]])

    def send_submission_success(self, application: dict, job: dict) -> str:
        text = f"Applied successfully ✅\n\n{job['title']}\n{job.get('company_name') or ''}".rstrip()
        return self._send(text)

    def send_submission_failure(self, application: dict, job: dict, reason: str) -> str:
        text = f"Couldn't complete this application.\n\n{job['title']}\n{job.get('company_name') or ''}\n\n{reason}".rstrip()
        return self._send(text)

    def parse_inbound(self, raw_event: dict) -> NormalizedEvent:
        external_message_id = str(raw_event["update_id"])

        callback = raw_event.get("callback_query")
        if callback is not None:
            data = callback.get("data", "")
            if data.startswith("ANSWER:"):
                return NormalizedEvent(
                    channel=self.channel, external_message_id=external_message_id,
                    action=AttentionAction.ANSWER, raw_text=data[len("ANSWER:"):],
                )
            action = AttentionAction(data)
            return NormalizedEvent(channel=self.channel, external_message_id=external_message_id, action=action)

        message = raw_event.get("message") or {}
        text = (message.get("text") or "").strip()
        upper = text.upper()
        if upper in ("APPLY", "SKIP", "CONFIRM", "EDIT"):
            return NormalizedEvent(channel=self.channel, external_message_id=external_message_id, action=AttentionAction(upper))
        return NormalizedEvent(channel=self.channel, external_message_id=external_message_id, action=AttentionAction.ANSWER, raw_text=text)
