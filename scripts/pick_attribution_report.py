#!/usr/bin/env python
"""逐股选股归因报告（P1_22 §13 的落地版）。

输入：
  output/verification/p1_22_shap_attribution_20260925/{shap_by_stock,shap_detail,gate_decomposition}_<date>.csv
  output/results_cn/cn_ensemble_selected.csv / cn_ensemble_preselected.csv（当日名单与通道）
  仓库日线（qfq）→ 实现收益（T+1 开盘进场、至最新收盘）
输出：
  output/results_cn/attribution/<date>/pick_attribution.{csv,md}
  output/results_cn/attribution/<date>/winners_vs_losers.md
用法： PYTHONPATH=. .venv/bin/python scripts/pick_attribution_report.py 2026-09-24 [code,code,...]
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from data.ingest.service import MarketDataService  # noqa: E402

SHAP_DIR = ROOT / "output/verification/p1_22_shap_attribution_20260925"


def load_returns(svc, codes, decision_date):
    frames = []
    for code in codes:
        bars = svc.warehouse.read_ohlcv(stock_code=code, market="CN", frequency="daily", adjust="qfq")
        if bars is None or bars.empty:
            continue
        bars = bars.copy()
        bars["trade_date"] = pd.to_datetime(bars["trade_date"])
        bars = bars.sort_values("trade_date")
        frames.append(bars[["stock_code", "trade_date", "open", "close"]])
    if not frames:
        return pd.DataFrame()
    bars = pd.concat(frames, ignore_index=True)
    out = []
    for code, group in bars.groupby("stock_code"):
        group = group.sort_values("trade_date").reset_index(drop=True)
        decision = group[group["trade_date"] <= pd.Timestamp(decision_date)]
        if decision.empty:
            continue
        base = decision.iloc[-1]
        entry_idx = decision.index[-1] + 1                       # T+1
        entry = group.loc[entry_idx] if entry_idx in group.index else None
        last = group.iloc[-1]
        five_back = group.loc[max(entry_idx - 5, 0)] if entry_idx in group.index else group.iloc[0]
        out.append({
            "stock_code": code,
            "close_decision": float(base["close"]),
            "last_trade_date": last["trade_date"].date().isoformat(),
            "last_close": float(last["close"]),
            "ret_close_to_last_%": round(float(last["close"] / base["close"] - 1) * 100, 3),
            "ret_entry_open_to_last_%": (round(float(last["close"] / entry["open"] - 1) * 100, 3)
                                         if entry is not None and float(entry["open"]) > 0 else np.nan),
            "ret_5d_%": round(float(last["close"] / five_back["close"] - 1) * 100, 3) if float(five_back["close"]) > 0 else np.nan,
        })
    return pd.DataFrame(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("trade_date")
    ap.add_argument("codes", nargs="?", default="", help="逗号分隔；留空=当日名单+候选池")
    args = ap.parse_args()
    date = args.trade_date
    out_dir = ROOT / "output/results_cn/attribution" / date
    out_dir.mkdir(parents=True, exist_ok=True)
    svc = MarketDataService(base_dir=str(ROOT / "assets/data"))

    by_stock = pd.read_csv(SHAP_DIR / f"shap_by_stock_{date}.csv")
    detail = pd.read_csv(SHAP_DIR / f"shap_detail_{date}.csv")
    gate = pd.read_csv(SHAP_DIR / f"gate_decomposition_{date}.csv")
    fam_cols = [c for c in by_stock.columns if c.startswith("fam_")]

    selection = {}
    for name, path in (("selected", ROOT / "output/results_cn/cn_ensemble_selected.csv"),
                       ("preselected", ROOT / "output/results_cn/cn_ensemble_preselected.csv")):
        if path.is_file():
            frame = pd.read_csv(path)
            for _, row in frame.iterrows():
                code = str(row.get("stock_code"))
                entry = selection.setdefault(code, {})
                entry[f"in_{name}"] = True
                for key in ("target_weight", "model_score", "ensemble_score", "lightgbm_score",
                            "transformer_score", "gate_tilt", "rank", "selection_channel", "selection_sleeve",
                            "signal_recipe", "constraint_status", "last_close"):
                    if key in row and pd.notna(row[key]):
                        entry[key] = row[key]

    if args.codes.strip():
        wanted = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        wanted = sorted(selection.keys())
    frame = by_stock[by_stock["stock_code"].isin(wanted)].copy()
    frame = frame.merge(gate, on="stock_code", how="left", suffixes=("", "_gate"))
    frame = frame.assign(**{c: frame[c] for c in ("raw_pred", "rank")})
    frame["in_selection"] = frame["stock_code"].map(lambda c: bool(selection.get(c, {}).get("in_selected")))
    frame["in_preselected"] = frame["stock_code"].map(lambda c: bool(selection.get(c, {}).get("in_preselected")))
    frame["channel"] = frame["stock_code"].map(lambda c: selection.get(c, {}).get("selection_channel"))
    frame["target_weight"] = frame["stock_code"].map(lambda c: selection.get(c, {}).get("target_weight"))
    frame["sleeve"] = frame["stock_code"].map(lambda c: selection.get(c, {}).get("selection_sleeve"))
    frame["recipe"] = frame["stock_code"].map(lambda c: selection.get(c, {}).get("signal_recipe"))
    # 策略归属（2026-09-29 新增）：策略 = (信号, 状态, 仓位, 退出, 预算, 证据)，见 factor_engine/strategies.py
    import tomllib as _tomllib
    from factor_engine.strategies import load_strategies, strategy_for_pick, state_mask  # noqa: E402
    try:
        _specs = load_strategies(_tomllib.load(open(ROOT / "config/cn_pipeline.toml", "rb")))
    except Exception:
        _specs = {}
    frame["strategy"] = [strategy_for_pick(ch, rc, sl, _specs)
                         for ch, rc, sl in zip(frame["channel"], frame["recipe"], frame["sleeve"])]
    if _specs:
        strategy_table = pd.DataFrame([spec.describe() for spec in _specs.values()])
        strategy_table.to_csv(out_dir / "strategy_registry_snapshot.csv", index=False)

    returns = load_returns(svc, list(frame["stock_code"].astype(str)), date)
    frame = frame.merge(returns, on="stock_code", how="left")

    # 每只票的 top ± 因子
    top_pos, top_neg = {}, {}
    for code, group in detail[detail["stock_code"].isin(frame["stock_code"])].groupby("stock_code"):
        ranked = group.sort_values("shap", ascending=False)
        top_pos[code] = "; ".join(f"{r.feature}({r.shap:+.3f})" for r in ranked.head(4).itertuples())
        top_neg[code] = "; ".join(f"{r.feature}({r.shap:+.3f})" for r in ranked.tail(4).itertuples())
    frame["top_positive_factors"] = frame["stock_code"].map(top_pos)
    frame["top_negative_factors"] = frame["stock_code"].map(top_neg)

    keep = ["stock_code", "strategy", "raw_pred", "rank", "channel", "sleeve", "recipe", "in_selection", "in_preselected",
            "target_weight", "dist_from_120d_low", "dist_from_60d_high", "volume_ratio_20d", "flow_z_5d",
            "tilt_points", "tilt_from_dist", "tilt_from_high", "tilt_from_volume", "tilt_from_flow",
            "last_close", "close_decision", "last_trade_date", "ret_close_to_last_%",
            "ret_entry_open_to_last_%", "ret_5d_%", "top_positive_factors", "top_negative_factors", *fam_cols]
    keep = [c for c in keep if c in frame.columns]
    frame = frame[keep].sort_values("ret_entry_open_to_last_%", ascending=False)
    frame.round(5).to_csv(out_dir / "pick_attribution.csv", index=False)

    # 逐股 Markdown
    lines = [f"# 选股归因报告 {date}（{len(frame)} 只）", "",
             "口径：raw = bias + ΣSHAP（LightGBM，准入 90 列）；rank = 当日横截面分位名次（越小越好）；",
             "tilt = 启动门禁四分项加总（dist/high/volume/flow，单位：分）；实现收益 = T+1 开盘进场→最新收盘。", ""]
    for _, row in frame.iterrows():
        sleeve = row.get("sleeve")
        sleeve_txt = f" sleeve={sleeve}" if isinstance(sleeve, str) and sleeve else ""
        lines += [f"## {row['stock_code']}  策略={row.get('strategy')}  raw={row['raw_pred']:+.4f} rank={row.get('rank')}"
                  f" channel={row.get('channel') or '-'}{sleeve_txt}"
                  + f" 实现收益={row.get('ret_entry_open_to_last_%')}%（T+1 开盘→{row.get('last_trade_date')}）",
                  "",
                  f"- 门禁：tilt={row['tilt_points']:+.2f} 分（dist {row['tilt_from_dist']:+.2f} / high {row['tilt_from_high']:+.2f} "
                  f"/ volume {row['tilt_from_volume']:+.2f} / flow {row['tilt_from_flow']:+.2f}）；"
                  f"dist120low={row['dist_from_120d_low']:.3f} dist60high={row['dist_from_60d_high']:.3f} "
                  f"volratio={row['volume_ratio_20d']:.2f} flowz={row['flow_z_5d']:.2f}",
                  f"- 正向贡献：{row['top_positive_factors']}",
                  f"- 负向贡献：{row['top_negative_factors']}",
                  f"- 族贡献： " + ", ".join(f"{c.replace('fam_', '')}={row[c]:+.4f}" for c in fam_cols
                                              if abs(row[c]) > 0.005), ""]
    (out_dir / "pick_attribution.md").write_text("\n".join(lines), encoding="utf-8")

    # 赢家 vs 输家
    wl = frame.dropna(subset=["ret_entry_open_to_last_%"]).copy()
    half = max(1, len(wl) // 3)
    winners, losers = wl.head(half), wl.tail(half)
    fam_table = pd.DataFrame({"winner_mean": winners[fam_cols].mean(), "loser_mean": losers[fam_cols].mean()})
    fam_table["diff"] = fam_table["winner_mean"] - fam_table["loser_mean"]
    fam_table = fam_table.sort_values("diff", key=lambda s: -s.abs()).round(4)
    summary = pd.DataFrame({
        "group": ["winner", "loser"],
        "names": [", ".join(winners["stock_code"]), ", ".join(losers["stock_code"])],
        "mean_ret_%": [round(float(winners["ret_entry_open_to_last_%"].mean()), 3),
                       round(float(losers["ret_entry_open_to_last_%"].mean()), 3)],
        "mean_rank": [round(float(winners["rank"].mean()), 1), round(float(losers["rank"].mean()), 1)],
        "mean_raw": [round(float(winners["raw_pred"].mean()), 4), round(float(losers["raw_pred"].mean()), 4)],
        "mean_tilt": [round(float(winners["tilt_points"].mean()), 3), round(float(losers["tilt_points"].mean()), 3)],
        "mean_dist120low": [round(float(winners["dist_from_120d_low"].mean()), 3), round(float(losers["dist_from_120d_low"].mean()), 3)],
        "mean_flowz": [round(float(winners["flow_z_5d"].mean()), 3), round(float(losers["flow_z_5d"].mean()), 3)],
        "mean_volratio": [round(float(winners["volume_ratio_20d"].mean()), 3), round(float(losers["volume_ratio_20d"].mean()), 3)],
    })
    (out_dir / "winners_vs_losers.md").write_text(
        f"# 0924 候选：赢家 vs 输家（按 T+1 开盘→最新收盘）\n\n" + summary.to_markdown(index=False)
        + "\n\n## 族级 SHAP 差（赢家均值 − 输家均值）\n\n" + fam_table.to_markdown()
        + "\n\n## 全部明细\n\n" + wl[["stock_code", "rank", "raw_pred", "tilt_points",
                                     "ret_entry_open_to_last_%", "top_positive_factors"]].to_markdown(index=False) + "\n",
        encoding="utf-8")
    print(summary.to_string(index=False))
    print("\n族级 SHAP 差：\n" + fam_table.to_string())
    print(f"\n[done] {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
