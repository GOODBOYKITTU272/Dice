"""Environment-driven Supabase client factory for DicePilot code.

Separate from the existing Indeed code path (main.py) on purpose: this
module is the only place DicePilot reads Supabase credentials, and it
fails fast if they're missing instead of crashing deep inside a worker.

SUPABASE_SERVICE_ROLE_KEY is server-side only — this factory is for
backend/worker code (Flask routes running server-side, the Dice worker,
tests). It must never be bundled into or shipped to any browser-facing
code. If a browser-facing Supabase client is introduced later, it needs
its own SUPABASE_ANON_KEY and its own factory — not this one.
"""
import os

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

_client: Client | None = None


class MissingSupabaseConfigError(RuntimeError):
    pass


def get_supabase_client() -> Client:
    """Return a shared Supabase client built from SUPABASE_URL /
    SUPABASE_SERVICE_ROLE_KEY. Server-side use only.
    """
    global _client
    if _client is not None:
        return _client

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise MissingSupabaseConfigError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set (env or .env) "
            "before any DicePilot Supabase access."
        )

    _client = create_client(url, key)
    return _client
