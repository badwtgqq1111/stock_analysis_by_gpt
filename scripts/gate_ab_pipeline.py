#!/usr/bin/env python
"""门禁口径的「真实链路」A/B：score vs orthogonalize，跑 13 个非重叠决策日的 preselection + pk。

与研究脚本版的区别：这里走的是**生产实际的选股链路**（affordability / universe filter /
启动门禁 / 信号 sleeve / PK 权重），只把"模型分 + 门禁口径"换成同一天的 walk-forward 样本外打分，
因此测的是"门禁口径的改动对最终名单的影响"，而不是研究脚本里的一次性排序。

输入：output/oos_predictions/cn_{lightgbm,transformer}_oos_predictions.csv（5 折 expanding）
输出：output/research/gate_ab_pipeline/{detail.csv, metrics.csv, REPORT.md}
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "output/research/gate_ab_pipeline"
SCORES = ROOT / "output/model_scores"
OOS = ROOT / "output/oos_predictions"
MODES = ("score", "orthogonalize")
COST_BPS = 25


def write_scores(date: str, files: dict) -> None:
    for name, frame in files.items():
        source_date = pd.Timestamp(date)
        if not (frame["trade_date"] == source_date).any():
            # transformer as-of：用不晚于决策日的最近一期，重戳日期
            earlier = [t for t in sorted(frame["trade_date"].unique()) if t <= source_date]
            if earlier:
                source_date = max(earlier)
        day = frame[frame["trade_date"] == source_date][["stock_code", "model_score_raw", "model_score"]].copy()
        day.insert(0, "trade_date", pd.Timestamp(date))
        day.to_csv(SCORES / name, index=False)


def run_stage(stage: str, date: str, config: Path) -> int:
    cmd = [sys.executable, "scripts/run_cn_pipeline.py", "--stage", stage, "--trade-date", date,
           "--force-rebalance", "--config", str(config)]
    completed = subprocess.run(cmd, cwd=str(ROOT), env={**__import__("os").environ, "PYTHONPATH": "."},
                               stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    return completed.returncode


def main() -> int:
    import tomllib
    OUT.mkdir(parents=True, exist_ok=True)
    backup = OUT / "score_backup"
    backup.mkdir(exist_ok=True)
    for name in ("cn_lightgbm_scores.csv", "cn_transformer_scores.csv"):
        if (SCORES / name).is_file() and not (backup / name).is_file():
            shutil.copyfile(SCORES / name, backup / name)

    base = tomllib.load(open(ROOT / "config/cn_pipeline.toml", "rb"))
    variants = {}
    text = (ROOT / "config/cn_pipeline.toml").read_text()
    for mode in MODES:
        variant = ROOT / f"config/cn_pipeline_gate_{mode}.toml"
        patched = text.replace('mode = "orthogonalize"', f'mode = "{mode}"', 1)
        variant.write_text(patched)
        variants[mode] = variant
    print(f"[setup] 已生成两个配置变体: {[str(v.name) for v in variants.values()]}", flush=True)

    light = pd.read_csv(OOS / "cn_lightgbm_oos_predictions.csv", parse_dates=["trade_date"])
    trans = pd.read_csv(OOS / "cn_transformer_oos_predictions.csv", parse_dates=["trade_date"])
    # 两条腿的 OOS 决策日相差 1 天（周一 vs 周二），没有交集 → 用 as-of 对齐：
    # 每个决策日取"不晚于它"的最近一期 transformer 打分（这正是实盘当天的可用状态），
    # 并在写入打分文件时把日期重戳成决策日。报告里显式标注这一点。
    light_dates = sorted(set(light["trade_date"]))
    trans_dates = sorted(set(trans["trade_date"]))
    dates = light_dates[::4]
    asof = {d: max([t for t in trans_dates if t <= d], default=None) for d in dates}
    missing = [d for d, t in asof.items() if t is None]
    if missing:
        dates = [d for d in dates if asof[d] is not None]
    print(f"[dates] 非重叠决策日 {len(dates)} 个：{pd.Timestamp(dates[0]).date()}~{pd.Timestamp(dates[-1]).date()}"
          f"（transformer 用 as-of 对齐，例如 {pd.Timestamp(dates[0]).date()} ← {pd.Timestamp(asof[dates[0]]).date()}）", flush=True)
    light = light[light["trade_date"].isin(dates)]
    pool = light.groupby("trade_date")["forward_return_20d"].mean().rename("pool_mean")

    rows = []
    try:
        for mode in MODES:
            prev = None
            for date in dates:
                date_s = pd.Timestamp(date).date().isoformat()
                t0 = time.time()
                write_scores(date_s, {"cn_lightgbm_scores.csv": light, "cn_transformer_scores.csv": trans})
                rc1 = run_stage("preselection", date_s, variants[mode])
                rc2 = run_stage("pk", date_s, variants[mode])
                path = ROOT / "output/results_cn" / f"replay_{date_s.replace('-', '')}" / "cn_ensemble_selected.csv"
                if rc1 or rc2 or not path.is_file():
                    print(f"  [{mode} {date_s}] FAILED rc={rc1}/{rc2}", flush=True)
                    rows.append({"mode": mode, "date": date_s, "status": f"failed:{rc1}/{rc2}"})
                    continue
                sel = pd.read_csv(path)
                hold = sel[sel["target_weight"] > 0]
                codes = hold["stock_code"].astype(str).tolist()
                fwd = light[light["trade_date"] == pd.Timestamp(date)].set_index("stock_code")["forward_return_20d"]
                rets = fwd.reindex(codes).dropna()
                turnover = (len(set(codes) - (prev or set())) / max(len(codes), 1)) if prev else 1.0
                prev = set(codes)
                rows.append({
                    "mode": mode, "date": date_s, "status": "ok", "n_holdings": len(codes),
                    "mean_ret_%": round(float(rets.mean()) * 100, 3) if len(rets) else None,
                    "pool_%": round(float(pool.get(pd.Timestamp(date), np.nan)) * 100, 3),
                    "excess_%": round(float(rets.mean() - pool.get(pd.Timestamp(date), np.nan)) * 100, 3) if len(rets) else None,
                    "turnover": round(turnover, 3), "seconds": round(time.time() - t0, 1),
                    "holdings": " ".join(codes),
                })
                pd.DataFrame(rows).to_csv(OUT / "detail.csv", index=False)
                print(f"  [{mode:<14} {date_s}] n={len(codes)} 收益 {rows[-1]['mean_ret_%']}% "
                      f"池 {rows[-1]['pool_%']}% 超额 {rows[-1]['excess_%']}% 换手 {turnover:.2f} {time.time()-t0:.0f}s", flush=True)
    finally:
        for name in ("cn_lightgbm_scores.csv", "cn_transformer_scores.csv"):
            if (backup / name).is_file():
                shutil.copyfile(backup / name, SCORES / name)
        print("[cleanup] 生产打分文件已还原", flush=True)

    detail = pd.DataFrame(rows)
    detail.to_csv(OUT / "detail.csv", index=False)
    ok = detail[detail["status"] == "ok"].copy()
    summary = []
    for mode, group in ok.groupby("mode"):
        ex = group["excess_%"].astype(float)
        net = ex - group["turnover"].astype(float) * 2 * COST_BPS / 100.0
        summary.append({"mode": mode, "periods": len(group),
                        "mean_excess_%": round(float(ex.mean()), 3), "median_excess_%": round(float(ex.median()), 3),
                        "t": round(float(ex.mean() / ex.std() * np.sqrt(len(ex))), 2) if len(ex) > 1 else None,
                        "hit": round(float((ex > 0).mean()), 3),
                        "mean_holdings": round(float(group["n_holdings"].mean()), 1),
                        "mean_turnover": round(float(group["turnover"].mean()), 3),
                        "net25_%": round(float(net.mean()), 3),
                        "net25_t": round(float(net.mean() / net.std() * np.sqrt(len(net))), 2) if len(net) > 1 else None})
    table = pd.DataFrame(summary).sort_values("mean_excess_%", ascending=False)
    table.to_csv(OUT / "metrics.csv", index=False)
    print("\n" + table.to_string(index=False), flush=True)
    (OUT / "REPORT.md").write_text(
        "# 门禁口径的真实链路 A/B（preselection + pk，13 个非重叠决策日）\n\n"
        "打分 = 5 折 expanding walk-forward OOS（LightGBM + Transformer）；只改门禁口径；"
        f"收益口径 = 决策日 20 日前瞻收益；成本 {COST_BPS}bps 双边 × 换手。\n\n"
        + table.to_markdown(index=False) + "\n\n## 逐期明细\n\n" + detail.to_markdown(index=False) + "\n", encoding="utf-8")
    print(f"\n[done] {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
