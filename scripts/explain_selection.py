"""逐股「为什么被选中」归因：LightGBM SHAP(pred_contrib) + 启动门禁倾斜分解。

位置说明（2026-10-09）：本脚本原来只躺在
`output/verification/p1_22_shap_attribution_20260925/explain_selection.py` —— 它是一次性
验证的产物，但其实是 `pick_attribution_report.py` 的**上游依赖**（后者读它生成的
`shap_by_stock_/shap_detail_/gate_decomposition_<date>.csv`）。
因为归因分析要接进每日流程，这里把它搬进 `scripts/` 并保持行为不变。

用法：
  uv run python scripts/explain_selection.py 2026-10-08 [code,code,...]

输出（写到 `output/verification/p1_22_shap_attribution_20260925/`，与历史产物同目录，
便于 `pick_attribution_report.py` 直接找到）：
  shap_detail_<date>.csv / shap_by_stock_<date>.csv / family_attribution_by_stock_<date>.csv
  gate_decomposition_<date>.csv
"""
import argparse
import json
import re
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

# ROOT 必须是**仓库根**：脚本在 <repo>/scripts/ 下，parents[1] = 仓库根。
# 原来写的是 parents[3]（= /Users/ccs/code），于是 sys.path 里没有仓库根，
# `import factor_engine` 直接 ModuleNotFoundError —— 2026-10-10 02:45 的每日归因步骤
# 就是这样静默失败的（日志里只有这一行 Traceback，页面停在 10-08）。
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lightgbm as lgb  # noqa: E402
import tomllib  # noqa: E402
from factor_engine.ml.preprocessing import preprocess_features_by_date  # noqa: E402
from factor_engine.ml.strategy_labels import build_cn_strategy_labels  # noqa: E402
from data.ingest.service import MarketDataService  # noqa: E402

SHAP_DEFAULT_DIR = ROOT / "output/verification/p1_22_shap_attribution_20260925"
MANIFEST = ROOT / "output/models/cn/lightgbm/alpha_zoo_hk/model_manifest.json"
MODEL = ROOT / "output/models/cn/lightgbm/alpha_zoo_hk/model.txt"
MATCHERS = [
    (r"^GTJA\d+", "GTJA191"), (r"^ALPHA101_", "Alpha101"), (r"^TA_", "TA-Lib"),
    (r"^moneyflow", "moneyflow/三把锁"), (r"^flow_second_wave", "flow_second_wave"),
    (r"^valuation|^pe_|^pb_|^roe_|^gross_margin|^quality_value|^growth_quality", "valuation/质量"),
    (r"^financial", "financial"), (r"^liquidity", "liquidity"), (r"^payoff", "payoff"),
    (r"^vol_", "vol_波动率"), (r"^vr_", "vr_量能"), (r"^tr_|^turnover", "tr_/turnover"),
    (r"^pv_", "pv_价量"), (r"^academic", "academic"), (r"^calendar", "calendar"),
    (r"^RPS", "RPS"), (r"^sector", "sector"), (r"^momentum", "momentum"),
    (r"^short_term_reversal", "short_term_reversal"), (r"^price_position", "price_position"),
    (r"^consecutive", "consecutive"), (r"^buying_pressure", "buying_pressure"),
    (r"^volume_price", "volume_price_divergence"),
]
UPPER = re.compile(r"^[A-Z]+[0-9]*[A-Z0-9]*$")


