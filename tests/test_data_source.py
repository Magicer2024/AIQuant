"""测试各种数据源"""
import akshare as ak
import pandas as pd
from datetime import date

print("[1] 测试 AKShare akshare.stock_zh_a_hist()...")
try:
    df = ak.stock_zh_a_hist(symbol='000001', period='daily', start_date='20260301', adjust='qfq')
    print(f"    OK - 获取 {len(df)} 行数据")
    print(df.head(2))
except Exception as e:
    print(f"    FAIL - {type(e).__name__}: {str(e)[:80]}")

print("\n[2] 测试 AKShare tencent 接口...")
try:
    df = ak.stock_zh_a_hist_min(symbol='000001', period=5, start_date='20260301 09:30:00')
    print(f"    OK - 获取 {len(df)} 行数据")
except Exception as e:
    print(f"    FAIL - {type(e).__name__}: {str(e)[:80]}")

print("\n[3] 测试 AKShare 新浪接口...")
try:
    df = ak.stock_zh_a_sina(symbol='000001')
    print(f"    OK - {df}")
except Exception as e:
    print(f"    FAIL - {type(e).__name__}: {str(e)[:80]}")

print("\n[4] 查看 AKShare 可用函数列表...")
try:
    funcs = [x for x in dir(ak) if 'stock' in x.lower() and 'hist' in x.lower()]
    for f in funcs[:10]:
        print(f"    - {f}")
except Exception as e:
    print(f"    FAIL - {e}")
