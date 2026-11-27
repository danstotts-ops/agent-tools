"""Linear as the shared context bus for the marketing agent fleet.

One team (MAGENT by default) holds the work. Conventions:
  - One issue per objective, never one per scheduler tick.
  - The label ``agent:<slug>`` names the owning agent.
  - The issue description is the brief (goal, inputs, definition of done).
  - Comments are the handoff log. Agents post them with ``post_handoff`` so
    every entry has the same machine-readable shape.
  - A project document titled "Current state" is the standing context for a
    program; any agent working an issue in that project reads it first.
  - ``blocks`` relations carry cross-agent dependencies.

Three public calls cover the loop:
  read_context(agent)            at the start of a run
  post_handoff(issue, agent, ..) at the end of a run
  open_objective(agent, title, ..) when one agent hands work to another

Every public function returns an ``{"ok": bool, ...}`` dict and never raises,
so a Linear outage cannot take an agent run down with it.

Auth: ``LINEAR_API_KEY`` (fleet key), falling back to ``COS_LINEAR_API_KEY``.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from typing import Any, Iterable

import requests

API_URL = "https://api.linear.app/graphql"
DEFAULT_TEAM = os.environ.get("AGENT_LINEAR_TEAM", "MAGENT")
LABEL_PREFIX = "agent:"
CONTEXT_DOC_TITLE = "current state"
OPEN_STATE_TYPES = ["triage", "backlog", "unstarted", "started"]

# Every comment or issue this module writes starts with one of these markers.
# Webhook handlers use is_agent_generated() to drop them, which is the
# bot-loop guard (fleet-hygiene check #5) for anything built on this bus.
HANDOFF_MARKER = "<!-- agent-handoff:v1"
OBJECTIVE_MARKER = "<!-- agent-objective:v1"

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
_SHA_RE = re.compile(r"sha=([0-9a-f]{12})")
_TIMEOUT_S = 20


class LinearError(RuntimeError):
    pass


# --- transport ---------------------------------------------------------------

def _api_key() -> str:
    key = os.environ.get("LINEAR_API_KEY") or os.environ.get("COS_LINEAR_API_KEY")
    if not key:
        raise LinearError("LINEAR_API_KEY (or COS_LINEAR_API_KEY) not set in environment.")
    return key


def _post(payload: dict) -> dict:
    """Single HTTP seam. Tests monkeypatch this."""
    resp = requests.post(
        API_URL,
        json=payload,
        headers={"Authorization": _api_key(), "Content-Type": "application/json"},
        timeout=_TIMEOUT_S,
    )
    try:
        body = resp.json()
    except ValueError:
        raise LinearError(f"Linear returned HTTP {resp.status_code} with a non-JSON body")
    return body


def _gql(query: str, variables: dict | None = None) -> dict:
    body = _post({"query": query, "variables": variables or {}})
    if body.get("errors"):
        msg = "; ".join(e.get("message", "unknown error") for e in body["errors"])
        raise LinearError(msg)
    return body.get("data") or {}


# --- helpers -----------------------------------------------------------------

def label_for(agent: str) -> str:
    if not _SLUG_RE.match(agent or ""):
        raise ValueError(f"Invalid agent slug {agent!r}: use lowercase letters, digits, - or _")
    return f"{LABEL_PREFIX}{agent}"


def is_agent_generated(text: str | None) -> bool:
    """True for any comment or issue body written through this module."""
    if not text:
        return False
    head = text.lstrip()[:80]
    return head.startswith(HANDOFF_MARKER) or head.startswith(OBJECTIVE_MARKER)


def _clip(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 15].rstrip() + "\n[truncated]"


def _bullets(items: Iterable[str] | None) -> list[str]:
    return [f"- {str(i).strip()}" for i in (items or []) if str(i).strip()]


# --- read_context ------------------------------------------------------------

_CONTEXT_QUERY = """
query AgentContext($team: String!, $label: String!, $states: [String!], $first: Int!) {
  issues(
    first: $first
    orderBy: updatedAt
    filter: {
      team: { key: { eq: $team } }
      labels: { some: { name: { eq: $label } } }
      state: { type: { in: $states } }
    }
  ) {
    nodes {
      id identifier title url description priority updatedAt
      state { name type }
      project {
        id name url
        documents(first: 20) { nodes { title url updatedAt content } }
      }
      comments(first: 50) { nodes { body createdAt user { name } } }
      inverseRelations { nodes { type issue { identifier title state { name type } } } }
    }
  }
}
"""


def read_context(
    agent: str,
    *,
    team: str = DEFAULT_TEAM,
    max_issues: int = 10,
    comments_per_issue: int = 5,
    char_budget: int = 8000,
) -> dict:
    """Open work for ``agent`` plus the standing project context.

    Returns ``issues`` (structured), ``project_docs`` and ``markdown``: a
    prompt-ready block capped at ``char_budget`` characters.
    """
    try:
        data = _gql(_CONTEXT_QUERY, {
            "team": team,
            "label": label_for(agent),
            "states": OPEN_STATE_TYPES,
            "first": max_issues,
        })
    except Exception as e:
        return {"ok": False, "error": str(e)}

    issues: list[dict] = []
    project_docs: dict[str, dict] = {}
    for node in (data.get("issues") or {}).get("nodes", []):
        comments = sorted(
            (node.get("comments") or {}).get("nodes", []),
            key=lambda c: c.get("createdAt") or "",
        )[-comments_per_issue:] if comments_per_issue > 0 else []
        blocked_by = [
            {
                "identifier": r["issue"]["identifier"],
                "title": r["issue"]["title"],
                "state": (r["issue"].get("state") or {}).get("name"),
            }
            for r in (node.get("inverseRelations") or {}).get("nodes", [])
            if r.get("type") == "blocks" and r.get("issue")
            and (r["issue"].get("state") or {}).get("type") not in ("completed", "canceled")
        ]
        project = node.get("project") or None
        if project:
            for doc in (project.get("documents") or {}).get("nodes", []):
                if (doc.get("title") or "").strip().lower() == CONTEXT_DOC_TITLE:
                    project_docs[project["id"]] = {
                        "project": project["name"],
                        "title": doc["title"],
                        "url": doc.get("url"),
                        "updated_at": doc.get("updatedAt"),
                        "content": doc.get("content") or "",
                    }
        issues.append({
            "id": node["id"],
            "identifier": node["identifier"],
            "title": node["title"],
            "url": node.get("url"),
            "state": (node.get("state") or {}).get("name"),
            "priority": node.get("priority"),
            "updated_at": node.get("updatedAt"),
            "project": project["name"] if project else None,
            "brief": node.get("description") or "",
            "blocked_by": blocked_by,
            "recent_comments": [
                {
                    "author": (c.get("user") or {}).get("name") or "unknown",
                    "created_at": c.get("createdAt"),
                    "body": c.get("body") or "",
                }
                for c in comments
            ],
        })

    docs = list(project_docs.values())
    return {
        "ok": True,
        "agent": agent,
        "team": team,
        "issues": issues,
        "project_docs": docs,
        "markdown": render_context(agent, team, issues, docs, char_budget),
    }


def render_context(agent: str, team: str, issues: list[dict], docs: list[dict], char_budget: int = 8000) -> str:
    lines = [f"## Linear context for {LABEL_PREFIX}{agent} ({team})"]
    if not issues:
        lines.append("No open issues are assigned to this agent.")
    for i in issues:
        lines.append("")
        lines.append(f"### {i['identifier']} {i['title']} [{i['state']}]")
        lines.append(i["url"] or "")
        if i["project"]:
            lines.append(f"Project: {i['project']}")
        if i["blocked_by"]:
            lines.append("Blocked by: " + ", ".join(
                f"{b['identifier']} ({b['state']})" for b in i["blocked_by"]))
        if i["brief"]:
            lines.append("Brief:")
            lines.append(_clip(i["brief"], 1500))
        if i["recent_comments"]:
            lines.append("Recent comments, oldest first:")
            for c in i["recent_comments"]:
                day = (c["created_at"] or "")[:10]
                lines.append(f"- {day} {c['author']}: {_clip(c['body'], 600)}")
    for d in docs:
        lines.append("")
        lines.append(f"### Project state: {d['project']} (updated {(d['updated_at'] or '')[:10]})")
        lines.append(_clip(d["content"], 2500))
    return _clip("\n".join(lines), char_budget)


# --- post_handoff ------------------------------------------------------------

_ISSUE_FOR_HANDOFF = """
query IssueForHandoff($id: String!) {
  issue(id: $id) {
    id identifier url
    team { id states { nodes { id name type } } }
    comments(first: 50) { nodes { body } }
  }
}
"""

_COMMENT_CREATE = """
mutation CommentCreate($input: CommentCreateInput!) {
  commentCreate(input: $input) { success comment { id url } }
}
"""

_ISSUE_UPDATE = """
mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) { success issue { state { name } } }
}
"""


def format_handoff(
    agent: str,
    summary: str,
    findings: Iterable[str] | None = None,
    links: Iterable[str] | None = None,
    next_steps: Iterable[str] | None = None,
    *,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Return ``(body, sha)``. The sha covers content only, not the timestamp,
    so a retried run that would post the same handoff is detectable."""
    label = label_for(agent)
    content = [f"Summary: {summary.strip()}"]
    for title, items in (("Findings", findings), ("Links", links), ("Next", next_steps)):
        b = _bullets(items)
        if b:
            content.append(f"{title}:")
            content.extend(b)
    content_text = "\n".join(content)
    sha = hashlib.sha256(f"{label}\n{content_text}".encode()).hexdigest()[:12]
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    body = f"{HANDOFF_MARKER} agent={agent} sha={sha} -->\nHandoff from {label}, {stamp}\n\n{content_text}"
    return body, sha


