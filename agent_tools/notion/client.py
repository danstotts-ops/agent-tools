"""Notion tools for the FP&A agent.

Wraps notion-client. Six core operations:
  - search           : find pages/dbs the integration can see
  - get_page         : read a page (properties + content as markdown)
  - query_database   : query a database with optional filter
  - create_db_row    : create a row in a database (the main write path)
  - update_page      : update properties on an existing page (e.g. mark a vendor 'renewed')
  - replace_content  : nuke + replace a page's body (used for Dashboard refresh)
  - append_to_page   : add blocks to the end of a page (audit log style)

Markdown conversion is intentionally minimal: covers paragraphs, h1/h2/h3,
bullet lists, code blocks, dividers, and to-dos. Tables and rich embeds
round-trip as best-effort plain text.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from notion_client import Client


def _client() -> Client:
    token = os.environ.get("NOTION_API_TOKEN")
    if not token:
        raise RuntimeError("NOTION_API_TOKEN not set in environment.")
    return Client(auth=token)


# --- public tool functions (return dicts, agent-friendly) --------------------

def search(query: str, page_size: int = 10) -> dict:
    try:
        resp = _client().search(query=query, page_size=page_size)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    results = []
    for r in resp.get("results", []):
        kind = r.get("object")
        title = _extract_title(r)
        results.append({
            "id": r.get("id"),
            "kind": kind,
            "title": title,
            "url": r.get("url"),
            "last_edited": r.get("last_edited_time"),
        })
    return {"ok": True, "results": results, "count": len(results)}


def get_page(page_id: str, max_blocks: int = 200) -> dict:
    try:
        c = _client()
        page = c.pages.retrieve(page_id=page_id)
        properties = _extract_properties(page)
        children = c.blocks.children.list(block_id=page_id, page_size=max_blocks)
        markdown = _blocks_to_markdown(children.get("results", []))
        return {
            "ok": True,
            "id": page.get("id"),
            "url": page.get("url"),
            "properties": properties,
            "content_markdown": markdown,
            "block_count": len(children.get("results", [])),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def query_database(database_id: str, filter: dict | None = None, sorts: list | None = None, page_size: int = 50) -> dict:
    """Query rows in a database. Notion split databases into data sources, so we resolve
    the data source under the database first, then query it.

    Filter / sort syntax follows Notion's API:
    https://developers.notion.com/reference/post-database-query
    """
    try:
        c = _client()
        # Resolve data source id (most FP&A Hub databases are single-source)
        db = c.databases.retrieve(database_id=database_id)
        sources = db.get("data_sources") or []
        if not sources:
            return {"ok": False, "error": f"Database {database_id} has no data sources"}
        data_source_id = sources[0]["id"]

        kwargs: dict = {"data_source_id": data_source_id, "page_size": page_size}
        if filter:
            kwargs["filter"] = filter
        if sorts:
            kwargs["sorts"] = sorts
        resp = c.data_sources.query(**kwargs)
        rows = []
        for page in resp.get("results", []):
            rows.append({
                "id": page.get("id"),
                "url": page.get("url"),
                "properties": _extract_properties(page),
                "last_edited": page.get("last_edited_time"),
            })
        return {
            "ok": True,
            "rows": rows,
            "count": len(rows),
            "has_more": resp.get("has_more", False),
            "data_source_id": data_source_id,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _resolve_schema(c: Client, database_id: str) -> dict:
    """Schema lives on the database's data source in v2.7+. Resolve it."""
    db = c.databases.retrieve(database_id=database_id)
    sources = db.get("data_sources") or []
    if not sources:
        return {}
    ds = c.data_sources.retrieve(data_source_id=sources[0]["id"])
    return ds.get("properties", {})


