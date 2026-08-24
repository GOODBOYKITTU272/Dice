"""Phase 7.4: the Apply/Skip/Confirm/Edit domain state machine. Every
function here is transport-neutral -- takes an AttentionProvider (never a
concrete Telegram/iMessage class by name) and plain ids, never a
Telegram update or an iMessage row. This is the ONLY place Apply/Skip/
Confirm/Edit business logic is allowed to live (see attention/__init__.py).

Never calls into Playwright/dice_browser directly, and never submits a
Dice application synchronously from an inbound-event handler -- Apply
and Confirm only ever flip Supabase state (QUEUED / resolved
intervention); the existing worker daemon (dice_browser/worker_daemon.py)
is what actually drives the browser, on its own poll loop, unchanged.
"""
from __future__ import annotations

import run_registry
from attention.events import (
    already_processed_inbound,
    already_sent_outbound,
    has_active_answer_confirmation,
    latest_active_question_id,
    latest_inbound_answer,
    record_inbound,
    record_outbound,
)
from attention.models import AttentionAction, MessageType, NormalizedEvent
from attention.providers.base import AttentionProvider
from db.application_repository import get_application, get_dice_job, update_application_status
from db.intervention_repository import (
    ApplicationReadiness,
    compute_application_readiness,
    get_open_intervention,
    list_open_interventions,
    resolve_question_intervention,
)


class UnresolvableEventError(RuntimeError):
    """Raised when an inbound event can't be attributed to an
    application/question (e.g. a CONFIRM with nothing pending). Callers
    should treat this as "ignore the message", never as a reason to guess."""


# ── outbound ────────────────────────────────────────────────────────────


def notify_job_offer(provider: AttentionProvider, application_id: str) -> None:
    """Idempotent: a second call for the same application+channel is a
    no-op (the DB's own partial unique index would reject a duplicate
    insert regardless, but checking first avoids sending the message
    twice while the DB write is still in flight)."""
    if already_sent_outbound(application_id, provider.channel, MessageType.JOB_OFFER.value):
        return
    application = get_application(application_id)
    job = get_dice_job(application["dice_job_id"])
    external_id = provider.send_job_offer(application, job)
    record_outbound(application_id, application["candidate_id"], provider.channel, MessageType.JOB_OFFER.value, external_id)


def notify_next_missing_question(provider: AttentionProvider, application_id: str) -> None:
    """Sends exactly one question -- the oldest OPEN intervention that
    hasn't already been asked over this channel. No-ops if none are
    open (nothing missing) or if the current one was already asked
    (never resend the same question while waiting on its answer)."""
    open_interventions = list_open_interventions(application_id)
    if not open_interventions:
        return
    already_asked_id = latest_active_question_id(application_id)
    for intervention in open_interventions:
        question_id = (intervention.get("options") or {}).get("question_id")
        if question_id and question_id == already_asked_id:
            return  # already waiting on this one's answer -- do not re-send
        application = get_application(application_id)
        external_id = provider.send_missing_question(application_id, intervention)
        record_outbound(
            application_id,
            application["candidate_id"],
            provider.channel,
            MessageType.MISSING_QUESTION.value,
            external_id,
            payload={"question_id": question_id},
        )
        return


def notify_submission_success(provider: AttentionProvider, application_id: str) -> None:
    if already_sent_outbound(application_id, provider.channel, MessageType.SUBMISSION_SUCCESS.value):
        return
    application = get_application(application_id)
    job = get_dice_job(application["dice_job_id"])
    external_id = provider.send_submission_success(application, job)
    record_outbound(application_id, application["candidate_id"], provider.channel, MessageType.SUBMISSION_SUCCESS.value, external_id)


def notify_submission_failure(provider: AttentionProvider, application_id: str, reason: str) -> None:
    if already_sent_outbound(application_id, provider.channel, MessageType.SUBMISSION_FAILURE.value):
        return
    application = get_application(application_id)
    job = get_dice_job(application["dice_job_id"])
    external_id = provider.send_submission_failure(application, job, reason)
    record_outbound(application_id, application["candidate_id"], provider.channel, MessageType.SUBMISSION_FAILURE.value, external_id)


# ── inbound ─────────────────────────────────────────────────────────────


