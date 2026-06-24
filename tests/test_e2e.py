"""tests/test_e2e.py — E2E smoke tests for the sample multiplication calculator.

Services expected (started by CI or run_tests.sh):
  Backend  http://localhost:8000   (uvicorn main:app)
  Frontend http://localhost:3000   (python -m http.server 3000)
"""
import pytest

FRONTEND = "http://localhost:3000"


def test_page_loads(page):
    """Elements are present on initial load."""
    page.goto(FRONTEND)
    assert page.locator("#a").is_visible()
    assert page.locator("#b").is_visible()
    assert page.locator("button").is_visible()


def test_multiply_3_by_7(page):
    """3 × 7 = 21."""
    page.goto(FRONTEND)
    page.fill("#a", "3")
    page.fill("#b", "7")
    page.click("button")
    page.wait_for_selector("#result:not(:empty)", timeout=5000)
    assert "21" in page.locator("#result").text_content()


def test_multiply_negative(page):
    """-4 × 5 = -20."""
    page.goto(FRONTEND)
    page.fill("#a", "-4")
    page.fill("#b", "5")
    page.click("button")
    page.wait_for_selector("#result:not(:empty)", timeout=5000)
    assert "-20" in page.locator("#result").text_content()


def test_empty_input_shows_error(page):
    """Clicking calculate with empty inputs shows validation error."""
    page.goto(FRONTEND)
    page.fill("#a", "")
    page.fill("#b", "")
    page.click("button")
    error_el = page.locator("#error")
    page.wait_for_selector("#error:not(:empty)", timeout=3000)
    assert error_el.text_content().strip() != ""
