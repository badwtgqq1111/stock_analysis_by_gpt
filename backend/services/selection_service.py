import re
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.data_service import (
    load_selected_data, load_ranking_data, parse_explanation, parse_shap_values,
)


class SelectionService:
    """「选股结果」页的取数层。

    2026-10-08 起 `/api/selection` 支持多数据源（见 `selection_sources.SOURCES`）。
    以前它只读港股 `lightgbm_top10_selected.csv`，于是页面显示的名单和
    `output/results_cn/` 下真正在跑的生产名单不是一回事，且页面上没有任何提示。
    """

    def get_selection(self, source: str = "", factor_set: str = "") -> dict:
        from backend.services import selection_sources as src

        key = source or src.DEFAULT_SOURCE
        try:
            payload = src.load_source(key)
        except KeyError:
            payload = src.load_source(src.DEFAULT_SOURCE)
            payload["note"] = f"未知数据源 `{key}`，已回退到默认源。" + (payload.get("note") or "")
        # 描述块单独嵌到 `source` 下：response_model 只放行声明的字段，
        # 不嵌套的话前端拿不到"这份名单来自哪个文件、什么时候生成的"。
        payload["source"] = {
            "key": payload["key"],
            "label": payload["label"],
            "note": payload["note"],
            "files": payload["files"],
            "trade_date": payload["trade_date"],
            "row_count": payload["row_count"],
            "supports_shap": payload["supports_shap"],
        }
        payload["available"] = src.list_sources()
        return payload

    def get_shap(self, stock_code: str, factor_set: str = "") -> dict:
        # SHAP 只存在于港股链路的 `factor_explanation` 列；CN 名单没有这个字段。
        df = load_selected_data(factor_set)
        if df is None or df.empty:
            return {"features": []}

        code_col = "stock_code" if "stock_code" in df.columns else None
        if code_col is None:
            return {"features": []}

        row = df[df[code_col].astype(str).str.zfill(5) == stock_code]
        if row.empty:
            return {"features": []}

        explanation = row.iloc[0].get("factor_explanation", None)
        if pd.isna(explanation) or not explanation:
            return {"features": []}

        try:
            stock_shap = parse_shap_values(str(explanation))
        except Exception:
            return {"features": []}

        # 注意：parse_shap_values 返回的**就是** {'positive': [...], 'negative': [...]}。
        # 旧代码在它上面又取了一次 `.get("stock_shap_values")`，返回 {} ⇒ 即使解析成功也永远没数据。
        features = []
        if isinstance(stock_shap, dict):
            for direction in ("positive", "negative"):
                for item in stock_shap.get(direction, []):
                    try:
                        value = float(item.get("shap_value", 0))
                    except (TypeError, ValueError):
                        continue
                    features.append({
                        "name": str(item.get("feature", "")),
                        "value": value,
                        "direction": direction,
                    })

        features.sort(key=lambda x: abs(x["value"]), reverse=True)
        return {"features": features}

    def get_importance(self, factor_set: str = "") -> dict:
        """Extract global feature importance from the selected CSV's factor_explanation."""
        df = load_selected_data(factor_set)
        if df is None or df.empty:
            df = load_ranking_data(factor_set)
        if df is None or df.empty:
            return {"factor_set": factor_set, "importances": [], "feature_count": 0, "train_rows": 0}

        # Aggregate importance_weight from top_features across all selected stocks
        agg: dict[str, float] = {}
        count: dict[str, int] = {}
        feature_count = 0
        train_rows = 0

        parsed_any = False
        for _, row in df.iterrows():
            expl = row.get("factor_explanation", None)
            if expl is None or pd.isna(expl) or not expl:
                continue
            expl_str = str(expl)

            # 优先结构化解析（2026-10-08 起 parse_explanation 能处理裸 nan 了）；
            # 解析不出来再退回下面的正则，保证旧数据格式也不至于全空。
            parsed = parse_explanation(expl_str)
            if isinstance(parsed, dict) and parsed.get("top_features"):
                parsed_any = True
                meta = parsed.get("model_metadata") or {}
                feature_count = feature_count or int(meta.get("feature_count") or 0)
                train_rows = train_rows or int(meta.get("train_rows") or 0)
                for item in parsed["top_features"]:
                    name = str(item.get("feature_name", ""))
                    try:
                        w = float(item.get("importance_weight", item.get("importance", 0)))
                    except (TypeError, ValueError):
                        continue
                    agg[name] = agg.get(name, 0) + w
                    count[name] = count.get(name, 0) + 1
                continue

            # Extract feature_count and train_rows
            if not feature_count:
                fc = re.search(r"'feature_count':\s*(\d+)", expl_str)
                if fc:
                    feature_count = int(fc.group(1))
            if not train_rows:
                tr = re.search(r"'train_rows':\s*(\d+)", expl_str)
                if tr:
                    train_rows = int(tr.group(1))

            # Extract top_features array
            for m in re.finditer(r"\{'feature_name':\s*'([^']+)',\s*'importance':\s*([\d.e+-]+)(?:[^}]*'importance_weight':\s*([\d.e+-]+))?", expl_str):
                name = m.group(1)
                try:
                    w = float(m.group(3) if m.group(3) else m.group(2))
                except (ValueError, IndexError):
                    continue
                agg[name] = agg.get(name, 0) + w
                count[name] = count.get(name, 0) + 1

        # Average importance across stocks
        importances = []
        for name, total in sorted(agg.items(), key=lambda x: -x[1]):
            importances.append({
                "factor": name,
                "importance": round(total / count[name], 6),
            })

        return {
            "factor_set": factor_set,
            "importances": importances[:30],
            "feature_count": feature_count,
            "train_rows": train_rows,
        }
