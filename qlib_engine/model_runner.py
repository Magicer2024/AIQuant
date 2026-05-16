"""
qlib_engine/model_runner.py —— Qlib 模型训练与预测

用法：
  runner = ModelRunner()
  runner.train(model_name="LGBModel", train_start="2020-01-01", train_end="2024-12-31")
  predictions = runner.predict("2025-01-01", "2025-06-01")
"""

import pandas as pd
import numpy as np
from typing import Optional, List

from qlib_engine import init_qlib


class ModelRunner:
    """
    Thin wrapper around Qlib's standard model workflow.

    Models: LGBModel, GRU, ALSTM, etc.
    All use the standard fit/predict interface.
    """

    def __init__(self):
        init_qlib()

    def train(
        self,
        model_name: str = "LGBModel",
        train_start: str = "2020-01-01",
        train_end: str = "2024-12-31",
        valid_start: str = "2025-01-01",
        valid_end: str = "2025-04-30",
        handler_class: str = "Alpha158",
        **model_kwargs,
    ) -> dict:
        """
        Train a Qlib model on Alpha158 features.

        Returns dict with metrics (IC, ICIR, Rank IC, etc.)
        """
        from qlib.utils import init_instance_by_config
        from qlib.workflow import R
        from qlib.workflow.record_temp import SignalRecord, SigAnaRecord

        handler_config = {
            "class": "Alpha158",
            "module_path": "qlib.contrib.data.handler",
            "kwargs": {
                "start_time": train_start,
                "end_time": valid_end,
                "fit_start_time": train_start,
                "fit_end_time": train_end,
                "instruments": "all",
            },
        }
        handler = init_instance_by_config(handler_config)

        dataset_config = {
            "class": "DatasetH",
            "module_path": "qlib.data.dataset",
            "kwargs": {
                "handler": handler,
                "segments": {
                    "train": (train_start, train_end),
                    "valid": (valid_start, valid_end),
                },
            },
        }
        dataset = init_instance_by_config(dataset_config)

        model_map = {
            "LGBModel": ("qlib.contrib.model.gbdt", "LGBModel"),
            "GRU": ("qlib.contrib.model.pytorch_gru", "GRU"),
            "ALSTM": ("qlib.contrib.model.pytorch_alstm", "ALSTM"),
        }
        module_path, class_name = model_map.get(model_name, model_map["LGBModel"])

        model_config = {
            "class": class_name,
            "module_path": module_path,
            "kwargs": model_kwargs or {},
        }
        model = init_instance_by_config(model_config)

        with R.start(experiment_name=f"aiquant_{model_name}"):
            R.log_params(flatten_dict={
                "model": model_name,
                "train": f"{train_start}-{train_end}",
                "valid": f"{valid_start}-{valid_end}",
            })
            model.fit(dataset)
            R.save_objects(**{"trained_model.pkl": model})

            recorder = R.get_recorder()
            sr = SignalRecord(model, dataset, recorder)
            sr.generate()

            sar = SigAnaRecord(recorder)
            sar.generate()

            metrics = recorder.list_metrics()
            self.model = model
            self.dataset = dataset

        return metrics

    def predict(
        self,
        start_date: str,
        end_date: str,
        stock_list: Optional[List[str]] = None,
    ) -> pd.Series:
        """
        Generate predictions for a date range.

        Returns pd.Series with MultiIndex (datetime, instrument), values = predicted return
        """
        if not hasattr(self, "model"):
            raise RuntimeError("Model not trained. Call train() first.")

        predictions = self.model.predict(self.dataset, segment="test")

        if predictions is None or len(predictions) == 0:
            if hasattr(self, "dataset"):
                predictions = self.model.predict(self.dataset)

        if stock_list and predictions is not None:
            predictions = predictions[predictions.index.get_level_values(1).isin(stock_list)]

        return predictions
