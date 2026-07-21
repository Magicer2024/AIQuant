"""
backtest/nl_parser.py —— 回测自然语言意图解析

把用户的一句话策略描述解析为可视化回测的两类输入：
  - conditions: 选股条件，结构与 backtest/conditions.py catalog 对齐
                [{category, indicator, operator, params: {key: value}}]
  - params:     回测参数（起止日期/止盈止损/持股天数/资金/持仓数/板块排除）

主路径走 LLM（utils/llm_client，config/llm_config.yaml）；
无密钥或调用失败时降级为本地正则提取（仅识别止损/止盈/持股天数/
回测区间/初始资金等参数），条件留空并在 note 中提示。
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from backtest.conditions import find_indicator, get_catalog
from utils.llm_client import chat_completion, is_llm_available

# 回测参数白名单（LLM 输出只认这些 key）
_PARAM_KEYS = {
    "start_date", "end_date", "initial_cash", "max_holdings", "max_buy_per_day",
    "take_profit_pct", "stop_loss_pct", "max_hold_days", "exclude_kcb", "exclude_cyb",
}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ──────────── LLM 主路径 ────────────

def _catalog_prompt_text() -> str:
    """把指标目录压缩成 prompt 文本（id/操作/参数）"""
    lines = []
    for cat in get_catalog():
        for it in cat["items"]:
            ops = ", ".join(o["id"] for o in it.get("operators", []))
            prm = ", ".join(f'{p["key"]}(默认{p.get("default")})' for p in it.get("params", []))
            lines.append(f'- {it["id"]}「{it["name"]}」操作[{ops}]' + (f" 参数{{{prm}}}" if prm else ""))
    return "\n".join(lines)


def _build_messages(text: str) -> List[Dict[str, str]]:
    today = date.today().isoformat()
    system = f"""你是 A 股量化回测参数解析助手。用户用自然语言描述回测策略，你解析为结构化 JSON。

【选股条件指标目录】（indicator=指标id，操作=可选operator id，参数=params可用key）
{_catalog_prompt_text()}

【回测参数】（均为可选，未提及就不要输出）
- start_date/end_date: 回测区间，YYYY-MM-DD 格式；今天是 {today}，"近一年/近半年/近3月"等请换算成绝对日期
- initial_cash: 初始资金（元，"50万"→500000）；max_holdings: 持仓上限只数；max_buy_per_day: 单日买入上限只数
- take_profit_pct: 止盈（小数，15%→0.15）；stop_loss_pct: 止损（正小数，5%→0.05）
- max_hold_days: 最大持股天数（整数）；exclude_kcb/exclude_cyb: 排除科创板/创业板（布尔）

【输出契约】只输出一个 JSON 对象，禁止任何其他文字：
{{"conditions":[{{"indicator":"指标id","operator":"操作id","params":{{"key":数值}}}}],"params":{{...}},"explanation":"一句中文策略解读"}}
- indicator/operator 必须严格使用目录中的 id；条件之间为 AND 关系；未提及的选股条件不要编造"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": text},
    ]


def _extract_json(raw: str) -> Dict[str, Any]:
    """从 LLM 输出中提取 JSON 对象（容错 markdown fence）"""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    # 截取首个 { 到末个 }，防止首尾杂讯
    i, j = raw.find("{"), raw.rfind("}")
    if i >= 0 and j > i:
        raw = raw[i:j + 1]
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("LLM 输出不是 JSON 对象")
    return data


def _validate_conditions(raw_list: Any, warnings: List[str]) -> List[Dict[str, Any]]:
    """按 catalog 校验条件：非法指标/操作符丢弃，缺省参数补默认值"""
    out: List[Dict[str, Any]] = []
    if not isinstance(raw_list, list):
        return out
    for item in raw_list[:8]:  # 上限 8 条，防失控
        if not isinstance(item, dict):
            continue
        ind_id = item.get("indicator")
        meta = find_indicator(ind_id) if isinstance(ind_id, str) else None
        if not meta:
            warnings.append(f"已忽略未知指标：{ind_id}")
            continue
        op_ids = [o["id"] for o in meta.get("operators", [])]
        operator = item.get("operator")
        if operator not in op_ids:
            warnings.append(f"指标 {ind_id} 的操作符 {operator} 非法，已用默认 {op_ids[0]}")
            operator = op_ids[0]
        raw_params = item.get("params") if isinstance(item.get("params"), dict) else {}
        params: Dict[str, Any] = {}
        for p in meta.get("params", []):
            v = raw_params.get(p["key"], p.get("default"))
            try:
                v = float(v) if p.get("type") == "float" else int(float(v))
                if p.get("min") is not None:
                    v = max(v, p["min"])
                if p.get("max") is not None:
                    v = min(v, p["max"])
            except (TypeError, ValueError):
                v = p.get("default")
                warnings.append(f"指标 {ind_id} 参数 {p['key']} 非法，已用默认 {v}")
            params[p["key"]] = v
        out.append({
            "category": _category_of(ind_id),
            "indicator": ind_id,
            "operator": operator,
            "params": params,
        })
    return out


