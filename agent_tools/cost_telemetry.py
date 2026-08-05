"""Canonical per-call LLM cost telemetry for the fleet.

One event per LLM call, captured in PostHog (project Runpod, id 105711,
US cloud) as ``agent_llm_call`` with the agent name as the distinct_id.
Every agent emits through this module so "which agent costs what" is
answerable from PostHog alone, regardless of where the agent runs or
whether its local disk survives a redeploy.

Usage:

    from agent_tools.cost_telemetry import emit_llm_call

    emit_llm_call(
        agent="cos-agent",
        model="claude-opus-5",
        input_tokens=1200,
        output_tokens=340,
        cache_read_tokens=8000,
        cache_write_tokens=0,
        purpose="route_objective",
    )

Guarantees:

- ``emit_llm_call`` never raises and never blocks the caller. Delivery
  happens on a daemon thread with a short timeout; on any failure it logs
  a warning and drops the event. Telemetry must never take an agent down.
- ``cost_usd`` is computed from ``PRICES_PER_MTOK``. Unknown models emit
  ``cost_usd=None`` alongside the raw token counts so cost can be
  backfilled once the rate is known. Do not invent rates.

Environment:

- ``POSTHOG_PROJECT_API_KEY`` overrides the default public capture token.
- ``POSTHOG_CAPTURE_URL`` overrides the capture endpoint.
- ``AGENT_COST_TELEMETRY_DISABLED=1`` disables emission entirely (tests,
  local one-offs). Cost is still computed and returned.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request

log = logging.getLogger("agent_tools.cost_telemetry")

# Public capture token for the Runpod PostHog project (id 105711, US cloud).
# Capture tokens are write-only and safe to embed; override via env.
DEFAULT_POSTHOG_PROJECT_API_KEY = "phc_AYL8fNwqZy4Rf4bhpWpA2guti7HHK4gudCr7GtZSgH8j"
DEFAULT_POSTHOG_CAPTURE_URL = "https://us.i.posthog.com/i/v0/e/"
EVENT_NAME = "agent_llm_call"
REQUEST_TIMEOUT_S = 3.0

# USD per million tokens: input, output, cache read, cache write (5-minute
# TTL writes). Rates are Anthropic's published price list; cache read is
# 0.1x input and cache write 1.25x input, the same multipliers the fleet
# already uses in fpa-agent-cloud/agent/metrics.py. When a new model ships,
# add a row here; an unknown model emits cost_usd=None (tokens included) so
# cost can be backfilled rather than fabricated.
PRICES_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-opus-5": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-5[1m]": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-sonnet-5": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25},
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25},
}


def compute_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float | None:
    """USD cost of one call, or None when the model has no known rate."""
    prices = PRICES_PER_MTOK.get(model)
    if prices is None:
        return None
    cost = (
        (input_tokens or 0) * prices["input"]
        + (output_tokens or 0) * prices["output"]
        + (cache_read_tokens or 0) * prices["cache_read"]
        + (cache_write_tokens or 0) * prices["cache_write"]
    ) / 1_000_000
    return round(cost, 6)


def emit_llm_call(
    *,
    agent: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    purpose: str | None = None,
    properties: dict | None = None,
    block: bool = False,
) -> float | None:
    """Compute cost_usd and capture one ``agent_llm_call`` PostHog event.

    Fire-and-forget: delivery runs on a daemon thread (unless ``block=True``,
    used by tests and smoke checks) with a ~3s timeout. Never raises; on any
    failure a warning is logged and the event is dropped. Returns the
    computed cost_usd (None for unknown models) either way.
    """
    cost: float | None = None
    try:
        cost = compute_cost_usd(
            model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
        )
        if os.environ.get("AGENT_COST_TELEMETRY_DISABLED"):
            return cost
        props: dict = {
            "agent": agent,
            "model": model,
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "cache_read_tokens": int(cache_read_tokens or 0),
            "cache_write_tokens": int(cache_write_tokens or 0),
            "cost_usd": cost,
        }
        if purpose:
            props["purpose"] = purpose
        if properties:
            props.update(properties)
        event = {
            "api_key": os.environ.get("POSTHOG_PROJECT_API_KEY")
            or DEFAULT_POSTHOG_PROJECT_API_KEY,
            "event": EVENT_NAME,
            "distinct_id": agent,
            "properties": props,
        }
        if block:
            _post_safe(event)
        else:
            threading.Thread(target=_post_safe, args=(event,), daemon=True).start()
    except Exception:  # noqa: BLE001 - telemetry must never break the caller
        log.warning("cost telemetry emission failed; event dropped", exc_info=True)
    return cost


def capture(event: dict) -> int:
    """Raw synchronous capture POST. Returns the HTTP status; raises on
    failure. Internal + smoke-test use; production paths go through
    emit_llm_call, which wraps this in the never-raise guarantee."""
    url = os.environ.get("POSTHOG_CAPTURE_URL") or DEFAULT_POSTHOG_CAPTURE_URL
    req = urllib.request.Request(
        url,
        data=json.dumps(event).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
        return resp.status


def _post_safe(event: dict) -> None:
    try:
        status = capture(event)
        if status >= 400:
            log.warning("PostHog capture returned HTTP %s; event dropped", status)
    except Exception as exc:  # noqa: BLE001
        log.warning("PostHog capture failed (%s); event dropped", exc)
