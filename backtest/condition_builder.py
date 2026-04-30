"""
backtest/condition_builder.py
=============================
可视化条件构建器

用户通过勾选/配置的方式构建选股条件，后端自动生成对应的策略代码。
支持4种条件类型、多种比较符、条件组AND/OR组合。

条件类型:
  - point  : 单点值比较
  - all_n  : 最近N日都满足
  - any_n  : 最近N日出现过满足
  - stat_n : 统计值满足（均值/最大/最小/求和/标准差）
"""

import pandas as pd
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# 数据结构定义
# ─────────────────────────────────────────────────────────────────────────────

class CondType(Enum):
    POINT  = "point"    # 单点值
    ALL_N  = "all_n"   # 持续N日
    ANY_N  = "any_n"   # 出现过N日
    STAT_N = "stat_n"  # 统计值

    @property
    def label(self):
        return {"point": "单点值", "all_n": "持续N日", "any_n": "出现过N日", "stat_n": "统计值"}[self.value]


class Comparator(Enum):
    GT          = "gt"           # 大于
    GTE         = "gte"          # 大于等于
    LT          = "lt"           # 小于
    LTE         = "lte"          # 小于等于
    EQ          = "eq"           # 等于
    CROSS_ABOVE = "cross_above"  # 上穿
    CROSS_BELOW = "cross_below"  # 下穿
    BETWEEN     = "between"      # 区间

    @property
    def label(self):
        return {
            "gt": ">", "gte": "≥", "lt": "<", "lte": "≤",
            "eq": "=", "cross_above": "上穿", "cross_below": "下穿", "between": "区间",
        }[self.value]


class StatMethod(Enum):
    MEAN = "mean"
    MAX  = "max"
    MIN  = "min"
    SUM  = "sum"
    STD  = "std"

    @property
    def label(self):
        return {"mean": "均值", "max": "最大值", "min": "最小值", "sum": "求和", "std": "标准差"}[self.value]


def _get_registry():
    from backtest.indicator_engine import _REGISTRY
    return _REGISTRY


@dataclass
class SingleCondition:
    """单个选股条件"""
    cond_type:   CondType = CondType.POINT
    indicator:    str = "close"
    comparator:   Comparator = Comparator.GT
    # 阈值
    threshold_value: float = 0.0
    threshold_is_indicator: bool = False
    threshold_indicator: str = "close"
    # 区间
    range_low: float = 0.0
    range_high: float = 100.0
    # 时间窗口
    period: int = 5
    # 统计方式
    stat_method: StatMethod = StatMethod.MEAN

    def evaluate(self, df: pd.DataFrame) -> pd.Series:
        """计算此条件的布尔序列"""
        from backtest.indicator_engine import eval_condition
        reg = _get_registry()
        series = reg.calc(self.indicator, df)

        if self.comparator == Comparator.BETWEEN:
            thr = [self.range_low, self.range_high]
        elif self.threshold_is_indicator:
            thr = reg.calc(self.threshold_indicator, df)
        else:
            thr = self.threshold_value

        return eval_condition(
            series=series,
            cond_type=self.cond_type.value,
            comparator=self.comparator.value,
            threshold=thr,
            period=self.period,
            stat_method=self.stat_method.value if self.cond_type == CondType.STAT_N else None,
        )

    def to_readable(self) -> str:
        reg = _get_registry()
        ind_info = reg.get(self.indicator)
        ind_label = ind_info["label"] if ind_info else self.indicator

        if self.comparator == Comparator.BETWEEN:
            return f"{ind_label} 在 {self.range_low} ~ {self.range_high}"

        op_label = self.comparator.label
        if self.comparator in (Comparator.CROSS_ABOVE, Comparator.CROSS_BELOW):
            if self.threshold_is_indicator:
                ti_info = reg.get(self.threshold_indicator)
                thr_label = ti_info["label"] if ti_info else self.threshold_indicator
            else:
                thr_label = str(self.threshold_value)
            return f"{ind_label} {op_label} {thr_label}"

        thr_str = str(self.threshold_value) if not self.threshold_is_indicator else "指标值"
        if self.cond_type == CondType.POINT:
            return f"{ind_label} {op_label} {thr_str}"
        elif self.cond_type == CondType.ALL_N:
            return f"近{self.period}日 {ind_label} 持续 {op_label} {thr_str}"
        elif self.cond_type == CondType.ANY_N:
            return f"近{self.period}日 {ind_label} 出现过 {op_label} {thr_str}"
        elif self.cond_type == CondType.STAT_N:
            return f"近{self.period}日 {ind_label} 的{self.stat_method.label} {op_label} {thr_str}"
        return ind_label


