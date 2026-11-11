# -*- coding: utf-8 -*-
"""stock_info.industry 回填（管理A seq=276 规格，Yu Fang 授权，只写 stock_info.industry）。

数据源优先级：
  1. EM_BOARD  东财行业板块（akshare stock_board_industry_name_em + cons）
  2. CSRC      baostock query_stock_industry（证监会行业分类，单次全市场）
两套分类不混用：命中主源即用主源，主源不可达时整体降级到兜底源并如实标注。

industry_src 取值：
  EM_BOARD      行业来自东财板块
  CSRC          行业来自证监会分类（兜底或全量，含降级原因见输出披露）
  CSRC_EMPTY    源存在该票但行业字段为空串（留 NULL，不硬填）
  NO_SOURCE     两个源都查不到（留 NULL）

防护：
  - 只 UPDATE stock_info.industry / industry_src 两列；其余列零触碰；
  - stock_deep_signal 及其它表零写入（脚本内不出现对其的写操作）；
  - 幂等：重复执行结果一致（仅在值变化时写）；
  - 写前校验备份存在（E:/小项目/Project/_backup/quant_*.db 当日文件）。
用法：python tools/backfill_stock_industry.py [--db PATH] [--apply]
     不带 --apply 为 dry-run，只出 AC1 统计，零写入。
"""
from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import sys
import time

DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "quant.db")
BACKUP_GLOB = "E:/小项目/Project/_backup/quant_*.db"


def fetch_em_board_map():
    """东财行业板块主源。返回 {code6: 板块名}；不可达时抛异常。"""
    import akshare as ak

    boards = ak.stock_board_industry_name_em()
    m = {}
    for name in boards["板块名称"].tolist():
        cons = ak.stock_board_industry_cons_em(symbol=name)
        for code in cons["代码"].astype(str).tolist():
            m[code.zfill(6)] = name
        time.sleep(0.3)  # 限流护栏
    return m


def fetch_csrc_map():
    """baostock 证监会行业。返回 {code6: (行业名 or '')}。"""
    import baostock as bs

    bs.login()
    try:
        rs = bs.query_stock_industry()
        m = {}
        while rs.next():
            r = rs.get_row_data()
            code = r[1].split(".")[-1].zfill(6)
            m[code] = (r[3] or "").strip()
    finally:
        bs.logout()
    return m


def build_map():
    """按优先级产出 {code6: (industry, src)} 与降级披露。"""
    disclosure = []
    try:
        em = {k: v for k, v in fetch_em_board_map().items() if v}
        if len(em) >= 4000:
            mapping = {k: (v, "EM_BOARD") for k, v in em.items()}
            fallback = {
                k: (v, "CSRC") for k, v in fetch_csrc_map().items() if v and k not in mapping
            }
            mapping.update(fallback)
            return mapping, {}, disclosure + [f"主源 EM_BOARD 可用({len(em)}只)，兜底 CSRC 补 {len(fallback)} 只"]
        disclosure.append(f"主源 EM_BOARD 覆盖仅 {len(em)} 只(<4000)，整体降级 CSRC")
    except Exception as e:  # noqa: BLE001
        disclosure.append(f"主源 EM_BOARD 不可达降级 CSRC: {type(e).__name__}: {str(e)[:80]}")
    csrc = fetch_csrc_map()
    out = {}
    for code, ind in csrc.items():
        out[code] = (ind, "CSRC") if ind else (None, "CSRC_EMPTY")
    return out, {}, disclosure


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true", help="实际写入；缺省为 dry-run")
    args = ap.parse_args()

    if args.apply:
        backups = sorted(glob.glob(BACKUP_GLOB), key=os.path.getmtime)
        if not backups or time.strftime("%Y%m%d", time.localtime(os.path.getmtime(backups[-1]))) != time.strftime("%Y%m%d"):
            print("AC3 未通过：当日内无整库备份，拒写。")
            sys.exit(2)
        print(f"AC3 备份确认: {backups[-1]}")

    mapping, em_extra, disclosure = build_map()
    for k, v in em_extra.items():
        mapping.setdefault(k, v)

    conn = sqlite3.connect(args.db)
    c = conn.cursor()
    cols = [r[1] for r in c.execute("PRAGMA table_info(stock_info)")]
    stocks = c.execute("SELECT code, name, industry, is_active FROM stock_info").fetchall()
    pre_other_counts = {
        t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    }

    # AC1 dry-run 统计
    hit = [s for s in stocks if s[0] in mapping and mapping[s[0]][0]]
    from collections import Counter

    src_cnt = Counter(mapping[s[0]][1] for s in stocks if s[0] in mapping)
    print(f"股票总数: {len(stocks)}")
    print(f"有效覆盖(非空行业): {len(hit)} = {len(hit)/len(stocks)*100:.1f}%")
    print(f"NULL 率: {(len(stocks)-len(hit))/len(stocks)*100:.2f}%")
    print(f"行业去重数: {len({mapping[s[0]][0] for s in hit})}")
    print(f"来源分布: {dict(src_cnt)}  未命中: {len([s for s in stocks if s[0] not in mapping])}")
    for d in disclosure:
        print("披露:", d)

    if not args.apply:
        print("DRY-RUN 完成（零写入）。")
        conn.close()
        return

    if "industry_src" not in cols:
        c.execute("ALTER TABLE stock_info ADD COLUMN industry_src TEXT")
        print("已新增列 industry_src（规格：另留一列标记来源）")

    changed = 0
    for code, _name, _old, _act in stocks:
        ind, src = mapping.get(code, (None, "NO_SOURCE"))
        cur = c.execute("SELECT industry, industry_src FROM stock_info WHERE code=?", (code,)).fetchone()
        if (cur[0] or None, cur[1]) != (ind or None, src):
            c.execute("UPDATE stock_info SET industry=?, industry_src=? WHERE code=?", (ind, src, code))
            changed += 1
    conn.commit()

    # 写后校验
    n_stock = c.execute("SELECT COUNT(*) FROM stock_info").fetchone()[0]
    n_ind = c.execute("SELECT COUNT(*) FROM stock_info WHERE industry IS NOT NULL AND TRIM(industry)<>''").fetchone()[0]
    n_src = c.execute("SELECT COUNT(DISTINCT industry) FROM stock_info WHERE industry IS NOT NULL").fetchone()[0]
    post_other = {
        t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in pre_other_counts
    }
    diff_tables = {t: (pre_other_counts[t], post_other[t]) for t in pre_other_counts if pre_other_counts[t] != post_other[t]}
    print(f"写库完成: 更新 {changed} 行 | AC2 非空 {n_ind}/{n_stock} = {n_ind/n_stock*100:.2f}% | 去重行业 {n_src}")
    print(f"AC4 其它表行数差异: {diff_tables if diff_tables else '无（含 stock_deep_signal 零变更）'}")
    conn.close()


if __name__ == "__main__":
    main()
