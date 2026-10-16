#!/usr/bin/env python
"""Independent volatility shadow research. Never writes production portfolios."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from factor_engine.portfolio.execution_assumptions import cost_model, load_assumptions  # noqa: E402
from factor_engine.portfolio.paper_account import persist_paper_account, run_paper_account  # noqa: E402
from factor_engine.portfolio.volatility_sizing import cap_weights, predicted_covariance, risk_contributions  # noqa: E402

OUT = ROOT / "output/research/volatility_shadow"


def build_histories(ledger: pd.DataFrame, returns: pd.DataFrame, target_vol: float) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    histories: dict[str, list] = {key: [] for key in (
        "cn_ensemble_equal", "cn_ensemble_volcap", "arm_select_equal", "arm_select_histvol", "arm_select_volrisk")}
    audits, risk_rows = [], []
    for source in ("preselected", "arm"):
        for date, group in ledger[ledger.source == source].groupby("trade_date"):
            date = pd.Timestamp(date)
            chosen = group.sort_values(["rank", "model_score", "stock_code"], ascending=[True, False, True])
            chosen = chosen.head(10) if source == "preselected" else chosen[pd.to_numeric(chosen.weight, errors="coerce").fillna(0) > 0]
            if chosen.empty:
                continue
            if chosen.stock_code.duplicated().any():
                raise ValueError(f"duplicate selected name at {date}")
            names = chosen.stock_code.tolist()
            equal = pd.Series(1 / len(names), index=names)
            prefix = "cn_ensemble" if source == "preselected" else "arm_select"
            weights = {f"{prefix}_equal": equal}
            status, error = "ready", ""
            cov = None
            try:
                known = returns.reindex(columns=names).loc[:date]
                if date not in known.index:
                    raise ValueError("decision date not in market calendar")
                cov, historical = predicted_covariance(known)
                if source == "preselected":
                    weights["cn_ensemble_volcap"] = cap_weights(equal, cov, target_vol=target_vol)
                else:
                    hist_w = 1 / historical.reindex(names)
                    forecast_w = pd.Series(1 / np.sqrt(np.diag(cov)), index=cov.index).reindex(names)
                    weights["arm_select_histvol"] = hist_w / hist_w.sum()
                    weights["arm_select_volrisk"] = forecast_w / forecast_w.sum()
            except ValueError as exc:
                status, error = "cash_insufficient_risk", str(exc)
                for name in (["cn_ensemble_volcap"] if source == "preselected" else ["arm_select_histvol", "arm_select_volrisk"]):
                    weights[name] = equal * 0
            for name, w in weights.items():
                snapshot = chosen[["trade_date", "stock_code", "rank", "name", "model_score"]].copy()
                snapshot["target_weight"] = snapshot.stock_code.map(w)
                histories[name].append(snapshot)
                audits.append({"trade_date": date, "variant": name, "status": status if not name.endswith("_equal") else "ready",
                               "error": error if not name.endswith("_equal") else "", "names": len(w), "exposure": float(w.sum()),
                               "predicted_annual_vol": float(np.sqrt(w @ cov @ w)) if cov is not None else None})
                if cov is not None:
                    risk_rows.append(risk_contributions(w, cov).assign(trade_date=date, variant=name))
    return ({key: pd.concat(rows, ignore_index=True) for key, rows in histories.items() if rows},
            pd.DataFrame(audits), pd.concat(risk_rows, ignore_index=True) if risk_rows else pd.DataFrame())


def account_metrics(result: dict, capital: float) -> dict:
    nav = result["nav"].nav.to_numpy()
    daily = np.diff(nav) / nav[:-1]
    n = len(daily)
    drawdown = float((nav / np.maximum.accumulate(np.r_[capital, nav])[1:] - 1).min())
    ann = float((nav[-1] / capital) ** (252 / n) - 1) if n >= 252 else None
    sd = float(np.std(daily, ddof=1)) if n > 1 else 0
    downside = float(np.sqrt(np.mean(np.minimum(daily, 0) ** 2))) if n else 0
    tail = daily[daily <= np.quantile(daily, .05)] if n else np.array([])
    fills = result["fills"]
    traded = float((fills.quantity * fills.price).sum()) if not fills.empty else 0
    fees = float(fills.commission.sum()) if not fills.empty else 0
    return {"observations": n, "total_return": float(nav[-1] / capital - 1), "max_drawdown": drawdown,
            "sharpe_rf0": float(daily.mean() / sd * np.sqrt(252)) if n >= 252 and sd else None,
            "sortino_rf0": float(daily.mean() / downside * np.sqrt(252)) if n >= 252 and downside else None,
            "calmar": ann / abs(drawdown) if ann is not None and drawdown < 0 else None,
            "cvar_95_daily": float(tail.mean()) if len(tail) >= 5 else None,
            "tail_observations": len(tail), "turnover_two_way": traded / float(nav.mean()),
            "fees": fees, "slippage_est": traded * float(cost_model()["slippage"]), "fills": len(fills)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUT)
    parser.add_argument("--capital", type=float, default=45000)
    parser.add_argument("--target-vol", type=float, default=.15)
    args = parser.parse_args()
    if args.capital <= 0 or not 0 < args.target_vol <= 1:
        raise ValueError("invalid capital or target volatility")
    ledger_path = ROOT / "output/results_cn/selection_ledger.parquet"
    ledger = pd.read_parquet(ledger_path)
    ledger = ledger[ledger.source.isin(["preselected", "arm"])].copy()
    ledger["trade_date"] = pd.to_datetime(ledger.trade_date)
    files = [str(p) for year in (2024, 2025, 2026) for p in (ROOT / "assets/data/clean/ohlcv/market=CN").glob(
        f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={year}/*.parquet")]
    bars = ds.dataset(files, format="parquet").to_table(columns=["stock_code", "trade_date", "open", "close"],
                filter=ds.field("stock_code").isin(ledger.stock_code.unique().tolist())).to_pandas()
    bars["trade_date"] = pd.to_datetime(bars.trade_date)
    if bars.duplicated(["trade_date", "stock_code"]).any():
        raise ValueError("duplicate daily bars")
    bench = pd.read_parquet(ROOT / "assets/data/meta/cn_benchmark_daily.parquet")
    calendar = pd.DatetimeIndex(pd.to_datetime(bench[bench.ts_code == "000905.SH"].trade_date)).sort_values()
    prices = bars.pivot(index="trade_date", columns="stock_code", values="close").reindex(calendar)
    returns = prices.pct_change(fill_method=None)
    histories, audit, risk = build_histories(ledger, returns, args.target_vol)
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    audit.to_csv(out / "risk_audit.csv", index=False)
    risk.to_csv(out / "risk_contributions.csv", index=False)
    results, all_nav = [], {}
    # Family comparisons keep the longer model window; the common comparison
    # is a separate cold-start replay on shared decision dates (no sunk costs).
    common_dates = sorted(set(histories["cn_ensemble_equal"].trade_date) & set(histories["arm_select_equal"].trade_date))
    if not common_dates:
        raise ValueError("no common decisions between model and ARM")
    for variant, history in histories.items():
        history.to_csv(out / f"{variant}.csv", index=False)
        history[history.trade_date == history.trade_date.max()].to_csv(out / f"{variant}_latest.csv", index=False)
        folder = out / "history" / variant
        folder.mkdir(parents=True, exist_ok=True)
        for date, frame in history.groupby("trade_date"):
            frame.to_csv(folder / f"{date:%Y-%m-%d}.csv", index=False)
        for scope in ("family", "common"):
            selected = history if scope == "family" else history[history.trade_date.isin(common_dates)]
            start = selected.trade_date.min()
            # One earlier bar retains first-session previous close for limits.
            earlier = calendar[calendar < start]
            bar_start = earlier[-1] if len(earlier) else start
            account_bars = bars[bars.trade_date >= bar_start]
            result = run_paper_account(selected, account_bars, initial_capital=args.capital,
                                       strategy_version=f"volatility-shadow.v2:{variant}:{scope}", causal_execution=True)
            result["nav"] = result["nav"][result["nav"].asof_date >= start].copy()
            persist_paper_account(result, str(out / scope / variant), assumptions=load_assumptions())
            results.append({"scope": scope, "variant": variant, "start": str(start.date()),
                            "end": str(pd.Timestamp(result["nav"].asof_date.max()).date()),
                            "decisions": selected.trade_date.nunique(), **account_metrics(result, args.capital)})
            if scope == "common":
                all_nav[variant] = result["nav"].set_index("asof_date").nav
    metrics = pd.DataFrame(results)
    metrics.to_csv(out / "comparison.csv", index=False)
    pd.DataFrame(all_nav).pct_change(fill_method=None).dropna().corr().to_csv(out / "strategy_correlation.csv")
    metadata = {"capital": args.capital, "risk_model": "ewma.v1", "target_vol": args.target_vol,
                "historical_correlation_window": 60, "risk_min_periods": 20, "annualization": 252,
                "source_ledger_sha256": hashlib.sha256(ledger_path.read_bytes()).hexdigest(),
                "execution_assumptions": load_assumptions(), "common_dates": [str(d.date()) for d in common_dates],
                "ratios_min_observations": 252, "cvar_min_tail_observations": 5,
                "promotion": "research_only; ARM sample too short; no production changes",
                "risk_cap": "decision-time predicted risk only, not a guarantee between rebalances",
                "timing": "all weights and target NAV use <=t close; causal_execution=True fills at next session open",
                "caveats": ["pre-existing ledger is a historical reconstruction, not a prospective live record",
                            "QFQ data are current vintages; corporate-action PIT vintages not archived",
                            "causal accounting is opt-in for research only; legacy default remains unchanged",
                            "suspended buy orders follow existing engine pending semantics, not continuous order-book retry"]}
    (out / "methodology.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    report = ["# 波动率影子组合（研究，不晋级）", "", "费用、整手、涨跌停等复用统一 paper_account 引擎。",
              "模型名单保留 rank 前10 等权基线；volcap 只缩放敞口。ARM 保留正权重名单，只改变权重。",
              "共同窗口使用共有决策日冷启动另跑，不拿不同起点的累计收益排名。",
              "年化风险比率要求252个日收益；CVaR至少5个尾部观测。短样本空值不是0。",
              "实验开启 causal_execution=True：t 收盘信号使用 t 净值定仓，t+1 开盘实际成交后才记入现金和持仓。生产默认仍是旧行为。",
              "复权数据版本和台账事后重建仍待独立审计，不用本报告宣称收益改善。", "", "```", metrics.to_string(index=False), "```", "",
              "风险贡献明细：risk_contributions.csv；预算和缺数审计：risk_audit.csv；策略相关性：strategy_correlation.csv。"]
    (out / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(metrics.to_string(index=False))
    print(f"risk_decisions={len(audit)}, cash_insufficient={(audit.status == 'cash_insufficient_risk').sum()}")
    print(f"output={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
