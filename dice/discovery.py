"""Phase 2 discovery pipeline: search -> detail -> classify -> save.

Discovery only. Never clicks Apply, never uploads a resume, never answers
an application question, never talks to a candidate API. That's all out of
scope for Phase 2 by design (see STATE.md).
"""
from __future__ import annotations

from typing import Any, Callable

from db.application_repository import upsert_dice_job
from dice.c2c_classifier import classify_c2c
from dice.easy_apply_detector import detect_easy_apply
from dice.job_parser import DiceJobDetailError, fetch_job_detail
from dice.models import DiceJobRecord, now_iso
from dice.qualification import is_contract, qualify_job
from dice.search import DiceSearchError, search_dice_jobs

ProgressPrinter = Callable[[str], None]


def _default_printer(line: str) -> None:
    # flush=True so progress is visible immediately in a redirected/piped
    # terminal (e.g. the Flask dev server's log), not stuck in a buffer
    # until the process happens to flush on its own.
    print(line, flush=True)


def run_discovery(
    role: str,
    max_results: int = 5,
    location: str = "United States",
    printer: ProgressPrinter = _default_printer,
) -> list[dict[str, Any]]:
    """Run the full discovery pipeline and return one summary dict per job
    (the same shape the local UI renders as table rows)."""
    printer(f"\nDice discovery — role={role!r} max_results={max_results} location={location!r}")
    printer("=" * 70)

    try:
        raw_results = search_dice_jobs(role, max_results, location=location)
    except DiceSearchError as exc:
        printer(f"SEARCH FAILED: {exc}")
        return []

    if not raw_results:
        printer("No jobs found for this role/filter combination.")
        return []

    rows: list[dict[str, Any]] = []

    for i, raw in enumerate(raw_results, 1):
        is_third_party = "third party" in raw.employment_type_text.lower()
        row: dict[str, Any] = {
            "dice_job_id": raw.dice_job_id,
            "title": raw.title,
            "company_name": raw.company_name,
            "location": raw.location,
            "canonical_url": raw.canonical_url,
            "employment_type": raw.employment_type_text,
            "is_third_party": is_third_party,
            "is_contract": is_contract(raw.employment_type_text),
        }

        try:
            detail = fetch_job_detail(raw.dice_job_id)
        except DiceJobDetailError as exc:
            printer(f"[{i}/{len(raw_results)}] Found: {raw.title} — {raw.company_name}")
            printer(f"      ERROR fetching detail: {exc}")
            printer("      Saved: NO (detail fetch failed)")
            qual = qualify_job(raw.employment_type_text, is_third_party, "UNKNOWN", raw.easy_apply_badge_present)
            row.update(
                {
                    "c2c_status": "UNKNOWN",
                    "c2c_reason": f"detail fetch failed: {exc}",
                    "c2c_evidence": [],
                    "easy_apply": raw.easy_apply_badge_present,
                    "discovery_status": "ERROR",
                    "is_qualified": qual.is_qualified,
                    "qualification_reason": qual.reason,
                }
            )
            rows.append(row)
            continue

        c2c = classify_c2c(detail.description_text, raw.employment_type_text)
        # Search-card badge only — see dice/job_parser.py for why the detail
        # page isn't used as a second signal (false positives from unrelated
        # "similar jobs" cards elsewhere on that page).
        easy_apply = detect_easy_apply(raw.easy_apply_badge_present)

        record = DiceJobRecord(
            dice_job_id=raw.dice_job_id,
            canonical_url=detail.canonical_url or raw.canonical_url,
            title=detail.title or raw.title,
            company_name=detail.company_name or raw.company_name,
            location=raw.location,
            employment_type=raw.employment_type_text or detail.employment_type,
            is_third_party=is_third_party,
            description=detail.description_text,
            c2c_status=c2c.status,
            c2c_reason=c2c.reason,
            is_easy_apply=easy_apply.is_easy_apply,
            easy_apply_evidence=easy_apply.evidence,
            discovered_at=now_iso(),
            raw_metadata={
                "salary_text": detail.salary_text,
                "experience_text": detail.experience_text,
            },
        )

        try:
            upsert_dice_job(record.to_row())
            discovery_status = "SAVED"
            saved_text = "YES"
        except Exception as exc:  # noqa: BLE001 - report, don't crash the whole run
            discovery_status = f"ERROR: {exc}"
            saved_text = "NO"

        qual = qualify_job(raw.employment_type_text, is_third_party, c2c.status, easy_apply.is_easy_apply)

        evidence_text = ", ".join(c2c.evidence) if c2c.evidence else "(none)"
        printer(f"[{i}/{len(raw_results)}] Found: {raw.title} — {raw.company_name}")
        printer(f"      Employment: {raw.employment_type_text or '(unspecified)'}")
        printer(f"      C2C: {c2c.status}")
        printer(f"      Evidence: {evidence_text}")
        printer(f"      Easy Apply: {'YES' if easy_apply.is_easy_apply else 'NO'}")
        if detail.salary_text or detail.experience_text:
            printer(f"      Salary: {detail.salary_text or '(none)'}  Experience: {detail.experience_text or '(none)'}")
        printer(f"      Saved: {saved_text}")
        printer(f"      Qualified: {qual.is_qualified} ({qual.reason})")

        row.update(
            {
                "c2c_status": c2c.status,
                "c2c_reason": c2c.reason,
                "c2c_evidence": c2c.evidence,
                "easy_apply": easy_apply.is_easy_apply,
                "discovery_status": discovery_status,
                "is_qualified": qual.is_qualified,
                "qualification_reason": qual.reason,
                "salary_text": detail.salary_text,
                "experience_text": detail.experience_text,
            }
        )
        rows.append(row)

    printer("=" * 70)
    _print_summary(rows, printer)
    return rows


def _print_summary(rows: list[dict[str, Any]], printer: ProgressPrinter) -> None:
    discovered = len(rows)
    contract = sum(1 for r in rows if r.get("is_contract"))
    third_party = sum(1 for r in rows if r.get("is_third_party"))
    confirmed = sum(1 for r in rows if r.get("c2c_status") == "CONFIRMED")
    likely = sum(1 for r in rows if r.get("c2c_status") == "LIKELY")
    unknown = sum(1 for r in rows if r.get("c2c_status") == "UNKNOWN")
    not_c2c = sum(1 for r in rows if r.get("c2c_status") == "NOT_C2C")
    easy_apply = sum(1 for r in rows if r.get("easy_apply"))
    stored = sum(1 for r in rows if r.get("discovery_status") == "SAVED")
    qualified = sum(1 for r in rows if r.get("is_qualified"))

    printer(
        f"Summary: Discovered={discovered} Contract={contract} ThirdParty={third_party} "
        f"C2C_Confirmed={confirmed} C2C_Likely={likely} C2C_Unknown={unknown} NOT_C2C={not_c2c} "
        f"EasyApply={easy_apply} Stored={stored} Qualified={qualified}"
    )
