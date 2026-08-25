"""Phase 7.4: attention.service -- the Apply/Skip/Confirm/Edit domain
state machine. Against the real, linked Supabase project (same
TEST-prefixed disposable row convention as
test_intervention_repository_integration.py). A FakeProvider stands in
for Telegram/iMessage -- no real network/OS calls anywhere in this file.

Never touches Dice.com or Playwright -- attention.service itself never
does either (see its own module docstring).
"""
from __future__ import annotations

import uuid

import pytest

from attention.models import AttentionAction, NormalizedEvent
from attention.service import CrossCandidateAuthorizationError, UnresolvableEventError, handle_apply, handle_confirm, handle_edit, handle_event, handle_skip, notify_job_offer, notify_next_missing_question, notify_reconnect_required, notify_reconnect_success
from db.application_repository import create_job_offer, get_application, update_application_status, upsert_dice_job
from db.intervention_repository import (
    ApplicationReadiness,
    compute_application_readiness,
    create_or_get_question_intervention,
)
from db.supabase_client import get_supabase_client

_created_job_ids: list[str] = []


class _FakeProvider:
    channel = "TELEGRAM"

    def __init__(self):
        self.sent: list[tuple] = []

    def send_job_offer(self, application, job):
        self.sent.append(("job_offer", application["id"]))
        return f"msg-{len(self.sent)}"

    def send_missing_question(self, application_id, question):
        qid = (question.get("options") or {}).get("question_id")
        self.sent.append(("missing_question", application_id, qid))
        return f"msg-{len(self.sent)}"

    def send_answer_confirmation(self, application_id, question_id, raw_answer):
        self.sent.append(("answer_confirmation", application_id, question_id, raw_answer))
        return f"msg-{len(self.sent)}"

    def send_submission_success(self, application, job):
        self.sent.append(("success", application["id"]))
        return f"msg-{len(self.sent)}"

    def send_submission_failure(self, application, job, reason):
        self.sent.append(("failure", application["id"], reason))
        return f"msg-{len(self.sent)}"

    def send_reconnect_required(self, application_id):
        self.sent.append(("reconnect_required", application_id))
        return f"msg-{len(self.sent)}"

    def send_reconnect_success(self, application, job):
        self.sent.append(("reconnect_success", application["id"]))
        return f"msg-{len(self.sent)}"

    def send_apply_ack(self, application_id):
        self.sent.append(("apply_ack", application_id))
        return f"msg-{len(self.sent)}"

    def send_skip_ack(self, application_id):
        self.sent.append(("skip_ack", application_id))
        return f"msg-{len(self.sent)}"

    def send_answer_accepted(self, application_id, question_id):
        self.sent.append(("answer_accepted", application_id, question_id))
        return f"msg-{len(self.sent)}"

    def send_ready_to_submit(self, application_id):
        self.sent.append(("ready_to_submit", application_id))
        return f"msg-{len(self.sent)}"

    def parse_inbound(self, raw_event):
        raise NotImplementedError


def _make_test_job():
    dice_job_id = f"TEST-{uuid.uuid4()}"
    job = upsert_dice_job({"dice_job_id": dice_job_id, "canonical_url": f"https://dice.com/job/{dice_job_id}", "title": "Attention Service Test Role", "company_name": "Test Co"})
    _created_job_ids.append(job["id"])
    return job


def _cleanup(job_id: str):
    client = get_supabase_client()
    apps = client.table("applications").select("id, run_id").eq("dice_job_id", job_id).execute().data
    run_ids = {a["run_id"] for a in apps if a.get("run_id")}
    for a in apps:
        aid = a["id"]
        for iv in client.table("interventions").select("id").eq("application_id", aid).execute().data:
            client.table("interventions").delete().eq("id", iv["id"]).execute()
        for ev in client.table("attention_events").select("id").eq("application_id", aid).execute().data:
            client.table("attention_events").delete().eq("id", ev["id"]).execute()
        for ev in client.table("application_events").select("id").eq("application_id", aid).execute().data:
            client.table("application_events").delete().eq("id", ev["id"]).execute()
        client.table("applications").delete().eq("id", aid).execute()
    for run_id in run_ids:
        client.table("application_runs").delete().eq("id", run_id).execute()
    client.table("dice_jobs").delete().eq("id", job_id).execute()


