"""Phase 8A: db/dice_auth_health_repository.py -- the durable per-candidate
Dice auth-health signal. Real Supabase (dice_auth_health) -- upsert
correctness under repeated writes is exactly what a fake client can't
be trusted to prove.
"""
from __future__ import annotations

import uuid

import pytest

from db import dice_auth_health_repository as repo
from db.supabase_client import get_supabase_client

_created: list[str] = []


@pytest.fixture(autouse=True)
def _cleanup(live_client):
    yield
    client = get_supabase_client()
    while _created:
        client.table("dice_auth_health").delete().eq("candidate_id", _created.pop()).execute()


def _new_candidate():
    cid = str(uuid.uuid4())
    _created.append(cid)
    return cid


def test_get_auth_health_returns_none_when_never_recorded():
    assert repo.get_auth_health(_new_candidate()) is None


def test_mark_healthy_then_get_round_trips():
    cid = _new_candidate()
    written = repo.mark_healthy(cid)
    assert written["is_healthy"] is True
    assert written["last_verified_at"] is not None

    fetched = repo.get_auth_health(cid)
    assert fetched["is_healthy"] is True
    assert fetched["invalidated_reason"] is None


def test_mark_invalid_then_get_round_trips():
    cid = _new_candidate()
    written = repo.mark_invalid(cid, "AUTH_REQUIRED on live re-check")
    assert written["is_healthy"] is False
    assert written["invalidated_reason"] == "AUTH_REQUIRED on live re-check"

    fetched = repo.get_auth_health(cid)
    assert fetched["is_healthy"] is False
    assert fetched["invalidated_at"] is not None


def test_mark_invalid_after_healthy_overwrites_not_appends():
    cid = _new_candidate()
    repo.mark_healthy(cid)
    repo.mark_invalid(cid, "AUTH_REQUIRED on live re-check")

    fetched = repo.get_auth_health(cid)
    assert fetched["is_healthy"] is False  # the invalidation wins, not the earlier healthy write


def test_mark_healthy_after_invalid_clears_the_invalidation():
    cid = _new_candidate()
    repo.mark_invalid(cid, "AUTH_REQUIRED on live re-check")
    repo.mark_healthy(cid)

    fetched = repo.get_auth_health(cid)
    assert fetched["is_healthy"] is True
    assert fetched["invalidated_reason"] is None
