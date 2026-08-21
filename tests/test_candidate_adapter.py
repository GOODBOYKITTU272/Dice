"""Phase 4E: Candidate Adapter. Offline tests only -- no live ApplyWizz
API call for any of these (see the separate live/integration check run
manually against real environment credentials, not part of this suite).
"""
from __future__ import annotations

import requests

import dice.candidate_adapter as candidate_adapter
from dice.candidate_adapter import fetch_candidate, normalize_candidate_payload, resolve_candidate_field
from dice.models import CandidateFetchStatus


def _complete_payload() -> dict:
    return {
        "client": {
            "id": "cand-001",
            "full_name": "Jordan Rivera",
            "email": "jordan.rivera@example.com",
            "visa_type": "H1B",
            "sponsorship": False,
        },
        "additional_information": {
            "primary_phone": "+1-555-0100",
            "eligible_to_work_in_us": True,
            "require_future_sponsorship": False,
            "willing_to_relocate": True,
            "experience": 6,
            "desired_start_date": "2026-09-01",
            "resume_url": "https://files.applywizz.example/resumes/cand-001.pdf",
            "linked_in_url": "https://linkedin.com/in/jordanrivera",
            "github_url": "https://github.com/jordanrivera",
        },
    }


# 1. complete candidate response
def test_complete_response_maps_every_field():
    result = normalize_candidate_payload(_complete_payload())
    assert result.status == CandidateFetchStatus.SUCCESS
    p = result.profile
    assert p.candidate_id == "cand-001"
    assert p.name == "Jordan Rivera"
    assert p.email == "jordan.rivera@example.com"
    assert p.phone == "+1-555-0100"
    assert p.visa_type == "H1B"
    assert p.work_authorized is True
    assert p.requires_sponsorship is False
    assert p.willing_to_relocate is True
    assert p.experience_years == 6
    assert p.desired_start_date == "2026-09-01"
    assert p.resume_url == "https://files.applywizz.example/resumes/cand-001.pdf"
    assert p.linkedin_url == "https://linkedin.com/in/jordanrivera"
    assert p.github_url == "https://github.com/jordanrivera"
    assert p.location is None  # no documented source field -- never guessed


# 2. missing optional field
def test_missing_optional_field_stays_none():
    payload = _complete_payload()
    del payload["additional_information"]["desired_start_date"]
    result = normalize_candidate_payload(payload)
    assert result.status == CandidateFetchStatus.SUCCESS
    assert result.profile.desired_start_date is None


# 3. null boolean remains None
def test_null_boolean_remains_none():
    payload = _complete_payload()
    payload["additional_information"]["eligible_to_work_in_us"] = None
    result = normalize_candidate_payload(payload)
    assert result.profile.work_authorized is None


# 4. explicit False remains False
def test_explicit_false_remains_false():
    payload = _complete_payload()
    payload["additional_information"]["willing_to_relocate"] = False
    result = normalize_candidate_payload(payload)
    assert result.profile.willing_to_relocate is False


# 5. explicit True remains True
def test_explicit_true_remains_true():
    payload = _complete_payload()
    payload["additional_information"]["willing_to_relocate"] = True
    result = normalize_candidate_payload(payload)
    assert result.profile.willing_to_relocate is True


# 6. missing sponsorship does not become False
def test_missing_sponsorship_stays_none_not_false():
    payload = _complete_payload()
    del payload["additional_information"]["require_future_sponsorship"]
    del payload["client"]["sponsorship"]
    result = normalize_candidate_payload(payload)
    assert result.profile.requires_sponsorship is None


def test_sponsorship_falls_back_to_client_when_additional_missing():
    payload = _complete_payload()
    del payload["additional_information"]["require_future_sponsorship"]
    payload["client"]["sponsorship"] = True
    result = normalize_candidate_payload(payload)
    assert result.profile.requires_sponsorship is True


# 7. missing relocation does not become False
def test_missing_relocation_stays_none_not_false():
    payload = _complete_payload()
    del payload["additional_information"]["willing_to_relocate"]
    result = normalize_candidate_payload(payload)
    assert result.profile.willing_to_relocate is None


# 8. malformed experience value
def test_malformed_experience_value_stays_none():
    payload = _complete_payload()
    payload["additional_information"]["experience"] = "five years"
    result = normalize_candidate_payload(payload)
    assert result.status == CandidateFetchStatus.SUCCESS
    assert result.profile.experience_years is None


