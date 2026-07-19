"""调试数据源连接"""
import akshare as ak
import pandas as pd
import time

print("测试单只股票拉取（各个数据源）...")

code = '000001'
start_date = '20260301'
end_date = '20260319'

# 1. 东方财富
print("\n[1] 东方财富 (stock_zh_a_hist)...")
try:
    print("  正在请求...", end='', flush=True)
    df = ak.stock_zh_a_hist(symbol=code, period='daily', start_date=start_date, end_date=end_date, adjust='qfq')
    print(f" OK - {len(df)} 行")
    print(df.head(2))
except Exception as e:
    print(f" FAIL")
    print(f"    {type(e).__name__}: {str(e)[:100]}")

time.sleep(2)

# 2. 腾讯
print("\n[2] 腾讯 (stock_zh_a_hist_tx)...")
try:
    print("  正在请求...", end='', flush=True)
    df = ak.stock_zh_a_hist_tx(symbol=code, start_date=start_date, end_date=end_date)
    print(f" OK - {len(df)} 行")
    print(df.head(2))
except Exception as e:
    print(f" FAIL")
    print(f"    {type(e).__name__}: {str(e)[:100]}")

time.sleep(2)

# 3. 新浪
print("\n[3] 新浪 (stock_zh_a_daily)...")
try:
    print("  正在请求...", end='', flush=True)
    df = ak.stock_zh_a_daily(symbol=code, start_date=start_date, end_date=end_date)
    print(f" OK - {len(df)} 行")
    print(df.head(2))
except Exception as e:
    print(f" FAIL")
    print(f"    {type(e).__name__}: {str(e)[:100]}")

print("\n\n=== 结论 ===")
print("如果所有源都失败，原因可能是：")
print("  1. 网络连接问题（代理、防火墙）")
print("  2. 服务器完全限速或宕机")
print("  3. 股票代码格式错误")
