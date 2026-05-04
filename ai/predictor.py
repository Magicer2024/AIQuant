"""
ai/predictor.py —— AI预测引擎

职责：
  1. 训练涨跌预测模型（基于技术指标）
  2. 对单股或批量股票进行预测
  3. 模型持久化（joblib）
  4. 预测置信度输出

模型：RandomForestClassifier（开箱即用，无需GPU）
"""

import os
from dataclasses import dataclass
from datetime import datetime

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, classification_report

from ai.features import build_features, build_label, FEATURE_COLUMNS


MODEL_DIR = os.path.join(os.path.dirname(__file__), "models")
os.makedirs(MODEL_DIR, exist_ok=True)


@dataclass
class PredictionResult:
    """预测结果"""
    code: str
    prediction: int          # 1=涨, 0=震荡, -1=跌
    confidence: float        # 置信度 0-1
    prob_up: float           # 上涨概率
    prob_down: float         # 下跌概率
    prob_neutral: float      # 震荡概率
    features_used: list      # 使用到的特征
    model_version: str       # 模型版本


class AIPredictor:
    """AI预测器"""

    def __init__(self):
        self.model: RandomForestClassifier | None = None
        self.feature_cols = FEATURE_COLUMNS
        self.model_path = os.path.join(MODEL_DIR, "rf_model.joblib")
        self.is_trained = False
        self._load_model()

    def _load_model(self):
        """加载已有模型"""
        if os.path.exists(self.model_path):
            try:
                self.model = joblib.load(self.model_path)
                self.is_trained = True
                print(f"[AIPredictor] 模型已加载: {self.model_path}")
            except Exception as e:
                print(f"[AIPredictor] 模型加载失败: {e}")

    def _save_model(self):
        """保存模型"""
        if self.model:
            joblib.dump(self.model, self.model_path)
            print(f"[AIPredictor] 模型已保存: {self.model_path}")

    def train(self, df: pd.DataFrame, forward_days: int = 5, threshold: float = 0.03,
              test_size: float = 0.2) -> dict:
        """
        训练模型
        :param df: 包含多只股票的历史数据（需有 code/date/open/high/low/close/volume 列）
        :return: 训练结果报告
        """
        print(f"[AIPredictor] 开始训练，数据量: {len(df)}")

        # 特征工程
        all_features = []
        all_labels = []

        for code, group in df.groupby("code"):
            if len(group) < 80:
                continue
            try:
                feat = build_features(group)
                label = build_label(feat, forward_days=forward_days, threshold=threshold)

                feat = feat[self.feature_cols + ["close"]]
                feat["label"] = label

                # 删除缺失值
                feat = feat.dropna()
                if len(feat) < 30:
                    continue

                all_features.append(feat[self.feature_cols])
                all_labels.append(feat["label"])
            except Exception as e:
                print(f"[AIPredictor] {code} 特征构建失败: {e}")
                continue

        if not all_features:
            return {"success": False, "error": "没有足够的有效数据训练"}

        X = pd.concat(all_features, ignore_index=True)
        y = pd.concat(all_labels, ignore_index=True)

        # 只保留二元分类（涨 vs 不涨）简化问题
        y_binary = (y > 0).astype(int)

        # 划分训练集/测试集
        X_train, X_test, y_train, y_test = train_test_split(
            X, y_binary, test_size=test_size, random_state=42, shuffle=True
        )

        # 训练随机森林
        self.model = RandomForestClassifier(
            n_estimators=100,
            max_depth=10,
            min_samples_split=10,
            min_samples_leaf=5,
            random_state=42,
            n_jobs=-1,
        )
        self.model.fit(X_train, y_train)
        self.is_trained = True

        # 评估
        y_pred = self.model.predict(X_test)
        acc = accuracy_score(y_test, y_pred)

        # 特征重要性
        importances = dict(zip(self.feature_cols, self.model.feature_importances_.tolist()))
        top_features = sorted(importances.items(), key=lambda x: x[1], reverse=True)[:10]

        # 保存模型
        self._save_model()

        return {
            "success": True,
            "accuracy": round(acc, 4),
            "train_samples": len(X_train),
            "test_samples": len(X_test),
            "top_features": [{"name": k, "importance": round(v, 4)} for k, v in top_features],
            "model_path": self.model_path,
        }

    def predict(self, code: str, df: pd.DataFrame) -> PredictionResult | None:
        """
        预测单只股票
        :param code: 股票代码
        :param df: 历史K线数据
        :return: 预测结果
        """
        if not self.is_trained or self.model is None:
            return None

        if len(df) < 80:
            return None

        try:
            feat = build_features(df)
            feat = feat.dropna()
            if len(feat) == 0:
                return None

            latest = feat[self.feature_cols].iloc[-1:]
            pred_proba = self.model.predict_proba(latest)[0]
            pred_class = self.model.predict(latest)[0]

            # 二分类：0=不涨，1=涨
            prob_up = pred_proba[1] if len(pred_proba) > 1 else 0.5
            prob_down = 1 - prob_up
            prob_neutral = 0.0  # 二分类无中性

            # 映射回三分类
            if pred_class == 1:
                prediction = 1
                confidence = prob_up
            else:
                prediction = -1
                confidence = prob_down

            return PredictionResult(
                code=code,
                prediction=prediction,
                confidence=round(confidence, 4),
                prob_up=round(prob_up, 4),
                prob_down=round(prob_down, 4),
                prob_neutral=round(prob_neutral, 4),
                features_used=self.feature_cols,
                model_version="rf_v1",
            )

        except Exception as e:
            print(f"[AIPredictor] {code} 预测失败: {e}")
            return None

    def batch_predict(self, stocks_data: dict[str, pd.DataFrame]) -> list[PredictionResult]:
        """批量预测"""
        results = []
        for code, df in stocks_data.items():
            result = self.predict(code, df)
            if result:
                results.append(result)
        return results

    def get_feature_importance(self) -> list[dict] | None:
        """获取特征重要性"""
        if not self.is_trained or self.model is None:
            return None
        importances = self.model.feature_importances_
        return [
            {"name": name, "importance": round(imp, 4)}
            for name, imp in zip(self.feature_cols, importances)
        ]

    def get_model_info(self) -> dict:
        """获取模型信息"""
        return {
            "is_trained": self.is_trained,
            "model_type": "RandomForestClassifier",
            "feature_count": len(self.feature_cols),
            "model_path": self.model_path,
            "model_exists": os.path.exists(self.model_path),
        }


# 全局单例
_predictor: AIPredictor | None = None


def get_predictor() -> AIPredictor:
    """获取预测器单例"""
    global _predictor
    if _predictor is None:
        _predictor = AIPredictor()
    return _predictor
