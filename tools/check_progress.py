from core.db import db_stats

s = db_stats()
total = s['股票列表数']
have_data = s['有行情股票数']
pct = round(have_data/total*100, 1)

print(f"股票列表总数: {total}")
print(f"已拉取行情的股票数: {have_data}")
print(f"总行情记录数: {s['行情记录总数']}")
print(f"最新数据日期: {s['最新日期']}")
print(f"数据库大小: {s['数据库大小(MB)']} MB")
print(f"同步进度: {pct}%")
