"""
ministries/libu/report_generator.py —— 礼部报表生成器

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
            rows += f"""
            <tr>
                <td><strong>{p.get('code', '')}</strong><br/><small style="color:var(--muted)">{p.get('name', '')}</small></td>
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
                <td><strong>{o.get('code', '')}</strong></td>
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
.container {{ max-width: 960px; margin: 0 auto; }}
h1 {{ font-size: 1.5rem; margin-bottom: 0.5rem; }}
h2 {{ font-size: 1.125rem; margin: 1.5rem 0 0.75rem; color: var(--accent); }}
.summary {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
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
@media print {{
    body {{ background: white; color: black; }}
    .stat {{ border: 1px solid #ccc; }}
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
</body>
</html>"""


# 单例
_report_generator: ReportGenerator | None = None


def get_report_generator() -> ReportGenerator:
    global _report_generator
    if _report_generator is None:
        _report_generator = ReportGenerator()
    return _report_generator
