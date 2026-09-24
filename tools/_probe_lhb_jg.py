"""临时探针：验证 akshare 龙虎榜机构类接口的可用性与列结构。"""
import os
import sys

os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)

import akshare as ak                    # noqa: E402

pd_show = lambda df: print(f"  shape={df.shape}\n  cols={list(df.columns)}\n{df.head(3).to_string()}\n")  # noqa: E731

print("=== 1) stock_lhb_jgmmtj_em 机构买卖每日统计 ===")
try:
    df = ak.stock_lhb_jgmmtj_em(start_date="20260915", end_date="20260922")
    pd_show(df)
except Exception as ex:
    print("  FAIL:", type(ex).__name__, ex)

print("=== 2) stock_lhb_stock_detail_em 个股席位明细 ===")
try:
    df = ak.stock_lhb_stock_detail_em(symbol="000001", date="20260918", flag="买入")
    pd_show(df)
except Exception as ex:
    print("  FAIL:", type(ex).__name__, ex)

print("=== 3) stock_lhb_jgstatistic_em 机构席位追踪 ===")
try:
    df = ak.stock_lhb_jgstatistic_em(symbol="近一月")
    pd_show(df)
except Exception as ex:
    print("  FAIL:", type(ex).__name__, ex)
