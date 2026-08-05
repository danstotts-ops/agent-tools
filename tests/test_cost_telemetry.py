"""Unit tests for agent_tools.cost_telemetry.

Two properties matter most:
1. Cost math is exact against the published per-MTok rates (unknown models
   yield None, never a guessed number).
2. Emission never raises and never blocks the caller, even with a dead or
   unroutable sink. Telemetry must not be able to take an agent down.
"""
from __future__ import annotations

import time

import pytest

from agent_tools import cost_telemetry
from agent_tools.cost_telemetry import compute_cost_usd, emit_llm_call


# ---- cost math -------------------------------------------------------------

def test_opus_5_input_output_only():
    # 1M in at $5 + 1M out at $25
    assert compute_cost_usd("claude-opus-5", 1_000_000, 1_000_000) == 30.0


def test_sonnet_5_rates():
    # 2M in at $3 + 1M out at $15
    assert compute_cost_usd("claude-sonnet-5", 2_000_000, 1_000_000) == 21.0


def test_haiku_with_cache_tokens():
    # 1M in ($1.00) + 0 out + 1M cache read ($0.10) + 1M cache write ($1.25)
    got = compute_cost_usd(
        "claude-haiku-4-5", 1_000_000, 0,
        cache_read_tokens=1_000_000, cache_write_tokens=1_000_000,
    )
    assert got == pytest.approx(2.35)


def test_cache_multipliers_track_input_price():
    # Cache read is 0.1x input, cache write 1.25x input, for every model.
    for model, prices in cost_telemetry.PRICES_PER_MTOK.items():
        assert prices["cache_read"] == pytest.approx(prices["input"] * 0.1), model
        assert prices["cache_write"] == pytest.approx(prices["input"] * 1.25), model


def test_unknown_model_returns_none_not_a_guess():
    assert compute_cost_usd("claude-mystery-9", 1_000_000, 1_000_000) is None


def test_zero_and_none_tokens_are_safe():
    assert compute_cost_usd("claude-opus-5", 0, 0) == 0.0
    assert compute_cost_usd("claude-opus-5", None, None) == 0.0  # type: ignore[arg-type]


# ---- never-raises / fire-and-forget ----------------------------------------

def test_emit_never_raises_with_dead_sink(monkeypatch):
    """A connection-refused capture endpoint must not raise or hang."""
    monkeypatch.delenv("AGENT_COST_TELEMETRY_DISABLED", raising=False)
    monkeypatch.setenv("POSTHOG_CAPTURE_URL", "http://127.0.0.1:1/")
    t0 = time.monotonic()
    cost = emit_llm_call(
        agent="test-agent", model="claude-haiku-4-5",
        input_tokens=1000, output_tokens=100, block=True,
    )
    assert cost == pytest.approx((1000 * 1.00 + 100 * 5.00) / 1_000_000)
    assert time.monotonic() - t0 < cost_telemetry.REQUEST_TIMEOUT_S + 2


def test_emit_nonblocking_returns_immediately(monkeypatch):
    monkeypatch.delenv("AGENT_COST_TELEMETRY_DISABLED", raising=False)
    monkeypatch.setenv("POSTHOG_CAPTURE_URL", "http://127.0.0.1:1/")
    t0 = time.monotonic()
    emit_llm_call(
        agent="test-agent", model="claude-haiku-4-5",
        input_tokens=1, output_tokens=1,
    )
    assert time.monotonic() - t0 < 0.5  # thread spawn, no network wait


def test_emit_disabled_env_skips_network(monkeypatch):
    monkeypatch.setenv("AGENT_COST_TELEMETRY_DISABLED", "1")

    def boom(_event):
        raise AssertionError("capture must not be called when disabled")

    monkeypatch.setattr(cost_telemetry, "capture", boom)
    cost = emit_llm_call(
        agent="test-agent", model="claude-opus-5",
        input_tokens=1_000_000, output_tokens=0, block=True,
    )
    assert cost == 5.0


