# AIQuant 项目长期记忆

> ⚠️ 本文件描述**主工作树 `dev_0.0.1`**。另有未合并 worktree `.claude/worktrees/feature+aiquant-9-improvements`
> （LLM / deployment / governance / risk_config / AI 模型 / WebSocket），**切勿当作主树现状**。

## 项目与用户
- A股量化系统：Flask + SQLite(`core/quant.db`，3.9G) + akshare；前端单文件 `dashboard.html`(3700+行)
  + `static/css/dashboard.css`，无构建步骤。
- 路径 `E:\小项目\Project\AIQuant`；Windows；`start.bat` 启动、`install.bat` 装依赖（主树无 start-bg.bat）。
- 用户"寇豆码"，成都；偏好中文、表格/编号列表、根因+代码改动说明、精简 UI；A股**红涨绿跌**。
- 主树**无** sklearn / LLM / 风控(`risk/` 空) / 三省六部 / WebSocket / `config/risk_config.yaml`（均在 worktree）。

## 关键实测结论（不可推导，改动前必读）
- **buy 档裸持有选股能力为负，但加纪律后转正**：T+10 裸持有 -0.75%/44.9%；
  加回踩入场+止损+移动止盈后 **+1.38%/61.5%**（与 deep_track 实测 +1.32%/61.13% 吻合 → 模拟器可信）。
  → **收益主要来自出场纪律，不是选股**。add 档真实规则下 +0.82%/54.5%，**不要改成 add 优先**。
- **daily_top_n 保持 10**：5→+1.35%、10→+1.38%、20→+1.22%、30→+0.71%（断崖）。扩样本应加扫描天数。
- **max_hold_days 待决策**：T+7 +1.40%/周转快 21% vs T+10 +1.38%/61.5%（用户 2026-09-02 暂不改）。
- **buy 档衰减拐点**：裸持有 T+5~T+6 峰值，T+9 转负，T+15 -2.33%（buy 选出的已是启动票，后劲不足）。

## 个股深度 · 候选排序键（2026-09-02 定案，见 `stock_deep.candidate_order_by`）
- **旧逻辑只用 `pct_above_ma20 ASC`**：buy 池扩展度**全部 ≥0**（跌破 MA20 凑不到 buy 的 5 分门槛），
  故该键实为"选价格恰好等于 MA20 的票"，维度单一、区分度极差。
- **新逻辑（三档）**：`level buy优先 → score≥6 归强信号档 → 档内 ext ASC → code`（已实现并落地）。
  评估（`tools/eval_topk_pick.py`，**修正末段剔除偏差后** 60 扫描日，复刻真实入场出场）：
  均值 **+0.07%→+1.56%**、PF **1.02→1.48**、胜率 60.2%→62.9%、止损率 12.6%；逐日 39/56 天胜出（全场最高）；
  弱市前半段 -2.51%→-0.26%；topn 5/8/10/15/20 新键全为正、旧键在 5/15 为负。
  ⚠ 两套数字口径不同：上述是"as_of 修正后候选表"上的**前瞻**增益(+1.5pp)；若重放当初修正前建的 620 只
  真实旧单，增益仅 +0.19pp（旧单本身在旧数据上挑的，ext 分布不同）。新键实现与模拟器已交叉验证一致(+1.56%)。
- **机制（buy 池全样本分层归因）**：
  - score=5 占 88.6% 均值 -0.05%(PF 0.99)；score=6 占 10.9% **+0.62%(PF 1.16)**；
    score=7 仅 20 只 **-1.18%(PF 0.79)** → **score 必须封顶成两档**（无脑 DESC 会让 20 只极端票霸榜）。
  - ext<1 **+0.91%(PF 1.29)**；ext 1~3 -0.14%；ext 3~6 -0.43% → 同档内挑贴 MA20 的。
  - frac20 分层最单调（<0.33 +2.27%/PF 1.97；≥0.66 -1.02%/PF 0.77）但占比仅 1.4%，
    做主键 top10 区分度不足（PF 1.14），仅落库保留。
  - risk_pct / atr_pct **无区分度**（PF≈1.0），勿用。
- **配套**：`stock_deep_signal` 增列 score/base_score/frac20/risk_pct/atr_pct（旧库自动 ALTER）；
  历史行需 `tools/backfill_signal_features.py` 回填，否则 score 为 NULL 会**静默退化成旧排序**。
- 展示端 `get_market_signal_latest` 原按 `level, code`（字典序！），已改为共用同一排序键。

