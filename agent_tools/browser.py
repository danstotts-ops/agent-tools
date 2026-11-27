"""Headless page capture for agents (Playwright, sync API).

One place for how the fleet opens a rendered page: device profiles, a user
agent beacons accept, a wait strategy that survives analytics traffic, and a
collector for console errors, page errors, and failed requests.

Standalone on purpose: this module imports only the stdlib and Playwright, so
an agent that needs nothing else from agent-tools can install the package with
``pip install --no-deps`` and pin ``playwright`` itself (see PLAYWRIGHT_VERSION).
Chromium must be installed separately: ``python -m playwright install
--with-deps chromium`` in the Dockerfile.

Usage::

    from agent_tools.browser import Browser, PageCheckError

    with Browser() as browser:
        for url in urls:
            try:
                with browser.page(url, profile="mobile") as (page, capture):
                    overflow = page.evaluate("() => document.documentElement.scrollWidth")
                    errors = capture.errors
            except PageCheckError as exc:
                ...  # exc.kind is the original Playwright error class name
"""

from __future__ import annotations

import contextlib
import ipaddress
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Literal, Sequence
from urllib.parse import urlparse

# The Playwright release this module is tested against. Consumers pin the same
# version so the Chromium build matches across the fleet.
PLAYWRIGHT_VERSION = "1.55.0"

# Chromium's default HeadlessChrome user agent is rejected by third-party
# beacons (Common Room's Signals SDK answers it with 403), which shows up as
# console errors real visitors never see. Browse as a real Chrome instead.
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36"
)
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

PROFILES: dict[str, dict[str, Any]] = {
    "mobile": {
        "viewport": {"width": 390, "height": 844},
        "device_scale_factor": 3,
        "is_mobile": True,
        "has_touch": True,
        "user_agent": MOBILE_USER_AGENT,
    },
    "desktop": {
        "viewport": {"width": 1440, "height": 900},
        "device_scale_factor": 1,
        "is_mobile": False,
        "has_touch": False,
        "user_agent": DESKTOP_USER_AGENT,
    },
}

Profile = Literal["mobile", "desktop"]
Wait = Literal["networkidle_fallback", "networkidle", "load", "load_settle", "domcontentloaded"]

DEFAULT_TIMEOUT_MS = 30_000
DEFAULT_SETTLE_MS = 2_000


class BrowserUnavailable(RuntimeError):
    """Playwright is not installed. Callers that treat the browser as optional
    can catch this and skip; a Chromium launch failure is raised as-is."""


class PageCheckError(RuntimeError):
    """A Playwright error while loading or inspecting one page.

    ``kind`` keeps the original error class name (``TimeoutError``, ``Error``)
    so callers that key findings on it stay stable.
    """

    def __init__(self, message: str, *, url: str, kind: str) -> None:
        super().__init__(message)
        self.url = url
        self.kind = kind


class UnsafeURLError(PageCheckError):
    """The URL is not plain http(s), or it points at a loopback, private,
    link-local, or otherwise non-public address (cloud metadata, the Railway
    private network, the host's own services)."""


ALLOWED_SCHEMES = frozenset({"http", "https"})
# Characters Python's urlparse and Chromium's URL parser disagree on (Chromium
# treats a backslash as a path separator), so "http://127.0.0.1\\@evil.test"
# would read as public here and private in the browser. Refuse them outright.
_AMBIGUOUS_URL_CHARS = re.compile(r"[\x00-\x20\x7f\\]")
# Host names must already be in the one form both parsers agree on: ASCII
# letters, digits, dots, hyphens (punycode xn-- labels for IDNs). Chromium
# applies UTS#46 mapping (fullwidth digits, lookalikes) that Python does not.
_PLAIN_HOSTNAME = re.compile(r"^[A-Za-z0-9.-]+$")
# Labels a browser reads as IPv4 parts (decimal, octal, 0x hex). A host made
# only of these must be a canonical dotted quad; shorthand such as 127.1,
# 0x7f.1, or 2130706433 is refused rather than trusted to the local resolver.
_NUMERIC_LABEL = re.compile(r"^(0x[0-9a-f]*|[0-9]+)$", re.I)


