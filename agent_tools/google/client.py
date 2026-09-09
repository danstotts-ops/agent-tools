"""Google REST client for GA4, Search Console, Tag Manager, and YouTube.

Auth (three sources, in order):
  1. GOOGLE_OAUTH_REFRESH_TOKEN (+ GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET):
     a stored user OAuth grant. Used in Railway / prod. Mirrors how the
     snowflake client takes a PEM key from env.
  2. Application Default Credentials (google.auth.default): local dev after
     `gcloud auth application-default login`.
  3. GOOGLE_API_KEY: last resort, and ONLY for the public YouTube reads listed in
     `_key_eligible`. A key identifies the caller, not a user, so it cannot serve
     GA4/GSC/GTM or anything scoped to a signed-in account. It exists so a service
     that only needs public channel and video statistics can hold a restricted key
     instead of a user grant carrying analytics.edit and tagmanager.publish.

Sources 1 and 2 always win when they produce a working credential, so adding a
key changes nothing for callers that already have one. The key path engages only
when the OAuth/ADC lookup fails outright or its refresh is rejected.

All calls go over a google-auth AuthorizedSession (requests transport) with an
explicit timeout. We deliberately avoid googleapiclient/httplib2: httplib2 has
no honored timeout and stalls intermittently in agent runtimes.
"""

from __future__ import annotations

import os
import threading
import urllib.parse
from typing import Any

SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/analytics.edit",
    "https://www.googleapis.com/auth/webmasters",
    "https://www.googleapis.com/auth/tagmanager.readonly",
    "https://www.googleapis.com/auth/tagmanager.edit.containers",
    "https://www.googleapis.com/auth/tagmanager.publish",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

HTTP_TIMEOUT = int(os.environ.get("GOOGLE_MCP_HTTP_TIMEOUT", "30"))

GA4_ADMIN = "https://analyticsadmin.googleapis.com/v1beta"
GA4_DATA = "https://analyticsdata.googleapis.com/v1beta"
GSC = "https://searchconsole.googleapis.com/webmasters/v3"
GTM = "https://tagmanager.googleapis.com/tagmanager/v2"
YT = "https://youtube.googleapis.com/youtube/v3"

API_KEY_ENV = "GOOGLE_API_KEY"

_LOCK = threading.Lock()
_SESSION = None
_SESSION_ERROR = None
_PLAIN_SESSION = None


class GoogleApiError(Exception):
    pass


def _build_credentials():
    refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN")
    if refresh_token:
        from google.oauth2.credentials import Credentials

        client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
        client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
        if not (client_id and client_secret):
            raise GoogleApiError(
                "GOOGLE_OAUTH_REFRESH_TOKEN is set but GOOGLE_OAUTH_CLIENT_ID / "
                "GOOGLE_OAUTH_CLIENT_SECRET are missing"
            )
        return Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=SCOPES,
        )

    import google.auth

    creds, _ = google.auth.default(scopes=SCOPES)
    return creds


def _session():
    global _SESSION, _SESSION_ERROR
    if _SESSION is None:
        with _LOCK:
            if _SESSION is None:
                if _SESSION_ERROR is not None:
                    # Cached: on a host with no ADC file and no metadata server,
                    # google.auth.default() can burn seconds before failing, and
                    # collect_youtube alone makes ~26 calls.
                    raise _SESSION_ERROR
                from google.auth.transport.requests import AuthorizedSession

                try:
                    _SESSION = AuthorizedSession(_build_credentials())
                except _auth_error_types() as exc:
                    _SESSION_ERROR = exc
                    raise
    return _SESSION


def _api_key() -> str:
    return os.environ.get(API_KEY_ENV, "").strip()


def _plain_session():
    """Unauthenticated requests session for API-key calls."""
    global _PLAIN_SESSION
    if _PLAIN_SESSION is None:
        with _LOCK:
            if _PLAIN_SESSION is None:
                import requests

                _PLAIN_SESSION = requests.Session()
    return _PLAIN_SESSION


