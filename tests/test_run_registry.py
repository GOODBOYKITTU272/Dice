"""run_registry.py: the bounded-run identity mechanism. Backed by the
real application_runs table + applications.run_id column (migration
20260822010000_application_runs.sql), so these are live-Supabase tests
(matching this project's own stated rule: atomic-claim-adjacent behavior
"can't be meaningfully faked in-process"). Disposable TEST- prefixed
dice_jobs/applications, cleaned up per test.
"""
from __future__ import annotations

import uuid

from db.application_repository import enqueue_application, get_supabase_client, upsert_dice_job
import run_registry

CANDIDATE = "55555555-5555-5555-5555-555555555555"


def _make_queued_application():
    dice_job_id = f"TEST-{uuid.uuid4()}"
    job = upsert_dice_job(
        {"dice_job_id": dice_job_id, "canonical_url": f"https://dice.com/job/{dice_job_id}", "title": "Run Registry Test Role"}
    )
    application = enqueue_application(CANDIDATE, job["id"])
    return job, application


def _cleanup(*job_ids: str, run_ids: list[str] = ()):
    sc = get_supabase_client()
    for job_id in job_ids:
        apps = sc.table("applications").select("id").eq("dice_job_id", job_id).execute().data
        for a in apps:
            sc.table("applications").delete().eq("id", a["id"]).execute()
        sc.table("dice_jobs").delete().eq("id", job_id).execute()
    for run_id in run_ids:
        sc.table("application_runs").delete().eq("id", run_id).execute()


def test_create_run_persists_and_stamps_application_ids():
    job_a, app_a = _make_queued_application()
    job_b, app_b = _make_queued_application()
    try:
        run = run_registry.create_run([app_a["id"], app_b["id"]], candidate_id=CANDIDATE)
        assert run["status"] == "PENDING"
        assert set(run["application_ids"]) == {app_a["id"], app_b["id"]}

        sc = get_supabase_client()
        stamped = sc.table("applications").select("id, run_id").in_("id", [app_a["id"], app_b["id"]]).execute().data
        assert all(a["run_id"] == run["id"] for a in stamped)
    finally:
        _cleanup(job_a["id"], job_b["id"], run_ids=[run["id"]])


def test_get_run_raises_for_unknown_id():
    import pytest

    with pytest.raises(run_registry.RunNotFoundError):
        run_registry.get_run(str(uuid.uuid4()))


def test_update_run_status_persists():
    job, app = _make_queued_application()
    run = run_registry.create_run([app["id"]], candidate_id=CANDIDATE)
    try:
        updated = run_registry.update_run_status(run["id"], "RUNNING")
        assert updated["status"] == "RUNNING"
        assert run_registry.get_run(run["id"])["status"] == "RUNNING"
    finally:
        _cleanup(job["id"], run_ids=[run["id"]])


def test_is_stopped_false_for_running_run():
    job, app = _make_queued_application()
    run = run_registry.create_run([app["id"]], candidate_id=CANDIDATE)
    try:
        run_registry.update_run_status(run["id"], "RUNNING")
        assert run_registry.is_stopped(run["id"]) is False
    finally:
        _cleanup(job["id"], run_ids=[run["id"]])


def test_is_stopped_true_after_stop():
    # is_stopped() checks stop_requested, not status -- status == 'STOPPED'
    # is written only by the worker daemon itself, once it actually stops.
    job, app = _make_queued_application()
    run = run_registry.create_run([app["id"]], candidate_id=CANDIDATE)
    try:
        run_registry.request_stop(run["id"])
        assert run_registry.is_stopped(run["id"]) is True
    finally:
        _cleanup(job["id"], run_ids=[run["id"]])


def test_is_stopped_false_for_unknown_run_id():
    assert run_registry.is_stopped(str(uuid.uuid4())) is False


def test_two_runs_do_not_see_each_others_application_ids():
    job_a, app_a = _make_queued_application()
    job_b1, app_b1 = _make_queued_application()
    job_b2, app_b2 = _make_queued_application()
    run_a = run_registry.create_run([app_a["id"]], candidate_id=CANDIDATE)
    run_b = run_registry.create_run([app_b1["id"], app_b2["id"]], candidate_id=CANDIDATE)
    try:
        assert run_registry.get_run(run_a["id"])["application_ids"] == [app_a["id"]]
        assert set(run_registry.get_run(run_b["id"])["application_ids"]) == {app_b1["id"], app_b2["id"]}
    finally:
        _cleanup(job_a["id"], job_b1["id"], job_b2["id"], run_ids=[run_a["id"], run_b["id"]])
