"""
tools/backfill_pct_turnover.py —— 用 baostock 回补历史行缺失的涨跌幅/换手率
==========================================================================
背景（2026-10-22 排查）：
  2025-01～2026-03 约 73% 的行情行 pct_change=0 且 turnover=0（2026-04 起
  逐步降到个位数），涉及 3,218 只股票——某次批量历史导入没带这两列，与腾讯
  兜底无关。pct_change=0 会误导旧日期展示；turnover=0 会让 strategy/chip.py
  的筹码衰减只能靠 volume/circ_shares 回退（有回退，但 TURNOVER 类因子拿不到真值）。

修法：
  逐只股票用 baostock adjustflag="3"（不复权）重取 turn / pctChg，
  只覆盖有问题的单元格，其余一律不动：
    - turnover == 0 且 baostock 有值 → 取 baostock；
    - 0 < turnover < 0.02 且 turnover×100 与 baostock 相对误差 ≤2%
      → 判为「百分数被存成小数」（如 0.3521% → 0.003521），还原为 baostock 值；
    - pct_change == 0 且收盘相对前收确有变化 → 取 baostock；横盘日保持不变。
  OHLC/volume/amount 不动。
  逐行校验「库内 OHLC 与不复权 OHLC 只差一个复权系数 q」，q 折算后偏差 >1%
  即跳过该行（旧数据口径差异，不冒险写）。
  首轮全量后有 189 行卡在 1%~2.1% 的边界（收盘完全吻合、仅开高低精度漂移），
  需要时可加 --relax-ohlc 用「三价 ≤3%」的收盘锚定校验放行。

稳定性：
  baostock 底层裸 socket 在服务端断连时会永久阻塞（2026-10-22 实测挂死 9 小时）；
  这里设 30s socket 超时，查询失败自动重登重试一次，且每 500 只主动换一次连接。

用法：
  python tools/backfill_pct_turnover.py --limit 50            # dry-run 抽样
  python tools/backfill_pct_turnover.py                       # dry-run 全量统计
  python tools/backfill_pct_turnover.py --apply               # 实际回补
  python tools/backfill_pct_turnover.py --since 2026-01-01 --apply
  python tools/backfill_pct_turnover.py --apply --limit 300   # 分批跑
  python tools/backfill_pct_turnover.py --apply --relax-ohlc --codes <代码列表>  # 收尾边界行

安全：
  - 默认 dry-run；--apply 前会把每行旧值写入库内备份表
    _bak_pct_turn_<YYYYMMDD_HHMMSS>(code, trade_date, pct_change, turnover)，
    回滚按该表逐行还原即可（旧值本来就是 0，回滚也可直接置 0）。
  - 只改 0 值单元格，可重复执行（幂等）。
  - 失败/跳过明细写到 reports/backfill_pct_turnover_<ts>.log。
"""
import argparse
import os
import socket
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import baostock as bs
from core.db import get_conn, connect_db
from core.sync import _format_code_for_baostock
from tools.fix_tencent_amount_bug import _scaled_match, TOL

# baostock 底层是裸 socket；服务端断连时 recv 会永久阻塞（2026-10-22 实测挂死 9 小时）。
# 建连前设默认超时，让卡住的请求抛错，由 _query_bs 重登后续跑。
SOCKET_TIMEOUT = 30

FRACTION_MAX = 0.02  # turnover 落在 (0, 0.02) 视为「百分数被存成了小数」的嫌疑区

# OHLC 严格校验（TOL=1%）失败时的兜底门槛，只在 --relax-ohlc 下启用。
# 2026-10-23 首轮全市场回补遗留 189 行：逐行复核后收盘按 q 折算偏差全为 0，
# 开高低最大偏差 2.07%，属历史写入的开高低价精度漂移，不是错股/错复权。
RELAX_OHLC_MAX = 0.03        # 三价里最差的一个不得超过 3%


def _close_anchored_match(stored, o, h, l, c):
    """收盘锚定的放宽校验：q 由收盘唯一确定，开高低折算后偏差 ≤3%。

    与 _scaled_match 同样把 q 折回库内基准后比较，只把「四价全≤1%」
    放宽到「三价 ≤3%」。仅用于回补 pct/turnover 两个单元格。
    """
    sc = stored.get("close")
    if not sc or sc <= 0 or c <= 0:
        return False
    q = c / sc
    for sv, ev in ((stored.get("open"), o), (stored.get("high"), h), (stored.get("low"), l)):
        if not sv or ev <= 0:
            return False
        if abs((ev / q) / sv - 1) > RELAX_OHLC_MAX:
            return False
    return True