@pytest.fixture(autouse=True)
def _cleanup_created_jobs():
    try:
        yield
    finally:
        while _created_job_ids:
            _cleanup(_created_job_ids.pop())


# ── job offer ──────────────────────────────────────────────────────────


# 1. discovered eligible job creates one job-offer message
def test_notify_job_offer_sends_exactly_one_message(live_client):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    offer = create_job_offer(candidate_id, job["id"])
    provider = _FakeProvider()

    notify_job_offer(provider, offer["id"])

    assert [s for s in provider.sent if s[0] == "job_offer"] == [("job_offer", offer["id"])]


# 2. duplicate polling does not resend same offer
def test_notify_job_offer_is_idempotent(live_client):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    offer = create_job_offer(candidate_id, job["id"])
    provider = _FakeProvider()

    notify_job_offer(provider, offer["id"])
    notify_job_offer(provider, offer["id"])
    notify_job_offer(provider, offer["id"])

    assert len([s for s in provider.sent if s[0] == "job_offer"]) == 1


# ── Phase M9: reconciliation -- an offer whose only outbound event
# landed on a non-primary channel must still reach the real primary,
# without fabricating a decision or creating a second application.
# Real production finding 2026-08-25: two offers sent during earlier
# iMessage-channel testing never reached Telegram (the actual configured
# primary) at all.
from attention.service import ensure_offer_reached_primary_channel  # noqa: E402
from attention.events import record_outbound  # noqa: E402


def test_ensure_offer_reached_primary_channel_redelivers_when_only_on_a_secondary_channel(monkeypatch):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    offer = create_job_offer(candidate_id, job["id"])
    # simulates the real historical bug: the only outbound JOB_OFFER
    # event is on IMESSAGE, never TELEGRAM.
    record_outbound(offer["id"], candidate_id, "IMESSAGE", "JOB_OFFER", "loopmessage-msg-1")

    telegram_provider = _FakeProvider()
    telegram_provider.channel = "TELEGRAM"
    monkeypatch.setattr("attention.routing.resolve_primary_provider", lambda cid: telegram_provider)

    result = ensure_offer_reached_primary_channel(offer["id"])

    assert result == {"redelivered": True, "channel": "TELEGRAM"}
    assert telegram_provider.sent == [("job_offer", offer["id"])]
    client = get_supabase_client()
    apps = client.table("applications").select("id").eq("candidate_id", candidate_id).execute().data
    assert len(apps) == 1  # never created a second application


def test_ensure_offer_reached_primary_channel_is_a_noop_when_already_there(monkeypatch):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    offer = create_job_offer(candidate_id, job["id"])
    telegram_provider = _FakeProvider()
    telegram_provider.channel = "TELEGRAM"
    monkeypatch.setattr("attention.routing.resolve_primary_provider", lambda cid: telegram_provider)
    notify_job_offer(telegram_provider, offer["id"])  # the normal, already-correct case

    result = ensure_offer_reached_primary_channel(offer["id"])

    assert result == {"redelivered": False, "reason": "already reached the primary channel"}
    assert len([s for s in telegram_provider.sent if s[0] == "job_offer"]) == 1  # not sent twice


def test_ensure_offer_reached_primary_channel_is_a_noop_once_no_longer_awaiting_decision(monkeypatch):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    offer = create_job_offer(candidate_id, job["id"])
    record_outbound(offer["id"], candidate_id, "IMESSAGE", "JOB_OFFER", "loopmessage-msg-1")
    handle_skip(offer["id"])  # the candidate already resolved it via the secondary channel

    telegram_provider = _FakeProvider()
    telegram_provider.channel = "TELEGRAM"
    monkeypatch.setattr("attention.routing.resolve_primary_provider", lambda cid: telegram_provider)

    result = ensure_offer_reached_primary_channel(offer["id"])

    assert result["redelivered"] is False
    assert "SKIPPED" in result["reason"]
    assert telegram_provider.sent == []  # never resurrects an already-decided offer


def test_ensure_offer_reached_primary_channel_is_a_noop_when_no_primary_channel_configured(monkeypatch):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    offer = create_job_offer(candidate_id, job["id"])
    monkeypatch.setattr("attention.routing.resolve_primary_provider", lambda cid: None)

    result = ensure_offer_reached_primary_channel(offer["id"])

    assert result == {"redelivered": False, "reason": "no primary channel configured"}


