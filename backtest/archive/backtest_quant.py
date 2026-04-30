"""
backtest_quant.py  ——  基于数据库的回测脚本
========================================
策略与 quant.py 完全一致：
  条件1: 成交量 > 5日均量 × 1.5（放量）
  条件2: 收盘 > MA5 > MA10 > MA20（多头排列）
  条件3: 3日涨幅 > 2%
  满足 2 条及以上 → 买入

止损: -3%  止盈: +7%  最大持仓: 5个交易日  每仓: 30%

数据来源：本地 SQLite 数据库（由 sync.py 维护）
"""

import pandas as pd
import numpy as np
import json
import os
from datetime import datetime, date

from core.db import init_db, get_all_stocks, get_daily_price, db_stats

# ─────────────────────────────────────────────
# 策略参数（与 quant.py 完全对齐）
# ─────────────────────────────────────────────
VOL_FACTOR   = 1.5
RISE_3D      = 0.02
SCORE_MIN    = 2
STOP_LOSS    = -0.03
TAKE_PROFIT  =  0.07
MAX_HOLD     =  5
INIT_CAPITAL = 100000
BT_START     = "2024-01-01"   # 回测起始日
BT_END       = None            # None = 今天


# ─────────────────────────────────────────────
# 信号生成（与 quant.py 完全一致）
# ─────────────────────────────────────────────

def gen_signals(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    close, vol = d["close"], d["volume"]
    d["MA5"]  = close.rolling(5).mean()
    d["MA10"] = close.rolling(10).mean()
    d["MA20"] = close.rolling(20).mean()
    d["VOL5"] = vol.rolling(5).mean()

    vol_up = vol > VOL_FACTOR * d["VOL5"]
    strong = (close > d["MA5"]) & (d["MA5"] > d["MA10"]) & (d["MA5"] > d["MA20"])
    rise3d = (close / close.shift(3) - 1) > RISE_3D

    d["SCORE"]      = vol_up.astype(int) + strong.astype(int) + rise3d.astype(int)
    d["BUY_SIGNAL"] = d["SCORE"] >= SCORE_MIN
    return d.dropna()


# ─────────────────────────────────────────────
# 单只股票回测
# ─────────────────────────────────────────────

def backtest_single(code: str, name: str) -> dict | None:
    # 读数据库
    df = get_daily_price(code, start_date=BT_START, end_date=BT_END)
    if df.empty or len(df) < 30:
        return None

    d = gen_signals(df)
    if d.empty:
        return None

    capital     = float(INIT_CAPITAL)
    position    = 0
    entry_price = 0.0
    entry_idx   = -1
    trades      = []
    equity      = []
    rows        = list(d.iterrows())

    for i, (dt, row) in enumerate(rows):
        price = float(row["close"])

        # ── 持仓中：检查出场条件 ──
        if position > 0:
            hold_days   = i - entry_idx
            ret         = (price - entry_price) / entry_price
            exit_reason = None

            if ret <= STOP_LOSS:
                exit_reason = "止损"
            elif ret >= TAKE_PROFIT:
                exit_reason = "止盈"
            elif hold_days >= MAX_HOLD:
                exit_reason = "到期平仓"

            if exit_reason:
                pnl_pct = ret * 100
                pnl_amt = (price - entry_price) * position
                capital += price * position
                trades.append({
                    "entry_date":  str(rows[entry_idx][0].date()),
                    "exit_date":   str(dt.date()),
                    "code":        code,
                    "name":        name,
                    "entry_price": round(entry_price, 2),
                    "exit_price":  round(price, 2),
                    "shares":      position,
                    "pnl_pct":     round(pnl_pct, 2),
                    "pnl_amt":     round(pnl_amt, 2),
                    "hold_days":   hold_days,
                    "exit_reason": exit_reason,
                    "win":         pnl_pct > 0,
                })
                position = 0

        # ── 空仓：检查买入信号 ──
        elif bool(row["BUY_SIGNAL"]) and i < len(rows) - 1:
            buy_cap = capital * 0.3
            shares  = int(buy_cap / price / 100) * 100
            if shares >= 100:
                capital    -= shares * price
                position    = shares
                entry_price = price
                entry_idx   = i

        equity.append(capital + position * price)

    # 强制平仓
    if position > 0:
        lp  = float(rows[-1][1]["close"])
        ld  = str(rows[-1][0].date())
        ret = (lp - entry_price) / entry_price
        trades.append({
            "entry_date":  str(rows[entry_idx][0].date()),
            "exit_date":   ld,
            "code":        code, "name": name,
            "entry_price": round(entry_price, 2),
            "exit_price":  round(lp, 2),
            "shares":      position,
            "pnl_pct":     round(ret * 100, 2),
            "pnl_amt":     round((lp - entry_price) * position, 2),
            "hold_days":   len(rows) - 1 - entry_idx,
            "exit_reason": "回测结束",
            "win":         ret > 0,
        })
        capital   += position * lp
        equity[-1] = capital

    if not trades:
        return None

    wins   = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]

    # 最大回撤
    ea  = np.array(equity)
    mdd = float(np.min((ea - np.maximum.accumulate(ea)) / np.maximum.accumulate(ea) * 100))

    # 盈亏比
    pf = (
        abs(sum(t["pnl_amt"] for t in wins)) /
        max(abs(sum(t["pnl_amt"] for t in losses)), 1)
    ) if losses else 999.0

    # 基准（买入持有）
    bench = (float(d.iloc[-1]["close"]) / float(d.iloc[0]["close"]) - 1) * 100
    total_ret = (capital / INIT_CAPITAL - 1) * 100

    return {
        "code":          code,
        "name":          name,
        "total_return":  round(total_ret, 2),
        "benchmark_ret": round(bench, 2),
        "alpha":         round(total_ret - bench, 2),
        "max_drawdown":  round(mdd, 2),
        "win_rate":      round(len(wins) / len(trades) * 100, 1),
        "profit_factor": round(pf, 2),
        "total_trades":  len(trades),
        "win_trades":    len(wins),
        "loss_trades":   len(losses),
        "avg_win_pct":   round(np.mean([t["pnl_pct"] for t in wins]), 2) if wins else 0,
        "avg_loss_pct":  round(np.mean([t["pnl_pct"] for t in losses]), 2) if losses else 0,
        "avg_hold_days": round(np.mean([t["hold_days"] for t in trades]), 1),
        "final_capital": round(capital, 2),
        "trades":        trades,
        "equity_curve":  [round(v, 2) for v in equity],
        "dates":         [str(r[0].date()) for r in rows],
    }