def handle_apply(application_id: str) -> bool:
    """Apply is authorization to complete and submit -- nothing more.
    Idempotent by construction: a second Apply finds the application no
    longer AWAITING_USER_DECISION and no-ops. Never opens a browser here
    -- QUEUED is the exact same status/claim mechanism the existing Jobs-
    selection UI's "Start Applications" button already uses; the worker
    daemon picks this run up on its own next poll. Returns True only when
    this call actually changed state -- handle_event uses that to decide
    whether to send the (send-once) Apply acknowledgement, never on a
    duplicate/replayed Apply."""
    application = get_application(application_id)
    if application["status"] != "AWAITING_USER_DECISION":
        return False
    update_application_status(application_id, "QUEUED")
    run_registry.create_run([application_id], candidate_id=application["candidate_id"], submission_policy="AUTHORIZED_AUTONOMOUS")
    return True


def handle_skip(application_id: str) -> bool:
    """Idempotent: a second Skip finds the application already SKIPPED
    (or past that point) and no-ops. Returns True only when this call
    actually changed state -- see handle_apply."""
    application = get_application(application_id)
    if application["status"] != "AWAITING_USER_DECISION":
        return False
    update_application_status(application_id, "SKIPPED")
    return True


def handle_answer(provider: AttentionProvider, event: NormalizedEvent, candidate_id: str | None = None) -> None:
    """Records the raw answer as PENDING (an inbound attention_events row
    only -- interventions.answer is untouched) and asks the user to
    Confirm/Edit. Never itself resolves the intervention -- only
    handle_confirm does that. `candidate_id`, when given (attention.
    consumer already resolved the real sender via candidate_attention_
    channels), takes priority over the single-candidate env-var fallback
    -- see _resolve_pending_application_id."""
    if already_processed_inbound(event.channel, event.external_message_id):
        return
    application_id = event.application_id or _resolve_pending_application_id("NEEDS_INPUT", candidate_id)
    question_id = event.question_id or latest_active_question_id(application_id)
    if question_id is None:
        raise UnresolvableEventError("no pending question to attribute this answer to")

    application = get_application(application_id)
    record_inbound(
        application_id,
        application["candidate_id"],
        event.channel,
        MessageType.ANSWER_CONFIRMATION.value,
        AttentionAction.ANSWER.value,
        event.external_message_id,
        payload={"question_id": question_id, "raw_answer": event.raw_text},
    )
    if has_active_answer_confirmation(application_id, question_id):
        # A "You answered... [Confirm][Edit]" card is already on screen
        # and unresolved for this question -- a repeat tap (Yes again, or
        # even a changed answer before Confirm/Edit) still gets recorded
        # above (so Confirm uses whichever was tapped last), but must not
        # spam a second card. Real live finding: without this, retapping
        # the same answer button produced a new "You answered" card every
        # single time.
        return
    external_id = provider.send_answer_confirmation(application_id, question_id, event.raw_text)
    record_outbound(
        application_id,
        application["candidate_id"],
        event.channel,
        MessageType.ANSWER_CONFIRMATION.value,
        external_id,
        payload={"question_id": question_id},
    )


def handle_confirm(provider: AttentionProvider, event: NormalizedEvent, candidate_id: str | None = None) -> None:
    """"This answer is correct for this application" -- not "remember
    this forever". Reuse eligibility is entirely find_reusable_answer()'s
    existing exact-question_id policy (dice_browser.worker already
    checks it before ever creating an intervention in the first place);
    nothing here decides reusability. Never opens a browser or resumes
    the application synchronously -- once every open intervention is
    resolved, the worker daemon's own poll loop picks the application
    back up (dice_browser/worker_daemon.py's RESUMABLE check)."""
    if already_processed_inbound(event.channel, event.external_message_id):
        return
    application_id = event.application_id or _resolve_pending_application_id("NEEDS_INPUT", candidate_id)
    question_id = event.question_id or latest_active_question_id(application_id)
    if question_id is None:
        raise UnresolvableEventError("no pending question to confirm")

    raw_answer = latest_inbound_answer(application_id, question_id)
    if raw_answer is None:
        raise UnresolvableEventError("no pending answer to confirm")

    intervention = get_open_intervention(application_id, question_id)
    if intervention is None:
        raise UnresolvableEventError(f"no open intervention for question_id={question_id!r}")

    application = get_application(application_id)
    resolve_question_intervention(intervention["id"], raw_answer, source="candidate_via_messaging")
    record_inbound(
        application_id,
        application["candidate_id"],
        event.channel,
        MessageType.ANSWER_CONFIRMATION.value,
        AttentionAction.CONFIRM.value,
        event.external_message_id,
        payload={"question_id": question_id},
    )

    if compute_application_readiness(application_id) != ApplicationReadiness.RESUMABLE:
        _send_answer_accepted_ack(provider, application, question_id)
        notify_next_missing_question(provider, application_id)
    else:
        # Fully resolved -- worker daemon resumes it on its own poll,
        # never this handler. The visible "ready to submit" ack is purely
        # informational and only makes sense for AUTHORIZED_AUTONOMOUS
        # (where resumption really is about to happen automatically);
        # REQUIRE_CONFIRMATION must never claim submission is starting,
        # and a STOPPED run must never claim it'll resume at all.
        _send_ready_to_submit_ack(provider, application)