def test_emit_survives_capture_exception(monkeypatch):
    monkeypatch.delenv("AGENT_COST_TELEMETRY_DISABLED", raising=False)

    def boom(_event):
        raise RuntimeError("posthog is down")

    monkeypatch.setattr(cost_telemetry, "capture", boom)
    # block=True exercises the failure path synchronously; must not raise.
    cost = emit_llm_call(
        agent="test-agent", model="claude-sonnet-5",
        input_tokens=100, output_tokens=100, block=True,
    )
    assert cost is not None


# ---- payload shape ----------------------------------------------------------

def test_event_payload_shape(monkeypatch):
    monkeypatch.delenv("AGENT_COST_TELEMETRY_DISABLED", raising=False)
    monkeypatch.delenv("POSTHOG_PROJECT_API_KEY", raising=False)
    seen: list[dict] = []
    monkeypatch.setattr(cost_telemetry, "capture", lambda e: seen.append(e) or 200)

    emit_llm_call(
        agent="cos-agent", model="claude-opus-5",
        input_tokens=1200, output_tokens=340,
        cache_read_tokens=8000, cache_write_tokens=50,
        purpose="route_objective",
        properties={"success": True},
        block=True,
    )

    assert len(seen) == 1
    event = seen[0]
    assert event["event"] == "agent_llm_call"
    assert event["distinct_id"] == "cos-agent"
    assert event["api_key"] == cost_telemetry.DEFAULT_POSTHOG_PROJECT_API_KEY
    props = event["properties"]
    assert props["agent"] == "cos-agent"
    assert props["model"] == "claude-opus-5"
    assert props["input_tokens"] == 1200
    assert props["output_tokens"] == 340
    assert props["cache_read_tokens"] == 8000
    assert props["cache_write_tokens"] == 50
    assert props["purpose"] == "route_objective"
    assert props["success"] is True
    assert props["cost_usd"] == round(
        (1200 * 5.00 + 340 * 25.00 + 8000 * 0.50 + 50 * 6.25) / 1_000_000, 6
    )


def test_unknown_model_event_carries_tokens_and_null_cost(monkeypatch):
    monkeypatch.delenv("AGENT_COST_TELEMETRY_DISABLED", raising=False)
    seen: list[dict] = []
    monkeypatch.setattr(cost_telemetry, "capture", lambda e: seen.append(e) or 200)

    cost = emit_llm_call(
        agent="x", model="claude-mystery-9",
        input_tokens=5, output_tokens=7, block=True,
    )
    assert cost is None
    assert seen[0]["properties"]["cost_usd"] is None
    assert seen[0]["properties"]["input_tokens"] == 5
    assert seen[0]["properties"]["output_tokens"] == 7


# ---- runtime hook -----------------------------------------------------------

def test_runtime_helper_uses_agent_name_env(monkeypatch):
    from agent_tools import runtime

    seen: list[dict] = []

    def fake_emit(**kwargs):
        seen.append(kwargs)
        return 0.0

    monkeypatch.setattr(cost_telemetry, "emit_llm_call", fake_emit)
    monkeypatch.setenv("AGENT_NAME", "env-agent")
    runtime._emit_cost_telemetry(
        agent_name=None,
        model="claude-opus-5",
        usage={
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": 40,
        },
    )
    assert seen == [{
        "agent": "env-agent",
        "model": "claude-opus-5",
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_read_tokens": 30,
        "cache_write_tokens": 40,
        "purpose": "run_ask",
    }]


def test_runtime_helper_skips_without_identity_or_usage(monkeypatch):
    from agent_tools import runtime

    def boom(**_kwargs):
        raise AssertionError("must not emit without an agent name or usage")

    monkeypatch.setattr(cost_telemetry, "emit_llm_call", boom)
    monkeypatch.delenv("AGENT_NAME", raising=False)
    runtime._emit_cost_telemetry(agent_name=None, model="m", usage={"input_tokens": 1})
    runtime._emit_cost_telemetry(agent_name="a", model="m", usage={})
