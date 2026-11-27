"""Auth for agent_tools.linear: OAuth app (client credentials) first, personal key second.

What matters:
1. With LINEAR_CLIENT_ID + LINEAR_CLIENT_SECRET set, GraphQL calls carry a
   Bearer app token minted by the client_credentials grant, with fixed scopes.
2. The token is cached and re-minted only near expiry or after an auth failure,
   and an auth failure is retried exactly once.
3. Without app credentials, the personal key is sent raw, as before.
4. Errors never contain the client secret or the token.
"""
from __future__ import annotations

import pytest

from agent_tools.linear import client as linear

SECRET = "sekrit-client-secret-value"


class Resp:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeHTTP:
    """Stands in for requests.post: answers the token URL and the GraphQL URL."""

    def __init__(self, token_resps=None, gql_resps=None):
        self.token_resps = list(token_resps or [])
        self.gql_resps = list(gql_resps or [])
        self.token_calls: list[dict] = []
        self.gql_calls: list[dict] = []

    def __call__(self, url, **kw):
        if url == linear.TOKEN_URL:
            self.token_calls.append(kw)
            return self.token_resps.pop(0)
        assert url == linear.API_URL, url
        self.gql_calls.append(kw)
        return self.gql_resps.pop(0)


def _tok(n: int, expires_in: int = 2591999) -> Resp:
    return Resp(200, {"access_token": f"app-token-{n}", "token_type": "Bearer", "expires_in": expires_in})


OK = Resp(200, {"data": {"viewer": {"id": "app-user"}}})
AUTH_FAIL = Resp(400, {"errors": [{"message": "Authentication required", "extensions": {"type": "authentication error"}}]})


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("LINEAR_CLIENT_ID", "LINEAR_CLIENT_SECRET", "LINEAR_API_KEY", "COS_LINEAR_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    linear._app_token.clear()
    yield
    linear._app_token.clear()


@pytest.fixture
def app_env(monkeypatch):
    monkeypatch.setenv("LINEAR_CLIENT_ID", "cid-123")
    monkeypatch.setenv("LINEAR_CLIENT_SECRET", SECRET)


def _install(monkeypatch, http):
    monkeypatch.setattr(linear.requests, "post", http)
    return http


def test_app_token_is_minted_with_client_credentials_and_sent_as_bearer(monkeypatch, app_env):
    http = _install(monkeypatch, FakeHTTP([_tok(1)], [OK]))
    assert linear._gql("query Q { viewer { id } }") == {"viewer": {"id": "app-user"}}
    (tc,) = http.token_calls
    assert tc["data"] == {"grant_type": "client_credentials", "scope": linear.APP_SCOPES}
    assert tc["auth"] == ("cid-123", SECRET)
    assert http.gql_calls[0]["headers"]["Authorization"] == "Bearer app-token-1"
    assert linear.auth_mode() == "app"


def test_app_token_is_cached_across_calls(monkeypatch, app_env):
    http = _install(monkeypatch, FakeHTTP([_tok(1)], [OK, OK, OK]))
    for _ in range(3):
        linear._gql("query Q { viewer { id } }")
    assert len(http.token_calls) == 1


def test_token_near_expiry_is_reminted(monkeypatch, app_env):
    http = _install(monkeypatch, FakeHTTP([_tok(1, expires_in=3600), _tok(2)], [OK, OK]))
    linear._gql("query Q { viewer { id } }")  # mints a token with < 1 day left
    linear._gql("query Q { viewer { id } }")  # so this call re-mints
    assert len(http.token_calls) == 2
    assert http.gql_calls[1]["headers"]["Authorization"] == "Bearer app-token-2"


def test_auth_failure_remints_and_retries_once(monkeypatch, app_env):
    http = _install(monkeypatch, FakeHTTP([_tok(1), _tok(2)], [AUTH_FAIL, OK]))
    assert linear._gql("query Q { viewer { id } }") == {"viewer": {"id": "app-user"}}
    assert [c["headers"]["Authorization"] for c in http.gql_calls] == ["Bearer app-token-1", "Bearer app-token-2"]


def test_persistent_auth_failure_is_reported_after_one_retry(monkeypatch, app_env):
    http = _install(monkeypatch, FakeHTTP([_tok(1), _tok(2)], [AUTH_FAIL, AUTH_FAIL]))
    out = linear.read_context("web-agent")
    assert out["ok"] is False and "Authentication" in out["error"]
    assert len(http.gql_calls) == 2 and len(http.token_calls) == 2


def test_api_key_path_is_unchanged_and_not_retried(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_personal")
    http = _install(monkeypatch, FakeHTTP([], [AUTH_FAIL]))
    with pytest.raises(linear.LinearError):
        linear._gql("query Q { viewer { id } }")
    assert http.gql_calls[0]["headers"]["Authorization"] == "lin_api_personal"
    assert http.token_calls == [] and len(http.gql_calls) == 1
    assert linear.auth_mode() == "api_key"


def test_app_credentials_win_over_api_key(monkeypatch, app_env):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_personal")
    http = _install(monkeypatch, FakeHTTP([_tok(1)], [OK]))
    linear._gql("query Q { viewer { id } }")
    assert http.gql_calls[0]["headers"]["Authorization"] == "Bearer app-token-1"


def test_half_configured_app_is_an_error_not_a_silent_fallback(monkeypatch):
    monkeypatch.setenv("LINEAR_CLIENT_ID", "cid-123")
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_personal")
    out = linear.read_context("web-agent")
    assert out["ok"] is False and "LINEAR_CLIENT_SECRET" in out["error"]
    assert linear.auth_mode() == "misconfigured"


@pytest.mark.parametrize("resp", [
    Resp(400, {"error": "invalid_client", "error_description": "Client does not support the client_credentials grant type"}),
    Resp(401, {"error": "invalid_client"}),
    Resp(200, {"token_type": "Bearer"}),
    Resp(502, ValueError("not json")),
])
def test_mint_failures_are_reported_without_leaking_secrets(monkeypatch, app_env, resp):
    _install(monkeypatch, FakeHTTP([resp], []))
    out = linear.read_context("web-agent")
    assert out["ok"] is False
    assert "token" in out["error"].lower()
    assert SECRET not in out["error"] and "cid-123" not in out["error"]


def test_no_credentials_at_all_names_both_options(monkeypatch):
    out = linear.read_context("web-agent")
    assert out["ok"] is False
    assert "LINEAR_CLIENT_ID" in out["error"] and "LINEAR_API_KEY" in out["error"]
