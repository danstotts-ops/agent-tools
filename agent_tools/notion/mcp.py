"""MCP server exposing Notion read/write helpers as agent tools.

Tools (read):
  - notion_search           : find pages/databases the integration can see
  - notion_get_page         : fetch a page's properties + content as markdown
  - notion_query_database   : query a database with optional filter and sorts

Tools (write):
  - notion_create_db_row        : add a row to a database (most common write)
  - notion_update_page          : update properties on an existing page
  - notion_append_to_page       : non-destructive append of markdown blocks
  - notion_replace_page_content : DESTRUCTIVE; replace page body wholesale

Database IDs each agent cares about should be specified in that agent's
system prompt or config; this MCP server is shared across all agents.
"""

from __future__ import annotations

import json

from claude_agent_sdk import create_sdk_mcp_server, tool

from . import client


def _pack(result: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(result)[:30_000]}]}


@tool(
    "notion_search",
    "Search Notion for pages and databases the integration can access. "
    "Returns id, title, kind (page/database), url, last_edited.",
    {"query": str, "page_size": int},
)
async def notion_search(args):
    return _pack(client.search(args["query"], page_size=args.get("page_size", 10)))


@tool(
    "notion_get_page",
    "Fetch a Notion page's properties + content rendered as markdown. "
    "Use to read a Dashboard, a Reference doc, or a single database row. "
    "max_blocks caps how many blocks to render (default 200).",
    {"page_id": str, "max_blocks": int},
)
async def notion_get_page(args):
    return _pack(client.get_page(args["page_id"], max_blocks=args.get("max_blocks", 200)))


@tool(
    "notion_query_database",
    "Query a Notion database with optional filter and sorts. Filter syntax follows "
    "the Notion API (https://developers.notion.com/reference/post-database-query). "
    "Pass page_size to control row count (default 50).",
    {"database_id": str, "filter": dict, "sorts": list, "page_size": int},
)
async def notion_query_database(args):
    return _pack(
        client.query_database(
            args["database_id"],
            filter=args.get("filter"),
            sorts=args.get("sorts"),
            page_size=args.get("page_size", 50),
        )
    )


@tool(
    "notion_create_db_row",
    "Create a new row in a Notion database. Pass properties as a flat dict "
    "(e.g. {\"Title\": \"FY26 April Close\", \"Total Plan\": 11613760.98}). "
    "The function looks up the database schema and translates values to Notion's "
    "typed payload. Optional content_markdown becomes the page body. "
    "Use only when the user explicitly asks to log/create/add a row. "
    "If intent is ambiguous, ask before creating.",
    {"database_id": str, "properties_dict": dict, "content_markdown": str},
)
async def notion_create_db_row(args):
    return _pack(
        client.create_db_row(
            args["database_id"],
            args["properties_dict"],
            content_markdown=args.get("content_markdown"),
        )
    )


@tool(
    "notion_update_page",
    "Update properties on an existing Notion page (typically a database row). "
    "Pass page_id and a flat dict of property updates. Optional archived=true "
    "soft-deletes. Ask before updating if the change is destructive or ambiguous.",
    {"page_id": str, "properties_dict": dict, "archived": bool},
)
async def notion_update_page(args):
    return _pack(
        client.update_page(
            args["page_id"],
            properties_dict=args.get("properties_dict"),
            archived=args.get("archived"),
        )
    )


@tool(
    "notion_append_to_page",
    "Non-destructive: append blocks rendered from markdown to the end of a page. "
    "Use for archive entries, decision-log narratives, or adding notes.",
    {"page_id": str, "markdown": str},
)
async def notion_append_to_page(args):
    return _pack(client.append_to_page(args["page_id"], args["markdown"]))


@tool(
    "notion_replace_page_content",
    "DESTRUCTIVE. Wipes the page's existing content and writes new markdown. "
    "Use ONLY when the user explicitly says 'replace' or 'overwrite', or for "
    "known dashboard-refresh workflows the agent's system prompt lists by ID. "
    "Always confirm with the user before calling this on any other page.",
    {"page_id": str, "new_markdown": str},
)
async def notion_replace_page_content(args):
    return _pack(client.replace_content(args["page_id"], args["new_markdown"]))


notion_server = create_sdk_mcp_server(
    name="notion",
    version="1.0.0",
    tools=[
        notion_search,
        notion_get_page,
        notion_query_database,
        notion_create_db_row,
        notion_update_page,
        notion_append_to_page,
        notion_replace_page_content,
    ],
)
