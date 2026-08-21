"""Offline parsing tests — no live Dice request, using saved fixture HTML.

Covers: discovery model normalization (search card + job detail parsing
into the typed models), duplicate Dice job handling (search-page dedup),
and Easy Apply signal extraction from real page structure.
"""
from pathlib import Path

import dice.search as search_module
from dice.job_parser import parse_job_detail_html
from dice.search import _parse_job_cards, search_dice_jobs

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ── search results page parsing (model normalization) ──────────────────────


def test_parse_job_cards_normalizes_fields():
    html = _read("search_page_sample.html")
    results = _parse_job_cards(html)

    assert len(results) == 3  # includes the intentional duplicate card

    first = results[0]
    assert first.dice_job_id == "11111111-1111-1111-1111-111111111111"
    assert first.title == "Senior Backend Engineer"
    assert first.company_name == "Acme Staffing Group"
    assert first.location == "Austin, Texas"
    assert first.canonical_url == "https://www.dice.com/job-detail/11111111-1111-1111-1111-111111111111"
    assert first.employment_type_text == "Contract, Third Party"
    assert first.easy_apply_badge_present is True

    second = results[1]
    assert second.dice_job_id == "22222222-2222-2222-2222-222222222222"
    assert second.easy_apply_badge_present is False
    assert second.employment_type_text == "Contract"


# ── duplicate Dice job handling ──────────────────────────────────────────


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


def test_search_dice_jobs_deduplicates_repeated_guid(monkeypatch):
    html = _read("search_page_sample.html")

    def fake_get(url, headers=None, timeout=None):
        return _FakeResponse(html)

    monkeypatch.setattr(search_module.requests, "get", fake_get)

    results = search_dice_jobs("Software Engineer", max_results=10)

    # 3 cards in the fixture, but one guid repeats -> 2 unique jobs.
    assert len(results) == 2
    ids = [r.dice_job_id for r in results]
    assert ids == ["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"]


def test_search_dice_jobs_respects_max_results(monkeypatch):
    html = _read("search_page_sample.html")

    def fake_get(url, headers=None, timeout=None):
        return _FakeResponse(html)

    monkeypatch.setattr(search_module.requests, "get", fake_get)

    results = search_dice_jobs("Software Engineer", max_results=1)
    assert len(results) == 1


# ── job detail page parsing (model normalization) ──────────────────────────


def test_parse_job_detail_extracts_structured_data():
    html = _read("job_detail_c2c_positive.html")
    detail = parse_job_detail_html(html, fallback_url="https://www.dice.com/job-detail/11111111-1111-1111-1111-111111111111")

    assert detail.title == "Senior Backend Engineer"
    assert "Corp to Corp" in detail.description_text
    assert "<p>" not in detail.description_text  # HTML stripped for classification/storage
    assert detail.employment_type == "CONTRACTOR"
    assert detail.company_name == "Acme Staffing Group"


def test_parse_job_detail_negative_evidence_description():
    html = _read("job_detail_c2c_negative.html")
    detail = parse_job_detail_html(html, fallback_url="https://www.dice.com/job-detail/22222222-2222-2222-2222-222222222222")

    assert "W2 only" in detail.description_text
    assert "W2 only" in detail.description_text