def create_db_row(database_id: str, properties_dict: dict, content_markdown: str | None = None) -> dict:
    """Create a row in a database.

    properties_dict uses simple key=value form, e.g.:
      {"Title": "FY26 - April Close", "Month": "2026-04-01", "Total Plan": 11613760.98}

    The function resolves the data source schema and converts each value to
    the correct Notion property shape based on the column's type.
    """
    try:
        c = _client()
        schema = _resolve_schema(c, database_id)
        if not schema:
            return {"ok": False, "error": f"Could not resolve schema for {database_id}"}
        properties = _build_properties_payload(properties_dict, schema)
        kwargs: dict = {"parent": {"database_id": database_id}, "properties": properties}
        if content_markdown:
            kwargs["children"] = _markdown_to_blocks(content_markdown)
        page = c.pages.create(**kwargs)
        return {"ok": True, "id": page.get("id"), "url": page.get("url")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def update_page(page_id: str, properties_dict: dict | None = None, archived: bool | None = None) -> dict:
    """Update properties on an existing page (typically a database row).

    Pass {"Status": "Done"} to update one or more fields. The function resolves
    the parent database's data source schema to translate values correctly.
    """
    try:
        c = _client()
        kwargs: dict = {"page_id": page_id}
        if properties_dict:
            page = c.pages.retrieve(page_id=page_id)
            parent = page.get("parent", {})
            # Notion may report the parent as either database_id or data_source_id
            if parent.get("type") == "database_id":
                schema = _resolve_schema(c, parent["database_id"])
            elif parent.get("type") == "data_source_id":
                ds = c.data_sources.retrieve(data_source_id=parent["data_source_id"])
                schema = ds.get("properties", {})
            else:
                return {"ok": False, "error": "update_page only supports pages that are rows of a database"}
            if not schema:
                return {"ok": False, "error": "Could not resolve schema for parent"}
            kwargs["properties"] = _build_properties_payload(properties_dict, schema)
        if archived is not None:
            kwargs["archived"] = archived
        if "properties" not in kwargs and "archived" not in kwargs:
            return {"ok": False, "error": "Nothing to update"}
        result = c.pages.update(**kwargs)
        return {"ok": True, "id": result.get("id"), "url": result.get("url")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def replace_content(page_id: str, new_markdown: str) -> dict:
    """Nuke the page's children and write fresh markdown. Used for Dashboard refreshes.

    Destructive. Only call when you intend to replace the page entirely.
    """
    try:
        c = _client()
        existing = c.blocks.children.list(block_id=page_id, page_size=200).get("results", [])
        for b in existing:
            try:
                c.blocks.delete(block_id=b["id"])
            except Exception:
                pass
        new_blocks = _markdown_to_blocks(new_markdown)
        # Append in chunks of 100 (Notion API limit)
        for i in range(0, len(new_blocks), 100):
            c.blocks.children.append(block_id=page_id, children=new_blocks[i : i + 100])
        return {"ok": True, "id": page_id, "blocks_written": len(new_blocks)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def list_recent_pr_coverage(days: int = 28, limit: int = 50) -> dict:
    """cos's PR Coverage Feed (synced from Method's #ext-method-runpod-pr-channel).
    Reference for what Method has been delivering: outlets covered, action
    items from calls, pitch notes. Useful when sizing PR budget value, vendor
    audits on Method retainer, or tying campaign spend to earned coverage.

    DB id: 356ff732-fc34-81ef-81a4-e7e377d2a3a6 (set PR_COVERAGE_DB_ID env).
    """
    db_id = (os.environ.get("PR_COVERAGE_DB_ID")
             or "356ff732-fc34-81ef-81a4-e7e377d2a3a6")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    out = query_database(
        db_id,
        filter={"property": "Date", "date": {"on_or_after": cutoff}},
        sorts=[{"property": "Date", "direction": "descending"}],
        page_size=min(limit, 100),
    )
    if not out.get("ok"):
        return out
    rows = []
    for r in out.get("rows", [])[:limit]:
        props = r.get("properties", {}) or {}
        rows.append({
            "subject": props.get("Subject"),
            "date": props.get("Date"),
            "type": props.get("Type"),
            "author": props.get("Author"),
            "urls": props.get("URLs"),
            "body": (props.get("Body") or "")[:600],
            "slack_ts": props.get("Slack TS"),
        })
    return {"ok": True, "rows": rows, "count": len(rows)}


def append_to_page(page_id: str, markdown: str) -> dict:
    try:
        c = _client()
        new_blocks = _markdown_to_blocks(markdown)
        for i in range(0, len(new_blocks), 100):
            c.blocks.children.append(block_id=page_id, children=new_blocks[i : i + 100])
        return {"ok": True, "id": page_id, "blocks_appended": len(new_blocks)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --- helpers -----------------------------------------------------------------

def _rich_text(s: str) -> list:
    # Notion rich_text content cap is 2000 chars per item
    chunks = []
    for i in range(0, len(s), 2000):
        chunks.append({"type": "text", "text": {"content": s[i : i + 2000]}})
    return chunks


def _extract_title(obj: dict) -> str:
    properties = obj.get("properties", {})
    for prop in properties.values():
        if prop.get("type") == "title":
            arr = prop.get("title", [])
            return "".join(t.get("plain_text", "") for t in arr) or "(untitled)"
    title_arr = obj.get("title", [])
    if title_arr:
        return "".join(t.get("plain_text", "") for t in title_arr)
    return obj.get("id", "")


def _extract_properties(page: dict) -> dict:
    """Return a flat dict of property_name -> simple Python value."""
    out: dict[str, Any] = {}
    for name, prop in (page.get("properties") or {}).items():
        t = prop.get("type")
        if t == "title":
            out[name] = "".join(x.get("plain_text", "") for x in prop.get("title", []))
        elif t == "rich_text":
            out[name] = "".join(x.get("plain_text", "") for x in prop.get("rich_text", []))
        elif t == "number":
            out[name] = prop.get("number")
        elif t == "select":
            sel = prop.get("select")
            out[name] = sel.get("name") if sel else None
        elif t == "multi_select":
            out[name] = [x.get("name") for x in prop.get("multi_select", [])]
        elif t == "status":
            st = prop.get("status")
            out[name] = st.get("name") if st else None
        elif t == "date":
            d = prop.get("date")
            out[name] = (d.get("start") if d else None)
        elif t == "checkbox":
            out[name] = prop.get("checkbox")
        elif t == "url":
            out[name] = prop.get("url")
        elif t == "email":
            out[name] = prop.get("email")
        elif t == "phone_number":
            out[name] = prop.get("phone_number")
        elif t == "people":
            out[name] = [x.get("id") for x in prop.get("people", [])]
        elif t == "files":
            out[name] = [x.get("name") for x in prop.get("files", [])]
        elif t == "formula":
            f = prop.get("formula", {})
            out[name] = f.get(f.get("type") or "string")
        elif t == "unique_id":
            uid = prop.get("unique_id", {})
            prefix = uid.get("prefix")
            num = uid.get("number")
            out[name] = f"{prefix}-{num}" if prefix and num is not None else num
        elif t == "created_time":
            out[name] = prop.get("created_time")
        elif t == "last_edited_time":
            out[name] = prop.get("last_edited_time")
        else:
            out[name] = f"<{t}>"
    return out


def _build_properties_payload(values: dict, schema: dict) -> dict:
    """Convert {col_name: simple_value} into Notion's typed property payload."""
    out = {}
    for name, val in values.items():
        if name not in schema:
            # silently skip unknown fields rather than 400-erroring
            continue
        col = schema[name]
        t = col.get("type")
        if val is None:
            continue
        if t == "title":
            out[name] = {"title": _rich_text(str(val))}
        elif t == "rich_text":
            out[name] = {"rich_text": _rich_text(str(val))}
        elif t == "number":
            out[name] = {"number": float(val) if val != "" else None}
        elif t == "select":
            out[name] = {"select": {"name": str(val)}}
        elif t == "multi_select":
            items = val if isinstance(val, list) else [val]
            out[name] = {"multi_select": [{"name": str(x)} for x in items]}
        elif t == "status":
            out[name] = {"status": {"name": str(val)}}
        elif t == "date":
            if isinstance(val, dict):
                out[name] = {"date": val}
            else:
                out[name] = {"date": {"start": str(val)}}
        elif t == "checkbox":
            out[name] = {"checkbox": bool(val)}
        elif t == "url":
            out[name] = {"url": str(val)}
        elif t == "email":
            out[name] = {"email": str(val)}
        elif t == "phone_number":
            out[name] = {"phone_number": str(val)}
        elif t == "people":
            ids = val if isinstance(val, list) else [val]
            out[name] = {"people": [{"id": x} for x in ids]}
        # Skip read-only / system types: formula, unique_id, created_time, last_edited_time, files
    return out


# --- markdown <-> blocks (minimal) -------------------------------------------

def _markdown_to_blocks(md: str) -> list:
    """Convert markdown to a list of Notion blocks. Minimal coverage but reliable."""
    lines = md.splitlines()
    blocks: list = []
    in_code = False
    code_buf: list[str] = []
    code_lang = "plain text"

    def flush_code() -> None:
        nonlocal code_buf, code_lang
        if code_buf:
            blocks.append({
                "object": "block",
                "type": "code",
                "code": {
                    "rich_text": _rich_text("\n".join(code_buf)),
                    "language": code_lang,
                },
            })
            code_buf = []
            code_lang = "plain text"

    for line in lines:
        stripped = line.rstrip()
        if stripped.startswith("```"):
            if in_code:
                flush_code()
                in_code = False
            else:
                in_code = True
                lang = stripped[3:].strip() or "plain text"
                code_lang = lang
            continue
        if in_code:
            code_buf.append(line)
            continue
        if not stripped:
            continue
        if stripped.startswith("# "):
            blocks.append({"object": "block", "type": "heading_1", "heading_1": {"rich_text": _rich_text(stripped[2:])}})
        elif stripped.startswith("## "):
            blocks.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": _rich_text(stripped[3:])}})
        elif stripped.startswith("### "):
            blocks.append({"object": "block", "type": "heading_3", "heading_3": {"rich_text": _rich_text(stripped[4:])}})
        elif stripped == "---":
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        elif stripped.startswith(("- ", "* ")):
            blocks.append({"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _rich_text(stripped[2:])}})
        elif stripped[:3].rstrip(".") and stripped[:3].rstrip(".").isdigit() and stripped[2:3] == ". ":
            # naive 1-9 numbered lists
            blocks.append({"object": "block", "type": "numbered_list_item", "numbered_list_item": {"rich_text": _rich_text(stripped[3:])}})
        elif stripped.startswith("> "):
            blocks.append({"object": "block", "type": "quote", "quote": {"rich_text": _rich_text(stripped[2:])}})
        else:
            blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rich_text(stripped)}})
    if in_code:
        flush_code()
    return blocks


def _blocks_to_markdown(blocks: list) -> str:
    out: list[str] = []
    for b in blocks:
        t = b.get("type")
        node = b.get(t, {})
        rich = node.get("rich_text", [])
        text = "".join(x.get("plain_text", "") for x in rich)
        if t == "heading_1":
            out.append(f"# {text}")
        elif t == "heading_2":
            out.append(f"## {text}")
        elif t == "heading_3":
            out.append(f"### {text}")
        elif t == "paragraph":
            if text.strip():
                out.append(text)
        elif t == "bulleted_list_item":
            out.append(f"- {text}")
        elif t == "numbered_list_item":
            out.append(f"1. {text}")
        elif t == "to_do":
            checked = node.get("checked")
            box = "[x]" if checked else "[ ]"
            out.append(f"- {box} {text}")
        elif t == "code":
            lang = node.get("language", "")
            out.append(f"```{lang}\n{text}\n```")
        elif t == "quote":
            out.append(f"> {text}")
        elif t == "divider":
            out.append("---")
        elif t == "table":
            out.append("(table - not rendered, view in Notion)")
        elif t == "child_page":
            out.append(f"[child page: {node.get('title', '')}]")
        elif t == "child_database":
            out.append(f"[child database: {node.get('title', '')}]")
        else:
            if text:
                out.append(text)
    return "\n\n".join(out)
