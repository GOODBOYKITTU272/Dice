"""Phase 7.9: dice_browser.browserless_session -- fully offline
(requests.post mocked). Never hits the real Browserless API or needs a
real token to run.
"""
from __future__ import annotations

import json

import pytest
import requests

import dice_browser.browserless_session as bs


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")

    def json(self):
        return self._payload


def test_is_configured_true_when_token_set(monkeypatch):
    monkeypatch.setenv("BROWSERLESS_TOKEN", "test-token")
    assert bs.is_configured() is True


def test_is_configured_false_when_token_unset(monkeypatch):
    monkeypatch.delenv("BROWSERLESS_TOKEN", raising=False)
    assert bs.is_configured() is False


def test_create_session_posts_to_the_real_endpoint_with_token_and_ttl(monkeypatch):
    calls = []
    monkeypatch.setenv("BROWSERLESS_TOKEN", "test-token")

    def fake_post(url, params=None, json=None, timeout=None):
        calls.append((url, params, json))
        return _FakeResponse({"id": "sess-1", "connect": "wss://example/connect", "stop": "https://example/stop", "ttl": 21600000})

    monkeypatch.setattr(requests, "post", fake_post)

    result = bs.create_session()

    assert result["connect"] == "wss://example/connect"
    url, params, body = calls[0]
    assert "production-sfo.browserless.io/session" in url
    assert params == {"token": "test-token"}
    assert body == {"ttl": bs.DEFAULT_SESSION_TTL_MS, "processKeepAlive": bs.DEFAULT_PROCESS_KEEP_ALIVE_MS}


def test_create_session_uses_configured_region(monkeypatch):
    monkeypatch.setenv("BROWSERLESS_TOKEN", "test-token")
    monkeypatch.setenv("BROWSERLESS_REGION", "production-lon.browserless.io")
    calls = []

    def fake_post(url, params=None, json=None, timeout=None):
        calls.append(url)
        return _FakeResponse({"connect": "wss://x"})

    monkeypatch.setattr(requests, "post", fake_post)
    bs.create_session()

    assert "production-lon.browserless.io" in calls[0]


def test_create_session_raises_without_token(monkeypatch):
    monkeypatch.delenv("BROWSERLESS_TOKEN", raising=False)
    with pytest.raises(bs.BrowserlessNotConfiguredError):
        bs.create_session()


def test_stop_session_never_raises_on_failure(monkeypatch):
    def _boom(*a, **kw):
        raise ConnectionError("network down")

    monkeypatch.setattr(requests, "post", _boom)
    bs.stop_session("https://example/stop")  # must not raise


def test_load_dice_cookies_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("DICE_AUTH_COOKIES_JSON", raising=False)
    assert bs.load_dice_cookies() is None


def test_load_dice_cookies_parses_the_env_var(monkeypatch):
    cookies = [{"name": "identity", "value": "abc", "domain": ".dice.com", "path": "/", "secure": True, "httpOnly": False, "expirationDate": 123}]
    monkeypatch.setenv("DICE_AUTH_COOKIES_JSON", json.dumps(cookies))

    result = bs.load_dice_cookies()

    assert result == cookies


def test_to_playwright_cookies_maps_same_site_values():
    raw = [
        {"name": "a", "value": "1", "domain": ".dice.com", "path": "/", "secure": True, "httpOnly": False, "sameSite": "no_restriction", "expirationDate": 100},
        {"name": "b", "value": "2", "domain": ".dice.com", "path": "/", "secure": True, "httpOnly": True, "sameSite": None, "expirationDate": 200},
    ]

    converted = bs.to_playwright_cookies(raw)

    assert converted[0]["sameSite"] == "None"
    assert converted[0]["expires"] == 100
    assert converted[1]["sameSite"] == "Lax"
    assert converted[1]["httpOnly"] is True