@dataclass
class ConditionGroup:
    """条件组（组内条件按 AND/OR 组合）"""
    group_id: int = 0
    conditions: list[SingleCondition] = field(default_factory=list)
    group_op: str = "and"  # "and" | "or"

    def evaluate(self, df: pd.DataFrame) -> pd.Series:
        if not self.conditions:
            return pd.Series(True, index=df.index)
        results = [c.evaluate(df) for c in self.conditions]
        if self.group_op == "and":
            result = results[0]
            for r in results[1:]:
                result = result & r
        else:
            result = results[0]
            for r in results[1:]:
                result = result | r
        return result


# ─────────────────────────────────────────────────────────────────────────────
# 代码生成
# ─────────────────────────────────────────────────────────────────────────────

def build_strategy_function(groups: list[ConditionGroup],
                            warmup_period: int = 20) -> str:
    """
    将条件组转换为 strategy_function 代码字符串。
    生成的函数返回 df，含 'signal' 列（True/False）。
    """
    lines = [
        "import pandas as pd",
        "",
        "def strategy_function(df, stock_code=None, **params):",
        '    """可视化条件构建器生成的选股策略"""',
        "    df = df.copy()",
        "",
    ]

    for i, grp in enumerate(groups):
        glabel = chr(65 + i)
        lines.append(f"    # ── 条件组 {glabel} ──")
        for j, cond in enumerate(grp.conditions):
            var = f"cond_{i}_{j}"
            lines.append(f"    # {cond.to_readable()}")
            lines.append(f"    {var} = _c_{i}_{j}(df)")
        gvars = [f"cond_{i}_{j}" for j in range(len(grp.conditions))]
        if not gvars:
            # 条件组为空时，生成全True（不过滤）
            lines.append(f"    group_{i} = pd.Series(True, index=df.index)")
        elif grp.group_op == "and":
            lines.append(f"    group_{i} = {' & '.join(gvars)}")
        else:
            lines.append(f"    group_{i} = {' | '.join(gvars)}")
        lines.append("")

    gvars_all = [f"group_{i}" for i in range(len(groups))]
    if len(groups) == 1:
        final = gvars_all[0]
    else:
        final = " | ".join(gvars_all)

    lines.append(f"    df['signal'] = {final}")
    lines.append(f"    # 预热期屏蔽（前{warmup_period}日：跳过前 N 行信号）")
    lines.append(f"    if len(df) > {warmup_period}:")
    lines.append(f"        df.iloc[:{warmup_period}, df.columns.get_loc('signal')] = False")
    lines.append("    return df")
    lines.append("")
    lines.append("# ── 自动生成的辅助函数 ──\n")

    for i, grp in enumerate(groups):
        for j, cond in enumerate(grp.conditions):
            lines.append(_gen_helper(cond, f"_c_{i}_{j}"))

    return "\n".join(lines)


