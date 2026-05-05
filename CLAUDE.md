# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

AIQuant 是一套 A 股量化交易系统，涵盖数据同步、策略信号生成、全市场回测、风控审核和可视化仪表板。技术栈：**Python 3.11+、Flask、Streamlit、SQLite、Plotly、AkShare/baostock**。

## Development commands

```bash
# Install dependencies
pip install -r requirements.txt flask flask-cors schedule requests pandas plotly

# Run the system
python app.py                     # Flask API + dashboard (port 5000)

# Test pipeline
python test_pipeline.py           # Full agent pipeline test (requires DB)
python test_pipeline.py --mock    # Mock-only, no DB dependency
python test_pipeline.py --agent SignalAgent   # Test single agent

# Backtest
python -m pytest tests/ -v        # Run test suite (framework present, tests being added)
```

## Architecture: "三省六部" (Three Provinces & Six Ministries)

The system uses an ancient-Chinese-governance metaphor to structure modules:

### Decision pipeline (agents/)

| Stage | Agent | Role | Governance role |
|-------|-------|------|-----------------|
| 1 | `DataAgent` | Fetch, validate, clean data | 太子院·数据官 |
| 2 | `SignalAgent` | Generate strategy signals/scores | 中书省·策略官 |
| 3 | `RiskAgent` | Risk audit, veto power (BLOCK) | 门下省·风控官 |
| 3 | `BacktestAgent` | Backtest verification (parallel with Risk) | 尚书省·回测官 |
| 4 | `ReportAgent` | Generate HTML/JSON reports | 尚书省·报表官 |

Key constraints:
- **RiskAgent has veto power**: if it returns BLOCK, the pipeline skips ReportAgent.
- Stage 3 (RiskAgent + BacktestAgent) runs in parallel via `ThreadPoolExecutor`.
- Agents communicate through a shared `AgentContext` (key-value store).
- All agents extend `BaseAgent` and follow `run(ctx) -> AgentResult`.

### Six ministries (ministries/)

| Ministry | Dir | Responsibility |
|----------|-----|----------------|
| 吏部 (Personnel) | `personnel/` | Account management, user permissions |
| 户部 (Revenue) | `revenue/` | Capital management, P&L tracking |
| 礼部 (Rites) | `rites/` | Data source integration, data cleaning, market monitoring |
| 兵部 (War) | `war/` | Trade execution, order management |
| 刑部 (Justice) | `justice/` | Risk rules, audit logging, compliance |
| 工部 (Works) | `works/` | Infrastructure, config, monitoring/alerting |

### Core modules

| Dir | Purpose |
|-----|---------|
| `core/` | SQLite database (`db.py`), data sync (`sync.py`, `data_fetcher.py`), task queue |
| `strategy/` | 5-strategy scoring (放量突破/均线粘合/量价背离/抄底/主力建仓), fusion score calculation |
| `backtest/` | Multiple backtest engines — see note below on fragmentation |
| `risk/` | Risk engine with rule-based checks (`engine.py`), config-driven rules (`rules.py`) |
| `llm/` | LLM client (OpenAI-compatible API), strategy advisor with local fallback |
| `ai/` | ML-based prediction (`predictor.py`) |
| `live/` | Live trading monitor |
| `deployment/` | Deployment manager for broker integration |
| `routes/` | Flask Blueprint API routes (one file per domain, ~30 blueprints) |
| `services/` | Business logic layer — one service per domain |
| `config/` | `settings.py` (base config) + `strategy_params.py` + `thresholds.py` |

### Data flow

```
baostock/AkShare → core/sync.py → SQLite (core/quant.db, ~12 tables)
                                        ↓
strategy/strategies.py → 5 strategy scores → stock_signal table
                                        ↓
agents pipeline: DataAgent → SignalAgent → [RiskAgent ∥ BacktestAgent] → ReportAgent
                                        ↓
Flask API (app.py, ~30 blueprints) → HTML dashboards (dashboard.html, etc.)
```

## Key architectural notes

- **Flask is the backend** (`app.py`), not Streamlit. The architecture doc refers to Streamlit but the current runtime is Flask on port 5000. The `backtest/` directory retains a Streamlit UI (`backtest_ui.py`) that may be invoked separately.
- **db.py is large (~1900 lines)** — it manages ~12 SQLite tables in a single file. New tables follow the same `upsert_*/get_*/get_latest_*/_clean/_fmt` pattern. Use `get_conn()` context manager for all DB operations.
- **Backtest engine fragmentation**: `backtest/` contains multiple engines with duplicated logic (fee, slippage, position management). The canonical core engines are `backtest.py` (single stock), `strategy_screen_backtest.py` (batch screening), and `custom_strategy_backtest.py` (custom functions). Others (`backtest_v3.py`, `backtest_bt.py`, `backtest_quant.py`) are experimental/historical.
- **Config-driven risk**: risk rules in `risk/rules.py` can be toggled via `risk/config_loader.py`. Risk levels: PASS < WARNING < RESTRICT < BLOCK.
- **LLM integration**: `llm/client.py` wraps OpenAI-compatible APIs (supports DeepSeek, Moonshot, Qwen). Falls back to local rule engine when unavailable.
- **WebSocket**: `routes/market_ws.py` provides real-time market data push via Flask-Sock.

## File naming conventions

- Chinese filenames (e.g., `仪表板.html`) exist for backward compatibility
- Configuration YAML files (e.g., `config/llm_config.yaml`) are gitignored — use `.example` templates
- `data_cache/`, `reports/`, `.sync_cache/` are gitignored runtime directories