# 9. missing candidate_id
def test_missing_candidate_id_is_invalid_response():
    payload = _complete_payload()
    del payload["client"]["id"]
    result = normalize_candidate_payload(payload)
    assert result.status == CandidateFetchStatus.INVALID_RESPONSE
    assert result.profile is None


class _FakeResponse:
    def __init__(self, status_code: int, json_data=None, raise_on_json: bool = False):
        self.status_code = status_code
        self._json_data = json_data
        self._raise_on_json = raise_on_json

    def json(self):
        if self._raise_on_json:
            raise ValueError("not JSON")
        return self._json_data


def test_fetch_candidate_requires_base_url(monkeypatch):
    monkeypatch.delenv("APPLYWIZZ_API_BASE_URL", raising=False)
    result = fetch_candidate("cand-001")
    assert result.status == CandidateFetchStatus.UPSTREAM_ERROR
    assert "APPLYWIZZ_API_BASE_URL" in result.error


# 10. upstream 404
def test_fetch_candidate_404_is_not_found(monkeypatch):
    monkeypatch.setenv("APPLYWIZZ_API_BASE_URL", "https://api.applywizz.example")
    monkeypatch.setattr(candidate_adapter.requests, "get", lambda *a, **k: _FakeResponse(404))
    result = fetch_candidate("does-not-exist")
    assert result.status == CandidateFetchStatus.NOT_FOUND
    assert result.profile is None


# 11. upstream auth error
def test_fetch_candidate_401_is_auth_error(monkeypatch):
    monkeypatch.setenv("APPLYWIZZ_API_BASE_URL", "https://api.applywizz.example")
    monkeypatch.setattr(candidate_adapter.requests, "get", lambda *a, **k: _FakeResponse(401))
    result = fetch_candidate("cand-001")
    assert result.status == CandidateFetchStatus.AUTH_ERROR


def test_fetch_candidate_403_is_auth_error(monkeypatch):
    monkeypatch.setenv("APPLYWIZZ_API_BASE_URL", "https://api.applywizz.example")
    monkeypatch.setattr(candidate_adapter.requests, "get", lambda *a, **k: _FakeResponse(403))
    result = fetch_candidate("cand-001")
    assert result.status == CandidateFetchStatus.AUTH_ERROR


# 12. upstream timeout
def test_fetch_candidate_timeout_is_upstream_error(monkeypatch):
    monkeypatch.setenv("APPLYWIZZ_API_BASE_URL", "https://api.applywizz.example")

    def raise_timeout(*a, **k):
        raise requests.Timeout("timed out")

    monkeypatch.setattr(candidate_adapter.requests, "get", raise_timeout)
    result = fetch_candidate("cand-001")
    assert result.status == CandidateFetchStatus.UPSTREAM_ERROR
    assert result.profile is None


def test_fetch_candidate_connection_error_is_upstream_error(monkeypatch):
    monkeypatch.setenv("APPLYWIZZ_API_BASE_URL", "https://api.applywizz.example")

    def raise_conn_error(*a, **k):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(candidate_adapter.requests, "get", raise_conn_error)
    result = fetch_candidate("cand-001")
    assert result.status == CandidateFetchStatus.UPSTREAM_ERROR


def test_fetch_candidate_server_error_is_upstream_error(monkeypatch):
    monkeypatch.setenv("APPLYWIZZ_API_BASE_URL", "https://api.applywizz.example")
    monkeypatch.setattr(candidate_adapter.requests, "get", lambda *a, **k: _FakeResponse(500))
    result = fetch_candidate("cand-001")
    assert result.status == CandidateFetchStatus.UPSTREAM_ERROR


# 13. malformed JSON
def test_fetch_candidate_malformed_json_is_invalid_response(monkeypatch):
    monkeypatch.setenv("APPLYWIZZ_API_BASE_URL", "https://api.applywizz.example")
    monkeypatch.setattr(candidate_adapter.requests, "get", lambda *a, **k: _FakeResponse(200, raise_on_json=True))
    result = fetch_candidate("cand-001")
    assert result.status == CandidateFetchStatus.INVALID_RESPONSE