def _gen_helper(cond: SingleCondition, fname: str) -> str:
    reg = _get_registry()
    ind_label = (reg.get(cond.indicator) or {}).get("label", cond.indicator)
    stat_map = {"mean": "mean()", "max": "max()", "min": "min()",
                "sum": "sum()", "std": "std()"}

    lines = [
        f"def {fname}(df):",
        f'    """{ind_label} {cond.to_readable()}"""',
        f"    s = df['{cond.indicator}']",
    ]

    # ALL_N / ANY_N：先逐日计算布尔值，再对布尔序列做 rolling 聚合
    # 这样"持续N日 A > B"的语义是：每天都判断 A > B，过去N天全为 True
    # 而非"A 的N日最小值 > 今天的 B"（错误逻辑）
    if cond.cond_type in (CondType.ALL_N, CondType.ANY_N):
        op_map = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "=="}

        if cond.comparator == Comparator.BETWEEN:
            lines.append(f"    daily = (s >= {cond.range_low}) & (s <= {cond.range_high})")
        elif cond.comparator == Comparator.CROSS_ABOVE:
            # 穿越类型不适合持续N日，退化为单点判断
            lines.append("    p = s.shift(1)")
            if cond.threshold_is_indicator:
                lines.append(f"    daily = (s > df['{cond.threshold_indicator}']) & (p <= df['{cond.threshold_indicator}'].shift(1))")
            else:
                lines.append(f"    daily = (s > {cond.threshold_value}) & (p <= {cond.threshold_value})")
        elif cond.comparator == Comparator.CROSS_BELOW:
            lines.append("    p = s.shift(1)")
            if cond.threshold_is_indicator:
                lines.append(f"    daily = (s < df['{cond.threshold_indicator}']) & (p >= df['{cond.threshold_indicator}'].shift(1))")
            else:
                lines.append(f"    daily = (s < {cond.threshold_value}) & (p >= {cond.threshold_value})")
        else:
            op = op_map[cond.comparator.value]
            if cond.threshold_is_indicator:
                lines.append(f"    daily = s {op} df['{cond.threshold_indicator}']")
            else:
                lines.append(f"    daily = s {op} {cond.threshold_value}")

        # daily 是每天的布尔序列，再对其做窗口聚合
        if cond.cond_type == CondType.ALL_N:
            # 过去N天全为True → rolling sum == N
            lines.append(f"    return daily.rolling({cond.period}).sum() == {cond.period}")
        else:  # ANY_N
            # 过去N天至少出现1次True → rolling sum >= 1
            lines.append(f"    return daily.rolling({cond.period}).sum() >= 1")

        return "\n".join(lines)

    # STAT_N：对指标值做统计后再比较（语义：指标的N日统计值 op 阈值）
    if cond.cond_type == CondType.STAT_N:
        lines.append(f"    s = s.rolling({cond.period}).{stat_map[cond.stat_method.value]}")

    # POINT / STAT_N 的比较逻辑
    if cond.comparator == Comparator.BETWEEN:
        lines.append(f"    return (s >= {cond.range_low}) & (s <= {cond.range_high})")
    elif cond.comparator == Comparator.CROSS_ABOVE:
        lines.append("    p = s.shift(1)")
        if cond.threshold_is_indicator:
            lines.append(f"    return (s > df['{cond.threshold_indicator}']) & (p <= df['{cond.threshold_indicator}'].shift(1))")
        else:
            lines.append(f"    return (s > {cond.threshold_value}) & (p <= {cond.threshold_value})")
    elif cond.comparator == Comparator.CROSS_BELOW:
        lines.append("    p = s.shift(1)")
        if cond.threshold_is_indicator:
            lines.append(f"    return (s < df['{cond.threshold_indicator}']) & (p >= df['{cond.threshold_indicator}'].shift(1))")
        else:
            lines.append(f"    return (s < {cond.threshold_value}) & (p >= {cond.threshold_value})")
    else:
        op_map = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "=="}
        op = op_map[cond.comparator.value]
        if cond.threshold_is_indicator:
            lines.append(f"    return s {op} df['{cond.threshold_indicator}']")
        else:
            lines.append(f"    return s {op} {cond.threshold_value}")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Streamlit UI
# ─────────────────────────────────────────────────────────────────────────────