def family(column: str) -> str:
    base = column[:-11] if column.endswith("_is_missing") else (column[:-6] if column.endswith("_clean") else column)
    for pattern, name in MATCHERS:
        if re.match(pattern, base):
            return name
    return "Alpha158(K线/窗口)" if UPPER.match(base) else "其他"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("trade_date")
    ap.add_argument("codes", nargs="?", default="")
    # 产物目录必须显式给默认值：下游 pick_attribution_report.py 读的是这个目录。
    ap.add_argument("--out-dir", default=str(SHAP_DEFAULT_DIR))
    args = ap.parse_args()
    date = args.trade_date
    # 原来这里是 Path(__file__).resolve().parent —— 脚本搬到 scripts/ 之后，
    # 三个 SHAP 产物（shap_detail 30MB）就写进了 scripts/ 目录，下游按约定路径找不到，
    # 每日归因链第二步直接 FileNotFoundError（2026-10-10 实测）。
    out_dir = Path(args.out_dir)

    manifest = json.load(open(MANIFEST))
    cols = manifest["feature_columns"]
    booster = lgb.Booster(model_file=str(MODEL))
    cfg = tomllib.load(open(ROOT / "config/cn_pipeline.toml", "rb"))
    gate_settings = dict(cfg["selection"]["startup_gate"])
    strength = float(gate_settings.get("score_strength", 0.0) or 0.0)

    svc = MarketDataService(base_dir=str(ROOT / "assets/data"))
    panel, _ = svc.read_clean_feature_panel(market="CN", factor_set=manifest["factor_set"],
                                            start_date=date, end_date=date,
                                            cleaning_version=manifest["cleaning_version"])
    # 列名兼容：clean panel 同时有 `trade_date` 与 `trade_date_clean`，但某些读取路径（例如
    # 只给 feature_columns 的 manifest 路径）只回后者 —— 2026-10-11/12 连续两天 attribution 就死在
    # `panel["trade_date"]` 的 KeyError 上。这里统一取存在的那个，取不到就直说。
    date_col = next((name for name in ("trade_date", "trade_date_clean") if name in panel.columns), None)
    if date_col is None:
        raise SystemExit(f"[explain] 面板里没有 trade_date/trade_date_clean（列：{list(panel.columns)[:8]}…）")
    panel = panel[pd.to_datetime(panel[date_col]).astype(str).str.startswith(date)].reset_index(drop=True)
    X = panel.reindex(columns=cols).astype(float)
    merged = pd.concat([panel[[date_col, "stock_code"]].rename(columns={date_col: "trade_date"}), X], axis=1)
    proc_cfg = dict(manifest["preprocessing"].get("config") or {"mode": "qlib_robust"})
    mode = proc_cfg.pop("mode", "qlib_robust")
    Xp, _ = preprocess_features_by_date(merged, cols, mode=mode, **proc_cfg)
    contrib = booster.predict(Xp[cols].to_numpy(dtype=float), pred_contrib=True)
    shap = pd.DataFrame(contrib[:, :-1], columns=cols)
    bias = contrib[:, -1]
    pred = shap.sum(axis=1) + bias
    codes_all = panel["stock_code"].astype(str).to_numpy()

    # 归因表（长表）
    long = shap.T.copy()
    long.columns = codes_all
    detail = long.reset_index().rename(columns={"index": "feature"})
    detail["family"] = detail["feature"].map(family)
    detail = detail.melt(id_vars=["feature", "family"], var_name="stock_code", value_name="shap")
    detail["abs_shap"] = detail["shap"].abs()

    by_stock = pd.DataFrame({"stock_code": codes_all, "raw_pred": pred, "bias": bias})
    by_stock["shap_sum"] = shap.sum(axis=1).to_numpy()
    by_stock["rank"] = by_stock["raw_pred"].rank(ascending=False, method="first")
    fam = detail.pivot_table(index="stock_code", columns="family", values="shap", aggfunc="sum").fillna(0.0)
    fam.columns = [f"fam_{c}" for c in fam.columns]
    by_stock = by_stock.merge(fam.reset_index(), on="stock_code", how="left")

    # 门禁倾斜分解（复刻 startup_gate_score 的四个分项）
    lookback = int(gate_settings.get("lookback_days", 400) or 400)
    start = (pd.Timestamp(date) - pd.Timedelta(days=lookback)).strftime("%Y-%m-%d")
    prices = svc.warehouse.read_ohlcv(market="CN", asset_type="equity", frequency="daily", adjust="qfq",
                                      start_date=start, end_date=date)
    prices = prices[[c for c in ["stock_code", "trade_date", "open", "high", "low", "close"] if c in prices.columns]].copy()
    prices["trade_date"] = pd.to_datetime(prices["trade_date"], errors="coerce")
    prices = prices.sort_values(["stock_code", "trade_date"]).drop_duplicates(["stock_code", "trade_date"], keep="last")
    session = max(v for v in prices["trade_date"].dropna().unique() if pd.Timestamp(v) <= pd.Timestamp(date))
    labels = build_cn_strategy_labels(prices, path_horizon=int(gate_settings.get("path_horizon", 60)),
                                      startup_low_window=int(gate_settings.get("low_window", 120)), entry_delay=1)
    labels = labels.loc[labels["trade_date"] == pd.Timestamp(session)].copy()
    ctx, _ = svc.read_clean_feature_panel(start_date=str(pd.Timestamp(session).date()),
                                          end_date=str(pd.Timestamp(session).date()),
                                          feature_columns=["pv_volume_ratio_20d", "moneyflow_net_z_5d"])
    ctx = ctx.rename(columns={"pv_volume_ratio_20d_clean": "volume_ratio", "moneyflow_net_z_5d_clean": "flow_z"})
    labels = labels.merge(ctx[["stock_code", "volume_ratio", "flow_z"]], on="stock_code", how="left")

    band_lo = float(gate_settings.get("score_band_min_dist_from_120d_low", 0.05))
    band_hi = float(gate_settings.get("score_band_max_dist_from_120d_low", 0.30))
    decay_hi = float(gate_settings.get("score_decay_max_dist_from_120d_low", 0.55))
    high_floor = float(gate_settings.get("score_high_distance_floor", -0.05))
    high_span = float(gate_settings.get("score_high_distance_span", 0.15))
    vol_hi = float(gate_settings.get("score_volume_ratio_max", 1.2))
    vol_span = float(gate_settings.get("score_volume_ratio_span", 0.4))
    flow_ref = float(gate_settings.get("score_flow_z_reference", 1.0))
    w = {"dist": float(gate_settings.get("score_weight_dist", 1.0)),
         "high": float(gate_settings.get("score_weight_high", 0.6)),
         "volume": float(gate_settings.get("score_weight_volume", 0.4)),
         "flow": float(gate_settings.get("score_weight_flow", 0.6))}
    dist = pd.to_numeric(labels["dist_from_120d_low"], errors="coerce")
    k_dist = pd.Series(0.0, index=labels.index)
    k_dist = k_dist.mask(dist.le(band_lo), (dist / max(band_lo, 1e-6)).clip(0, 1))
    k_dist = k_dist.mask(dist.gt(band_lo) & dist.le(band_hi), 1.0)
    if decay_hi > band_hi:
        k_dist = k_dist.mask(dist.gt(band_hi) & dist.le(decay_hi), ((decay_hi - dist) / (decay_hi - band_hi)).clip(0, 1))
    high = pd.to_numeric(labels["dist_from_60d_high"], errors="coerce")
    k_high = ((-high + high_floor) / high_span).clip(0, 1).fillna(0.0)
    ratio = pd.to_numeric(labels["volume_ratio"], errors="coerce")
    k_vol = ((vol_hi - ratio) / vol_span).clip(0, 1).fillna(0.0)
    flow = pd.to_numeric(labels["flow_z"], errors="coerce")
    k_flow = (flow / flow_ref).clip(0, 1).fillna(0.0)
    total_w = w["dist"] + w["high"] + w["volume"] + w["flow"]
    k = (k_dist * w["dist"] + k_high * w["high"] + k_vol * w["volume"] + k_flow * w["flow"]) / total_w
    gate_decomp = pd.DataFrame({
        "stock_code": labels["stock_code"].astype(str), "dist_from_120d_low": dist, "dist_from_60d_high": high,
        "volume_ratio_20d": ratio, "flow_z_5d": flow,
        "k_dist": k_dist, "k_high": k_high, "k_volume": k_vol, "k_flow": k_flow,
        "gate_score": k, "tilt_points": 100 * strength * (k - k.mean()),
        "tilt_from_dist": 100 * strength * (k_dist - k_dist.mean()) * w["dist"] / total_w,
        "tilt_from_high": 100 * strength * (k_high - k_high.mean()) * w["high"] / total_w,
        "tilt_from_volume": 100 * strength * (k_vol - k_vol.mean()) * w["volume"] / total_w,
        "tilt_from_flow": 100 * strength * (k_flow - k_flow.mean()) * w["flow"] / total_w,
    })

    by_stock = by_stock.merge(gate_decomp, on="stock_code", how="left")
    detail.to_csv(out_dir / f"shap_detail_{date}.csv", index=False, float_format="%.6g")
    by_stock.to_csv(out_dir / f"shap_by_stock_{date}.csv", index=False, float_format="%.6g")
    gate_decomp.to_csv(out_dir / f"gate_decomposition_{date}.csv", index=False, float_format="%.6g")

    csv = ROOT / "output/model_scores/cn_lightgbm_scores.csv"
    if csv.is_file():
        sc = pd.read_csv(csv)
        sc = sc[sc["trade_date"].astype(str) == date]
        chk = by_stock.merge(sc[["stock_code", "model_score_raw"]], on="stock_code", how="inner")
        gap = (chk["raw_pred"] - chk["model_score_raw"]).abs()
        print(f"[check] bias+ΣSHAP vs persisted model_score_raw: n={len(chk)} max|gap|={gap.max():.3e}")

    targets = [c for c in args.codes.split(",") if c] or by_stock.sort_values("raw_pred", ascending=False).head(8)["stock_code"].tolist()
    print(f"\n=== {date} 逐股归因（模型 raw 分前 8 + 指定名单）===")
    for code in targets:
        row = by_stock[by_stock.stock_code == code]
        if row.empty:
            print(f"{code}: 无面板记录"); continue
        row = row.iloc[0]
        top = detail[detail.stock_code == code].nlargest(6, "abs_shap")
        fams = {k: v for k, v in row.items() if str(k).startswith("fam_") and abs(v) > 1e-4}
        fams = dict(sorted(fams.items(), key=lambda kv: -abs(kv[1]))[:5])
        print(f"\n{code}  raw={row.raw_pred:.4f} rank={int(row['rank'])} "
              f"| tilt={row.tilt_points:+.2f}分 (dist {row.tilt_from_dist:+.2f} / high {row.tilt_from_high:+.2f} / "
              f"vol {row.tilt_from_volume:+.2f} / flow {row.tilt_from_flow:+.2f}) "
              f"| dist120low={row.dist_from_120d_low:.3f} dist60high={row.dist_from_60d_high:.3f} "
              f"volratio={row.volume_ratio_20d:.2f} flowz={row.flow_z_5d}")
        print("   族贡献: " + ", ".join(f"{k[4:]}={v:+.4f}" for k, v in fams.items()))
        for _, t in top.iterrows():
            print(f"     {t['shap']:+.4f}  [{t['family']}] {t['feature']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