# ─────────────────────────────────────────────
# 全量回测
# ─────────────────────────────────────────────

def run_backtest(max_stocks: int = None) -> tuple[list, dict]:
    """
    对数据库中所有股票跑回测
    max_stocks: 限制数量（调试用），None=全量
    """
    stocks_df = get_all_stocks()
    if stocks_df.empty:
        print("[BT] 数据库股票列表为空，请先运行 sync.py 同步数据")
        return [], {}

    if max_stocks:
        stocks_df = stocks_df.head(max_stocks)

    total   = len(stocks_df)
    results = []

    print(f"\n{'='*55}")
    print(f"  策略回测  |  区间: {BT_START} ~ {BT_END or date.today()}")
    print(f"  共 {total} 只股票（来自数据库）")
    print(f"{'='*55}")

    for i, row in stocks_df.iterrows():
        code, name = row["code"], row["name"]
        r = backtest_single(code, name)
        if r:
            results.append(r)
            flag = "[+]" if r["total_return"] > 0 else "[-]"
            print(f"  {flag} {code} {name}  "
                  f"收益:{r['total_return']:+.1f}%  "
                  f"胜率:{r['win_rate']:.0f}%  "
                  f"交易:{r['total_trades']}次")

        if (i + 1) % 100 == 0:
            print(f"  --- 进度 {i+1}/{total} ---")

    if not results:
        print("[BT] 无有效回测结果")
        return [], {}

    all_trades = [t for r in results for t in r["trades"]]
    all_pnl    = [t["pnl_pct"] for t in all_trades]
    all_wins   = [t for t in all_trades if t["win"]]
    all_loss   = [t for t in all_trades if not t["win"]]

    summary = {
        "回测区间":     f"{BT_START} ~ {BT_END or date.today()}",
        "回测股票数":   len(results),
        "总交易次数":   len(all_trades),
        "综合胜率":     f"{len(all_wins)/max(len(all_trades),1)*100:.1f}%",
        "平均单次盈亏": f"{np.mean(all_pnl):+.2f}%" if all_pnl else "—",
        "平均盈利":     f"{np.mean([t['pnl_pct'] for t in all_wins]):+.2f}%" if all_wins else "—",
        "平均亏损":     f"{np.mean([t['pnl_pct'] for t in all_loss]):+.2f}%" if all_loss else "—",
        "平均持仓天数": f"{np.mean([t['hold_days'] for t in all_trades]):.1f}天" if all_trades else "—",
        "策略平均收益": f"{np.mean([r['total_return'] for r in results]):+.2f}%",
        "基准平均收益": f"{np.mean([r['benchmark_ret'] for r in results]):+.2f}%",
        "平均Alpha":    f"{np.mean([r['alpha'] for r in results]):+.2f}%",
        "平均最大回撤": f"{np.mean([r['max_drawdown'] for r in results]):.2f}%",
    }

    print(f"\n{'─'*50}")
    print("  汇总统计：")
    for k, v in summary.items():
        print(f"    {k}: {v}")

    return results, summary


