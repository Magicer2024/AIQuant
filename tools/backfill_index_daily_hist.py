# -*- coding: utf-8 -*-
"""
tools/backfill_index_daily_hist.py —— 任务2-D：指数日线历史补数（2026-10-17）

背景：index_daily 五只指数实测仅从 2024-01-02 起，2015-2026 市场闸门回放
（任务2）缺指数斜率项与弱市闸门数据。本脚本补 000001/000300/000905 三只指数
2014-06-01 ~ 2023-12-31 日线。

安全边界（派单约束）：
  - 只新增：INSERT OR IGNORE + 硬守卫 trade_date < 2024-01-01（既有数据起点
    2024-01-02 之前），绝不触碰既有行；
  - 不修改生产写入逻辑（不走 upsert_index_daily——那会对冲突行 DO UPDATE）；
  - 幂等：重复运行不产生重复/覆盖。

数据源与口径（如实标注）：
  - 首选新浪 ak.stock_zh_index_daily（东财 index_zh_a_hist 实测 RemoteDisconnected）；
  - 新浪列仅有 date/open/high/low/close/volume：
      amount        → 置 NULL（源缺）；
      pct_change    → 由全序列收盘价衍生 round((close/prev-1)*100,2)，
                      定义与东财「涨跌幅」一致（收盘环比）；区间首行的前值取
                      区间外真实前收盘（全序列连续），非修数；
      volume        → 存新浪原值（单位口径与东财手/成交量存在差异，
                      regime/弱市闸门/回放均只用 close，不受影响）；
  - 早年指数基期/新股计入等口径差异按原样入库，不做平滑。

用法：PYTHONIOENCODING=utf-8 python tools/backfill_index_daily_hist.py [--dry-run]
"""
import argparse
import sqlite3
import sys
from datetime import datetime

import akshare as ak
import pandas as pd

DB = r"E:\小项目\Project\AIQuant\core\quant.db"
SINA_SYMBOLS = {"000001": "sh000001", "000300": "sh000300", "000905": "sh000905"}
START, END = "2014-06-01", "2023-12-31"
GUARD_MAX = "2024-01-01"   # 只写 trade_date < GUARD_MAX（既有数据起点之前）


def fetch_window(code: str) -> pd.DataFrame:
    """拉全历史（算 pct 需要区间首行的前值），再切补数区间。"""
    df = ak.stock_zh_index_daily(symbol=SINA_SYMBOLS[code])
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["date"].astype(str)).dt.strftime("%Y-%m-%d")
    df["pct_change"] = (df["close"].astype(float).pct_change() * 100).round(2)
    df["amount"] = None
    df = df[(df["trade_date"] >= START) & (df["trade_date"] <= GUARD_MAX)].copy()
    df = df[["trade_date", "open", "high", "low", "close", "volume", "amount", "pct_change"]]
    return df


def official_calendar() -> set:
    cal = ak.tool_trade_date_hist_sina()
    return {pd.to_datetime(d).strftime("%Y-%m-%d") for d in cal["trade_date"]
            if START <= pd.to_datetime(d).strftime("%Y-%m-%d") <= END}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只取数与校验，不落库")
    args = ap.parse_args()

    cal = official_calendar()
    print(f"官方交易日历 {START}~{END}: {len(cal)} 天")

    conn = sqlite3.connect(DB)
    report = {}
    for code in SINA_SYMBOLS:
        df = fetch_window(code)
        guarded = df[df["trade_date"] < GUARD_MAX]
        assert len(guarded) == len(df), f"{code} 返回了守卫区间外的行！"
        anomalies = []
        if df["close"].isna().any():
            anomalies.append(f"close 缺失 {int(df['close'].isna().sum())} 行")
        dups = df[df["trade_date"].duplicated(keep=False)]
        if len(dups):
            anomalies.append(f"源内重复日 {len(dups)} 行")
        if df["pct_change"].iloc[0] is None or pd.isna(df["pct_change"].iloc[0]):
            anomalies.append("区间首日 pct_change 无前值（全序列起点即区间内）")
        src_dates = set(df["trade_date"])
        missing_days = sorted(cal - src_dates)
        extra_days = sorted(src_dates - cal)
        rng = (df["trade_date"].min(), df["trade_date"].max())
        before = conn.execute("SELECT COUNT(*) FROM index_daily WHERE code=? "
                              "AND trade_date>=? AND trade_date<?",
                              (code, START, GUARD_MAX)).fetchone()[0]
        if not args.dry_run:
            conn.executemany(
                """INSERT OR IGNORE INTO index_daily
                   (code,trade_date,open,high,low,close,volume,amount,pct_change)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                [(code, r.trade_date, r.open, r.high, r.low, r.close,
                  r.volume, None, r.pct_change)
                 for r in df.itertuples(index=False)])
            conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM index_daily WHERE code=? "
                             "AND trade_date>=? AND trade_date<?",
                             (code, START, GUARD_MAX)).fetchone()[0]
        untouched = conn.execute("SELECT COUNT(*) FROM index_daily WHERE code=? "
                                 "AND trade_date>=?", (code, GUARD_MAX)).fetchone()[0]
        report[code] = dict(range=rng, fetched=len(df), inserted=after - before,
                            missing_days=missing_days, extra_days=extra_days,
                            anomalies=anomalies, recent_rows_untouched=untouched)
        print(f"[{code}] {rng[0]}~{rng[1]} 取 {len(df)} 行；新插入 {after - before} 行；"
              f"2024+ 既有 {untouched} 行未动；缺日历日 {len(missing_days)}；"
              f"非日历日行 {len(extra_days)}；异常: {anomalies or '无'}")
    conn.close()
    if args.dry_run:
        print("DRY-RUN，未落库")
        return
    out = f"backfill_report_{datetime.now():%Y%m%d_%H%M%S}.txt"
    with open(out, "w", encoding="utf-8") as f:
        for code, r in report.items():
            f.write(f"== {code} ==\n")
            for k, v in r.items():
                f.write(f"  {k}: {v}\n")
    print("已落库（INSERT OR IGNORE），报告:", out)


if __name__ == "__main__":
    sys.exit(main())