# ── Skip ───────────────────────────────────────────────────────────────


# 3 & 4. Skip prevents deep wizard inspection / resume upload -- both are
# structurally guaranteed by never reaching QUEUED (the only status the
# worker daemon's claim mechanism ever picks up).
def test_skip_transitions_to_skipped_never_queued(live_client):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    offer = create_job_offer(candidate_id, job["id"])

    handle_skip(offer["id"])

    assert get_application(offer["id"])["status"] == "SKIPPED"


def test_skip_is_idempotent(live_client):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    offer = create_job_offer(candidate_id, job["id"])

    handle_skip(offer["id"])
    handle_skip(offer["id"])  # second Skip must not raise or double-transition

    assert get_application(offer["id"])["status"] == "SKIPPED"


# ── Apply ──────────────────────────────────────────────────────────────


# 5 & 6. Apply persists authorization and unlocks the existing QUEUED/
# claim mechanism -- never opens a browser itself.
def test_apply_transitions_to_queued_and_creates_a_run(live_client):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    offer = create_job_offer(candidate_id, job["id"])

    handle_apply(offer["id"])

    application = get_application(offer["id"])
    assert application["status"] == "QUEUED"
    assert application["run_id"] is not None


def test_apply_is_idempotent(live_client):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    offer = create_job_offer(candidate_id, job["id"])

    handle_apply(offer["id"])
    run_id_after_first = get_application(offer["id"])["run_id"]
    handle_apply(offer["id"])  # second Apply must not raise or create a second run

    assert get_application(offer["id"])["run_id"] == run_id_after_first


# 17. AUTHORIZED_AUTONOMOUS -- Apply's created run always uses this
# policy; no second Submit confirmation is ever asked for at this layer.
def test_apply_always_uses_authorized_autonomous_policy(live_client):
    import run_registry

    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    offer = create_job_offer(candidate_id, job["id"])

    handle_apply(offer["id"])

    run = run_registry.get_run(get_application(offer["id"])["run_id"])
    assert run["submission_policy"] == "AUTHORIZED_AUTONOMOUS"


# ── missing question / Confirm / Edit ─────────────────────────────────


def _needs_input_application(candidate_id: str, job_id: str) -> dict:
    from db.application_repository import enqueue_application

    application = enqueue_application(candidate_id, job_id)
    update_application_status(application["id"], "PROCESSING", worker_id="test-attention-worker")
    return application


# 9. answer is not finalized before Confirm
def test_handle_answer_does_not_resolve_the_intervention(live_client):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    application = _needs_input_application(candidate_id, job["id"])
    update_application_status(application["id"], "NEEDS_INPUT")
    intervention = create_or_get_question_intervention(
        application_id=application["id"], question_id="q-1", question_prompt="Are you 18 or older?",
        field_type="RADIO", reason="no trusted candidate mapping", choices=["Yes", "No"],
    )
    provider = _FakeProvider()
    event = NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.ANSWER,
                             application_id=application["id"], question_id="q-1", raw_text="Yes")

    from attention.service import handle_answer
    handle_answer(provider, event)

    client = get_supabase_client()
    row = client.table("interventions").select("status, answer").eq("id", intervention["id"]).execute().data[0]
    assert row["status"] == "OPEN"
    assert row["answer"] is None
    # but the confirmation prompt WAS sent
    assert any(s[0] == "answer_confirmation" and s[3] == "Yes" for s in provider.sent)


# 11. Confirm accepts pending answer
def test_handle_confirm_resolves_the_intervention(live_client):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    application = _needs_input_application(candidate_id, job["id"])
    update_application_status(application["id"], "NEEDS_INPUT")
    intervention = create_or_get_question_intervention(
        application_id=application["id"], question_id="q-1", question_prompt="Are you 18 or older?",
        field_type="RADIO", reason="no trusted candidate mapping", choices=["Yes", "No"],
    )
    provider = _FakeProvider()
    answer_event = NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.ANSWER,
                                    application_id=application["id"], question_id="q-1", raw_text="Yes")
    from attention.service import handle_answer
    handle_answer(provider, answer_event)

    confirm_event = NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.CONFIRM,
                                     application_id=application["id"], question_id="q-1")
    handle_confirm(provider, confirm_event)

    client = get_supabase_client()
    row = client.table("interventions").select("status, answer").eq("id", intervention["id"]).execute().data[0]
    assert row["status"] == "ANSWERED"
    assert row["answer"] == "Yes"


