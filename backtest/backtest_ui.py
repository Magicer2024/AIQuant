"""backtest_ui.py —— 回测结果 UI 渲染"""
import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import os


# ─── 颜色常量（A股红涨绿跌）───
COLOR_UP    = "#f85149"   # 红色=涨/正收益
COLOR_DOWN  = "#3fb950"   # 绿色=跌/负收益
COLOR_BG    = "#0d1117"
COLOR_SURF  = "#161b22"
COLOR_TEXT  = "#e6edf3"
COLOR_TEXT2 = "#8b949e"


def _fmt_pct(val, plus=True):
    """格式化百分比值，红涨绿跌"""
    if val is None or (isinstance(val, float) and (pd.isna(val) or abs(val) == float('inf'))):
        return "—"
    sign = "+" if plus and val >= 0 else ""
    color = COLOR_UP if val >= 0 else COLOR_DOWN
    return f'<span style="color:{color};font-weight:600">{sign}{val:.2f}%</span>'


def _fmt_money(val):
    """格式化金额"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "—"
    return f"¥{val:,.0f}"


# ─── 预设策略列表 ───
PRESET_OPTIONS = [
    "5日线突破（3天内收在5日线上）",
    "均线多头 + 放量",
    "超跌反弹（5日跌幅 > 8%）",
    "量价背离（量减价稳）",
    "融合信号高分选股",
]


# ─── 预设策略 Tab ───
def render_preset_tab():
    """渲染预设策略选择 Tab，返回 (选中的策略名, 索引)"""
    st.markdown("**🎯 选择策略模板**")
    preset_idx = st.selectbox(
        "预设策略",
        range(len(PRESET_OPTIONS)),
        format_func=lambda i: PRESET_OPTIONS[i],
        label_visibility="collapsed",
    )
    selected = PRESET_OPTIONS[preset_idx]
    st.caption(_preset_desc(selected))
    return selected, preset_idx


def _preset_desc(name: str) -> str:
    descs = {
        "5日线突破（3天内收在5日线上）": (
            "- 5日线 > 10日线\n"
            "- 近2日振幅 < 5%（稳）\n"
            "- 当日振幅 < 8%\n"
            "- 换手率 1%~6%（活跃但不炒作）\n"
            "- 排序：周成交量比（大→小）"
        ),
        "均线多头 + 放量": (
            "- 收盘在 MA5/10/20 之上（多头排列）\n"
            "- 今日量能 > 5日均量 × 1.5（放量）\n"
            "- 排序：融合信号分（高→低）"
        ),
        "超跌反弹（5日跌幅 > 8%）": (
            "- 近5日跌幅 > 8%（超跌）\n"
            "- 当日收盘 > 当日最低（不是跌停）\n"
            "- 换手率 > 1%（有承接）\n"
            "- 振幅 < 10%（避免跌停板）\n"
            "- 排序：5日跌幅（跌最多优先）"
        ),
        "量价背离（量减价稳）": (
            "- 近2日价格变动 < 3%（价稳）\n"
            "- 近5日量/均量 < 0.8（缩量）\n"
            "- 收盘在 MA20 以上（趋势向上）\n"
            "- 排序：融合信号分（高→低）"
        ),
        "融合信号高分选股": (
            "- 融合信号分 ≥ 15（5策略综合评分 0-50）\n"
            "- 排序：融合分（高→低）"
        ),
    }
    return descs.get(name, "")


# ─── 自定义条件 Tab（可视化条件构建器）──
def render_custom_tab() -> dict:
    """
    渲染可视化条件构建器 Tab。
    返回：{
        "mode": "condition_builder",
        "groups": list[ConditionGroup],
    }
    """
    from backtest.condition_builder import render_condition_builder, ConditionGroup

    groups = render_condition_builder()
    return {"mode": "condition_builder", "groups": groups}


# ─── 自定义策略代码 Tab ───
def render_code_tab() -> tuple[str, dict]:
    """渲染自定义策略代码 Tab，返回 (代码字符串, code_params字典)"""
    st.markdown("**💻 编写选股策略函数**")
    st.caption(
        "函数签名：`def strategy(df, stock_code=None, **params) -> pd.DataFrame`\n\n"
        "返回 DataFrame 必须含 `signal` 列（值为 True/False 表示是否选入）。\n"
        "可用的 df 列：open/high/low/close/volume/amount/pct_change/turnover，\n"
        "以及 add_indicators() 计算的所有指标（ma5/ma10/vol_ma5/fusion_score 等）。"
    )

    default_code = '''def strategy(df, stock_code=None, **params):
    """
    示例：放量突破策略
    选股条件：收盘在 MA5 以上 + 成交量 > 1.5 倍均量
    """
    df = df.copy()
    df["ma5"]  = df["close"].rolling(5).mean()
    df["vol_ma5"] = df["volume"].rolling(5).mean()

    # 信号：收盘在 MA5 以上 且 放量
    df["signal"] = (df["close"] > df["ma5"]) & (df["volume"] > 1.5 * df["vol_ma5"])

    return df[["signal"]]
'''

    code = st.text_area(
        "策略代码",
        value=default_code,
        height=300,
        label_visibility="collapsed",
        key="strategy_code_area",
    )
    st.caption("💡 按 Ctrl+Enter 或点击「开始回测」运行")
    return code, {}


# ─── CSV扫描结果 Tab ──────────────────────────────────────────────────────────
def render_csv_tab() -> dict:
    """
    渲染 CSV 扫描结果标签页。
    - 读取 scan_results/full_scan_results.csv
    - 以表格展示所有组合（可按列排序筛选）
    - 点击行可预览 + 填充到可视化条件构建器
    返回：{"mode": "csv", "selected": {...} 或 None, "fill_success": bool}
    """
    # ── 模式切换：单股测试 vs 多股组合 ──────────────────────────────────
    mode = st.radio(
        "回测模式",
        ["🧪 单股测试", "📊 多股组合"],
        horizontal=True,
        index=0,
        help="单股：每只股独立回测，排序/持仓参数不影响结果\n多股：跨截面选股，按排序字段取TopN，真正模拟组合管理",
    )
    is_multi = (mode == "📊 多股组合")

    if is_multi:
        CSV_PATH = "e:/小项目/jiaoyi/scan_results/full_scan_multi.csv"
        scan_cmd = "python scripts/diag/full_scan_multi.py"
        label = "多股组合模式（跨截面选股·近2年）"
    else:
        CSV_PATH = "e:/小项目/jiaoyi/scan_results/full_scan_v3.csv"
        if not os.path.exists(CSV_PATH):
            CSV_PATH = "e:/小项目/jiaoyi/scan_results/full_scan_results_t1.csv"
        scan_cmd = "python scripts/diag/full_scan_v3.py"
        label = "单股测试模式（T+1·近2年·参数化）"

    st.markdown(f"**📂 从扫描结果 CSV 导入条件**（{label}）")

    # 1. 检查文件
    if not os.path.exists(CSV_PATH):
        st.warning(f"⚠️ 未找到扫描结果文件：`{CSV_PATH}`")
        st.info(f"💡 请先运行以下命令生成扫描结果：")
        st.code(scan_cmd, language="bash")
        return {"mode": "csv", "selected": None, "fill_success": False}

    # 2. 读取CSV
    try:
        df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")
    except Exception as e:
        st.error(f"读取CSV失败：{e}")
        return {"mode": "csv", "selected": None, "fill_success": False}

    st.caption(f"📊 共 {len(df)} 个组合，文件：`{CSV_PATH}`")

    # 3. 筛选控件
    col_f1, col_f2, col_f3, col_f4 = st.columns([1, 1, 1, 2])
    with col_f1:
        min_wr = st.number_input(
            "最低胜率(%)", 0.0, 100.0, 0.0, 1.0, key="csv_min_wr"
        )
    with col_f2:
        min_ret = st.number_input(
            "最低总收益(%)", -100.0, 100000.0, -100.0, 100.0, key="csv_min_ret"
        )
    with col_f3:
        min_geo = st.number_input(
            "最低几何均盈(%)", -20.0, 100.0, 0.0, 0.5, key="csv_min_geo"
        )
    with col_f4:
        sort_options = [
            ("综合评分（高→低）", "综合评分", False),
            ("总收益（高→低）", "总收益率(%)", False),
            ("胜率（高→低）", "胜率(%)", False),
            ("几何均盈（高→低）", "几何均盈(%)", False),
            ("盈亏比（高→低）", "盈亏比", False),
            ("夏普比（高→低）", "夏普比(估)", False),
            ("交易次数（多→少）", "交易次数", False),
        ]
        sort_idx = st.selectbox(
            "排序方式", range(len(sort_options)),
            format_func=lambda x: sort_options[x][0], key="csv_sort"
        )
        sort_key_col = sort_options[sort_idx][1]
        sort_asc = sort_options[sort_idx][2]

    # 4. 筛选
    mask = (df["胜率(%)"] >= min_wr) & (df["总收益率(%)"] >= min_ret)
    if "几何均盈(%)" in df.columns:
        mask = mask & (df["几何均盈(%)"] >= min_geo)
    filtered = df[mask].sort_values(sort_key_col, ascending=sort_asc)
    st.caption(f"筛选后 **{len(filtered)}** / {len(df)} 个组合")

    if filtered.empty:
        st.info("当前筛选条件下无结果，请降低筛选阈值。")
        return {"mode": "csv", "selected": None, "fill_success": False}

    # 5. 分正负收益展示（各自限 400 行，避免 Styler 超限）
    # 单股模式无候选排序，列无意义；多股模式保留
    _base_cols = [
        "胜率(%)", "总收益率(%)", "几何均盈(%)",
        "单笔均盈(%)", "单笔均亏(%)", "盈亏比", "夏普比(估)",
        "交易次数", "持股天数", "止损(%)", "止盈(%)", "持仓上限", "条件"
    ]
    disp_cols = _base_cols + (["候选排序"] if is_multi else [])

    pos_df = filtered[filtered["总收益率(%)"] >= 0].head(400)
    neg_df = filtered[filtered["总收益率(%)"] <  0].sort_values("总收益率(%)", ascending=False).head(400)

    _fmt = {
        "胜率(%)":     "{:.1f}",
        "总收益率(%)":  "{:+.1f}",
        "几何均盈(%)":  "{:+.3f}",
        "单笔均盈(%)":  "{:+.2f}",
        "单笔均亏(%)":  "{:+.2f}",
        "盈亏比":       "{:.2f}",
        "夏普比(估)":  "{:.2f}",
    }
    _hl_cols = ["胜率(%)", "总收益率(%)", "几何均盈(%)", "盈亏比"]

    def _render_table(sub_df, title, hl_color):
        avail = [c for c in disp_cols if c in sub_df.columns]
        hl_sub = [c for c in _hl_cols if c in avail]
        st.markdown(f"**{title}** — {len(sub_df)} 行")
        if sub_df.empty:
            st.info("无数据")
            return
        st.dataframe(
            sub_df[avail].style.highlight_max(
                subset=hl_sub, color=hl_color, axis=0
            ).format({k: v for k, v in _fmt.items() if k in avail}),
            use_container_width=True,
            hide_index=True,
            height=280,
        )

    tab_pos, tab_neg = st.tabs([
        f"🔴 正收益组合（{len(filtered[filtered['总收益率(%)'] >= 0])}）",
        f"🟢 负收益组合（{len(filtered[filtered['总收益率(%)'] <  0])}）",
    ])
    with tab_pos:
        _render_table(pos_df, "正收益组合（按总收益率降序，最多400行）", "#c0392b")   # 红
    with tab_neg:
        _render_table(neg_df, "负收益组合（总收益率从高到低，最多400行）", "#27ae60")  # 绿

    # 6. 选择控件
    st.divider()
    st.markdown("**🎯 选中一行填充到条件构建器**")

    row_col1, row_col2, row_col3 = st.columns([1, 1, 1])
    with row_col1:
        max_row = max(1, len(filtered))
        row_num = st.number_input(
            "排名号（1=收益率最高）", 1, max_row, 1, 1, key="csv_row_num"
        )
    with row_col2:
        cond_search = st.text_input(
            "或输入条件关键词搜索", "", key="csv_cond_search",
            placeholder="如：RSI6 超跌 布林..."
        )
    with row_col3:
        st.markdown("")
        st.markdown("")
        do_fill = st.button(
            "✅ 填充到条件构建器", type="primary",
            use_container_width=True, key="csv_select_btn"
        )

    # 关键词搜索
    if cond_search:
        matched = filtered[filtered["条件"].str.contains(cond_search, na=False)]
        if not matched.empty:
            st.success(f"找到 {len(matched)} 个匹配「{cond_search}」的条件")
            for _, row in matched.head(5).iterrows():
                idx = filtered.index.get_loc(row.name) + 1
                st.markdown(
                    f"  #{idx} | 胜率{row['胜率(%)']}% | "
                    f"收益{row['总收益率(%)']}% | {row['条件']}"
                )
        else:
            st.warning(f"无匹配「{cond_search}」的条件")

    # 7. 执行填充
    fill_success = False
    selected_row = None

    if do_fill:
        if cond_search:
            matched = filtered[filtered["条件"].str.contains(cond_search, na=False)]
            if not matched.empty:
                selected_row = matched.iloc[0]
        else:
            if row_num <= len(filtered):
                selected_row = filtered.iloc[row_num - 1]

        if selected_row is not None:
            cond_str = selected_row.get("条件", "")
            st.session_state["_csv_sel_cond"] = cond_str
            st.session_state["_csv_sel_data"] = selected_row.to_dict()
            st.session_state["_csv_filled_flag"] = True
            fill_success = True

    # 8. 展示选中行的详细信息并解析
    if st.session_state.get("_csv_filled_flag"):
        st.session_state["_csv_filled_flag"] = False
        cond_str = st.session_state.pop("_csv_sel_cond", "")
        sel_data = st.session_state.pop("_csv_sel_data", {})
        fill_success = True

        st.success(f"**已选择条件：{cond_str}**")
        # 指标卡片
        m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
        with m1:
            st.metric("胜率", f"{sel_data.get('胜率(%)', 0):.1f}%")
        with m2:
            st.metric("总收益", f"{sel_data.get('总收益率(%)', 0):+.1f}%")
        with m3:
            st.metric("几何均盈", f"{sel_data.get('几何均盈(%)', 0):+.3f}%")
        with m4:
            st.metric("盈亏比", f"{sel_data.get('盈亏比', 0):.2f}")
        with m5:
            st.metric("持股", f"{sel_data.get('持股天数', 0)}天")
        with m6:
            st.metric("止损/止盈", f"{sel_data.get('止损(%)', 0):.0f}%/{sel_data.get('止盈(%)', 0):.0f}%")
        with m7:
            st.metric("交易次数", f"{sel_data.get('交易次数', 0)}笔")

        # 解析条件
        st.markdown("**🔧 解析为可视化条件：**")
        try:
            from backtest.condition_builder import parse_condition_str, ConditionGroup
            groups = parse_condition_str(cond_str)
        except Exception as e:
            st.error(f"解析失败：{e}")
            groups = [ConditionGroup(group_id=0)]

        parsed_count = sum(len(g.conditions) for g in groups)

        if groups and parsed_count > 0:
            st.session_state.cb_groups = groups

            st.markdown(
                f'<div style="background:#1a3a1a;border:1px solid #3fb950;'
                f'border-radius:8px;padding:12px 16px;margin:8px 0">'
                f'✅ 成功解析出 <b>{len(groups)}</b> 个条件组，共 '
                f'<b>{parsed_count}</b> 个条件，已自动填充到下方「可视化条件组合」面板。'
                f'<br>👉 请切换到 <b>🛠️ 自定义条件组合</b> 标签页查看并微调。'
                f'</div>',
                unsafe_allow_html=True
            )

            # 展示解析出的条件
            for gi, grp in enumerate(groups):
                for ci, cond in enumerate(grp.conditions):
                    readable = cond.to_readable()
                    st.markdown(
                        f"&nbsp;&nbsp;&nbsp;&nbsp;"
                        f"**条件组 {chr(65+gi)} #{ci+1}:** `{readable}`"
                    )
        else:
            st.warning(
                "⚠️ 此条件包含无法自动解析的特殊逻辑（如「MACD金叉」「贴近布林下轨」），"
                "请手动在「可视化条件组合」面板中构建。"
            )
            st.session_state.cb_groups = [ConditionGroup(group_id=0)]

    return {
        "mode": "csv",
        "selected": selected_row,
        "fill_success": fill_success,
    }


# ─── 预设回测（存根，实际逻辑已在 strategy_backtest_app.py 内联）──
def run_preset_backtest(params, selected_preset, sort_key, sort_ascending):
    pass


# ─── 自定义回测（存根）──
def run_custom_backtest(params, groups):
    pass


# ══════════════════════════════════════════════════════════
#  回测结果总览（指标卡 + 资金曲线 + 回撤曲线）
# ══════════════════════════════════════════════════════════

def render_backtest_result(result: dict):
    """渲染回测结果：指标卡片 + Plotly 资金/回撤图 + 统计摘要"""
    if not result or result.get("error"):
        st.error(f"❌ {result.get('error', '回测无结果')}")
        return

    # ── 1. 核心指标卡片 ──────────────────────────────
    m1, m2, m3, m4, m5, m6 = st.columns(6)
    with m1:
        st.markdown(f'<div style="background:{COLOR_SURF};border-radius:10px;padding:14px;text-align:center;border:1px solid #30363d">'
                    f'<div class="metric-lbl" style="font-size:12px;color:{COLOR_TEXT2}">总收益</div>'
                    f'<div style="color:{COLOR_UP if result.get("total_return",0)>=0 else COLOR_DOWN};'
                    f'font-size:26px;font-weight:700">{result.get("total_return",0):+.2f}%</div></div>',
                    unsafe_allow_html=True)
    with m2:
        st.markdown(f'<div style="background:{COLOR_SURF};border-radius:10px;padding:14px;text-align:center;border:1px solid #30363d">'
                    f'<div class="metric-lbl" style="font-size:12px;color:{COLOR_TEXT2}">年化</div>'
                    f'<div style="color:{COLOR_UP if result.get("annual_return",0)>=0 else COLOR_DOWN};'
                    f'font-size:26px;font-weight:700">{result.get("annual_return",0):+.2f}%</div></div>',
                    unsafe_allow_html=True)
    with m3:
        st.markdown(f'<div style="background:{COLOR_SURF};border-radius:10px;padding:14px;text-align:center;border:1px solid #30363d">'
                    f'<div class="metric-lbl" style="font-size:12px;color:{COLOR_TEXT2}">最大回撤</div>'
                    f'<div style="color:{COLOR_DOWN};font-size:26px;font-weight:700">{result.get("max_drawdown",0):.2f}%</div></div>',
                    unsafe_allow_html=True)
    with m4:
        sr = result.get("sharpe", 0)
        sc = COLOR_UP if sr >= 1.5 else (COLOR_DOWN if sr < 0 else "#d4a72c")
        st.markdown(f'<div style="background:{COLOR_SURF};border-radius:10px;padding:14px;text-align:center;border:1px solid #30363d">'
                    f'<div class="metric-lbl" style="font-size:12px;color:{COLOR_TEXT2}">Sharpe</div>'
                    f'<div style="color:{sc};font-size:26px;font-weight:700">{sr:.2f}</div></div>',
                    unsafe_allow_html=True)
    with m5:
        wr = result.get("win_rate", 0)
        wc = COLOR_UP if wr >= 50 else COLOR_DOWN
        st.markdown(f'<div style="background:{COLOR_SURF};border-radius:10px;padding:14px;text-align:center;border:1px solid #30363d">'
                    f'<div class="metric-lbl" style="font-size:12px;color:{COLOR_TEXT2}">胜率</div>'
                    f'<div style="color:{wc};font-size:26px;font-weight:700">{wr:.1f}%</div></div>',
                    unsafe_allow_html=True)
    with m6:
        nt = result.get("total_trades", 0)
        st.markdown(f'<div style="background:{COLOR_SURF};border-radius:10px;padding:14px;text-align:center;border:1px solid #30363d">'
                    f'<div class="metric-lbl" style="font-size:12px;color:{COLOR_TEXT2}">交易次数</div>'
                    f'<div style="color:{COLOR_TEXT};font-size:26px;font-weight:700">{nt}</div></div>',
                    unsafe_allow_html=True)

    # ── 2. 辅助指标行 ────────────────────────────────
    if result.get("total_trades", 0) > 0:
        sm1, sm2, sm3, sm4 = st.columns(4)
        with sm1:
            st.caption(f"均盈 {_fmt_pct(result.get('avg_win_pct', 0))}")
        with sm2:
            st.caption(f"均亏 {_fmt_pct(result.get('avg_loss_pct', 0))}")
        with sm3:
            pr = result.get("profit_ratio", 0)
            st.caption(f"盈亏比 <span style='color:{COLOR_UP if pr>1 else COLOR_DOWN}'><b>{pr:.2f}</b></span>",
                      unsafe_allow_html=True)
        with sm4:
            init_c = result.get("init_capital", 0)
            final_c = result.get("final_capital", 0)
            st.caption(f"资金 {_fmt_money(init_c)} → {_fmt_money(final_c)}")

    # ── 3. 资金曲线 + 回撤曲线（Plotly 双轴图）──────
    eq_curve = result.get("equity_curve", [])
    if len(eq_curve) > 2:
        eq_df = pd.DataFrame(eq_curve)
        eq_df['date'] = pd.to_datetime(eq_df['date'])
        init = result.get("init_capital", eq_df['equity'].iloc[0])

        # 计算收益率序列和回撤
        eq_df['ret_pct'] = (eq_df['equity'] - init) / init * 100
        eq_df['peak'] = eq_df['equity'].cummax()
        eq_df['drawdown'] = (eq_df['equity'] - eq_df['peak']) / eq_df['peak'] * 100

        fig = make_subplots(
            rows=2, cols=1,
            shared_xaxes=True,
            vertical_spacing=0.06,
            row_heights=[0.7, 0.3],
        )

        # 上图：资金曲线 + 现金 + 持仓市值
        fig.add_trace(
            go.Scatter(x=eq_df['date'], y=eq_df['equity'],
                       name='总资产', line=dict(color='#58a6ff', width=2),
                       fillcolor='rgba(88,166,255,0.05)'),
            row=1, col=1,
        )
        if 'cash' in eq_df.columns:
            fig.add_trace(
                go.Scatter(x=eq_df['date'], y=eq_df['cash'],
                           name='现金', line=dict(color='#8b949e', width=1, dash='dot'),
                           opacity=0.7),
                row=1, col=1,
            )
        if 'pos_value' in eq_df.columns:
            fig.add_trace(
                go.Scatter(x=eq_df['date'], y=eq_df['pos_value'],
                           name='持仓市值', line=dict(color='#a371f7', width=1, dash='dot'),
                           opacity=0.7),
                row=1, col=1,
            )

        # 水平参考线：初始资金
        fig.add_hline(y=init, line_dash="dash", line_color="#484f58",
                     annotation_text=f"初始¥{init:,.0f}", annotation_font_size=10,
                     row=1, col=1)

        # 下图：回撤曲线（红色填充）
        fig.add_trace(
            go.Scatter(x=eq_df['date'], y=eq_df['drawdown'],
                       name='回撤%', fill='tozeroy',
                       fillcolor='rgba(248,81,73,0.25)',
                       line=dict(color='#f85149', width=1.5)),
            row=2, col=1,
        )
        fig.add_hline(y=0, line_dash="dot", line_color="#484f58", row=2, col=1)

        fig.update_layout(
            height=450,
            margin=dict(l=40, r=20, t=10, b=30),
            paper_bgcolor=COLOR_BG, plot_bgcolor=COLOR_BG,
            font=dict(size=11, color=COLOR_TEXT),
            legend=dict(font_size=10, orientation="h", yanchor="bottom", y=1.02,
                       bgcolor="rgba(22,27,34,0.8)"),
            xaxis2=dict(title="", gridcolor='#21262d', tickformat="%m/%d"),
            yaxis1=dict(title="资产(元)", gridcolor='#21262d', tickprefix="¥",
                       tickformat=","),
            yaxis2=dict(title="回撤%", gridcolor='#21262d',
                       ticksuffix="%"),
            hovermode="x unified",
        )

        st.plotly_chart(fig, use_container_width=True)

    elif len(eq_curve) <= 2 and not result.get("error"):
        st.info("📊 数据点不足，无法绘制资金曲线")


# ══════════════════════════════════════════════════════════
#  交易记录表格 + 退出原因统计
# ══════════════════════════════════════════════════════════

def render_trades_table(trades: pd.DataFrame | list):
    """渲染交易记录表 + 退出原因分布"""
    if trades is None or len(trades) == 0:
        st.info("📋 本次回测无交易记录")
        return

    df = pd.DataFrame(trades) if isinstance(trades, list) else trades.copy()

    # 按买入时间倒序（由近到远）
    if 'buy_date' in df.columns:
        df = df.sort_values('buy_date', ascending=False).reset_index(drop=True)

    # 格式化显示列
    display_cols = []
    for c in ["buy_date", "sell_date", "code", "name", "entry_price",
              "sell_price", "shares", "pnl", "ret_pct", "hold_days", "reason"]:
        if c in df.columns:
            display_cols.append(c)

    disp_df = df[display_cols].copy() if display_cols else df.copy()

    # 列名中文化 + 颜色格式化
    rename_map = {
        "buy_date": "买入日", "sell_date": "卖出日", "code": "代码",
        "name": "名称", "entry_price": "买入价", "sell_price": "卖出价",
        "shares": "股数", "pnl": "盈亏(¥)", "ret_pct": "收益率%",
        "hold_days": "持仓天", "reason": "退出原因",
    }
    disp_df.columns = [rename_map.get(c, c) for c in disp_df.columns]

    # 收益率着色
    if "收益率%" in disp_df.columns:
        disp_df["收益率%"] = disp_df["收益率%"].apply(
            lambda v: _fmt_pct(v) if pd.notna(v) else "—"
        )

    # 盈亏金额着色
    if "盈亏(¥)" in disp_df.columns:
        disp_df["盈亏(¥)"] = disp_df["盈亏(¥)"].apply(
            lambda v: f"<span style='color:{COLOR_UP if v>=0 else COLOR_DOWN};font-weight:600'>{v:+,.0f}</span>"
                  if pd.notna(v) else "—"
        )

    # 用 HTML 渲染表格（支持颜色）
    html_table = disp_df.to_html(escape=False, index=False, justify="center")
    styled = (
        f'<style>.trade-table{{width:100%;border-collapse:collapse;font-size:13px;}}'
        f'.trade-table th{{background:{COLOR_SURF};color:{COLOR_TEXT2};padding:8px;'
        f'border-bottom:1px solid #30363d;}}'
        f'.trade-table td{{padding:6px 8px;border-bottom:1px solid #21262d;color:{COLOR_TEXT};}}'
        f'.trade-table tr:hover{{background:#1c2333}}</style>'
        f'{html_table}'
    )
    st.markdown(styled, unsafe_allow_html=True)

    # ── 退出原因统计饼图 ─────────────────────────────
    if "reason" in df.columns:
        reason_counts = df["reason"].value_counts()
        if len(reason_counts) > 0:
            rc1, rc2 = st.columns([1, 2])
            with rc1:
                # 饼图
                colors_pie = ['#f85149', '#3fb950', '#d4a72c', '#58a6ff', '#a371f7',
                              '#f0883e', '#79c0ff']
                pie_fig = go.Figure(data=[go.Pie(
                    labels=reason_counts.index.tolist(),
                    values=reason_counts.values.tolist(),
                    hole=0.4,
                    marker_colors=colors_pie[:len(reason_counts)],
                    textinfo="label+percent+value",
                    textfont=dict(size=11, color=COLOR_TEXT),
                )])
                pie_fig.update_layout(
                    height=260, margin=dict(t=10, b=10, l=10, r=10),
                    paper_bgcolor=COLOR_BG, font=dict(color=COLOR_TEXT),
                    title=dict(text="退出原因分布", font_size=13),
                )
                st.plotly_chart(pie_fig, use_container_width=True)

            with rc2:
                # 各原因的胜率和平均收益
                st.markdown("**各退出原因表现**")
                reason_stats = []
                for reason_name, group in df.groupby("reason"):
                    wins = (group["pnl"] > 0).sum()
                    total = len(group)
                    avg_ret = group["ret_pct"].mean()
                    avg_pnl = group["pnl"].mean()
                    reason_stats.append({
                        "退出原因": reason_name,
                        "次数": total,
                        "胜率": f"{wins/total*100:.1f}%" if total > 0 else "—",
                        "均收益%": _fmt_pct(avg_ret),
                        "均盈亏¥": f"<span style='color:{COLOR_UP if avg_pnl>=0 else COLOR_DOWN}'>{avg_pnl:+,.0f}</span>"
                              if pd.notna(avg_pnl) else "—",
                    })
                stats_df = pd.DataFrame(reason_stats)
                stats_html = stats_df.to_html(escape=False, index=False, justify="center")
                st.markdown(
                    f'<style>.rs-table{{width:100%;border-collapse:collapse;font-size:12px;}}'
                    f'.rs-table th{{background:{COLOR_SURF};color:{COLOR_TEXT2};padding:6px;'
                    f'border:1px solid #30363d;}}'
                    f'.rs-table td{{padding:5px 8px;border:1px solid #21262d;}}'
                    f'</style>{stats_html}',
                    unsafe_allow_html=True
                )


# ══════════════════════════════════════════════════════════
#  每日持仓明细 + 候选股日历
# ══════════════════════════════════════════════════════════

def render_position_detail(result: dict):
    """渲染每日持仓明细、候选股日历和当前持仓"""
    if not result or result.get("error"):
        return

    daily_sel = result.get("daily_selected", [])
    equity_curve = result.get("equity_curve", [])
    final_positions = result.get("final_positions", [])

    tab_pos, tab_cal, tab_cur = st.tabs(["📊 每日权益明细", "📅 候选股日历", f"📦 当前持仓({len(final_positions)})"])

    # ── Tab1: 每日权益明细 ────────────────────────────
    with tab_pos:
        if equity_curve and len(equity_curve) > 0:
            eq_df = pd.DataFrame(equity_curve)
            eq_df.columns = ["日期", "总资产", "现金", "持仓市值"]
            # 收益率
            init_val = float(eq_df["总资产"].iloc[0]) if len(eq_df) > 0 else 1
            eq_df["收益率%"] = ((eq_df["总资产"] - init_val) / init_val * 100).round(2)
            # 着色
            eq_df["收益率%"] = eq_df["收益率%"].apply(_fmt_pct)

            # 显示表格（分页，避免过长）
            page_size = 50
            total_rows = len(eq_df)
            if total_rows > page_size:
                page = st.number_input(
                    "页码", min_value=1,
                    max_value=(total_rows + page_size - 1) // page_size,
                    value=1, step=1
                )
                start_i = (page - 1) * page_size
                end_i = start_i + page_size
                show_df = eq_df.iloc[start_i:end_i]
                st.caption(f"第 {start_i+1}-{min(end_i, total_rows)} 行 / 共 {total_rows} 行")
            else:
                show_df = eq_df

            # HTML 表格
            html_tbl = show_df.to_html(escape=False, index=False, justify="center")
            st.markdown(
                f'<style>.eq-tbl{{width:100%;border-collapse:collapse;font-size:12px;}}'
                f'.eq-tbl th{{background:{COLOR_SURF};color:{COLOR_TEXT2};padding:6px;'
                f'border:1px solid #30363d;}}'
                f'.eq-tbl td{{padding:4px 6px;border:1px solid #21262d;}}'
                f'</style>{html_tbl}',
                unsafe_allow_html=True
            )
        else:
            st.info("无权益数据")

    # ── Tab2: 候选股日历 ──────────────────────────────
    with tab_cal:
        if not daily_sel or len(daily_sel) == 0:
            st.info("无候选股数据")
            return

        cal_df = pd.DataFrame(daily_sel)

        # 过滤掉空记录
        has_code = cal_df["code"] != "" if "code" in cal_df.columns else True
        cal_valid = cal_df[has_code] if has_code.any() else cal_df

        if cal_valid.empty:
            st.info("所选时间段内无候选股票被筛选出")
            return

        st.caption(f"共 **{len(cal_valid)}** 条候选股记录，覆盖 "
                   f"{cal_valid['date'].nunique()} 个交易日")

        # 搜索过滤
        search_col, date_col = st.columns([2, 1])
        with search_col:
            search_q = st.text_input("🔍 搜索（代码/名称）", "", key="cal_search")
        with date_col:
            all_dates_sorted = sorted(cal_valid["date"].unique(), reverse=True)
            date_filter = st.selectbox(
                "日期筛选", options=["全部"] + list(all_dates_sorted[:30]),
                key="cal_date_flt"
            )

        filtered = cal_valid.copy()

        if search_q:
            mask = (filtered["code"].str.contains(search_q, case=False, na=False) |
                    filtered["name"].str.contains(search_q, case=False, na=False))
            filtered = filtered[mask]

        if date_filter != "全部":
            filtered = filtered[filtered["date"] == date_filter]

        if filtered.empty:
            st.warning("当前筛选条件下无结果")
            return

        # 选择展示列
        want_cols = ["date", "code", "name", "close", "pct_change",
                     "fusion_score", "sort_val", "vol_rank"]
        avail_cols = [c for c in want_cols if c in filtered.columns]
        disp_cal = filtered[avail_cols].copy()
        _cal_rename = {
            "date": "日期", "code": "代码", "name": "名称",
            "close": "收盘价", "pct_change": "涨跌幅%",
            "fusion_score": "融合分", "sort_val": "排序值",
            "vol_rank": "量排名"
        }
        disp_cal.columns = [_cal_rename.get(c, c) for c in disp_cal.columns]

        # 涨跌幅着色
        if "涨跌幅%" in disp_cal.columns:
            disp_cal["涨跌幅%"] = disp_cal["涨跌幅%"].apply(
                lambda v: _fmt_pct(v) if pd.notna(v) else "—"
            )

        # 分页
        cal_page_size = 80
        if len(disp_cal) > cal_page_size:
            cal_pg = st.number_input(
                "页码", min_value=1,
                max_value=(len(disp_cal) + cal_page_size - 1) // cal_page_size,
                value=1, key="cal_pg"
            )
            ci_s = (cal_pg - 1) * cal_page_size
            ci_e = ci_s + cal_page_size
            show_cal = disp_cal.iloc[ci_s:ci_e].reset_index(drop=True)
            st.caption(f"显示 {ci_s+1}-{min(ci_e, len(disp_cal))} / {len(disp_cal)} 条")
        else:
            show_cal = disp_cal

        cal_html = show_cal.to_html(escape=False, index=False, justify="center")
        st.markdown(
            f'<style>.cal-tbl{{width:100%;border-collapse:collapse;font-size:12px;}}'
            f'.cal-tbl th{{background:{COLOR_SURF};color:{COLOR_TEXT2};padding:6px;'
            f'border:1px solid #30363d;}}'
            f'.cal-tbl td{{padding:4px 6px;border:1px solid #21262d;}}'
            f'</style>{cal_html}',
            unsafe_allow_html=True
        )

    # ── Tab3: 当前持仓（回测结束时的实际持仓）──────────
    with tab_cur:
        if not final_positions:
            st.info("📦 回测结束时无持仓（已全部平仓）")
        else:
            fp_df = pd.DataFrame(final_positions)
            # 获取最后一天的收盘价
            eq = result.get("equity_curve", [])
            if eq:
                last_date = eq[-1].get("date", "")
                pos_value = eq[-1].get("pos_value", 0)
                cash = eq[-1].get("cash", 0)
                equity = eq[-1].get("equity", 0)
                st.caption(f"📌 回测最后一天 {last_date} | 总资产 ¥{equity:,.0f} = 现金 ¥{cash:,.0f} + 持仓 ¥{pos_value:,.0f}")

            # 计算每只持仓的当前市值和盈亏
            if 'close' not in fp_df.columns and eq:
                # 从equity_curve推断收盘价（持仓市值/股数）
                fp_df['当前市值'] = ''
                fp_df['浮盈(¥)'] = ''

            # 简化展示
            disp_cols = ['code', 'name', 'buy_date', 'entry_price', 'shares']
            avail = [c for c in disp_cols if c in fp_df.columns]
            fp_disp = fp_df[avail].copy()
            rename = {"code":"代码","name":"名称","buy_date":"买入日","entry_price":"买入价","shares":"股数"}
            fp_disp.columns = [rename.get(c,c) for c in fp_disp.columns]

            st.dataframe(fp_disp, use_container_width=True, hide_index=True)
            st.caption(f"共 {len(fp_disp)} 只持仓")





