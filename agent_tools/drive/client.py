"""Google Drive read-only tools for the FP&A agent.

Auth: OAuth user token (Dan's identity). Uses access_token directly with
auto-refresh via refresh_token if available.

CAVEAT: tokens minted via OAuth Playground use Google's playground OAuth client
which has a ~7-day refresh-token TTL. After expiration, Dan must re-authorize.
For permanent access, switch to a dedicated Google Cloud OAuth app — same
code path, just different client_id / client_secret in env.

Tools:
  - search_files(query)              — search Drive by name/content/mimeType
  - get_file_metadata(file_id)        — name, mimeType, modifiedTime, owners
  - read_file_content(file_id)        — text content for Docs/Sheets/text/PDFs
  - list_recent_files(limit, since)   — most recently modified files
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request


def _access_token() -> str:
    tok = os.environ.get("DRIVE_ACCESS_TOKEN")
    if not tok:
        raise RuntimeError("DRIVE_ACCESS_TOKEN not set")
    return tok


def _refresh_access_token() -> str | None:
    """Exchange refresh_token for new access_token. Persists back to env (process-only).

    The OAuth playground client_secret isn't shipped with refresh tokens it issues,
    so refresh requires Dan to provide DRIVE_OAUTH_CLIENT_SECRET in env. Without it,
    refresh fails silently and the agent surfaces a 401.
    """
    refresh = os.environ.get("DRIVE_REFRESH_TOKEN")
    cid = os.environ.get("DRIVE_OAUTH_CLIENT_ID")
    secret = os.environ.get("DRIVE_OAUTH_CLIENT_SECRET")
    if not (refresh and cid and secret):
        return None
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": cid,
        "client_secret": secret,
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            payload = json.loads(r.read())
        new_token = payload.get("access_token")
        if new_token:
            os.environ["DRIVE_ACCESS_TOKEN"] = new_token
        return new_token
    except Exception:
        return None


def _api(path: str, params: dict | None = None, retries_left: int = 1) -> dict:
    url = f"https://www.googleapis.com/drive/v3{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {_access_token()}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 401 and retries_left > 0:
            if _refresh_access_token():
                return _api(path, params, retries_left=retries_left - 1)
        body_text = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"Drive API HTTP {e.code}: {body_text[:300]}")


def search_files(query: str, page_size: int = 20) -> dict:
    """Search Drive. query uses Drive's search syntax:
      - "FY26 budget" → name/content match
      - "name contains 'budget'" → name only
      - "mimeType = 'application/pdf'" → filter by type

    Returns {ok, files: [{id, name, mimeType, modifiedTime, owners}]}.
    """
    try:
        # Escape single quotes in user-supplied substring
        q_safe = query.replace("'", "\\'")
        # If user gave Drive syntax already (contains '=' or 'contains'), use directly
        if "=" in query or " contains " in query:
            q = query
        else:
            q = f"fullText contains '{q_safe}' and trashed = false"
        d = _api("/files", {
            "q": q,
            "pageSize": page_size,
            "fields": "files(id,name,mimeType,modifiedTime,owners(displayName,emailAddress),webViewLink,size)",
            "orderBy": "modifiedTime desc",
        })
        return {"ok": True, "files": d.get("files", []), "count": len(d.get("files", []))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_file_metadata(file_id: str) -> dict:
    try:
        d = _api(f"/files/{file_id}", {
            "fields": "id,name,mimeType,modifiedTime,size,webViewLink,owners(displayName,emailAddress),parents",
        })
        return {"ok": True, "file": d}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def read_file_content(file_id: str, max_chars: int = 30_000) -> dict:
    """Read file content. Handles Google Docs/Sheets export and direct download.
    Returns {ok, content, mimeType, truncated}.
    """
    try:
        meta = _api(f"/files/{file_id}", {"fields": "id,name,mimeType,size"})
        mime = meta.get("mimeType", "")
        if mime.startswith("application/vnd.google-apps."):
            export_mime = {
                "application/vnd.google-apps.document": "text/plain",
                "application/vnd.google-apps.spreadsheet": "text/csv",
                "application/vnd.google-apps.presentation": "text/plain",
            }.get(mime)
            if not export_mime:
                return {"ok": False, "error": f"Cannot export Google Apps type: {mime}"}
            url = f"https://www.googleapis.com/drive/v3/files/{file_id}/export?{urllib.parse.urlencode({'mimeType': export_mime})}"
        else:
            url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_access_token()}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", "replace")
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars]
        return {
            "ok": True,
            "name": meta.get("name"),
            "mimeType": mime,
            "content": text,
            "bytes": len(raw),
            "truncated": truncated,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def list_recent_files(limit: int = 20, modified_since_iso: str | None = None) -> dict:
    """List files modified recently (defaults to last 30 days)."""
    try:
        since = modified_since_iso
        if not since:
            since = time.strftime("%Y-%m-%dT00:00:00", time.gmtime(time.time() - 30 * 86400))
        q = f"modifiedTime > '{since}' and trashed = false"
        d = _api("/files", {
            "q": q,
            "pageSize": limit,
            "fields": "files(id,name,mimeType,modifiedTime,owners(displayName))",
            "orderBy": "modifiedTime desc",
        })
        return {"ok": True, "files": d.get("files", []), "count": len(d.get("files", []))}
    except Exception as e:
        return {"ok": False, "error": str(e)}
