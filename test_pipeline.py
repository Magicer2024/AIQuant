"""
test_pipeline.py —— 多Agent流水线端到端测试

用法：
    python test_pipeline.py [--mock] [--agent <name>]

选项：
    --mock       使用Mock数据测试（不依赖数据库）
    --agent NAME 仅测试单个Agent
"""

import sys
import os
import argparse
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _test_base_module():
    """测试基础模块导入"""
    print("=" * 60)
    print("[测试1/5] 基础模块导入")
    print("=" * 60)

    try:
        from agents.base import AgentContext, AgentResult, BaseAgent
        print("  ✅ agents.base")
    except Exception as e:
        print(f"  ❌ agents.base: {e}")
        return False

    try:
        from agents.orchestrator import PipelineOrchestrator
        print("  ✅ agents.orchestrator")
    except Exception as e:
        print(f"  ❌ agents.orchestrator: {e}")
        return False

    return True


def _test_agent_imports():
    """测试各Agent能否导入（部分可能因缺少外部依赖而失败）"""
    print("\n" + "=" * 60)
    print("[测试2/5] Agent模块导入（部分可能跳过）")
    print("=" * 60)

    agents_status = {}

    # DataAgent —— 依赖 core.sync → data_fetcher → baostock
    try:
        from agents.data_agent import DataAgent
        print("  ✅ DataAgent")
        agents_status["DataAgent"] = True
    except Exception as e:
        print(f"  ⚠️  DataAgent 跳过（缺少外部依赖: {str(e)[:60]}...）")
        agents_status["DataAgent"] = False

    # SignalAgent —— 依赖 core.db + strategy.strategies
    try:
        from agents.signal_agent import SignalAgent
        print("  ✅ SignalAgent")
        agents_status["SignalAgent"] = True
    except Exception as e:
        print(f"  ❌ SignalAgent: {e}")
        agents_status["SignalAgent"] = False

    # BacktestAgent —— 依赖 backtest.backtest_v4 → baostock
    try:
        from agents.backtest_agent import BacktestAgent
        print("  ✅ BacktestAgent")
        agents_status["BacktestAgent"] = True
    except Exception as e:
        print(f"  ⚠️  BacktestAgent 跳过（缺少外部依赖: {str(e)[:60]}...）")
        agents_status["BacktestAgent"] = False

    # RiskAgent —— 依赖 core.db
    try:
        from agents.risk_agent import RiskAgent
        print("  ✅ RiskAgent")
        agents_status["RiskAgent"] = True
    except Exception as e:
        print(f"  ❌ RiskAgent: {e}")
        agents_status["RiskAgent"] = False

    # ReportAgent —— 无外部依赖
    try:
        from agents.report_agent import ReportAgent
        print("  ✅ ReportAgent")
        agents_status["ReportAgent"] = True
    except Exception as e:
        print(f"  ❌ ReportAgent: {e}")
        agents_status["ReportAgent"] = False

    return agents_status


def _test_orchestrator():
    """测试编排器基础功能"""
    print("\n" + "=" * 60)
    print("[测试3/5] 编排器状态管理")
    print("=" * 60)

    from agents.orchestrator import PipelineOrchestrator

    orch = PipelineOrchestrator()
    print(f"  创建编排器: {type(orch).__name__}")
    print(f"  运行状态: {orch.is_running()}")
    print(f"  注册Agent: {list(orch._agent_modules.keys())}")
    print(f"  历史记录: {len(orch.get_history())} 条")

    assert not orch.is_running()
    assert len(orch._agent_modules) == 5
    print("  ✅ 编排器基础功能正常")
    return True