def render_condition_builder() -> list[ConditionGroup]:
    """
    渲染可视化条件构建器，返回条件组列表。
    使用 st.session_state.cb_groups 持久化状态。
    """
    import streamlit as st
    from backtest.indicator_engine import _REGISTRY

    reg = _REGISTRY
    cats = reg.by_category()

    # 扁平化指标选项
    all_ind_keys   = []
    all_ind_labels = {}
    for cat, inds in cats.items():
        for ind in inds:
            all_ind_keys.append(ind["key"])
            all_ind_labels[ind["key"]] = f"[{cat}] {ind['label']}"

    comp_options  = [c.value for c in Comparator]
    comp_labels   = {c.value: c.label for c in Comparator}
    stat_options  = [s.value for s in StatMethod]
    stat_labels   = {s.value: s.label for s in StatMethod}
    type_options  = [t.value for t in CondType]
    type_labels   = {t.value: t.label for t in CondType}

    # 初始化
    if "cb_groups" not in st.session_state:
        st.session_state.cb_groups = [ConditionGroup(group_id=0)]

    groups = st.session_state.cb_groups
    st.markdown("**🛠️ 可视化条件组合**")

    # 工具栏
    t1, t2, t3 = st.columns([1, 1, 1])
    with t1:
        if st.button("➕ 添加条件组", use_container_width=True):
            new_id = max(g.group_id for g in groups) + 1
            groups.append(ConditionGroup(group_id=new_id))
            st.session_state.cb_groups = groups
            st.rerun()
    with t2:
        if len(groups) > 1 and st.button("➖ 删除最后组", use_container_width=True):
            groups.pop()
            st.session_state.cb_groups = groups
            st.rerun()
    with t3:
        if st.button("🔄 重置", use_container_width=True):
            st.session_state.cb_groups = [ConditionGroup(group_id=0)]
            st.rerun()

    BORDER_COLORS = ["#58a6ff", "#3fb950", "#e3b341", "#f85149", "#bc8cff"]

    updated_groups = []

    for gi, grp in enumerate(groups):
        bc = BORDER_COLORS[gi % len(BORDER_COLORS)]
        glabel = chr(65 + gi)

        st.markdown(f"""
        <style>
        .cbg-{gi} {{ border: 2px solid {bc}; border-radius: 12px;
                      padding: 12px 16px; margin: 8px 0; background: #0d1117; }}
        </style>
        """, unsafe_allow_html=True)

        st.markdown(f'<div class="cbg-{gi}">', unsafe_allow_html=True)

        # 组标题行
        h1, h2, h3 = st.columns([1, 2, 1])
        with h1:
            st.markdown(f"**条件组 {glabel}**")
        with h2:
            grp.group_op = st.radio(
                "组内逻辑", ["and", "or"],
                index=0 if grp.group_op == "and" else 1,
                horizontal=True, key=f"gop_{gi}",
                format_func=lambda x: "AND 同时满足" if x == "and" else "OR 满足任一",
            )
        with h3:
            if len(groups) > 1 and st.button("删除此组", key=f"dg_{gi}", type="secondary"):
                groups.remove(grp)
                st.session_state.cb_groups = groups
                st.rerun()
                return

        st.divider()

        new_conds = []
        for ci, cond in enumerate(grp.conditions):
            # 指标 + 类型
            r1, r2, r3 = st.columns([2, 3, 1])

            with r1:
                # 条件类型
                ct_idx = type_options.index(cond.cond_type.value)
                cond.cond_type = CondType(
                    st.selectbox("类型", type_options, index=ct_idx,
                                key=f"ct_{gi}_{ci}", label_visibility="collapsed",
                                format_func=lambda x: type_labels[x])
                )
                # 指标
                ind_idx = all_ind_keys.index(cond.indicator) if cond.indicator in all_ind_keys else 0
                cond.indicator = st.selectbox(
                    "指标", all_ind_keys, index=ind_idx,
                    key=f"ind_{gi}_{ci}", label_visibility="collapsed",
                    format_func=lambda x: all_ind_labels[x]
                )

            with r2:
                # 比较符
                cmp_idx = comp_options.index(cond.comparator.value)
                cond.comparator = Comparator(
                    st.selectbox("比较符", comp_options, index=cmp_idx,
                                key=f"cmp_{gi}_{ci}", label_visibility="collapsed",
                                format_func=lambda x: comp_labels[x])
                )
                # 阈值输入
                if cond.comparator == Comparator.BETWEEN:
                    a, b, c_ = st.columns([1, 1, 2])
                    with a:
                        cond.range_low = st.number_input("下限", value=float(cond.range_low),
                            key=f"rl_{gi}_{ci}", label_visibility="collapsed", format="%.4f")
                    with b:
                        st.markdown("~")
                    with c_:
                        cond.range_high = st.number_input("上限", value=float(cond.range_high),
                            key=f"rh_{gi}_{ci}", label_visibility="collapsed", format="%.4f")
                else:
                    use_ind_thr = st.checkbox("阈值为指标", value=cond.threshold_is_indicator,
                                              key=f"tind_{gi}_{ci}")
                    cond.threshold_is_indicator = use_ind_thr
                    if use_ind_thr:
                        ti_idx = all_ind_keys.index(cond.threshold_indicator) if cond.threshold_indicator in all_ind_keys else 0
                        cond.threshold_indicator = st.selectbox(
                            "阈值指标", all_ind_keys, index=ti_idx,
                            key=f"thind_{gi}_{ci}", label_visibility="collapsed",
                            format_func=lambda x: all_ind_labels[x]
                        )
                    else:
                        default_val = float(cond.threshold_value)
                        cond.threshold_value = st.number_input(
                            "阈值", value=default_val if default_val else 0.0,
                            key=f"thv_{gi}_{ci}", label_visibility="collapsed", format="%.4f"
                        )

            with r3:
                st.markdown("")
                if st.button("🗑️", key=f"dc_{gi}_{ci}", help="删除"):
                    continue

            # 时间窗口参数
            need_win = cond.cond_type in (CondType.ALL_N, CondType.ANY_N, CondType.STAT_N)
            need_stat = cond.cond_type == CondType.STAT_N
            if need_win:
                if need_stat:
                    w1, w2 = st.columns([1, 2])
                else:
                    w1 = st.columns([1])[0]
                with w1:
                    cond.period = st.number_input("N日", 1, 120, cond.period,
                        key=f"per_{gi}_{ci}", label_visibility="collapsed")
                if need_stat:
                    with w2:
                        sm_idx = stat_options.index(cond.stat_method.value)
                        cond.stat_method = StatMethod(
                            st.selectbox("统计方式", stat_options, index=sm_idx,
                                        key=f"stm_{gi}_{ci}", label_visibility="collapsed",
                                        format_func=lambda x: stat_labels[x])
                        )

            st.caption(f"💡 {cond.to_readable()}")
            new_conds.append(cond)

        # 添加条件按钮
        ac1, ac2 = st.columns([1, 4])
        with ac1:
            if st.button(f"➕ 添加条件", key=f"ac_{gi}", use_container_width=True):
                grp.conditions.append(SingleCondition())
                st.session_state.cb_groups = groups
                st.rerun()

        grp.conditions = new_conds
        updated_groups.append(grp)
        st.markdown('</div>', unsafe_allow_html=True)

    st.session_state.cb_groups = updated_groups
    return updated_groups