# 10. Edit discards pending answer
def test_handle_edit_discards_pending_answer_and_reasks(live_client):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    application = _needs_input_application(candidate_id, job["id"])
    update_application_status(application["id"], "NEEDS_INPUT")
    intervention = create_or_get_question_intervention(
        application_id=application["id"], question_id="q-1", question_prompt="Are you 18 or older?",
        field_type="RADIO", reason="no trusted candidate mapping", choices=["Yes", "No"],
    )
    provider = _FakeProvider()
    answer_event = NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.ANSWER,
                                    application_id=application["id"], question_id="q-1", raw_text="Yes")
    from attention.service import handle_answer
    handle_answer(provider, answer_event)

    edit_event = NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.EDIT,
                                  application_id=application["id"], question_id="q-1")
    handle_edit(provider, edit_event)

    client = get_supabase_client()
    row = client.table("interventions").select("status, answer").eq("id", intervention["id"]).execute().data[0]
    assert row["status"] == "OPEN"
    assert row["answer"] is None
    assert any(s[0] == "missing_question" for s in provider.sent)


# 15. multiple missing questions handled sequentially
def test_notify_next_missing_question_asks_one_at_a_time(live_client):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    application = _needs_input_application(candidate_id, job["id"])
    update_application_status(application["id"], "NEEDS_INPUT")
    create_or_get_question_intervention(application_id=application["id"], question_id="q-1", question_prompt="Question 1", field_type="TEXT_INPUT", reason="no trusted candidate mapping")
    create_or_get_question_intervention(application_id=application["id"], question_id="q-2", question_prompt="Question 2", field_type="TEXT_INPUT", reason="no trusted candidate mapping")
    provider = _FakeProvider()

    notify_next_missing_question(provider, application["id"])
    notify_next_missing_question(provider, application["id"])  # re-poll before q-1 is answered -- must not send q-2 yet

    missing = [s for s in provider.sent if s[0] == "missing_question"]
    assert len(missing) == 1
    assert missing[0][2] == "q-1"


# 16. final confirmed answer resumes same application (readiness becomes
# RESUMABLE -- the daemon's own poll is what actually resumes the browser)
def test_confirming_last_open_intervention_makes_application_resumable(live_client):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    application = _needs_input_application(candidate_id, job["id"])
    update_application_status(application["id"], "NEEDS_INPUT")
    create_or_get_question_intervention(application_id=application["id"], question_id="q-1", question_prompt="Question 1", field_type="TEXT_INPUT", reason="no trusted candidate mapping")
    provider = _FakeProvider()
    from attention.service import handle_answer

    handle_answer(provider, NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.ANSWER, application_id=application["id"], question_id="q-1", raw_text="an answer"))
    handle_confirm(provider, NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.CONFIRM, application_id=application["id"], question_id="q-1"))

    assert compute_application_readiness(application["id"]) == ApplicationReadiness.RESUMABLE


# 25. duplicate inbound messaging events are idempotent
def test_duplicate_inbound_event_id_is_processed_once(live_client):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    application = _needs_input_application(candidate_id, job["id"])
    update_application_status(application["id"], "NEEDS_INPUT")
    create_or_get_question_intervention(application_id=application["id"], question_id="q-1", question_prompt="Question 1", field_type="TEXT_INPUT", reason="no trusted candidate mapping")
    provider = _FakeProvider()
    same_external_id = str(uuid.uuid4())
    event = NormalizedEvent(channel="TELEGRAM", external_message_id=same_external_id, action=AttentionAction.ANSWER,
                             application_id=application["id"], question_id="q-1", raw_text="an answer")

    from attention.service import handle_answer
    handle_answer(provider, event)
    handle_answer(provider, event)  # identical external_message_id -- must be a pure no-op the second time

    assert len([s for s in provider.sent if s[0] == "answer_confirmation"]) == 1