def _key_eligible(method: str, url: str, params=None) -> bool:
    """True when a bare API key can serve this request.

    Only the YouTube Data API's public reads qualify: channels/search/videos
    addressed by id. `mine=true` resolves against the signed-in account and a
    key has no account, so it is excluded along with every write and every
    GA4/GSC/GTM endpoint.
    """
    if method.upper() != "GET":
        return False
    if not url.startswith(YT):
        return False
    return "mine" not in (params or {})


def _auth_error_types() -> tuple[type[BaseException], ...]:
    """Credential failures worth falling back on, not HTTP or transport errors.

    google.auth raises DefaultCredentialsError when nothing resolves and
    RefreshError when a stored grant is rejected (revoked, expired, or asking
    for scopes it was never granted). Both derive from GoogleAuthError.
    """
    try:
        from google.auth import exceptions as ga_exceptions
    except ImportError:  # google-auth absent entirely
        return (Exception,)
    return (ga_exceptions.GoogleAuthError,)


def _request(method: str, url: str, *, params=None, json_body=None) -> Any:
    try:
        resp = _session().request(
            method, url, params=params, json=json_body, timeout=HTTP_TIMEOUT
        )
    except _auth_error_types():
        # No usable user credential. A restricted key can still serve the public
        # YouTube reads; for anything else the credential error is the real
        # answer and must not be masked by a key that cannot work.
        key = _api_key()
        if not (key and _key_eligible(method, url, params)):
            raise
        resp = _plain_session().request(
            method,
            url,
            params={**(params or {}), "key": key},
            json=json_body,
            timeout=HTTP_TIMEOUT,
        )
    if resp.status_code >= 400:
        raise GoogleApiError(f"HTTP {resp.status_code} {method} {url}: {resp.text[:400]}")
    if not resp.content:
        return {}
    ctype = resp.headers.get("content-type", "")
    return resp.json() if "json" in ctype else {"raw": resp.text}


def _enc(s: str) -> str:
    return urllib.parse.quote(s, safe="")


# ---------------- GA4 ----------------

def ga4_account_summaries() -> dict:
    return _request("GET", f"{GA4_ADMIN}/accountSummaries", params={"pageSize": 200})


def ga4_run_report(
    property_id: str,
    start_date: str = "7daysAgo",
    end_date: str = "yesterday",
    dimensions: list[str] | None = None,
    metrics: list[str] | None = None,
    limit: int = 50,
) -> dict:
    body = {
        "dateRanges": [{"startDate": start_date, "endDate": end_date}],
        "dimensions": [{"name": d} for d in (dimensions or ["date"])],
        "metrics": [{"name": m} for m in (metrics or ["activeUsers", "sessions"])],
        "limit": str(limit),
    }
    return _request("POST", f"{GA4_DATA}/properties/{property_id}:runReport", json_body=body)


def ga4_run_realtime_report(
    property_id: str,
    dimensions: list[str] | None = None,
    metrics: list[str] | None = None,
    limit: int = 50,
) -> dict:
    body = {
        "dimensions": [{"name": d} for d in (dimensions or ["unifiedScreenName"])],
        "metrics": [{"name": m} for m in (metrics or ["activeUsers"])],
        "limit": str(limit),
    }
    return _request(
        "POST", f"{GA4_DATA}/properties/{property_id}:runRealtimeReport", json_body=body
    )


def ga4_list_custom_dimensions(property_id: str) -> dict:
    return _request(
        "GET", f"{GA4_ADMIN}/properties/{property_id}/customDimensions",
        params={"pageSize": 200},
    )


def ga4_update_property_display_name(property_id: str, display_name: str) -> dict:
    return _request(
        "PATCH", f"{GA4_ADMIN}/properties/{property_id}",
        params={"updateMask": "displayName"},
        json_body={"displayName": display_name},
    )


def ga4_create_custom_dimension(
    property_id: str, parameter_name: str, display_name: str, scope: str = "EVENT"
) -> dict:
    body = {"parameterName": parameter_name, "displayName": display_name, "scope": scope}
    return _request(
        "POST", f"{GA4_ADMIN}/properties/{property_id}/customDimensions", json_body=body
    )


# ---------------- Search Console ----------------

def gsc_list_sites() -> dict:
    return _request("GET", f"{GSC}/sites")


