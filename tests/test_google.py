"""Tests for the google service: request construction + write gating.

No live credentials needed. A fake session records calls; the OAuth/ADC path
is bypassed by setting the module session directly.
"""

from __future__ import annotations

import json

import pytest

from agent_tools.google import client
from agent_tools.google import mcp


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.content = b"x"
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self):
        self.calls = []

    def request(self, method, url, params=None, json=None, timeout=None):
        self.calls.append(
            {"method": method, "url": url, "params": params, "json": json}
        )
        return _FakeResponse({"ok": True})


@pytest.fixture
def fake_session(monkeypatch):
    fs = _FakeSession()
    monkeypatch.setattr(client, "_SESSION", fs)
    return fs


def test_ga4_run_report_builds_runreport_call(fake_session):
    client.ga4_run_report("312964275", "2026-01-01", "2026-01-31", ["date"], ["sessions"], 10)
    call = fake_session.calls[-1]
    assert call["method"] == "POST"
    assert call["url"].endswith("/properties/312964275:runReport")
    assert call["json"]["dateRanges"][0]["startDate"] == "2026-01-01"
    assert call["json"]["metrics"] == [{"name": "sessions"}]


def test_gsc_query_url_encodes_site(fake_session):
    client.gsc_query("sc-domain:runpod.io", "2026-01-01", "2026-01-07", ["query"])
    call = fake_session.calls[-1]
    assert call["method"] == "POST"
    # colon in sc-domain must be percent-encoded into the path segment
    assert "sc-domain%3Arunpod.io" in call["url"]
    assert call["url"].endswith("/searchAnalytics/query")


def test_gtm_publish_version_targets_versions_publish(fake_session):
    client.gtm_publish_version("6334001222", "111", "9")
    call = fake_session.calls[-1]
    assert call["method"] == "POST"
    assert call["url"].endswith("/containers/111/versions/9:publish")


def test_yt_update_reuses_existing_snippet(fake_session, monkeypatch):
    monkeypatch.setattr(
        client,
        "yt_get_video",
        lambda vid: {"items": [{"snippet": {"title": "old", "categoryId": "28", "description": "d"}}]},
    )
    client.yt_update_video_metadata("vid123", title="new")
    call = fake_session.calls[-1]
    assert call["method"] == "PUT"
    assert call["json"]["snippet"]["title"] == "new"
    # categoryId preserved from existing snippet
    assert call["json"]["snippet"]["categoryId"] == "28"


def test_request_raises_on_http_error(monkeypatch):
    class ErrSession:
        def request(self, *a, **k):
            r = _FakeResponse({"error": "nope"}, status=403)
            r.text = "insufficient scopes"
            return r

    monkeypatch.setattr(client, "_SESSION", ErrSession())
    with pytest.raises(client.GoogleApiError):
        client.gsc_list_sites()


# ---- write gating (mcp layer, no SDK internals) ----

def test_gated_dry_run_does_not_execute():
    called = {"n": 0}

    def execute():
        called["n"] += 1
        return {"done": True}

    out = mcp._gated(True, "act", {"a": 1}, execute)
    body = json.loads(out["content"][0]["text"])
    assert body["dry_run"] is True
    assert called["n"] == 0


def test_gated_execute_when_not_dry_run():
    out = mcp._gated(False, "act", {"a": 1}, lambda: {"done": True})
    body = json.loads(out["content"][0]["text"])
    assert body["dry_run"] is False
    assert body["result"] == {"done": True}


def test_servers_exist():
    assert set(mcp.GOOGLE_SERVERS) == {"ga4", "gsc", "gtm", "youtube"}


# ---- API-key fallback (client) ----


@pytest.fixture(autouse=True)
def _reset_client_state(monkeypatch):
    """Clear the session singletons and the negative cache between tests."""
    monkeypatch.setattr(client, "_SESSION", None, raising=False)
    monkeypatch.setattr(client, "_SESSION_ERROR", None, raising=False)
    monkeypatch.setattr(client, "_PLAIN_SESSION", None, raising=False)
    monkeypatch.delenv(client.API_KEY_ENV, raising=False)
    yield


def _no_credentials(monkeypatch):
    """Make the OAuth/ADC path fail the way an uncredentialed host does."""
    from google.auth import exceptions as ga_exceptions

    def boom():
        raise ga_exceptions.DefaultCredentialsError("no ADC here")

    monkeypatch.setattr(client, "_build_credentials", boom)


