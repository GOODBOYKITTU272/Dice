"""Easy Apply detection — detection only. Never clicks Apply.

Dice explicitly labels certain jobs "Easy Apply" in its own UI (a badge
rendered on both the search card and the job detail page). We treat that
label as authoritative rather than inferring it from employment type or
company, per "do not assume all Contract jobs are Easy Apply."

Two independent readings are taken — the search-results card and the job
detail page — and cross-checked rather than trusting either alone
(TRD: "do not classify an external redirect as Dice Easy Apply").
"""
from __future__ import annotations

from dice.models import EasyApplyResult


def detect_easy_apply(
    search_card_easy_apply: bool,
    detail_page_easy_apply: bool | None = None,
) -> EasyApplyResult:
    if detail_page_easy_apply is None:
        return EasyApplyResult(
            is_easy_apply=search_card_easy_apply,
            evidence={
                "source": "search_card_only",
                "search_card_badge": search_card_easy_apply,
                "detail_page_checked": False,
            },
        )

    if search_card_easy_apply != detail_page_easy_apply:
        # Disagreement: trust the detail page (closer to the real apply
        # flow) but never silently — record the conflict as evidence.
        return EasyApplyResult(
            is_easy_apply=detail_page_easy_apply,
            evidence={
                "source": "detail_page_overrides_search_card",
                "search_card_badge": search_card_easy_apply,
                "detail_page_badge": detail_page_easy_apply,
                "conflict": True,
            },
        )

    return EasyApplyResult(
        is_easy_apply=search_card_easy_apply,
        evidence={
            "source": "search_card_and_detail_page_agree",
            "search_card_badge": search_card_easy_apply,
            "detail_page_badge": detail_page_easy_apply,
            "conflict": False,
        },
    )