# ─────────────────────────────────────────────────────────────────────────────
# 条件解析器（CSV扫描结果 → SingleCondition / ConditionGroup）
# ─────────────────────────────────────────────────────────────────────────────

def _build_indicator_alias_map():
    """构建别名 → 注册表key 的映射表（覆盖扫描CSV中的各种写法）"""
    from backtest.indicator_engine import _REGISTRY
    reg = _REGISTRY
    m = {}
    # 中文别名
    alias_map = {
        # 指标key → 别名列表
        "RSI6":     ["RSI6", "rsi6"],
        "RSI14":    ["RSI14", "rsi14"],
        "RSI12":    ["RSI12", "rsi12"],
        "KDJ_K":    ["KDJ_K", "KDJ"],
        "KDJ_J":    ["KDJ_J", "kdj_j"],
        "MACD":     ["MACD", "macd"],
        "MACD_DIF": ["MACD_DIF", "MACD_DIF", "DIF", "dif"],
        "威廉R":     ["威廉R", "威廉%R", "威廉", "WR", "威廉R"],
        "CCI":      ["CCI", "cci"],
        "量比":      ["量比", "vol_ratio", "量比>"],
        "换手率":    ["换手率", "turnover", "换手率<"],
        "close":    ["收盘", "收盘价"],
        "MA5":      ["MA5", "ma5", "收盘>MA"],
        "MA10":     ["MA10", "ma10"],
        "MA20":     ["MA20", "ma20"],
        "BOLL_LOWER": ["布林下轨", "BOLL_LOWER"],
        "BOLL_UPPER": ["布林上轨"],
        "BOLL_MID":  ["布林中轨"],
        "RSI":      ["RSI"],
    }
    # 从注册表动态添加
    for cat, inds in reg.by_category().items():
        for ind in inds:
            key = ind["key"]
            label = ind["label"]
            m[key] = key
            m[key.upper()] = key
            m[label] = key
            # 处理中文括号
            for v in alias_map.get(key, []):
                m[v] = key
    # 手动补充扫描CSV中出现的特殊写法
    extra = {
        "MACD>-0.05": "MACD",
        "MACD>-0.1":  "MACD",
        "MACD金叉":   "MACD",
        "收盘贴近布林下轨(1%)": "BOLL_LOWER",
        "收盘贴近布林下轨(0%)": "BOLL_LOWER",
        "收盘贴近布林下轨(-1%)": "BOLL_LOWER",
        "收盘>MA":    "MA5",
    }
    for v in extra:
        m[v] = extra[v]
    # 中文"跌幅"系列 → ret_Nd
    for n in [3, 5, 10, 20, 60]:
        m[f"近{n}日跌幅"] = f"ret_{n}d"
        m[f"{n}日跌幅"]   = f"ret_{n}d"
        m[f"跌幅{n}日"]   = f"ret_{n}d"
    return m


