"""run_registry.py: the bounded-run identity mechanism. No Supabase
schema migration is available in this environment (the linked Supabase
CLI session isn't authorized for this project), so the run's membership
lives in a local JSON file instead -- consistent with this whole project
being a single-operator local tool, not a distributed system. This is
what guarantees "select 5 jobs" can never become "process every queued
job": the worker only ever iterates the exact application_ids stored
here, never a DB pool query.
"""
from __future__ import annotations

import uuid

import pytest

import run_registry


@pytest.fixture(autouse=True)
def _isolated_runs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(run_registry, "RUNS_DIR", tmp_path / "runs")


def test_create_run_persists_exact_application_ids_in_order():
    ids = [str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())]
    run = run_registry.create_run(ids, candidate_id="cand-1")
    assert run["application_ids"] == ids
    assert run["status"] == "QUEUED"

    fetched = run_registry.get_run(run["id"])
    assert fetched["application_ids"] == ids


def test_get_run_raises_for_unknown_id():
    with pytest.raises(run_registry.RunNotFoundError):
        run_registry.get_run(str(uuid.uuid4()))


def test_update_run_status_persists():
    run = run_registry.create_run([str(uuid.uuid4())], candidate_id="cand-1")
    updated = run_registry.update_run_status(run["id"], "RUNNING")
    assert updated["status"] == "RUNNING"
    assert run_registry.get_run(run["id"])["status"] == "RUNNING"


def test_is_stopped_false_for_running_run():
    run = run_registry.create_run([str(uuid.uuid4())], candidate_id="cand-1")
    run_registry.update_run_status(run["id"], "RUNNING")
    assert run_registry.is_stopped(run["id"]) is False


def test_is_stopped_true_after_stop():
    run = run_registry.create_run([str(uuid.uuid4())], candidate_id="cand-1")
    run_registry.update_run_status(run["id"], "STOPPED")
    assert run_registry.is_stopped(run["id"]) is True


def test_is_stopped_false_for_unknown_run_id():
    assert run_registry.is_stopped(str(uuid.uuid4())) is False


def test_two_runs_do_not_see_each_others_application_ids():
    ids_a = [str(uuid.uuid4())]
    ids_b = [str(uuid.uuid4()), str(uuid.uuid4())]
    run_a = run_registry.create_run(ids_a, candidate_id="cand-1")
    run_b = run_registry.create_run(ids_b, candidate_id="cand-1")
    assert run_registry.get_run(run_a["id"])["application_ids"] == ids_a
    assert run_registry.get_run(run_b["id"])["application_ids"] == ids_b
