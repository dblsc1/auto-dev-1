"""tests/conftest.py — playwright browser fixture with system-chromium fallback.

Playwright 1.60+ bundles a headless-shell binary that the installer refuses to
download on Ubuntu 26+.  We fall back to the system snap chromium when the
bundled binary is missing.
"""
import os
import shutil
import pytest
from playwright.sync_api import sync_playwright, BrowserContext


def _chromium_kwargs() -> dict:
    """Return launch kwargs; injects executable_path when bundled binary missing."""
    from playwright._impl._driver import compute_driver_executable
    # playwright stores browsers under ~/.cache/ms-playwright/
    cache = os.path.expanduser("~/.cache/ms-playwright")
    # if any chromium dir exists and has a real binary, trust it
    import glob
    shells = glob.glob(f"{cache}/chromium*/**/chrome-headless-shell", recursive=True)
    bins   = glob.glob(f"{cache}/chromium*/**/chrome",                 recursive=True)
    if shells or bins:
        return {}
    # bundled binary missing — try system chromium (snap or apt)
    for candidate in ("/snap/bin/chromium", "/usr/bin/chromium",
                      "/usr/bin/chromium-browser", shutil.which("chromium") or ""):
        if candidate and os.path.exists(candidate):
            return {"executable_path": candidate}
    raise RuntimeError("No chromium found. Run: playwright install chromium")


@pytest.fixture(scope="session")
def browser_context() -> BrowserContext:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, **_chromium_kwargs())
        ctx = browser.new_context()
        yield ctx
        ctx.close()
        browser.close()


@pytest.fixture()
def page(browser_context):
    pg = browser_context.new_page()
    yield pg
    pg.close()
