"""Thin boundary onto jobspy_enhanced.dice.util — Phase 3A.

IMPORTANT — what this module deliberately does NOT do:
Never imports jobspy_enhanced.dice.Dice, never calls .scrape() or
._fetch_job_details(). Confirmed by reading the installed 1.3.7 source:
Dice._fetch_job_details() unconditionally calls _apply_w2_c2c_and_link()
on every successful parse path, which both infers Easy Apply from the
*absence* of an external URL and makes a live GET request to Dice's own
/job-applications/{id}/start-apply apply-initiation URL to resolve
redirects. Both are explicitly prohibited for DicePilot's discovery
system. There is no supported way to use the Dice class without also
triggering that behavior, so this module only imports free-standing
utility functions from jobspy_enhanced.dice.util and jobspy_enhanced.util
— each independently audited, none of them touching an apply-adjacent
URL or making network requests of their own.

Everything here is read-only text/dict parsing. Nothing in this module
performs an HTTP request.
"""
from __future__ import annotations

from typing import Any

from jobspy_enhanced.dice import util as upstream_util


def try_next_data(soup: Any) -> dict[str, Any] | None:
    """__NEXT_DATA__ extraction — pure parsing, no request. Returns None if
    the page doesn't have this script tag (common; Dice's current site may
    not emit it — see dice/job_parser.py for the fallback chain)."""
    return upstream_util.extract_from_next_data(soup)


def clean_description(raw_description: str) -> str:
    """Unicode-unescape + HTML-strip + whitespace cleanup. Upstream's
    version handles unicode-escaped description text (e.g. \\u2019) that
    our own tag-strip-only cleaner didn't handle."""
    return upstream_util.clean_description(raw_description or "")


def extract_salary_text(description: str, job_data: dict[str, Any] | None = None) -> str | None:
    """Best-effort salary text from structured job_data first, description
    regex second. Returns a short human-readable string, or None — this is
    metadata for raw_metadata, never used for C2C/qualification decisions."""
    if job_data:
        comp = upstream_util.extract_salary_from_json(job_data)
        if comp:
            return _format_compensation(comp)
    comp = upstream_util.extract_salary_from_description(description or "")
    if comp:
        return _format_compensation(comp)
    return None


def _format_compensation(comp: Any) -> str:
    parts = [comp.currency or "USD"]
    if comp.max_amount and comp.max_amount != comp.min_amount:
        parts.append(f"{comp.min_amount:g}-{comp.max_amount:g}")
    else:
        parts.append(f"{comp.min_amount:g}")
    if comp.interval:
        parts.append(comp.interval.value)
    return " ".join(str(p) for p in parts)


def extract_experience_text(description: str) -> str | None:
    """Best-effort experience text from description regex. Metadata only —
    never used for C2C/qualification decisions."""
    return upstream_util.extract_experience_from_description(description or "")