def test_fetch_candidate_success_path(monkeypatch):
    monkeypatch.setenv("APPLYWIZZ_API_BASE_URL", "https://api.applywizz.example")
    monkeypatch.setattr(
        candidate_adapter.requests, "get", lambda *a, **k: _FakeResponse(200, json_data=_complete_payload())
    )
    result = fetch_candidate("cand-001")
    assert result.status == CandidateFetchStatus.SUCCESS
    assert result.profile.candidate_id == "cand-001"


# 14. extra unknown source fields ignored safely
def test_extra_unknown_source_fields_ignored():
    payload = _complete_payload()
    payload["client"]["some_future_field_we_dont_know_about"] = "unexpected"
    payload["additional_information"]["another_new_field"] = {"nested": True}
    payload["a_totally_unrelated_top_level_key"] = "ignored"
    result = normalize_candidate_payload(payload)
    assert result.status == CandidateFetchStatus.SUCCESS
    assert result.profile.candidate_id == "cand-001"


# 15. source field renaming/mapping
def test_eligible_to_work_in_us_maps_to_work_authorized():
    payload = _complete_payload()
    payload["additional_information"]["eligible_to_work_in_us"] = False
    result = normalize_candidate_payload(payload)
    assert result.profile.work_authorized is False


def test_linked_in_url_maps_to_linkedin_url():
    payload = _complete_payload()
    payload["additional_information"]["linked_in_url"] = "https://linkedin.com/in/renamed-check"
    result = normalize_candidate_payload(payload)
    assert result.profile.linkedin_url == "https://linkedin.com/in/renamed-check"


# 16. nested source response
def test_flat_non_nested_payload_is_invalid_response():
    flat_payload = {"id": "cand-001", "full_name": "Jordan Rivera"}  # no client/additional_information nesting
    result = normalize_candidate_payload(flat_payload)
    assert result.status == CandidateFetchStatus.INVALID_RESPONSE


def test_missing_additional_information_object_defaults_to_all_unknown():
    payload = {"client": {"id": "cand-001", "full_name": "Jordan Rivera"}}
    result = normalize_candidate_payload(payload)
    assert result.status == CandidateFetchStatus.SUCCESS
    assert result.profile.phone is None
    assert result.profile.work_authorized is None
    assert result.profile.experience_years is None


# 17. no secret/token logging
def test_token_never_appears_in_error_results(monkeypatch):
    monkeypatch.setenv("APPLYWIZZ_API_BASE_URL", "https://api.applywizz.example")
    monkeypatch.setenv("APPLYWIZZ_API_TOKEN", "super-secret-token-value-12345")

    def raise_conn_error(*a, **k):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(candidate_adapter.requests, "get", raise_conn_error)
    result = fetch_candidate("cand-001")
    assert "super-secret-token-value-12345" not in (result.error or "")
    assert "super-secret-token-value-12345" not in repr(result)


# 18. no candidate facts invented
def test_empty_additional_information_invents_nothing():
    payload = {"client": {"id": "cand-001"}, "additional_information": {}}
    result = normalize_candidate_payload(payload)
    assert result.status == CandidateFetchStatus.SUCCESS
    p = result.profile
    assert p.name is None
    assert p.email is None
    assert p.phone is None
    assert p.visa_type is None
    assert p.work_authorized is None
    assert p.requires_sponsorship is None
    assert p.willing_to_relocate is None
    assert p.experience_years is None
    assert p.desired_start_date is None
    assert p.resume_url is None
    assert p.linkedin_url is None
    assert p.github_url is None


# ── Question-engine integration contract (Part 6) ─────────────────────────


def test_resolve_candidate_field_returns_non_sensitive_value():
    result = normalize_candidate_payload(_complete_payload())
    assert resolve_candidate_field(result.profile, "email") == "jordan.rivera@example.com"


def test_resolve_candidate_field_excludes_sensitive_fields():
    result = normalize_candidate_payload(_complete_payload())
    candidate = result.profile
    assert resolve_candidate_field(candidate, "visa_type") is None
    assert resolve_candidate_field(candidate, "work_authorized") is None
    assert resolve_candidate_field(candidate, "requires_sponsorship") is None
    # confirm the underlying field really does have a value -- the None
    # above is the accessor refusing, not the data being absent
    assert candidate.visa_type == "H1B"
    assert candidate.work_authorized is True


def test_resolve_candidate_field_unknown_name_returns_none():
    result = normalize_candidate_payload(_complete_payload())
    assert resolve_candidate_field(result.profile, "desired_salary") is None
