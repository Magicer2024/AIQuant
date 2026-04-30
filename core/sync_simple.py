"""
sync_simple.py - 简化版数据同步
用本地缓存数据 + 超级降频请求
==============================

核心思路：
1. 首先用现有的 CSV 缓存补充数据库
2. 然后用超级降频（每分钟一个请求）逐步拉新数据
3. 优先级：本地CSV > 多源网络数据
"""

import akshare as ak
import pandas as pd
import time
import os
import glob
from datetime import datetime, date, timedelta
from core.db import init_db, upsert_stock_list, upsert_daily_price, get_all_stocks, db_stats

# ============ 配置 ============
HISTORY_START = "20240101"

# ============ 方案 1：导入本地 CSV 缓存 ============
def import_local_cache():
    """
    扫描 data_cache/ 目录，把已有的 CSV 导入到数据库
    这样可以快速填充数据而不需要网络请求
    """
    init_db()
    
    print("\n>>> 导入本地 CSV 缓存...")
    
    # 查找所有 CSV 文件
    csv_files = glob.glob("data_cache/*.csv") + glob.glob("data_cache/bt_*.csv")
    
    if not csv_files:
        print("    未找到本地 CSV 文件")
        return 0
    
    imported_count = 0
    
    for csv_file in csv_files:
        try:
            # 从文件名提取股票代码
            base = os.path.basename(csv_file)
            # 格式: bt_000001_20240101_20260301.csv 或 000001.csv
            if base.startswith("bt_"):
                code = base.replace("bt_", "").split("_")[0]
            else:
                code = base.replace(".csv", "").split("_")[0]
            
            # 读取 CSV
            df = pd.read_csv(csv_file, index_col=0, parse_dates=True)
            df.index = pd.to_datetime(df.index)
            
            # 标准化列名（兼容多种格式）
            col_map = {
                'open': 'open', 'high': 'high', 'low': 'low', 'close': 'close', 
                'volume': 'volume', 'amount': 'amount', 'pct_change': 'pct_change', 
                'turnover': 'turnover'
            }
            df.rename(columns=col_map, inplace=True, errors='ignore')
            
            # 只保留关键列
            cols_to_keep = [c for c in ['open', 'high', 'low', 'close', 'volume', 'amount', 'pct_change', 'turnover'] 
                           if c in df.columns]
            df = df[cols_to_keep]
            
            # 写入数据库
            n = upsert_daily_price(code, df)
            print(f"    [{code}] {os.path.basename(csv_file)} - 导入 {n} 行")
            imported_count += 1
            
            # 导入成功后删除缓存文件
            try:
                os.remove(csv_file)
                print(f"    [{code}] 已删除缓存文件")
            except Exception as e:
                print(f"    [{code}] 删除缓存失败: {str(e)[:30]}")
            
        except Exception as e:
            print(f"    [ERROR] {csv_file}: {str(e)[:50]}")
            continue
    
    print(f"\n    总共导入 {imported_count} 个股票的历史数据")
    
    s = db_stats()
    print(f"    当前数据库: {s['有行情股票数']} 只股票，{s['行情记录总数']} 条行情")
    
    return imported_count


# ============ 方案 2：超级降频拉新数据（可选） ============
def slow_fetch_new_data(num_stocks: int = 20, interval_sec: int = 60):
    """
    超级降频拉取新数据（每只股票间隔 >=60 秒）
    这是一个温和的、可以长期运行的方案
    
    Args:
        num_stocks: 每次运行拉取多少只股票
        interval_sec: 每只股票的请求间隔（秒）
    """
    init_db()
    
    stocks_df = get_all_stocks()
    if stocks_df.empty:
        print("股票列表为空")
        return
    
    stocks = list(zip(stocks_df["code"], stocks_df["name"]))[:num_stocks]
    
    print(f"\n>>> 超级降频拉取 {num_stocks} 只股票")
    print(f"    每只间隔: {interval_sec} 秒")
    print(f"    总耗时: ~{num_stocks * interval_sec / 60:.1f} 分钟\n")
    
    success = 0
    fail = 0
    
    for code, name in stocks:
        if code.startswith(("688", "300")) or "ST" in name:
            continue
        
        try:
            print(f"  [{code}] 正在拉取...", end='', flush=True)
            df = ak.stock_zh_a_hist(symbol=code, period='daily', 
                                   start_date=HISTORY_START, adjust='qfq')
            if not df.empty:
                n = upsert_daily_price(code, df)
                print(f" OK ({n} 行)")
                success += 1
            else:
                print(" 无数据")
                
        except Exception as e:
            print(f" FAIL: {str(e)[:30]}")
            fail += 1
        
        # 每只后间隔
        if code != stocks[-1][0]:  # 最后一只不需要等待
            time.sleep(interval_sec)
    
    print(f"\n  成功: {success}, 失败: {fail}")


# ============ 主入口 ============
if __name__ == "__main__":
    import sys
    
    init_db()
    
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
    else:
        cmd = "cache"
    
    if cmd == "cache":
        # 默认：导入本地缓存
        import_local_cache()
    
    elif cmd == "fetch":
        # 可选：拉取新数据（超级降频）
        num = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        interval = int(sys.argv[3]) if len(sys.argv) > 3 else 60
        slow_fetch_new_data(num, interval)
    
    elif cmd == "stat":
        s = db_stats()
        for k, v in s.items():
            print(f"{k:15s}: {v}")
    
    else:
        print(f"命令: cache | fetch [数量] [间隔秒数] | stat")