def _parse_single_condition(cond_str: str) -> SingleCondition | None:
    """
    将形如 "RSI6<20" 或 "近3日RSI6<30" 的字符串解析为 SingleCondition。
    返回 None 表示无法解析。
    """
    import re
    s = cond_str.strip()

    # ── 预处理：去除百分号后缀 ──────────────────────────────
    pct_match = re.search(r'([\u4e00-\u9fa5a-zA-Z0-9_<>=]+)(<|>)([0-9.\-]+)%', s)
    if pct_match:
        prefix, op, num = pct_match.group(1), pct_match.group(2), pct_match.group(3)
        s = s.replace(f'{op}{num}%', f'{op}{num}')
        s = prefix + op + num  # 重建无百分号版本

    # ── 预处理：去除括号内容（如 (1%) 贴近布林下轨） ──────────
    # "收盘贴近布林下轨(1%)" → indicator="BOLL_LOWER", op="<", thr=close*1.01 不可直接解析
    boll_match = re.match(r'收盘贴近布林下轨\(([+-]?\d+)%\)', s)
    if boll_match:
        # 转换为：(close - BOLL_LOWER) / BOLL_LOWER * 100 < pct
        # 这个条件需要两个指标比较，不属于单条件，跳过
        return None

    # ── MACD金叉 ────────────────────────────────────────────
    if s == "MACD金叉":
        c = SingleCondition(
            cond_type=CondType.POINT,
            indicator="MACD",
            comparator=Comparator.CROSS_ABOVE,
            threshold_value=0.0,
        )
        return c

    # ── 近N日跌幅模式：近3日跌幅>15% → ret_3d < -15 ──────────
    # 注意：这里指标是 ret_Nd（正数表示涨幅），但中文"跌幅"是负数
    # 所以比较方向要翻转：跌幅>15% → ret_3d < -15
    fall_match = re.match(
        r'(?:近|持续)(\d+)日?跌幅([<>])([0-9.]+)',
        s
    )
    if fall_match:
        period = int(fall_match.group(1))
        op_raw  = fall_match.group(2)
        thr_val = float(fall_match.group(3))
        # 中文"跌幅>15"（跌幅超过15%）→ ret_3d < -15
        # 中文"跌幅<15"（跌幅不足15%）→ ret_3d > -15（但这种情况极少）
        # 这里简化为：取绝对值变负
        threshold = -abs(thr_val)
        comp = Comparator.LT if op_raw == ">" else Comparator.GT
        return SingleCondition(
            cond_type=CondType.ALL_N,
            indicator=f"ret_{period}d",
            comparator=comp,
            threshold_value=threshold,
            period=period,
        )

    # ── 持续N日模式：近3日RSI6<30 或 持续3日RSI6<30 ──────────
    duration_match = re.match(
        r'(?:近|持续)(\d+)日([A-Za-z0-9_\u4e00-\u9fa5]+)([<>=]+)([0-9.\-]+)',
        s
    )
    if duration_match:
        period = int(duration_match.group(1))
        rest   = duration_match.group(2) + duration_match.group(3) + duration_match.group(4)
        # 递归解析 rest
        inner = _parse_indicator_condition(rest)
        if inner:
            inner.cond_type = CondType.ALL_N
            inner.period = period
            return inner

    # ── 通用：指标 + 比较符 + 阈值 ──────────────────────────
    return _parse_indicator_condition(s)


