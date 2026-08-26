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

import html
import os

import requests

from attention.formatting import job_metadata_line
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

    def __init__(self, chat_id: str | None = None):
        """chat_id, when given, overrides TELEGRAM_CHAT_ID for every send
        from this instance -- what lets attention.service look up a
        candidate's real bound chat id (candidate_attention_channels)
        and send there, rather than the env var being the permanent
        identity source. Omitting it keeps the original single-candidate
        env-var behavior (existing tests, and single-candidate V1 code
        paths that haven't been threaded through channel resolution)."""
        self._chat_id_override = chat_id

    def fetch_updates(self, offset: int | None = None, timeout: int = 0) -> list[dict]:
        """getUpdates long-polling -- the acceptable-for-V1 mechanism
        (no publicly reachable backend exists yet for a webhook). Passing
        offset=<last update_id>+1 is what tells Telegram to stop
        returning already-seen updates; the caller (attention.consumer)
        derives that from attention_events, never from in-memory state,
        so a restart never re-processes or loses updates."""
        params: dict = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        resp = requests.get(f"{_API_BASE}/bot{_bot_token()}/getUpdates", params=params, timeout=_DEFAULT_TIMEOUT_SECONDS + timeout)
        resp.raise_for_status()
        return resp.json().get("result", [])

    def answer_callback(self, callback_query_id: str) -> None:
        """Dismisses the tap's loading spinner on the user's button --
        without this, Telegram leaves the button showing as stuck loading
        indefinitely, which real live testing showed causes users to tap
        it repeatedly (each retap is still safely deduped downstream, but
        the confusion is a real, avoidable UX gap). Never raises -- a
        failure here must not block the actual event from being
        processed."""
        try:
            requests.post(
                f"{_API_BASE}/bot{_bot_token()}/answerCallbackQuery",
                json={"callback_query_id": callback_query_id},
                timeout=_DEFAULT_TIMEOUT_SECONDS,
            )
        except Exception:
            pass

    def send_typing_indicator(self, chat_id: str) -> None:
        """Shows Telegram's native "..." typing animation for a few
        seconds -- real visible feedback the instant a tap is received,
        before the actual "Checking the application..." text message
        arrives a couple seconds later. Never raises -- a failure here
        must not block the actual event from being processed."""
        try:
            requests.post(
                f"{_API_BASE}/bot{_bot_token()}/sendChatAction",
                json={"chat_id": chat_id, "action": "typing"},
                timeout=_DEFAULT_TIMEOUT_SECONDS,
            )
        except Exception:
            pass

    def clear_buttons(self, chat_id: str, message_id: str) -> None:
        """Strips the inline keyboard off an already-handled message so
        its buttons can never be tapped again -- without this, old Apply/
        Skip/Confirm/Edit buttons stay live forever and real live testing
        showed users repeatedly (and confusingly) re-tapping stale
        messages. Safely deduped downstream regardless, but this removes
        the confusion at the source. Never raises -- a failure here must
        not block the actual event from being processed."""
        try:
            requests.post(
                f"{_API_BASE}/bot{_bot_token()}/editMessageReplyMarkup",
                json={"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
                timeout=_DEFAULT_TIMEOUT_SECONDS,
            )
        except Exception:
            pass

    def _send(self, text: str, buttons: list[list[tuple[str, str]]] | None = None, parse_mode: str | None = None) -> str:
        payload: dict = {"chat_id": self._chat_id_override or _chat_id(), "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in buttons]
            }
        resp = requests.post(f"{_API_BASE}/bot{_bot_token()}/sendMessage", json=payload, timeout=_DEFAULT_TIMEOUT_SECONDS)
        resp.raise_for_status()
        return str(resp.json()["result"]["message_id"])

    def send_job_offer(self, application: dict, job: dict) -> str:
        # HTML parse_mode (Telegram-native rich formatting) so the job
        # title stands out and each qualifying signal reads as its own
        # tag -- job title/company are escaped since they're real,
        # uncontrolled Dice listing text and could otherwise break HTML
        # parsing or (worst case) inject markup into the sent message.
        title = html.escape(job["title"])
        company = html.escape(job.get("company_name") or "Unknown Company")
        tags = []
        if job.get("c2c_status") in ("CONFIRMED", "LIKELY"):
            tags.append("🔵 C2C")
        if job.get("is_easy_apply"):
            tags.append("⚡ Easy Apply")
        tags_line = f"\n{'  '.join(tags)}" if tags else ""
        text = f"🟣 I found something promising for you\n\n<b>{title}</b> — {company}{tags_line}\n\nWant me to apply?"
        # Embeds the exact application_id being offered ("APPLY:<uuid>",
        # well under Telegram's 64-byte callback_data limit) -- see
        # parse_inbound's own comment for why a bare "APPLY"/"SKIP" is no
        # longer safe once more than one offer can be open at once.
        application_id = application["id"]
        buttons = [[("Apply", f"APPLY:{application_id}"), ("Skip", f"SKIP:{application_id}")]]
        return self._send(text, buttons=buttons, parse_mode="HTML")

    def send_apply_ack(self, application_id: str) -> str:
        return self._send("Checking the application...")

    def send_skip_ack(self, application_id: str) -> str:
        return self._send("Skipped 👍\n\nI won't apply to this job.")

    def send_answer_accepted(self, application_id: str, question_id: str) -> str:
        return self._send("Got it ✅")

    def send_ready_to_submit(self, application_id: str) -> str:
        return self._send("✅ I now have everything needed.\n\nSubmitting your application now...")

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
        meta = job_metadata_line(job)
        meta_suffix = f"\n{meta}" if meta else ""
        text = f"Applied successfully ✅\n\n{job['title']}{meta_suffix}\n\nYour application was submitted."
        return self._send(text)

    def send_submission_failure(self, application: dict, job: dict, reason: str) -> str:
        text = f"Couldn't complete this application.\n\n{job['title']}\n{job.get('company_name') or ''}\n\n{reason}".rstrip()
        return self._send(text)

    def send_login_approval_request(self, challenge_id: str) -> str:
        """Phase F2B (revised): a browser login challenge, not a job
        offer -- deliberately its own callback namespace ("AUTH_APPROVE:"
        / "AUTH_DENY:") so it can never be confused with APPLY:/SKIP: by
        anything downstream. Intercepted entirely in attention/consumer.py
        before parse_inbound() ever runs; never reaches AttentionAction."""
        text = "Someone is trying to sign in to your ApplyWizz account.\n\nWas this you?"
        buttons = [[("Approve sign in", f"AUTH_APPROVE:{challenge_id}"), ("Deny", f"AUTH_DENY:{challenge_id}")]]
        return self._send(text, buttons=buttons)

    def send_login_approved_confirmation(self) -> str:
        return self._send("✅ Signed in. You can return to ApplyWizz now.")

    def send_login_denied_confirmation(self) -> str:
        return self._send("Sign-in request denied.")

    def send_reconnect_required(self, application_id: str) -> str:
        text = "🔐 Dice needs to be reconnected\n\nYour application is saved.\nReconnect Dice and I'll continue automatically."
        return self._send(text)

    def send_reconnect_success(self, application: dict, job: dict) -> str:
        return self._send(f"✅ Dice connected\n\nI'm continuing your application for {job['title']} now.")

    def extract_chat_id(self, raw_event: dict) -> str | None:
        """Only used for sender resolution (attention.channels), never
        for correlating an application/question -- that stays entirely
        NormalizedEvent's job."""
        callback = raw_event.get("callback_query")
        if callback is not None:
            chat = (callback.get("message") or {}).get("chat") or {}
            return str(chat["id"]) if "id" in chat else None
        message = raw_event.get("message") or {}
        chat = message.get("chat") or {}
        return str(chat["id"]) if "id" in chat else None

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
            # Phase M9 fix, real production bug: APPLY/SKIP buttons now
            # carry their own application_id ("APPLY:<uuid>") rather than
            # relying solely on "the candidate's most recent open offer"
            # -- that fallback silently misrouted a tap on an OLD card to
            # a newer, already-resolved application the moment more than
            # one offer existed at once (live-reproduced 2026-08-25: every
            # tap on two-day-old cards landed on a same-day test job
            # instead). Bare "APPLY"/"SKIP" (no ":") is kept working for
            # any already-sent button using the old format.
            if ":" in data and data.split(":", 1)[0] in ("APPLY", "SKIP"):
                action_str, application_id = data.split(":", 1)
                return NormalizedEvent(
                    channel=self.channel, external_message_id=external_message_id,
                    action=AttentionAction(action_str), application_id=application_id,
                )
            action = AttentionAction(data)
            return NormalizedEvent(channel=self.channel, external_message_id=external_message_id, action=action)

        message = raw_event.get("message") or {}
        text = (message.get("text") or "").strip()
        upper = text.upper()
        if upper in ("APPLY", "SKIP", "CONFIRM", "EDIT"):
            return NormalizedEvent(channel=self.channel, external_message_id=external_message_id, action=AttentionAction(upper))
        return NormalizedEvent(channel=self.channel, external_message_id=external_message_id, action=AttentionAction.ANSWER, raw_text=text)
