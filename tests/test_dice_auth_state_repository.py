"""Phase M8B: db/dice_auth_state_repository.py -- candidate-scoped Dice
auth state, Vault-backed (supabase/migrations/
20260825060000_candidate_scoped_dice_auth_state.sql). Real Supabase --
Vault's encrypt-on-write/decrypt-on-read round trip is exactly what a
fake client can't be trusted to prove, same reasoning as
test_dice_auth_health_repository.py.
"""
from __future__ import annotations

import json
import uuid

import pytest

from db import dice_auth_state_repository as repo
from db.supabase_client import get_supabase_client

_created: list[str] = []


@pytest.fixture(autouse=True)
def _cleanup(live_client):
    yield
    client = get_supabase_client()
    while _created:
        client.table("dice_candidate_auth_state").delete().eq("candidate_id", _created.pop()).execute()


def _new_candidate():
    cid = str(uuid.uuid4())
    _created.append(cid)
    return cid


def test_get_auth_state_returns_none_when_never_provisioned():
    assert repo.get_auth_state(_new_candidate()) is None


def test_save_then_get_round_trips_the_exact_cookie_json():
    cid = _new_candidate()
    cookies = json.dumps([{"name": "session", "value": "abc123", "domain": ".dice.com"}])

    repo.save_auth_state(cid, cookies)

    assert repo.get_auth_state(cid) == cookies


def test_two_candidates_have_fully_independent_auth_state():
    cid_a = _new_candidate()
    cid_b = _new_candidate()
    repo.save_auth_state(cid_a, json.dumps([{"name": "cookie-a"}]))
    repo.save_auth_state(cid_b, json.dumps([{"name": "cookie-b"}]))

    assert json.loads(repo.get_auth_state(cid_a)) == [{"name": "cookie-a"}]
    assert json.loads(repo.get_auth_state(cid_b)) == [{"name": "cookie-b"}]


def test_save_auth_state_for_a_candidate_never_leaks_to_a_different_one():
    cid_a = _new_candidate()
    cid_b = _new_candidate()
    repo.save_auth_state(cid_a, json.dumps([{"name": "cookie-a"}]))

    assert repo.get_auth_state(cid_b) is None


def test_reconnect_replaces_the_same_candidates_state_not_a_second_row():
    cid = _new_candidate()
    repo.save_auth_state(cid, json.dumps([{"name": "old-cookie"}]))
    repo.save_auth_state(cid, json.dumps([{"name": "new-cookie"}]))

    client = get_supabase_client()
    rows = client.table("dice_candidate_auth_state").select("id").eq("candidate_id", cid).execute().data
    assert len(rows) == 1
    assert json.loads(repo.get_auth_state(cid)) == [{"name": "new-cookie"}]


def test_invalidate_makes_get_return_none():
    cid = _new_candidate()
    repo.save_auth_state(cid, json.dumps([{"name": "cookie"}]))

    repo.invalidate_auth_state(cid, "manual test invalidation")

    assert repo.get_auth_state(cid) is None


def test_invalidating_one_candidate_does_not_affect_another():
    cid_a = _new_candidate()
    cid_b = _new_candidate()
    repo.save_auth_state(cid_a, json.dumps([{"name": "cookie-a"}]))
    repo.save_auth_state(cid_b, json.dumps([{"name": "cookie-b"}]))

    repo.invalidate_auth_state(cid_a, "manual test invalidation")

    assert repo.get_auth_state(cid_a) is None
    assert json.loads(repo.get_auth_state(cid_b)) == [{"name": "cookie-b"}]


def test_reconnect_after_invalidation_reactivates_the_state():
    cid = _new_candidate()
    repo.save_auth_state(cid, json.dumps([{"name": "old-cookie"}]))
    repo.invalidate_auth_state(cid, "manual test invalidation")
    assert repo.get_auth_state(cid) is None

    repo.save_auth_state(cid, json.dumps([{"name": "reconnected-cookie"}]))

    assert json.loads(repo.get_auth_state(cid)) == [{"name": "reconnected-cookie"}]


def test_raw_cookies_are_not_stored_in_a_plain_readable_column():
    """The state table itself must never hold the plaintext cookies --
    only a pointer into vault.secrets. Confirms the table row has no
    column containing the raw cookie JSON verbatim."""
    cid = _new_candidate()
    secret_marker = "unique-marker-should-never-appear-in-plain-table"
    repo.save_auth_state(cid, json.dumps([{"name": secret_marker}]))

    client = get_supabase_client()
    row = client.table("dice_candidate_auth_state").select("*").eq("candidate_id", cid).execute().data[0]
    assert secret_marker not in json.dumps(row)
