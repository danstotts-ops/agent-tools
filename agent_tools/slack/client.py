"""Thin Slack helpers for the FP&A agent. Mirrors the relevant subset of EA agent."""

from __future__ import annotations

import os
from functools import lru_cache

from slack_sdk import WebClient


def _client() -> WebClient:
    """User-token client for reads (history, threads)."""
    token = (os.environ.get("SLACK_USER_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("SLACK_USER_TOKEN not set in environment.")
    return WebClient(token=token)


def _bot_client() -> WebClient:
    """Bot-token client for posting. Bot posts that mention Dan trigger a real
    Slack notification; user-token self-posts do not."""
    token = (os.environ.get("SLACK_BOT_TOKEN") or os.environ.get("SLACK_USER_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("Neither SLACK_BOT_TOKEN nor SLACK_USER_TOKEN set.")
    return WebClient(token=token)


@lru_cache(maxsize=1)
def my_user_id() -> str:
    return _client().auth_test()["user_id"]


def _mention_self() -> str:
    return f"<@{my_user_id()}>"


def list_channel_messages(channel: str, limit: int = 100) -> list[dict]:
    resp = _client().conversations_history(channel=channel, limit=limit)
    return resp.get("messages", [])


def get_thread(channel: str, thread_ts: str) -> dict:
    resp = _client().conversations_replies(channel=channel, ts=thread_ts, limit=200)
    return {"messages": resp.get("messages", [])}


def post_in_thread(channel: str, thread_ts: str, text: str) -> dict:
    """Threaded reply via bot token, prefixed with @Dan so it triggers a notification."""
    msg = f"{_mention_self()} {text}"
    resp = _bot_client().chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=msg,
        unfurl_links=False,
        unfurl_media=False,
    )
    return {"ts": resp["ts"]}


def post_message(channel: str, text: str) -> dict:
    """Top-level post via bot token, prefixed with @Dan so it triggers a notification."""
    msg = f"{_mention_self()} {text}"
    resp = _bot_client().chat_postMessage(
        channel=channel,
        text=msg,
        unfurl_links=False,
        unfurl_media=False,
    )
    return {"ts": resp["ts"]}
