"""测试替代数据源"""
import akshare as ak
import pandas as pd

# 获取所有 stock 相关的函数
all_funcs = dir(ak)
stock_funcs = [f for f in all_funcs if 'stock' in f.lower()]

print("AKShare 中所有包含 'stock' 的函数:")
for i, f in enumerate(sorted(stock_funcs)):
    print(f"  {i+1:3d}. {f}")

print(f"\n共 {len(stock_funcs)} 个函数")

# 重点看看以下几个
print("\n\n尝试几个可能的接口:")

# 1. try EM 版本
print("\n[1] stock_zh_a_hist_min_em (分钟线):")
try:
    df = ak.stock_zh_a_hist_min_em(symbol='000001', start_date='2026-03-01 09:30:00', 
                                   end_date='2026-03-01 15:00:00', period=5)
    print(f"    OK - 获取 {len(df)} 行")
except Exception as e:
    print(f"    FAIL - {type(e).__name__}")

# 2. try tencent 版本  
print("\n[2] stock_zh_a_tick (逐笔成交):")
try:
    df = ak.stock_zh_a_tick(symbol='000001', start_time='09:30:00', end_time='15:00:00')
    print(f"    OK - 获取 {len(df)} 行")
except Exception as e:
    print(f"    FAIL - {type(e).__name__}")

# 3. try 东财网页数据
print("\n[3] stock_zh_a_index_daily (指数日线):")
try:
    df = ak.stock_zh_a_index_daily(symbol='000001')
    print(f"    OK - 获取 {len(df)} 行")
except Exception as e:
    print(f"    FAIL - {type(e).__name__}")
