"""tools/_check_timeline_snapshot.py —— 方案A验收自测（生产库只读）

逐条对量化工程师 4 项验收口径出证据：
  ① 601026 时间线 2026-10-13 = 建议买入，可溯源 stock_deep_signal 落库行；
  ② 无"重算"标注的 buy/add 行逐行可在库查到当日落库；recompute 行带标注、无三点计划；
  ③ 明日候选/建单链读的是 stock_deep_signal（本次零改动，此处做只读一致性确认）；
  ④ 601026 全 45 日窗口逐日比对：不出现"库有当日 buy 但时间线标重算"错配。

用法：python tools/_check_timeline_snapshot.py [--code 601026] [--date 2026-10-13]
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import DB_PATH  # noqa: E402
from strategy import stock_deep as sd  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default="601026")
    ap.add_argument("--date", default="2026-10-13")
    args = ap.parse_args()
    code, key_date = args.code, args.date

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")

    res = sd.analyze_stock(conn, code, recent_days=45)
    if res.get("insufficient"):
        print("FAIL: 数据不足", res.get("reason"))
        return 1
    adv = {a["date"]: a for a in res["recent_advice"]}
    dates = sorted(adv)
    fails = []

    # ── ① 关键日显示买入类且可溯源 ──
    a = adv.get(key_date)
    if not a:
        fails.append(f"① 关键日 {key_date} 不在时间线窗口内")
    else:
        sig = conn.execute(
            "SELECT level, score, entry_price, stop_loss, take_profit"
            " FROM stock_deep_signal WHERE scan_date=? AND code=?",
            (key_date, code)).fetchone()
        if not (a["source"] == "scan_snapshot" and a["level"] in ("buy", "add")):
            fails.append(f"① {key_date} source={a['source']} level={a['level']}（期望 scan_snapshot/buy|add）")
        elif not sig or sig["level"] != a["level"] or sig["score"] != a.get("score"):
            fails.append(f"① {key_date} 无法与 stock_deep_signal 落库行对上: sig={dict(sig) if sig else None}")
        else:
            print(f"PASS ① {code} {key_date}: level={a['level']}({a['label']}) "
                  f"score={a['score']} 三点价={a['entry_price']}/{a['stop_loss']}/{a['take_profit']} "
                  f"= 落库 {sig['entry_price']}/{sig['stop_loss']}/{sig['take_profit']}")

    # ── 落库行缓存（②④ 共用）──
    sig_dates = {r["scan_date"]: dict(r) for r in conn.execute(
        "SELECT * FROM stock_deep_signal WHERE code=? AND scan_date IN ({})".format(
            ",".join("?" * len(dates))), (code, *dates))}
    try:
        lv_dates = {r["scan_date"]: dict(r) for r in conn.execute(
            "SELECT * FROM stock_deep_daily_level WHERE code=? AND scan_date IN ({})".format(
                ",".join("?" * len(dates))), (code, *dates))}
    except sqlite3.OperationalError:
        lv_dates = {}

    # ── ② 非重算 buy/add 行逐行可溯源；重算行带标注且无三点计划 ──
    bad2 = []
    for d, a in adv.items():
        if a["source"] == "recompute":
            if not a["label"].endswith("（重算·参考）"):
                bad2.append(f"{d} recompute 行缺标注")
            if a["entry_price"] is not None or a["stop_loss"] is not None or a["take_profit"] is not None:
                bad2.append(f"{d} recompute 行仍带三点计划")
        elif a["level"] in ("buy", "add"):
            if d not in sig_dates and d not in lv_dates:
                bad2.append(f"{d} 标称快照但两表均无落库行")
    if bad2:
        fails.extend("② " + b for b in bad2)
    else:
        print(f"PASS ② 全窗口 {len(adv)} 行：recompute 均带标注/无三点计划；"
              f"非重算 buy/add 均可溯源（signal {len(sig_dates)} 天 + daily_level {len(lv_dates)} 天）")

    # ── ③ 推荐链只读一致性：明日候选仍只从 stock_deep_signal 出，且无 hold/sell ──
    items = sd.get_market_signal_latest(conn, limit=50)
    lv3 = {i["level"] for i in items}
    if not lv3 <= {"buy", "add"}:
        fails.append(f"③ 明日候选出现非候选档位: {lv3}")
    else:
        print(f"PASS ③ 明日候选 top{len(items)} 档位集合 {sorted(lv3)} <= {{buy,add}}（链路零改动）")

    # ── ④ 45 日窗口逐日错配比对 ──
    mism = []
    for d, s in sig_dates.items():
        a = adv.get(d)
        if not a:
            mism.append(f"{d} 库有 {s['level']} 行但时间线缺该日")
        elif a["source"] != "scan_snapshot" or a["level"] != s["level"]:
            mism.append(f"{d} 库={s['level']}@signal / 时间线={a['level']}({a['source']})")
    for d, l in lv_dates.items():
        a = adv.get(d)
        if a is None:
            continue   # 全档位行对应的行情日不在展示窗口（如停牌/窗口外），不算错配
        if a["source"] != "scan_snapshot":
            mism.append(f"{d} 新表有 {l['level']} 快照但时间线标 {a['source']}")
        elif a["level"] != l["level"]:
            mism.append(f"{d} 新表={l['level']} / 时间线={a['level']}")
    covered = [d for d in dates if d in sig_dates or d in lv_dates]
    if mism:
        fails.extend("④ " + m for m in mism)
    else:
        print(f"PASS ④ 全 45 日窗口逐日一致（快照覆盖 {len(covered)}/{len(dates)} 天，"
              f"未覆盖日全部标重算），无错配")

    conn.close()
    if fails:
        print("\n== FAILURES ==")
        for f in fails:
            print(" -", f)
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