## 个股深度 · 深析信号（stock_deep）
- **历史回补必须传 `as_of`**：`load_history`/`analyze_stock`/`run_full_market_scan` 已支持；
  不传则取最新行情 = **未来函数**。证据(000651)：最新 38.95 sell / as_of 08-20 41.42 add。
- **分档 `_classify`**：buy≥5 / add≥2 / hold≥-2 / sell<-2。add 门槛极低 → 每天筛出 1000~1800 只，
  **其中 buy 仅 40~90 只**（add 是"技术面不差"，**不等于建议买入**）。
- **并行必须用多进程**：纯 Python 不释放 GIL，8 线程实测**慢 4.2 倍**；
  ProcessPoolExecutor 8 进程提速 3.3x（65→20ms/只），12 进程饱和（12 核）。
- **Windows spawn**：不能在 Flask 请求/线程里起进程池（会重复导入主模块重启 app）。
  解法 `tools/backfill_deep_scan.py`，Flask 用 `subprocess.Popen` 拉起，进度写 JSON 轮询。生产约 45 秒/扫描日。

## 个股深度 · 推荐跟踪（deep_track）
- **回补上限 = `stock_deep_signal` 的 DISTINCT scan_date 数**，不是用户输入天数。
- 入场：T+1 起 5 日内 low 触及买点才成交，价=min(买点,开盘)；出场：T+1 不判、
  收盘≤止损 / 已启动且收盘≤最高×(1-3%) / 满 max_hold 收盘平。
- 接口：`POST/GET /api/investor/deep_track/backfill_scan[/status]`（异步+进度，重复启动 409）；
  `POST .../refresh` 同步推进。列表返回 `total`，前端首屏 100 + 「加载更多(+100)」。
- 重建跟踪单无需重扫：`sync_from_scan` 只读 `stock_deep_signal`+`daily_price`，
  清空 deep_track 后逐日重跑 + `update_open_tracks` 即可（分钟级）。

## 短线推荐引擎 / 每日推荐数据流
- `SHORT_ENGINE="pure_bottom"`：短线推荐 100% 由 `strategy_bottom_fishing` 驱动，融合分=BOTTOM_SCORE×5。
- **"推荐已涨过的票"根因**：`score_rebound` 奖励"较10日最低反弹5%"→信号在反弹后最强；
  叠加 MA20 向上趋势门；全链路无超买/扩展度否决。方案见 `docs/short-reco-anti-chase-methods.md`。
- 18:00 调度只增量重算 fusion_score，**不写 stock_signal**；`stock_signal` 靠手动/前端兜底触发
  → **今日推荐时效不是调度保证的**。
- `rec_filters.py`：ST/退市 + 流动性 + 市值[30亿,800亿]；无波动率上限/换手率下限。候选池 = limit×3。
- `recommend_outcome` 无 status 列；optimizer 是 suggest-only（消费 daily_price 重放）。

## 前端与注意事项
- 今日推荐区 `#todayPicks`：三个 `.pick-group[data-hz=short|mid|long]`，默认简略态，点「详情」展开，
  展开态存 `localStorage['aiq_pick_expanded']`。
- 无浏览器验证套路：python 正则抽内联 script → node stub 环境 → 灌真实 API 数据 → 断言 HTML。
  项目未装 playwright。
- **⚠ 5000 端口常有用户自己的服务常驻**（Windows 可 SO_REUSEADDR 同绑，后起者拿不到流量 →
  新路由看着像 404）。改完后端**必须让用户重启 `start.bat`**（自带杀旧逻辑）。
  自测另起 5001，测完清理进程。
- **⚠ `PRAGMA table_info` 结果用索引 `r[1]`**，不要 `r["name"]` —— 没设 row_factory 的连接会
  抛异常被 except 吞掉，导致特性列检测静默失败（2026-09-02 已踩）。

## 待办 / 开放问题
- **待决策**：`DEEP_TRACK['max_hold_days']` 10→7（数据已备，用户暂不改）。
- 是否用新排序键**重建历史 610 单**（用户数据未动，仅代码+回填生效于未来建单）。
- 短线"挂高位"改进（见 `docs/short-reco-anti-chase-methods.md`）。
- 提选股质量的方向：降动量类权重、加位置类权重（frac20 低分位），需重新回测。
- `stock_info.industry` 恒空，无填充代码；行业配额类优化需先接行业数据源。

---
*最后更新: 2026-09-02（压缩重写；新增候选排序键定案与评估结论）*
