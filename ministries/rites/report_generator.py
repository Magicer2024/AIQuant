"""
ministries/rites/report_generator.py —— 礼部报表生成器

职责：
  1. 生成各类量化交易报告（HTML格式）
  2. 持仓报告、交易报告、风控报告、综合日报
  3. 报告模板渲染
"""

from datetime import datetime


class ReportGenerator:
    """报告生成器"""

    THEME = {
        "bg": "#0f172a",
        "card": "#1e293b",
        "border": "#334155",
        "text": "#e2e8f0",
        "muted": "#94a3b8",
        "up": "#ef4444",
        "down": "#22c55e",
        "accent": "#3b82f6",
    }

    @classmethod
    def generate_position_report(cls, account: dict, positions: list) -> str:
        """生成持仓报告"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        total_pnl = sum(p.get("unrealized_pnl", 0) for p in positions)
        total_pnl_pct = (total_pnl / (account.get("initial_capital", 1) or 1)) * 100

        rows = ""
        for p in positions:
            pnl_cls = "up" if p.get("unrealized_pnl", 0) >= 0 else "down"
            p_name = p.get('name', p.get('code', ''))
            rows += f"""
            <tr>
                <td><a href="javascript:void(0)" class="stock-link" onclick="showKline('{p.get('code', '')}', '{p_name.replace(chr(39), chr(92)+chr(39))}')">{p.get('code', '')}</a><br/><small style="color:var(--muted)">{p.get('name', '')}</small></td>
                <td>{p.get('shares', 0)}</td>
                <td>{p.get('entry_price', 0):.2f}</td>
                <td>{p.get('current_price', 0):.2f}</td>
                <td>{p.get('market_value', 0):,.2f}</td>
                <td class="{pnl_cls}">{p.get('unrealized_pnl', 0):+.2f}</td>
                <td class="{pnl_cls}">{p.get('unrealized_pnl_pct', 0):+.2f}%</td>
            </tr>
            """

        return cls._wrap_html(f"""
        <h1>持仓报告</h1>
        <p style="color:var(--muted)">生成时间: {now}</p>

        <div class="summary">
            <div class="stat">
                <div class="stat-label">总资产</div>
                <div class="stat-value">¥{account.get('total_assets', 0):,.2f}</div>
            </div>
            <div class="stat">
                <div class="stat-label">现金</div>
                <div class="stat-value">¥{account.get('cash', 0):,.2f}</div>
            </div>
            <div class="stat">
                <div class="stat-label">持仓市值</div>
                <div class="stat-value">¥{account.get('market_value', 0):,.2f}</div>
            </div>
            <div class="stat">
                <div class="stat-label">总收益</div>
                <div class="stat-value" style="color:{'var(--up)' if total_pnl >= 0 else 'var(--down)'}">{total_pnl:+.2f}</div>
            </div>
        </div>

        <h2>持仓明细</h2>
        <table>
            <thead>
                <tr>
                    <th>股票</th><th>数量</th><th>成本价</th><th>现价</th><th>市值</th><th>盈亏</th><th>盈亏%</th>
                </tr>
            </thead>
            <tbody>{rows or '<tr><td colspan="7" style="text-align:center;color:var(--muted)">暂无持仓</td></tr>'}</tbody>
        </table>
        """)

    @classmethod
    def generate_trade_report(cls, orders: list) -> str:
        """生成交易报告"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        rows = ""
        for o in orders:
            status_color = {
                "filled": "#22c55e",
                "pending": "#f59e0b",
                "cancelled": "#6b7280",
                "rejected": "#ef4444",
            }.get(o.get("status"), "var(--text)")
            rows += f"""
            <tr>
                <td>{o.get('order_id', '')}</td>
                <td><a href="javascript:void(0)" class="stock-link" onclick="showKline('{o.get('code', '')}', '{o.get('code', '')}')">{o.get('code', '')}</a></td>
                <td>{'买入' if o.get('direction') == 'buy' else '卖出'}</td>
                <td>{o.get('shares', 0)}</td>
                <td>{o.get('price', 0):.2f}</td>
                <td style="color:{status_color}">{o.get('status', '')}</td>
                <td>{o.get('reason', '')}</td>
                <td>{o.get('created_at', '')[:16]}</td>
            </tr>
            """

        return cls._wrap_html(f"""
        <h1>交易报告</h1>
        <p style="color:var(--muted)">生成时间: {now} | 共 {len(orders)} 笔订单</p>

        <table>
            <thead>
                <tr>
                    <th>订单号</th><th>代码</th><th>方向</th><th>数量</th><th>价格</th><th>状态</th><th>原因</th><th>时间</th>
                </tr>
            </thead>
            <tbody>{rows or '<tr><td colspan="8" style="text-align:center;color:var(--muted)">暂无订单</td></tr>'}</tbody>
        </table>
        """)

    @classmethod
    def generate_risk_report(cls, risk_status: dict, events: list) -> str:
        """生成风控报告"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        rows = ""
        for e in events:
            level_color = {"warning": "#f59e0b", "restrict": "#f97316", "block": "#ef4444"}.get(
                e.get("level", "").lower(), "var(--text)"
            )
            rows += f"""
            <tr>
                <td style="color:{level_color}">{e.get('level', '')}</td>
                <td>{e.get('rule', '')}</td>
                <td>{e.get('message', '')}</td>
                <td>{e.get('created_at', '')[:16]}</td>
            </tr>
            """

        return cls._wrap_html(f"""
        <h1>风控报告</h1>
        <p style="color:var(--muted)">生成时间: {now}</p>

        <div class="summary">
            <div class="stat">
                <div class="stat-label">整体等级</div>
                <div class="stat-value">{risk_status.get('overall_level', 'NORMAL')}</div>
            </div>
            <div class="stat">
                <div class="stat-label">拦截次数</div>
                <div class="stat-value">{risk_status.get('block_count', 0)}</div>
            </div>
            <div class="stat">
                <div class="stat-label">限制次数</div>
                <div class="stat-value">{risk_status.get('restrict_count', 0)}</div>
            </div>
            <div class="stat">
                <div class="stat-label">告警次数</div>
                <div class="stat-value">{risk_status.get('warning_count', 0)}</div>
            </div>
        </div>

        <h2>近期风控事件</h2>
        <table>
            <thead>
                <tr><th>等级</th><th>规则</th><th>信息</th><th>时间</th></tr>
            </thead>
            <tbody>{rows or '<tr><td colspan="4" style="text-align:center;color:var(--muted)">暂无事件</td></tr>'}</tbody>
        </table>
        """)

    @classmethod
    def generate_daily_report(cls, data: dict) -> str:
        """生成综合日报"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        account = data.get("account", {})
        positions = data.get("positions", [])
        orders = data.get("orders", [])
        risk = data.get("risk", {})

        return cls._wrap_html(f"""
        <h1>AIQuant 每日报告</h1>
        <p style="color:var(--muted)">生成时间: {now}</p>

        <div class="summary">
            <div class="stat">
                <div class="stat-label">总资产</div>
                <div class="stat-value">¥{account.get('total_assets', 0):,.2f}</div>
            </div>
            <div class="stat">
                <div class="stat-label">总收益</div>
                <div class="stat-value" style="color:{'var(--up)' if account.get('total_return', 0) >= 0 else 'var(--down)'}">
                    {account.get('total_return', 0):+.2f}%
                </div>
            </div>
            <div class="stat">
                <div class="stat-label">持仓数</div>
                <div class="stat-value">{len(positions)}</div>
            </div>
            <div class="stat">
                <div class="stat-label">今日订单</div>
                <div class="stat-value">{len(orders)}</div>
            </div>
        </div>

        <h2>系统状态</h2>
        <p>风控等级: <strong>{risk.get('overall_level', 'NORMAL')}</strong></p>
        <p>数据源: <strong>{data.get('data_source', 'local')}</strong></p>
        <p>策略模式: <strong>{data.get('strategy_mode', 'default')}</strong></p>

        <div style="margin-top:2rem;padding-top:1rem;border-top:1px solid var(--border);color:var(--muted);font-size:0.75rem;text-align:center;">
            AIQuant 量化投资系统 | 本报告仅供参考，不构成投资建议
        </div>
        """)

    @classmethod
    def _wrap_html(cls, content: str) -> str:
        """包装HTML报告"""
        t = cls.THEME
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>AIQuant Report</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
:root {{
    --bg: {t['bg']};
    --card: {t['card']};
    --border: {t['border']};
    --text: {t['text']};
    --muted: {t['muted']};
    --up: {t['up']};
    --down: {t['down']};
    --accent: {t['accent']};
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    padding: 2rem;
    line-height: 1.6;
}}
.container {{ max-width: 1200px; margin: 0 auto; }}
h1 {{ font-size: 1.5rem; margin-bottom: 0.5rem; }}
h2 {{ font-size: 1.125rem; margin: 1.5rem 0 0.75rem; color: var(--accent); }}
.summary {{
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 1rem;
    margin: 1.5rem 0;
}}
.stat {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 0.5rem;
    padding: 1rem;
    text-align: center;
}}
.stat-label {{ font-size: 0.75rem; color: var(--muted); text-transform: uppercase; }}
.stat-value {{ font-size: 1.25rem; font-weight: 600; margin-top: 0.25rem; }}
table {{
    width: 100%;
    border-collapse: collapse;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 0.5rem;
    overflow: hidden;
    margin-top: 0.75rem;
}}
th, td {{ padding: 0.75rem 1rem; text-align: left; font-size: 0.875rem; border-bottom: 1px solid var(--border); }}
th {{ background: rgba(255,255,255,0.03); color: var(--muted); font-weight: 500; font-size: 0.75rem; text-transform: uppercase; }}
tr:hover {{ background: rgba(255,255,255,0.02); }}
.up {{ color: var(--up); }}
.down {{ color: var(--down); }}
.warning {{ color: #f59e0b; }}
.ok {{ color: var(--down); }}
.risk-low {{ color: var(--down); }}
.risk-medium {{ color: #f59e0b; }}
.risk-high {{ color: var(--up); }}
.badge {{ background: var(--accent); color: #fff; padding: 2px 8px; border-radius: 4px; font-size: 12px; }}
.subtitle {{ color: var(--muted); font-size: 0.875rem; margin-bottom: 1rem; }}
.non-trading-banner {{ background: rgba(245,158,11,0.15); border: 1px solid rgba(245,158,11,0.3); border-radius: 4px; padding: 8px 12px; margin-bottom: 16px; color: #f59e0b; font-size: 0.875rem; }}
.footer {{ margin-top: 2rem; padding-top: 1rem; border-top: 1px solid var(--border); color: var(--muted); font-size: 0.75rem; text-align: center; }}
.stock-link {{ color: var(--accent); text-decoration: none; font-weight: 700; }}
.stock-link:hover {{ text-decoration: underline; }}
/* ---- Modal ---- */
.modal {{
    display: none;
    position: fixed;
    top: 0; left: 0;
    width: 100%; height: 100%;
    background: rgba(0,0,0,0.7);
    z-index: 1000;
    align-items: center;
    justify-content: center;
}}
.modal.active {{ display: flex; }}
.modal-content {{
    background: {t['card']};
    border-radius: 12px;
    padding: 28px;
    max-width: 960px;
    width: 95%;
    max-height: 90vh;
    overflow-y: auto;
}}
.modal-content h2 {{
    margin: 0 0 16px;
    font-size: 1.2rem;
}}
.btn-agent {{
    padding: 5px 12px;
    font-size: 0.75rem;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--muted);
    cursor: pointer;
    transition: all 0.2s;
    font-family: inherit;
}}
.btn-agent:hover {{ border-color: var(--accent); color: var(--accent); }}
.btn-secondary {{
    padding: 4px 12px;
    font-size: 12px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--card);
    color: var(--muted);
    cursor: pointer;
    font-family: inherit;
}}
.btn-secondary:hover {{ background: rgba(255,255,255,0.05); color: var(--text); }}
.loading-container {{
    display: flex;
    align-items: center;
    justify-content: center;
    height: 520px;
    color: var(--muted);
    font-size: 0.875rem;
}}
@media print {{
    body {{ background: white; color: black; }}
    .stat {{ border: 1px solid #ccc; }}
    .modal {{ display: none !important; }}
}}
@media (max-width: 768px) {{
    .summary {{ grid-template-columns: repeat(2, 1fr); }}
}}
</style>
</head>
<body>
<div class="container">
{content}
</div>

<!-- K-line Chart Modal -->
<div class="modal" id="klineModal">
    <div class="modal-content">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
            <h2 id="klineTitle" style="margin:0;">K线图</h2>
            <div style="display:flex;gap:8px;flex-wrap:wrap;">
                <button class="btn-agent" onclick="switchKlinePeriod(250)" id="btnPeriod250">一年</button>
                <button class="btn-agent" onclick="switchKlinePeriod(120)" id="btnPeriod120">半年</button>
                <button class="btn-agent" onclick="switchKlinePeriod(60)" id="btnPeriod60">60日</button>
                <button class="btn-agent" onclick="switchKlinePeriod(30)" id="btnPeriod30">30日</button>
                <button class="btn-secondary" onclick="closeKlineModal()">关闭</button>
            </div>
        </div>
        <div id="klineChart" style="width:100%;height:520px;"></div>
    </div>
</div>

<script>
var klineChartInstance = null;
var klineCurrentCode = '';
var klineCurrentName = '';
var klineCurrentDays = 250;

function showKline(code, name) {{
    klineCurrentCode = code;
    klineCurrentName = name;
    klineCurrentDays = 250;
    document.getElementById('klineTitle').textContent = code + ' ' + name + ' - K线图';
    document.getElementById('klineModal').classList.add('active');
    var buttons = document.querySelectorAll('#klineModal .btn-agent');
    for (var i = 0; i < buttons.length; i++) {{
        buttons[i].style.borderColor = 'var(--border)';
    }}
    document.getElementById('btnPeriod250').style.borderColor = 'var(--accent)';
    loadKlineData(code, 250);
}}

function closeKlineModal() {{
    document.getElementById('klineModal').classList.remove('active');
    if (klineChartInstance) {{
        klineChartInstance.dispose();
        klineChartInstance = null;
    }}
}}

function switchKlinePeriod(days) {{
    klineCurrentDays = days;
    ['btnPeriod250','btnPeriod120','btnPeriod60','btnPeriod30'].forEach(function(id) {{
        var btn = document.getElementById(id);
        if (btn) btn.style.borderColor = parseInt(id.replace('btnPeriod','')) === days ? 'var(--accent)' : 'var(--border)';
    }});
    loadKlineData(klineCurrentCode, days);
}}

function loadKlineData(code, days) {{
    var chartDom = document.getElementById('klineChart');
    if (klineChartInstance) {{
        klineChartInstance.dispose();
        klineChartInstance = null;
    }}
    chartDom.innerHTML = '<div class="loading-container">加载K线数据...</div>';

    fetch('/api/chart/kline/' + encodeURIComponent(code) + '?days=' + days)
        .then(function(r) {{ return r.json(); }})
        .then(function(res) {{
            if (!res || !res.success) {{
                chartDom.innerHTML = '<div class="loading-container" style="color:#ef4444">' + (res && res.error ? res.error : '加载失败') + '</div>';
                return;
            }}
            renderKlineChart(code, res.data);
        }})
        .catch(function(e) {{
            chartDom.innerHTML = '<div class="loading-container" style="color:#ef4444">请求失败: ' + e.message + '</div>';
        }});
}}

function renderKlineChart(code, rawData) {{
    var chartDom = document.getElementById('klineChart');
    if (klineChartInstance) klineChartInstance.dispose();

    klineChartInstance = echarts.init(chartDom, 'dark');

    var dates = [];
    var ohlc = [];
    var volumes = [];
    var ma5Data = [];
    var ma10Data = [];
    var ma20Data = [];

    for (var i = 0; i < rawData.length; i++) {{
        var d = rawData[i];
        dates.push(d[0]);
        var prevClose = i > 0 ? rawData[i-1][2] : 0;
        var chg = prevClose ? parseFloat(((d[2] - prevClose) / prevClose * 100).toFixed(2)) : null;
        ohlc.push([d[1], d[2], d[3], d[4], chg]);

        var up = d[2] >= d[1] ? 1 : -1;
        volumes.push([i, d[5], up]);

        if (i >= 4) {{
            var sum5 = 0;
            for (var j = i - 4; j <= i; j++) sum5 += rawData[j][2];
            ma5Data.push(Math.round(sum5 / 5 * 100) / 100);
        }} else {{
            ma5Data.push(null);
        }}
        if (i >= 9) {{
            var sum10 = 0;
            for (var j = i - 9; j <= i; j++) sum10 += rawData[j][2];
            ma10Data.push(Math.round(sum10 / 10 * 100) / 100);
        }} else {{
            ma10Data.push(null);
        }}
        if (i >= 19) {{
            var sum20 = 0;
            for (var j = i - 19; j <= i; j++) sum20 += rawData[j][2];
            ma20Data.push(Math.round(sum20 / 20 * 100) / 100);
        }} else {{
            ma20Data.push(null);
        }}
    }}

    var option = {{
        animation: false,
        title: {{
            text: klineCurrentCode + ' ' + klineCurrentName,
            left: 'center',
            textStyle: {{ fontSize: 13, color: '#e2e8f0' }}
        }},
        tooltip: {{
            trigger: 'axis',
            axisPointer: {{ type: 'cross' }},
            formatter: function(params) {{
                var d = params[0];
                if (!d || !d.axisValue) return '';
                var k = null;
                for (var i = 0; i < params.length; i++) {{
                    if (params[i].seriesName === 'K线') {{ k = params[i]; break; }}
                }}
                var html = '<b>' + d.axisValue + '</b><br/>';
                if (k) {{
                    var v = k.data;
                    html += '开: ' + v[1] + '<br/>收: ' + v[2] + '<br/>低: ' + v[3] + '<br/>高: ' + v[4] + '<br/>';
                    if (v[5] != null) html += '涨跌幅: ' + (v[5] > 0 ? '+' : '') + v[5] + '%<br/>';
                }}
                var vol = null;
                for (var i = 0; i < params.length; i++) {{
                    if (params[i].seriesName === '成交量') {{ vol = params[i]; break; }}
                }}
                if (vol && vol.data) {{
                    html += '量: ' + (vol.data[1] / 10000).toFixed(0) + '万';
                }}
                return html;
            }}
        }},
        grid: [
            {{ left: '8%', right: '3%', top: '12%', height: '55%' }},
            {{ left: '8%', right: '3%', top: '75%', height: '16%' }}
        ],
        xAxis: [
            {{
                type: 'category',
                data: dates,
                axisLine: {{ lineStyle: {{ color: '#334155' }} }},
                axisLabel: {{ color: '#8b949e', fontSize: 10, formatter: function(v) {{ return v.slice(5); }} }},
                gridIndex: 0
            }},
            {{
                type: 'category',
                data: dates,
                axisLine: {{ lineStyle: {{ color: '#334155' }} }},
                axisLabel: {{ show: false }},
                gridIndex: 1
            }}
        ],
        yAxis: [
            {{
                type: 'value',
                scale: true,
                axisLine: {{ lineStyle: {{ color: '#334155' }} }},
                axisLabel: {{ color: '#8b949e', fontSize: 10 }},
                splitLine: {{ lineStyle: {{ color: '#1e293b' }} }},
                gridIndex: 0
            }},
            {{
                type: 'value',
                axisLine: {{ lineStyle: {{ color: '#334155' }} }},
                axisLabel: {{ show: false }},
                splitLine: {{ show: false }},
                gridIndex: 1
            }}
        ],
        dataZoom: [
            {{ type: 'inside', xAxisIndex: [0, 1], start: 50, end: 100 }},
            {{ type: 'slider', xAxisIndex: [0, 1], start: 50, end: 100, bottom: 5, height: 15, borderColor: '#334155', backgroundColor: '#1e293b', fillerColor: 'rgba(59,130,246,0.2)' }}
        ],
        series: [
            {{
                name: 'K线',
                type: 'candlestick',
                data: ohlc,
                xAxisIndex: 0,
                yAxisIndex: 0,
                itemStyle: {{
                    color: '#ef4444',
                    color0: '#22c55e',
                    borderColor: '#ef4444',
                    borderColor0: '#22c55e'
                }}
            }},
            {{
                name: 'MA5',
                type: 'line',
                data: ma5Data,
                xAxisIndex: 0,
                yAxisIndex: 0,
                smooth: true,
                lineStyle: {{ width: 1, color: '#f59e0b' }},
                symbol: 'none'
            }},
            {{
                name: 'MA10',
                type: 'line',
                data: ma10Data,
                xAxisIndex: 0,
                yAxisIndex: 0,
                smooth: true,
                lineStyle: {{ width: 1, color: '#3b82f6' }},
                symbol: 'none'
            }},
            {{
                name: 'MA20',
                type: 'line',
                data: ma20Data,
                xAxisIndex: 0,
                yAxisIndex: 0,
                smooth: true,
                lineStyle: {{ width: 1, color: '#a855f7' }},
                symbol: 'none'
            }},
            {{
                name: '成交量',
                type: 'bar',
                data: volumes,
                xAxisIndex: 1,
                yAxisIndex: 1,
                itemStyle: {{
                    color: function(params) {{
                        return params.data[2] > 0 ? '#ef4444' : '#22c55e';
                    }}
                }}
            }}
        ]
    }};

    klineChartInstance.setOption(option);

    window.addEventListener('resize', function() {{
        if (klineChartInstance) klineChartInstance.resize();
    }});
}}

// Close modal on backdrop click
document.getElementById('klineModal').addEventListener('click', function(e) {{
    if (e.target === document.getElementById('klineModal')) closeKlineModal();
}});
</script>
</body>
</html>"""

    @classmethod
    def generate_pipeline_report(cls, report: dict) -> str:
        """生成流水线日报 HTML（供 ReportAgent 使用）"""
        today = report.get("date", "")
        summary = report.get("summary", {})
        risk = report.get("risk", {})
        recs = report.get("recommendations", [])
        is_trading_day = report.get("is_trading_day", True)
        effective_date = report.get("effective_trade_date", today)

        # 风险等级颜色
        risk_color_class = f"risk-{risk.get('overall_risk', 'medium')}"

        # 非交易日提示
        banner = ""
        if not is_trading_day and effective_date != today:
            banner = f'<div class="non-trading-banner">非交易日，数据基于最近交易日 <b>{effective_date}</b></div>'

        # 建议仓位
        pos = risk.get("position_advice", {})
        suggested_pos = pos.get("suggested_position", 0.5) * 100

        # 推荐列表行
        rows = ""
        for rec in recs:
            strategy_label = "融合" if rec.get("strategy") == "fusion" else "v4超跌"
            score = rec.get("score", 0)
            trigger = "·".join(rec.get("trigger_list", [])) or "-"
            bt = rec.get("backtest", {})
            wr = bt.get("win_rate", bt.get("win_rate_5d", 0))
            avg_ret = bt.get("avg_return", bt.get("avg_return_5d", 0))
            risk_flags = rec.get("risk", {}).get("flags", [])
            risk_text = " ".join(risk_flags) if risk_flags else "正常"

            stock_name = rec.get('name', rec.get('code', ''))
            rows += f"""
            <tr>
                <td><a href="javascript:void(0)" class="stock-link" onclick="showKline('{rec.get('code', '')}', '{stock_name.replace(chr(39), chr(92)+chr(39))}')">{rec.get('code', '')}</a><br><small>{rec.get('name', '')}</small></td>
                <td><span class="badge">{strategy_label}</span></td>
                <td><b>{score:.1f}</b></td>
                <td>{rec.get('price', '-')}</td>
                <td>{trigger}</td>
                <td>{rec.get('stop_loss', '-')} / {rec.get('take_profit', '-')}</td>
                <td>{wr*100:.1f}%</td>
                <td>{avg_ret*100:.2f}%</td>
                <td class="{'warning' if risk_flags else 'ok'}">{risk_text}</td>
            </tr>
            """

        if not rows:
            rows = '<tr><td colspan="9" style="text-align:center;color:var(--muted);">今日无推荐</td></tr>'

        content = f"""
        <h1>每日交易计划 <span style="font-size:1rem;color:var(--muted);">{today}</span></h1>
        <div class="subtitle">生成时间: {report.get('generated_at', '')}</div>
        {banner}

        <div class="summary">
            <div class="stat">
                <div class="stat-label">融合策略命中</div>
                <div class="stat-value">{summary.get('fusion_hits', 0)}</div>
            </div>
            <div class="stat">
                <div class="stat-label">v4超跌命中</div>
                <div class="stat-value">{summary.get('v4_hits', 0)}</div>
            </div>
            <div class="stat">
                <div class="stat-label">精选推荐</div>
                <div class="stat-value">{summary.get('recommendations_count', 0)}</div>
            </div>
            <div class="stat">
                <div class="stat-label">市场环境</div>
                <div class="stat-value {risk_color_class}">{risk.get('market_trend', 'unknown')}</div>
            </div>
            <div class="stat">
                <div class="stat-label">建议仓位</div>
                <div class="stat-value">{suggested_pos:.0f}%</div>
            </div>
        </div>

        <h2>推荐列表</h2>
        <table>
            <thead>
                <tr>
                    <th>股票</th>
                    <th>策略</th>
                    <th>评分</th>
                    <th>现价</th>
                    <th>触发条件</th>
                    <th>止损/止盈</th>
                    <th>历史胜率</th>
                    <th>平均收益</th>
                    <th>风险状态</th>
                </tr>
            </thead>
            <tbody>
                {rows}
            </tbody>
        </table>

        <div class="footer">
            由多Agent协作流水线自动生成 | 仅供参考，投资有风险
        </div>
        """
        return cls._wrap_html(content)


# 单例
_report_generator: ReportGenerator | None = None


def get_report_generator() -> ReportGenerator:
    global _report_generator
    if _report_generator is None:
        _report_generator = ReportGenerator()
    return _report_generator
