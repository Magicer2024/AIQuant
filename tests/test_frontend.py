"""Playwright 冒烟测试：独立端口、临时数据库、禁用调度及外网。"""
import collections
import os
import threading
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
    # stderr 合并到 stdout，后台线程持续排空 PIPE（防缓冲区写满卡死服务），
    # 只留最后 4KB：启动失败时把尾部日志带进 pytest 失败信息
    proc = subprocess.Popen(
        [sys.executable, str(root / "app.py")], cwd=root,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "AIQUANT_DB_PATH": isolated_database,
             "FLASK_HOST": "127.0.0.1", "FLASK_PORT": str(port), "FLASK_DEBUG": "0"})
    log_tail = collections.deque(maxlen=40)

    def _drain():
        for line in proc.stdout:
            log_tail.append(line.rstrip())

    threading.Thread(target=_drain, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            if proc.poll() is not None:
                pytest.fail("测试服务启动失败：%s\n%s" % (proc.returncode, "\n".join(log_tail)))
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
            assert buttons.count() == 4
            for i in range(buttons.count()):
                buttons.nth(i).click()
                assert "active" in (buttons.nth(i).get_attribute("class") or "")
        finally:
            browser.close()


def test_health_api(flask_server):
    response = requests.get(flask_server + "/api/health", timeout=5)
    assert response.status_code == 200
    assert response.json()["success"] is True


# ── T9（dashboard.html 请求收敛 A3）行为用例 ─────────────────────

def test_request_cache_dedup(flask_server):
    """同一只读 URL 带 cacheTtl 连发两次只打一次网络；不带缓存则两次都打。"""
    from playwright.sync_api import sync_playwright
    probe_url = "/api/investor/stocks/search?q=ZZUNIQUE_T9"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.continue_()
                       if route.request.url.startswith(flask_server) else route.abort())
            page.goto(flask_server + "/dashboard", wait_until="domcontentloaded")
            page.wait_for_function("typeof RequestManager !== 'undefined'")

            counted = []

            def _count(req):
                if probe_url in req.url:
                    counted.append(req.url)

            page.on("request", _count)

            # ① 同一 URL + cacheTtl：两次调用只允许一个网络请求
            page.evaluate(
                "async (u) => {"
                "for (let i = 0; i < 2; i++) {"
                "  try { await RequestManager.fetch('t9-probe', u, {cacheTtl: 5000}); }"
                "  catch (e) {}"
                "}}",
                flask_server + probe_url,
            )
            page.wait_for_timeout(200)
            assert len(counted) == 1, f"缓存去重失败，实际请求 {len(counted)} 次"

            # ② 不带 cacheTtl：每次都必须真正发请求
            page.evaluate(
                "async (u) => {"
                "for (let i = 0; i < 2; i++) {"
                "  try { await RequestManager.fetch('t9-direct-' + i, u); }"
                "  catch (e) {}"
                "}}",
                flask_server + probe_url + "&NOCACHE=1",
            )
            page.wait_for_timeout(300)
            direct = [u for u in counted if "NOCACHE=1" in u]
            assert len(direct) == 2, f"直连路径请求数应为 2，实际 {len(direct)}"
        finally:
            browser.close()


def test_poll_helper_cleared_on_tab_switch(flask_server):
    """timerManager.poll 注册的轮询在切页（clearAll）后必须真正停止。

    T9 前 optimizer/fixHistory 等裸 setInterval 不登记 timerManager，切页后泄漏空转。
    """
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.continue_()
                       if route.request.url.startswith(flask_server) else route.abort())
            page.goto(flask_server + "/dashboard", wait_until="domcontentloaded")
            page.wait_for_function("typeof timerManager !== 'undefined'")

            page.evaluate(
                "() => {"
                "window.__t9poll = 0;"
                "timerManager.poll('t9probe', () => { window.__t9poll++; }, 100);"
                "}")
            page.wait_for_function("window.__t9poll >= 2", timeout=3000)

            # 切到设置页（防抖 120ms 后 _doSwitchTab 执行 clearAll）
            page.locator(".nav-item", has_text="设置").click()
            # 基线必须在「timer 被清掉的那一刻」读取：clearAll 之前合法触发的
            # tick（点击延迟+120ms 防抖窗）不算泄漏，否则用例会时序抖动
            page.wait_for_function(
                "!Object.keys(timerManager.timers).includes('t9probe')", timeout=3000)
            n_base = page.evaluate("window.__t9poll")
            # 泄漏的 100ms interval 在 600ms 内会再触发约 6 次，冻结即证明已停
            page.wait_for_timeout(600)
            n_after = page.evaluate("window.__t9poll")
            assert n_after == n_base, f"切页后轮询未停止：{n_base} -> {n_after}"
        finally:
            browser.close()


def test_switch_tab_debounce_keeps_last(flask_server):
    """120ms 内连续切两页，最终停在最后点击的页（防抖只执行末次）。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.continue_()
                       if route.request.url.startswith(flask_server) else route.abort())
            page.goto(flask_server + "/dashboard", wait_until="domcontentloaded")
            page.wait_for_function("typeof switchTab !== 'undefined'")

            page.evaluate(
                "() => {"
                "switchTab('settings'); switchTab('home');"
                "}")
            page.wait_for_timeout(350)  # 超过 120ms 防抖窗
            assert page.evaluate("currentTab") == "home"
            # 导航高亮也应落在首页
            cls = page.locator(".nav-item", has_text="今日").get_attribute("class")
            assert "active" in (cls or "")
        finally:
            browser.close()
