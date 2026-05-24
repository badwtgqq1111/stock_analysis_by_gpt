import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.data_service import load_factor_ic_data


class FactorICService:
    def get_factor_ic(self, factor_set: str = "qlib_alpha158", horizon: int = 20, top_n: int = 10) -> dict:
        data = load_factor_ic_data(factor_set, horizon)

        dates = [d.strftime("%Y-%m-%d") for d in data["dates"]]
        factors = list(data["factors"])

        # Top N factors by absolute mean IC
        ic_means = {}
        for f in factors:
            arr = data["ic_series"].get(f, [])
            if len(arr) > 0:
                ic_means[f] = abs(float(arr.mean()))
        top_factors = sorted(ic_means, key=ic_means.get, reverse=True)[:top_n]

        ic_series = {}
        rank_ic_series = {}
        for f in top_factors:
            ic_series[f] = [float(v) if not hasattr(v, 'isnan') or not bool(getattr(v, 'isnan', lambda: False)()) else None for v in data["ic_series"].get(f, [])]
            rank_ic_series[f] = [float(v) if not hasattr(v, 'isnan') or not bool(getattr(v, 'isnan', lambda: False)()) else None for v in data["rank_ic_series"].get(f, [])]

        # Summary table
        summary = []
        summary_df = data.get("summary")
        if summary_df is not None:
            for _, row in summary_df.iterrows():
                summary.append({
                    "factor": str(row.get("因子", row.iloc[0])),
                    "mean_ic": _safe_float(row.get("IC均值", None)),
                    "std_ic": _safe_float(row.get("IC标准差", None)),
                    "ic_ir": _safe_float(row.get("ICIR", None)),
                    "mean_rank_ic": _safe_float(row.get("RankIC均值", None)),
                    "ic_positive_rate": _safe_float(row.get("IC>0占比", None)),
                })

        # Top 10 factors by mean IC
        top10 = sorted(summary, key=lambda x: abs(x.get("mean_ic") or 0), reverse=True)[:10]

        return {
            "dates": dates,
            "factors": top_factors,
            "ic_series": ic_series,
            "rank_ic_series": rank_ic_series,
            "summary": summary,
            "top10": top10,
            "factor_set": factor_set,
            "horizon": horizon,
            "top_n": top_n,
        }


def _safe_float(val) -> float | None:
    if val is None:
        return None
    try:
        import numpy as np
        f = float(val)
        if np.isnan(f) or np.isinf(f):
            return None
        return round(f, 4)
    except (TypeError, ValueError):
        return None
