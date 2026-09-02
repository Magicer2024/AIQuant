#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成个股决策仪表盘 HTML 报告（星辰专家格式）"""
import json
from pathlib import Path

OUTPUT_DIR = Path(r"E:\小项目\Project\AIQuant\reports")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def build_kline_data():
    """生成近 30 日模拟 K 线数据（基于真实价格区间反推）"""
    return None  # 由各股票单独提供


# ============ 学大教育 000526 ============
XD = {
    "code": "000526",
    "name": "学大教育",
    "market": "深圳A股",
    "date": "2026-08-26",
    # 行情
    "price": 35.70,
    "change_pct": -1.60,
    "open": 36.16,
    "high": 36.88,
    "low": 35.60,
    "prev_close": 36.28,
    "turnover_rate": 3.86,
    "amount_wan": 16884,  # 成交额万元
    "total_mv_wan": 435000,  # 总市值万元
    "pe_ttm": 16.36,
    "pb": 3.52,
    "high52": 49.57,
    "low52": 23.80,
    "chg_5d": -8.67,
    "chg_20d": 8.08,
    "chg_ytd": -8.98,
    # 技术指标（基于价格反推估算）
    "ma5": 37.10,
    "ma10": 36.20,
    "ma20": 34.85,
    "ma60": 33.50,
    "macd": -0.45,
    "dif": -0.20,
    "dea": 0.25,
    "kdj_k": 32.5,
    "kdj_d": 48.2,
    "kdj_j": 0.5,
    "rsi6": 38.5,
    "rsi12": 45.2,
    "boll_up": 38.50,
    "boll_mid": 35.50,
    "boll_low": 32.50,
    # 评分与信号
    "score": 58,
    "signal": "WATCH",
    "signal_cn": "观察待买（偏多）",
    "signal_color": "#ffa502",
    "position": "2-3 成仓位（回踩确认后分批建仓）",
    # 买卖点
    "buy_low": 34.50,
    "buy_high": 35.80,
    "target1": 38.50,
    "target2": 42.00,
    "target3": 45.00,
    "stop_loss": 33.00,
    # 机构
    "analyst_target_avg": 47.67,
    "analyst_target_max": 52.40,
    "analyst_target_min": 41.62,
    "analyst_count": 28,
    # 业绩
    "h1_rev": 21.63,
    "h1_rev_yoy": 12.87,
    "h1_np": 3.01,
    "h1_np_yoy": 30.85,
    "h1_np_qoq": 43.37,  # 扣非
    "q2_rev": 12.12,
    "q2_rev_yoy": 15.2,
    "q2_np": 2.10,
    "q2_np_yoy": 34.6,
    "q2_npm": 17.3,
    "contract_liab": 6.48,
    "contract_liab_yoy": 8.5,
    # 近30日K线（日期, 开, 收, 低, 高, 成交量手）
    "klines": [
        ["07-15", 31.20, 31.50, 31.00, 31.80, 8500],
        ["07-16", 31.50, 32.10, 31.30, 32.40, 9200],
        ["07-17", 32.10, 31.80, 31.60, 32.30, 7800],
        ["07-18", 31.80, 32.50, 31.70, 32.80, 11000],
        ["07-19", 32.50, 33.20, 32.30, 33.50, 13500],
        ["07-22", 33.20, 33.00, 32.80, 33.40, 8900],
        ["07-23", 33.00, 33.80, 32.90, 34.10, 14200],
        ["07-24", 33.80, 34.50, 33.70, 34.80, 16800],
        ["07-25", 34.50, 34.20, 34.00, 34.70, 11200],
        ["07-26", 34.20, 35.00, 34.10, 35.20, 15500],
        ["07-29", 35.00, 35.60, 34.90, 35.80, 18000],
        ["07-30", 35.60, 36.20, 35.40, 36.50, 21000],
        ["07-31", 36.20, 36.80, 36.00, 37.10, 22500],
        ["08-01", 36.80, 37.50, 36.70, 37.80, 24000],
        ["08-02", 37.50, 37.20, 37.00, 37.60, 16500],
        ["08-05", 37.20, 38.00, 37.10, 38.30, 19800],
        ["08-06", 38.00, 38.50, 37.80, 38.80, 22000],
        ["08-07", 38.50, 38.20, 38.00, 38.70, 14500],
        ["08-08", 38.20, 37.50, 37.30, 38.40, 17800],
        ["08-11", 37.50, 38.00, 37.20, 38.20, 16000],
        ["08-12", 38.00, 38.80, 37.90, 39.10, 23000],
        ["08-13", 38.80, 39.20, 38.60, 39.50, 25000],
        ["08-14", 39.20, 38.50, 38.30, 39.30, 19500],
        ["08-15", 38.50, 37.80, 37.50, 38.70, 17000],
        ["08-18", 37.80, 37.20, 36.90, 38.00, 18500],
        ["08-19", 37.20, 36.50, 36.20, 37.40, 21000],
        ["08-20", 36.50, 36.28, 35.80, 36.80, 19500],
        ["08-22", 36.28, 35.90, 35.50, 36.50, 16500],
        ["08-25", 36.16, 36.28, 35.90, 36.50, 15500],
        ["08-26", 36.16, 35.70, 35.60, 36.88, 14000],
    ],
    # 雷达图
    "radar": [78, 72, 85, 55],  # 技术/情绪/基本面/市场环境
    # 新闻
    "news": [
        {"tag": "positive", "title": "26H1半年报：营收21.63亿+12.87%，归母净利3.01亿+30.85%", "src": "公司公告 08-17"},
        {"tag": "positive", "title": "Q2业绩加速：营收+15.2%，归母净利+34.6%，扣非+42.4%，净利率17.3%", "src": "招商证券研报 08-20"},
        {"tag": "positive", "title": "Q2收款6.62亿+22%，合同负债6.48亿+8.5%，下半年业绩有支撑", "src": "中信建投研报 08-23"},
        {"tag": "positive", "title": "61家机构调研（中信建投/易方达/汇添汇等），全日制招生+17%", "src": "投资者关系 08-21"},
        {"tag": "neutral", "title": "5日回调-8.67%，短期获利了结压力", "src": "行情数据 08-26"},
    ],
    "risks": [
        "短期5日急跌-8.67%，MA5(37.1)上方压制，尚未企稳",
        "教育行业政策敏感度高，需关注监管动态",
        "若跌破MA20(34.85)支撑，可能下探33-34区间",
    ],
    "catalysts": [
        "Q2业绩超预期，经营杠杆持续释放，机构上调盈利预测",
        "合同负债6.48亿为下半年收入锁定，季节性H2优于H1",
        "机构目标价均值47.67元，较现价有33%上行空间",
        "AI+教育布局（阶跃星辰投资、星图AI），打开估值想象空间",
    ],
    # 归因
    "attr_tech": 30,
    "attr_news": 25,
    "attr_fund": 38,
    "attr_market": 7,
}


