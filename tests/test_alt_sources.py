"""测试腾讯数据源替代方案"""
import akshare as ak
import pandas as pd
import time

print("测试各种数据源稳定性...\n")

# 1. 原始接口（东方财富）
print("[1] stock_zh_a_hist (东方财富) - 拉取 000001:")
try:
    df = ak.stock_zh_a_hist(symbol='000001', period='daily', start_date='20260301', adjust='qfq')
    print(f"    OK - {len(df)} 行\n")
except Exception as e:
    print(f"    FAIL - {str(e)[:60]}...\n")

# 2. 腾讯数据源
print("[2] stock_zh_a_hist_tx (腾讯) - 拉取 000001:")
try:
    df = ak.stock_zh_a_hist_tx(symbol='000001', start_date='20260301', end_date='20260319')
    print(f"    OK - {len(df)} 行")
    print(df.head(3))
except Exception as e:
    print(f"    FAIL - {str(e)[:60]}...\n")

# 3. EM 数据源（新浪）
print("\n[3] stock_zh_a_daily (新浪) - 拉取 000001:")
try:
    df = ak.stock_zh_a_daily(symbol='000001', start_date='20260301', end_date='20260319')
    print(f"    OK - {len(df)} 行")
    print(df.head(3))
except Exception as e:
    print(f"    FAIL - {str(e)[:60]}...\n")

# 4. 现货行情
print("\n[4] stock_zh_a_spot_em (实时行情) - 拉取前10只:")
try:
    df = ak.stock_zh_a_spot_em()
    print(f"    OK - {len(df)} 行")
    print(df.head(3))
except Exception as e:
    print(f"    FAIL - {str(e)[:60]}...\n")
