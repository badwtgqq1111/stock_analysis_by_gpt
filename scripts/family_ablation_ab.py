#!/usr/bin/env python
"""M1 因子族消融 A/B：同一面板、同一划分、同一超参，只换输入特征集。

臂：full / masks_pruned（去掉常量缺失掩码列）/ 逐个去掉 Alpha101、GTJA191、Alpha158、TA-Lib、
moneyflow(+flow_second_wave)、valuation/质量。
评价：与生产训练同口径的验证集日均 rank IC、验证 MSE、以及"按预测取 Top-50 的 label 提升"
（label 已是当日横截面 rank，因此提升是干净的横截面量）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from data.ingest.service import MarketDataService  # noqa: E402
from factor_engine.ml.model_training import train_lightgbm_panel  # noqa: E402

OUT = ROOT / "output/research/family_ablation"
UPPER = re.compile(r"^[A-Z]+[0-9]*[A-Z0-9]*$")


def base(column: str) -> str:
    return column[:-11] if column.endswith("_is_missing") else (column[:-6] if column.endswith("_clean") else column)


def family(column: str) -> str:
    b = base(column)
    for pattern, name in ((r"^GTJA\d+", "GTJA191"), (r"^ALPHA101_", "Alpha101"), (r"^TA_", "TA-Lib"),
                          (r"^moneyflow", "moneyflow"), (r"^flow_second_wave", "moneyflow"),
                          (r"^valuation|^pe_|^pb_|^roe_|^gross_margin|^quality_value|^growth_quality", "valuation"),
                          (r"^financial", "financial"), (r"^liquidity", "liquidity"), (r"^payoff", "payoff"),
                          (r"^vol_", "vol"), (r"^vr_", "vr"), (r"^tr_|^turnover", "tr"), (r"^pv_", "pv"),
                          (r"^academic", "academic")):
        if re.match(pattern, b):
            return name
    return "Alpha158" if UPPER.match(b) else "other"


def main() -> int:
    cfg = json.load(open(ROOT / "config/cn_pipeline.toml")) if False else None
    import tomllib
    cfg = tomllib.load(open(ROOT / "config/cn_pipeline.toml", "rb"))
    lg = cfg["lightgbm"]
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None, help="面板窗口天数；默认用 [lightgbm].days（与生产一致）")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    svc = MarketDataService(base_dir=str(ROOT / "assets/data"))
    print("[ablation] 读取面板与标签（与生产同一入口）…", flush=True)
    panel, features, label_column = svc._clean_panel_training_data(
        market="CN", factor_set=cfg["pipeline"]["factor_set"], frequency="daily", adjust="qfq",
        days=int(args.days if args.days is not None else lg["days"]), label_horizon=int(lg["label_horizon"]),
        cleaning_version=cfg["clean_panel"]["cleaning_version"], label_mode=lg["label_mode"],
        startup_only=False, label_path_horizon=60, preserve_startup_context=False,
        min_stock_count=int(cfg["pipeline"]["min_training_stocks"]), end_date=None, show_progress=True,
    )
    print(f"[ablation] 面板 {len(panel):,} 行 / 特征 {len(features):,} 列 / 标签 {label_column}", flush=True)

    masks = [c for c in features if c.endswith("_is_missing")]
    constant_masks = [c for c in masks if panel[c].nunique(dropna=False) <= 1]
    arms = {
        "full": list(features),
        "masks_pruned": [c for c in features if c not in set(constant_masks)],
        "no_Alpha101": [c for c in features if family(c) != "Alpha101"],
        "no_GTJA191": [c for c in features if family(c) != "GTJA191"],
        "no_Alpha158": [c for c in features if family(c) != "Alpha158"],
        "no_TA-Lib": [c for c in features if family(c) != "TA-Lib"],
        "no_moneyflow": [c for c in features if family(c) != "moneyflow"],
        "no_valuation": [c for c in features if family(c) != "valuation"],
        "no_all_base_zoo": [c for c in features if family(c) not in {"Alpha101", "GTJA191", "Alpha158"}],
    }
    rows = []
    for name, cols in arms.items():
        t0 = time.time()
        res = train_lightgbm_panel(
            panel, cols, model_dir=OUT / "models" / name, label_column=label_column,
            validation_days=int(lg["validation_days"]), cleaning_version=cfg["clean_panel"]["cleaning_version"],
            factor_set=cfg["pipeline"]["factor_set"], embargo_days=lg.get("embargo_days"),
            min_feature_coverage=float(cfg["model_features"]["min_feature_coverage"]),
            drop_constant_features=bool(cfg["model_features"]["drop_constant_features"]),
            n_estimators=int(lg["n_estimators"]), learning_rate=float(lg["learning_rate"]),
            num_leaves=int(lg["num_leaves"]), max_depth=int(lg["max_depth"]),
            min_child_samples=int(lg["min_child_samples"]), reg_lambda=float(lg["reg_lambda"]),
            early_stopping_rounds=int(lg["early_stopping_rounds"]), min_trees=int(lg["min_trees"]),
            eval_metric=lg["eval_metric"], show_progress=False,
        )
        secs = time.time() - t0
        ic = (res.get("validation_ic") or {}).get("value")
        used = res["artifact"]["feature_count"] if isinstance(res.get("artifact"), dict) else len(cols)
        # Top-50 提升：用刚训练出的模型对验证集打分（验证窗口取 manifest 的 validation_start/end）
        import lightgbm as lgb
        from factor_engine.ml.preprocessing import preprocess_features_by_date
        man = json.load(open(res["artifact"]["manifest_path"]))
        booster = lgb.Booster(model_file=res["artifact"]["model_path"])
        vcols = man["feature_columns"]
        proc_cfg = dict(man["preprocessing"].get("config") or {"mode": "qlib_robust"})
        mode = proc_cfg.pop("mode", "qlib_robust")
        frame = panel.copy()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"])
        frame = frame[(frame["trade_date"] >= pd.Timestamp(man["validation_start"])) &
                      (frame["trade_date"] <= pd.Timestamp(man["validation_end"]))]
        Xp, _ = preprocess_features_by_date(frame, vcols, mode=mode, **proc_cfg)
        frame = frame.assign(pred=booster.predict(Xp[vcols].to_numpy(dtype=float)))
        lift = (frame.sort_values(["trade_date", "pred"], ascending=[True, False]).groupby("trade_date").head(50)
                .groupby("trade_date")[label_column].mean() - frame.groupby("trade_date")[label_column].mean()).mean()
        rows.append({"arm": name, "features_in": len(cols), "features_used": int(used),
                     "shared_features": len(vcols), "val_rows": int(res["artifact"]["validation_rows"]) if "validation_rows" in (res.get("artifact") or {}) else None,
                     "val_ic": ic, "val_ic_best": (res.get("validation_ic") or {}).get("best"),
                     "trees_kept": res.get("trees_kept"), "top50_lift": float(lift), "seconds": round(secs, 1)})
        print(f"  {name:<16} cols={len(cols):>5} used={used:>5} valIC={ic if ic is None else round(ic,4)} "
              f"lift={lift:+.4f} {secs:.0f}s", flush=True)
    res_df = pd.DataFrame(rows).sort_values("val_ic", ascending=False)
    tag = f"days{int(args.days if args.days is not None else lg['days'])}"
    res_df.to_csv(OUT / f"family_ablation_{tag}.csv", index=False)
    print("\n=== 族级消融（同面板/同划分/同超参）===")
    pd.set_option("display.width", 200)
    print(res_df.round(4).to_string(index=False))
    print("\n基线 full 的 val_ic =", res_df.loc[res_df.arm == "full", "val_ic"].iloc[0])
    print("输出:", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
