"""Phase 7.7: one easy-to-read timing report per application, built
entirely from the existing durable event tables (application_events,
attention_events) -- no new observability framework, no new table.

Exists so a slow real run is measurable in seconds, not guessed at from
memory (the exact problem tonight's one-hour Apply->Submitted gap
caused, before it turned out to be three separate debugging detours
rather than the pipeline itself being slow).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from db.supabase_client import get_supabase_client


def _label_for_application_event(row: dict[str, Any]) -> str:
    return row["event_type"].upper()


def _label_for_attention_event(row: dict[str, Any]) -> str | None:
    direction = row["direction"]
    message_type = row["message_type"]
    action = row.get("action")
    if direction == "INBOUND" and action in ("APPLY", "SKIP"):
        return f"{action}_RECEIVED"
    if direction == "OUTBOUND" and message_type in (
        "APPLY_ACK", "SKIP_ACK", "SUBMISSION_SUCCESS", "SUBMISSION_FAILURE", "READY_TO_SUBMIT",
    ):
        return f"{message_type}_SENT"
    return None


def get_application_timeline(application_id: str) -> list[dict[str, Any]]:
    """Every timestamped milestone for one application, merged from both
    event tables and sorted chronologically. Each entry: {label, at}."""
    client = get_supabase_client()
    timeline: list[dict[str, Any]] = []

    app_events = (
        client.table("application_events").select("event_type, created_at").eq("application_id", application_id).execute().data
        or []
    )
    for row in app_events:
        timeline.append({"label": _label_for_application_event(row), "at": row["created_at"]})

    attn_events = (
        client.table("attention_events")
        .select("direction, message_type, action, created_at")
        .eq("application_id", application_id)
        .execute()
        .data
        or []
    )
    for row in attn_events:
        label = _label_for_attention_event(row)
        if label is not None:
            timeline.append({"label": label, "at": row["created_at"]})

    timeline.sort(key=lambda e: e["at"])
    return timeline


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def format_latency_report(application_id: str) -> str:
    """Human-readable "APPLICATION LATENCY" report: each consecutive
    milestone pair and the gap between them, plus the total span."""
    timeline = get_application_timeline(application_id)
    if not timeline:
        return "APPLICATION LATENCY\n\n(no events recorded yet)"

    lines = ["APPLICATION LATENCY", ""]
    for i in range(1, len(timeline)):
        prev, curr = timeline[i - 1], timeline[i]
        gap = (_parse(curr["at"]) - _parse(prev["at"])).total_seconds()
        lines.append(f"{prev['label']} -> {curr['label']}: {gap:.1f}s")

    total = (_parse(timeline[-1]["at"]) - _parse(timeline[0]["at"])).total_seconds()
    lines.append("")
    lines.append(f"TOTAL ({timeline[0]['label']} -> {timeline[-1]['label']}): {total:.1f}s")
    return "\n".join(lines)
