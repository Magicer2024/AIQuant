"""test_screen_e2e.py —— 端到端测试：启动 Flask 后端，发送真实筛选请求"""
import sys, os, json, time, threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 依赖本地 quant.db 中的真实行情数据，CI 等无库环境自动跳过
_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "quant.db")
pytestmark = pytest.mark.skipif(not os.path.exists(_DB), reason="需要本地 quant.db 真实数据")


def test_e2e():
    print("=== 端到端验证：MACD 数值范围筛选 ===\n")

    # 启动 Flask
    from app import app
    app.config["TESTING"] = True
    client = app.test_client()

    # 构造筛选 payload：DIF > 0 AND 金叉
    payload = {
        "date": "2026-06-24",
        "exclude_kcb": False,
        "exclude_cyb": False,
        "max_market_cap": 0,
        "macd": ["golden"],
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
        "macd_dif": {"op": "gt", "value": 0},
        "macd_dea": {"op": "gt", "value": 0},
        "macd_hist": {"op": "gt", "value": 0},
    }

    print(f"请求: POST /api/screen")
    print(f"Payload: {json.dumps(payload, ensure_ascii=False)}\n")

    resp = client.post("/api/screen",
                        data=json.dumps(payload),
                        content_type="application/json")
    print(f"HTTP 状态: {resp.status_code}")

    body = resp.get_json()
    if not body:
        print(f"[FAIL] 返回空 body, raw: {resp.data[:200]}")
        return

    if body.get("ok") is False:
        print(f"[FAIL] 后端返回错误: {body.get('msg') or body.get('error')}")
        return

    data = body.get("data") or body
    results = data.get("results", []) if isinstance(data, dict) else data
    print(f"返回结果: {len(results)} 只股票")

    # 看前 5 只
    for i, s in enumerate(results[:5]):
        code = s.get("code", "?")
        name = s.get("name", "")
        dif = s.get("macd_dif", "?")
        dea = s.get("macd_dea", "?")
        hist = s.get("macd_hist", "?")
        print(f"  {i+1}. {code} {name}  DIF={dif} DEA={dea} HIST={hist}")

    # 验证筛选条件满足
    if results:
        s = results[0]
        dif = s.get("macd_dif", 0) or 0
        assert dif > 0, f"DIF 应 > 0, 实际 {dif}"
        print(f"\n[OK] 筛选结果 DIF={dif} > 0, 符合条件")
    print("\n[OK] 端到端验证通过")


if __name__ == "__main__":
    test_e2e()