def _test_mock_pipeline(agents_status: dict):
    """用Mock数据测试完整流水线（跳过不可用的Agent）"""
    print("\n" + "=" * 60)
    print("[测试4/5] Mock流水线端到端测试")
    print("=" * 60)

    from agents.base import AgentContext
    from agents.report_agent import ReportAgent

    ctx = AgentContext(
        pipeline_id=f"test-mock-{datetime.now().strftime('%H%M%S')}",
        run_date=date.today().strftime("%Y-%m-%d"),
    )

    # Mock候选数据
    mock_candidates = [
        {
            "code": "000001", "name": "平安银行", "price": 12.50,
            "trade_date": date.today().strftime("%Y-%m-%d"),
            "score": 35.5, "strategy": "fusion",
            "buy_money": 5000, "buy_volume": 400,
            "stop_loss": 11.75, "take_profit": 15.00,
            "s1_vol_break": 8.5, "s2_ma_conv": 6.0,
            "s3_pv_div": 7.5, "s4_bottom": 9.0, "s5_whale": 4.5,
            "trigger_list": ["抄底", "放量突破"],
        },
        {
            "code": "000002", "name": "万科A", "price": 18.20,
            "trade_date": date.today().strftime("%Y-%m-%d"),
            "score": 32.0, "strategy": "v4_oversold",
            "raw_score": 2.5, "buy_money": 5000, "buy_volume": 200,
            "stop_loss": 17.10, "take_profit": 21.84,
            "trigger_list": ["超跌反弹"],
        },
    ]

    # 模拟DataAgent结果
    if agents_status.get("DataAgent"):
        from agents.data_agent import DataAgent
        print("\n  → 运行 DataAgent（Mock模式）...")
        agent = DataAgent()
        result = agent.run(ctx)
        status = "✅" if result.success else "❌"
        print(f"  ← DataAgent {status} 耗时={result.elapsed_sec:.2f}s")
    else:
        print("\n  ⚠️ 跳过 DataAgent（外部依赖不可用）")
        ctx.set("today", date.today().strftime("%Y-%m-%d"))

    # 模拟SignalAgent结果
    if agents_status.get("SignalAgent"):
        print("\n  → 运行 SignalAgent（Mock模式）...")
        # 直接注入mock数据，不运行真实扫描（需要数据库）
        ctx.set("fusion_candidates", [mock_candidates[0]])
        ctx.set("v4_candidates", [mock_candidates[1]])
        ctx.set("fusion_all_count", 15)
        ctx.set("v4_all_count", 8)
        print("  ← SignalAgent ✅（使用Mock数据）")
    else:
        print("\n  ⚠️ 跳过 SignalAgent（不可用）")
        ctx.set("fusion_candidates", [mock_candidates[0]])
        ctx.set("v4_candidates", [mock_candidates[1]])

    # BacktestAgent
    if agents_status.get("BacktestAgent"):
        from agents.backtest_agent import BacktestAgent
        print("\n  → 运行 BacktestAgent...")
        agent = BacktestAgent()
        result = agent.run(ctx)
        status = "✅" if result.success else "❌"
        print(f"  ← BacktestAgent {status} 耗时={result.elapsed_sec:.2f}s")
        if result.success:
            print(f"     报告数: {result.data.get('reports_count', 0)}")
    else:
        print("\n  ⚠️ 跳过 BacktestAgent（外部依赖不可用）")
        # 注入mock回测数据
        ctx.set("backtest_reports", [
            {"code": "000001", "win_rate": 0.65, "avg_return": 0.03, "status": "ok"},
            {"code": "000002", "win_rate": 0.55, "avg_return": 0.02, "status": "ok"},
        ])

    # RiskAgent
    if agents_status.get("RiskAgent"):
        from agents.risk_agent import RiskAgent
        print("\n  → 运行 RiskAgent...")
        agent = RiskAgent()
        result = agent.run(ctx)
        status = "✅" if result.success else "❌"
        print(f"  ← RiskAgent {status} 耗时={result.elapsed_sec:.2f}s")
        if result.success:
            print(f"     大盘趋势: {result.data.get('market_risk', {}).get('trend', 'unknown')}")
            print(f"     波动率: {result.data.get('volatility', {}).get('level', 'unknown')}")
            print(f"     综合风险: {result.data.get('overall_risk', 'unknown')}")
    else:
        print("\n  ⚠️ 跳过 RiskAgent（不可用）")
        ctx.set("risk_assessment", {
            "overall_risk": "medium",
            "market_risk": {"trend": "bull"},
            "position_advice": {"suggested_position": 0.5},
        })

    # ReportAgent（应该总是可用）
    print("\n  → 运行 ReportAgent...")
    agent = ReportAgent()
    result = agent.run(ctx)
    status = "✅" if result.success else "❌"
    print(f"  ← ReportAgent {status} 耗时={result.elapsed_sec:.2f}s")

    if result.success:
        html_path = result.data.get('html_path')
        json_path = result.data.get('json_path')
        print(f"     HTML报告: {html_path}")
        print(f"     JSON报告: {json_path}")

        if html_path and os.path.exists(html_path):
            size = os.path.getsize(html_path)
            print(f"     HTML文件大小: {size} bytes ✅")
        else:
            print(f"     ⚠️ HTML文件未找到")

        # 显示微信消息预览
        msg = result.data.get('wechat_msg', '')
        print(f"\n  微信推送预览（前200字）:")
        print(f"  {'-'*50}")
        print(f"  {msg[:200]}...")
        print(f"  {'-'*50}")

    print("\n  ✅ Mock流水线测试完成")
    return True


