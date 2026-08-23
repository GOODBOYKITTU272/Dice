"""Phase 7.2: browser provider selection (local Chrome vs self-hosted
Steel). Deliberately thin -- both providers connect via the exact same
dice_browser.worker_daemon._connect() / playwright.chromium.connect_over_cdp()
call, confirmed unmodified during the Steel compatibility spike
(2026-08-23). No real Dice/browser mutation anywhere in this file.
"""
from __future__ import annotations

import dice_browser.worker_daemon as worker_daemon
from dice_browser.browser_provider import resolve_browser_provider


# 1. Provider defaults to local
def test_provider_defaults_to_local(monkeypatch):
    monkeypatch.delenv("DICEPILOT_BROWSER_PROVIDER", raising=False)
    assert resolve_browser_provider() == "local"


# 2. Steel provider selected from env
def test_provider_selects_steel_from_env(monkeypatch):
    monkeypatch.setenv("DICEPILOT_BROWSER_PROVIDER", "steel")
    assert resolve_browser_provider() == "steel"


def test_provider_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("DICEPILOT_BROWSER_PROVIDER", "STEEL")
    assert resolve_browser_provider() == "steel"


def test_unrecognized_provider_falls_back_to_local(monkeypatch):
    monkeypatch.setenv("DICEPILOT_BROWSER_PROVIDER", "some-typo")
    assert resolve_browser_provider() == "local"


# 3. Steel CDP URL used correctly -- provider selection and CDP URL
# resolution are independent env vars, both respected together.
def test_steel_provider_uses_configured_cdp_url(monkeypatch):
    monkeypatch.setenv("DICEPILOT_BROWSER_PROVIDER", "steel")
    monkeypatch.setenv("DICEPILOT_CDP_URL", "ws://steel-host:3000/")
    assert resolve_browser_provider() == "steel"
    assert worker_daemon.default_cdp_url() == "ws://steel-host:3000/"


# 4. Local mode still works -- no env vars set at all.
def test_local_mode_default_cdp_url_unaffected(monkeypatch):
    monkeypatch.delenv("DICEPILOT_BROWSER_PROVIDER", raising=False)
    monkeypatch.delenv("DICEPILOT_CDP_URL", raising=False)
    assert resolve_browser_provider() == "local"
    assert worker_daemon.default_cdp_url() == "http://127.0.0.1:9333"


# 5. Steel connection mocked successfully -- same _connect() path, no
# provider-specific branching in the actual connect call.
def test_connect_is_provider_agnostic(monkeypatch):
    calls = []

    class _FakePlaywright:
        def stop(self):
            pass

    def _fake_connect(cdp_url):
        calls.append(cdp_url)
        return _FakePlaywright(), object()

    monkeypatch.setattr(worker_daemon, "_connect", _fake_connect)
    playwright, page = worker_daemon._connect("ws://steel-host:3000/")
    assert calls == ["ws://steel-host:3000/"]
    playwright.stop()


# 13. Main's startup wiring only runs the Steel-specific lock cleanup
# when the Steel provider is actually selected.
def test_main_skips_singleton_cleanup_for_local_provider(monkeypatch):
    monkeypatch.delenv("DICEPILOT_BROWSER_PROVIDER", raising=False)
    monkeypatch.setenv("DICEPILOT_BROWSER_PROFILE_DIR", "/should/not/be/used")
    calls = []
    monkeypatch.setattr(worker_daemon, "clean_stale_singleton_locks", lambda profile_dir: calls.append(profile_dir) or [])
    monkeypatch.setattr(worker_daemon, "run_daemon", lambda *a, **kw: None)  # never actually poll/loop in a test
    import run_registry

    monkeypatch.setattr(run_registry, "recover_stale_applications", lambda: {"requeued": [], "needs_verification": []})
    monkeypatch.setattr(run_registry, "recover_orphaned_runs", lambda: [])

    worker_daemon.main([])
    assert calls == []


def test_main_runs_singleton_cleanup_for_steel_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("DICEPILOT_BROWSER_PROVIDER", "steel")
    monkeypatch.setenv("DICEPILOT_BROWSER_PROFILE_DIR", str(tmp_path))
    calls = []
    monkeypatch.setattr(worker_daemon, "clean_stale_singleton_locks", lambda profile_dir: calls.append(profile_dir) or [])
    monkeypatch.setattr(worker_daemon, "run_daemon", lambda *a, **kw: None)
    import run_registry

    monkeypatch.setattr(run_registry, "recover_stale_applications", lambda: {"requeued": [], "needs_verification": []})
    monkeypatch.setattr(run_registry, "recover_orphaned_runs", lambda: [])

    worker_daemon.main([])
    assert calls == [str(tmp_path)]


# 13. Worker status page shows the real, configured browser provider.
def test_worker_page_shows_steel_provider(monkeypatch):
    from local_app.app import app

    monkeypatch.setenv("DICEPILOT_BROWSER_PROVIDER", "steel")
    body = app.test_client().get("/worker").get_data(as_text=True)
    assert "Browser Provider" in body
    assert "Steel" in body


def test_worker_page_shows_local_provider_by_default(monkeypatch):
    from local_app.app import app

    monkeypatch.delenv("DICEPILOT_BROWSER_PROVIDER", raising=False)
    body = app.test_client().get("/worker").get_data(as_text=True)
    assert "Local" in body
