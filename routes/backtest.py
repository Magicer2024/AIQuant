"""
routes/backtest.py —— 批量回测 + 策略选股回测 + 策略实验室回测路由（薄层）
"""
import traceback
from datetime import date
from flask import jsonify, request

from routes import backtest_bp
from services.backtest_service import (
    run_batch_backtest,
    get_strategy_lab_strategies_for_backtest,
    run_strategy_lab_backtest,
)
from ministries.personnel.strategy_registry import get_strategy_registry


@backtest_bp.route("/batch", methods=["GET"])
def batch_backtest():
    weights_str = request.args.get("weights", "")
    weights = None
    if weights_str:
        try:
            weights = [float(w.strip()) for w in weights_str.split(",")]
            if len(weights) != 5:
                return jsonify({"error": "weights 必须包含 5 个数值"}), 400
        except ValueError:
            return jsonify({"error": "weights 格式错误，需如 0.30,0.15,0.20,0.20,0.15"}), 400

    try:
        result = run_batch_backtest(
            start_date=request.args.get("start_date", "2025-04-03"),
            end_date=request.args.get("end_date", date.today().strftime("%Y-%m-%d")),
            min_score=float(request.args.get("min_score", 15.0)),
            capital=float(request.args.get("capital", 100000)),
            max_positions=int(request.args.get("max_positions", 3)),
            max_position_size=float(request.args.get("max_position_size", 400000)),
            stop_loss=float(request.args.get("stop_loss", -0.06)),
            take_profit=float(request.args.get("take_profit", 0.20)),
            use_market_timing=request.args.get("use_market_timing", "true").lower() == "true",
            use_dynamic_position=request.args.get("use_dynamic_position", "false").lower() == "true",
            weights=weights,
            use_v4=True,
        )
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@backtest_bp.route("/screen", methods=["POST"])
def screen_backtest():
    """策略选股回测：基于选股条件 + 历史日期区间回测"""
    try:
        data = request.get_json() or {}
    except Exception:
        return jsonify({"error": "请求体需为 JSON"}), 400

    strategy_name = data.get("strategy", "融合信号高分选股")
    start_date = data.get("start_date", "2025-01-01")
    end_date = data.get("end_date", date.today().strftime("%Y-%m-%d"))
    capital = float(data.get("capital", 100000))
    max_positions = int(data.get("max_positions", 3))
    hold_days = int(data.get("hold_days", 5))
    buy_num = int(data.get("buy_num_per_day", 1))
    stop_loss = float(data.get("stop_loss", -0.06))
    take_profit = float(data.get("take_profit", 0.20))
    trailing_stop = float(data.get("trailing_stop", 0.0))
    fusion_threshold = float(data.get("fusion_threshold", 15.0))

    # 验证策略名称
    valid_strategies = [
        "5日线突破（3天内收在5日线上）",
        "均线多头 + 放量",
        "超跌反弹（5日跌幅 > 8%）",
        "量价背离（量减价稳）",
        "融合信号高分选股",
    ]
    if strategy_name not in valid_strategies:
        return jsonify({"error": f"未知策略，可选: {valid_strategies}"}), 400

    try:
        from backtest.strategy_screen_backtest import (
            StrategyScreenBacktest,
            BacktestParams,
            make_preset_strategy,
        )

        filters, sort_by, sort_asc = make_preset_strategy(strategy_name)

        # 如果选择融合信号策略，追加阈值过滤
        if strategy_name == "融合信号高分选股":
            filters = [
                lambda row, t=fusion_threshold: (row.get('fusion_score', 0) or 0) >= t,
            ]

        params = BacktestParams(
            start_date=start_date,
            end_date=end_date,
            capital=capital,
            hold_days=hold_days,
            max_positions=max_positions,
            buy_num_per_day=buy_num,
            stop_loss=stop_loss,
            take_profit=take_profit,
            trailing_stop=trailing_stop,
            screen_filters=filters,
            sort_by=sort_by,
            sort_ascending=sort_asc,
            exclude_kcb=True,
            exclude_cyb=True,
            exclude_st=True,
        )

        engine = StrategyScreenBacktest(params)
        result = engine.run()

        # 精简 trade 列表（只返回关键字段，避免过大）
        if "trades" in result and len(result["trades"]) > 50:
            trades_brief = []
            for t in result["trades"]:
                trades_brief.append({
                    "code": t.get("code", ""),
                    "name": t.get("name", ""),
                    "buy_date": str(t.get("buy_date", "")),
                    "sell_date": str(t.get("sell_date", "")),
                    "entry_price": t.get("entry_price"),
                    "exit_price": t.get("exit_price"),
                    "pnl": t.get("pnl"),
                    "ret_pct": t.get("ret_pct"),
                    "hold_days": t.get("hold_days"),
                    "exit_reason": t.get("exit_reason", ""),
                })
            result["trades_brief"] = trades_brief
            # 限制完整 trades 最多 50 条
            result["trades"] = result["trades"][:50]

        # 精简 equity_curve
        if "equity_curve" in result and isinstance(result["equity_curve"], list):
            # 对超过 200 个点的 equity curve 做采样
            eq = result["equity_curve"]
            if len(eq) > 200:
                step = len(eq) // 200
                result["equity_curve"] = [eq[i] for i in range(0, len(eq), step)]

        result["strategy_name"] = strategy_name
        result["success"] = True
        return jsonify(result)

    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@backtest_bp.route("/strategies", methods=["GET"])
