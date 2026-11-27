# Browser capture audit (2026-11-26)

Read-only inventory of every place the agent fleet renders or drives a web page. It feeds the decision on adding a shared `agent_tools.browser` helper. Nothing was run against a browser.

## Real implementations

| # | Where | Stack | Purpose | Runs |
|---|---|---|---|---|
| 1 | website-hygiene-agent `hygiene_agent/site_inspector.py:989-1135` | Playwright Python sync, pinned 1.55.0, `playwright install --with-deps chromium` in Dockerfile | Mobile console, pageerror, and 5xx check on key paths. 390x844 DPR 2, real-Chrome UA, networkidle with a load+2s fallback | Railway cron |
| 2 | web-agent `scripts/verify_consent.py:234-312` | Raw CDP over `websockets` (comes in through `uvicorn[standard]`), apt `chromium` via `railpack.json`, unpinned | Consent and tracking probe. 1440x1000, fixed sleep | Railway cron 12:00 UTC |
| 3 | web-agent `scripts/*.mjs` plus 12 `ai-edits/analysis/*/scripts/*.mjs` | Playwright Node, `require()` from a hardcoded Codex runtime path | LCP, CLS, and INP traces, rendered pricing, screenshots | Interactive only, can't run on Railway |
| 4 | video-agent `playwright/src/capture.mjs`, `agent/playwright_tools.py`, `agent/storyboard_tools.py` | Playwright Node CLI from Python subprocess, `^1.50.0` (lock 1.59.1), install errors swallowed (`Dockerfile:77`) | Scripted UI recordings and storyboards, 1920x1080 DPR 2 | Local launchd |
| 5 | video-agent `remotion/render.mjs` | Remotion chrome-headless-shell | Video render | Local and Docker. Out of scope. |
| 6 | campaigns-agent `artifacts/kimi-k3.1-pro-howto-2026-11-04/src/capture.js` | Playwright Node, system Chrome | One-off console screenshots | One-off |

These are HTTP stand-ins, not renders: `web-agent/psi_tools.py` and a second PSI client at `site_inspector.py:1140`, `web-agent/perf_tools.py`, `brand-design-agent/brand_design_agent/capture.py` (requests only).

## Duplication

- The fleet launches Chrome in three ways: Playwright's bundled Chromium (#1 and #4), apt Chromium plus a hand-rolled path search (#2), and the Codex runtime path or system Chrome (#3 and #6).
- The mobile profile is declared four times and doesn't agree: DPR 2 in #1, DPR 3 in the web-agent traces, and 375x812 in the `/webflow-mobile-qa` skill.
- Wait strategies differ everywhere. Only #1 handles networkidle flapping on beacon-heavy pages.
- Only #1 has the real-Chrome UA workaround (HeadlessChrome gets 403s). Only #1 and #2 collect console and network errors.
- The PSI client exists twice.

## Gaps: the charter claims captures, but no code produces them

| Agent | Claim | Reality |
|---|---|---|
| brand-design | "no capture, no critique" | `capture.py:1-14` declares screenshots out of scope. Logo, imagery, motion, and responsive dimensions score UNKNOWN, and contrast is computed from stylesheet values rather than rendered pixels. |
| plg-program | Friction must be walked and timed | `tools/walk.py` is a ledger only. The Chrome profile is signed out of the console. |
| devrel-program | First-run test, 40-minute target | Covers runpodctl and the MCP only. No docs or quickstart render. |
| web-agent | Mobile QA per Webflow change | `--visual-qa` is an attestation flag, not a capture. |
| content-review, social | Screenshot-dependent brand checks | Excluded, or need a screenshot a human supplies. |

## Recommendation

Build a thin, Python-only `agent_tools.browser` behind an optional `agent-tools[browser]` extra, pinned to one Playwright version:

- `open_page(url, profile="mobile"|"desktop", wait=..., timeout_ms=...)`: a context manager that yields `(page, collector)`. The collector records console errors, pageerrors, failed requests, and statuses. Real-Chrome UA by default.
- `screenshot(page, path, full_page=False)` and `computed_styles(page, selectors, props)`. Brand-design is missing the second one.
- Two profiles: mobile is 390x844, DPR 3, touch. Desktop is 1440x900.
- Tests run against a local `http.server` fixture. Add CI, since this repo has none.

Migration order:

1. website-hygiene (#1), as the proof.
2. brand-design rendered captures, which closes the largest gap.
3. web-agent consent (#2), moving from raw CDP to Playwright contexts.

The web-agent Node trace scripts, video-agent, and Remotion stay as they are until the helper has proven itself.

## Side findings

- agent-tools pins are spread across four SHAs: `75098b2`, `8bdf957`, `a27fe22`, `499f0c2`. `strategy-agent-wt-ttv45/requirements.txt:21` is a worktree and still on `@main`.
- `web-agent-model-library` is a byte-identical copy of web-agent's `psi_tools.py` and `perf_tools.py`.
