"""Phase 7.10: automatic resume delivery for the deployed worker.

Real live finding: a Railway Start Command that ran `touch resume.pdf`
"solved" the mandatory resume check by creating an empty file, and a
Start Command pointing at a Mac-only path solved nothing at all -- the
worker needs the actual resume bytes, resolved automatically by the
service itself, never created manually in a deploy command.

The candidate's resume lives in Supabase Storage (the same project
already used for every other piece of durable state) under
`resumes/<candidate_id>/resume.pdf`. On worker startup, if the
configured destination path doesn't already have a real (non-empty)
file, this downloads it there once -- after that, the existing
mandatory startup readiness check (dice_browser.worker_daemon.
check_startup_readiness) validates it exactly like any other resume
path, no special-casing needed downstream.
"""
from __future__ import annotations

from pathlib import Path

RESUME_BUCKET = "resumes"


def resume_exists_in_storage(candidate_id: str) -> bool:
    """Phase 8A (readiness gate): a cheap existence+size check against
    Storage metadata only -- never downloads bytes. Used by readiness.py
    to answer "is there a usable resume" before offering a job, from a
    process that has no reason to also write a local worker file.
    Never raises for a missing/unreachable resume -- the caller's own
    readiness check turns a False into a clear NOT_OFFERABLE reason."""
    try:
        from db.supabase_client import get_supabase_client

        client = get_supabase_client()
        files = client.storage.from_(RESUME_BUCKET).list(candidate_id)
    except Exception:
        return False
    for f in files or []:
        if f.get("name") == "resume.pdf" and (f.get("metadata") or {}).get("size", 0) > 0:
            return True
    return False


def ensure_resume_available(candidate_id: str, destination_path: str) -> bool:
    """Downloads the candidate's resume from Supabase Storage to
    `destination_path` if it isn't already there as a real file.
    Returns True if a usable (non-empty) file exists at that path
    afterward, False otherwise -- never raises for a missing/unreachable
    resume, since the caller's own readiness check is what turns that
    into a clear startup failure."""
    dest = Path(destination_path)
    if dest.is_file() and dest.stat().st_size > 0:
        return True

    try:
        from db.supabase_client import get_supabase_client

        client = get_supabase_client()
        data = client.storage.from_(RESUME_BUCKET).download(f"{candidate_id}/resume.pdf")
    except Exception:
        return False

    if not data:
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest.is_file() and dest.stat().st_size > 0