def test_handle_confirm_raises_when_nothing_pending(live_client):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    application = _needs_input_application(candidate_id, job["id"])
    update_application_status(application["id"], "NEEDS_INPUT")
    create_or_get_question_intervention(application_id=application["id"], question_id="q-1", question_prompt="Question 1", field_type="TEXT_INPUT", reason="no trusted candidate mapping")
    provider = _FakeProvider()

    with pytest.raises(UnresolvableEventError):
        handle_confirm(provider, NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.CONFIRM, application_id=application["id"], question_id="q-1"))


# 13. stale callback rejected -- a Confirm button tapped after that
# question was already resolved (e.g. via a different channel, or a
# second identical button press delivered after the first one already
# went through) must not silently re-resolve or corrupt state.
def test_handle_confirm_raises_for_an_already_resolved_question(live_client):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    application = _needs_input_application(candidate_id, job["id"])
    update_application_status(application["id"], "NEEDS_INPUT")
    create_or_get_question_intervention(application_id=application["id"], question_id="q-1", question_prompt="Question 1", field_type="TEXT_INPUT", reason="no trusted candidate mapping")
    provider = _FakeProvider()
    from attention.service import handle_answer

    handle_answer(provider, NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.ANSWER, application_id=application["id"], question_id="q-1", raw_text="an answer"))
    handle_confirm(provider, NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.CONFIRM, application_id=application["id"], question_id="q-1"))

    # stale: a second, distinct Confirm callback for the same
    # already-resolved question -- get_open_intervention no longer finds
    # it OPEN, so this must raise rather than re-resolve it.
    with pytest.raises(UnresolvableEventError):
        handle_confirm(provider, NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.CONFIRM, application_id=application["id"], question_id="q-1"))


# 22. ambiguous correlation is never guessed -- two interventions OPEN
# at once with neither yet asked over the channel (no MISSING_QUESTION
# sent) means an unstructured inbound reply (no explicit question_id,
# e.g. a plain-text iMessage answer) has no safe way to know which
# question it's answering. Must raise, never guess "the first one".
def test_handle_answer_raises_when_multiple_interventions_are_open_and_none_yet_asked(live_client):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    application = _needs_input_application(candidate_id, job["id"])
    update_application_status(application["id"], "NEEDS_INPUT")
    create_or_get_question_intervention(application_id=application["id"], question_id="q-1", question_prompt="Question 1", field_type="TEXT_INPUT", reason="no trusted candidate mapping")
    create_or_get_question_intervention(application_id=application["id"], question_id="q-2", question_prompt="Question 2", field_type="TEXT_INPUT", reason="no trusted candidate mapping")
    provider = _FakeProvider()
    from attention.service import handle_answer

    with pytest.raises(UnresolvableEventError):
        handle_answer(provider, NormalizedEvent(channel="IMESSAGE", external_message_id=str(uuid.uuid4()), action=AttentionAction.ANSWER, application_id=application["id"], raw_text="an answer"))


# ── Phase 7.5b: visible Apply/Skip/Confirm acknowledgements ─────────────


def test_apply_ack_sent_once_via_handle_event_not_on_duplicate(live_client):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    offer = create_job_offer(candidate_id, job["id"])
    provider = _FakeProvider()

    handle_event(provider, NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.APPLY), candidate_id)
    handle_event(provider, NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.APPLY), candidate_id)

    assert [s for s in provider.sent if s[0] == "apply_ack"] == [("apply_ack", offer["id"])]


def test_skip_ack_sent_once_via_handle_event_not_on_duplicate(live_client):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    offer = create_job_offer(candidate_id, job["id"])
    provider = _FakeProvider()

    handle_event(provider, NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.SKIP), candidate_id)
    handle_event(provider, NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.SKIP), candidate_id)

    assert [s for s in provider.sent if s[0] == "skip_ack"] == [("skip_ack", offer["id"])]


