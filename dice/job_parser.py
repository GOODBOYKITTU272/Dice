"""Fetch and normalize one Dice job's detail page.

Two parsing tiers, in order:
  1. __NEXT_DATA__ (jobspy_enhanced.dice.util.extract_from_next_data, via
     dice/upstream_adapter.py) — tried first. Dice's site may or may not
     still emit this script tag; if it doesn't, this tier just returns
     nothing and we fall through.
  2. Our own JSON-LD parse (`<script type="application/ld+json"
     id="jobDetailStructuredData">`) — the same structured data Dice
     publishes for Google Jobs indexing, and the tier this module relied
     on exclusively before Phase 3A. Still the primary path in practice
     until live validation confirms tier 1 is actually reachable.

Both tiers are pure parsing of already-fetched HTML — no extra request per
tier, no fetch of any apply-adjacent URL.

Deliberately does NOT attempt to read an Easy Apply signal off the detail
page. Confirmed by inspection: a page-wide search for the "Easy Apply"
badge on a Dice job-detail page also matches unrelated "similar jobs"
recommendation cards further down the same page, producing false
positives for jobs that aren't actually Easy Apply. The primary job's own
apply control is client-rendered (streamed in behind a
BAILOUT_TO_CLIENT_SIDE_RENDERING placeholder) rather than present as plain
scoped HTML, so there's no reliable server-rendered element to scope a
check to. Easy Apply detection instead relies solely on the search-results
card badge (dice/search.py), which IS reliably scoped to one job. See
dice/easy_apply_detector.py. This does not change in Phase 3A — the
__NEXT_DATA__ tier is used for title/description/employment_type only,
never for an apply/Easy-Apply signal.
"""
from __future__ import annotations

import json

import requests
from bs4 import BeautifulSoup

from dice.models import JobDetail
from dice.upstream_adapter import (
    clean_description,
    extract_experience_text,
    extract_salary_text,
    try_next_data,
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT_SECONDS = 20


class DiceJobDetailError(RuntimeError):
    pass


def fetch_job_detail(dice_job_id: str) -> JobDetail:
    url = f"https://www.dice.com/job-detail/{dice_job_id}"
    response = requests.get(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise DiceJobDetailError(f"Dice job detail returned HTTP {response.status_code} for {url}")

    return parse_job_detail_html(response.text, fallback_url=url)


def parse_job_detail_html(html: str, fallback_url: str = "") -> JobDetail:
    """Pure parsing step, split out from fetch_job_detail so it's testable
    offline against saved fixture HTML — no live Dice request needed."""
    soup = BeautifulSoup(html, "html.parser")

    next_data = try_next_data(soup)
    if next_data and (next_data.get("title") or next_data.get("description")):
        detail = _from_next_data(next_data, fallback_url)
        if detail is not None:
            return detail

    return _from_json_ld(soup, fallback_url)


def _from_next_data(job_data: dict, fallback_url: str) -> JobDetail | None:
    title = job_data.get("title")
    description_raw = job_data.get("description", "") or ""
    if not title or not description_raw:
        return None  # incomplete — fall through to JSON-LD rather than ship a half-empty record

    description_text = clean_description(description_raw)
    company = job_data.get("companyName") or (job_data.get("company") or {}).get("name")
    location_raw = job_data.get("location") or (job_data.get("jobLocation") or {}).get("address", {}).get(
        "addressLocality"
    )
    employment_type = job_data.get("employmentType")
    date_posted = job_data.get("postedDate") or job_data.get("datePosted")

    return JobDetail(
        title=title,
        description_html=description_raw,
        description_text=description_text,
        employment_type=employment_type,
        company_name=company,
        date_posted=date_posted,
        canonical_url=fallback_url,
        salary_text=extract_salary_text(description_text, job_data),
        experience_text=extract_experience_text(description_text),
    )


def _from_json_ld(soup: BeautifulSoup, fallback_url: str) -> JobDetail:
    ld_script = soup.find("script", id="jobDetailStructuredData")
    if ld_script is None or not ld_script.string:
        raise DiceJobDetailError(f"No jobDetailStructuredData JSON-LD block found for {fallback_url}")

    try:
        data = json.loads(ld_script.string)
    except json.JSONDecodeError as exc:
        raise DiceJobDetailError(f"Could not parse JSON-LD for {fallback_url}: {exc}") from exc

    description_html = data.get("description", "") or ""
    description_text = clean_description(description_html)

    hiring_org = data.get("hiringOrganization") or {}
    company_name = hiring_org.get("name")

    return JobDetail(
        title=data.get("title", ""),
        description_html=description_html,
        description_text=description_text,
        employment_type=data.get("employmentType"),
        company_name=company_name,
        date_posted=data.get("datePosted"),
        canonical_url=data.get("url", fallback_url),
        salary_text=extract_salary_text(description_text, data),
        experience_text=extract_experience_text(description_text),
    )
