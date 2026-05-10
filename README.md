# agent-tools

Shared MCP servers and standard runtime for Dan's Slack-driven agents.

## Why this exists

All 11 agents (fpa, cos, strategy, revops, video, web, content-review, pmm, marketing-recruiter, social, ea) historically copy-pasted the same tool modules and run loop. Bugs got fixed in one repo and never made it to the others. Slack reliability suffered as a result.

This package centralizes:

- **Standard runtime** (`agent_tools.runtime`): one wrapper around `ClaudeSDKClient` that every agent uses. Handles the agent loop, error-into-Slack hook, telemetry, memory injection, and the `claude_code` system-prompt preset.
- **MCP servers** (`agent_tools.slack`, `agent_tools.snowflake`, `agent_tools.notion`, `agent_tools.drive`, ...): one canonical implementation per service, imported by every agent that needs it.
- **Smoke-test harness** (`agent_tools.smoke`): pre-deploy probe that catches token expiry, schema drift, and MCP misconfiguration before users hit it in Slack.

## Layout

```
agent_tools/
  runtime.py            # ClaudeSDKClient wrapper, run_ask() entry point
  smoke.py              # smoke-test harness
  slack/
    client.py           # Slack API wrapper (lifted from fpa-agent-cloud)
    mcp.py              # MCP server: post_in_thread, list_channel_messages, get_thread
  snowflake/
    client.py           # Snowflake key-pair auth + query()
    mcp.py              # MCP server: snowflake_query
  notion/
    client.py           # Notion search/read/write helpers
    mcp.py              # MCP server: notion_search, notion_get_page, notion_query_database, notion_create_db_row, notion_update_page, notion_append_to_page, notion_replace_page_content
  drive/
    client.py           # Google Drive read-only via OAuth user token
    mcp.py              # MCP server: drive_search_files, drive_get_file_metadata, drive_read_file_content, drive_list_recent_files
```

## Usage from an agent

```python
from agent_tools.runtime import run_ask
from agent_tools.slack.mcp import slack_server
from agent_tools.snowflake.mcp import snowflake_server
from agent_tools.notion.mcp import notion_server
from agent_tools.drive.mcp import drive_server

SYSTEM_PROMPT = (Path(__file__).parent / "system_prompt.md").read_text()

def handle_slack_message(text, channel_id, thread_ts, thread_context=None):
    return run_ask(
        text=text,
        channel_id=channel_id,
        thread_ts=thread_ts,
        system_prompt_append=SYSTEM_PROMPT,
        mcp_servers={
            "slack": slack_server,
            "snowflake": snowflake_server,
            "notion": notion_server,
            "drive": drive_server,
        },
        thread_context=thread_context,
    )
```

## Installation

This is a private package, not on PyPI. Each agent adds it to its `requirements.txt` as a git dependency:

```
agent-tools @ git+https://github.com/danstotts-ops/agent-tools.git@main
```

For local development, install in editable mode:

```bash
pip install -e ~/agent-tools
```

## Environment variables

Each MCP server reads its own credentials from env. Agents must set these in `.env` (local) or Railway env (prod):

- **Slack**: `SLACK_BOT_TOKEN`, `SLACK_USER_TOKEN`
- **Snowflake**: `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PRIVATE_KEY`, `SNOWFLAKE_PRIVATE_KEY_PASSPHRASE`, `SNOWFLAKE_ROLE`, `SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_DATABASE`
- **Notion**: `NOTION_API_TOKEN`
- **Drive**: `DRIVE_ACCESS_TOKEN`, `DRIVE_REFRESH_TOKEN`, `DRIVE_OAUTH_CLIENT_ID`, `DRIVE_OAUTH_CLIENT_SECRET`

## What's NOT here yet

These MCP servers will be added in subsequent phases as agents need them:

- `hubspot` — HubSpot via Snowflake mirror (currently in fpa-agent-cloud)
- `posthog` — PostHog product analytics (fpa, revops)
- `ramp` — Ramp expense data (fpa, cos)
- `linkedin_ads` — LinkedIn ads spend (fpa, social)
- `gong` — Gong call transcripts (revops, marketing-recruiter)
- `fireflies` — Fireflies meeting summaries (cos, ea)
- `gmail` — Gmail read/draft (ea)
- `calendar` — Google Calendar (ea, cos)
- `mode` — Mode Analytics SQL pulls (revops, fpa)

The pattern is identical: lift the existing `*_tools.py` from whichever agent originally implemented it, drop it in `agent_tools/<service>/client.py`, write the `mcp.py` wrapper.

## Status

**Phase 1 (in progress).** Foundation + slack/snowflake/notion/drive servers. fpa-agent will be the first agent migrated onto this package as proof-of-pattern; the rest follow in their own PRs.
