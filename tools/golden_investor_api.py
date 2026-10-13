"""tools/golden_investor_api.py —— T7 护栏：investor 蓝图 GET 接口响应快照冻结/比对

用途：把 routes/investor.py 业务逻辑下沉到 service 模块前后，对同一份库快照
抓取所有 GET 接口的完整 JSON 响应，逐字节级 deep-diff，确保 API 契约零变化。

用法（独立进程，库路径经 AIQUANT_DB_PATH 注入，必须在 import 项目前设置）：
    python tools/golden_investor_api.py dump --db data_cache/t7_golden.db --out reports/t7_golden_before.json
    python tools/golden_investor_api.py diff reports/t7_golden_before.json reports/t7_golden_after.json

注意：
  - 只打 GET（只读语义），POST 变更类接口由 pytest 套件守护；
  - 归一化只剔除「跨进程必变且与业务无关」的键（如响应头 request_id 不收，
    body 内出现 generated_at 类键时加入 VOLATILE_KEYS），业务字段一律不遮。
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time

# （GET, path, 备注）—— path 含动态段的用快照库中确定存在的代码
ENDPOINTS: list[tuple[str, str, str]] = [
    ("GET", "/api/investor/account_config", "账户配置"),
    ("GET", "/api/investor/today", "今日推荐-全部"),
    ("GET", "/api/investor/today?horizon=short", "今日推荐-short"),
    ("GET", "/api/investor/today?horizon=mid", "今日推荐-mid"),
    ("GET", "/api/investor/today?horizon=long", "今日推荐-long"),
    ("GET", "/api/investor/today/dates", "可选日期"),
    ("GET", "/api/investor/surge_picks", "强势突破"),
    ("GET", "/api/investor/reversal_picks", "反转首日"),
    ("GET", "/api/investor/reversal_history", "反转历史"),
    ("GET", "/api/investor/lhb_jg_track", "龙虎榜机构跟踪"),
    ("GET", "/api/investor/exit_advice", "出场跟踪"),
    ("GET", "/api/investor/market-overview", "市场速览"),
    ("GET", "/api/investor/positions", "持仓列表"),
    ("GET", "/api/investor/positions/history", "历史持仓"),
    ("GET", "/api/investor/watchlist", "自选股"),
    ("GET", "/api/investor/alerts", "风险告警"),
    ("GET", "/api/investor/outcome_summary", "复盘汇总"),
    ("GET", "/api/investor/outcome_list", "复盘明细"),
    ("GET", "/api/investor/recommendations/history", "推荐历史"),
    ("GET", "/api/investor/explain", "指标白话-全部"),
    ("GET", "/api/investor/factor_trend?codes=000001,600519&days=30", "因子趋势"),
    ("GET", "/api/investor/rule_signals", "规则信号"),
    ("GET", "/api/investor/stock_deep/market", "全市场深析"),
    ("GET", "/api/investor/stock_deep/recent", "深析最近信号"),
    ("GET", "/api/investor/stock_deep/000001", "个股深析-平安银行"),
    ("GET", "/api/investor/stocks/search?q=000001", "股票搜索"),
    ("GET", "/api/investor/deep_track", "深度跟踪列表"),
]

# body 内与业务无关、跨进程必变的键（噪声地板实测确认，仅基础设施类，不遮业务字段）：
#   request_id —— before_request 每请求生成 uuid；
#   updated_at —— market-overview 内嵌的服务器时钟（行情/业务日期走各自字段，不受影响）。
VOLATILE_KEYS: set[str] = {"request_id", "updated_at"}


def _strip_volatile(node):
    """递归剔除易变键，返回新对象（不改动原响应）"""
    if isinstance(node, dict):
        return {k: _strip_volatile(v) for k, v in node.items() if k not in VOLATILE_KEYS}
    if isinstance(node, list):
        return [_strip_volatile(v) for v in node]
    return node


def _resolve_dynamic_paths() -> list[tuple[str, str, str]]:
    """快照库中取一个个股深析代码之外的动态路径占位（目前无，预留扩展）"""
    return []


def dump(db_path: str, out_path: str) -> None:
    # 库路径必须在项目配置导入前注入（settings 在 import 时读 env）。
    # 不用 AIQUANT_TESTING=1：它会装审计钩子，只允许连 AIQUANT_TEST_ROOT 内的库；
    # 这里是「生产代码 + 快照库」验证，用非测试模式 + 关闭调度/同步/Qlib 开关，
    # 且不调用 initialize_runtime（不起调度器、不做网络/初始化副作用）。
    os.environ["AIQUANT_DB_PATH"] = os.path.abspath(db_path)
    os.environ.pop("AIQUANT_TESTING", None)
    os.environ["AIQUANT_SCHEDULER_ENABLED"] = "0"
    os.environ["AIQUANT_AUTO_SYNC_ENABLED"] = "0"
    os.environ["AIQUANT_QLIB_ENABLED"] = "0"

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app import create_app

    app = create_app()  # 非测试模式：异常走正常 500 响应，与生产一致
    client = app.test_client()

    snapshot = {"_meta": {"db": os.path.abspath(db_path),
                          "captured_at_struct": "omitted",
                          "endpoint_count": len(ENDPOINTS)}}
    for method, path, note in ENDPOINTS:
        t0 = time.time()
        try:
            resp = client.open(path, method=method)
            try:
                body = resp.get_json()
            except Exception:
                body = {"_non_json": resp.get_data(as_text=True)[:500]}
            entry = {"status": resp.status_code, "json": _strip_volatile(body)}
        except Exception as exc:  # 测试客户端抛异常时也要固化，便于前后比对
            entry = {"status": "EXC", "error": f"{type(exc).__name__}: {exc}"}
        elapsed_ms = round((time.time() - t0) * 1000)
        snapshot[f"{method} {path}"] = entry  # 耗时不入库：跨运行不可比，只打印
        print(f"[{entry['status']}] {method} {path} ({note}) {elapsed_ms}ms")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"\n快照已写入 {out_path}（{len(ENDPOINTS)} 个端点）")


def _diff_values(path: str, a, b, diffs: list[str]) -> None:
    """递归 deep-diff，叶子差异记录完整 JSON 路径"""
    if type(a) != type(b):
        diffs.append(f"{path}: 类型不同 {type(a).__name__} vs {type(b).__name__}")
        return
    if isinstance(a, dict):
        ka, kb = set(a), set(b)
        for k in sorted(ka - kb):
            diffs.append(f"{path}.{k}: 仅 before 存在")
        for k in sorted(kb - ka):
            diffs.append(f"{path}.{k}: 仅 after 存在")
        for k in sorted(ka & kb):
            _diff_values(f"{path}.{k}", a[k], b[k], diffs)
    elif isinstance(a, list):
        if len(a) != len(b):
            diffs.append(f"{path}: 数组长度 {len(a)} vs {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            _diff_values(f"{path}[{i}]", x, y, diffs)
    elif a != b:
        diffs.append(f"{path}: {a!r} vs {b!r}")


def diff(before_path: str, after_path: str) -> int:
    with open(before_path, encoding="utf-8") as f:
        before = json.load(f)
    with open(after_path, encoding="utf-8") as f:
        after = json.load(f)

    diffs: list[str] = []
    keys_b = set(before) - {"_meta"}
    keys_a = set(after) - {"_meta"}
    for k in sorted(keys_b - keys_a):
        diffs.append(f"{k}: 仅 before 存在")
    for k in sorted(keys_a - keys_b):
        diffs.append(f"{k}: 仅 after 存在")
    for k in sorted(keys_b & keys_a):
        _diff_values(k, before[k], after[k], diffs)

    if not diffs:
        print(f"✅ GOLDEN DIFF = 0：{len(keys_b)} 个端点响应完全一致")
        return 0
    print(f"❌ 发现 {len(diffs)} 处差异（前 50）：")
    for d in diffs[:50]:
        print("  " + d)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_dump = sub.add_parser("dump")
    p_dump.add_argument("--db", required=True)
    p_dump.add_argument("--out", required=True)
    p_diff = sub.add_parser("diff")
    p_diff.add_argument("before")
    p_diff.add_argument("after")
    args = parser.parse_args()

    if args.cmd == "dump":
        dump(args.db, args.out)
        return 0
    return diff(args.before, args.after)


if __name__ == "__main__":
    raise SystemExit(main())