def post_handoff(
    issue: str,
    agent: str,
    summary: str,
    *,
    findings: Iterable[str] | None = None,
    links: Iterable[str] | None = None,
    next_steps: Iterable[str] | None = None,
    state: str | None = None,
) -> dict:
    """Post a structured handoff comment on ``issue`` (identifier or id).

    Skips the post when an identical handoff (same agent, same content) is
    already on the issue. ``state`` optionally moves the issue to a workflow
    state by name, e.g. "In Review" or "Done".
    """
    try:
        if not summary or not summary.strip():
            return {"ok": False, "error": "summary is required"}
        body, sha = format_handoff(agent, summary, findings, links, next_steps)
        data = _gql(_ISSUE_FOR_HANDOFF, {"id": issue})
        node = data.get("issue")
        if not node:
            return {"ok": False, "error": f"Issue {issue} not found"}

        existing = {
            m.group(1)
            for c in (node.get("comments") or {}).get("nodes", [])
            if is_agent_generated(c.get("body"))
            for m in [_SHA_RE.search(c.get("body") or "")] if m
        }
        result: dict[str, Any] = {"ok": True, "issue": node["identifier"], "url": node.get("url"), "sha": sha}
        if sha in existing:
            result["skipped"] = "duplicate handoff"
        else:
            created = _gql(_COMMENT_CREATE, {"input": {"issueId": node["id"], "body": body}})
            payload = created.get("commentCreate") or {}
            if not payload.get("success"):
                return {"ok": False, "error": "commentCreate returned success=false"}
            result["comment_url"] = (payload.get("comment") or {}).get("url")

        if state:
            states = (node.get("team") or {}).get("states", {}).get("nodes", [])
            match = next((s for s in states if s["name"].lower() == state.lower()), None)
            if not match:
                result["state_error"] = f"No workflow state named {state!r} on this team"
            else:
                updated = _gql(_ISSUE_UPDATE, {"id": node["id"], "input": {"stateId": match["id"]}})
                result["state"] = ((updated.get("issueUpdate") or {}).get("issue") or {}).get("state", {}).get("name")
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --- open_objective ----------------------------------------------------------

