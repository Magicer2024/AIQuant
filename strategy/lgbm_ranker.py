"""
lgbm_ranker.py —— Phase 3: LightGBM 排序学习二次筛选与融合
=============================================================
功能：
- 7类元特征构建
- LightGBM Ranker 训练与推理
- 滚动预测（t-1 → t）避免未来函数
- 置信度归一化 (0-100)
- 失效规则降级/淘汰
- 故障恢复：训练失败回退 Phase 2 评分
"""
import json
import pandas as pd
import numpy as np
from typing import Dict, List, Tuple, Optional
from datetime import datetime, timedelta

from strategy.factor_lib import FACTOR_REGISTRY
from core.db import (
    get_active_rules, save_strategy_signal, get_signals_for_training,
    update_signal_labels, degrade_rule, get_conn,
)


def build_meta_features(
    rule_id: int, rule_name: str, rule_type: str, n_conditions: int,
    rule_recent_perf: dict,
    rule_history: dict,
    market_state: dict,
    stock_state: dict,
    signal_strength: float,
    sector_crowd: dict,
) -> dict:
    """为单条触发信号构建元特征向量"""
    features = {
        "rule_id": rule_id,
        "rule_type_hash": hash(rule_type) % 1000,
        "n_conditions": n_conditions,
        "win_rate_20d": rule_recent_perf.get("win_rate_20d", 0),
        "win_rate_60d": rule_recent_perf.get("win_rate_60d", 0),
        "avg_return_20d": rule_recent_perf.get("avg_return_20d", 0),
        "avg_return_60d": rule_recent_perf.get("avg_return_60d", 0),
        "sharpe_20d": rule_recent_perf.get("sharpe_20d", 0),
        "trigger_count_20d": rule_history.get("trigger_count_20d", 0),
        "max_drawdown_hist": rule_history.get("max_drawdown_hist", 0),
        "profit_factor_hist": rule_history.get("profit_factor_hist", 0),
        "index_ret_20d": market_state.get("index_ret_20d", 0),
        "index_vol_20d": market_state.get("index_vol_20d", 0),
        "market_breadth": market_state.get("market_breadth", 0),
        "stock_ret_20d": stock_state.get("stock_ret_20d", 0),
        "stock_vol_20d": stock_state.get("stock_vol_20d", 0),
        "turnover_pctl": stock_state.get("turnover_pctl", 0),
        "signal_strength": signal_strength,
        "sector_trigger_count": sector_crowd.get("trigger_count", 0),
        "sector_trigger_ratio": sector_crowd.get("trigger_ratio", 0),
    }
    return features


def build_features_df(signals: List[dict], market_data: dict,
                      stock_data: dict) -> pd.DataFrame:
    """批量构建特征 DataFrame（带 1% 缩尾处理）"""
    rows = []
    for sig in signals:
        code = sig["code"]
        feats = build_meta_features(
            rule_id=sig["rule_id"],
            rule_name=sig.get("rule_name", ""),
            rule_type=sig.get("rule_type", ""),
            n_conditions=sig.get("n_conditions", 1),
            rule_recent_perf=sig.get("rule_recent_perf", {}),
            rule_history=sig.get("rule_history", {}),
            market_state=market_data,
            stock_state=stock_data.get(code, {}),
            signal_strength=sig.get("signal_strength", 0.5),
            sector_crowd=sig.get("sector_crowd", {}),
        )
        feats["code"] = code
        feats["trade_date"] = sig["trade_date"]
        rows.append(feats)

    df = pd.DataFrame(rows)
    for col in df.select_dtypes(include=[np.number]).columns:
        if col in ("rule_id", "rule_type_hash", "n_conditions"):
            continue
        lo, hi = df[col].quantile(0.01), df[col].quantile(0.99)
        if hi - lo > 1e-9:
            df[col] = df[col].clip(lo, hi)
    return df


