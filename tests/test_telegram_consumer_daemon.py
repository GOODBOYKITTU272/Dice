"""Phase 7.8: attention.telegram_consumer_daemon -- the always-on
Telegram long-poll loop. Never makes a real Telegram/Supabase call in
this file; poll_telegram_once and check_startup_readiness's own network
calls are monkeypatched at the module level.
"""
from __future__ import annotations

import attention.telegram_consumer_daemon as consumer_daemon


def test_readiness_check_passes_with_valid_config(monkeypatch):
    monkeypatch.setattr(consumer_daemon.TelegramProvider, "fetch_updates", lambda self, offset=None, timeout=0: [])

    results = consumer_daemon.check_startup_readiness()

    assert results["Telegram"] is True
    assert results["Supabase"] is True  # live_client fixture isn't needed -- real Supabase creds are already in test env


def test_readiness_check_fails_when_telegram_not_configured(monkeypatch):
    from attention.providers.telegram import TelegramNotConfiguredError

    def _boom(self, offset=None, timeout=0):
        raise TelegramNotConfiguredError("TELEGRAM_BOT_TOKEN is not configured")

    monkeypatch.setattr(consumer_daemon.TelegramProvider, "fetch_updates", _boom)

    results = consumer_daemon.check_startup_readiness()

    assert results["Telegram"] is False


def test_main_refuses_to_start_when_telegram_not_configured(monkeypatch):
    from attention.providers.telegram import TelegramNotConfiguredError

    def _boom(self, offset=None, timeout=0):
        raise TelegramNotConfiguredError("TELEGRAM_BOT_TOKEN is not configured")

    monkeypatch.setattr(consumer_daemon.TelegramProvider, "fetch_updates", _boom)
    calls = []
    monkeypatch.setattr(consumer_daemon, "run_consumer_daemon", lambda *a, **kw: calls.append("called"))

    exit_code = consumer_daemon.main([])

    assert exit_code == 1
    assert calls == []


def test_main_starts_when_config_valid(monkeypatch):
    monkeypatch.setattr(consumer_daemon.TelegramProvider, "fetch_updates", lambda self, offset=None, timeout=0: [])
    calls = []
    monkeypatch.setattr(consumer_daemon, "run_consumer_daemon", lambda *a, **kw: calls.append("called"))

    exit_code = consumer_daemon.main([])

    assert exit_code == 0
    assert calls == ["called"]


def test_run_consumer_daemon_polls_with_the_long_poll_timeout(monkeypatch):
    captured_timeouts = []

    def fake_poll(provider, timeout=0):
        captured_timeouts.append(timeout)
        return []

    monkeypatch.setattr(consumer_daemon, "poll_telegram_once", fake_poll)

    consumer_daemon.run_consumer_daemon(max_iterations=3, long_poll_timeout=25, error_backoff_seconds=0)

    assert captured_timeouts == [25, 25, 25]


def test_run_consumer_daemon_survives_a_bad_poll_and_keeps_going(monkeypatch):
    calls = {"count": 0}

    def flaky_poll(provider, timeout=0):
        calls["count"] += 1
        if calls["count"] == 1:
            raise ConnectionError("network blip")
        return []

    monkeypatch.setattr(consumer_daemon, "poll_telegram_once", flaky_poll)

    consumer_daemon.run_consumer_daemon(max_iterations=3, error_backoff_seconds=0)

    assert calls["count"] == 3  # kept going past the first failure
