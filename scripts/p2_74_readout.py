#!/usr/bin/env python3
"""P2.74 S3 冻结表征读出 + 验证期 RankIC（快速版）。

加载 `checkpoints/jepa.pt`，冻结表征，构造 312 维读出特征：
  [126 日历史均值(40) ; 先验(16) ; 锚点(128) ; 修正(128)]
训练 MLP 预测 21 日收益，比较：
  R0 [s,P] / R1 [s,P,anchor] / R2 [s,P,anchor,revision]
用验证期 2022–2023 的逐日 RankIC 判断"修正"是否有增量。

为了可跑，训练/验证各抽样（默认 30k / 8k）；这不是最终全量报告。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STORE = ROOT / "output/research/p2_74_stock_jepa"
INDEX = STORE / "sample_index.parquet"
CHECKPOINT = STORE / "checkpoints/jepa.pt"

SPEC = importlib.util.spec_from_file_location("p2_74_jepa_train", ROOT / "scripts/p2_74_jepa_train.py")
J = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(J)

SPEC2 = importlib.util.spec_from_file_location("p2_74_prior", ROOT / "scripts/p2_74_prior.py")
P = importlib.util.module_from_spec(SPEC2)
assert SPEC2.loader is not None
SPEC2.loader.exec_module(P)


def _load_model(device):
    ckpt = torch.load(CHECKPOINT, map_location=device, weights_only=False)
    cfg = ckpt["config"]
    ctx, tgt, proj, anchor_ema, rev = J.make_model(cfg)
    ctx.load_state_dict(ckpt["ctx"]); tgt.load_state_dict(ckpt["tgt"])
    proj.load_state_dict(ckpt["proj"]); anchor_ema.load_state_dict(ckpt["anchor_ema"])
    rev.load_state_dict(ckpt["rev"])
    for m in (ctx, tgt, proj, anchor_ema, rev):
        m.to(device).eval()
        for p in m.parameters():
            p.requires_grad_(False)
    return ctx, anchor_ema, rev, ckpt.get("feat_mu"), ckpt.get("feat_sd")


def _features(model, arrays, samples, device, batch=64):
    ctx, anchor_ema, rev, feat_mu, feat_sd = model
    ds = J.WindowDataset(samples, arrays, feat_mu=feat_mu, feat_sd=feat_sd)
    if len(ds) == 0:
        return None, None
    loader = DataLoader(ds, batch_size=batch, shuffle=False)
    out = {"r0": [], "r1": [], "r2": []}
    labels = []
    with torch.no_grad():
        for ctx_x, _tgt_x, prior in loader:
            ctx_x = torch.nan_to_num(ctx_x.to(device))
            prior = torch.nan_to_num(prior.to(device))
            h = ctx(ctx_x)
            anchor = anchor_ema(prior)
            revision = rev(h, anchor)
            s = ctx_x.mean(dim=1)
            out["r0"].append(torch.cat([s, prior], dim=1).cpu().numpy())
            out["r1"].append(torch.cat([s, prior, anchor], dim=1).cpu().numpy())
            out["r2"].append(torch.cat([s, prior, anchor, revision], dim=1).cpu().numpy())
            labels.append(_tgt_x.mean(dim=1).cpu().numpy())  # 占位，会被覆盖
    return {k: np.concatenate(v) for k, v in out.items()}, None


def _train_readout(x_train, y_train, x_val, y_val, device, epochs=6):
    x_mu, x_sd = x_train.mean(0), x_train.std(0)
    x_sd = np.where(x_sd > 0, x_sd, 1.0)
    y_mu, y_sd = y_train.mean(), y_train.std() or 1.0
    xt = torch.tensor((x_train - x_mu) / x_sd, dtype=torch.float32, device=device)
    xv = torch.tensor((x_val - x_mu) / x_sd, dtype=torch.float32, device=device)
    yt = torch.tensor((y_train - y_mu) / y_sd, dtype=torch.float32, device=device).unsqueeze(1)
    model = nn.Sequential(
        nn.LayerNorm(xt.shape[1]), nn.Linear(xt.shape[1], 256), nn.GELU(),
        nn.Dropout(0.05), nn.Linear(256, 1),
    ).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=1e-4, weight_decay=0.05)
    for _ in range(epochs):
        perm = torch.randperm(len(xt), device=device)
        for i in range(0, len(xt), 512):
            idx = perm[i:i + 512]
            opt.zero_grad(set_to_none=True)
            loss = F.smooth_l1_loss(model(xt[idx]), yt[idx])
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
    with torch.no_grad():
        pred = model(xv).squeeze(1).cpu().numpy()
    return pred


def run(args) -> dict:
    device = torch.device("mps" if torch.backends.mps.is_available() and not args.cpu else "cpu")
    model = _load_model(device)
    samples = pd.read_parquet(INDEX)
    samples["trade_date"] = pd.to_datetime(samples["trade_date"])
    labels = P.build_labels()
    samples = samples.merge(labels, on=["trade_date", "stock_code"], how="inner")
    train = samples[(samples["trade_date"] >= "2016-01-01") & (samples["trade_date"] <= "2021-12-31")]
    val = samples[(samples["trade_date"] >= args.eval_start) & (samples["trade_date"] <= args.eval_end)]
    train = train.sample(min(args.train, len(train)), random_state=7)
    # 验证期按日分层抽样：保证每个交易日有足够截面算 RankIC（随机抽会导致每天只有十几只）
    per_day = max(10, int(args.val // max(1, val["trade_date"].nunique())))
    parts = [g.sample(min(per_day, len(g)), random_state=11) for _, g in val.groupby("trade_date")]
    val = pd.concat(parts, ignore_index=True) if parts else val.iloc[0:0].copy()
    print(f"[readout] train={len(train):,} val={len(val):,}", flush=True)

    arrays_train = J.load_arrays(tuple(range(2016, 2022)))
    eval_years = tuple(range(pd.Timestamp(args.eval_start).year, pd.Timestamp(args.eval_end).year + 1))
    arrays_val = J.load_arrays(eval_years)
    f_train, _ = _features(model, arrays_train, train, device)
    f_val, _ = _features(model, arrays_val, val, device)
    if f_train is None or f_val is None:
        raise SystemExit("没有可用的样本")
    # WindowDataset 会丢掉缺上下文的样本，按相同规则对齐标签：
    def _valid(frame, arrays):
        keep = []
        for row in frame.itertuples():
            item = arrays.get(str(row.stock_code))
            if item is None:
                continue
            pos = item["pos"].get(pd.Timestamp(row.trade_date).to_datetime64())
            if pos is not None and pos >= 125 and pos + 21 < len(item["X"]):
                keep.append(row.Index)
        return frame.loc[keep]
    train = _valid(train, arrays_train)
    val = _valid(val, arrays_val)
    y_train = train["R21"].to_numpy(np.float64)
    y_val = val["R21"].to_numpy(np.float64)
    result = {}
    preds = {}
    for key in ("r0", "r1", "r2"):
        preds[key] = _train_readout(f_train[key], y_train, f_val[key], y_val, device, args.epochs)
    # 逐日 RankIC（预测 vs 实现 21 日收益）
    for key, pred in preds.items():
        tmp = val[["trade_date"]].copy()
        tmp["pred"] = pred
        tmp["y"] = y_val
        ics = []
        for _, day in tmp.groupby("trade_date"):
            sub = day[["pred", "y"]].dropna()
            if len(sub) >= 10 and sub["pred"].nunique() > 3:
                ics.append(float(sub["pred"].corr(sub["y"], method="spearman")))
        result[key] = {"mean_rank_ic": float(np.mean(ics)) if ics else None, "n_dates": len(ics)}
        print(key, result[key], flush=True)
    outdf = val[["trade_date", "stock_code", "R21", "E21"]].copy()
    for key, pred in preds.items():
        outdf[f"pred_{key}"] = pred
    outdf.to_parquet(STORE / f"readout_preds_{args.tag}.parquet", index=False)
    out = {"train": len(train), "val": len(val), "results": result}
    out["eval_start"] = args.eval_start
    out["eval_end"] = args.eval_end
    (STORE / f"readout_{args.tag}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="P2.74 冻结表征读出")
    ap.add_argument("--train", type=int, default=30000)
    ap.add_argument("--val", type=int, default=8000)
    ap.add_argument("--epochs", type=int, default=6)
    ap.add_argument("--eval-start", default="2022-01-01")
    ap.add_argument("--eval-end", default="2023-12-31")
    ap.add_argument("--tag", default="val")
    ap.add_argument("--cpu", action="store_true")
    args = ap.parse_args()
    run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
