# AIQuant

A 股量化选股与个人股票评分系统：数据同步 → 策略打分 → 选股筛选 → 回测验证 → 可视化仪表板。

## 技术栈

Python 3.11+ / Flask / SQLite / pandas / AkShare + baostock + 东方财富接口 / scikit-learn / 可选 Qlib

## 快速开始

```bash
# 安装依赖（constraints.txt 锁定可复现版本；基础安装不含 Qlib）
pip install -r requirements.txt -c constraints.txt

# 启动（Windows 一键）
start.bat

# 或手动启动
python app.py
```

启动后访问 http://localhost:5000/ 打开仪表板，健康检查 http://localhost:5000/api/health、就绪检查 http://localhost:5000/api/ready。

首次启动会自动创建 `core/quant.db` 并开启每日 19:00 定时数据同步（scheduler/，龙虎榜通常 18:30 后才齐全，故宁晚勿缺）。

## 目录结构

```
app.py               Flask 入口（注册 7 个 Blueprint，端口 5000）
dashboard.html       单文件前端仪表板
core/                数据层：db.py（SQLite ~12 表）、sync.py（行情同步）、
                     data_fetcher / data_cleaner、em_*（东财接口+护栏）、
                     repository/（15 个领域 Repository）
strategy/            策略层：scorer.py（每日打分）、factor_lib、indicators、
                     rules_store、intent/parser（自然语言选股）、optimizer（参数寻优）
backtest/            回测层：engine.py、service.py、conditions.py
routes/              Flask API：system / sync / scoring / screen / backtest / optimizer / investor
scheduler/           定时任务（每日 19:00 自动同步+重算分；独立 Scheduler 实例+持久化）
qlib_engine/         可选 Qlib 集成（依赖见 requirements-qlib.txt，QLIB_ENABLED 默认 False，懒导入，不影响主流程）
ai/                  机器学习预测（RandomForest）
config/              settings.py、strategy_params.py、thresholds.py
utils/               api 响应封装（含 identity_meta 身份链）、序列化、trade_constraints、缓存、计时
tests/               pytest 测试套件
docs/                文档（ARCHITECTURE.md 已过时，结构以 CLAUDE.md 为准）
```

## 测试

```bash
pip install -r requirements-dev.txt -c constraints.txt
python -m pytest tests/ -q -m "not browser"   # 核心测试（浏览器作业单独跑）
```

浏览器冒烟测试需 Playwright：`pip install -r requirements-browser.txt -c constraints.txt`，再 `AIQUANT_BROWSER_TESTS=1 python -m pytest tests/test_frontend.py -m browser`。

网络基准测试默认跳过，手动执行：`python tests/test_em_speed.py`。

## 配置

- `config/settings.py`：同步、回测、策略挖掘参数
- `config/llm_config.yaml`：LLM 密钥（已 gitignore，不上库）
- `config/personal_config.py`：个人账户配置

## 部署

提供 `Dockerfile`（单容器，暴露 5000 端口，含健康检查）。
