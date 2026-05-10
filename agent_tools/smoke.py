"""Pre-deploy smoke test harness.

Each agent declares a small list of `SmokeCase`s in its repo (typically
`scripts/smoke.py`) and calls `run_smoke()`. The harness runs each case
through the same `run_ask_async` runtime and reports pass/fail. Wire this
into the Railway deploy command so a failing smoke blocks the rollout.

Two layers of testing:

  1. probe_only=True  : just dry-checks env vars + reachability of each MCP
     server (one cheap call per server). Fast; runs before agent loop is
     exercised. Catches token expiry, network, schema drift.

  2. probe_only=False : also runs the full agent loop with each prompt and
     verifies expected_tool was actually called. Slower but catches
     prompt regressions.

Usage from an agent's scripts/smoke.py:

    import asyncio
    from agent_tools.smoke import SmokeCase, run_smoke
    from agent_tools.snowflake.mcp import snowflake_server
    from agent_tools.notion.mcp import notion_server

    SYSTEM_PROMPT = (Path(__file__).parent.parent / "agent" / "system_prompt.md").read_text()

    CASES = [
        SmokeCase(
            name="snowflake_basic",
            prompt="What's our QTD revenue?",
            expected_tool_substring="snowflake_query",
        ),
        SmokeCase(
            name="notion_search",
            prompt="Search Notion for the latest monthly close.",
            expected_tool_substring="notion_search",
        ),
    ]

    if __name__ == "__main__":
        ok = asyncio.run(run_smoke(
            cases=CASES,
            system_prompt_append=SYSTEM_PROMPT,
            mcp_servers={
                "snowflake": snowflake_server,
                "notion": notion_server,
            },
        ))
        sys.exit(0 if ok else 1)
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import traceback
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .runtime import run_ask_async


@dataclass
class SmokeCase:
    name: str
    prompt: str
    expected_tool_substring: str | None = None


SMOKE_CHANNEL = os.environ.get("SMOKE_CHANNEL", "C_SMOKE_DUMMY")
SMOKE_THREAD_TS = os.environ.get("SMOKE_THREAD_TS", "0.0")


async def run_smoke(
    *,
    cases: list[SmokeCase],
    system_prompt_append: str,
    mcp_servers: dict[str, Any],
    probe_only: bool = False,
    probe_fns: dict[str, Callable[[], Awaitable[bool]]] | None = None,
) -> bool:
    """Run smoke cases. Returns True if all passed.

    probe_fns: optional dict of {server_name: async cheap-call} for quick
    auth/reachability checks. If provided, runs them first and short-circuits
    if any fail (no point running prompts when Snowflake auth is broken).
    """
    failures: list[tuple[str, str]] = []
    t0 = time.time()

    if probe_fns:
        print("[smoke] probing connectivity...")
        for server_name, fn in probe_fns.items():
            try:
                ok = await fn()
                if not ok:
                    failures.append((f"probe:{server_name}", "probe returned False"))
                    print(f"[smoke] PROBE FAIL  {server_name}")
                else:
                    print(f"[smoke] probe ok    {server_name}")
            except Exception as exc:
                failures.append((f"probe:{server_name}", f"{type(exc).__name__}: {exc}"))
                print(f"[smoke] PROBE FAIL  {server_name}  {exc}")
        if failures:
            print(f"[smoke] {len(failures)} probe(s) failed; aborting before agent loop")
            _summarize(failures, time.time() - t0)
            return False

    if probe_only:
        _summarize(failures, time.time() - t0)
        return not failures

    print("[smoke] running prompt cases...")
    for case in cases:
        case_t0 = time.time()
        try:
            captured: dict = {}

            def _on_complete(payload: dict, _captured=captured):
                _captured.update(payload)

            await run_ask_async(
                text=case.prompt,
                channel_id=SMOKE_CHANNEL,
                thread_ts=SMOKE_THREAD_TS,
                system_prompt_append=system_prompt_append,
                mcp_servers=mcp_servers,
                on_complete=_on_complete,
            )
            tool_calls = captured.get("tool_calls") or []
            error = captured.get("error")
            if error:
                failures.append((case.name, f"agent error: {error}"))
                print(f"[smoke] FAIL  {case.name}  agent error: {error}")
                continue
            if case.expected_tool_substring and not any(
                case.expected_tool_substring in (t or "") for t in tool_calls
            ):
                failures.append((case.name, f"expected tool '{case.expected_tool_substring}' not invoked; got {tool_calls}"))
                print(f"[smoke] FAIL  {case.name}  no '{case.expected_tool_substring}' in {tool_calls}")
                continue
            duration = time.time() - case_t0
            print(f"[smoke] OK    {case.name}  ({duration:.1f}s, tools={tool_calls})")
        except Exception as exc:
            traceback.print_exc()
            failures.append((case.name, f"{type(exc).__name__}: {exc}"))
            print(f"[smoke] FAIL  {case.name}  {exc}")

    _summarize(failures, time.time() - t0)
    return not failures


def _summarize(failures: list[tuple[str, str]], duration: float) -> None:
    print(f"\n[smoke] done in {duration:.1f}s")
    if failures:
        print(f"[smoke] {len(failures)} failure(s):")
        for name, msg in failures:
            print(f"  - {name}: {msg}")
    else:
        print("[smoke] all green")


# ---------------- Stock probes for the four shared servers ------------------

async def probe_snowflake() -> bool:
    from .snowflake import client as sf
    result = sf.query("SELECT 1 AS ok", max_rows=1)
    return bool(result.get("ok"))


async def probe_slack() -> bool:
    from .slack import client as sl
    return bool(sl.my_user_id())


async def probe_notion() -> bool:
    from .notion import client as nt
    result = nt.search("a", page_size=1)
    return bool(result.get("ok"))


async def probe_drive() -> bool:
    from .drive import client as dr
    result = dr.list_recent_files(limit=1)
    return bool(result.get("ok"))


STANDARD_PROBES = {
    "slack": probe_slack,
    "snowflake": probe_snowflake,
    "notion": probe_notion,
    "drive": probe_drive,
}
