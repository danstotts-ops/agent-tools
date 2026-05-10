"""MCP server exposing Slack read/post helpers as agent tools.

Tools:
  - slack_post_in_thread       : post a threaded reply (already used by runtime to post the final answer; exposed here for agents that want to post intermediate updates)
  - slack_list_channel_messages: read recent top-level messages
  - slack_get_thread           : read a full thread by ts
"""

from __future__ import annotations

import json

from claude_agent_sdk import create_sdk_mcp_server, tool

from . import client


def _pack(result: dict | list) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(result)[:30_000]}]}


@tool(
    "slack_post_in_thread",
    "Post a threaded reply in a Slack channel. Use for status updates "
    "or progress messages while the agent is working. The agent's final "
    "answer is already posted automatically by the runtime; do not call "
    "this for the final answer.",
    {"channel": str, "thread_ts": str, "text": str},
)
async def slack_post_in_thread(args):
    try:
        result = client.post_in_thread(args["channel"], args["thread_ts"], args["text"])
        return _pack({"ok": True, "ts": result["ts"]})
    except Exception as exc:
        return _pack({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


@tool(
    "slack_list_channel_messages",
    "List recent top-level messages in a Slack channel. Returns up to "
    "`limit` messages (default 50, max 200) ordered newest-first.",
    {"channel": str, "limit": int},
)
async def slack_list_channel_messages(args):
    try:
        msgs = client.list_channel_messages(args["channel"], limit=args.get("limit", 50))
        return _pack({"ok": True, "messages": msgs, "count": len(msgs)})
    except Exception as exc:
        return _pack({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


@tool(
    "slack_get_thread",
    "Fetch all replies in a Slack thread, given the parent message ts. "
    "Use to gather context before answering a follow-up.",
    {"channel": str, "thread_ts": str},
)
async def slack_get_thread(args):
    try:
        result = client.get_thread(args["channel"], args["thread_ts"])
        return _pack({"ok": True, **result})
    except Exception as exc:
        return _pack({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


slack_server = create_sdk_mcp_server(
    name="slack",
    version="1.0.0",
    tools=[slack_post_in_thread, slack_list_channel_messages, slack_get_thread],
)
