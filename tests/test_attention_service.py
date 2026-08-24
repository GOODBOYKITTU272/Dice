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
from attention.service import UnresolvableEventError, handle_apply, handle_confirm, handle_edit, handle_skip, notify_job_offer, notify_next_missing_question
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
