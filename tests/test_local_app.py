"""Local operator UI -- Flask test client only, no live server process.
interventions_view() talks to Supabase directly; if it's unreachable the
page still renders (an error banner, not a 500), which is itself part of
what's checked here.
"""
from local_app.app import app


def test_index_page_loads():
    client = app.test_client()
    resp = client.get("/")
    assert resp.status_code == 200


def test_interventions_page_loads():
    client = app.test_client()
    resp = client.get("/interventions")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Open Interventions" in body


def test_resolve_unknown_intervention_does_not_500():
    client = app.test_client()
    resp = client.post("/interventions/00000000-0000-0000-0000-000000000000/resolve", data={"answer": "Yes"})
    assert resp.status_code in (200, 302)
