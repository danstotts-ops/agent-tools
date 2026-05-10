"""Adapter for legacy hand-rolled agents to the Claude Agent SDK.

Existing agents (ea, strategy, revops, video, web, pmm, marketing-recruiter,
social, content-review) have a flat structure:

    TOOLS = [  # Anthropic-format tool dicts
        {"name": "...", "description": "...", "input_schema": {...}},
        ...
    ]

    def dispatch(name: str, args: dict) -> str:
        # returns a JSON string
        ...

`wrap_legacy_tools()` takes those two things and produces a single MCP server
that exposes every tool. The agent_v2.py for each agent becomes ~30 lines:
pull TOOLS + dispatch from the existing agent.py, wrap, pass to runtime.

This is the path for agents where writing per-domain MCP server files (like
fpa's mcp_servers/) is overkill. The benefits (claude_code system preset,
memory injection, hook-based error visibility) come from the runtime; how
the tools are namespaced is a stylistic choice.

Limitations:
- All tools end up in one MCP namespace (no per-domain grouping in tool names).
- input_schema must be a flat JSON-schema-style dict using the same keys as
  Anthropic's tool format ("type": "object", "properties": {...}, "required": [...]).
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Callable

from claude_agent_sdk import create_sdk_mcp_server, tool


def _schema_to_params(input_schema: dict) -> dict:
    """Convert Anthropic-format input_schema to the simple {name: type} dict
    expected by claude_agent_sdk.tool decorator."""
    props = input_schema.get("properties", {}) or {}
    out: dict = {}
    for name, spec in props.items():
        t = spec.get("type", "string")
        if isinstance(t, list):
            # JSON schema can specify ["string", "integer"]; pick first non-null
            t = next((x for x in t if x != "null"), "string")
        out[name] = {
            "string": str,
            "integer": int,
            "number": float,
            "boolean": bool,
            "array": list,
            "object": dict,
        }.get(t, str)
    return out


def _make_tool_fn(name: str, dispatch_fn: Callable[[str, dict], str]):
    """Build an async wrapper that calls the agent's existing dispatch().

    The dispatch function is sync in legacy agents; we run it directly since
    most of these tools are I/O-bound but not async-aware. If a future agent
    needs true async, it can override this.
    """
    async def _impl(args):
        result_str = dispatch_fn(name, args)
        # dispatch returns a JSON string; pass it through verbatim, capped.
        text = result_str if isinstance(result_str, str) else json.dumps(result_str)
        return {"content": [{"type": "text", "text": text[:30_000]}]}
    _impl.__name__ = f"_tool_{name}"
    return _impl


def wrap_legacy_tools(
    tools: list[dict],
    dispatch_fn: Callable[[str, dict], str],
    *,
    server_name: str = "legacy",
    version: str = "1.0.0",
):
    """Return an MCP server exposing every tool in `tools` via `dispatch_fn`.

    Args
    ----
    tools
        The agent's existing TOOLS list (Anthropic-format dicts with name,
        description, input_schema).
    dispatch_fn
        The agent's existing dispatch(name, args) -> json_string function.
    server_name
        Logical name for the MCP server (becomes the namespace in tool calls
        like mcp__<server_name>__<tool_name>).
    """
    decorated = []
    for t in tools:
        name = t["name"]
        description = t.get("description", "")
        input_schema = t.get("input_schema", {"type": "object", "properties": {}})
        params = _schema_to_params(input_schema)
        impl = _make_tool_fn(name, dispatch_fn)
        decorated.append(tool(name, description, params)(impl))
    return create_sdk_mcp_server(name=server_name, version=version, tools=decorated)
