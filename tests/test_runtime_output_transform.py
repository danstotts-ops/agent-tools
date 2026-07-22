"""Unit tests for the opt-in output_transform hook in run_ask_async.

The hook lets an agent run a hard house-style / mrkdwn sanitizer over the
model's final answer before it is posted to Slack (e.g. content-review-agent's
_clean_for_slack em-dash guardrail, which the SDK ask path otherwise bypasses).
The application is isolated in _apply_output_transform so it is testable without
the SDK or Slack.
"""
from agent_tools.runtime import _apply_output_transform


def test_none_transform_is_noop():
    assert _apply_output_transform("hello — world", None) == "hello — world"


def test_empty_text_is_noop():
    called = []
    assert _apply_output_transform("", lambda t: called.append(t) or "x") == ""
    assert called == []  # transform never invoked on empty text


def test_transform_is_applied():
    # Mirrors the content-review-agent em-dash guardrail (spaced dash -> comma).
    import re

    em = re.compile(r"[ \t]*[—–][ \t]*")
    out = _apply_output_transform("a — b", lambda t: em.sub(", ", t))
    assert out == "a, b"


def test_transform_exception_returns_original():
    def boom(_text):
        raise ValueError("kaboom")

    # A failing transform must never block the Slack post; original is returned.
    assert _apply_output_transform("keep me", boom) == "keep me"
