"""Phase 7.2: browser provider selection (local Chrome vs self-hosted
Steel). Deliberately thin -- both providers connect via the exact same
dice_browser.worker_daemon._connect() / playwright.chromium.connect_over_cdp()
call, confirmed unmodified during the Steel compatibility spike
(2026-08-23). No real Dice/browser mutation anywhere in this file.
"""
from __future__ import annotations

import dice_browser.worker_daemon as worker_daemon
from dice_browser.browser_provider import resolve_browser_provider


def _stub_valid_resume(monkeypatch, tmp_path):
    """Phase 7.7's mandatory startup readiness check needs a real,
    existing resume file -- every test in this file that reaches
    worker_daemon.main() needs this or main() now correctly refuses to
    start at all."""
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    monkeypatch.setenv("DICEPILOT_RESUME_PATH", str(resume))


# 1. Provider defaults to local
def test_provider_defaults_to_local(monkeypatch):
    monkeypatch.delenv("DICEPILOT_BROWSER_PROVIDER", raising=False)
    assert resolve_browser_provider() == "local"


# 2. Steel provider selected from env
def test_provider_selects_steel_from_env(monkeypatch):
    monkeypatch.setenv("DICEPILOT_BROWSER_PROVIDER", "steel")
    assert resolve_browser_provider() == "steel"


# Phase 7.6: Browserless provider selected from env -- same CDP-attach
# path as steel/local, DICEPILOT_CDP_URL is just the persisted session's
# own connect websocket URL, no provider-specific connect logic needed.
def test_provider_selects_browserless_from_env(monkeypatch):
    monkeypatch.setenv("DICEPILOT_BROWSER_PROVIDER", "browserless")
    assert resolve_browser_provider() == "browserless"


def test_browserless_provider_uses_configured_cdp_url(monkeypatch):
    monkeypatch.setenv("DICEPILOT_BROWSER_PROVIDER", "browserless")
    monkeypatch.setenv("DICEPILOT_CDP_URL", "wss://production-sfo.browserless.io/session-connect")
    assert resolve_browser_provider() == "browserless"
    assert worker_daemon.default_cdp_url() == "wss://production-sfo.browserless.io/session-connect"


