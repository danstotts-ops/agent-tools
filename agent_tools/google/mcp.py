"""SDK MCP servers for Google services, one per product so agents mount only
what they need:

  ga4_server     - GA4 reporting + admin
  gsc_server     - Search Console
  gtm_server     - Tag Manager
  youtube_server - YouTube Data

Read tools are always safe. Write/admin tools take dry_run (default True) and
only mutate when called with dry_run=False.
"""

from __future__ import annotations

import json

from claude_agent_sdk import create_sdk_mcp_server, tool

from . import client


def _pack(result) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(result)[:30_000]}]}


def _gated(dry_run: bool, action: str, request: dict, execute):
    if dry_run:
        return _pack(
            {
                "dry_run": True,
                "action": action,
                "would_send": request,
                "note": "No change made. Re-call with dry_run=false to execute.",
            }
        )
    return _pack({"dry_run": False, "action": action, "result": execute()})


# ----------------------------- GA4 -----------------------------

@tool(
    "ga4_list_account_summaries",
    "List GA4 accounts and their properties (display names + numeric property IDs "
    "needed for reporting). For runpod.io use property 312964275.",
    {},
)
async def ga4_list_account_summaries(args):
    return _pack(client.ga4_account_summaries())


@tool(
    "ga4_run_report",
    "Run a GA4 core report. property_id is numeric (e.g. 312964275). dimensions "
    "e.g. ['date','sessionDefaultChannelGroup']; metrics e.g. "
    "['activeUsers','sessions','conversions']. Dates accept GA4 keywords "
    "(today, yesterday, NdaysAgo) or YYYY-MM-DD.",
    {
        "property_id": str,
        "start_date": str,
        "end_date": str,
        "dimensions": list,
        "metrics": list,
        "limit": int,
    },
)
async def ga4_run_report(args):
    return _pack(
        client.ga4_run_report(
            property_id=args["property_id"],
            start_date=args.get("start_date", "7daysAgo"),
            end_date=args.get("end_date", "yesterday"),
            dimensions=args.get("dimensions"),
            metrics=args.get("metrics"),
            limit=args.get("limit", 50),
        )
    )


@tool(
    "ga4_run_realtime_report",
    "Run a GA4 realtime report (last ~30 min). property_id is numeric.",
    {"property_id": str, "dimensions": list, "metrics": list, "limit": int},
)
async def ga4_run_realtime_report(args):
    return _pack(
        client.ga4_run_realtime_report(
            property_id=args["property_id"],
            dimensions=args.get("dimensions"),
            metrics=args.get("metrics"),
            limit=args.get("limit", 50),
        )
    )


@tool(
    "ga4_list_custom_dimensions",
    "List custom dimensions configured on a GA4 property. property_id is numeric.",
    {"property_id": str},
)
async def ga4_list_custom_dimensions(args):
    return _pack(client.ga4_list_custom_dimensions(args["property_id"]))


@tool(
    "ga4_update_property_display_name",
    "Rename a GA4 property. ADMIN WRITE. dry_run defaults True; pass dry_run=false "
    "to apply.",
    {"property_id": str, "display_name": str, "dry_run": bool},
)
async def ga4_update_property_display_name(args):
    return _gated(
        args.get("dry_run", True),
        "ga4.properties.patch(displayName)",
        {"property_id": args["property_id"], "display_name": args["display_name"]},
        lambda: client.ga4_update_property_display_name(
            args["property_id"], args["display_name"]
        ),
    )


@tool(
    "ga4_create_custom_dimension",
    "Create a GA4 custom dimension. ADMIN WRITE. scope is EVENT or USER. dry_run "
    "defaults True.",
    {
        "property_id": str,
        "parameter_name": str,
        "display_name": str,
        "scope": str,
        "dry_run": bool,
    },
)
async def ga4_create_custom_dimension(args):
    return _gated(
        args.get("dry_run", True),
        "ga4.properties.customDimensions.create",
        {
            "property_id": args["property_id"],
            "parameter_name": args["parameter_name"],
            "display_name": args["display_name"],
            "scope": args.get("scope", "EVENT"),
        },
        lambda: client.ga4_create_custom_dimension(
            args["property_id"],
            args["parameter_name"],
            args["display_name"],
            args.get("scope", "EVENT"),
        ),
    )


