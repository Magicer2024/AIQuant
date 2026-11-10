# stock_daily_flags —— 停牌 / 涨跌停 / ST 可查询标记（只读研究用）

给入场端 alpha 切片研究回答"当天到底能不能成交"。**不进策略参数、不改推荐口径。**

## 生成与刷新

```bash
python tools/build_stock_daily_flags.py --days 250 --db core/quant.db
```
幂等 upsert，每日跑一次即可增量刷新（重算最近 N 个交易日）。

## 表结构（stock_daily_flags，主键 code+trade_date）

| 字段 | 含义 | 判定 |
|---|---|---|
| limit_pct | 当日涨跌停幅度 | ST 0.05；30/68 开头 0.20；4/8 开头（北交所）0.30；其余 0.10 |
| is_st | ST 标记 | stock_info.name LIKE '%ST%'（见局限⑃） |
| suspended | 停牌 | 交易日内该股无 daily_price 行，或 volume<=0 |
| limit_up_sealed / limit_down_sealed | 收盘封涨/跌停 | close >= prev_close*(1+lp)*(1-0.002)（tol 与 utils/trade_constraints.py 同源） |
| one_word_up / one_word_down | 一字板 | high==low 且封板 |
| open_sealed_up / open_sealed_down | 开盘即封板 | open 已达涨/跌停价（buy/sell 当日开盘不可执行） |
| prev_close | 涨跌停基准 | 该股上一**有行情**交易日收盘（跳过停牌日，与交易所除权基准一致；不处理送转除权，见局限②） |

## 典型查询（切片研究示例）

```sql
-- 排除停牌与一字涨停后，某信号日的可成交样本
SELECT s.code, s.trade_date FROM stock_signal s
JOIN stock_daily_flags f ON f.code=s.code AND f.trade_date=s.trade_date
WHERE f.suspended=0 AND f.one_word_up=0;

-- 涨停打开日（touch 但未 seal）：limit_up_sealed 需结合当日 high 判定
```

## 已知局限（如实标注）

1. **日历口径**：交易日历 = daily_price 全市场有行的日期；若出现全市场缺日（如 10-29 事故当天），该日无 flag，P0 自愈合入后不会再发生。
2. **除权日**：prev_close 直接用上一有行情日 close，分红送转日会把除权缺口误判为跌停/涨停各一次。全库缺复权因子表（技术债），除权密集区（6-7 月）建议按 `abs(pct_change) > limit_pct*100+0.5` 的异常行二次核对。
3. **is_st 依赖 stock_info.name 快照**：当前库内名称无 ST 命中；若名称源不含 ST 前缀，此字段恒 0，需接实时名称后重跑。
4. 仅回补最近 250 个交易日；更早区间加 `--days`（每 250 日 ≈ 110 万行）。
5. 首日/复牌首日无 prev_close 基准 → 涨跌停一律不判（保守 0）。

代码：core/stock_flags.py（纯函数可单测）+ tools/build_stock_daily_flags.py（CLI）。测试：tests/test_stock_flags.py（8 项）。
