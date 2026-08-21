"""Deterministic Phase 2 qualification. No LLM, no guessing.

Derives QUALIFIED entirely from fields already on a dice_jobs row
(employment_type, is_third_party, c2c_status, is_easy_apply) — no new
column, no migration. See STATE.md for why: every value qualification
needs already exists from Phase 1/2.

QUALIFIED is a read-time judgment only. It does NOT enqueue an
application and is NOT written to Supabase — recomputing it from the same
four fields always gives the same answer, so persisting it would just be
a second copy of the same fact that could drift out of sync.
"""
from __future__ import annotations

from dataclasses import dataclass

C2C_ELIGIBLE_STATUSES = {"CONFIRMED", "LIKELY"}


def is_contract(employment_type_text: str | None) -> bool:
    """True if the raw employment-type text contains "Contract" as one of
    its comma-separated values — matches "Contract" and "Full-time,
    Contract", not "Full-time" alone."""
    parts = (employment_type_text or "").lower().split(",")
    return any(part.strip() == "contract" for part in parts)


@dataclass
class QualificationResult:
    is_qualified: bool
    reason: str


def qualify_job(
    employment_type_text: str | None,
    is_third_party: bool,
    c2c_status: str,
    is_easy_apply: bool,
) -> QualificationResult:
    """A job is QUALIFIED only when all three hold:
      1. employment funnel eligible: Contract OR a true Third Party signal
      2. c2c_status in {CONFIRMED, LIKELY}
      3. is_easy_apply is True
    Contract does NOT imply Third Party, and Third Party is never inferred
    from Contract alone — is_third_party must already be its own explicit
    signal (set by the C2C/employment-type parsing, not derived here).
    """
    funnel_ok = is_contract(employment_type_text) or bool(is_third_party)
    c2c_ok = c2c_status in C2C_ELIGIBLE_STATUSES
    easy_apply_ok = bool(is_easy_apply)

    if funnel_ok and c2c_ok and easy_apply_ok:
        return QualificationResult(is_qualified=True, reason="eligible")

    reasons: list[str] = []
    if not funnel_ok:
        reasons.append("Not Contract/Third Party")
    if c2c_status == "UNKNOWN":
        reasons.append("C2C unknown")
    elif c2c_status == "NOT_C2C":
        reasons.append("Not C2C")
    elif not c2c_ok:
        reasons.append(f"C2C status {c2c_status} not eligible")
    if not easy_apply_ok:
        reasons.append("Not Easy Apply")

    return QualificationResult(is_qualified=False, reason="; ".join(reasons))
