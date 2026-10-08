"""post_in_thread tags the token owner by default; mention=False posts the text as is.

Shared-channel agents (content-review-agent) pass run_ask(mention_owner=False)
so a teammate's review reply is not prefixed with <@dan>.
"""
import pytest

from agent_tools.slack import client


@pytest.fixture
def posted(monkeypatch):
    sent = []

    class FakeBot:
        def chat_postMessage(self, **kw):
            sent.append(kw["text"])
            return {"ts": "1.0"}

    monkeypatch.setattr(client, "_bot_client", lambda: FakeBot())
    monkeypatch.setattr(client, "_mention_self", lambda: "<@UDAN>")
    return sent


def test_default_mentions_owner(posted):
    client.post_in_thread("C1", "1.0", "hello")
    assert posted == ["<@UDAN> hello"]


def test_mention_false_posts_plain_text(posted):
    client.post_in_thread("C1", "1.0", "hello", mention=False)
    assert posted == ["hello"]
