"""
agents/report_agent.py —— 报告生成器

职责：
  1. 整合所有上游Agent输出
  2. 生成每日交易计划 HTML 报告
  3. 生成微信推送文本
  4. 保存报告到 reports/ 目录
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime

from agents.base import BaseAgent, AgentContext


class ReportAgent(BaseAgent):
    """报告生成器（尚书省·礼部·报表官）：整合输出，生成HTML报告和微信推送"""

    name = "ReportAgent"
    governance_role = "尚书省·报表官"

    # 风险标记中文映射
    RISK_FLAG_CN = {
        "no_data": "无数据",
        "limit_up_3d": "连续3日涨停",
        "heavy_drop_with_volume": "放量大跌",
        "near_20d_low": "接近20日低点",
        "rsi_overbought": "RSI超买",
        "screen_error": "筛查异常",
    }

    # 市场趋势中文映射
    TREND_CN = {
        "bull": "牛市",
        "neutral": "震荡",
        "bear": "熊市",
        "unknown": "未知",
    }

    # 风险等级中文映射
    RISK_LEVEL_CN = {
        "low": "低风险",
        "medium": "中等风险",
        "high": "高风险",
    }

    def _translate_flags(self, flags: list[str]) -> list[str]:
        """将风险标记翻译为中文"""
        return [self.RISK_FLAG_CN.get(f, f) for f in flags]

    def _translate_trend(self, trend: str) -> str:
        """将市场趋势翻译为中文"""
        return self.TREND_CN.get(trend, trend)

    def _translate_risk_level(self, level: str) -> str:
        """将风险等级翻译为中文"""
        return self.RISK_LEVEL_CN.get(level, level)

    def _execute(self, ctx: AgentContext) -> dict:
        today = date.today().strftime("%Y-%m-%d")

        # 收集上游数据
        data_result = ctx.get_result("DataAgent")
        signal_result = ctx.get_result("SignalAgent")
        backtest_result = ctx.get_result("BacktestAgent")
        risk_result = ctx.get_result("RiskAgent")

        # 生成报告数据
        report_data = self._build_report_data(ctx, today)

        # 生成HTML报告
        html_path = self._save_html_report(report_data, today)

        # 生成微信推送
        wechat_msg = self._build_wechat_msg(report_data)

        # 保存JSON数据
        json_path = self._save_json_report(report_data, today)

        return {
            "status": "ok",
            "html_path": html_path,
            "json_path": json_path,
            "wechat_msg": wechat_msg,
            "report_data": report_data,
        }

    def _build_report_data(self, ctx: AgentContext, today: str) -> dict:
        """整合所有数据构建报告结构"""
        signal_data = ctx.get_result("SignalAgent")
        risk_data = ctx.get_result("RiskAgent")
        backtest_data = ctx.get_result("BacktestAgent")

        # 非交易日时使用最近交易日数据
        effective_trade_date = ctx.get("effective_trade_date") or today
        is_trading_day = ctx.get("is_trading_day", True)

        fusion_top = signal_data.data.get("fusion_top", []) if signal_data else []
        v4_top = signal_data.data.get("v4_top", []) if signal_data else []

        # 合并候选并去重
        all_codes = set()
        merged = []
        for item in fusion_top + v4_top:
            if item["code"] not in all_codes:
                all_codes.add(item["code"])
                merged.append(item)

        # 回测报告映射
        bt_map = {}
        if backtest_data:
            for r in backtest_data.data.get("reports", []):
                bt_map[r["code"]] = r

        # 风险筛查映射
        risk_map = {}
        if risk_data:
            for r in risk_data.data.get("stock_risks", []):
                risk_map[r["code"]] = r

        # 构建推荐列表（含回测和风险信息）
        recommendations = []
        for item in merged:
            code = item["code"]
            bt = bt_map.get(code, {})
            risk = risk_map.get(code, {})

            rec = {
                **item,
                "backtest": {
                    "win_rate": bt.get("win_rate", bt.get("win_rate_5d", 0)),
                    "avg_return": bt.get("avg_return", bt.get("avg_return_5d", 0)),
                    "status": bt.get("status", "unknown"),
                },
                "risk": {
                    "flags": risk.get("risk_flags", []),
                    "rsi": risk.get("rsi", None),
                },
                "warning": len(risk.get("risk_flags", [])) > 0,
            }
            recommendations.append(rec)

        # 排序：融合优先，其次v4
        recommendations.sort(key=lambda x: (x.get("strategy") != "fusion", -x["score"]))

        # 风险摘要
        risk_summary = {}
        if risk_data:
            risk_summary = {
                "market_trend": risk_data.data.get("market_risk", {}).get("trend", "unknown"),
                "volatility_level": risk_data.data.get("volatility", {}).get("level", "medium"),
                "overall_risk": risk_data.data.get("overall_risk", "medium"),
                "position_advice": risk_data.data.get("position_advice", {}),
            }

        return {
            "date": today,
            "effective_trade_date": effective_trade_date,
            "is_trading_day": is_trading_day,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "summary": {
                "fusion_hits": signal_data.data.get("fusion_hits", 0) if signal_data else 0,
                "v4_hits": signal_data.data.get("v4_hits", 0) if signal_data else 0,
                "recommendations_count": len(recommendations),
                "risk_level": risk_summary.get("overall_risk", "medium"),
            },
            "risk": risk_summary,
            "recommendations": recommendations,
            "pipeline_status": ctx.summary(),
        }

    def _build_wechat_msg(self, report: dict) -> str:
        """生成微信推送文本"""
        today = report["date"]
        effective_date = report.get("effective_trade_date", today)
        is_trading_day = report.get("is_trading_day", True)
        recs = report["recommendations"][:3]
        risk = report.get("risk", {})

        # 非交易日标注
        date_note = ""
        if not is_trading_day and effective_date != today:
            date_note = f"（非交易日，数据日期: {effective_date}）\n"

        market_trend_cn = self._translate_trend(risk.get('market_trend', 'unknown'))
        overall_risk_cn = self._translate_risk_level(risk.get('overall_risk', 'medium'))

        if not recs:
            msg = f"A股小波段【{today}】\n{date_note}\n"
            msg += "今日无符合条件股票，空仓观望\n"
            msg += f"市场环境: {market_trend_cn}\n"
            msg += "(仅供参考，投资有风险)"
            return msg

        msg = f"A股小波段【{today}】交易计划\n{date_note}\n"
        msg += f"市场环境: {market_trend_cn} | 风险: {overall_risk_cn}\n"
        msg += f"建议仓位: {risk.get('position_advice', {}).get('suggested_position', 0.5) * 100:.0f}%\n\n"

        for i, rec in enumerate(recs):
            strategy_label = "融合" if rec.get("strategy") == "fusion" else "v4超跌"
            score = rec.get("score", 0)
            trigger = "·".join(rec.get("trigger_list", [])) or "无"
            msg += f"{i+1}. {rec['code']} {rec['name']} [{strategy_label} {score:.1f}分]\n"
            msg += f"   触发: {trigger}\n"
            msg += f"   现价: {rec['price']} 元 | 买入: {rec['buy_money']} 元\n"
            msg += f"   止损: {rec['stop_loss']} | 止盈: {rec['take_profit']}\n"

            # 回测信息
            bt = rec.get("backtest", {})
            if bt.get("status") == "ok":
                wr = bt.get("win_rate", 0)
                msg += f"   历史胜率: {wr*100:.1f}%\n"

            # 风险提示
            risk_flags = rec.get("risk", {}).get("flags", [])
            risk_flags_cn = self._translate_flags(risk_flags)
            if risk_flags_cn:
                msg += f"   ⚠️ 风险: {'、'.join(risk_flags_cn)}\n"
            msg += "\n"

        msg += "(仅供参考，投资有风险)"
        return msg

    def _save_html_report(self, report: dict, today: str) -> str:
        """生成并保存HTML报告"""
        reports_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "reports")
        os.makedirs(reports_dir, exist_ok=True)

        path = os.path.join(reports_dir, f"daily_report_{today}.html")

        html = self._render_html(report)
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)

        return path

    def _save_json_report(self, report: dict, today: str) -> str:
        """保存JSON格式报告"""
        reports_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "reports")
        os.makedirs(reports_dir, exist_ok=True)

        path = os.path.join(reports_dir, f"daily_report_{today}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)

        return path

    def _render_html(self, report: dict) -> str:
        """渲染HTML报告"""
        today = report["date"]
        effective_date = report.get("effective_trade_date", today)
        is_trading_day = report.get("is_trading_day", True)
        recs = report["recommendations"]
        risk = report.get("risk", {})
        summary = report.get("summary", {})

        # 非交易日提示
        non_trading_banner = ""
        if not is_trading_day and effective_date != today:
            non_trading_banner = f'<div style="background:#fff7e6;border:1px solid #ffd591;border-radius:4px;padding:8px 12px;margin-bottom:16px;color:#d46b08;font-size:13px;">⚠️ 非交易日，以下数据基于最近交易日 <b>{effective_date}</b> 的行情</div>'

        # 风险等级颜色
        risk_color = {"low": "#52c41a", "medium": "#faad14", "high": "#f5222d"}.get(
            risk.get("overall_risk", "medium"), "#faad14"
        )

        # 中文翻译后的值
        market_trend_cn = self._translate_trend(risk.get("market_trend", "unknown"))
        overall_risk_cn = self._translate_risk_level(risk.get("overall_risk", "medium"))

        rows = ""
        for rec in recs:
            strategy_badge = "融合" if rec.get("strategy") == "fusion" else "v4超跌"
            score = rec.get("score", 0)
            trigger = "·".join(rec.get("trigger_list", [])) or "-"
            bt = rec.get("backtest", {})
            wr = bt.get("win_rate", 0)
            avg_ret = bt.get("avg_return", 0)
            risk_flags = rec.get("risk", {}).get("flags", [])
            risk_flags_cn = self._translate_flags(risk_flags)
            warning = "⚠️ " + "、".join(risk_flags_cn) if risk_flags_cn else "✅ 正常"

            rows += f"""
            <tr>
                <td><b>{rec['code']}</b><br><small>{rec['name']}</small></td>
                <td><span class="badge">{strategy_badge}</span></td>
                <td><b>{score:.1f}</b></td>
                <td>{rec['price']}</td>
                <td>{trigger}</td>
                <td>{rec['stop_loss']} / {rec['take_profit']}</td>
                <td>{wr*100:.1f}%</td>
                <td>{avg_ret*100:.2f}%</td>
                <td class="{'warning' if risk_flags else 'ok'}">{warning}</td>
            </tr>
            """

        if not rows:
            rows = '<tr><td colspan="9" style="text-align:center;color:#999;">今日无推荐</td></tr>'

        pos = risk.get("position_advice", {})

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>每日交易计划 - {today}</title>
<style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }}
    .container {{ max-width: 1200px; margin: 0 auto; background: #fff; border-radius: 12px; padding: 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
    h1 {{ margin: 0 0 8px; font-size: 22px; }}
    .subtitle {{ color: #888; font-size: 14px; margin-bottom: 20px; }}
    .summary {{ display: flex; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }}
    .card {{ background: #fafafa; border-radius: 8px; padding: 16px; flex: 1; min-width: 180px; }}
    .card h3 {{ margin: 0 0 8px; font-size: 13px; color: #666; text-transform: uppercase; }}
    .card .value {{ font-size: 24px; font-weight: 700; }}
    .risk-low {{ color: #52c41a; }}
    .risk-medium {{ color: #faad14; }}
    .risk-high {{ color: #f5222d; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 14px; }}
    th {{ background: #f0f0f0; padding: 10px; text-align: left; font-weight: 600; }}
    td {{ padding: 10px; border-bottom: 1px solid #eee; }}
    tr:hover {{ background: #fafafa; }}
    .badge {{ background: #1890ff; color: #fff; padding: 2px 8px; border-radius: 4px; font-size: 12px; }}
    .warning {{ color: #f5222d; }}
    .ok {{ color: #52c41a; }}
    .footer {{ margin-top: 24px; padding-top: 16px; border-top: 1px solid #eee; color: #999; font-size: 12px; text-align: center; }}
</style>
</head>
<body>
<div class="container">
    <h1>每日交易计划 <span style="font-size:16px;color:#666;">{today}</span></h1>
    <div class="subtitle">生成时间: {report.get('generated_at', '')}</div>
    {non_trading_banner}

    <div class="summary">
        <div class="card">
            <h3>融合策略命中</h3>
            <div class="value">{summary.get('fusion_hits', 0)}</div>
        </div>
        <div class="card">
            <h3>v4超跌命中</h3>
            <div class="value">{summary.get('v4_hits', 0)}</div>
        </div>
        <div class="card">
            <h3>精选推荐</h3>
            <div class="value">{summary.get('recommendations_count', 0)}</div>
        </div>
        <div class="card">
            <h3>市场环境</h3>
            <div class="value risk-{risk.get('overall_risk', 'medium')}">{market_trend_cn}</div>
        </div>
        <div class="card">
            <h3>建议仓位</h3>
            <div class="value">{pos.get('suggested_position', 0.5)*100:.0f}%</div>
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
        由多Agent协作Alpha流水线自动生成 | 仅供参考，投资有风险
    </div>
</div>
</body>
</html>
"""


# 兼容旧接口
wechat_send = None