def handle_edit(provider: AttentionProvider, event: NormalizedEvent, candidate_id: str | None = None) -> None:
    """Discards the pending (unconfirmed) answer -- it was never written
    to interventions.answer, so there's nothing to undo there -- and asks
    the same question again."""
    if already_processed_inbound(event.channel, event.external_message_id):
        return
    application_id = event.application_id or _resolve_pending_application_id("NEEDS_INPUT", candidate_id)
    question_id = event.question_id or latest_active_question_id(application_id)
    if question_id is None:
        raise UnresolvableEventError("no pending question to edit")

    application = get_application(application_id)
    record_inbound(
        application_id,
        application["candidate_id"],
        event.channel,
        MessageType.ANSWER_CONFIRMATION.value,
        AttentionAction.EDIT.value,
        event.external_message_id,
        payload={"question_id": question_id},
    )
    intervention = get_open_intervention(application_id, question_id)
    if intervention is not None:
        external_id = provider.send_missing_question(application_id, intervention)
        record_outbound(
            application_id,
            application["candidate_id"],
            event.channel,
            MessageType.MISSING_QUESTION.value,
            external_id,
            payload={"question_id": question_id},
        )


def handle_event(provider: AttentionProvider, event: NormalizedEvent, candidate_id: str | None = None) -> None:
    """Single dispatch entry point both providers' inbound handlers call
    into -- keeps the action->handler mapping in exactly one place.
    `candidate_id` should be the sender identity attention.consumer
    already resolved via candidate_attention_channels -- omitting it
    falls back to the single-candidate env var (DICEPILOT_CANDIDATE_ID),
    which is only ever correct when that's genuinely the one candidate
    involved (existing offline tests written before real multi-identity
    resolution existed).

    APPLY/SKIP record their own inbound attention_events row here (the
    one place with both the resolved application_id and the full event)
    rather than inside handle_apply/handle_skip themselves -- those two
    keep their existing application_id-only signature (and every
    existing test that calls them directly) unchanged. Real live finding:
    without this, already_processed_inbound() had nothing to ever match
    against for these two actions, so neither dedup nor Telegram's
    getUpdates offset (attention.consumer._last_seen_external_id, also
    derived from recorded inbound events) ever advanced past a
    processed Apply/Skip."""
    if event.action == AttentionAction.APPLY:
        application_id = event.application_id or _resolve_offer_application_id(candidate_id)
        _record_offer_decision_inbound(event, application_id, AttentionAction.APPLY)
        if handle_apply(application_id):
            _send_once(provider, application_id, MessageType.APPLY_ACK, provider.send_apply_ack)
    elif event.action == AttentionAction.SKIP:
        application_id = event.application_id or _resolve_offer_application_id(candidate_id)
        _record_offer_decision_inbound(event, application_id, AttentionAction.SKIP)
        if handle_skip(application_id):
            _send_once(provider, application_id, MessageType.SKIP_ACK, provider.send_skip_ack)
    elif event.action == AttentionAction.ANSWER:
        handle_answer(provider, event, candidate_id)
    elif event.action == AttentionAction.CONFIRM:
        handle_confirm(provider, event, candidate_id)
    elif event.action == AttentionAction.EDIT:
        handle_edit(provider, event, candidate_id)


def _send_once(provider: AttentionProvider, application_id: str, message_type: MessageType, send_fn) -> None:
    """For the send-once acks (APPLY_ACK/SKIP_ACK/READY_TO_SUBMIT) --
    already_sent_outbound + the DB's own partial unique index are the
    belt-and-suspenders backstop; the actual "only once" guarantee comes
    from the caller only invoking this when the underlying state
    transition really happened (handle_apply/handle_skip's bool return,
    handle_confirm's own OPEN-intervention check) -- live-verified today
    under 12x duplicate Apply and 5x duplicate Confirm deliveries."""
    if already_sent_outbound(application_id, provider.channel, message_type.value):
        return
    application = get_application(application_id)
    external_id = send_fn(application_id)
    record_outbound(application_id, application["candidate_id"], provider.channel, message_type.value, external_id)


