#!/usr/bin/env python3
"""因子值快照（value snapshot）：把某个因子的**值 + 前向收益**冻结成一份可复现的档案。

为什么要它（参考 FactorMiner 的 `factor_db`，2026-10-12）：本仓库的 `factor_library/factors.json` 只存**指标**，
几个月后再看"这条因子当时到底长什么样、标签怎么算的"就无从核对；而面板会随每日清洗推进而变化。
所以对**入池候选/要写进文档的因子**存一份快照：
  `output/factor_library/snapshots/<factor_id>.parquet`（trade_date / stock_code / value / fwd_return_{1,5,20}d）
  `output/factor_library/snapshots/<factor_id>.json`（血缘：面板窗口/清洗版本/行数/股票数/覆盖率/sha256/命令）

用法：
    uv run python scripts/snapshot_factor_values.py --factor tr_volatility_20_clean --months 24
    uv run python scripts/snapshot_factor_values.py --factor <id> --verify      # 与当前面板对账（漂移检测）
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "output/factor_library/snapshots"
HORIZONS = (1, 5, 20)


def _load_inspector_module():
    """复用 factor_inspector 的**同一套**取数与标签逻辑（口径只能有一份）。"""
    spec = importlib.util.spec_from_file_location("factor_inspector_shared", ROOT / "scripts/factor_inspector.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build(factor_id: str, months: int) -> dict:
    module = _load_inspector_module()
    t0 = time.time()
    panel, panel_meta = module.load_panel(
        ["stock_code", "trade_date", "pit_valid", "quality_status", factor_id], months=months)
    if factor_id not in panel.columns or panel.empty:
        return {"status": "missing_column", "factor": factor_id, "panel": panel_meta}
    # 面板加载器会把请求的 `*_clean` 名字 materialize 出来（实测：请求 `foo_clean` 会得到一列全 NaN），
    # 所以"列存在"不足以说明因子真的在面板里 —— **覆盖率为 0 一律当缺列**，并且不留假快照。
    if float(panel[factor_id].notna().mean()) == 0.0:
        return {"status": "missing_column", "factor": factor_id,
                "reason": "列存在但覆盖率 0（面板里没有这个因子；请求 *_clean 时会被 materialize 成全 NaN）",
                "panel": panel_meta}
    if "quality_status" in panel.columns:
        flag = panel["quality_status"].astype(str).str.lower()
        panel = panel[flag.isin(["valid", "ok", "good", ""]) | panel["quality_status"].isna()]
    panel, label_meta = module.attach_forward_returns(panel, months=months, horizons=HORIZONS)
    keep = ["trade_date", "stock_code", factor_id, *[f"fwd_return_{h}d" for h in HORIZONS]]
    snapshot = panel[keep].rename(columns={factor_id: "value"}).sort_values(["trade_date", "stock_code"])
    OUT.mkdir(parents=True, exist_ok=True)
    parquet = OUT / f"{factor_id}.parquet"
    snapshot.to_parquet(parquet, index=False)
    digest = hashlib.sha256(parquet.read_bytes()).hexdigest()
    meta = {
        "status": "ok",
        "factor": factor_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_seconds": round(time.time() - t0, 1),
        "parquet": str(parquet.relative_to(ROOT)),
        "sha256": digest,
        "rows": int(len(snapshot)),
        "dates": int(snapshot["trade_date"].nunique()),
        "stocks": int(snapshot["stock_code"].nunique()),
        "coverage": float(snapshot["value"].notna().mean()),
        "label": {f"h{h}": f"fwd_return_{h}d" for h in HORIZONS},
        "label_source": label_meta,
        "panel": panel_meta,
        "command": f"uv run python scripts/snapshot_factor_values.py --factor {factor_id} --months {months}",
        "reproduce": [
            "标签：clean/ohlcv qfq 收盘 close(t+h)/close(t) − 1（与流水线 label_mode=forward_return 同口径）",
            "因子值：assets/data/feature/clean_feature_panel 的同名列（clean 后）",
            f"校验：uv run python scripts/snapshot_factor_values.py --factor {factor_id} --verify",
        ],
    }
    (OUT / f"{factor_id}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def verify(factor_id: str, months: int) -> dict:
    """重算一遍并与快照对账：值/标签的最大绝对差与覆盖率差（漂移检测）。"""
    path = OUT / f"{factor_id}.parquet"
    if not path.exists():
        return {"status": "no_snapshot", "factor": factor_id}
    old = pd.read_parquet(path)
    module = _load_inspector_module()
    panel, _ = module.load_panel(["stock_code", "trade_date", factor_id], months=months)
    if factor_id not in panel.columns:
        return {"status": "missing_column", "factor": factor_id}
    if "quality_status" in panel.columns:
        flag = panel["quality_status"].astype(str).str.lower()
        panel = panel[flag.isin(["valid", "ok", "good", ""]) | panel["quality_status"].isna()]
    panel = panel[["trade_date", "stock_code", factor_id]].rename(columns={factor_id: "value_now"})
    merged = old.merge(panel, on=["trade_date", "stock_code"], how="inner")
    if merged.empty:
        return {"status": "no_overlap", "factor": factor_id}
    diff = (merged["value"] - merged["value_now"]).abs()
    return {
        "status": "ok",
        "factor": factor_id,
        "overlap_rows": int(len(merged)),
        "max_abs_diff": float(diff.max()),
        "mean_abs_diff": float(diff.mean()),
        "miss_rate_old": float(merged["value"].isna().mean()),
        "miss_rate_now": float(merged["value_now"].isna().mean()),
        "verdict": "identical" if float(diff.max()) == 0 else (
            "close" if float(diff.max()) < 1e-9 else "drift"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="因子值快照 / 对账")
    ap.add_argument("--factor", required=True)
    ap.add_argument("--months", type=int, default=24)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--print", action="store_true")
    args = ap.parse_args()
    payload = verify(args.factor, args.months) if args.verify else build(args.factor, args.months)
    if args.print or True:
        if payload.get("status") == "ok" and "parquet" in payload:
            print(f"[snapshot] {payload['factor']} 行 {payload['rows']:,} / 日 {payload['dates']} / 股 {payload['stocks']}"
                  f" / 覆盖 {payload['coverage']:.1%} / sha {payload['sha256'][:12]}")
            print(f"           -> {payload['parquet']}")
        elif payload.get("status") == "ok":
            print(f"[snapshot] 对账 {payload['factor']}：重叠 {payload['overlap_rows']:,} 行，"
                  f"max|Δ| {payload['max_abs_diff']:.3e} ⇒ {payload['verdict']}")
        else:
            print(f"[snapshot] {payload['factor']}: {payload.get('status')}")
    return 0 if payload.get("status") in ("ok",) else 3


if __name__ == "__main__":
    raise SystemExit(main())