def _category_of(indicator_id: str) -> str:
    for cat in get_catalog():
        for it in cat["items"]:
            if it["id"] == indicator_id:
                return cat["category"]
    return "technical"


def _validate_params(raw: Any, warnings: List[str]) -> Dict[str, Any]:
    """回测参数白名单校验与类型归一"""
    out: Dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if k not in _PARAM_KEYS or v is None:
            continue
        try:
            if k in ("start_date", "end_date"):
                s = str(v).strip().replace("/", "-")
                if _DATE_RE.match(s):
                    out[k] = s
                else:
                    warnings.append(f"日期 {k}={v} 格式非法，已忽略")
            elif k in ("exclude_kcb", "exclude_cyb"):
                out[k] = bool(v)
            elif k in ("max_holdings", "max_buy_per_day", "max_hold_days"):
                iv = int(float(v))
                if iv > 0:
                    out[k] = iv
            elif k == "initial_cash":
                fv = float(v)
                if fv > 0:
                    out[k] = fv
            elif k in ("take_profit_pct", "stop_loss_pct"):
                fv = abs(float(v))
                if fv > 1:  # 用户/模型给了百分数（15 → 0.15）
                    fv = fv / 100.0
                if 0 < fv <= 5:
                    out[k] = round(fv, 4)
        except (TypeError, ValueError):
            warnings.append(f"参数 {k}={v} 非法，已忽略")
    return out


# ──────────── 本地兜底（无 LLM 时） ────────────

def _local_fallback(text: str) -> Dict[str, Any]:
    """本地正则参数提取：只能识别常见回测参数，选股条件需 LLM"""
    params: Dict[str, Any] = {}
    today = date.today()

    m = re.search(r"止损\s*(\d+\.?\d*)\s*%?", text)
    if m:
        params["stop_loss_pct"] = round(float(m.group(1)) / 100, 4)
    m = re.search(r"止盈\s*(\d+\.?\d*)\s*%?", text)
    if m:
        params["take_profit_pct"] = round(float(m.group(1)) / 100, 4)
    m = re.search(r"(?:持股|持有|拿)\s*(?:不超过|最多|≤)?\s*(\d+)\s*(?:天|日)", text)
    if m:
        params["max_hold_days"] = int(m.group(1))
    m = re.search(r"(?:资金|本金)\s*(\d+\.?\d*)\s*(万|千)?", text)
    if m:
        mult = {"万": 10000, "千": 1000}.get(m.group(2), 1)
        params["initial_cash"] = float(m.group(1)) * mult
    m = re.search(r"(?:持仓|最多买|买入)[^\d]{0,4}(\d+)\s*只", text)
    if m:
        params["max_holdings"] = int(m.group(1))

    m = re.search(r"近\s*(?:(\d+)|(半))?\s*(年|个月|月|周|天|日)", text)
    if m:
        num_s, half, unit = m.group(1), m.group(2), m.group(3)
        n = float(num_s) if num_s else (0.5 if half else 1.0)
        days = n * 365 if unit == "年" else n * 30 if unit in ("个月", "月") \
            else n * 7 if unit == "周" else n
        params["start_date"] = (today - timedelta(days=int(days))).isoformat()
        params["end_date"] = today.isoformat()

    if "科创板" in text and ("排除" in text or "不要" in text or "剔除" in text):
        params["exclude_kcb"] = True
    if "创业板" in text and ("排除" in text or "不要" in text or "剔除" in text):
        params["exclude_cyb"] = True

    return {
        "success": True,
        "source": "local",
        "conditions": [],
        "params": params,
        "explanation": "",
        "warnings": [],
        "note": "LLM 未配置（设置环境变量 LLM_API_KEY 后可 AI 解析选股条件），本地仅提取了回测参数",
    }


# ──────────── 入口 ────────────

def parse_backtest_intent(text: str) -> Dict[str, Any]:
    """解析自然语言回测描述

    :return: {success, source: "llm"|"local", conditions, params,
              explanation, warnings, note?}
    """
    text = (text or "").strip()
    if not text:
        return {"success": False, "error": "请输入策略描述"}
    if len(text) > 500:
        text = text[:500]

    if not is_llm_available():
        return _local_fallback(text)

    try:
        raw = chat_completion(_build_messages(text), temperature=0.1, json_mode=True)
        parsed = _extract_json(raw)
    except Exception as e:
        result = _local_fallback(text)
        result["note"] = f"LLM 调用失败（{type(e).__name__}），已降级为本地参数提取"
        return result

    warnings: List[str] = []
    conditions = _validate_conditions(parsed.get("conditions"), warnings)
    params = _validate_params(parsed.get("params"), warnings)
    explanation = str(parsed.get("explanation") or "").strip()

    if not conditions and not params:
        return {
            "success": False,
            "error": "未能从描述中解析出任何条件或参数，请补充指标/参数信息后重试",
            "source": "llm",
        }
    return {
        "success": True,
        "source": "llm",
        "conditions": conditions,
        "params": params,
        "explanation": explanation,
        "warnings": warnings,
    }
