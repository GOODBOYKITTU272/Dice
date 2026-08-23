"""Phase 7.3: Steel session lifecycle -- the smallest thing that lets the
worker daemon create/reuse/recover a Steel Browser session automatically,
without a human ever needing to open the Steel viewer first.

The Steel viewer (localhost:5173, or its tunneled cloud equivalent) is
OBSERVABILITY ONLY: it attaches to whatever session already exists. It
never creates or keeps that session alive -- closing it has zero effect
on the Steel service, the worker, or in-flight application processing.
The durable identity is the persistent Chrome profile
(DICEPILOT_BROWSER_PROFILE_DIR / Steel's own CHROME_USER_DATA_DIR), not
any particular session ID, which is disposable and expected to change
across recoveries.
"""
from __future__ import annotations

import os

import requests

STEEL_BASE_URL_ENV_VAR = "STEEL_BASE_URL"
_DEFAULT_TIMEOUT_SECONDS = 10.0


class SteelUnavailableError(RuntimeError):
    pass


def resolve_steel_base_url(cdp_url: str) -> str:
    """STEEL_BASE_URL overrides; otherwise derived from the ws:// CDP URL
    (Steel's self-hosted websocketUrl is just its own base URL with the
    scheme swapped -- confirmed during the compatibility spike), so a
    normal single-service Steel deployment never needs to configure both
    separately."""
    explicit = os.environ.get(STEEL_BASE_URL_ENV_VAR)
    if explicit:
        return explicit
    return cdp_url.replace("wss://", "https://").replace("ws://", "http://")


def steel_api_healthy(base_url: str, timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> bool:
    try:
        resp = requests.get(f"{base_url.rstrip('/')}/v1/sessions", timeout=timeout)
        return resp.ok
    except requests.RequestException:
        return False


def list_live_sessions(base_url: str, timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> list[dict]:
    resp = requests.get(f"{base_url.rstrip('/')}/v1/sessions", timeout=timeout)
    resp.raise_for_status()
    return resp.json().get("sessions", [])


def ensure_steel_session(base_url: str, timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Reuses a live session if one exists; otherwise creates one via the
    exact same API call Steel's own viewer UI makes. This is what makes
    "operator must open the Steel viewer first" unnecessary for normal
    operation -- the worker can always get itself a session on its own."""
    try:
        sessions = list_live_sessions(base_url, timeout=timeout)
    except requests.RequestException as exc:
        raise SteelUnavailableError(f"Steel API not reachable at {base_url}") from exc

    live = [s for s in sessions if s.get("status") == "live"]
    if live:
        return live[0]

    try:
        resp = requests.post(f"{base_url.rstrip('/')}/v1/sessions", json={}, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise SteelUnavailableError(f"Steel API not reachable at {base_url}") from exc
    return resp.json()