def _candidate_codes(conn, since: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT code FROM daily_price "
        "WHERE trade_date>=? AND volume>0 AND close>0 "
        "  AND (pct_change=0 OR turnover=0 OR (turnover>0 AND turnover<?)) ORDER BY code",
        (since, FRACTION_MAX)).fetchall()
    return [r["code"] for r in rows]


def _query_bs(code: str, start: str, end: str, log):
    """查 baostock，失败/超时重登一次；返回 {date: row}（无数据返回 {}）。"""
    for attempt in (1, 2):
        try:
            rs = bs.query_history_k_data_plus(
                _format_code_for_baostock(code), "date,open,high,low,close,turn,pctChg",
                start_date=start, end_date=end, frequency="d", adjustflag="3")
            got = {}
            while rs.error_code == '0' and rs.next():
                r = rs.get_row_data()
                got[r[0]] = r
            if rs.error_code not in ('0', ''):
                raise RuntimeError(f"baostock error_code={rs.error_code} {rs.error_msg}")
            return got
        except Exception as e:
            log.write(f"{code}\tQUERY_ERR_TRY{attempt}\t{type(e).__name__}: {e}\n")
            log.flush()
            print(f"  [WARN] {code} 查询失败({type(e).__name__})，重登后重试", flush=True)
            try:
                bs.logout()
            except Exception:
                pass
            try:
                bs.login()
            except Exception as e2:
                log.write(f"{code}\tRELOGIN_ERR\t{type(e2).__name__}: {e2}\n")
    return {}


