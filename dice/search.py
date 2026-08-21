"""Dice search results discovery — plain HTTP, no browser.

Dice's public search page (https://www.dice.com/jobs) is server-rendered
with real job data in the initial HTML response (confirmed by inspection:
each result is a `data-testid="job-card"` block with a stable
`data-job-guid`, title link, company name, location, an employment-type
badge, and — when applicable — an "Easy Apply" badge). This is public,
unauthenticated search-result content, the same thing a browser or search
engine crawler receives; no login, no API key, no bypass of any kind.

The Dice job-search JSON API (job-search-api.svc.dhigroupinc.com) returned
403 on inspection — it needs an internal key not meant for third-party use,
so we deliberately don't touch it. HTML discovery via the public search
page is sufficient, per "prefer HTTP/API discovery if sufficient."

Uses Dice's own `filters.employmentType=CONTRACTS|THIRD_PARTY` search
filter so "Contract / Third Party eligibility" is enforced by Dice itself,
not guessed after the fact.
"""
from __future__ import annotations

from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from dice.models import RawSearchResult

SEARCH_URL = "https://www.dice.com/jobs"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT_SECONDS = 20
RESULTS_PER_PAGE_CAP = 30  # observed page size; used only to bound pagination
MAX_PAGES = 5  # hard safety cap so a large "max results" can't page forever


class DiceSearchError(RuntimeError):
    pass


def _build_url(role: str, page: int, location: str, country_code: str) -> str:
    params = {
        "q": role,
        "location": location,
        "countryCode": country_code,
        "radius": "30",
        "radiusUnit": "mi",
        "page": str(page),
        "filters.employmentType": "CONTRACTS|THIRD_PARTY",
        "language": "en",
    }
    query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
    return f"{SEARCH_URL}?{query}"


def _parse_job_cards(html: str) -> list[RawSearchResult]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[RawSearchResult] = []

    for card in soup.find_all(attrs={"data-testid": "job-card"}):
        guid = card.get("data-job-guid")
        if not guid:
            continue

        title_el = card.find(attrs={"data-testid": "job-search-job-detail-link"})
        title = title_el.get_text(strip=True) if title_el else ""

        company_el = card.find(attrs={"data-testid": "job-card-company-name"})
        company_name = company_el.get_text(strip=True) if company_el else None

        location_el = card.select_one('p[class*="text-zinc-600"]')
        location_text = None
        if location_el:
            raw = location_el.get_text(" ", strip=True)
            location_text = raw.split("•")[0].strip() or None

        employment_el = card.find(id="employmentType-label")
        employment_type_text = employment_el.get_text(strip=True) if employment_el else ""

        easy_apply_present = card.find(id="easyApply-label") is not None

        if not title:
            continue

        results.append(
            RawSearchResult(
                dice_job_id=guid,
                title=title,
                company_name=company_name,
                location=location_text,
                canonical_url=f"https://www.dice.com/job-detail/{guid}",
                employment_type_text=employment_type_text,
                easy_apply_badge_present=easy_apply_present,
            )
        )

    return results


def search_dice_jobs(
    role: str,
    max_results: int,
    location: str = "United States",
    country_code: str = "US",
) -> list[RawSearchResult]:
    """Search Dice for `role`, restricted to Contract/Third Party, up to
    max_results jobs. Paginates only as far as needed."""
    if max_results <= 0:
        return []

    collected: list[RawSearchResult] = []
    seen_ids: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        url = _build_url(role, page, location, country_code)
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
            raise DiceSearchError(
                f"Dice search returned HTTP {response.status_code} for page {page}"
            )

        page_results = _parse_job_cards(response.text)
        if not page_results:
            break

        for result in page_results:
            if result.dice_job_id in seen_ids:
                continue
            seen_ids.add(result.dice_job_id)
            collected.append(result)
            if len(collected) >= max_results:
                return collected

        if len(page_results) < RESULTS_PER_PAGE_CAP:
            break  # last page

    return collected