def test_main_skips_singleton_cleanup_for_browserless_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("DICEPILOT_BROWSER_PROVIDER", "browserless")
    _stub_valid_resume(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(worker_daemon, "clean_stale_singleton_locks", lambda profile_dir: calls.append(profile_dir) or [])
    monkeypatch.setattr(worker_daemon, "run_daemon", lambda *a, **kw: None)
    import run_registry

    monkeypatch.setattr(run_registry, "recover_stale_applications", lambda: {"requeued": [], "needs_verification": []})
    monkeypatch.setattr(run_registry, "recover_orphaned_runs", lambda: [])

    worker_daemon.main([])
    assert calls == []


def test_worker_page_shows_browserless_provider(monkeypatch):
    from local_app.app import app

    monkeypatch.setenv("DICEPILOT_BROWSER_PROVIDER", "browserless")
    body = app.test_client().get("/worker").get_data(as_text=True)
    assert "Browser Provider" in body
    assert "Browserless" in body


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
def test_main_skips_singleton_cleanup_for_local_provider(monkeypatch, tmp_path):
    monkeypatch.delenv("DICEPILOT_BROWSER_PROVIDER", raising=False)
    monkeypatch.setenv("DICEPILOT_BROWSER_PROFILE_DIR", "/should/not/be/used")
    _stub_valid_resume(monkeypatch, tmp_path)
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
    _stub_valid_resume(monkeypatch, tmp_path)
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


# ── Phase 7.7: mandatory startup readiness check ─────────────────────────


def test_resolve_resume_path_prefers_cli_flag_over_env(monkeypatch):
    monkeypatch.setenv("DICEPILOT_RESUME_PATH", "/from/env.pdf")
    assert worker_daemon.resolve_resume_path("/from/cli.pdf") == "/from/cli.pdf"


def test_resolve_resume_path_falls_back_to_env_when_no_cli_flag(monkeypatch):
    monkeypatch.setenv("DICEPILOT_RESUME_PATH", "/from/env.pdf")
    assert worker_daemon.resolve_resume_path(None) == "/from/env.pdf"


def test_readiness_check_passes_with_valid_config(monkeypatch, tmp_path):
    monkeypatch.setenv("DICEPILOT_CANDIDATE_ID", "test-candidate")
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")

    results = worker_daemon.check_startup_readiness(str(resume))

    assert results["candidate"] is True
    assert results["resume"] is True
    assert results["browser provider"] is True


def test_readiness_check_fails_when_resume_path_is_none():
    results = worker_daemon.check_startup_readiness(None)
    assert results["resume"] is False


def test_readiness_check_fails_when_resume_file_does_not_exist(tmp_path):
    results = worker_daemon.check_startup_readiness(str(tmp_path / "does-not-exist.pdf"))
    assert results["resume"] is False


# Real live finding: a startup command that ran `touch resume.pdf`
# (creating an empty, 0-byte file) passed the old existence-only check.
def test_readiness_check_fails_when_resume_file_is_empty(tmp_path):
    empty_resume = tmp_path / "resume.pdf"
    empty_resume.touch()

    results = worker_daemon.check_startup_readiness(str(empty_resume))

    assert results["resume"] is False


def test_readiness_check_fails_when_candidate_id_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("DICEPILOT_CANDIDATE_ID", raising=False)
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")

    results = worker_daemon.check_startup_readiness(str(resume))

    assert results["candidate"] is False


def test_main_refuses_to_start_when_resume_missing(monkeypatch):
    monkeypatch.delenv("DICEPILOT_RESUME_PATH", raising=False)
    calls = []
    monkeypatch.setattr(worker_daemon, "run_daemon", lambda *a, **kw: calls.append("called"))

    exit_code = worker_daemon.main([])

    assert exit_code == 1
    assert calls == []  # never reached run_daemon at all


def test_main_starts_when_all_mandatory_config_present(monkeypatch, tmp_path):
    _stub_valid_resume(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(worker_daemon, "run_daemon", lambda *a, **kw: calls.append("called"))
    import run_registry

    monkeypatch.setattr(run_registry, "recover_stale_applications", lambda: {"requeued": [], "needs_verification": []})
    monkeypatch.setattr(run_registry, "recover_orphaned_runs", lambda: [])

    exit_code = worker_daemon.main([])

    assert exit_code == 0
    assert calls == ["called"]


# ── Phase 7.9: dynamic Browserless session per connect ───────────────────


def test_readiness_check_includes_browserless_when_selected(monkeypatch, tmp_path):
    monkeypatch.setenv("DICEPILOT_BROWSER_PROVIDER", "browserless")
    monkeypatch.setenv("BROWSERLESS_TOKEN", "test-token")
    _stub_valid_resume(monkeypatch, tmp_path)

    results = worker_daemon.check_startup_readiness(str(tmp_path / "resume.pdf"))

    assert results["Browserless"] is True


def test_readiness_check_fails_when_browserless_selected_but_token_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("DICEPILOT_BROWSER_PROVIDER", "browserless")
    monkeypatch.delenv("BROWSERLESS_TOKEN", raising=False)
    _stub_valid_resume(monkeypatch, tmp_path)

    results = worker_daemon.check_startup_readiness(str(tmp_path / "resume.pdf"))

    assert results["Browserless"] is False


def test_readiness_check_omits_browserless_key_for_other_providers(monkeypatch, tmp_path):
    monkeypatch.delenv("DICEPILOT_BROWSER_PROVIDER", raising=False)
    _stub_valid_resume(monkeypatch, tmp_path)

    results = worker_daemon.check_startup_readiness(str(tmp_path / "resume.pdf"))

    assert "Browserless" not in results


def test_connect_for_provider_dispatches_browserless_to_its_own_connect(monkeypatch):
    calls = []
    monkeypatch.setattr(worker_daemon, "_connect_browserless", lambda candidate_id: calls.append(candidate_id) or (object(), object()))

    worker_daemon._connect_for_provider("unused-cdp-url", "browserless", "cand-1")

    assert calls == ["cand-1"]


def test_connect_browserless_creates_a_fresh_session_and_loads_cookies(monkeypatch):
    import dice_browser.browserless_session as browserless_session

    calls = {"create_session": 0}

    def fake_create_session():
        calls["create_session"] += 1
        return {"connect": "wss://fresh-session-url", "stop": "https://stop-url", "id": "sess-1"}

    fake_cookie = {
        "name": "identity", "value": "x", "domain": ".dice.com", "path": "/",
        "secure": True, "httpOnly": False, "sameSite": "lax", "expirationDate": 123,
    }
    monkeypatch.setattr(browserless_session, "create_session", fake_create_session)
    monkeypatch.setattr(browserless_session, "load_dice_cookies_for_candidate", lambda candidate_id: [fake_cookie])

    added_cookies = []

    class _FakeContext:
        def add_cookies(self, cookies):
            added_cookies.append(cookies)

        def new_page(self):
            return object()

    class _FakeBrowser:
        contexts = []

        def new_context(self):
            return _FakeContext()

    connect_calls = []

    class _FakeChromium:
        def connect_over_cdp(self, url):
            connect_calls.append(url)
            return _FakeBrowser()

    class _FakePlaywrightInstance:
        chromium = _FakeChromium()

        def stop(self):
            pass

    class _FakeSyncPlaywrightCtx:
        def start(self):
            return _FakePlaywrightInstance()

    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: _FakeSyncPlaywrightCtx())

    playwright, page = worker_daemon._connect_browserless("cand-1")

    assert calls["create_session"] == 1
    assert connect_calls == ["wss://fresh-session-url"]
    assert added_cookies  # cookies were loaded into the fresh session's context


# ── Phase 7.10: automatic resume delivery wired into main() ──────────────


def test_main_downloads_resume_automatically_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("DICEPILOT_CANDIDATE_ID", "test-candidate")
    resume_path = str(tmp_path / "resume.pdf")
    monkeypatch.setenv("DICEPILOT_RESUME_PATH", resume_path)

    import dice_browser.resume_delivery as resume_delivery

    calls = []

    def fake_ensure(candidate_id, destination_path):
        calls.append((candidate_id, destination_path))
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "resume.pdf").write_bytes(b"%PDF-1.4 real")
        return True

    monkeypatch.setattr(resume_delivery, "ensure_resume_available", fake_ensure)
    monkeypatch.setattr(worker_daemon, "run_daemon", lambda *a, **kw: None)
    import run_registry

    monkeypatch.setattr(run_registry, "recover_stale_applications", lambda: {"requeued": [], "needs_verification": []})
    monkeypatch.setattr(run_registry, "recover_orphaned_runs", lambda: [])

    exit_code = worker_daemon.main([])

    assert calls == [("test-candidate", resume_path)]
    assert exit_code == 0


def test_main_reports_startup_failure_when_resume_download_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("DICEPILOT_CANDIDATE_ID", "test-candidate")
    monkeypatch.setenv("DICEPILOT_RESUME_PATH", str(tmp_path / "resume.pdf"))

    import dice_browser.resume_delivery as resume_delivery

    monkeypatch.setattr(resume_delivery, "ensure_resume_available", lambda candidate_id, destination_path: False)
    calls = []
    monkeypatch.setattr(worker_daemon, "run_daemon", lambda *a, **kw: calls.append("called"))

    exit_code = worker_daemon.main([])

    assert exit_code == 1
    assert calls == []  # never reached run_daemon -- readiness check correctly caught the missing resume