def test_public_youtube_read_falls_back_to_api_key(monkeypatch):
    _no_credentials(monkeypatch)
    monkeypatch.setenv(client.API_KEY_ENV, "AIza-test-key")
    plain = _FakeSession()
    monkeypatch.setattr(client, "_plain_session", lambda: plain)

    client.yt_get_video("vid123")

    call = plain.calls[-1]
    assert call["method"] == "GET"
    assert call["url"].endswith("/videos")
    assert call["params"]["key"] == "AIza-test-key"
    # the caller's own params survive alongside the key
    assert call["params"]["id"] == "vid123"


def test_channel_videos_search_is_key_eligible(monkeypatch):
    _no_credentials(monkeypatch)
    monkeypatch.setenv(client.API_KEY_ENV, "AIza-test-key")
    plain = _FakeSession()
    monkeypatch.setattr(client, "_plain_session", lambda: plain)

    client.yt_list_channel_videos("UCrrgg7pwLmCfpF4nlj1cg0Q", max_results=5)

    assert plain.calls[-1]["params"]["key"] == "AIza-test-key"
    assert plain.calls[-1]["params"]["channelId"] == "UCrrgg7pwLmCfpF4nlj1cg0Q"


def test_api_key_does_not_rescue_mine_true(monkeypatch):
    """mine=true needs an account; a key has none, so do not pretend."""
    _no_credentials(monkeypatch)
    monkeypatch.setenv(client.API_KEY_ENV, "AIza-test-key")
    from google.auth import exceptions as ga_exceptions

    with pytest.raises(ga_exceptions.DefaultCredentialsError):
        client.yt_list_my_channels()


def test_api_key_does_not_rescue_youtube_writes(monkeypatch):
    _no_credentials(monkeypatch)
    monkeypatch.setenv(client.API_KEY_ENV, "AIza-test-key")
    monkeypatch.setattr(
        client, "yt_get_video", lambda vid: {"items": [{"snippet": {"title": "old"}}]}
    )
    from google.auth import exceptions as ga_exceptions

    with pytest.raises(ga_exceptions.DefaultCredentialsError):
        client.yt_update_video_metadata("vid123", title="new")


@pytest.mark.parametrize(
    "call",
    [
        lambda: client.gsc_list_sites(),
        lambda: client.gtm_list_accounts(),
        lambda: client.ga4_account_summaries(),
    ],
)
def test_api_key_does_not_rescue_non_youtube_apis(monkeypatch, call):
    _no_credentials(monkeypatch)
    monkeypatch.setenv(client.API_KEY_ENV, "AIza-test-key")
    from google.auth import exceptions as ga_exceptions

    with pytest.raises(ga_exceptions.DefaultCredentialsError):
        call()


def test_credential_error_propagates_without_a_key(monkeypatch):
    _no_credentials(monkeypatch)
    from google.auth import exceptions as ga_exceptions

    with pytest.raises(ga_exceptions.DefaultCredentialsError):
        client.yt_get_video("vid123")


def test_refresh_rejection_also_falls_back(monkeypatch):
    """A stored grant missing a scope raises at refresh, not at construction."""
    from google.auth import exceptions as ga_exceptions

    class RefusingSession:
        def request(self, *a, **k):
            raise ga_exceptions.RefreshError("invalid_scope")

    monkeypatch.setattr(client, "_SESSION", RefusingSession())
    monkeypatch.setenv(client.API_KEY_ENV, "AIza-test-key")
    plain = _FakeSession()
    monkeypatch.setattr(client, "_plain_session", lambda: plain)

    client.yt_get_video("vid123")

    assert plain.calls[-1]["params"]["key"] == "AIza-test-key"


def test_oauth_session_still_wins_when_it_works(monkeypatch):
    """A key present alongside a working credential must not change routing."""
    authed = _FakeSession()
    monkeypatch.setattr(client, "_SESSION", authed)
    monkeypatch.setenv(client.API_KEY_ENV, "AIza-test-key")
    plain = _FakeSession()
    monkeypatch.setattr(client, "_plain_session", lambda: plain)

    client.yt_get_video("vid123")

    assert len(authed.calls) == 1
    assert plain.calls == []
    assert "key" not in authed.calls[-1]["params"]


def test_adc_lookup_is_attempted_once(monkeypatch):
    """The negative cache keeps a 26-call collect from 26 slow ADC lookups."""
    from google.auth import exceptions as ga_exceptions

    attempts = {"n": 0}

    def boom():
        attempts["n"] += 1
        raise ga_exceptions.DefaultCredentialsError("no ADC here")

    monkeypatch.setattr(client, "_build_credentials", boom)
    monkeypatch.setenv(client.API_KEY_ENV, "AIza-test-key")
    monkeypatch.setattr(client, "_plain_session", lambda: _FakeSession())

    for _ in range(4):
        client.yt_get_video("vid123")

    assert attempts["n"] == 1
