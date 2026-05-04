"""
routes/ai.py —— AI预测API

路由：
  POST /api/ai/train              → 训练模型
  POST /api/ai/predict            → 单股预测
  POST /api/ai/batch_predict      → 批量预测
  GET  /api/ai/model              → 模型信息
  GET  /api/ai/features           → 特征重要性
"""

from flask import Blueprint, jsonify, request

from ai.predictor import get_predictor
from ministries.rites.data_source_manager import get_data_source_manager


def _fetch_stock_data(code: str, start_date: str, end_date: str, min_rows: int = 80):
    """
    获取股票日线数据（本地优先，自动 fallback 到 akshare 在线拉取）
    返回 DataFrame 或 None
    """
    import pandas as pd

    # 1. 先尝试数据源管理器（本地数据库）
    ds = get_data_source_manager()
    try:
        raw = ds.get_daily_price(code, start_date=start_date, end_date=end_date)
        if raw and len(raw) >= min_rows:
            df = pd.DataFrame(raw)
            df["code"] = code
            df["date"] = pd.to_datetime(df["date"])
            return df
    except Exception:
        pass

    # 2. 本地没有，尝试 akshare 在线拉取
    try:
        import akshare as ak
        # 判断交易所前缀
        ak_code = code if "." not in code else code.split(".")[0]
        prefix = "sh" if ak_code.startswith("6") else "sz"
        symbol = f"{prefix}{ak_code}"
        df = ak.stock_zh_a_daily(
            symbol=symbol,
            start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
            adjust="qfq"
        )
        if df.empty or len(df) < min_rows:
            return None
        df["date"] = pd.to_datetime(df["date"])
        df["code"] = code
        # 确保所需列存在
        for col in ["open", "high", "low", "close", "volume"]:
            if col not in df.columns:
                return None
        return df
    except Exception as e:
        print(f"[AI/fetch] {code} akshare 拉取失败: {e}")
        return None

ai_bp = Blueprint("ai", __name__, url_prefix="/api/ai")
_predictor = get_predictor()


@ai_bp.route("/model", methods=["GET"])
def get_model_info():
    """获取模型信息"""
    return jsonify({
        "success": True,
        "info": _predictor.get_model_info(),
    })


@ai_bp.route("/model/info", methods=["GET"])
def get_model_info_alias():
    """获取模型信息（别名）"""
    return jsonify({
        "success": True,
        "info": _predictor.get_model_info(),
    })


@ai_bp.route("/train", methods=["POST"])
def train_model():
    """训练AI模型（留空则自动使用本地数据库中所有有数据的股票）"""
    data = request.get_json() or {}
    codes = data.get("codes", [])
    days = data.get("days", 365)
    forward_days = data.get("forward_days", 5)
    threshold = data.get("threshold", 0.03)

    # 如果未传入codes，自动获取本地数据库中所有有数据的股票代码
    if not codes:
        try:
            from core.db import get_conn
            with get_conn() as conn:
                rows = conn.execute("SELECT DISTINCT code FROM daily_price ORDER BY code").fetchall()
                codes = [r[0] for r in rows]
        except Exception as e:
            return jsonify({"success": False, "error": f"无法读取本地股票列表: {e}"}), 500

    if not codes:
        return jsonify({"success": False, "error": "本地数据库为空，请先拉取股票数据"}), 400

    ds = get_data_source_manager()
    import pandas as pd
    from datetime import datetime, timedelta

    all_data = []
    end = datetime.now()
    start = end - timedelta(days=days + 100)

    for code in codes:
        try:
            df = _fetch_stock_data(code, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), min_rows=80)
            if df is not None:
                all_data.append(df)
        except Exception as e:
            print(f"[AI/train] {code} 数据获取失败: {e}")
            continue

    if not all_data:
        return jsonify({"success": False, "error": "没有获取到足够的训练数据（请确认股票代码正确，或 akshare 网络正常）"}), 400

    combined = pd.concat(all_data, ignore_index=True)
    result = _predictor.train(combined, forward_days=forward_days, threshold=threshold)

    return jsonify(result)


