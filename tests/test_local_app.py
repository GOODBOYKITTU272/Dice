"""Local operator UI -- Flask test client only, no live server process.
Runs against the real, linked Supabase project (matching this file's
existing convention) so "real application row renders" can be checked
against genuine data rather than a mock. Disposable TEST- rows created
here are cleaned up in a finally block, same pattern as every other
integration test in this project.
"""
from __future__ import annotations

import uuid

from db.application_repository import enqueue_application, get_supabase_client, update_application_status, upsert_dice_job
from db.intervention_repository import create_or_get_question_intervention
from local_app.app import app
from local_app import queries

# The real, worker-submitted application from Phase 6.1 -- used to prove
# real data renders rather than a mock/placeholder.
REAL_APPLICATION_ID = "728341e7-365e-4f38-bb93-d24424b56c60"
REAL_DICE_JOB_ID = "4f5a17f3-2483-4407-83cb-fe558e26a9e4"


def _client():
    return app.test_client()


def _make_test_job_and_application():
    dice_job_id = f"TEST-{uuid.uuid4()}"
    job = upsert_dice_job(
        {
            "dice_job_id": dice_job_id,
            "canonical_url": f"https://dice.com/job/{dice_job_id}",
            "title": "Local App Test Role",
            "c2c_status": "LIKELY",
            "is_easy_apply": True,
        }
    )
    candidate_id = str(uuid.uuid4())
    application = enqueue_application(candidate_id, job["id"])
    return job, application


def _cleanup(job_id: str):
    sc = get_supabase_client()
    apps = sc.table("applications").select("id").eq("dice_job_id", job_id).execute().data
    for a in apps:
        aid = a["id"]
        sc.table("interventions").delete().eq("application_id", aid).execute()
        sc.table("application_events").delete().eq("application_id", aid).execute()
        sc.table("applications").delete().eq("id", aid).execute()
    sc.table("dice_jobs").delete().eq("id", job_id).execute()


# ── Routes load ──────────────────────────────────────────────────────────


def test_all_nav_pages_load():
    client = _client()
    for route in ("/", "/jobs", "/applications", "/interventions", "/events", "/worker", "/candidate", "/browser-session", "/settings"):
        resp = client.get(route)
        assert resp.status_code == 200, f"{route} returned {resp.status_code}"


def test_job_detail_and_application_detail_load():
    client = _client()
    assert client.get(f"/jobs/{REAL_DICE_JOB_ID}").status_code == 200
    assert client.get(f"/applications/{REAL_APPLICATION_ID}").status_code == 200


def test_get_jobs_apply_does_not_crash_by_falling_through_to_job_detail():
    # Real bug found live on the Vercel deployment (2026-08-22): Flask
    # routed GET /jobs/apply to job_detail_view(job_id="apply") -- there
    # was no GET handler for the literal path /jobs/apply (it's POST-only),
    # so the router fell back to the dynamic /jobs/<job_id> rule, which
    # does accept GET. "apply" then hit Supabase as an invalid UUID and
    # 500'd. /jobs/review is a POST-only literal path too, same exposure.
    client = _client()
    for path in ("/jobs/apply", "/jobs/review"):
        resp = client.get(path)
        assert resp.status_code != 500, f"GET {path} must not 500 (routing must not fall through to /jobs/<job_id>)"


def test_resolve_unknown_intervention_does_not_500():
    client = _client()
    resp = client.post("/interventions/00000000-0000-0000-0000-000000000000/resolve", data={"answer": "Yes"})
    assert resp.status_code in (200, 302)


# ── Real application row renders ────────────────────────────────────────


def test_real_submitted_application_renders_in_applications_list():
    client = _client()
    body = client.get("/applications").get_data(as_text=True)
    assert "Cynet Systems" in body
    assert "SUBMITTED" in body


def test_real_application_detail_renders_actual_stored_values():
    client = _client()
    body = client.get(f"/applications/{REAL_APPLICATION_ID}").get_data(as_text=True)
    assert "Java Developer" in body
    assert "Cynet Systems" in body
    assert REAL_APPLICATION_ID in body
    assert REAL_DICE_JOB_ID in body


# ── Submitted application evidence renders ──────────────────────────────


def test_verification_evidence_renders_for_submitted_application():
    client = _client()
    body = client.get(f"/applications/{REAL_APPLICATION_ID}").get_data(as_text=True)
    assert "VERIFIED_SUBMITTED" in body
    assert "Awesome! Your application is on its way" in body
    assert "wizard" in body and "success" in body
    assert "positively verified by Dice" in body


# ── Counters match repository data ──────────────────────────────────────


def test_dashboard_and_applications_counters_match_repository_data():
    sc = get_supabase_client()
    applications = sc.table("applications").select("*").execute().data
    expected = queries.application_counts(applications)

    client = _client()
    dashboard_body = client.get("/").get_data(as_text=True)
    applications_body = client.get("/applications").get_data(as_text=True)

    for body in (dashboard_body, applications_body):
        assert f'>{expected["submitted"]}<' in body
        assert f'>{expected["needs_input"]}<' in body
        assert f'>{expected["failed"]}<' in body


# ── Interventions: empty and non-empty states ───────────────────────────


def test_interventions_page_handles_empty_state():
    client = _client()
    resp = client.get("/interventions")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    # Either a real open intervention exists, or the explicit empty state does.
    assert "Needs Your Input" in body


def test_interventions_page_handles_non_empty_state():
    job, application = _make_test_job_and_application()
    try:
        update_application_status(application["id"], "PROCESSING")
        iv = create_or_get_question_intervention(
            application_id=application["id"],
            question_id="test-onsite-question",
            question_prompt="Are you able and willing to regularly come into the office to work?",
            field_type="RADIO",
            reason="no trusted candidate mapping",
            choices=["Yes", "No"],
            sensitive=False,
        )
        client = _client()
        body = client.get("/interventions").get_data(as_text=True)
        assert "Are you able and willing to regularly come into the office to work?" in body
        assert "Local App Test Role" in body

        detail_body = client.get(f"/interventions/{iv['id']}").get_data(as_text=True)
        assert "Are you able and willing to regularly come into the office to work?" in detail_body
        assert "Yes" in detail_body and "No" in detail_body
    finally:
        _cleanup(job["id"])


# ── Events: chronological, real data ─────────────────────────────────────


def test_events_page_renders_real_chronological_data():
    client = _client()
    body = client.get("/events").get_data(as_text=True)
    assert "easy_apply_opened" in body or "submission_result" in body


def test_events_page_renders_without_error():
    client = _client()
    resp = client.get("/events?event_type=submission_result")
    assert resp.status_code == 200
    assert "submission_result" in resp.get_data(as_text=True)


# ── No secrets shown in Browser Session ─────────────────────────────────


def test_browser_session_page_shows_no_secrets():
    import os

    client = _client()
    body = client.get("/browser-session").get_data(as_text=True)
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if service_key:
        assert service_key not in body
    api_token = os.environ.get("APPLYWIZZ_API_TOKEN")
    if api_token:
        assert api_token not in body


# ── No fake data when repository returns real rows ──────────────────────


def test_no_hardcoded_mock_application_rows():
    """A disposable real row must actually appear -- proves the page reads
    live Supabase data rather than a static/mock list that would never
    reflect a freshly created row."""
    job, application = _make_test_job_and_application()
    try:
        client = _client()
        body = client.get("/applications").get_data(as_text=True)
        assert application["id"][:8] in body
        assert "Local App Test Role" in body
    finally:
        _cleanup(job["id"])
