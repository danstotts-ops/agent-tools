"""Tests for agent_tools.browser.

Unit tests use fakes. The integration tests drive real Chromium against a
local http.server fixture and skip when Playwright or its Chromium build is
not installed (CI installs both).
"""

from __future__ import annotations

import ast
import contextlib
import http.server
import socket
import sys
import threading
from pathlib import Path

import pytest

from agent_tools import browser
from agent_tools.browser import (
    PLAYWRIGHT_VERSION,
    Browser,
    BrowserUnavailable,
    PageCapture,
    PageCheckError,
    UnsafeURLError,
    computed_styles,
    console_error_text,
    context_options,
    goto,
    screenshot,
    validate_url,
)

# --- unit -------------------------------------------------------------------


def test_module_is_standalone():
    """browser.py must import only the stdlib and playwright, so consumers can
    install agent-tools with --no-deps."""
    tree = ast.parse(Path(browser.__file__).read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    third_party = roots - set(sys.stdlib_module_names) - {"__future__"}
    assert third_party == {"playwright"}


def test_profiles_do_not_announce_headless():
    for profile in ("mobile", "desktop"):
        ua = context_options(profile)["user_agent"]
        assert "HeadlessChrome" not in ua
        assert "Chrome/" in ua
    assert "Mobile" in context_options("mobile")["user_agent"]


def test_mobile_profile_shape():
    options = context_options("mobile")
    assert options["viewport"] == {"width": 390, "height": 844}
    assert options["device_scale_factor"] == 3
    assert options["is_mobile"] is True
    assert options["has_touch"] is True


def test_context_options_overrides_do_not_mutate_profiles():
    options = context_options("mobile", device_scale_factor=2)
    options["viewport"]["width"] = 1
    assert options["device_scale_factor"] == 2
    assert browser.PROFILES["mobile"]["viewport"]["width"] == 390
    assert browser.PROFILES["mobile"]["device_scale_factor"] == 3


def test_context_options_rejects_unknown_profile():
    with pytest.raises(ValueError):
        context_options("tablet")  # type: ignore[arg-type]


class _Message:
    def __init__(self, text: str, url: str | None) -> None:
        self.text = text
        self.location = {"url": url, "lineNumber": 0} if url else {}


def test_console_error_text_appends_resource_url():
    assert console_error_text(_Message("403", "https://x.test/a.js")) == "403 [https://x.test/a.js]"
    assert console_error_text(_Message("boom", None)) == "boom"


def test_capture_errors_dedupes_and_sorts():
    capture = PageCapture(url="u", console_errors=["b", "a"], page_errors=["a", "c"])
    assert capture.errors == ["a", "b", "c"]


class _FakeTimeout(Exception):
    pass


class _FakePage:
    def __init__(self, fail_on: set[str]) -> None:
        self.fail_on = fail_on
        self.calls: list[str] = []
        self.waited: list[int] = []

    def goto(self, url, wait_until, timeout):
        self.calls.append(wait_until)
        if wait_until in self.fail_on:
            raise _FakeTimeout(wait_until)
        return {"wait_until": wait_until}

    def wait_for_timeout(self, ms):
        self.waited.append(ms)


def test_goto_uses_networkidle_when_it_settles():
    page = _FakePage(fail_on=set())
    assert goto(page, "u", timeout_error=_FakeTimeout)["wait_until"] == "networkidle"
    assert page.calls == ["networkidle"]
    assert page.waited == []


def test_goto_falls_back_to_load_on_networkidle_timeout():
    page = _FakePage(fail_on={"networkidle"})
    assert goto(page, "u", timeout_error=_FakeTimeout)["wait_until"] == "load"
    assert page.calls == ["networkidle", "load"]
    assert page.waited == [2000]


def test_goto_still_fails_when_page_never_loads():
    page = _FakePage(fail_on={"networkidle", "load"})
    with pytest.raises(_FakeTimeout):
        goto(page, "u", timeout_error=_FakeTimeout)
    assert page.calls == ["networkidle", "load"]


def test_goto_load_settle_and_passthrough():
    page = _FakePage(fail_on=set())
    goto(page, "u", wait="load_settle", settle_ms=500)
    goto(page, "u", wait="domcontentloaded")
    assert page.calls == ["load", "domcontentloaded"]
    assert page.waited == [500]


def test_missing_playwright_raises_browser_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright", None)
    with pytest.raises(BrowserUnavailable):
        with Browser():
            pass


def test_page_requires_entered_browser():
    with pytest.raises(RuntimeError):
        with Browser().page("http://127.0.0.1/"):
            pass


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "data:text/html,<h1>x</h1>",
        "chrome://settings",
        "view-source:https://www.runpod.io/",
        "about:blank",
        "javascript:alert(1)",
        "https:///nohost",
    ],
)
def test_validate_url_rejects_non_http_schemes(url):
    with pytest.raises(UnsafeURLError):
        validate_url(url, allow_private_hosts=True)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/",
        "http://localhost/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
    ],
)
def test_validate_url_rejects_private_hosts_by_default(url):
    with pytest.raises(UnsafeURLError) as info:
        validate_url(url)
    assert isinstance(info.value, PageCheckError)
    assert info.value.kind == "UnsafeURLError"
    validate_url(url, allow_private_hosts=True)  # explicit opt-in for local targets