def test_intermediate_confirm_sends_got_it_before_next_question(live_client):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    application = _needs_input_application(candidate_id, job["id"])
    update_application_status(application["id"], "NEEDS_INPUT")
    create_or_get_question_intervention(application_id=application["id"], question_id="q-1", question_prompt="Question 1", field_type="TEXT_INPUT", reason="no trusted candidate mapping")
    create_or_get_question_intervention(application_id=application["id"], question_id="q-2", question_prompt="Question 2", field_type="TEXT_INPUT", reason="no trusted candidate mapping")
    provider = _FakeProvider()
    from attention.service import handle_answer

    notify_next_missing_question(provider, application["id"])  # asks q-1
    handle_answer(provider, NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.ANSWER, application_id=application["id"], question_id="q-1", raw_text="an answer"))
    handle_confirm(provider, NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.CONFIRM, application_id=application["id"], question_id="q-1"))

    kinds = [s[0] for s in provider.sent]
    assert "answer_accepted" in kinds
    ack_index = kinds.index("answer_accepted")
    missing_after_ack = [s for s in provider.sent[ack_index:] if s[0] == "missing_question"]
    assert missing_after_ack and missing_after_ack[0][2] == "q-2"


def _authorized_autonomous_application_with_run(candidate_id: str, job_id: str) -> dict:
    import run_registry

    application = _needs_input_application(candidate_id, job_id)
    run_registry.create_run([application["id"]], candidate_id=candidate_id, submission_policy="AUTHORIZED_AUTONOMOUS")
    update_application_status(application["id"], "NEEDS_INPUT")
    return get_application(application["id"])


def test_final_confirm_authorized_autonomous_sends_ready_to_submit(live_client):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    application = _authorized_autonomous_application_with_run(candidate_id, job["id"])
    create_or_get_question_intervention(application_id=application["id"], question_id="q-1", question_prompt="Question 1", field_type="TEXT_INPUT", reason="no trusted candidate mapping")
    provider = _FakeProvider()
    from attention.service import handle_answer

    handle_answer(provider, NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.ANSWER, application_id=application["id"], question_id="q-1", raw_text="an answer"))
    handle_confirm(provider, NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.CONFIRM, application_id=application["id"], question_id="q-1"))

    assert [s for s in provider.sent if s[0] == "ready_to_submit"] == [("ready_to_submit", application["id"])]


def test_final_confirm_require_confirmation_does_not_send_ready_to_submit(live_client):
    import run_registry

    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    application = _needs_input_application(candidate_id, job["id"])
    run_registry.create_run([application["id"]], candidate_id=candidate_id, submission_policy="REQUIRE_CONFIRMATION")
    update_application_status(application["id"], "NEEDS_INPUT")
    application = get_application(application["id"])
    create_or_get_question_intervention(application_id=application["id"], question_id="q-1", question_prompt="Question 1", field_type="TEXT_INPUT", reason="no trusted candidate mapping")
    provider = _FakeProvider()
    from attention.service import handle_answer

    handle_answer(provider, NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.ANSWER, application_id=application["id"], question_id="q-1", raw_text="an answer"))
    handle_confirm(provider, NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.CONFIRM, application_id=application["id"], question_id="q-1"))

    assert [s for s in provider.sent if s[0] == "ready_to_submit"] == []


def test_final_confirm_stopped_run_does_not_send_ready_to_submit(live_client):
    import run_registry

    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    application = _authorized_autonomous_application_with_run(candidate_id, job["id"])
    run_registry.update_run_status(application["run_id"], "STOPPED")
    create_or_get_question_intervention(application_id=application["id"], question_id="q-1", question_prompt="Question 1", field_type="TEXT_INPUT", reason="no trusted candidate mapping")
    provider = _FakeProvider()
    from attention.service import handle_answer

    handle_answer(provider, NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.ANSWER, application_id=application["id"], question_id="q-1", raw_text="an answer"))
    handle_confirm(provider, NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.CONFIRM, application_id=application["id"], question_id="q-1"))

    assert [s for s in provider.sent if s[0] == "ready_to_submit"] == []


# ── Phase 7.5b: repeated-answer-tap duplicate-card guard ────────────────