# ─────────────────────────────────────────────
# 生成 HTML 回测报告
# ─────────────────────────────────────────────

def gen_report(results: list, summary: dict, out_path: str = None) -> str:
    if not results:
        return ""

    if out_path is None:
        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "回测报告.html")

    names      = [r["name"] for r in results]
    ret_vals   = [r["total_return"] for r in results]
    bench_vals = [r["benchmark_ret"] for r in results]
    win_rates  = [r["win_rate"] for r in results]
    max_dds    = [abs(r["max_drawdown"]) for r in results]

    all_trades = [t for r in results for t in r["trades"]]
    all_pnl    = [t["pnl_pct"] for t in all_trades]
    exit_reasons    = {}
    for t in all_trades:
        exit_reasons[t["exit_reason"]] = exit_reasons.get(t["exit_reason"], 0) + 1

    recent_trades = sorted(all_trades, key=lambda x: x["exit_date"], reverse=True)[:50]
    best  = max(results, key=lambda x: x["total_return"])
    worst = min(results, key=lambda x: x["total_return"])

    sum_html = "".join(
        f"<tr><td>{k}</td><td><strong>{v}</strong></td></tr>"
        for k, v in summary.items()
    )

    # 预序列化 JS 数据（避免 f-string 大括号冲突）
    js_names      = json.dumps(names, ensure_ascii=False)
    js_strat      = json.dumps(ret_vals)
    js_bench      = json.dumps(bench_vals)
    js_wr         = json.dumps(win_rates)
    js_mdd        = json.dumps(max_dds)
    js_bdates     = json.dumps(best["dates"])
    js_beq        = json.dumps(best["equity_curve"])
    js_reasons    = json.dumps(
        [{"name": k, "value": v} for k, v in exit_reasons.items()],
        ensure_ascii=False
    )
    js_allpnl     = json.dumps(all_pnl)

    trade_rows = ""
    for t in recent_trades:
        c = "#c0392b" if t["win"] else "#27ae60"
        trade_rows += (
            f"<tr><td>{t['exit_date']}</td>"
            f"<td>{t['code']} {t['name']}</td>"
            f"<td>{t['entry_price']}</td><td>{t['exit_price']}</td>"
            f"<td style='color:{c};font-weight:bold'>{t['pnl_pct']:+.2f}%</td>"
            f"<td>{t['hold_days']}天</td><td>{t['exit_reason']}</td></tr>"
        )

    stock_rows = ""
    for r in sorted(results, key=lambda x: x["total_return"], reverse=True):
        rc = "#c0392b" if r["total_return"] > 0 else "#27ae60"
        ac = "#c0392b" if r["alpha"] > 0 else "#27ae60"
        stock_rows += (
            f"<tr><td>{r['code']} {r['name']}</td>"
            f"<td style='color:{rc};font-weight:bold'>{r['total_return']:+.2f}%</td>"
            f"<td>{r['benchmark_ret']:+.2f}%</td>"
            f"<td style='color:{ac}'>{r['alpha']:+.2f}%</td>"
            f"<td>{r['win_rate']}%</td>"
            f"<td>{r['max_drawdown']:.1f}%</td>"
            f"<td>{r['total_trades']}</td>"
            f"<td>{r['avg_hold_days']}天</td></tr>"
        )

    stats = db_stats()

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>量化策略回测报告</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Microsoft YaHei","PingFang SC",sans-serif;background:#0d1117;color:#e6edf3}}
.hdr{{background:linear-gradient(135deg,#1a2233,#0d1117);padding:28px 40px;border-bottom:1px solid #30363d}}
.hdr h1{{font-size:24px;color:#f0f6ff}}
.hdr p{{color:#8b949e;margin-top:6px;font-size:13px}}
.wrap{{max-width:1400px;margin:0 auto;padding:20px}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin-bottom:20px}}
.kpi{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:18px;text-align:center}}
.kpi .v{{font-size:26px;font-weight:700;margin-bottom:4px}}
.kpi .l{{font-size:12px;color:#8b949e}}
.pos{{color:#ff6b6b}} .neg{{color:#51cf66}} .neu{{color:#74c0fc}}
.g2{{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:18px}}
.g3{{display:grid;grid-template-columns:2fr 1fr;gap:18px;margin-bottom:18px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:18px;margin-bottom:18px}}
.card h3{{font-size:14px;color:#c9d1d9;margin-bottom:14px;padding-bottom:8px;border-bottom:1px solid #21262d}}
.ch{{width:100%;height:300px}} .ch-lg{{width:100%;height:360px}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{background:#21262d;color:#8b949e;padding:9px 10px;text-align:left;font-weight:500;border-bottom:1px solid #30363d}}
td{{padding:8px 10px;border-bottom:1px solid #21262d;color:#c9d1d9}}
tr:hover td{{background:#1c2128}}
.rule{{background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:14px;font-size:13px;line-height:1.9}}
.rule code{{background:#21262d;padding:1px 6px;border-radius:3px;color:#79c0ff;font-family:Consolas,monospace}}
.tip{{background:#1c2128;border-left:3px solid #f0883e;padding:10px 14px;border-radius:3px;font-size:12px;color:#c9d1d9;margin-top:12px}}
.db-info{{background:#21262d;border-radius:6px;padding:10px 14px;font-size:12px;color:#8b949e;margin-bottom:18px}}
</style>
</head>
<body>
<div class="hdr">
  <h1>A股量化策略回测报告</h1>
  <p>
    回测区间: {summary.get('回测区间','—')} &nbsp;|&nbsp;
    股票数: {summary.get('回测股票数','—')} &nbsp;|&nbsp;
    数据来源: 本地数据库 &nbsp;|&nbsp;
    生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}
  </p>
</div>
<div class="wrap">

<div class="db-info">
  本地数据库: {stats['有行情股票数']} 只股票 &nbsp;/&nbsp;
  {stats['行情记录总数']} 条记录 &nbsp;/&nbsp;
  {stats['数据库大小(MB)']} MB &nbsp;/&nbsp;
  最新数据: {stats['最新日期']}
</div>

<!-- KPI -->
<div class="kpis">
  <div class="kpi">
    <div class="v {'pos' if float(summary['策略平均收益'].rstrip('%')) > 0 else 'neg'}">{summary['策略平均收益']}</div>
    <div class="l">策略平均收益</div>
  </div>
  <div class="kpi">
    <div class="v neu">{summary['基准平均收益']}</div>
    <div class="l">买入持有基准</div>
  </div>
  <div class="kpi">
    <div class="v {'pos' if float(summary['平均Alpha'].rstrip('%')) > 0 else 'neg'}">{summary['平均Alpha']}</div>
    <div class="l">平均 Alpha</div>
  </div>
  <div class="kpi">
    <div class="v pos">{summary['综合胜率']}</div>
    <div class="l">综合胜率</div>
  </div>
  <div class="kpi">
    <div class="v neu">{summary['总交易次数']}</div>
    <div class="l">总交易次数</div>
  </div>
  <div class="kpi">
    <div class="v neu">{summary['平均持仓天数']}</div>
    <div class="l">平均持仓天数</div>
  </div>
</div>

<!-- 策略说明 -->
<div class="card">
  <h3>策略规则</h3>
  <div class="rule">
    <b>买入条件（满足 2 条及以上触发）：</b><br>
    &nbsp; 条件1：今日成交量 &gt; 5日均量 × <code>1.5</code>（放量确认）<br>
    &nbsp; 条件2：收盘 &gt; MA5 &gt; MA10 &gt; MA20（均线多头排列）<br>
    &nbsp; 条件3：近3日涨幅 &gt; <code>2%</code>（短期动量）<br>
    <b>止损：</b><code>-3%</code> &nbsp; <b>止盈：</b><code>+7%</code> &nbsp;
    <b>最大持仓：</b><code>5个交易日</code> &nbsp; <b>每仓资金：</b><code>30%</code>
  </div>
  <div class="tip">回测结果仅供参考，不代表未来收益。实盘存在滑点、冲击成本等差异。</div>
</div>

<!-- 图1+图2 -->
<div class="g2">
  <div class="card"><h3>各股票策略收益 vs 买入持有基准</h3><div id="c1" class="ch"></div></div>
  <div class="card"><h3>各股票胜率 & 最大回撤</h3><div id="c2" class="ch"></div></div>
</div>

<!-- 图3+图4 -->
<div class="g3">
  <div class="card"><h3>最佳标的资金曲线（{best['name']}  {best['total_return']:+.1f}%）</h3><div id="c3" class="ch-lg"></div></div>
  <div class="card"><h3>退出原因分布</h3><div id="c4" class="ch-lg"></div></div>
</div>

<!-- 图5 -->
<div class="card"><h3>单笔交易盈亏分布（红=盈利 绿=亏损，A股惯例）</h3><div id="c5" class="ch"></div></div>

<!-- 各股明细 -->
<div class="card">
  <h3>各股票回测明细（按收益率排序）</h3>
  <table>
    <thead><tr><th>股票</th><th>策略收益</th><th>基准收益</th><th>Alpha</th><th>胜率</th><th>最大回撤</th><th>交易次数</th><th>均持仓</th></tr></thead>
    <tbody>{stock_rows}</tbody>
  </table>
</div>

<!-- 汇总 + 交易记录 -->
<div class="g2">
  <div class="card">
    <h3>综合统计</h3>
    <table><tbody>{sum_html}</tbody></table>
  </div>
  <div class="card">
    <h3>最近交易记录（最新50笔）</h3>
    <div style="max-height:320px;overflow-y:auto">
    <table>
      <thead><tr><th>日期</th><th>股票</th><th>买入</th><th>卖出</th><th>收益</th><th>持仓</th><th>原因</th></tr></thead>
      <tbody>{trade_rows}</tbody>
    </table>
    </div>
  </div>
</div>

</div>
<script>
var names  = {js_names};
var strat  = {js_strat};
var bench  = {js_bench};
var wr     = {js_wr};
var mdd    = {js_mdd};
var bdates = {js_bdates};
var beq    = {js_beq};
var reasons= {js_reasons};
var allPnl = {js_allpnl};
var init   = {INIT_CAPITAL};

// c1 收益对比
(function(){{
  var c=echarts.init(document.getElementById('c1'));
  c.setOption({{backgroundColor:'transparent',
    tooltip:{{trigger:'axis'}},
    legend:{{data:['策略','基准'],textStyle:{{color:'#8b949e'}}}},
    grid:{{left:40,right:10,bottom:60,top:36,containLabel:true}},
    xAxis:{{type:'category',data:names,axisLabel:{{color:'#8b949e',rotate:30,fontSize:10}},axisLine:{{lineStyle:{{color:'#30363d'}}}}}},
    yAxis:{{type:'value',axisLabel:{{color:'#8b949e',formatter:'{{value}}%'}},splitLine:{{lineStyle:{{color:'#21262d'}}}}}},
    series:[
      {{name:'策略',type:'bar',barMaxWidth:24,data:strat.map(v=>{{return{{value:v,itemStyle:{{color:v>=0?'#ff6b6b':'#51cf66'}}}}}})  }},
      {{name:'基准',type:'bar',barMaxWidth:24,data:bench.map(v=>{{return{{value:v,itemStyle:{{color:v>=0?'#ff9f7f':'#87d068'}}}}}})  }}
    ]
  }});
}})();

// c2 胜率&回撤
(function(){{
  var c=echarts.init(document.getElementById('c2'));
  c.setOption({{backgroundColor:'transparent',
    tooltip:{{trigger:'axis'}},
    legend:{{data:['胜率%','最大回撤%'],textStyle:{{color:'#8b949e'}}}},
    grid:{{left:40,right:10,bottom:60,top:36,containLabel:true}},
    xAxis:{{type:'category',data:names,axisLabel:{{color:'#8b949e',rotate:30,fontSize:10}},axisLine:{{lineStyle:{{color:'#30363d'}}}}}},
    yAxis:{{type:'value',max:100,axisLabel:{{color:'#8b949e',formatter:'{{value}}%'}},splitLine:{{lineStyle:{{color:'#21262d'}}}}}},
    series:[
      {{name:'胜率%',type:'bar',barMaxWidth:24,data:wr,itemStyle:{{color:'#74c0fc'}}}},
      {{name:'最大回撤%',type:'bar',barMaxWidth:24,data:mdd,itemStyle:{{color:'#f06595'}}}}
    ]
  }});
}})();

// c3 资金曲线
(function(){{
  var c=echarts.init(document.getElementById('c3'));
  c.setOption({{backgroundColor:'transparent',
    tooltip:{{trigger:'axis',formatter:p=>p[0].name+'<br>净值: ¥'+p[0].value.toLocaleString()}},
    grid:{{left:64,right:10,bottom:36,top:16}},
    xAxis:{{type:'category',data:bdates,axisLabel:{{color:'#8b949e',interval:'auto',fontSize:9}},axisLine:{{lineStyle:{{color:'#30363d'}}}}}},
    yAxis:{{type:'value',axisLabel:{{color:'#8b949e',formatter:v=>'¥'+v.toLocaleString()}},splitLine:{{lineStyle:{{color:'#21262d'}}}}}},
    series:[{{type:'line',data:beq,smooth:0.3,
      lineStyle:{{color:'#ff6b6b',width:2}},
      areaStyle:{{color:{{type:'linear',x:0,y:0,x2:0,y2:1,colorStops:[{{offset:0,color:'rgba(255,107,107,0.3)'}},{{offset:1,color:'rgba(255,107,107,0.02)'}}]}}}},
      markLine:{{data:[{{yAxis:init,lineStyle:{{color:'#8b949e',type:'dashed'}},label:{{formatter:'初始'}}}}]}}
    }}]
  }});
}})();

// c4 退出原因
(function(){{
  var c=echarts.init(document.getElementById('c4'));
  c.setOption({{backgroundColor:'transparent',
    tooltip:{{trigger:'item',formatter:'{{b}}: {{c}}次 ({{d}}%)'}},
    legend:{{orient:'vertical',left:'left',textStyle:{{color:'#8b949e'}}}},
    series:[{{type:'pie',radius:['35%','70%'],data:reasons,
      itemStyle:{{borderColor:'#161b22',borderWidth:2}},label:{{color:'#c9d1d9'}}}}]
  }});
}})();

// c5 盈亏分布
(function(){{
  var c=echarts.init(document.getElementById('c5'));
  var bk={{}};
  for(var i=-15;i<=15;i++)bk[i]=0;
  allPnl.forEach(v=>{{var k=Math.max(-15,Math.min(15,Math.round(v)));bk[k]=(bk[k]||0)+1;}});
  var xd=Object.keys(bk).map(Number).sort((a,b)=>a-b);
  var yd=xd.map(k=>bk[k]||0);
  c.setOption({{backgroundColor:'transparent',
    tooltip:{{trigger:'axis',formatter:p=>'盈亏~'+p[0].name+'%: '+p[0].value+'笔'}},
    grid:{{left:40,right:10,bottom:36,top:16,containLabel:true}},
    xAxis:{{type:'category',data:xd.map(v=>v+'%'),axisLabel:{{color:'#8b949e'}},axisLine:{{lineStyle:{{color:'#30363d'}}}}}},
    yAxis:{{type:'value',axisLabel:{{color:'#8b949e'}},splitLine:{{lineStyle:{{color:'#21262d'}}}}}},
    series:[{{type:'bar',barMaxWidth:28,
      data:yd.map((v,i)=>{{return{{value:v,itemStyle:{{color:xd[i]>0?'#ff6b6b':'#51cf66'}}}}}})
    }}]
  }});
}})();
</script>
</body>
</html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n[BT] HTML 报告已生成: {out_path}")
    return out_path


# ─────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    init_db()

    stats = db_stats()
    print(f"[DB] 数据库: {stats['有行情股票数']} 只股票 / {stats['行情记录总数']} 条记录 / 最新: {stats['最新日期']}")

    if stats["有行情股票数"] == 0:
        print("\n[警告] 数据库暂无行情数据！")
        print("请先运行: python sync.py init")
        print("或测试用:  python sync.py init_test （只同步50只）")
        sys.exit(0)

    # 取最多500只跑回测（全量数据则去掉限制）
    max_n = int(sys.argv[1]) if len(sys.argv) > 1 else None
    results, summary = run_backtest(max_stocks=max_n)

    if results:
        gen_report(results, summary)