def _send_answer_accepted_ack(provider: AttentionProvider, application: dict, question_id: str) -> None:
    external_id = provider.send_answer_accepted(application["id"], question_id)
    record_outbound(
        application["id"], application["candidate_id"], provider.channel,
        MessageType.ANSWER_ACCEPTED.value, external_id, payload={"question_id": question_id},
    )


def _send_ready_to_submit_ack(provider: AttentionProvider, application: dict) -> None:
    run_id = application.get("run_id")
    if not run_id:
        return
    run = run_registry.get_run(run_id)
    if run["status"] == "STOPPED":
        return
    if run["submission_policy"] != "AUTHORIZED_AUTONOMOUS":
        return  # REQUIRE_CONFIRMATION must never claim submission is starting
    _send_once(provider, application["id"], MessageType.READY_TO_SUBMIT, lambda application_id: provider.send_ready_to_submit(application_id))


def _record_offer_decision_inbound(event: NormalizedEvent, application_id: str, action: AttentionAction) -> None:
    application = get_application(application_id)
    record_inbound(
        application_id,
        application["candidate_id"],
        event.channel,
        MessageType.JOB_OFFER.value,
        action.value,
        event.external_message_id,
    )


def _default_candidate_id() -> str:
    import os

    candidate_id = os.environ.get("DICEPILOT_CANDIDATE_ID")
    if not candidate_id:
        raise UnresolvableEventError("DICEPILOT_CANDIDATE_ID is not configured")
    return candidate_id


def _resolve_pending_application_id(status: str, candidate_id: str | None = None) -> str:
    """A channel with no structured correlation (iMessage's plain-text
    replies) always means "whichever candidate attention.consumer
    resolved this sender to". Resolves to that candidate's most recently
    created application currently in `status` -- there is only ever
    meant to be one (job offers and missing questions are both strictly
    sequential), but "most recent" is the well-defined tiebreaker if
    that invariant is ever violated. Falls back to the single-candidate
    env var only when no explicit candidate_id is given (existing
    offline tests / a channel not yet wired through attention.consumer's
    real sender resolution)."""
    from db.supabase_client import get_supabase_client

    candidate_id = candidate_id or _default_candidate_id()
    client = get_supabase_client()
    rows = (
        client.table("applications")
        .select("id, created_at")
        .eq("candidate_id", candidate_id)
        .eq("status", status)
        .execute()
        .data
        or []
    )
    if not rows:
        raise UnresolvableEventError(f"no {status} application for candidate {candidate_id!r}")
    rows.sort(key=lambda r: r.get("created_at") or "")
    return rows[-1]["id"]


_OFFER_LIFECYCLE_STATUSES = ("AWAITING_USER_DECISION", "QUEUED", "SKIPPED")


def _resolve_offer_application_id(candidate_id: str | None = None) -> str:
    """APPLY/SKIP's own resolver -- deliberately broader than
    _resolve_pending_application_id's single-status lookup. Real live
    finding: handle_apply/handle_skip's own idempotency check (a second
    Apply/Skip finds the application already past AWAITING_USER_DECISION
    and no-ops) can only ever run once an application_id is actually
    resolved -- a duplicate delivery arriving AFTER the first one already
    transitioned the status away from AWAITING_USER_DECISION would
    otherwise fail resolution entirely before that idempotency check
    gets the chance to fire. Resolving across the whole small offer
    lifecycle (still-pending, already applied, already skipped) instead
    lets a duplicate correctly resolve to the SAME application either way,
    exactly matching the invariant "there is only ever one active job
    offer conversation with this candidate at a time"."""
    from db.supabase_client import get_supabase_client

    candidate_id = candidate_id or _default_candidate_id()
    client = get_supabase_client()
    rows: list[dict] = []
    for status in _OFFER_LIFECYCLE_STATUSES:
        rows.extend(
            client.table("applications").select("id, created_at").eq("candidate_id", candidate_id).eq("status", status).execute().data or []
        )
    if not rows:
        raise UnresolvableEventError(f"no pending or recently-decided job offer for candidate {candidate_id!r}")
    rows.sort(key=lambda r: r.get("created_at") or "")
    return rows[-1]["id"]
