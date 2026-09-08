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
- **daily_top_n 由 10 改 4**（2026-09-05）：旧 topn 敏感性测试显示 5→+1.35%、10→+1.38% 几乎无差，差距为噪音。
  砍到 4 后用 Q12 综合质量分（详见下节）从全 buy 池（每天 40~90 只）精挑，均值 2.15%/胜率 67.6%/PF 2.18，远超旧 N 键。
  资金约束：沿用 `POSITION_PLAN_ACCOUNT = 10000` + 主板中价 10~30 元，10 只全建仓会撞 `MAX_POSITIONS=5` 硬顶。
- **max_hold_days 待决策**：T+7 +1.40%/周转快 21% vs T+10 +1.38%/61.5%（用户 2026-09-02 暂不改）。
- **buy 档衰减拐点**：裸持有 T+5~T+6 峰值，T+9 转负，T+15 -2.33%（buy 选出的已是启动票，后劲不足）。

## 个股深度 · 候选排序键（2026-09-05 升级 Q12，见 `stock_deep.candidate_order_by`）
- **历史版本（2026-09-02 三档键）**：`level buy优先 → score≥6 归强信号档 → 档内 ext ASC`。60 扫描日均值+1.56%/胜率62.9%/PF1.48，已废弃。
- **Q12 综合质量分（当前）**：`2.2*(1-frac20) + 1.0*(pct_above_ma60<0) - 1.5*(volume_ratio>=2) - 0.6*(max(0,atr_pct-4)/4)`，按 level 优先 buy 排序。
  - 60 扫描日均值 **2.15%** / 胜率 **67.6%** / PF **2.18**（vs 旧 N 键 +1.04%/60.8%/1.33）。
  - 弱市前段 **+1.06 vs -1.08**（旧键弱市是亏的！）；topn=4/5/8/10 递减 1.52→1.10（头部区分度强，契合 top4）。
  - 机制：买"未启动、位置低"的；不买"已启动、量价齐升、趋势漂亮"的（与「buy 选出的已是启动票」完全吻合）。
  - 过拟合探针验证：Q7/Q10/Q11 权重 1.2→2.2→3.2→4.5 对应 1.52→1.72→1.70→1.65，**2.2 见顶后回落**，证明不是拟合噪音。
  - 权重来源：`tools/eval_topk_pick.py` 关键归因（frac20<0.33 +2.27%/PF1.97、ext60<0 +1.33%/PF1.48、vol_ratio≥2 -1.36%/PF0.71、atr_pct>4 -0.43%/PF0.90）。
- **`stock_deep_signal` 增 5 列**：`pct_above_ma60 / regime / pattern / obv_grad_pct / volume_ratio`；旧库自动 ALTER。
  **历史行需重扫填充**（`run_full_market_scan` 重跑对应 scan_date），否则新列全 NULL → 三档降级到纯旧键。
- **三档降级**：`candidate_order_by` 检测新列缺失自动回退旧档位键 → 退化到纯 ext ASC，**绝不崩**。
- **接口 `main_n` + `reserve_n`**：`/api/investor/stock_deep/market` 返回双层切片（主推+储备），前端不写死 4。
- **每日推荐上限 4**（2026-09-05 改）：固定，不做资金自适应。前端双层：4 主推（高亮徽章+is-main 卡片）+ 6 储备（默认收起，localStorage.aiq_sd_more 记忆展开态）。

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

## 数据源 · 东财/baostock 风控与降级
- **`clist/hist` 旧接口 404**（2026-08-21 起稳定 daily 404，已被两级降级吸收）：
  `em_realtime._do_fetch` except → `_fetch_klines_by_date_tencent`（qt.gtimg.cn 500 只/批 ≈10s 全 A）
  → 再降级 akshare。4419 只扫描无影响，**勿再修**。
- **5 指数串行易触发 RemoteDisconnected**（2026-09-04 实测）：5 × stock/kline/get 在 ~30s 窗口
  被东财主动 RST，3 次 retry 用尽仍失败。**已在 `core/sync.py:268` `sync_indices_by_date`
  for 内加 `time.sleep(0.4)` 错峰**（`import time` L12 已存在），baostock fallback 未动；零业务回归。
- **`fetch_index_klines` 无 SQLite 缓存**（仅 HTTP 直连）—— 脆弱性根源，但既有 `index_daily` 表 +
  baostock fallback 已天然兜底（dashboard `MAX(trade_date)` 自动回退 T-1），无需额外加缓存兜底。

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
*最后更新: 2026-09-05（新增"东财/baostock 风控与降级"章节：5 指数 sleep 0.4 错峰落地）*