def test_validate_url_checks_what_a_name_resolves_to(monkeypatch):
    monkeypatch.setattr(
        browser.socket, "getaddrinfo", lambda *a, **k: [(None, None, None, "", ("10.1.2.3", 0))]
    )
    with pytest.raises(UnsafeURLError):
        validate_url("https://internal.example.test/")


def test_validate_url_allows_public_hosts(monkeypatch):
    validate_url("https://8.8.8.8/")
    monkeypatch.setattr(
        browser.socket, "getaddrinfo", lambda *a, **k: [(None, None, None, "", ("104.18.0.1", 0))]
    )
    validate_url("https://www.runpod.io/pricing")


def test_unresolvable_host_is_left_to_navigation(monkeypatch):
    def fail(*a, **k):
        raise browser.socket.gaierror("nope")

    monkeypatch.setattr(browser.socket, "getaddrinfo", fail)
    validate_url("https://does-not-resolve.example/")


class _RedirectingPage:
    """Fake page whose navigation lands on a metadata IP."""

    def __init__(self) -> None:
        self.url = ""
        self.closed = False

    def on(self, *_):
        pass

    def goto(self, url, wait_until, timeout):
        self.url = "http://169.254.169.254/latest/meta-data/"
        return type("R", (), {"status": 200})()

    def close(self):
        self.closed = True


def test_redirect_to_private_host_is_refused(monkeypatch):
    monkeypatch.setattr(
        browser.socket, "getaddrinfo", lambda *a, **k: [(None, None, None, "", ("104.18.0.1", 0))]
    )
    fake_page = _RedirectingPage()
    b = Browser()
    b._browser = type("B", (), {"new_page": lambda self, **k: fake_page})()
    with pytest.raises(UnsafeURLError) as info:
        with b.page("https://www.runpod.io/go", wait="load"):
            raise AssertionError("caller must not see the redirected page")
    assert "169.254.169.254" in str(info.value)
    assert fake_page.closed


def test_pinned_version_matches_extra():
    pyproject = (Path(browser.__file__).parents[1] / "pyproject.toml").read_text()
    assert f'"playwright=={PLAYWRIGHT_VERSION}"' in pyproject


# --- integration (real Chromium) ---------------------------------------------

_PAGES = {
    "/ok": b"""<!doctype html><html><head><style>
        h1 { color: rgb(103, 61, 230); font-size: 32px; }
      </style></head><body>
      <h1>Hello</h1><h1>Again</h1>
      <img src="http://127.0.0.1:9/missing.png">
      <script>console.error('fixture console error'); setTimeout(() => { throw new Error('fixture page error'); }, 0);</script>
      </body></html>""",
}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/boom":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"down")
            return
        body = _PAGES.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def site():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="module")
def chromium():
    try:
        with Browser() as b:
            yield b
    except BrowserUnavailable:
        pytest.skip("playwright not installed")
    except Exception as exc:  # Chromium build missing
        if "Executable doesn't exist" in str(exc):
            pytest.skip("chromium not installed; run 'python -m playwright install chromium'")
        raise


def _closed_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_page_collects_status_and_errors(chromium, site):
    with chromium.page(f"{site}/ok", wait="load_settle", settle_ms=300, allow_private_hosts=True) as (page, capture):
        assert page.viewport_size == {"width": 390, "height": 844}
    assert capture.status == 200
    assert capture.final_url == f"{site}/ok"
    assert any("fixture console error" in e for e in capture.console_errors)
    assert any("fixture page error" in e for e in capture.page_errors)
    assert any("missing.png" in r for r in capture.failed_requests)


def test_server_error_is_a_status_not_an_exception(chromium, site):
    with chromium.page(f"{site}/boom", wait="load", allow_private_hosts=True) as (_, capture):
        pass
    assert capture.status == 500


def test_unreachable_url_raises_page_check_error(chromium):
    url = f"http://127.0.0.1:{_closed_port()}/"
    with pytest.raises(PageCheckError) as info:
        with chromium.page(url, wait="load", timeout_ms=5000, allow_private_hosts=True):
            pass
    assert info.value.url == url
    assert info.value.kind == "Error"


def test_errors_inside_the_block_become_page_check_error(chromium, site):
    with pytest.raises(PageCheckError):
        with chromium.page(f"{site}/ok", wait="load", allow_private_hosts=True) as (page, _):
            page.evaluate("() => { throw new Error('inspection failed') }")


def test_computed_styles_and_screenshot(chromium, site, tmp_path):
    with chromium.page(f"{site}/ok", profile="desktop", wait="load", allow_private_hosts=True) as (page, _):
        assert page.viewport_size == {"width": 1440, "height": 900}
        styles = computed_styles(page, ["h1", "nav"], ["color", "font-size"])
        shot = screenshot(page, tmp_path / "nested" / "ok.png")
    assert styles["h1"] == [{"color": "rgb(103, 61, 230)", "font-size": "32px"}] * 2
    assert styles["nav"] == []
    assert shot.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_private_target_is_refused_before_navigation(chromium, site):
    with pytest.raises(UnsafeURLError):
        with chromium.page(f"{site}/ok"):
            pass