ga4_server = create_sdk_mcp_server(
    name="ga4",
    version="1.0.0",
    tools=[
        ga4_list_account_summaries,
        ga4_run_report,
        ga4_run_realtime_report,
        ga4_list_custom_dimensions,
        ga4_update_property_display_name,
        ga4_create_custom_dimension,
    ],
)


# ------------------------ Search Console ------------------------

@tool(
    "gsc_list_sites",
    "List Search Console sites the user can access (e.g. sc-domain:runpod.io).",
    {},
)
async def gsc_list_sites(args):
    return _pack(client.gsc_list_sites())


@tool(
    "gsc_query_search_analytics",
    "Query Search Console search analytics. site_url e.g. 'sc-domain:runpod.io'. "
    "dimensions any of ['query','page','country','device','date']. Dates YYYY-MM-DD. "
    "search_type web|image|video|news.",
    {
        "site_url": str,
        "start_date": str,
        "end_date": str,
        "dimensions": list,
        "row_limit": int,
        "search_type": str,
    },
)
async def gsc_query_search_analytics(args):
    return _pack(
        client.gsc_query(
            site_url=args["site_url"],
            start_date=args["start_date"],
            end_date=args["end_date"],
            dimensions=args.get("dimensions"),
            row_limit=args.get("row_limit", 50),
            search_type=args.get("search_type", "web"),
        )
    )


@tool(
    "gsc_list_sitemaps",
    "List submitted sitemaps for a Search Console property.",
    {"site_url": str},
)
async def gsc_list_sitemaps(args):
    return _pack(client.gsc_list_sitemaps(args["site_url"]))


@tool(
    "gsc_submit_sitemap",
    "Submit a sitemap to Search Console. WRITE. feedpath is the full sitemap URL. "
    "dry_run defaults True.",
    {"site_url": str, "feedpath": str, "dry_run": bool},
)
async def gsc_submit_sitemap(args):
    return _gated(
        args.get("dry_run", True),
        "searchconsole.sitemaps.submit",
        {"site_url": args["site_url"], "feedpath": args["feedpath"]},
        lambda: client.gsc_submit_sitemap(args["site_url"], args["feedpath"]),
    )


gsc_server = create_sdk_mcp_server(
    name="gsc",
    version="1.0.0",
    tools=[gsc_list_sites, gsc_query_search_analytics, gsc_list_sitemaps, gsc_submit_sitemap],
)


# ------------------------- Tag Manager -------------------------

@tool("gtm_list_accounts", "List GTM accounts the user can access.", {})
async def gtm_list_accounts(args):
    return _pack(client.gtm_list_accounts())


@tool("gtm_list_containers", "List containers under a GTM account.", {"account_id": str})
async def gtm_list_containers(args):
    return _pack(client.gtm_list_containers(args["account_id"]))


@tool(
    "gtm_list_workspaces",
    "List workspaces in a GTM container.",
    {"account_id": str, "container_id": str},
)
async def gtm_list_workspaces(args):
    return _pack(client.gtm_list_workspaces(args["account_id"], args["container_id"]))


@tool(
    "gtm_list_tags",
    "List tags in a GTM workspace.",
    {"account_id": str, "container_id": str, "workspace_id": str},
)
async def gtm_list_tags(args):
    return _pack(
        client.gtm_list_tags(args["account_id"], args["container_id"], args["workspace_id"])
    )


@tool(
    "gtm_list_triggers",
    "List triggers in a GTM workspace.",
    {"account_id": str, "container_id": str, "workspace_id": str},
)
async def gtm_list_triggers(args):
    return _pack(
        client.gtm_list_triggers(args["account_id"], args["container_id"], args["workspace_id"])
    )