def test_repeated_answer_before_confirm_does_not_resend_confirmation_card(live_client):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    application = _needs_input_application(candidate_id, job["id"])
    update_application_status(application["id"], "NEEDS_INPUT")
    create_or_get_question_intervention(application_id=application["id"], question_id="q-1", question_prompt="Are you 18 or older?", field_type="RADIO", reason="no trusted candidate mapping", choices=["Yes", "No"])
    provider = _FakeProvider()
    from attention.service import handle_answer

    handle_answer(provider, NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.ANSWER, application_id=application["id"], question_id="q-1", raw_text="Yes"))
    handle_answer(provider, NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.ANSWER, application_id=application["id"], question_id="q-1", raw_text="Yes"))
    handle_answer(provider, NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.ANSWER, application_id=application["id"], question_id="q-1", raw_text="No"))

    assert len([s for s in provider.sent if s[0] == "answer_confirmation"]) == 1
    # Confirm still resolves to whichever was tapped last.
    handle_confirm(provider, NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.CONFIRM, application_id=application["id"], question_id="q-1"))
    from db.intervention_repository import get_resolved_answers

    assert get_resolved_answers(application["id"])["q-1"] == "No"


def test_edit_allows_a_fresh_confirmation_card_for_the_next_answer(live_client):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    application = _needs_input_application(candidate_id, job["id"])
    update_application_status(application["id"], "NEEDS_INPUT")
    create_or_get_question_intervention(application_id=application["id"], question_id="q-1", question_prompt="Are you 18 or older?", field_type="RADIO", reason="no trusted candidate mapping", choices=["Yes", "No"])
    provider = _FakeProvider()
    from attention.service import handle_answer

    handle_answer(provider, NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.ANSWER, application_id=application["id"], question_id="q-1", raw_text="Yes"))
    handle_edit(provider, NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.EDIT, application_id=application["id"], question_id="q-1"))
    handle_answer(provider, NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.ANSWER, application_id=application["id"], question_id="q-1", raw_text="No"))

    assert len([s for s in provider.sent if s[0] == "answer_confirmation"]) == 2


# Phase 8D: reconnect notifications -- distinct from generic submission
# failure, and idempotent per application+channel (never spammed on a
# later worker poll for the same still-unresolved auth condition).
def test_notify_reconnect_required_sends_once_not_twice(live_client):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    application = create_job_offer(candidate_id, job["id"])
    provider = _FakeProvider()

    notify_reconnect_required(provider, application["id"])
    notify_reconnect_required(provider, application["id"])  # simulates a second worker poll

    assert provider.sent == [("reconnect_required", application["id"])]


def test_notify_reconnect_success_sends_once_not_twice(live_client):
    candidate_id = str(uuid.uuid4())
    job = _make_test_job()
    application = create_job_offer(candidate_id, job["id"])
    provider = _FakeProvider()

    notify_reconnect_success(provider, application["id"])
    notify_reconnect_success(provider, application["id"])

    assert provider.sent == [("reconnect_success", application["id"])]


# ── Phase M8A: cross-candidate ownership hardening ───────────────────────
# Defense-in-depth: neither real provider's parse_inbound() ever sets
# event.application_id (see handle_event's docstring), so none of these
# are reachable via a real Telegram/LoopMessage tap today. These tests
# simulate what WOULD happen if a future provider or a hand-crafted
# event ever did carry a foreign application_id -- every one must be
# REJECTED with zero state change and zero side effect.


def test_apply_rejects_cross_candidate_application(live_client):
    candidate_a = str(uuid.uuid4())
    candidate_b = str(uuid.uuid4())
    job = _make_test_job()
    offer = create_job_offer(candidate_a, job["id"])

    with pytest.raises(CrossCandidateAuthorizationError):
        handle_apply(offer["id"], candidate_id=candidate_b)

    assert get_application(offer["id"])["status"] == "AWAITING_USER_DECISION"


def test_skip_rejects_cross_candidate_application(live_client):
    candidate_a = str(uuid.uuid4())
    candidate_b = str(uuid.uuid4())
    job = _make_test_job()
    offer = create_job_offer(candidate_a, job["id"])

    with pytest.raises(CrossCandidateAuthorizationError):
        handle_skip(offer["id"], candidate_id=candidate_b)

    assert get_application(offer["id"])["status"] == "AWAITING_USER_DECISION"


def test_handle_event_apply_rejects_cross_candidate_and_sends_no_ack(live_client):
    candidate_a = str(uuid.uuid4())
    candidate_b = str(uuid.uuid4())
    job = _make_test_job()
    offer = create_job_offer(candidate_a, job["id"])
    provider = _FakeProvider()
    event = NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.APPLY, application_id=offer["id"])

    with pytest.raises(CrossCandidateAuthorizationError):
        handle_event(provider, event, candidate_b)

    assert get_application(offer["id"])["status"] == "AWAITING_USER_DECISION"
    assert provider.sent == []


