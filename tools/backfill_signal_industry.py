# -*- coding: utf-8 -*-
"""信号层行业回填（管理A seq=477 A1-数据派单）：
stock_deep_signal.industry / deep_track.industry 按 stock_info.industry 当前分类回填。

硬边界（锁死）：
  - 只 UPDATE 两表的 industry 列中【空值行】（NULL 或空白串）；已有值一律覆盖式跳过；
  - 不删行、不改其它列（含 created_at/updated_at）、不动历史行情与 stock_info；
  - 映射源 = stock_info.industry 非空值（证监会分类，与管理A实测 99.91% 同源）；
  - 幂等：重跑更新 0 行；
  - 写前门禁：data_snapshots/quant_pre_a1industry_*.db 当日备份必须存在，否则拒写。

方法学披露（管理A规格）：用当前分类映射历史信号属【已知偏差】，
消费侧（接口/UI/周报）必须标注「按当前分类」，不得表述为信号时点分类。

用法：python tools/backfill_signal_industry.py --db PATH [--apply]
"""
from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import sys
import time

BACKUP_GLOB = "E:/小项目/Project/AIQuant/data_snapshots/quant_pre_a1industry_*.db"
TABLES = ("stock_deep_signal", "deep_track")


def load_map(conn) -> dict:
    return {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT code, industry FROM stock_info "
            "WHERE industry IS NOT NULL AND TRIM(industry) <> ''"
        )
    }


def stats(conn, label):
    print(f"── {label}")
    for t in TABLES:
        tot = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        non = conn.execute(
            f"SELECT COUNT(*) FROM {t} WHERE industry IS NOT NULL AND TRIM(industry)<>''"
        ).fetchone()[0]
        print(f"  {t}: 非空 {non}/{tot} = {non / tot * 100:.2f}%  (NULL {tot - non})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    mapping = load_map(conn)
    print(f"映射源 stock_info.industry 非空 code 数: {len(mapping)}")
    stats(conn, "回填前")

    # 计算计划：仅空值行
    plan_total = 0
    plan_unmapped = 0
    for t in TABLES:
        codes = [
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT code FROM {t} WHERE industry IS NULL OR TRIM(industry)=''"
            )
        ]
        hit = [c for c in codes if c in mapping]
        miss = [c for c in codes if c not in mapping]
        rows = 0
        if hit:
            q = ",".join("?" * len(hit))
            rows = conn.execute(
                f"SELECT COUNT(*) FROM {t} WHERE (industry IS NULL OR TRIM(industry)='') "
                f"AND code IN ({q})",
                hit,
            ).fetchone()[0]
        plan_total += rows
        print(f"  {t}: 空值 distinct code {len(codes)}，可映射 {len(hit)}，"
              f"计划回填 {rows} 行，不可映射 code {len(miss)} 个（保持 NULL，不硬填）")
        plan_unmapped += conn.execute(
            f"SELECT COUNT(*) FROM {t} WHERE (industry IS NULL OR TRIM(industry)='') "
            f"AND code NOT IN (SELECT code FROM stock_info WHERE industry IS NOT NULL AND TRIM(industry)!='')"
        ).fetchone()[0]

    if not args.apply:
        print(f"DRY-RUN：预计回填 {plan_total} 行，残留 NULL {plan_unmapped} 行（含已 NULL 不可映射）。零写入。")
        conn.close()
        return

    # 写前门禁：当日备份
    backups = [b for b in glob.glob(BACKUP_GLOB)
               if time.strftime("%Y%m%d", time.localtime(os.path.getmtime(b))) == time.strftime("%Y%m%d")]
    if not backups:
        print(f"写前门禁未过：当日无备份 {BACKUP_GLOB}，拒写。")
        sys.exit(2)
    print(f"写前门禁通过：{backups[-1]}")

    changed = 0
    for t in TABLES:
        cur = conn.execute(
            f"""UPDATE {t}
                SET industry = (
                    SELECT i.industry FROM stock_info i
                    WHERE i.code = {t}.code
                      AND i.industry IS NOT NULL AND TRIM(i.industry) <> ''
                )
                WHERE (industry IS NULL OR TRIM(industry) = '')
                  AND EXISTS (
                    SELECT 1 FROM stock_info i2
                    WHERE i2.code = {t}.code
                      AND i2.industry IS NOT NULL AND TRIM(i2.industry) <> ''
                  )"""
        )
        changed += cur.rowcount
        print(f"  {t}: 更新 {cur.rowcount} 行")
    conn.commit()

    print(f"写库完成：共更新 {changed} 行")
    stats(conn, "回填后")
    # 其它列零触碰核验：created_at 分布不变（抽样）
    for t in TABLES:
        col = "created_at" if conn.execute(f"PRAGMA table_info({t})").fetchone() else None
        if any(r[1] == "created_at" for r in conn.execute(f"PRAGMA table_info({t})")):
            mn = conn.execute(f"SELECT MIN(created_at), MAX(created_at), COUNT(created_at) FROM {t}").fetchone()
            print(f"  {t} created_at 边界（应不变）: {mn}")
    conn.close()


if __name__ == "__main__":
    main()
