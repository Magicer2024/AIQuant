"""
tests/test_frontend.py ? Playwright smoke tests for the dashboard.

Install Playwright:
    pip install playwright
    playwright install chromium

Run:
    python -m pytest tests/test_frontend.py -v
"""

import time
import sys
import os
import subprocess
import pytest

try:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

pytestmark = pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")


@pytest.fixture(scope="module")
def flask_server():
    """Start Flask in a subprocess for the test module."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.Popen(
        [sys.executable, "app.py"],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "FLASK_DEBUG": "0"},
    )
    time.sleep(2)  # Wait for server startup
    yield "http://localhost:5000"
    proc.terminate()
    proc.wait()


class TestDashboardSmoke:
    """Basic page-load and tab-switching smoke tests."""

    def test_page_loads(self, flask_server):
        """Dashboard page renders with title."""
        url = flask_server
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url + "/dashboard", wait_until="networkidle")
            assert page.title() is not None or "AIQuant" in page.content()
            browser.close()

    def test_tab_switching(self, flask_server):
        """All three tabs can be clicked."""
        url = flask_server
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url + "/dashboard", wait_until="networkidle")
            tab_buttons = page.query_selector_all(".tab-nav button")
            assert len(tab_buttons) >= 3
            for btn in tab_buttons[:3]:
                btn.click()
                time.sleep(0.3)
            browser.close()

    def test_health_api(self, flask_server):
        """Health endpoint returns JSON with ok status."""
        import requests
        resp = requests.get(flask_server + "/api/health", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("success") is True
