"""Fetch and normalize one Dice job's detail page.

Uses the schema.org JobPosting JSON-LD block Dice embeds in every job
detail page (`<script type="application/ld+json" id="jobDetailStructuredData">`)
— the same structured data Dice publishes for Google Jobs indexing. This is
publicly intended for exactly this kind of programmatic consumption; no
scraping trick involved, just reading a standard structured-data block.

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
dice/easy_apply_detector.py.
"""
from __future__ import annotations

import json
import re

import requests
from bs4 import BeautifulSoup

from dice.models import JobDetail

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
    ld_script = soup.find("script", id="jobDetailStructuredData")
    if ld_script is None or not ld_script.string:
        raise DiceJobDetailError(f"No jobDetailStructuredData JSON-LD block found for {fallback_url}")

    try:
        data = json.loads(ld_script.string)
    except json.JSONDecodeError as exc:
        raise DiceJobDetailError(f"Could not parse JSON-LD for {fallback_url}: {exc}") from exc

    description_html = data.get("description", "") or ""
    description_text = BeautifulSoup(description_html, "html.parser").get_text(" ", strip=True)
    description_text = re.sub(r"\s+", " ", description_text).strip()

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
    )
