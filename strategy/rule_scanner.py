"""
strategy/rule_scanner.py —— 每日常驻规则扫描
================================================

消费 strategy_rules 中"启用"的规则，对最新交易日做一次全市场因子评估，
把命中独立落地到 strategy_signals 表（与融合分信号在 stock_signal 中物理隔离）。

复用件：
  - rules_store.get_active_rules      启用规则（已按 fitness DESC 排序）
  - scorer._load_stock_data_streaming 全市场流式加载
  - factor_lib.compute_all_factors    向量化因子
  - rule_engine._iter_condition_items / _evaluate_conditions_vec  条件评估

去重：写入前按 trade_date 清空当日 strategy_signals，再批量插入。
整体 try/except，保证被 sync 调用时永不抛出。

性能优化（全市场扫描曾耗时约 12 分钟）：
  - Top-N 规则：get_active_rules 已按 fitness 降序，仅取前 MAX_RULES 条，
    既压缩规则评估成本，也从源头收敛单日命中量。
  - 末行评估：因子算好后只对最新一行做条件评估（tail(1)），结果与全量一致但更省。
  - 有界窗口：compute_all_factors 前把行情裁到最近 LOOKBACK_ROWS 行，
    足够覆盖最长 rolling 窗口(252)，显著降低长历史个股的因子计算量。
"""
from __future__ import annotations

import json
import time
import traceback
from typing import Any, Dict, List, Optional

from core.db import get_conn

# 默认只扫描 fitness 最高的前 N 条启用规则（None/0 表示不限）
DEFAULT_MAX_RULES = 50
# compute_all_factors 前保留的最大行数，需 > 最长 rolling 窗口(252) 以保证末行因子准确
DEFAULT_LOOKBACK_ROWS = 300


def scan_active_rules(trade_date: Optional[str] = None,
                      max_rules: Optional[int] = DEFAULT_MAX_RULES,
                      lookback_rows: int = DEFAULT_LOOKBACK_ROWS) -> Dict[str, Any]:
    """对启用规则做最新一日全市场扫描，命中写入 strategy_signals。

    :param trade_date: 目标交易日，缺省取 daily_price 的 MAX(trade_date)
    :param max_rules: 仅扫描 fitness 最高的前 N 条启用规则；None/<=0 表示不限
    :param lookback_rows: 因子计算保留的最近行数，需 > 252；<=0 表示不裁剪
    :return: {trade_date, rules, hits, elapsed_s}
    """
    result: Dict[str, Any] = {"trade_date": trade_date, "rules": 0, "hits": 0, "elapsed_s": 0}
    start = time.time()
    try:
        from strategy.rules_store import get_active_rules
        from strategy.scorer import _load_stock_data_streaming
        from strategy.factor_lib import compute_all_factors
        from backtest.rule_engine import _iter_condition_items, _evaluate_conditions_vec

        # 目标交易日
        if not trade_date:
            with get_conn() as conn:
                row = conn.execute("SELECT MAX(trade_date) AS d FROM daily_price").fetchone()
                trade_date = row["d"] if row else None
        if not trade_date:
            result["elapsed_s"] = round(time.time() - start, 1)
            return result
        result["trade_date"] = trade_date

        # 解析启用规则（get_active_rules 已按 fitness DESC 排序）
        rules: List[Dict[str, Any]] = []
        for r in get_active_rules():
            try:
                conds = json.loads(r.get("conditions") or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if not _iter_condition_items(conds):
                continue
            rules.append({"id": r["id"], "rule_name": r["rule_name"], "conditions": conds})
            if max_rules and max_rules > 0 and len(rules) >= max_rules:
                break  # Top-N：只保留 fitness 最高的前 N 条
        result["rules"] = len(rules)

        # 无启用规则：清空当日命中后返回（保持推荐区一致）
        if not rules:
            with get_conn() as conn:
                conn.execute("DELETE FROM strategy_signals WHERE trade_date = ?", (trade_date,))
            result["elapsed_s"] = round(time.time() - start, 1)
            return result

        # 逐股计算因子（每只一次），对每条规则评估最新一日
        hits: List[tuple] = []  # (trade_date, code, rule_id, rule_name, confidence)
        for code, df in _load_stock_data_streaming(trade_date):
            if df is None or len(df) < 30:
                continue
            # 有界窗口：只保留最近 lookback_rows 行，覆盖最长 rolling 窗口即可
            if lookback_rows and lookback_rows > 0 and len(df) > lookback_rows:
                df = df.tail(lookback_rows)
            try:
                factor_df = compute_all_factors(df)
            except Exception:
                continue
            if factor_df is None or factor_df.empty:
                continue
            last_idx = factor_df.index[-1]
            last_date = str(last_idx.date()) if hasattr(last_idx, "date") else str(last_idx)[:10]
            if last_date != trade_date:
                continue
            # 末行评估：只对最新一行做条件判断，结果与全量 iloc[-1] 一致
            last_row_df = factor_df.tail(1)
            for rule in rules:
                ev = _evaluate_conditions_vec(last_row_df, rule["conditions"])
                last = ev.iloc[-1]
                if bool(last["passed"]):
                    hits.append((
                        trade_date, code, rule["id"], rule["rule_name"],
                        round(float(last["strength"]), 4),
                    ))

        # 去重写库：先删当日再批量插入
        with get_conn() as conn:
            conn.execute("DELETE FROM strategy_signals WHERE trade_date = ?", (trade_date,))
            if hits:
                conn.executemany(
                    "INSERT INTO strategy_signals "
                    "(trade_date, code, rule_id, rule_name, confidence, raw_score, features_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    [(h[0], h[1], h[2], h[3], h[4], h[4]) for h in hits],
                )
        result["hits"] = len(hits)
    except Exception:
        traceback.print_exc()
    result["elapsed_s"] = round(time.time() - start, 1)
    return result
