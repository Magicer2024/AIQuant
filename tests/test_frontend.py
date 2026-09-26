"""Playwright 冒烟测试：独立端口、临时数据库、禁用调度及外网。"""
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest
import requests

pytestmark = [pytest.mark.browser, pytest.mark.skipif(
    os.getenv("AIQUANT_BROWSER_TESTS") != "1", reason="独立浏览器作业启用")]


@pytest.fixture()
def flask_server(isolated_database):
    from playwright.sync_api import sync_playwright  # 缺失依赖时独立作业必须失败
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.Popen(
        [sys.executable, str(root / "app.py")], cwd=root,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "AIQUANT_DB_PATH": isolated_database,
             "FLASK_PORT": str(port), "FLASK_DEBUG": "0"})
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            if proc.poll() is not None:
                pytest.fail(f"测试服务启动失败：{proc.returncode}")
            try:
                if requests.get(url + "/api/health", timeout=0.5).status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("测试服务启动超时")
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=15)


def test_dashboard_navigation(flask_server):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(flask_server) else route.abort())
            page.goto(flask_server + "/dashboard", wait_until="domcontentloaded")
            assert "AIQuant" in page.title()
            buttons = page.locator(".nav-item")
            assert buttons.count() == 2
            for i in range(buttons.count()):
                buttons.nth(i).click()
                assert "active" in (buttons.nth(i).get_attribute("class") or "")
        finally:
            browser.close()


def test_health_api(flask_server):
    response = requests.get(flask_server + "/api/health", timeout=5)
    assert response.status_code == 200
    assert response.json()["success"] is True