_TEAM_AND_LABEL = """
query TeamAndLabel($team: String!, $label: String!, $title: String!, $states: [String!]) {
  teams(filter: { key: { eq: $team } }) { nodes { id } }
  issueLabels(first: 1, filter: { name: { eq: $label } }) { nodes { id } }
  issues(
    first: 1
    filter: {
      team: { key: { eq: $team } }
      title: { eq: $title }
      labels: { some: { name: { eq: $label } } }
      state: { type: { in: $states } }
    }
  ) { nodes { id identifier url } }
}
"""

_LABEL_CREATE = """
mutation LabelCreate($input: IssueLabelCreateInput!) {
  issueLabelCreate(input: $input) { success issueLabel { id } }
}
"""

_ISSUE_CREATE = """
mutation IssueCreate($input: IssueCreateInput!) {
  issueCreate(input: $input) { success issue { id identifier url } }
}
"""


def open_objective(
    agent: str,
    title: str,
    brief: str,
    *,
    requested_by: str,
    team: str = DEFAULT_TEAM,
    project_id: str | None = None,
    priority: int | None = None,
) -> dict:
    """Open one issue for ``agent`` to own. Idempotent on (agent, title) while
    the issue is open, so a scheduler that fires repeatedly files one issue.

    ``requested_by`` is the slug of the agent (or "dan") handing the work over.
    """
    try:
        label = label_for(agent)
        title = title.strip()
        if not title:
            return {"ok": False, "error": "title is required"}
        data = _gql(_TEAM_AND_LABEL, {
            "team": team, "label": label, "title": title, "states": OPEN_STATE_TYPES,
        })
        teams = (data.get("teams") or {}).get("nodes", [])
        if not teams:
            return {"ok": False, "error": f"Linear team {team!r} not found"}
        team_id = teams[0]["id"]

        open_issues = (data.get("issues") or {}).get("nodes", [])
        if open_issues:
            hit = open_issues[0]
            return {"ok": True, "created": False, "issue": hit["identifier"], "url": hit.get("url")}

        labels = (data.get("issueLabels") or {}).get("nodes", [])
        if labels:
            label_id = labels[0]["id"]
        else:
            made = _gql(_LABEL_CREATE, {"input": {"name": label, "teamId": team_id, "color": "#5E6AD2"}})
            label_id = (((made.get("issueLabelCreate") or {}).get("issueLabel")) or {}).get("id")
            if not label_id:
                return {"ok": False, "error": f"Could not create label {label}"}

        description = f"{OBJECTIVE_MARKER} agent={agent} from={requested_by} -->\n{brief.strip()}"
        issue_input: dict[str, Any] = {
            "teamId": team_id,
            "title": title,
            "description": description,
            "labelIds": [label_id],
        }
        if project_id:
            issue_input["projectId"] = project_id
        if priority is not None:
            issue_input["priority"] = priority
        created = _gql(_ISSUE_CREATE, {"input": issue_input})
        payload = created.get("issueCreate") or {}
        if not payload.get("success"):
            return {"ok": False, "error": "issueCreate returned success=false"}
        issue = payload["issue"]
        return {"ok": True, "created": True, "issue": issue["identifier"], "url": issue.get("url")}
    except Exception as e:
        return {"ok": False, "error": str(e)}