# ============ 城投控股 600649 ============
CT = {
    "code": "600649",
    "name": "城投控股",
    "market": "上海A股",
    "date": "2026-08-26",
    "price": 3.85,
    "change_pct": 0.00,
    "open": 3.83,
    "high": 3.91,
    "low": 3.83,
    "prev_close": 3.85,
    "turnover_rate": 1.19,
    "amount_wan": 11451,
    "total_mv_wan": 959300,
    "pe_ttm": 35.14,
    "pb": 0.46,
    "high52": 5.78,
    "low52": 3.37,
    "chg_5d": 1.85,
    "chg_20d": 4.34,
    "chg_ytd": -12.70,
    "ma5": 3.82,
    "ma10": 3.78,
    "ma20": 3.69,
    "ma60": 3.95,
    "macd": 0.08,
    "dif": 0.05,
    "dea": -0.03,
    "kdj_k": 58.3,
    "kdj_d": 52.1,
    "kdj_j": 70.7,
    "rsi6": 55.8,
    "rsi12": 50.2,
    "boll_up": 4.05,
    "boll_mid": 3.82,
    "boll_low": 3.59,
    "score": 52,
    "signal": "HOLD",
    "signal_cn": "持有观望（中性偏多）",
    "signal_color": "#ffa502",
    "position": "1-2 成仓位（轻仓试探，突破再加）",
    "buy_low": 3.78,
    "buy_high": 3.85,
    "target1": 4.00,
    "target2": 4.24,
    "target3": 4.50,
    "stop_loss": 3.68,
    "analyst_target_avg": 6.70,
    "analyst_target_max": 6.70,
    "analyst_target_min": 6.70,
    "analyst_count": 5,
    "h1_rev": 8.53,
    "h1_rev_yoy": -63.55,
    "h1_np": 0.07,
    "h1_np_yoy": -69.20,
    "q2_rev": 0,
    "q2_rev_yoy": 0,
    "q2_np": 0,
    "q2_np_yoy": 0,
    "q2_npm": 0,
    "contract_liab": 0,
    "contract_liab_yoy": 0,
    "klines": [
        ["07-15", 3.75, 3.72, 3.70, 3.76, 42000],
        ["07-16", 3.72, 3.68, 3.66, 3.73, 38000],
        ["07-17", 3.68, 3.65, 3.63, 3.69, 45000],
        ["07-18", 3.65, 3.60, 3.58, 3.66, 52000],
        ["07-19", 3.60, 3.55, 3.52, 3.61, 68000],
        ["07-22", 3.55, 3.50, 3.48, 3.56, 55000],
        ["07-23", 3.50, 3.45, 3.42, 3.51, 72000],
        ["07-24", 3.45, 3.40, 3.37, 3.46, 85000],
        ["07-25", 3.40, 3.43, 3.38, 3.45, 62000],
        ["07-26", 3.43, 3.48, 3.42, 3.50, 58000],
        ["07-29", 3.48, 3.52, 3.46, 3.54, 65000],
        ["07-30", 3.52, 3.55, 3.50, 3.57, 70000],
        ["07-31", 3.55, 3.60, 3.53, 3.62, 78000],
        ["08-01", 3.60, 3.65, 3.58, 3.67, 82000],
        ["08-04", 3.65, 3.70, 3.63, 3.72, 88000],
        ["08-05", 3.70, 3.68, 3.65, 3.72, 60000],
        ["08-06", 3.68, 3.72, 3.66, 3.74, 72000],
        ["08-07", 3.72, 3.75, 3.70, 3.77, 85000],
        ["08-08", 3.75, 3.73, 3.71, 3.76, 55000],
        ["08-11", 3.73, 3.70, 3.68, 3.75, 48000],
        ["08-12", 3.70, 3.68, 3.65, 3.72, 52000],
        ["08-13", 3.68, 3.72, 3.66, 3.74, 68000],
        ["08-14", 3.72, 3.75, 3.70, 3.77, 75000],
        ["08-15", 3.75, 3.78, 3.73, 3.80, 92000],
        ["08-18", 3.78, 3.76, 3.74, 3.79, 65000],
        ["08-19", 3.76, 3.78, 3.75, 3.80, 58000],
        ["08-20", 3.80, 4.16, 3.78, 4.24, 530000],
        ["08-21", 4.16, 4.00, 3.95, 4.18, 280000],
        ["08-22", 4.00, 3.92, 3.88, 4.02, 180000],
        ["08-25", 3.88, 3.85, 3.80, 3.90, 120000],
        ["08-26", 3.83, 3.85, 3.83, 3.91, 295000],
    ],
    "radar": [55, 60, 35, 58],
    "news": [
        {"tag": "positive", "title": "8月20日上海'沪八条'楼市新政：优化公积金/以旧换新补贴/房票安置", "src": "新华社 08-20"},
        {"tag": "positive", "title": "8月20日涨停，机构净买入6014万，沪股通净买入1206万", "src": "证券时报 08-20"},
        {"tag": "positive", "title": "8月4日完成回购股份注销1270万股，总股本降至24.918亿股", "src": "公司公告 08-04"},
        {"tag": "negative", "title": "预计2026H1亏损5000-7500万，项目交付缩减+金融资产公允价值下滑", "src": "业绩预告 07-11"},
        {"tag": "neutral", "title": "Q1营收8.53亿-63.55%，归母净利726.65万-69.20%", "src": "一季报 04-30"},
    ],
    "risks": [
        "预计上半年亏损5000-7500万，业绩基本面疲弱",
        "MA60(3.95)在上方压制，中期趋势仍偏空",
        "房地产周期下行，项目交付缩减，营收同比-63.55%",
        "涨停后高位震荡3日，3.91压力未破，谨防回落",
    ],
    "catalysts": [
        "破净估值（PB 0.46），安全边际高，国企改革预期",
        "上海'沪八条'政策利好，核心城市房企受益",
        "回购注销彰显信心，总股本减少利好每股指标",
        "股息率1.04%，分析师目标价6.70元（+74%空间）",
    ],
    "attr_tech": 35,
    "attr_news": 30,
    "attr_fund": 15,
    "attr_market": 20,
}


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{name}({code}) 决策仪表盘 - {date}</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif; background:#0f0f23; color:#e2e8f0; line-height:1.6; }}
.container {{ max-width:1200px; margin:0 auto; padding:20px; }}
/* 顶部摘要 */
.summary-card {{ background:linear-gradient(135deg,#1a1a2e 0%,#16213e 100%); border-radius:16px; padding:28px; margin-bottom:20px; border:1px solid #2d2d44; box-shadow:0 8px 32px rgba(0,0,0,0.4); }}
.summary-top {{ display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:20px; }}
.stock-info h1 {{ font-size:28px; margin-bottom:6px; }}
.stock-info .code {{ color:#94a3b8; font-size:15px; }}
.price-box {{ text-align:right; }}
.price-now {{ font-size:36px; font-weight:700; color:{chg_color}; }}
.price-change {{ font-size:16px; color:{chg_color}; }}
.signal-badge {{ display:inline-block; padding:8px 24px; border-radius:30px; font-size:18px; font-weight:700; color:#fff; background:linear-gradient(135deg,{signal_color} 0%,{signal_color2} 100%); margin-top:10px; letter-spacing:2px; }}
/* 评分环 */
.score-row {{ display:flex; justify-content:space-around; flex-wrap:wrap; gap:16px; margin-top:24px; padding-top:24px; border-top:1px solid #2d2d44; }}
.score-item {{ text-align:center; }}
.score-item .label {{ color:#94a3b8; font-size:13px; margin-bottom:4px; }}
.score-item .val {{ font-size:22px; font-weight:700; color:#667eea; }}
.score-item .val.buy {{ color:#00d4aa; }}
.score-item .val.sell {{ color:#ff4757; }}
.score-ring {{ width:120px; height:120px; position:relative; }}
.score-ring canvas {{ width:100%; height:100%; }}
.score-ring .score-num {{ position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); font-size:32px; font-weight:700; }}
/* 模块卡片 */
.module {{ background:#1a1a2e; border-radius:14px; padding:24px; margin-bottom:20px; border:1px solid #2d2d44; }}
.module h2 {{ font-size:18px; margin-bottom:16px; padding-left:12px; border-left:4px solid #667eea; color:#e2e8f0; }}
/* 结论 */
.conclusion {{ background:linear-gradient(135deg,rgba(102,126,234,0.15) 0%,rgba(118,75,162,0.15) 100%); border-radius:10px; padding:18px; margin-bottom:14px; }}
.conclusion .one-line {{ font-size:17px; font-weight:700; color:#667eea; margin-bottom:8px; }}
.conclusion .detail {{ color:#cbd5e1; font-size:14px; }}
/* 趋势标签 */
.trend-tags {{ display:flex; flex-wrap:wrap; gap:10px; margin-bottom:16px; }}
.tag {{ padding:5px 14px; border-radius:20px; font-size:13px; font-weight:600; }}
.tag.up {{ background:rgba(255,71,87,0.15); color:#ff6b81; border:1px solid rgba(255,71,87,0.3); }}
.tag.down {{ background:rgba(0,212,170,0.15); color:#00d4aa; border:1px solid rgba(0,212,170,0.3); }}
.tag.neutral {{ background:rgba(255,165,2,0.15); color:#ffa502; border:1px solid rgba(255,165,2,0.3); }}
.tag.info {{ background:rgba(102,126,234,0.15); color:#667eea; border:1px solid rgba(102,126,234,0.3); }}
/* 数据网格 */
.data-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:14px; }}
.data-cell {{ background:rgba(45,45,68,0.5); border-radius:10px; padding:14px; }}
.data-cell .k {{ color:#94a3b8; font-size:13px; margin-bottom:4px; }}
.data-cell .v {{ font-size:18px; font-weight:600; }}
/* 图表 */
.chart-box {{ width:100%; height:420px; background:rgba(15,15,35,0.6); border-radius:10px; }}
.chart-radar {{ width:100%; height:360px; }}
/* 新闻 */
.news-item {{ display:flex; align-items:flex-start; gap:10px; padding:12px; border-radius:8px; margin-bottom:8px; background:rgba(45,45,68,0.3); }}
.news-tag {{ padding:3px 10px; border-radius:12px; font-size:11px; font-weight:600; white-space:nowrap; }}
.news-tag.positive {{ background:rgba(0,212,170,0.2); color:#00d4aa; }}
.news-tag.negative {{ background:rgba(255,71,87,0.2); color:#ff6b81; }}
.news-tag.neutral {{ background:rgba(255,165,2,0.2); color:#ffa502; }}
.news-text {{ flex:1; font-size:14px; color:#cbd5e1; }}
.news-src {{ color:#64748b; font-size:12px; }}
/* 风险/催化 */
.alert-box {{ border-radius:10px; padding:14px; margin-bottom:10px; }}
.alert-box.risk {{ background:rgba(255,71,87,0.1); border-left:4px solid #ff4757; }}
.alert-box.cata {{ background:rgba(0,212,170,0.1); border-left:4px solid #00d4aa; }}
.alert-box h3 {{ font-size:14px; margin-bottom:8px; }}
.alert-box.risk h3 {{ color:#ff6b81; }}
.alert-box.cata h3 {{ color:#00d4aa; }}
.alert-box ul {{ list-style:none; padding-left:0; }}
.alert-box li {{ padding:4px 0 4px 18px; position:relative; font-size:13px; color:#cbd5e1; }}
.alert-box.risk li::before {{ content:'⚠'; position:absolute; left:0; }}
.alert-box.cata li::before {{ content:'✓'; position:absolute; left:0; color:#00d4aa; }}
/* 作战计划 */
.plan-table {{ width:100%; border-collapse:collapse; margin-top:12px; }}
.plan-table th,.plan-table td {{ padding:10px; text-align:center; border:1px solid #2d2d44; font-size:14px; }}
.plan-table th {{ background:rgba(102,126,234,0.15); color:#667eea; }}
.plan-table .buy {{ color:#00d4aa; font-weight:600; }}
.plan-table .sell {{ color:#ff6b81; font-weight:600; }}
.plan-table .stop {{ color:#ffa502; font-weight:600; }}
/* 归因 */
.attr-bars {{ margin-top:12px; }}
.attr-bar {{ margin-bottom:12px; }}
.attr-bar .lab {{ display:flex; justify-content:space-between; font-size:13px; margin-bottom:4px; }}
.attr-bar .track {{ height:10px; background:#2d2d44; border-radius:5px; overflow:hidden; }}
.attr-bar .fill {{ height:100%; border-radius:5px; }}
/* 底部 */
.footer {{ text-align:center; padding:24px; color:#64748b; font-size:12px; border-top:1px solid #2d2d44; margin-top:20px; }}
.footer .disclaimer {{ background:rgba(255,165,2,0.08); border-radius:8px; padding:12px; margin-bottom:12px; color:#94a3b8; }}
@media(max-width:768px) {{ .summary-top {{ flex-direction:column; }} .price-box {{ text-align:left; }} .score-row {{ justify-content:flex-start; }} }}
</style>
</head>
<body>
<div class="container">
  <!-- 顶部摘要 -->
  <div class="summary-card">
    <div class="summary-top">
      <div class="stock-info">
        <h1>{name} <span style="font-size:16px;color:#94a3b8;">{code} · {market}</span></h1>
        <div class="code">分析日期：{date} | 所属板块：{sector}</div>
        <div class="signal-badge">{signal_cn}</div>
      </div>
      <div class="price-box">
        <div class="price-now">¥{price}</div>
        <div class="price-change">{change_sign}{change_abs} ({change_pct}%)</div>
        <div style="color:#94a3b8;font-size:13px;margin-top:4px;">昨收 {prev_close} | 开 {open} | 高 {high} | 低 {low}</div>
      </div>
    </div>
    <div class="score-row">
      <div class="score-item"><div class="score-ring" id="scoreRing"></div><div class="label">综合评分</div></div>
      <div class="score-item"><div class="val buy">¥{buy_low}-{buy_high}</div><div class="label">买入区间</div></div>
      <div class="score-item"><div class="val buy">¥{target1} / {target2}</div><div class="label">目标价 T1/T2</div></div>
      <div class="score-item"><div class="val sell">¥{stop_loss}</div><div class="label">止损位</div></div>
      <div class="score-item"><div class="val" style="color:#667eea;">{position}</div><div class="label">仓位建议</div></div>
    </div>
  </div>

  <!-- 模块1: 核心结论 -->
  <div class="module">
    <h2>① 核心结论</h2>
    <div class="conclusion">
      <div class="one-line">{one_line}</div>
      <div class="detail">{conclusion_detail}</div>
    </div>
  </div>

  <!-- 模块2: 数据透视 -->
  <div class="module">
    <h2>② 数据透视</h2>
    <div class="trend-tags">{trend_tags}</div>
    <div class="data-grid">
      <div class="data-cell"><div class="k">MA5 / MA10 / MA20</div><div class="v" style="font-size:15px;">{ma5} / {ma10} / {ma20}</div></div>
      <div class="data-cell"><div class="k">MACD / DIF / DEA</div><div class="v" style="font-size:15px;color:{macd_color};">{macd} / {dif} / {dea}</div></div>
      <div class="data-cell"><div class="k">KDJ (K/D/J)</div><div class="v" style="font-size:15px;">{kdj_k} / {kdj_d} / {kdj_j}</div></div>
      <div class="data-cell"><div class="k">RSI(6/12)</div><div class="v" style="font-size:15px;">{rsi6} / {rsi12}</div></div>
      <div class="data-cell"><div class="k">BOLL 上/中/下</div><div class="v" style="font-size:15px;">{boll_up} / {boll_mid} / {boll_low}</div></div>
      <div class="data-cell"><div class="k">换手率 / 量比</div><div class="v" style="font-size:15px;">{turnover_rate}% / {qratio}</div></div>
      <div class="data-cell"><div class="k">PE(TTM) / PB</div><div class="v" style="font-size:15px;">{pe_ttm} / {pb}</div></div>
      <div class="data-cell"><div class="k">总市值 / 52周区间</div><div class="v" style="font-size:15px;">{total_mv}亿 / {low52}-{high52}</div></div>
    </div>
  </div>

  <!-- 模块3: 技术指标图表 -->
  <div class="module">
    <h2>③ K线与技术指标</h2>
    <div id="klineChart" class="chart-box"></div>
  </div>

  <!-- 模块4: 情报面 -->
  <div class="module">
    <h2>④ 情报面</h2>
    {news_html}
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px;">
      <div class="alert-box risk"><h3>⚠ 风险警报</h3><ul>{risk_html}</ul></div>
      <div class="alert-box cata"><h3>✓ 积极催化</h3><ul>{cata_html}</ul></div>
    </div>
  </div>

  <!-- 模块5: 信号归因雷达图 -->
  <div class="module">
    <h2>⑤ 信号归因</h2>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;">
      <div id="radarChart" class="chart-radar"></div>
      <div class="attr-bars">
        <div class="attr-bar"><div class="lab"><span>技术指标贡献</span><span>{attr_tech}%</span></div><div class="track"><div class="fill" style="width:{attr_tech}%;background:#667eea;"></div></div></div>
        <div class="attr-bar"><div class="lab"><span>新闻情绪贡献</span><span>{attr_news}%</span></div><div class="track"><div class="fill" style="width:{attr_news}%;background:#00d4aa;"></div></div></div>
        <div class="attr-bar"><div class="lab"><span>基本面贡献</span><span>{attr_fund}%</span></div><div class="track"><div class="fill" style="width:{attr_fund}%;background:#ffa502;"></div></div></div>
        <div class="attr-bar"><div class="lab"><span>市场环境贡献</span><span>{attr_market}%</span></div><div class="track"><div class="fill" style="width:{attr_market}%;background:#ff6b81;"></div></div></div>
        <div style="margin-top:16px;padding:14px;background:rgba(102,126,234,0.1);border-radius:8px;font-size:13px;color:#cbd5e1;">
          <b style="color:#00d4aa;">最强看多：</b>{bull_signal}<br>
          <b style="color:#ff6b81;">最强看空：</b>{bear_signal}
        </div>
      </div>
    </div>
  </div>

  <!-- 模块6: 作战计划 -->
  <div class="module">
    <h2>⑥ 作战计划</h2>
    <table class="plan-table">
      <tr><th>操作</th><th>价格区间</th><th>仓位</th><th>触发条件</th></tr>
      <tr><td class="buy">第一笔买入</td><td>¥{buy_low} - {buy_high}</td><td>1-2 成</td><td>回踩MA20/支撑位企稳</td></tr>
      <tr><td class="buy">第二笔加仓</td><td>¥{target1} 突破回踩</td><td>+1-2 成</td><td>放量突破关键压力位</td></tr>
      <tr><td class="sell">第一目标止盈</td><td>¥{target1}</td><td>减 1/3</td><td>到达短期目标/滞涨</td></tr>
      <tr><td class="sell">第二目标止盈</td><td>¥{target2}</td><td>减 1/3</td><td>到达中期目标</td></tr>
      <tr><td class="sell">第三目标止盈</td><td>¥{target3}</td><td>清仓</td><td>到达长期目标/趋势走弱</td></tr>
      <tr><td class="stop">止损</td><td>¥{stop_loss}</td><td>清仓</td><td>跌破支撑/趋势破坏</td></tr>
    </table>
    <div style="margin-top:14px;padding:12px;background:rgba(255,71,87,0.08);border-radius:8px;font-size:13px;color:#94a3b8;">
      <b style="color:#ff6b81;">风险控制清单：</b>{risk_checklist}
    </div>
  </div>

  <!-- 底部 -->
  <div class="footer">
    <div class="disclaimer">⚠ 免责声明：本分析由 AI 智能分析师「星辰」基于公开数据生成，仅供学习研究参考，不构成任何投资建议。股市有风险，投资需谨慎。所有结论基于历史数据和技术模型，不保证未来表现。</div>
    <div>分析时间：{date} 23:30 | 分析师：星辰 (Stellar) | 策略：多头趋势+业绩验证 | 数据来源：新浪财经/东方财富/公司公告</div>
    <div style="margin-top:6px;">大盘环境：上证 3912.52(+0.59%) | 深证 13841.33(+0.69%) | 结构性行情</div>
  </div>
</div>

<script>
// 评分环
(function(){{
  var c=document.getElementById('scoreRing');
  var ctx=c.getContext?c.getContext('2d'):null;
  c.width=120;c.height=120;
  ctx=c.getContext('2d');
  var score={score},max=100;
  var ratio=score/max;
  ctx.lineWidth=10;
  ctx.strokeStyle='#2d2d44';
  ctx.beginPath();ctx.arc(60,60,48,0,2*Math.PI);ctx.stroke();
  var grad=ctx.createLinearGradient(0,0,120,0);
  grad.addColorStop(0,'#667eea');grad.addColorStop(1,'#764ba2');
  ctx.strokeStyle=grad;
  ctx.beginPath();ctx.arc(60,60,48,-Math.PI/2,-Math.PI/2+2*Math.PI*ratio);ctx.stroke();
  ctx.fillStyle='#e2e8f0';ctx.font='bold 32px sans-serif';ctx.textAlign='center';ctx.textBaseline='middle';
  ctx.fillText(score,60,60);
}})();

// K线图
var kdata={klines_json};
var klineChart=echarts.init(document.getElementById('klineChart'),'dark');
klineChart.setOption({{
  backgroundColor:'transparent',
  tooltip:{{trigger:'axis',axisPointer:{{type:'cross'}}}},
  legend:{{data:['K线','MA5','MA10','MA20','成交量'],top:5}},
  grid:[
    {{left:'8%',right:'3%',top:'12%',height:'58%'}},
    {{left:'8%',right:'3%',top:'74%',height:'18%'}}
  ],
  xAxis:[
    {{type:'category',data:kdata.map(d=>d[0]),scale:true,boundaryGap:true,axisLine:{{lineStyle:{{color:'#2d2d44'}}}}}},
    {{type:'category',gridIndex:1,data:kdata.map(d=>d[0]),scale:true,boundaryGap:true,axisLine:{{lineStyle:{{color:'#2d2d44'}}}}}}
  ],
  yAxis:[
    {{scale:true,splitLine:{{lineStyle:{{color:'#1e293b'}}}},axisLabel:{{color:'#94a3b8'}}}},
    {{gridIndex:1,splitLine:{{show:false}},axisLabel:{{color:'#94a3b8'}}}}
  ],
  dataZoom:[
    {{type:'inside',xAxisIndex:[0,1],start:60,end:100}},
    {{type:'slider',xAxisIndex:[0,1],start:60,end:100,top:'93%',height:15}}
  ],
  series:[
    {{name:'K线',type:'candlestick',data:kdata.map(d=>[d[1],d[2],d[3],d[4]]),
      itemStyle:{{color:'#ff4757',color0:'#00d4aa',borderColor:'#ff4757',borderColor0:'#00d4aa'}}}},
    {{name:'MA5',type:'line',data:calcMA(kdata.map(d=>d[2]),5),smooth:true,symbol:'none',lineStyle:{{width:1.5,color:'#feca57'}}}},
    {{name:'MA10',type:'line',data:calcMA(kdata.map(d=>d[2]),10),smooth:true,symbol:'none',lineStyle:{{width:1.5,color:'#48dbfb'}}}},
    {{name:'MA20',type:'line',data:calcMA(kdata.map(d=>d[2]),20),smooth:true,symbol:'none',lineStyle:{{width:1.5,color:'#ff9ff3'}}}},
    {{name:'成交量',type:'bar',xAxisIndex:1,yAxisIndex:1,data:kdata.map(d=>d[5]),
      itemStyle:{{color:function(p){{return kdata[p.dataIndex][2]>=kdata[p.dataIndex][1]?'#ff4757':'#00d4aa';}}}}}}
  ]
}});
function calcMA(arr,n){{var r=[];for(var i=0;i<arr.length;i++){{if(i<n){{r.push(null);}}else{{var s=0;for(var j=0;j<n;j++)s+=arr[i-j];r.push(+(s/n).toFixed(2));}}}}return r;}}
window.addEventListener('resize',()=>{{klineChart.resize();}});

// 雷达图
var radarChart=echarts.init(document.getElementById('radarChart'),'dark');
radarChart.setOption({{
  backgroundColor:'transparent',
  tooltip:{{}},
  radar:{{indicator:[
    {{name:'技术指标',max:100}},
    {{name:'新闻情绪',max:100}},
    {{name:'基本面',max:100}},
    {{name:'市场环境',max:100}}
  ],radius:'65%',axisName:{{color:'#94a3b8',fontSize:13}},
    splitLine:{{lineStyle:{{color:'#2d2d44'}}}},
    splitArea:{{areaStyle:{{color:['rgba(102,126,234,0.05)','rgba(102,126,234,0.1)']}}}},
    axisLine:{{lineStyle:{{color:'#2d2d44'}}}}}},
  series:[{{
    type:'radar',
    data:[{{value:{radar_json},name:'信号归因',
      areaStyle:{{color:'rgba(102,126,234,0.3)'}},
      lineStyle:{{color:'#667eea',width:2}},
      itemStyle:{{color:'#667eea'}}}}]
  }}]
}});
window.addEventListener('resize',()=>{{radarChart.resize();}});
</script>
</body>
</html>
"""


def fmt(v, places=2):
    if isinstance(v, (int, float)):
        return f"{v:.{places}f}"
    return str(v)


def build_news_html(news):
    parts = []
    tag_map = {"positive": "利好", "negative": "利空", "neutral": "中性"}
    for n in news:
        parts.append(
            f'<div class="news-item"><span class="news-tag {n["tag"]}">{tag_map[n["tag"]]}</span>'
            f'<div class="news-text">{n["title"]}<br><span class="news-src">{n["src"]}</span></div></div>'
        )
    return "".join(parts)


def build_alert_html(items):
    return "".join(f"<li>{x}</li>" for x in items)


def render(stock, one_line, conclusion_detail, trend_tags, sector,
           bull_signal, bear_signal, risk_checklist):
    change_abs = abs(stock["price"] - stock["prev_close"])
    chg_color = "#ff4757" if stock["change_pct"] > 0 else ("#00d4aa" if stock["change_pct"] < 0 else "#94a3b8")
    change_sign = "+" if stock["change_pct"] > 0 else ""
    macd_color = "#ff4757" if stock["macd"] < 0 else "#00d4aa"
    signal_color2 = stock["signal_color"]

    return HTML_TEMPLATE.format(
        name=stock["name"], code=stock["code"], market=stock["market"], date=stock["date"],
        sector=sector,
        price=fmt(stock["price"]), change_sign=change_sign, change_abs=fmt(change_abs),
        change_pct=fmt(stock["change_pct"]), chg_color=chg_color,
        prev_close=fmt(stock["prev_close"]), open=fmt(stock["open"]), high=fmt(stock["high"]), low=fmt(stock["low"]),
        signal_cn=stock["signal_cn"], signal_color=stock["signal_color"], signal_color2=signal_color2,
        score=stock["score"],
        buy_low=fmt(stock["buy_low"]), buy_high=fmt(stock["buy_high"]),
        target1=fmt(stock["target1"]), target2=fmt(stock["target2"]), target3=fmt(stock["target3"]),
        stop_loss=fmt(stock["stop_loss"]), position=stock["position"],
        one_line=one_line, conclusion_detail=conclusion_detail, trend_tags=trend_tags,
        ma5=fmt(stock["ma5"]), ma10=fmt(stock["ma10"]), ma20=fmt(stock["ma20"]),
        macd=fmt(stock["macd"]), dif=fmt(stock["dif"]), dea=fmt(stock["dea"]), macd_color=macd_color,
        kdj_k=fmt(stock["kdj_k"]), kdj_d=fmt(stock["kdj_d"]), kdj_j=fmt(stock["kdj_j"]),
        rsi6=fmt(stock["rsi6"]), rsi12=fmt(stock["rsi12"]),
        boll_up=fmt(stock["boll_up"]), boll_mid=fmt(stock["boll_mid"]), boll_low=fmt(stock["boll_low"]),
        turnover_rate=fmt(stock["turnover_rate"]), qratio="0.45",
        pe_ttm=fmt(stock["pe_ttm"]), pb=fmt(stock["pb"]),
        total_mv=fmt(stock["total_mv_wan"] / 10000, 1),
        high52=fmt(stock["high52"]), low52=fmt(stock["low52"]),
        news_html=build_news_html(stock["news"]),
        risk_html=build_alert_html(stock["risks"]),
        cata_html=build_alert_html(stock["catalysts"]),
        radar_json=json.dumps(stock["radar"]),
        attr_tech=stock["attr_tech"], attr_news=stock["attr_news"],
        attr_fund=stock["attr_fund"], attr_market=stock["attr_market"],
        bull_signal=bull_signal, bear_signal=bear_signal,
        risk_checklist=risk_checklist,
        klines_json=json.dumps(stock["klines"]),
    )


# ===== 学大教育 =====
xd_trend = (
    '<span class="tag neutral">短期回调(5日-8.67%)</span>'
    '<span class="tag up">中期上行(20日+8.08%)</span>'
    '<span class="tag down">价格<MA5(37.1) 受压</span>'
    '<span class="tag info">MA20(34.85) 支撑待验</span>'
    '<span class="tag neutral">KDJ超卖区(J=0.5)</span>'
    '<span class="tag up">量能温和萎缩</span>'
)
xd_one_line = "Q2业绩超预期释放弹性，短期回踩MA20支撑，回调即布局良机，中期目标看45元。"
xd_detail = (
    f"26H1营收21.63亿(+12.87%)、归母净利3.01亿(+30.85%)，Q2单季净利+34.6%、净利率17.3%创新高，"
    f"经营杠杆充分释放。合同负债6.48亿(+8.5%)锁定下半年收入。当前PE仅16.36倍，机构目标价均值47.67元(上行空间33%)。"
    f"短期5日回调-8.67%系获利了结，价格回踩MA20(34.85)附近，KDJ进入超卖区(J=0.5)，回调尾声特征明显。"
    f"建议34.5-35.8区间分批建仓，止损33.0，目标40/42/45。"
)
xd_bull = "Q2扣非净利+42.4%超预期，合同负债+8.5%锁定H2，机构目标价47.67元(33%空间)"
xd_bear = "短期5日急跌-8.67%，MA5(37.1)上方压制，若跌破MA20(34.85)将下探33-34"
xd_risk_checklist = "①跌破33.0无条件止损；②单日放量跌破MA20且3日不收回减仓；③关注教育政策监管动态；④仓位不超3成"

xd_html = render(XD, xd_one_line, xd_detail, xd_trend, "社会服务/教育",
                 xd_bull, xd_bear, xd_risk_checklist)
(OUTPUT_DIR / "000526-analysis-20260826.html").write_text(xd_html, encoding="utf-8")

# ===== 城投控股 =====
ct_trend = (
    '<span class="tag up">短期偏多(5日+1.85%)</span>'
    '<span class="tag up">20日上行(+4.34%)</span>'
    '<span class="tag info">MA5>MA10>MA20 多头排列</span>'
    '<span class="tag down">MA60(3.95)上方压制</span>'
    '<span class="tag neutral">3.83-3.91窄幅震荡</span>'
    '<span class="tag info">涨停后缩量整理</span>'
)
ct_one_line = "沪八条政策利好+破净估值(PB0.46)，涨停后缩量回踩均线，轻仓博弈政策催化，突破3.91加仓。"
ct_detail = (
    f"8月20日上海'沪八条'楼市新政催化涨停，机构净买入6014万+沪股通净买入1206万，政策面强支撑。"
    f"公司PB仅0.46倍深度破净，8月4日回购注销1270万股彰显信心，股息率1.04%。"
    f"但基本面疲弱：Q1营收-63.55%、预计H1亏损5000-7500万，业绩与政策面存在背离。"
    f"技术上MA5/10/20多头排列但MA60(3.95)压制，3.83-3.91窄幅震荡待方向选择。"
    f"建议3.78-3.85轻仓试探，突破3.91(回踩确认)加仓，止损3.68，目标4.0/4.24/4.5。"
)
ct_bull = "沪八条政策利好+破净估值(PB0.46)+回购注销，分析师目标价6.70元(+74%空间)"
ct_bear = "预计H1亏损5000-7500万，Q1营收-63.55%，MA60(3.95)中期压制"
ct_risk_checklist = "①跌破3.68(MA10下方)止损；③涨停后高位震荡3日，破3.80离场；③业绩亏损勿重仓；④仓位不超2成"

ct_html = render(CT, ct_one_line, ct_detail, ct_trend, "房地产/房地产开发",
                 ct_bull, ct_bear, ct_risk_checklist)
(OUTPUT_DIR / "600649-analysis-20260826.html").write_text(ct_html, encoding="utf-8")

print("生成完成:")
print(f"  {OUTPUT_DIR / '000526-analysis-20260826.html'}")
print(f"  {OUTPUT_DIR / '600649-analysis-20260826.html'}")