def main():
    ap = argparse.ArgumentParser(description="用 baostock 回补历史行缺失的涨跌幅/换手率")
    ap.add_argument("--since", default="2025-01-01", help="只处理该日期之后（默认 2025-01-01）")
    ap.add_argument("--apply", action="store_true", help="实际写库（默认 dry-run）")
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 只股票（分批用）")
    ap.add_argument("--codes", default=None, help="逗号分隔的股票代码，只处理这些")
    ap.add_argument("--relax-ohlc", action="store_true",
                    help="OHLC 严格校验失败时，改用收盘锚定的放宽校验（三价 ≤3%%）")
    args = ap.parse_args()

    with get_conn() as conn:
        if args.codes:
            codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        else:
            codes = _candidate_codes(conn, args.since)
    if args.limit:
        codes = codes[:args.limit]
    print(f"候选股票 {len(codes)} 只（since={args.since}, apply={args.apply}）")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    reports_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
    os.makedirs(reports_dir, exist_ok=True)
    log_path = os.path.join(reports_dir, f"backfill_pct_turnover_{ts}.log")
    log = open(log_path, "w", encoding="utf-8")

    bak_table = f"_bak_pct_turn_{ts}"
    if args.apply:
        with get_conn() as conn:
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {bak_table} "
                f"(code TEXT, trade_date TEXT, pct_change REAL, turnover REAL, "
                f" PRIMARY KEY(code, trade_date))")
        print(f"旧值备份表: {bak_table}")

    socket.setdefaulttimeout(SOCKET_TIMEOUT)  # 必须在 login 建 socket 之前生效
    lg = bs.login()
    if lg.error_code != '0':
        print(f"[ERROR] baostock 登录失败: {lg.error_msg}")
        sys.exit(1)

    t0 = time.time()
    total_rows = upd_pct = upd_turn = mismatched = missing = no_bs = relaxed = 0
    done = 0
    # 复用连接：get_conn 每次 close 在 WAL 下约 0.7s，3000+ 只会白白多花半小时
    rconn = connect_db(readonly=True)
    wconn = connect_db() if args.apply else None
    try:
        for code in codes:
            done += 1
            if done > 1 and done % 500 == 0:
                # 定期换连接，避免长会话被服务端静默断开
                try:
                    bs.logout()
                except Exception:
                    pass
                try:
                    bs.login()
                except Exception as e:
                    log.write(f"{code}\tRELOGIN_ERR_PERIODIC\t{type(e).__name__}: {e}\n")
            rows = [dict(r) for r in rconn.execute(
                "SELECT trade_date, open, high, low, close, pct_change, turnover FROM ("
                "  SELECT trade_date, open, high, low, close, volume, pct_change, turnover,"
                "         LAG(close) OVER (ORDER BY trade_date) AS pc"
                "  FROM daily_price WHERE code=? AND trade_date>=date(?, '-15 day')"
                ") WHERE trade_date>=? AND volume>0 AND close>0 AND ("
                "  turnover=0 OR (turnover>0 AND turnover<?)"
                # pct=0 且收盘相对前收确有变化，才是缺失；横盘日 pct=0 本就正确
                "  OR (pct_change=0 AND pc IS NOT NULL AND ABS(close-pc)>1e-6)"
                ") ORDER BY trade_date",
                (code, args.since, args.since, FRACTION_MAX)).fetchall()]
            if not rows:
                continue
            total_rows += len(rows)
            got = _query_bs(code, rows[0]["trade_date"], rows[-1]["trade_date"], log)
            if not got:
                no_bs += 1
                log.write(f"{code}\tNO_BAOSTOCK\trows={len(rows)}\n")
                continue

            updates = []
            for r in rows:
                b = got.get(r["trade_date"])
                if not b:
                    missing += 1
                    continue
                try:
                    b_o, b_h, b_l, b_c = (float(b[1]), float(b[2]), float(b[3]), float(b[4]))
                    b_t, b_p = float(b[5]), float(b[6])
                except (ValueError, TypeError):
                    missing += 1
                    continue
                if not _scaled_match(r, b_o, b_h, b_l, b_c, TOL):
                    if not (args.relax_ohlc and _close_anchored_match(r, b_o, b_h, b_l, b_c)):
                        mismatched += 1
                        log.write(f"{code}\tOHLC_MISMATCH\t{r['trade_date']}\n")
                        continue
                    relaxed += 1
                    log.write(f"{code}\tOHLC_RELAXED\t{r['trade_date']}\n")
                new_pct = b_p if (r["pct_change"] == 0 and b_p != 0) else r["pct_change"]
                cur_t = r["turnover"]
                if cur_t == 0 and b_t > 0:
                    new_turn = b_t
                elif (0 < cur_t < FRACTION_MAX and b_t > 0
                      and abs(cur_t * 100 / b_t - 1) <= 0.02):
                    # 百分数被存成了小数（如 0.3521% → 0.003521），×100 还原；用 baostock 校验防误伤
                    new_turn = b_t
                else:
                    new_turn = cur_t
                if new_pct == r["pct_change"] and new_turn == r["turnover"]:
                    continue
                updates.append((new_pct, new_turn, code, r["trade_date"],
                                r["pct_change"], r["turnover"]))
                if new_pct != r["pct_change"]:
                    upd_pct += 1
                if new_turn != r["turnover"]:
                    upd_turn += 1

            if args.apply and updates:
                wconn.executemany(
                    f"INSERT OR REPLACE INTO {bak_table}(code, trade_date, pct_change, turnover) "
                    f"VALUES (?,?,?,?)",
                    [(u[2], u[3], u[4], u[5]) for u in updates])
                wconn.executemany(
                    "UPDATE daily_price SET pct_change=?, turnover=? "
                    "WHERE code=? AND trade_date=?",
                    [(u[0], u[1], u[2], u[3]) for u in updates])
                if done % 50 == 0:
                    wconn.commit()

            if done % 100 == 0:
                print(f"  {done}/{len(codes)} 只  待改 pct={upd_pct} turn={upd_turn} "
                      f"对齐失败={mismatched}  {time.time()-t0:.0f}s", flush=True)
    finally:
        if wconn is not None:
            wconn.commit()
            wconn.close()
        rconn.close()
        bs.logout()
        log.close()

    print(f"\n扫描 {done} 只 / {total_rows} 行；可回补 pct_change={upd_pct} 格、"
          f"turnover={upd_turn} 格；OHLC 对不齐 {mismatched} 行；baostock 缺日期 {missing} 行；"
          f"无 baostock 数据 {no_bs} 只；放宽校验放行 {relaxed} 行；耗时 {time.time()-t0:.0f}s")
    print(f"明细: {log_path}")
    if args.apply:
        print(f"旧值备份表: {bak_table}（回滚：按该表逐行还原 pct_change/turnover）")
    else:
        print("[dry-run] 未写库。加 --apply 才会实际回补。")


if __name__ == "__main__":
    main()