def _is_public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def validate_url(url: str, *, allow_private_hosts: bool = False) -> None:
    """Refuse URLs a capture should never load.

    Always: anything but http/https (file:, data:, chrome:, view-source:,
    about:), user:password@ credentials, backslashes, whitespace, or control
    characters, non-ASCII or percent-encoded host names, and numeric hosts
    that are not a canonical IP (127.1, 0x7f.1, 2130706433): all forms that
    browsers and Python parse differently. Unless
    ``allow_private_hosts``: hosts that are, or resolve to, non-public
    addresses, and hosts that do not resolve (fail closed).

    ``Browser.page`` applies this to the requested URL and the post-redirect
    URL. The browser still issues the redirected request, subresource
    requests are not filtered, and DNS rebinding is not defeated. Intercepting
    every request would disable Playwright's HTTP cache and still miss
    redirect hops, so it is not done here: for untrusted URLs, the control is
    network-level egress policy on the container. This is a guard rail.
    """
    if _AMBIGUOUS_URL_CHARS.search(url):
        raise UnsafeURLError(f"refusing URL with backslash, whitespace, or control characters: {url!r}", url=url, kind="UnsafeURLError")
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f"refusing {scheme or 'scheme-less'} URL: {url}", url=url, kind="UnsafeURLError")
    if "@" in parsed.netloc:
        raise UnsafeURLError(f"refusing URL with embedded credentials: {url}", url=url, kind="UnsafeURLError")
    host = parsed.hostname
    if not host:
        raise UnsafeURLError(f"URL has no host: {url}", url=url, kind="UnsafeURLError")
    if not _looks_like_ip(host):
        if not _PLAIN_HOSTNAME.match(host):
            raise UnsafeURLError(
                f"refusing host {host!r}: use plain ASCII (punycode xn-- for IDNs)",
                url=url,
                kind="UnsafeURLError",
            )
        labels = [label for label in host.rstrip(".").split(".")]
        if labels and all(_NUMERIC_LABEL.match(label) for label in labels):
            raise UnsafeURLError(
                f"refusing non-canonical numeric host {host!r}; use a dotted-quad IPv4",
                url=url,
                kind="UnsafeURLError",
            )
    if allow_private_hosts:
        return
    try:
        addresses = [host] if _looks_like_ip(host) else [
            info[4][0] for info in socket.getaddrinfo(host, parsed.port or None, proto=socket.IPPROTO_TCP)
        ]
    except socket.gaierror as exc:
        raise UnsafeURLError(
            f"refusing {host}: it did not resolve, so it cannot be checked ({exc})",
            url=url,
            kind="UnsafeURLError",
        ) from exc
    for address in addresses:
        if not _is_public_ip(address):
            raise UnsafeURLError(
                f"refusing non-public host {host} ({address}); pass allow_private_hosts=True for local targets",
                url=url,
                kind="UnsafeURLError",
            )


def _looks_like_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    return True


@dataclass
class PageCapture:
    """What the page reported while it loaded and while the caller inspected it."""

    url: str
    status: int | None = None
    final_url: str | None = None
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[str]:
        """Console and uncaught page errors, deduplicated and sorted."""
        return sorted(set(self.console_errors + self.page_errors))


def context_options(profile: Profile = "mobile", **overrides: Any) -> dict[str, Any]:
    """Playwright ``new_page``/``new_context`` options for a device profile."""
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; expected one of {sorted(PROFILES)}")
    options = {key: (dict(value) if isinstance(value, dict) else value) for key, value in PROFILES[profile].items()}
    options.update(overrides)
    return options


def console_error_text(message: Any) -> str:
    """Console message text plus the failing resource URL when Chromium has one."""
    text = str(getattr(message, "text", "") or "")
    location = getattr(message, "location", None)
    url = str(location.get("url") or "") if isinstance(location, dict) else ""
    return f"{text} [{url}]" if url else text


def goto(
    page: Any,
    url: str,
    *,
    wait: Wait = "networkidle_fallback",
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    settle_ms: int = DEFAULT_SETTLE_MS,
    timeout_error: type[BaseException] = TimeoutError,
) -> Any:
    """Navigate and return the main response.

    ``networkidle_fallback``: analytics and chat beacons keep the network busy
    often enough that a networkidle wait intermittently times out on healthy
    pages. On that timeout only, retry once waiting for the load event plus a
    short settle, so a page that is actually down still fails the retry.
    ``load_settle``: load event, then ``settle_ms``.
    """
    if wait == "networkidle_fallback":
        try:
            return page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        except timeout_error:
            response = page.goto(url, wait_until="load", timeout=timeout_ms)
            page.wait_for_timeout(settle_ms)
            return response
    if wait == "load_settle":
        response = page.goto(url, wait_until="load", timeout=timeout_ms)
        page.wait_for_timeout(settle_ms)
        return response
    return page.goto(url, wait_until=wait, timeout=timeout_ms)