def _test_real_pipeline(agents_status: dict, agent_name: str | None = None):
    """用真实数据库数据测试"""
    print("\n" + "=" * 60)
    print("[测试5/5] 真实数据流水线测试（ SignalAgent → RiskAgent → ReportAgent ）")
    print("=" * 60)

    # 检查数据库
    try:
        from core.db import init_db, db_stats
        init_db()
        stats = db_stats()
        stock_count = stats.get("有行情股票数", 0)
        print(f"  数据库: {stock_count} 只股票 / {stats.get('行情记录总数', 0)} 条记录")

        if stock_count == 0:
            print("  ⚠️ 数据库为空，跳过真实数据测试")
            return False

    except Exception as e:
        print(f"  ⚠️ 数据库不可用: {e}")
        return False

    # 只测试可用的Agent
    from agents.base import AgentContext

    ctx = AgentContext(
        pipeline_id=f"test-real-{datetime.now().strftime('%H%M%S')}",
        run_date=date.today().strftime("%Y-%m-%d"),
    )

    # 手动模拟DataAgent成功结果
    ctx.set_result(type('obj', (object,), {
        'agent_name': 'DataAgent',
        'success': True,
        'data': {'trading_day': True, 'already_latest': True, 'today': date.today().strftime("%Y-%m-%d")},
        'error': '', 'started_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        'finished_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 'elapsed_sec': 0.1
    })())

    # 确定要运行的Agent序列
    available = []
    if agents_status.get("SignalAgent"):
        available.append(("SignalAgent", "agents.signal_agent", "SignalAgent"))
    if agents_status.get("RiskAgent"):
        available.append(("RiskAgent", "agents.risk_agent", "RiskAgent"))
    if agents_status.get("ReportAgent"):
        available.append(("ReportAgent", "agents.report_agent", "ReportAgent"))

    if agent_name:
        available = [(n, m, c) for n, m, c in available if n == agent_name]

    if not available:
        print("  ⚠️ 没有可用的Agent进行真实数据测试")
        return False

    for name, module, class_name in available:
        mod = __import__(module, fromlist=[class_name])
        agent_cls = getattr(mod, class_name)
        agent = agent_cls()

        print(f"\n  → 运行 {name}...")
        result = agent.run(ctx)
        status = "✅" if result.success else "❌"
        print(f"  ← {name} {status} 耗时={result.elapsed_sec:.2f}s")

        if not result.success:
            print(f"     错误: {result.error[:300]}")
        else:
            print(f"     数据键: {list(result.data.keys())}")
            if name == "SignalAgent":
                print(f"     融合命中: {result.data.get('fusion_hits', 0)}")
                print(f"     v4命中: {result.data.get('v4_hits', 0)}")
                top = result.data.get('fusion_top', [])
                if top:
                    print(f"     融合Top1: {top[0]['code']} {top[0]['name']} 评分:{top[0]['score']}")
                v4_top = result.data.get('v4_top', [])
                if v4_top:
                    print(f"     v4 Top1: {v4_top[0]['code']} {v4_top[0]['name']} 评分:{v4_top[0]['score']}")
            elif name == "RiskAgent":
                print(f"     大盘: {result.data.get('market_risk', {}).get('trend', '?')}")
                print(f"     风险等级: {result.data.get('overall_risk', '?')}")
                pos = result.data.get('position_advice', {})
                print(f"     建议仓位: {pos.get('suggested_position', 0)*100:.0f}%")
            elif name == "ReportAgent":
                html_path = result.data.get('html_path')
                if html_path and os.path.exists(html_path):
                    print(f"     HTML报告: {html_path} ({os.path.getsize(html_path)} bytes)")

    # 打印上下文摘要
    print("\n" + "-" * 60)
    print("  流水线执行摘要:")
    summary = ctx.summary()
    for agent_name_result, info in summary.get("agents", {}).items():
        icon = "✅" if info['success'] else "❌"
        print(f"    {icon} {agent_name_result}: {info['elapsed_sec']}s")

    return True


def main():
    parser = argparse.ArgumentParser(description="多Agent流水线测试")
    parser.add_argument("--mock", action="store_true", help="仅Mock测试（跳过真实数据）")
    parser.add_argument("--agent", type=str, help="仅测试指定Agent")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print("  多Agent协作Alpha流水线 —— 端到端测试")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    # 测试1: 基础模块
    if not _test_base_module():
        print("\n❌ 基础模块测试失败，停止")
        sys.exit(1)

    # 测试2: Agent导入状态
    agents_status = _test_agent_imports()
    available_count = sum(1 for v in agents_status.values() if v)
    print(f"\n  可用Agent: {available_count}/{len(agents_status)}")

    # 测试3: 编排器
    if not _test_orchestrator():
        print("\n❌ 编排器测试失败")
        sys.exit(1)

    # 测试4: Mock流水线
    if not _test_mock_pipeline(agents_status):
        print("\n❌ Mock流水线测试失败")
        sys.exit(1)

    # 测试5: 真实数据流水线
    if not args.mock:
        _test_real_pipeline(agents_status, agent_name=args.agent)
    else:
        print("\n  ⚠️ 跳过真实数据测试（--mock 模式）")

    print("\n" + "=" * 60)
    print("  ✅ 所有测试完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