def list_strategies():
    """获取策略列表（从吏部策略注册中心）"""
    registry = get_strategy_registry()
    return jsonify({
        "success": True,
        "strategies": registry.to_dict(),
        "presets": [
            "5日线突破（3天内收在5日线上）",
            "均线多头 + 放量",
            "超跌反弹（5日跌幅 > 8%）",
            "量价背离（量减价稳）",
            "融合信号高分选股",
        ],
    })


# ═══════════════════════════════════════════════════════════════
# 策略实验室回测端点
# ═══════════════════════════════════════════════════════════════

@backtest_bp.route("/strategy-lab-strategies", methods=["GET"])
def strategy_lab_strategies():
    """获取策略实验室活跃策略列表（用于回测选择）"""
    try:
        strategies = get_strategy_lab_strategies_for_backtest()
        return jsonify({"success": True, "strategies": strategies})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@backtest_bp.route("/run-strategy-lab", methods=["POST"])
def run_strategy_lab():
    """使用策略实验室规则进行多股票筛选回测"""
    try:
        data = request.get_json() or {}
    except Exception:
        return jsonify({"error": "请求体需为 JSON"}), 400

    rule_ids = data.get("rule_ids", [])
    if not rule_ids:
        return jsonify({"success": False, "error": "请至少选择一个策略规则"}), 400
    if not isinstance(rule_ids, list):
        return jsonify({"success": False, "error": "rule_ids 需为数组"}), 400

    start_date = data.get("start_date", "2025-01-01")
    end_date = data.get("end_date", date.today().strftime("%Y-%m-%d"))
    capital = float(data.get("capital", 100000))
    max_positions = int(data.get("max_positions", 5))
    buy_num_per_day = int(data.get("buy_num_per_day", 3))
    hold_days = int(data.get("hold_days", 10))
    stop_loss = float(data.get("stop_loss", -0.06))
    take_profit = float(data.get("take_profit", 0.15))
    trailing_stop = float(data.get("trailing_stop", 0.0))
    signal_mode = data.get("signal_mode", "any")

    try:
        result = run_strategy_lab_backtest(
            rule_ids=rule_ids,
            start_date=start_date,
            end_date=end_date,
            capital=capital,
            max_positions=max_positions,
            buy_num_per_day=buy_num_per_day,
            hold_days=hold_days,
            stop_loss=stop_loss,
            take_profit=take_profit,
            trailing_stop=trailing_stop,
            signal_mode=signal_mode,
        )
        return jsonify(result)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500
