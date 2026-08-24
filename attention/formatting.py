"""Phase 7.5b: the one shared piece of message-copy logic both providers
need -- deriving the "C2C • Easy Apply" metadata line from the job's own
stored fields (dice_jobs.c2c_status/is_easy_apply) rather than hardcoding
it, since not every job is actually both."""
from __future__ import annotations


def job_metadata_line(job: dict) -> str:
    parts = []
    if job.get("c2c_status") in ("CONFIRMED", "LIKELY"):
        parts.append("C2C")
    if job.get("is_easy_apply"):
        parts.append("Easy Apply")
    return " • ".join(parts)
