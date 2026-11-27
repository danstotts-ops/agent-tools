"""open_page() starts its own Playwright instance, and the sync API allows one
per thread, so it lives in a module without the shared Browser fixture."""

from __future__ import annotations

import pytest

from agent_tools.browser import BrowserUnavailable, open_page
from tests.test_browser import site  # noqa: F401  (fixture)


def test_open_page_one_shot(site):  # noqa: F811
    try:
        with open_page(f"{site}/ok", wait="load") as (page, capture):
            assert page.viewport_size == {"width": 390, "height": 844}
    except BrowserUnavailable:
        pytest.skip("playwright not installed")
    except Exception as exc:
        if "Executable doesn't exist" in str(exc):
            pytest.skip("chromium not installed")
        raise
    assert capture.status == 200
