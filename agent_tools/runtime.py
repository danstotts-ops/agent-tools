"""Standard agent runtime wrapping ClaudeSDKClient.

Every Slack-driven agent (fpa, cos, strategy, revops, etc.) calls run_ask()
with its own system prompt + MCP servers. The wrapper provides:

  - The Claude Agent SDK loop (multi-turn tool use, retries, streaming)
  - The `claude_code` system-prompt preset so the agent inherits skills,
    memory injection, and the same tool conventions Dan gets locally
  - A PostToolUse hook that posts tool errors back into the Slack thread
    so failures are visible in real time, not buried in Railway logs
  - Optional on_complete callback for telemetry / metrics persistence

Sync entry point: `run_ask(...)`
Async entry point: `run_ask_async(...)`
"""

from __future__ import annotations

import asyncio
import json
import time
import traceback
from pathlib import Path
from typing import Any, Awaitable, Callable

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
)

from .slack.client import post_in_thread

DEFAULT_MEMORY_DIR = Path.home() / ".claude" / "projects" / "-Users-danstotts" / "memory"
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TURNS = 12


def _make_post_tool_use_hook(channel_id: str, thread_ts: str):
    """Build a PostToolUse hook closing over the Slack thread.

    When a tool returns a JSON payload with `ok: false`, post the error tag in-thread
    so the user sees what failed rather than waiting for a stale or empty final reply.
    """

    async def _hook(input_data, tool_use_id, context):
        try:
            tool_result = input_data.get("tool_result") or {}
            content = tool_result.get("content") or []
            for block in content:
                text = block.get("text") if isinstance(block, dict) else None
                if not text:
                    continue
                try:
                    parsed = json.loads(text)
                except (TypeError, ValueError):
                    continue
                if isinstance(parsed, dict) and parsed.get("ok") is False:
                    tool_name = input_data.get("tool_name", "?")
                    err = parsed.get("error", "unknown")
                    post_in_thread(
                        channel_id,
                        thread_ts,
                        f":warning: tool `{tool_name}` returned error: `{str(err)[:300]}`",
                    )
        except Exception:
            traceback.print_exc()
        return {}

    return _hook


def _build_user_msg(text: str, thread_context: list[dict] | None) -> str:
    pieces = []
    if thread_context:
        pieces.append("Thread context (oldest first, may be empty):")
        for m in thread_context:
            who = m.get("user") or "?"
            txt = (m.get("text") or "").strip()
            pieces.append(f"- @{who}: {txt[:500]}")
        pieces.append("")
    pieces.append(f"User message: {text.strip()}")
    pieces.append(
        'Reply directly in 2nd person ("you", "your"). Never refer to the user '
        "in 3rd person. Your final assistant message is what gets posted in-thread. "
        "Do not use em dashes. Company name is 'Runpod' not 'RunPod'."
    )
    return "\n".join(pieces)