def gsc_query(
    site_url: str,
    start_date: str,
    end_date: str,
    dimensions: list[str] | None = None,
    row_limit: int = 50,
    search_type: str = "web",
) -> dict:
    body = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": dimensions or ["query"],
        "rowLimit": row_limit,
        "type": search_type,
    }
    return _request(
        "POST", f"{GSC}/sites/{_enc(site_url)}/searchAnalytics/query", json_body=body
    )


def gsc_list_sitemaps(site_url: str) -> dict:
    return _request("GET", f"{GSC}/sites/{_enc(site_url)}/sitemaps")


def gsc_submit_sitemap(site_url: str, feedpath: str) -> dict:
    _request("PUT", f"{GSC}/sites/{_enc(site_url)}/sitemaps/{_enc(feedpath)}")
    return {"submitted": feedpath}


# ---------------- Tag Manager ----------------

def gtm_list_accounts() -> dict:
    return _request("GET", f"{GTM}/accounts")


def gtm_list_containers(account_id: str) -> dict:
    return _request("GET", f"{GTM}/accounts/{account_id}/containers")


def gtm_list_workspaces(account_id: str, container_id: str) -> dict:
    return _request(
        "GET", f"{GTM}/accounts/{account_id}/containers/{container_id}/workspaces"
    )


def gtm_list_tags(account_id: str, container_id: str, workspace_id: str) -> dict:
    return _request(
        "GET",
        f"{GTM}/accounts/{account_id}/containers/{container_id}"
        f"/workspaces/{workspace_id}/tags",
    )


def gtm_list_triggers(account_id: str, container_id: str, workspace_id: str) -> dict:
    return _request(
        "GET",
        f"{GTM}/accounts/{account_id}/containers/{container_id}"
        f"/workspaces/{workspace_id}/triggers",
    )


def gtm_create_tag(account_id: str, container_id: str, workspace_id: str, tag: dict) -> dict:
    return _request(
        "POST",
        f"{GTM}/accounts/{account_id}/containers/{container_id}"
        f"/workspaces/{workspace_id}/tags",
        json_body=tag,
    )


def gtm_create_version(
    account_id: str, container_id: str, workspace_id: str, name: str, notes: str = ""
) -> dict:
    return _request(
        "POST",
        f"{GTM}/accounts/{account_id}/containers/{container_id}"
        f"/workspaces/{workspace_id}:create_version",
        json_body={"name": name, "notes": notes},
    )


def gtm_publish_version(account_id: str, container_id: str, version_id: str) -> dict:
    return _request(
        "POST",
        f"{GTM}/accounts/{account_id}/containers/{container_id}/versions/{version_id}:publish",
    )


# ---------------- YouTube ----------------

def yt_list_my_channels() -> dict:
    return _request(
        "GET", f"{YT}/channels",
        params={"part": "snippet,statistics,contentDetails", "mine": "true"},
    )


def yt_list_channel_videos(channel_id: str, max_results: int = 25) -> dict:
    return _request(
        "GET", f"{YT}/search",
        params={
            "part": "snippet", "channelId": channel_id, "order": "date",
            "type": "video", "maxResults": max_results,
        },
    )


def yt_get_video(video_id: str) -> dict:
    return _request(
        "GET", f"{YT}/videos", params={"part": "snippet,status,statistics", "id": video_id}
    )


def yt_update_video_metadata(
    video_id: str,
    title: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    category_id: str | None = None,
) -> dict:
    current = yt_get_video(video_id)
    items = current.get("items", [])
    if not items:
        raise GoogleApiError(f"video {video_id} not found or not accessible")
    snippet = items[0]["snippet"]
    new_snippet = {
        "title": title if title is not None else snippet.get("title", ""),
        "description": description if description is not None else snippet.get("description", ""),
        "categoryId": category_id if category_id is not None else snippet.get("categoryId", "22"),
    }
    if tags is not None:
        new_snippet["tags"] = tags
    elif "tags" in snippet:
        new_snippet["tags"] = snippet["tags"]
    body = {"id": video_id, "snippet": new_snippet}
    return _request("PUT", f"{YT}/videos", params={"part": "snippet"}, json_body=body)
