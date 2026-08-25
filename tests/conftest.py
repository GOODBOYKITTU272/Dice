"""Shared test fixtures.

fake_repo gives the pure-logic tests (status transitions, duplicate
enqueue, NEEDS_INPUT preservation) something to run against without a live
Supabase project. It re-implements just enough of the postgrest-py surface
that db/application_repository.py actually calls.

live_client is for the integration tests that need real Postgres semantics
(atomic claim under FOR UPDATE SKIP LOCKED can't be meaningfully faked
in-process) — those tests skip themselves if the DicePilot schema hasn't
been applied to the linked project yet.
"""
from __future__ import annotations

import copy
import itertools
import os
import uuid
from datetime import datetime, timezone

import pytest


class FakeAPIError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, store: "FakeSupabaseClient", table: str):
        self._store = store
        self._table = table
        self._op = None
        self._payload = None
        self._filters: list[tuple[str, object]] = []

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def upsert(self, payload, on_conflict=None):
        self._op = "upsert"
        self._payload = payload
        self._on_conflict = on_conflict
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def select(self, *_cols):
        self._op = "select"
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def execute(self):
        rows = self._store.tables[self._table]

        if self._op == "select":
            matched = [r for r in rows if self._matches(r)]
            return _Result(copy.deepcopy(matched))

        if self._op == "insert":
            row = self._store._prepare_row(self._table, self._payload)
            self._store._check_unique(self._table, row)
            rows.append(row)
            return _Result([copy.deepcopy(row)])

        if self._op == "upsert":
            key_cols = (self._on_conflict or "id").split(",")
            existing = next(
                (r for r in rows if all(r.get(k) == self._payload.get(k) for k in key_cols)),
                None,
            )
            if existing:
                existing.update(self._payload)
                existing["updated_at"] = datetime.now(timezone.utc).isoformat()
                return _Result([copy.deepcopy(existing)])
            row = self._store._prepare_row(self._table, self._payload)
            rows.append(row)
            return _Result([copy.deepcopy(row)])

        if self._op == "update":
            matched = [r for r in rows if self._matches(r)]
            for r in matched:
                r.update(self._payload)
            return _Result([copy.deepcopy(r) for r in matched])

        raise AssertionError(f"unsupported op {self._op}")

    def _matches(self, row):
        return all(row.get(col) == val for col, val in self._filters)


class _RpcCall:
    def __init__(self, store: "FakeSupabaseClient", name: str, params: dict):
        self._store = store
        self._name = name
        self._params = params

    def execute(self):
        if self._name != "claim_next_queued_application":
            raise AssertionError(f"unsupported rpc {self._name}")

        candidate_id = self._params["p_candidate_id"]
        worker_id = self._params["p_worker_id"]
        rows = self._store.tables["applications"]

        has_active = any(
            r["candidate_id"] == candidate_id and r["status"] in ("PROCESSING", "SUBMITTING")
            for r in rows
        )
        if has_active:
            return _Result([])

        candidate_app_ids = {r["id"] for r in rows if r["candidate_id"] == candidate_id}
        has_session_block = any(
            iv["application_id"] in candidate_app_ids
            and iv["status"] == "OPEN"
            and iv["intervention_scope"] == "SESSION_LEVEL"
            for iv in self._store.tables["interventions"]
        )
        if has_session_block:
            return _Result([])

        candidates = [
            r for r in rows if r["candidate_id"] == candidate_id and r["status"] == "QUEUED"
        ]
        if not candidates:
            return _Result([])

        candidates.sort(key=lambda r: (r["priority"], r["queued_at"]))
        claimed = candidates[0]
        now = datetime.now(timezone.utc).isoformat()
        claimed["status"] = "PROCESSING"
        claimed["worker_id"] = worker_id
        claimed["lock_acquired_at"] = now
        claimed["started_at"] = claimed.get("started_at") or now
        claimed["updated_at"] = now
        return _Result([copy.deepcopy(claimed)])


