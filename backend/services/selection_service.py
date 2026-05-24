import re
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.data_service import (
    load_selected_data, load_ranking_data, parse_shap_values,
    get_stock_names_batch,
)


class SelectionService:
    def get_selection(self, factor_set: str = "") -> dict:
        df = load_selected_data(factor_set)
        if df is None or df.empty:
            df = load_ranking_data(factor_set)

        if df is None or df.empty:
            return {"rows": [], "columns": [], "empty": True}

        codes = [str(c).zfill(5) for c in df["stock_code"].tolist()]
        names_map = get_stock_names_batch(codes)

        columns = []
        col_map = {
            "stock_code": "股票代码", "ranking_score": "综合评分",
            "signal_tier": "信号等级", "setup_type": "形态类型",
            "risk_adjusted_score": "风险调整得分", "win_rate": "胜率(%)",
            "backtest_return": "回测收益(%)",
        }
        for col in df.columns:
            if col in col_map:
                columns.append({"key": col, "title": col_map[col]})

        rows = []
        for _, row in df.iterrows():
            code = str(row["stock_code"]).zfill(5)
            r = {"stock_code": code, "stock_name": names_map.get(code, "")}
            for col in df.columns:
                if col == "stock_code":
                    continue
                val = row[col]
                if pd.isna(val):
                    r[col] = None
                elif isinstance(val, (int, float)):
                    r[col] = round(float(val), 4)
                else:
                    r[col] = str(val)
            rows.append(r)

        return {"rows": rows, "columns": columns, "empty": False}

    def get_shap(self, stock_code: str, factor_set: str = "") -> dict:
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
            parsed = parse_shap_values(str(explanation))
        except Exception:
            return {"features": []}

        features = []
        if isinstance(parsed, dict):
            stock_shap = parsed.get("stock_shap_values", {})
            if isinstance(stock_shap, dict):
                for item in stock_shap.get("positive", []):
                    features.append({
                        "name": str(item.get("feature", "")),
                        "value": float(item.get("shap_value", 0)),
                        "direction": "positive",
                    })
                for item in stock_shap.get("negative", []):
                    features.append({
                        "name": str(item.get("feature", "")),
                        "value": float(item.get("shap_value", 0)),
                        "direction": "negative",
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

        for _, row in df.iterrows():
            expl = row.get("factor_explanation", None)
            if pd.isna(expl) or not expl:
                continue
            expl_str = str(expl)

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