@tool(
    "gtm_create_tag",
    "Create a tag in a GTM workspace (not published). WRITE. tag is a GTM Tag "
    "resource dict. dry_run defaults True.",
    {"account_id": str, "container_id": str, "workspace_id": str, "tag": dict, "dry_run": bool},
)
async def gtm_create_tag(args):
    return _gated(
        args.get("dry_run", True),
        "tagmanager.workspaces.tags.create",
        {
            "account_id": args["account_id"],
            "container_id": args["container_id"],
            "workspace_id": args["workspace_id"],
            "tag": args["tag"],
        },
        lambda: client.gtm_create_tag(
            args["account_id"], args["container_id"], args["workspace_id"], args["tag"]
        ),
    )


@tool(
    "gtm_create_version",
    "Freeze a GTM workspace into a new container version (not published). WRITE. "
    "dry_run defaults True.",
    {
        "account_id": str,
        "container_id": str,
        "workspace_id": str,
        "name": str,
        "notes": str,
        "dry_run": bool,
    },
)
async def gtm_create_version(args):
    return _gated(
        args.get("dry_run", True),
        "tagmanager.workspaces.create_version",
        {
            "account_id": args["account_id"],
            "container_id": args["container_id"],
            "workspace_id": args["workspace_id"],
            "name": args["name"],
        },
        lambda: client.gtm_create_version(
            args["account_id"],
            args["container_id"],
            args["workspace_id"],
            args["name"],
            args.get("notes", ""),
        ),
    )


@tool(
    "gtm_publish_version",
    "Publish a GTM container version live. SENSITIVE WRITE (pushes tags to "
    "production). dry_run defaults True; set dry_run=false only with explicit approval.",
    {"account_id": str, "container_id": str, "version_id": str, "dry_run": bool},
)
async def gtm_publish_version(args):
    return _gated(
        args.get("dry_run", True),
        "tagmanager.versions.publish",
        {
            "account_id": args["account_id"],
            "container_id": args["container_id"],
            "version_id": args["version_id"],
        },
        lambda: client.gtm_publish_version(
            args["account_id"], args["container_id"], args["version_id"]
        ),
    )


gtm_server = create_sdk_mcp_server(
    name="gtm",
    version="1.0.0",
    tools=[
        gtm_list_accounts,
        gtm_list_containers,
        gtm_list_workspaces,
        gtm_list_tags,
        gtm_list_triggers,
        gtm_create_tag,
        gtm_create_version,
        gtm_publish_version,
    ],
)


# --------------------------- YouTube ---------------------------

@tool(
    "yt_list_my_channels",
    "List the authenticated user's YouTube channels (id, title, stats).",
    {},
)
async def yt_list_my_channels(args):
    return _pack(client.yt_list_my_channels())


@tool(
    "yt_list_channel_videos",
    "List a channel's recent videos (most recent first).",
    {"channel_id": str, "max_results": int},
)
async def yt_list_channel_videos(args):
    return _pack(
        client.yt_list_channel_videos(args["channel_id"], args.get("max_results", 25))
    )


@tool(
    "yt_get_video",
    "Get full details for a video (snippet, status, statistics).",
    {"video_id": str},
)
async def yt_get_video(args):
    return _pack(client.yt_get_video(args["video_id"]))


@tool(
    "yt_update_video_metadata",
    "Update a video's snippet (title/description/tags). WRITE. Only provided fields "
    "change. dry_run defaults True.",
    {
        "video_id": str,
        "title": str,
        "description": str,
        "tags": list,
        "category_id": str,
        "dry_run": bool,
    },
)
async def yt_update_video_metadata(args):
    return _gated(
        args.get("dry_run", True),
        "youtube.videos.update(snippet)",
        {k: v for k, v in args.items() if k != "dry_run"},
        lambda: client.yt_update_video_metadata(
            video_id=args["video_id"],
            title=args.get("title"),
            description=args.get("description"),
            tags=args.get("tags"),
            category_id=args.get("category_id"),
        ),
    )


youtube_server = create_sdk_mcp_server(
    name="youtube",
    version="1.0.0",
    tools=[yt_list_my_channels, yt_list_channel_videos, yt_get_video, yt_update_video_metadata],
)


# Convenience map for agents that want everything.
GOOGLE_SERVERS = {
    "ga4": ga4_server,
    "gsc": gsc_server,
    "gtm": gtm_server,
    "youtube": youtube_server,
}