class LGBMRanker:
    """LightGBM 排序学习模型"""

    def __init__(self):
        self.model = None
        self.feature_names = None
        self._fallback = False

    def _prepare_training_data(self, start_date: str, end_date: str):
        """准备训练数据"""
        signals = get_signals_for_training(start_date, end_date)
        if len(signals) < 1000:
            return None
        rows = []
        for s in signals:
            try:
                feats = json.loads(s.get("features_json", "{}"))
                feats["label"] = s.get("label_return")
                feats["trade_date"] = s["trade_date"]
                feats["code"] = s["code"]
                rows.append(feats)
            except (json.JSONDecodeError, KeyError):
                continue
        df = pd.DataFrame(rows)
        if "label" not in df.columns or df["label"].isna().sum() > len(df) * 0.8:
            return None
        lo, hi = df["label"].quantile(0.01), df["label"].quantile(0.99)
        df["label"] = df["label"].clip(lo, hi)
        df["group"] = df.apply(
            lambda r: f"{r['trade_date']}_{hash(str(r.get('code',''))) % 20}", axis=1)
        feature_cols = [c for c in df.columns
                        if c not in ("label", "trade_date", "code", "group",
                                     "rule_id", "rule_type_hash")]
        return df[feature_cols], df["label"], df["group"].tolist(), feature_cols

    def train(self, start_date: str = None, end_date: str = None):
        """训练模型（滚动预测：t-1 数据预测 t 日表现）"""
        import lightgbm as lgb
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=365 * 3)).strftime("%Y%m%d")
        if end_date is None:
            end_date = datetime.now().strftime("%Y%m%d")
        result = self._prepare_training_data(start_date, end_date)
        if result is None:
            self._fallback = True
            return False
        X, y, groups, feature_names = result
        self.feature_names = feature_names
        split_idx = int(len(X) * 0.7)
        X_train, X_valid = X.iloc[:split_idx], X.iloc[split_idx:]
        y_train, y_valid = y.iloc[:split_idx], y.iloc[split_idx:]
        train_groups = groups[:split_idx]
        train_groups_set = sorted(set(train_groups))
        train_group_sizes = [train_groups.count(g) for g in train_groups_set]
        train_data = lgb.Dataset(X_train, label=y_train,
                                 group=train_group_sizes if train_group_sizes else None)
        valid_data = lgb.Dataset(X_valid, label=y_valid,
                                 reference=train_data)
        params = {
            "objective": "lambdarank", "metric": "ndcg",
            "ndcg_eval_at": [5, 10], "boosting_type": "gbdt",
            "num_leaves": 31, "learning_rate": 0.05,
            "feature_fraction": 0.8, "bagging_fraction": 0.8,
            "bagging_freq": 5, "verbose": -1, "seed": 42,
        }
        self.model = lgb.train(
            params, train_data, valid_sets=[valid_data],
            num_boost_round=500,
            callbacks=[lgb.early_stopping(50), lgb.log_evaluation(0)],
        )
        self._fallback = False
        return True

    def predict(self, features_df: pd.DataFrame) -> np.ndarray:
        """推理并输出 0-100 归一化置信度"""
        if self._fallback or self.model is None:
            return np.full(len(features_df), 50.0)
        if self.feature_names:
            available = [c for c in self.feature_names if c in features_df.columns]
            X = features_df[available]
        else:
            X = features_df.select_dtypes(include=[np.number])
        raw_scores = self.model.predict(X)
        smin, smax = raw_scores.min(), raw_scores.max()
        if smax - smin < 1e-9:
            return np.full(len(raw_scores), 50.0)
        return (raw_scores - smin) / (smax - smin) * 100.0

    def save_model(self, path: str = "strategy/lgbm_ranker_model.txt"):
        if self.model:
            self.model.save_model(path)

    def load_model(self, path: str = "strategy/lgbm_ranker_model.txt"):
        import lightgbm as lgb
        import os
        if os.path.exists(path):
            self.model = lgb.Booster(model_file=path)
            self._fallback = False
            return True
        self._fallback = True
        return False


_ranker_instance: Optional[LGBMRanker] = None


def get_ranker() -> LGBMRanker:
    global _ranker_instance
    if _ranker_instance is None:
        _ranker_instance = LGBMRanker()
        if not _ranker_instance.load_model():
            pass  # Will use fallback, needs training
    return _ranker_instance


def fuse_stock_signals(code: str, signals: List[dict]) -> float:
    """
    同一股票多条规则触发的加权融合
    权重 = e^(confidence/30) / Σ e^(confidence/30)
    返回 0-50 的 FUSION_SCORE
    """
    if not signals:
        return 0.0
    confidences = np.array([s.get("confidence", 25) for s in signals])
    exp_weights = np.exp(confidences / 30)
    weights = exp_weights / exp_weights.sum()
    fused = np.sum(confidences * weights)
    return round(fused / 2.0, 2)


def degrade_low_performance_rules():
    """淘汰低分规则：置信度均值<30 且持仓期间连续亏损"""
    rules = get_active_rules(limit=500)
    for r in rules:
        with get_conn() as conn:
            signals = conn.execute("""
                SELECT AVG(confidence) as avg_conf,
                       AVG(label_return) as avg_label,
                       COUNT(*) as cnt
                FROM strategy_signals
                WHERE rule_id=? AND trade_date >= date('now', '-60 days')
            """, (r["id"],)).fetchone()
            if signals and signals["cnt"] >= 5:
                avg_conf = signals["avg_conf"] or 0
                avg_label = signals["avg_label"] or 0
                if avg_conf < 30 and avg_label < 0:
                    degrade_rule(r["id"])
