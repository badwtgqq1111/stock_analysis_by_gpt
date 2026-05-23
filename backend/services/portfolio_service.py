import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.data_service import load_portfolio_backtest


class PortfolioService:
    def get_backtest(self) -> dict:
        data = load_portfolio_backtest()

        dates = [d.strftime("%Y-%m-%d") for d in data["dates"]]
        equity = [float(v) for v in data["equity"]]
        drawdown = [float(v) * 100 for v in data["drawdown"]]

        metrics = data.get("metrics", {})
        holdings = []
        holdings_df = data.get("holdings")
        if holdings_df is not None:
            for _, row in holdings_df.iterrows():
                r = {}
                for col in holdings_df.columns:
                    val = row[col]
                    try:
                        if hasattr(val, 'isna') and bool(getattr(val, 'isna', lambda: False)()):
                            r[col] = None
                        elif isinstance(val, (int, float)):
                            r[col] = round(float(val), 4)
                        else:
                            r[col] = str(val)
                    except Exception:
                        r[col] = str(val)
                holdings.append(r)

        return {
            "dates": dates,
            "equity": equity,
            "drawdown": drawdown,
            "metrics": {
                "总收益率": metrics.get("总收益率", "N/A"),
                "年化收益率": metrics.get("年化收益率", "N/A"),
                "最大回撤": metrics.get("最大回撤", "N/A"),
                "夏普比率": metrics.get("夏普比率", "N/A"),
                "胜率": metrics.get("胜率", "N/A"),
            },
            "holdings": holdings,
            "description": "LightGBM Top10 等权组合的模拟回测结果",
        }