def _parse_indicator_condition(s: str):
    """解析 'INDICATOR<20' 或 'INDICATOR>0' 等简单格式"""
    import re

    # 尝试各种比较符
    m = re.match(r'([A-Za-z0-9_\u4e00-\u9fa5]+)([<>=]+)([0-9.\-]+)', s)
    if not m:
        return None

    raw_ind = m.group(1)
    op_str  = m.group(2)
    thr_str = m.group(3)

    # 归一化比较符
    op_map = {
        "<":  Comparator.LT,
        ">":  Comparator.GT,
        "<=": Comparator.LTE,
        ">=": Comparator.GTE,
        "=":  Comparator.EQ,
    }
    comp = op_map.get(op_str)
    if not comp:
        return None

    try:
        threshold = float(thr_str)
    except ValueError:
        threshold = 0.0

    # 指标名映射
    alias_map = _build_indicator_alias_map()
    indicator = alias_map.get(raw_ind)

    if not indicator:
        # 模糊匹配：大小写不敏感前缀匹配
        for key in alias_map:
            if raw_ind.lower() == key.lower():
                indicator = alias_map[key]
                break
            if raw_ind.upper().startswith(key.upper()):
                indicator = alias_map[key]
                break

    if not indicator:
        # 尝试从原始别名中找
        indicator = raw_ind  # 直接用原始值

    c = SingleCondition(
        cond_type=CondType.POINT,
        indicator=indicator,
        comparator=comp,
        threshold_value=threshold,
    )
    return c


def parse_condition_str(cond_str: str) -> list[ConditionGroup]:
    """
    将CSV扫描结果中的条件字符串（如 "近3日跌幅>15% AND RSI14<20"）
    解析为 ConditionGroup 列表（只含一个组，组内为AND关系）。
    无法解析的条件自动跳过。
    """
    cond_str = cond_str.strip()

    # 按 " AND " 分割
    parts = re.split(r'\s+AND\s+', cond_str, flags=re.IGNORECASE)

    group = ConditionGroup(group_id=0, group_op="and")

    for part in parts:
        part = part.strip().strip('()')
        if not part:
            continue
        cond = _parse_single_condition(part)
        if cond:
            group.conditions.append(cond)

    return [group]


# ── re import (在文件顶部已导入，这里确保可用) ──
import re

