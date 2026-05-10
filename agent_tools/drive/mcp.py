"""MCP server exposing Google Drive read-only helpers as agent tools.

Tools:
  - drive_search_files       : full-text or syntax-based search
  - drive_get_file_metadata  : metadata for a file by id
  - drive_read_file_content  : text content of Docs/Sheets/text files (capped)
  - drive_list_recent_files  : recently modified files (default last 30 days)

Auth: OAuth user token via env (DRIVE_ACCESS_TOKEN, DRIVE_REFRESH_TOKEN, ...).
See agent_tools/drive/client.py for refresh behavior.
"""

from __future__ import annotations

import json

from claude_agent_sdk import create_sdk_mcp_server, tool

from . import client


def _pack(result: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(result)[:30_000]}]}


@tool(
    "drive_search_files",
    "Search Google Drive. Pass a plain query like 'FY26 budget' for full-text "
    "search, or use Drive search syntax for control "
    "(e.g. \"name contains 'budget' and mimeType = 'application/vnd.google-apps.spreadsheet'\"). "
    "Returns file metadata: id, name, mimeType, modifiedTime, owners, webViewLink, size.",
    {"query": str, "page_size": int},
)
async def drive_search_files(args):
    return _pack(client.search_files(args["query"], page_size=args.get("page_size", 20)))


@tool(
    "drive_get_file_metadata",
    "Get full metadata for a Drive file by id (name, mimeType, modifiedTime, "
    "owners, parents, webViewLink, size).",
    {"file_id": str},
)
async def drive_get_file_metadata(args):
    return _pack(client.get_file_metadata(args["file_id"]))


@tool(
    "drive_read_file_content",
    "Read text content of a Drive file. Handles Google Docs (export to plain text), "
    "Sheets (CSV), Slides (text), and direct download for txt/md/csv. "
    "Capped at max_chars (default 30000). For .xlsx/.docx binaries, returns "
    "raw bytes which the agent should note and not try to interpret.",
    {"file_id": str, "max_chars": int},
)
async def drive_read_file_content(args):
    return _pack(
        client.read_file_content(args["file_id"], max_chars=args.get("max_chars", 30000))
    )


@tool(
    "drive_list_recent_files",
    "List Drive files modified recently. Defaults to the last 30 days. "
    "Pass modified_since_iso to set an explicit cutoff (ISO 8601). "
    "Useful for 'what's been updated lately' style questions.",
    {"limit": int, "modified_since_iso": str},
)
async def drive_list_recent_files(args):
    return _pack(
        client.list_recent_files(
            limit=args.get("limit", 20),
            modified_since_iso=args.get("modified_since_iso"),
        )
    )


drive_server = create_sdk_mcp_server(
    name="drive",
    version="1.0.0",
    tools=[
        drive_search_files,
        drive_get_file_metadata,
        drive_read_file_content,
        drive_list_recent_files,
    ],
)