def _import_sync_api() -> Any:
    try:
        from playwright import sync_api
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise BrowserUnavailable(
            "playwright is not installed; pip install 'agent-tools[browser]' "
            f"(playwright=={PLAYWRIGHT_VERSION}) and run "
            "'python -m playwright install --with-deps chromium'"
        ) from exc
    return sync_api


class Browser:
    """One headless Chromium shared across pages. Use as a context manager."""

    def __init__(self, *, headless: bool = True, args: Sequence[str] | None = None) -> None:
        self.headless = headless
        self.args = list(args or [])
        self._playwright: Any = None
        self._browser: Any = None
        self._error: type[BaseException] = Exception
        self._timeout_error: type[BaseException] = TimeoutError

    def __enter__(self) -> "Browser":
        sync_api = _import_sync_api()
        self._error = sync_api.Error
        self._timeout_error = sync_api.TimeoutError
        self._playwright = sync_api.sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch(headless=self.headless, args=self.args)
        except BaseException:
            self._playwright.stop()
            self._playwright = None
            raise
        return self

    def __exit__(self, *exc_info: Any) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            self._browser = None
            if self._playwright is not None:
                self._playwright.stop()
                self._playwright = None

    @contextlib.contextmanager
    def page(
        self,
        url: str,
        *,
        profile: Profile = "mobile",
        wait: Wait = "networkidle_fallback",
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        settle_ms: int = DEFAULT_SETTLE_MS,
        allow_private_hosts: bool = False,
        **overrides: Any,
    ) -> Iterator[tuple[Any, PageCapture]]:
        """Open ``url`` and yield ``(page, capture)``.

        Playwright errors from navigation or from the caller's inspection
        inside the block are raised as ``PageCheckError``. ``url`` and the
        post-redirect URL go through ``validate_url`` (``UnsafeURLError``, a
        ``PageCheckError``). The page is always closed on exit.
        """
        if self._browser is None:
            raise RuntimeError("Browser.page() needs an entered Browser: use 'with Browser() as browser'")
        validate_url(url, allow_private_hosts=allow_private_hosts)
        page = self._browser.new_page(**context_options(profile, **overrides))
        capture = PageCapture(url=url)
        page.on(
            "console",
            lambda message: capture.console_errors.append(console_error_text(message))
            if message.type == "error"
            else None,
        )
        page.on("pageerror", lambda error: capture.page_errors.append(str(error)))
        page.on(
            "requestfailed",
            lambda request: capture.failed_requests.append(f"{request.url} {request.failure or ''}".strip()),
        )
        try:
            try:
                response = goto(
                    page,
                    url,
                    wait=wait,
                    timeout_ms=timeout_ms,
                    settle_ms=settle_ms,
                    timeout_error=self._timeout_error,
                )
            except self._error as exc:
                raise PageCheckError(str(exc), url=url, kind=type(exc).__name__) from exc
            capture.status = response.status if response is not None else None
            capture.final_url = page.url
            if capture.final_url != url:
                validate_url(capture.final_url, allow_private_hosts=allow_private_hosts)
            try:
                yield page, capture
            except self._error as exc:
                raise PageCheckError(str(exc), url=url, kind=type(exc).__name__) from exc
        finally:
            page.close()


@contextlib.contextmanager
def open_page(url: str, *, headless: bool = True, **page_kwargs: Any) -> Iterator[tuple[Any, PageCapture]]:
    """One-shot convenience: launch Chromium, open one page, close everything.

    Playwright's sync API allows one running instance per thread, so do not
    call this while a ``Browser`` is open; reuse that browser's ``page()``.
    """
    with Browser(headless=headless) as browser, browser.page(url, **page_kwargs) as pair:
        yield pair


def screenshot(page: Any, path: str | Path, *, full_page: bool = False) -> Path:
    """Write a PNG of the current viewport (or the full page) and return its path."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(target), full_page=full_page)
    return target


_COMPUTED_STYLES_JS = """([selectors, props, limit]) => Object.fromEntries(
  selectors.map(sel => [sel, [...document.querySelectorAll(sel)].slice(0, limit).map(el => {
    const s = getComputedStyle(el);
    return Object.fromEntries(props.map(p => [p, s.getPropertyValue(p)]));
  })])
)"""


def computed_styles(
    page: Any, selectors: Sequence[str], props: Sequence[str], *, limit: int = 20
) -> dict[str, list[dict[str, str]]]:
    """Rendered CSS values, keyed by selector, one dict per matched element.

    ``props`` are CSS property names (``color``, ``font-family``). Values come
    from ``getComputedStyle``, so they reflect what the browser actually
    applied, not what a stylesheet declares.
    """
    return page.evaluate(_COMPUTED_STYLES_JS, [list(selectors), list(props), limit])
