"""MCP server exposing Snowflake query as an agent tool.

Tool:
  - snowflake_query : execute a SELECT/SHOW. Returns up to max_rows rows.

Auth: key-pair via env vars (see agent_tools/snowflake/client.py).
"""

from __future__ import annotations

import json

from claude_agent_sdk import create_sdk_mcp_server, tool

from . import client


def _pack(result: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(result)[:30_000]}]}


@tool(
    "snowflake_query",
    "Run a SQL query against Runpod's Snowflake warehouse. SELECT and SHOW only. "
    "Returns up to max_rows rows (default 50). Use for any Runpod actuals: "
    "revenue, spend, conversions, attribution, users. Key tables include "
    "GOLD.MARKETING.AGG_REVENUE_ATTRIBUTION_DAILY (revenue by user x channel x product), "
    "SILVER.FINANCE.REF_REVENUE_TARGET (FY plan), GOLD.DEMAND_GEN.AGG_AD_METRICS_DAILY "
    "(Google Ads). Query INFORMATION_SCHEMA to discover tables and columns.",
    {"sql": str, "max_rows": int},
)
async def snowflake_query(args):
    result = client.query(args["sql"], max_rows=args.get("max_rows", 50))
    return _pack(result)


snowflake_server = create_sdk_mcp_server(
    name="snowflake",
    version="1.0.0",
    tools=[snowflake_query],
)