class FakeSupabaseClient:
    """In-memory stand-in for the pieces of the Supabase client this repo uses."""

    _seq = itertools.count()

    def __init__(self):
        self.tables = {
            "dice_jobs": [],
            "applications": [],
            "application_events": [],
            "interventions": [],
            "browser_profiles": [],
        }

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, params):
        return _RpcCall(self, name, params)

    def _prepare_row(self, table, payload):
        row = dict(payload)
        row.setdefault("id", str(uuid.uuid4()))
        now = datetime.now(timezone.utc).isoformat()
        row.setdefault("created_at", now)
        row.setdefault("updated_at", now)
        if table == "applications":
            row.setdefault("status", "QUEUED")
            row.setdefault("priority", 100)
            row.setdefault("attempt_count", 0)
            row.setdefault("queued_at", now)
        if table == "dice_jobs":
            row.setdefault("c2c_status", "UNKNOWN")
            row.setdefault("is_easy_apply", False)
        if table == "interventions":
            row.setdefault("status", "OPEN")
        return row

    def _check_unique(self, table, row):
        if table == "dice_jobs":
            if any(r["dice_job_id"] == row["dice_job_id"] for r in self.tables[table]):
                raise FakeAPIError("23505", "duplicate key value violates unique constraint")
        if table == "applications":
            if any(
                r["candidate_id"] == row["candidate_id"] and r["dice_job_id"] == row["dice_job_id"]
                for r in self.tables[table]
            ):
                raise FakeAPIError("23505", "duplicate key value violates unique constraint")


@pytest.fixture
def fake_repo(monkeypatch):
    """Patch db.application_repository to use an in-memory fake Supabase client."""
    import db.application_repository as repo

    client = FakeSupabaseClient()
    monkeypatch.setattr(repo, "get_supabase_client", lambda: client)
    return repo


@pytest.fixture
def fake_intervention_repo(monkeypatch):
    """Same in-memory fake Supabase client as fake_repo, but shared
    between db.application_repository and db.intervention_repository so
    Phase 4F's module sees the same rows the lower-level repo writes.
    Returns db.intervention_repository; import db.application_repository
    separately for setup (upsert_dice_job/enqueue_application/etc.)."""
    import db.application_repository as app_repo
    import db.intervention_repository as iv_repo

    client = FakeSupabaseClient()
    monkeypatch.setattr(app_repo, "get_supabase_client", lambda: client)
    monkeypatch.setattr(iv_repo, "get_supabase_client", lambda: client)
    return iv_repo


@pytest.fixture
def live_client():
    """Real Supabase client for the linked DicePilot project.

    Skips the test if SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY aren't
    configured, or if the DicePilot tables haven't been migrated onto the
    project yet.
    """
    if not (os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_ROLE_KEY")):
        pytest.skip("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set")

    from db.supabase_client import get_supabase_client

    client = get_supabase_client()
    try:
        client.table("applications").select("id").limit(1).execute()
    except Exception as exc:  # noqa: BLE001 - schema not applied yet
        pytest.skip(f"DicePilot schema not available on linked project: {exc}")
    return client


@pytest.fixture(scope="session", autouse=True)
def _sweep_dice_auth_health_test_pollution():
    """Phase 8B, live-found 2026-08-25: worker.process_one_application/
    resume_needs_input_application now write real dice_auth_health rows
    as a side effect of any successful open_job() call -- including in
    every pre-existing test across the suite (test_worker_run.py,
    test_worker_daemon_architecture.py, test_jobs_apply_to_worker.py,
    ...) that exercises those functions with a synthetic candidate_id,
    none of which know this new table exists to clean it up themselves.

    Rather than hand-patch every one of those files, this is one
    session-scoped blanket sweep: snapshot which candidate_ids already
    have a row before the suite runs, and afterward delete only the
    ones that appeared during this run -- never touching a row that
    predates the test session (e.g. the real candidate's genuine
    production auth-health state)."""
    try:
        from db.supabase_client import get_supabase_client

        client = get_supabase_client()
        before = {r["candidate_id"] for r in client.table("dice_auth_health").select("candidate_id").execute().data}
    except Exception:  # noqa: BLE001 - table/creds unavailable; nothing to sweep
        yield
        return

    yield

    try:
        after = {r["candidate_id"] for r in client.table("dice_auth_health").select("candidate_id").execute().data}
        for candidate_id in after - before:
            client.table("dice_auth_health").delete().eq("candidate_id", candidate_id).execute()
    except Exception:  # noqa: BLE001 - best-effort cleanup only, never fail the suite over it
        pass
