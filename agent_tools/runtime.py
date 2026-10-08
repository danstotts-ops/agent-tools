"""Standard agent runtime wrapping ClaudeSDKClient.

Every Slack-driven agent (fpa, cos, strategy, revops, etc.) calls run_ask()
with its own system prompt + MCP servers. The wrapper provides:

  - The Claude Agent SDK loop (multi-turn tool use, retries, streaming)
  - The `claude_code` system-prompt preset so the agent inherits skills,
    memory injection, and the same tool conventions Dan gets locally
  - A PostToolUse hook that posts tool errors back into the Slack thread
    so failures are visible in real time, not buried in Railway logs
  - Optional on_complete callback for telemetry / metrics persistence
  - Per-ask LLM cost telemetry via agent_tools.cost_telemetry (PostHog
    event `agent_llm_call`). Free for any agent that sets AGENT_NAME in
    its environment or passes agent_name=; fire-and-forget, never raises.

Sync entry point: `run_ask(...)`
Async entry point: `run_ask_async(...)`
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import traceback
from pathlib import Path
from typing import Any, Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)


async def _allow_all_tools(tool_name: str, tool_input: dict, context):
    """can_use_tool callback that approves every tool call.

    Replaces permission_mode="bypassPermissions" because the SDK refuses to
    use --dangerously-skip-permissions inside containers running as root
    (which is how Railway runs everything). This callback achieves the same
    behavior -- auto-approve every tool -- without tripping that safety
    check. The runtime's own MCP allowlist (mcp_servers passed in) already
    bounds what tools the model can call.
    """
    return PermissionResultAllow()

from .slack.client import post_in_thread

DEFAULT_MEMORY_DIR = Path.home() / ".claude" / "projects" / "-Users-danstotts" / "memory"
# Model policy (2026-07-24, set by Dan): fleet default is Opus 5. Fable is retired —
# never set this to claude-fable-5. Override per service with AGENT_MODEL (e.g. on
# Railway) for high-frequency mechanical paths that should stay on a cheaper tier.
DEFAULT_MODEL = os.environ.get("AGENT_MODEL", "claude-opus-5")
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


def _apply_output_transform(
    text: str, transform: Callable[[str], str] | None
) -> str:
    """Apply an optional output transform to the model's final answer.

    Used by agents that need a hard, deterministic post-processing pass over
    the reply the SDK produces (e.g. a Slack-mrkdwn cleanup / house-style
    sanitizer that must run even when the system prompt slips). Kept as a
    standalone helper so it is unit-testable without the SDK or Slack.

    Never raises: a None transform or empty text is a no-op, and a transform
    that throws logs and returns the original text so it can never block the
    Slack post.
    """
    if not text or transform is None:
        return text
    try:
        return transform(text)
    except Exception:
        traceback.print_exc()
        return text


def _emit_cost_telemetry(
    *, agent_name: str | None, model: str, usage: dict, purpose: str = "run_ask"
) -> None:
    """Fire one agent_llm_call cost event for a completed ask.

    Agent identity comes from the explicit agent_name argument, falling back
    to the AGENT_NAME env var. Without a name (or without usage data) this is
    a silent no-op. Delegates to agent_tools.cost_telemetry.emit_llm_call,
    which is fire-and-forget and never raises; the extra guard here keeps
    even an import-time surprise from touching the Slack reply path.
    """
    try:
        agent = agent_name or os.environ.get("AGENT_NAME") or ""
        if not agent or not usage:
            return
        from .cost_telemetry import emit_llm_call

        emit_llm_call(
            agent=agent,
            model=model,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
            cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
            purpose=purpose,
        )
    except Exception:
        traceback.print_exc()


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
    permission_mode: str = "default",
    extra_setting_sources: list[str] | None = None,
    extra_dirs: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    on_complete: Callable[[dict], None] | None = None,
    output_transform: Callable[[str], str] | None = None,
    agent_name: str | None = None,
    mention_owner: bool = True,
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
        SDK permission mode. Default "default" pairs with a can_use_tool
        callback that auto-approves every tool. We do NOT use
        "bypassPermissions" because that flag refuses to run inside
        containers as root, which is how Railway runs all of these agents.
        Override to "acceptEdits" or pass a custom callback for agents
        that need stricter gates (e.g. social-agent's publish path).
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
    output_transform
        Optional post-processing applied to the model's final answer before it
        is posted to Slack. Use for a hard house-style / mrkdwn sanitizer that
        must run even when the system prompt slips (e.g. content-review-agent's
        _clean_for_slack em-dash guardrail). Not applied to error fallbacks;
        a transform that raises is logged and ignored.
    agent_name
        Identity used as the distinct_id on the agent_llm_call PostHog cost
        event. Defaults to the AGENT_NAME env var; when neither is set the
        cost event is skipped (everything else still works).
    mention_owner
        Prefix the reply with <@token owner> (default True). Pass False for
        agents in shared channels; replies to an owner's own thread already
        notify him. Mid-run tool-error warnings always tag him.
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
        can_use_tool=_allow_all_tools,
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

    # House-style / mrkdwn sanitizer, opt-in per agent. Runs only on real model
    # output, never on the error fallback below, and never breaks the post.
    final_text = _apply_output_transform(final_text, output_transform)

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
        # The error fallback always tags the owner, since only he can fix it.
        posted = post_in_thread(channel_id, thread_ts, final_text,
                                mention=mention_owner or bool(error_msg))
    duration = time.time() - t0

    # Per-ask cost telemetry (PostHog agent_llm_call). Fire-and-forget;
    # never blocks or breaks the reply path.
    _emit_cost_telemetry(agent_name=agent_name, model=model, usage=usage)

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
    output_transform: Callable[[str], str] | None = None,
    agent_name: str | None = None,
    mention_owner: bool = True,
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
            output_transform=output_transform,
            agent_name=agent_name,
            mention_owner=mention_owner,
        )
    )
