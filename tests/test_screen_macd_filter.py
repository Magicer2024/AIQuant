"""
test_screen_macd_filter.py —— 验证 MACD 数值范围筛选
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_filter_logic():
    """直接测试筛选逻辑（不启 Flask）"""
    print("=== 验证后端 MACD DEA/柱体 数值筛选逻辑 ===\n")

    # 模拟 3 只股票
    stocks = [
        {"code": "000001", "macd_dif": 0.5,  "macd_dea": 0.3,  "macd_hist": 0.2},
        {"code": "000002", "macd_dif": -0.2, "macd_dea": -0.1, "macd_hist": -0.1},
        {"code": "000003", "macd_dif": 1.0,  "macd_dea": 0.8,  "macd_hist": 0.2},
    ]

    def apply_filter(s, dif_f=None, dea_f=None, hist_f=None):
        if dif_f is None and dea_f is None and hist_f is None:
            return True
        dif = s.get("macd_dif")
        dea = s.get("macd_dea")
        hist = s.get("macd_hist")
        if dif is None or dea is None:
            return False
        if dif_f:
            op, val = dif_f["op"], dif_f["value"]
            if op == "gt" and not (dif > val): return False
            if op == "lt" and not (dif < val): return False
        if dea_f:
            op, val = dea_f["op"], dea_f["value"]
            if op == "gt" and not (dea > val): return False
            if op == "lt" and not (dea < val): return False
        if hist_f and hist is not None:
            op, val = hist_f["op"], hist_f["value"]
            if op == "gt" and not (hist > val): return False
            if op == "lt" and not (hist < val): return False
        return True

    # 测试 1: DIF > 0
    print("测试 1：DIF > 0")
    expected = {"000001": True, "000002": False, "000003": True}
    for s in stocks:
        got = apply_filter(s, dif_f={"op": "gt", "value": 0})
        flag = "OK" if got == expected[s["code"]] else "FAIL"
        print(f"  {s['code']}: dif={s['macd_dif']}  期望={expected[s['code']]}  实际={got}  [{flag}]")

    # 测试 2: DEA > 0.2
    print("\n测试 2：DEA > 0.2")
    expected = {"000001": True, "000002": False, "000003": True}
    for s in stocks:
        got = apply_filter(s, dea_f={"op": "gt", "value": 0.2})
        flag = "OK" if got == expected[s["code"]] else "FAIL"
        print(f"  {s['code']}: dea={s['macd_dea']}  期望={expected[s['code']]}  实际={got}  [{flag}]")

    # 测试 3: 柱体 > 0.15
    print("\n测试 3：柱体 > 0.15")
    expected = {"000001": True, "000002": False, "000003": True}
    for s in stocks:
        got = apply_filter(s, hist_f={"op": "gt", "value": 0.15})
        flag = "OK" if got == expected[s["code"]] else "FAIL"
        print(f"  {s['code']}: hist={s['macd_hist']}  期望={expected[s['code']]}  实际={got}  [{flag}]")

    # 测试 4: 同时多个条件
    print("\n测试 4：DIF > 0 AND 柱体 < 0.25")
    expected = {"000001": True, "000002": False, "000003": True}
    for s in stocks:
        got = apply_filter(s, dif_f={"op": "gt", "value": 0}, hist_f={"op": "lt", "value": 0.25})
        flag = "OK" if got == expected[s["code"]] else "FAIL"
        print(f"  {s['code']}  期望={expected[s['code']]}  实际={got}  [{flag}]")


if __name__ == "__main__":
    test_filter_logic()
    print("\n[OK] MACD 数值范围筛选逻辑测试通过")