def test_handle_event_skip_rejects_cross_candidate_and_sends_no_ack(live_client):
    candidate_a = str(uuid.uuid4())
    candidate_b = str(uuid.uuid4())
    job = _make_test_job()
    offer = create_job_offer(candidate_a, job["id"])
    provider = _FakeProvider()
    event = NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.SKIP, application_id=offer["id"])

    with pytest.raises(CrossCandidateAuthorizationError):
        handle_event(provider, event, candidate_b)

    assert get_application(offer["id"])["status"] == "AWAITING_USER_DECISION"
    assert provider.sent == []


def test_answer_rejects_cross_candidate_application(live_client):
    candidate_a = str(uuid.uuid4())
    candidate_b = str(uuid.uuid4())
    job = _make_test_job()
    application = _needs_input_application(candidate_a, job["id"])
    update_application_status(application["id"], "NEEDS_INPUT")
    intervention = create_or_get_question_intervention(
        application_id=application["id"], question_id="q-1", question_prompt="Are you 18 or older?",
        field_type="RADIO", reason="no trusted candidate mapping", choices=["Yes", "No"],
    )
    provider = _FakeProvider()
    event = NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.ANSWER,
                             application_id=application["id"], question_id="q-1", raw_text="Yes")

    with pytest.raises(CrossCandidateAuthorizationError):
        handle_event(provider, event, candidate_b)

    client = get_supabase_client()
    row = client.table("interventions").select("status, answer").eq("id", intervention["id"]).execute().data[0]
    assert row["status"] == "OPEN"
    assert row["answer"] is None
    assert provider.sent == []


def test_confirm_rejects_cross_candidate_application(live_client):
    candidate_a = str(uuid.uuid4())
    candidate_b = str(uuid.uuid4())
    job = _make_test_job()
    application = _needs_input_application(candidate_a, job["id"])
    update_application_status(application["id"], "NEEDS_INPUT")
    intervention = create_or_get_question_intervention(
        application_id=application["id"], question_id="q-1", question_prompt="Are you 18 or older?",
        field_type="RADIO", reason="no trusted candidate mapping", choices=["Yes", "No"],
    )
    provider = _FakeProvider()
    from attention.service import handle_answer
    handle_answer(provider, NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.ANSWER,
                                             application_id=application["id"], question_id="q-1", raw_text="Yes"))

    confirm_event = NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.CONFIRM,
                                     application_id=application["id"], question_id="q-1")
    with pytest.raises(CrossCandidateAuthorizationError):
        handle_event(provider, confirm_event, candidate_b)

    client = get_supabase_client()
    row = client.table("interventions").select("status, answer").eq("id", intervention["id"]).execute().data[0]
    assert row["status"] == "OPEN"  # still unconfirmed -- candidate B's confirm never touched candidate A's intervention


def test_edit_rejects_cross_candidate_application(live_client):
    candidate_a = str(uuid.uuid4())
    candidate_b = str(uuid.uuid4())
    job = _make_test_job()
    application = _needs_input_application(candidate_a, job["id"])
    update_application_status(application["id"], "NEEDS_INPUT")
    intervention = create_or_get_question_intervention(
        application_id=application["id"], question_id="q-1", question_prompt="Are you 18 or older?",
        field_type="RADIO", reason="no trusted candidate mapping", choices=["Yes", "No"],
    )
    provider = _FakeProvider()
    from attention.service import handle_answer
    handle_answer(provider, NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.ANSWER,
                                             application_id=application["id"], question_id="q-1", raw_text="Yes"))

    edit_event = NormalizedEvent(channel="TELEGRAM", external_message_id=str(uuid.uuid4()), action=AttentionAction.EDIT,
                                  application_id=application["id"], question_id="q-1")
    with pytest.raises(CrossCandidateAuthorizationError):
        handle_event(provider, edit_event, candidate_b)

    client = get_supabase_client()
    row = client.table("interventions").select("status, answer").eq("id", intervention["id"]).execute().data[0]
    assert row["answer"] is None  # candidate B's edit never discarded candidate A's pending answer