async def run_ask_async(
    *,
    text: str,
    channel_id: str,
    thread_ts: str,
    system_prompt_append: str,
    mcp_servers: dict[str, Any],
    thread_context: list[dict] | None = None,
    model: str = DEFAULT_MODEL,
    max_turns: int = DEFAULT_MAX_TURNS,
    permission_mode: str = "acceptEdits",
    extra_setting_sources: list[str] | None = None,
    extra_dirs: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    on_complete: Callable[[dict], None] | None = None,
) -> str:
    """Run one Slack ask through the agent loop. Returns the posted message ts.

    Parameters
    ----------
    text
        The user's Slack message.
    channel_id, thread_ts
        Where to post the reply. Errors during the run are also posted here.
    system_prompt_append
        Agent-specific operating principles. Appended to the `claude_code`
        preset, not replacing it.
    mcp_servers
        Dict of {name: server} as returned by create_sdk_mcp_server. Keys
        become the MCP namespace the model sees.
    thread_context
        Prior messages in the thread, oldest first. Optional.
    permission_mode
        SDK permission mode. Default "acceptEdits" auto-approves read-only +
        append tools but prompts for destructive writes. Use "default" for
        agents that need stricter gates (e.g. social-agent's publish path).
    extra_setting_sources
        Additional Claude Code setting sources beyond the user-level default.
    extra_dirs
        Additional dirs to mount via add_dirs (e.g. an agent-specific config dir).
    disallowed_tools
        Names of built-in SDK tools to disable. Default disables Bash, Edit,
        Write, NotebookEdit (agents shouldn't touch the filesystem directly).
    on_complete
        Callback invoked after the reply is posted with a telemetry dict.
        Use this to persist metrics in your agent's metrics module.
    """
    setting_sources = ["user"] + (extra_setting_sources or [])
    add_dirs = [str(DEFAULT_MEMORY_DIR)] + (extra_dirs or [])
    if disallowed_tools is None:
        disallowed_tools = ["Bash", "Edit", "Write", "NotebookEdit"]

    options = ClaudeAgentOptions(
        model=model,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": system_prompt_append,
        },
        mcp_servers=mcp_servers,
        max_turns=max_turns,
        permission_mode=permission_mode,
        setting_sources=setting_sources,
        add_dirs=add_dirs,
        disallowed_tools=disallowed_tools,
        hooks={
            "PostToolUse": [
                HookMatcher(hooks=[_make_post_tool_use_hook(channel_id, thread_ts)])
            ],
        },
    )

    user_msg = _build_user_msg(text, thread_context)
    final_text = ""
    tool_calls: list[str] = []
    usage: dict = {}
    error_msg: str | None = None
    t0 = time.time()

    try:
        async with ClaudeSDKClient(options=options) as agent:
            await agent.query(user_msg)
            async for msg in agent.receive_response():
                msg_type = getattr(msg, "type", None)
                if msg_type == "tool_use":
                    tool_calls.append(getattr(msg, "name", "?"))
                if msg_type == "result":
                    final_text = (getattr(msg, "result", "") or "").strip()
                    usage = getattr(msg, "usage", {}) or {}
    except Exception as exc:
        traceback.print_exc()
        error_msg = f"{type(exc).__name__}: {str(exc)[:300]}"

    if not final_text:
        final_text = (
            f":x: agent error: `{error_msg}`"
            if error_msg
            else "(no reply produced; check Railway logs)"
        )

    posted = post_in_thread(channel_id, thread_ts, final_text)
    duration = time.time() - t0

    if on_complete is not None:
        try:
            on_complete({
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "user_text": text,
                "final_text": final_text,
                "tool_calls": tool_calls,
                "usage": usage,
                "duration_seconds": duration,
                "error": error_msg,
                "reply_ts": posted["ts"],
            })
        except Exception:
            traceback.print_exc()

    return posted["ts"]


def run_ask(
    *,
    text: str,
    channel_id: str,
    thread_ts: str,
    system_prompt_append: str,
    mcp_servers: dict[str, Any],
    thread_context: list[dict] | None = None,
    model: str = DEFAULT_MODEL,
    max_turns: int = DEFAULT_MAX_TURNS,
    permission_mode: str = "acceptEdits",
    extra_setting_sources: list[str] | None = None,
    extra_dirs: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    on_complete: Callable[[dict], None] | None = None,
) -> str:
    """Sync wrapper for run_ask_async. Most agents call this from their listener."""
    return asyncio.run(
        run_ask_async(
            text=text,
            channel_id=channel_id,
            thread_ts=thread_ts,
            system_prompt_append=system_prompt_append,
            mcp_servers=mcp_servers,
            thread_context=thread_context,
            model=model,
            max_turns=max_turns,
            permission_mode=permission_mode,
            extra_setting_sources=extra_setting_sources,
            extra_dirs=extra_dirs,
            disallowed_tools=disallowed_tools,
            on_complete=on_complete,
        )
    )