@ai_bp.route("/predict", methods=["POST"])
def predict_stock():
    """预测单只股票"""
    data = request.get_json() or {}
    code = data.get("code", "").strip()
    days = data.get("days", 180)

    if not code:
        return jsonify({"success": False, "error": "缺少股票代码"}), 400

    if not _predictor.is_trained:
        return jsonify({"success": False, "error": "模型未训练，请先调用 /api/ai/train"}), 400

    from datetime import datetime, timedelta

    end = datetime.now()
    start = end - timedelta(days=days + 100)

    try:
        df = _fetch_stock_data(code, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), min_rows=80)
        if df is None:
            return jsonify({"success": False, "error": "数据不足（无法获取该股票历史数据）"}), 400

        df = df.sort_values("date")
        result = _predictor.predict(code, df)
        if not result:
            return jsonify({"success": False, "error": "预测失败"}), 500

        return jsonify({
            "success": True,
            "code": result.code,
            "prediction": result.prediction,
            "prediction_label": "上涨" if result.prediction == 1 else "下跌",
            "confidence": result.confidence,
            "prob_up": result.prob_up,
            "prob_down": result.prob_down,
            "model_version": result.model_version,
        })

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@ai_bp.route("/batch_predict", methods=["POST"])
def batch_predict():
    """批量预测"""
    data = request.get_json() or {}
    codes = data.get("codes", [])
    days = data.get("days", 180)

    if not codes:
        return jsonify({"success": False, "error": "缺少股票代码列表"}), 400

    if not _predictor.is_trained:
        return jsonify({"success": False, "error": "模型未训练"}), 400

    from datetime import datetime, timedelta

    end = datetime.now()
    start = end - timedelta(days=days + 100)

    stocks_data = {}
    for code in codes:
        try:
            df = _fetch_stock_data(code, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), min_rows=80)
            if df is not None:
                df = df.sort_values("date")
                stocks_data[code] = df
        except Exception as e:
            print(f"[AI/batch] {code} 数据获取失败: {e}")
            continue

    results = _predictor.batch_predict(stocks_data)

    return jsonify({
        "success": True,
        "predicted": len(results),
        "results": [
            {
                "code": r.code,
                "prediction": r.prediction,
                "prediction_label": "上涨" if r.prediction == 1 else "下跌",
                "confidence": r.confidence,
                "prob_up": r.prob_up,
                "prob_down": r.prob_down,
            }
            for r in results
        ],
    })


@ai_bp.route("/scan", methods=["POST"])
def scan_stocks():
    """全股票扫描：预测上涨且置信度达标的股票"""
    data = request.get_json() or {}
    threshold = data.get("threshold", 0.6)
    max_stocks = data.get("max_stocks", 500)
    days = data.get("days", 180)

    if not _predictor.is_trained:
        return jsonify({"success": False, "error": "模型未训练，请先调用 /api/ai/train"}), 400

    from datetime import datetime, timedelta
    import pandas as pd
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import os

    end = datetime.now()
    start = end - timedelta(days=days + 100)

    # 获取本地数据库中有数据的股票代码（限制数量以控制耗时）
    try:
        from core.db import get_conn, get_stock_name
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT code FROM daily_price ORDER BY code LIMIT ?",
                (max_stocks,)
            ).fetchall()
            codes = [r[0] for r in rows]
    except Exception as e:
        return jsonify({"success": False, "error": f"无法读取本地股票列表: {e}"}), 500

    if not codes:
        return jsonify({"success": False, "error": "本地数据库为空"}), 400

    # 预加载所有股票数据到内存（避免每个线程重复查库）
    print(f"[AI/scan] 开始预加载 {len(codes)} 只股票数据...")
    stocks_data = {}
    for code in codes:
        try:
            df = _fetch_stock_data(code, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), min_rows=80)
            if df is not None and not df.empty:
                stocks_data[code] = df.sort_values("date")
        except Exception:
            pass

    print(f"[AI/scan] 预加载完成，有效数据 {len(stocks_data)} 只，开始并发预测...")

    results = []
    predicted_count = 0

    # 多线程并发预测（利用CPU多核）
    workers = min(os.cpu_count() or 4, 8)
    matched_raw = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_predictor.predict, code, stocks_data[code]): code
                   for code in stocks_data}
        for future in as_completed(futures):
            code = futures[future]
            try:
                pred = future.result()
                predicted_count += 1
                if pred and pred.prediction == 1 and pred.confidence >= threshold:
                    matched_raw.append({"code": code, "pred": pred})
            except Exception as e:
                print(f"[AI/scan] {code} 预测异常: {e}")

    print(f"[AI/scan] 预测完成，共预测 {predicted_count} 只，符合条件 {len(matched_raw)} 只")

    # 批量获取名称和最新价格
    for item in matched_raw:
        code = item["code"]
        pred = item["pred"]
        try:
            with get_conn() as conn:
                latest = conn.execute(
                    "SELECT close, trade_date FROM daily_price WHERE code=? ORDER BY trade_date DESC LIMIT 1",
                    (code,)
                ).fetchone()
            latest_price = round(latest["close"], 2) if latest else None
            latest_date = latest["trade_date"] if latest else None
            name = get_stock_name(code)
            results.append({
                "code": code,
                "name": name,
                "latest_price": latest_price,
                "latest_date": latest_date,
                "confidence": pred.confidence,
                "prob_up": pred.prob_up,
                "prob_down": pred.prob_down,
            })
        except Exception:
            pass

    # 按置信度降序排列
    results.sort(key=lambda x: x["confidence"], reverse=True)

    return jsonify({
        "success": True,
        "scanned": len(codes),
        "loaded": len(stocks_data),
        "predicted": predicted_count,
        "matched": len(results),
        "threshold": threshold,
        "results": results,
    })


@ai_bp.route("/features", methods=["GET"])
def get_feature_importance():
    """获取特征重要性"""
    importance = _predictor.get_feature_importance()
    if importance is None:
        return jsonify({"success": False, "error": "模型未训练"}), 400

    return jsonify({
        "success": True,
        "features": importance,
    })
