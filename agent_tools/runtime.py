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
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
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
                    if channel_id.startswith("C_SMOKE"):
                        print(f"[runtime] tool `{tool_name}` errored "
                              f"(smoke channel, no Slack post): {str(err)[:300]}",
                              flush=True)
                    else:
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
    permission_mode: str = "bypassPermissions",
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
        SDK permission mode. Default "bypassPermissions" auto-approves all
        tool calls -- the right behavior for an autonomous Slack agent
        where there is no human in the loop to approve each call. Use
        "acceptEdits" or "default" for agents that need stricter gates
        (e.g. social-agent's publish path).
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

    # Track the most recent assistant text in case ResultMessage.result
    # is None (which happens with some SDK paths). The final assistant
    # turn's text is then used as final_text.
    last_assistant_text = ""

    try:
        async with ClaudeSDKClient(options=options) as agent:
            await agent.query(user_msg)
            async for msg in agent.receive_response():
                if isinstance(msg, AssistantMessage):
                    chunks = []
                    for block in msg.content:
                        if isinstance(block, ToolUseBlock):
                            tool_calls.append(block.name)
                        elif isinstance(block, TextBlock):
                            chunks.append(block.text)
                    if chunks:
                        last_assistant_text = "\n".join(chunks).strip()
                elif isinstance(msg, ResultMessage):
                    final_text = (msg.result or last_assistant_text or "").strip()
                    usage = msg.usage or {}
                    if msg.is_error and msg.errors:
                        error_msg = "; ".join(str(e) for e in msg.errors)[:300]
            # Fall back if the loop ended without a ResultMessage producing text.
            if not final_text:
                final_text = last_assistant_text
    except Exception as exc:
        traceback.print_exc()
        error_msg = f"{type(exc).__name__}: {str(exc)[:300]}"

    if not final_text:
        final_text = (
            f":x: agent error: `{error_msg}`"
            if error_msg
            else "(no reply produced; check Railway logs)"
        )

    # Skip the Slack post for smoke channels. Lets `agent_tools.smoke`
    # exercise the agent loop end-to-end without spamming a real channel.
    if channel_id.startswith("C_SMOKE"):
        posted = {"ts": "smoke-no-post"}
        print(f"[runtime] smoke channel; skipping Slack post. final_text="
              f"{final_text[:200]!r}", flush=True)
    else:
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
