#!/usr/bin/env python3
"""P2.74 S2：STOCK-JEPA 表征训练（论文双分支的最小实现）。

结构：
  * 上下文编码器：126×40 → 128（4 层 Transformer / 8 头 / FFN 512）
  * 目标编码器：21×40 → 128（同结构，EMA 0.996，无梯度）
  * 先验投影器：16 → 128；锚点 EMA 0.99
  * 修正预测器：concat(上下文, detach 锚点) → 256 → 256 → 128
  * 两个独立损失：锚点损失只更新投影器；修正损失只更新上下文+修正器

用法：
  python3 scripts/p2_74_jepa_train.py --smoke
  python3 scripts/p2_74_jepa_train.py --epochs 4 --steps 1000
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STORE = ROOT / "output/research/p2_74_stock_jepa"
FEATURE_DIR = STORE / "features"
PRIOR_DIR = STORE / "prior"
INDEX = STORE / "sample_index.parquet"
CONFIG = ROOT / "config/p2_74_stock_jepa.toml"


def load_config() -> dict:
    import tomllib
    with CONFIG.open("rb") as fh:
        return tomllib.load(fh)


def load_arrays(years: tuple[int, ...]) -> dict:
    """code → {dates, X, P}；只加载指定年份，控制 smoke 内存。"""
    arrays = {}
    for year in years:
        fp = FEATURE_DIR / f"year={year}" / "part.parquet"
        pp = PRIOR_DIR / f"year={year}" / "part.parquet"
        if not fp.exists() or not pp.exists():
            continue
        feat = pd.read_parquet(fp)
        prior = pd.read_parquet(pp)
        feat["trade_date"] = pd.to_datetime(feat["trade_date"])
        prior["trade_date"] = pd.to_datetime(prior["trade_date"])
        merged = feat.merge(prior, on=["trade_date", "stock_code"], how="inner")
        fcols = [c for c in feat.columns if c not in ("trade_date", "stock_code")]
        pcols = [c for c in prior.columns if c not in ("trade_date", "stock_code")]
        for code, sub in merged.groupby("stock_code", sort=False):
            sub = sub.sort_values("trade_date")
            item = arrays.setdefault(code, {"dates": [], "X": [], "P": []})
            item["dates"].append(sub["trade_date"].to_numpy("datetime64[ns]"))
            item["X"].append(sub[fcols].to_numpy(np.float32))
            item["P"].append(sub[pcols].to_numpy(np.float32))
        print(f"[arrays] {year} rows={len(merged):,}", flush=True)
    for code, item in arrays.items():
        item["dates"] = np.concatenate(item["dates"])
        item["X"] = np.concatenate(item["X"])
        item["P"] = np.concatenate(item["P"])
        item["pos"] = {d: i for i, d in enumerate(item["dates"])}
    return arrays


class WindowDataset(Dataset):
    def __init__(self, samples: pd.DataFrame, arrays: dict, context: int = 126, target: int = 21,
                 feat_mu=None, feat_sd=None):
        self.arrays = arrays
        self.context = context
        self.target = target
        self.feat_mu = feat_mu
        self.feat_sd = feat_sd
        items = []
        for row in samples.itertuples():
            code = str(row.stock_code)
            item = arrays.get(code)
            if item is None:
                continue
            pos = item["pos"].get(pd.Timestamp(row.trade_date).to_datetime64())
            if pos is None or pos < context - 1 or pos + target >= len(item["X"]):
                continue
            items.append((code, int(pos)))
        self.items = items

    def __len__(self):
        return len(self.items)

    def __getitem__(self, idx):
        code, pos = self.items[idx]
        item = self.arrays[code]
        ctx = item["X"][pos - self.context + 1: pos + 1]
        tgt = item["X"][pos + 1: pos + 1 + self.target]
        if self.feat_mu is not None and self.feat_sd is not None:
            ctx = np.clip((ctx - self.feat_mu) / self.feat_sd, -8, 8)
            tgt = np.clip((tgt - self.feat_mu) / self.feat_sd, -8, 8)
            ctx = np.nan_to_num(ctx, nan=0.0, posinf=8.0, neginf=-8.0)
            tgt = np.nan_to_num(tgt, nan=0.0, posinf=8.0, neginf=-8.0)
        prior = item["P"][pos]
        return torch.from_numpy(ctx), torch.from_numpy(tgt), torch.from_numpy(prior)


class SeqEncoder(nn.Module):
    def __init__(self, in_dim=40, d=128, layers=4, heads=8, ff=512, dropout=0.1, max_len=130):
        super().__init__()
        self.proj = nn.Linear(in_dim, d)
        self.cls = nn.Parameter(torch.zeros(1, 1, d))
        self.mask_token = nn.Parameter(torch.zeros(1, 1, d))
        self.pos = nn.Parameter(torch.zeros(1, max_len, d))
        layer = nn.TransformerEncoderLayer(
            d_model=d, nhead=heads, dim_feedforward=ff, dropout=dropout,
            activation="gelu", batch_first=True, norm_first=True,
        )
        self.enc = nn.TransformerEncoder(layer, num_layers=layers)
        self.norm = nn.LayerNorm(d)

    def forward(self, x, mask=None):
        h = self.proj(x)
        if mask is not None:
            h = torch.where(mask.unsqueeze(-1), self.mask_token, h)
        cls = self.cls.expand(h.shape[0], -1, -1)
        h = torch.cat([cls, h], dim=1) + self.pos[:, : h.shape[1] + 1]
        h = self.enc(h)
        return self.norm(h[:, 0])


def random_context_mask(batch: int, steps: int, device, min_frac: float = 0.30,
                        block_min: int = 5, block_max: int = 21) -> torch.Tensor:
    """论文掩码：连续 5~21 天块，直到至少 30% 上下文被遮。"""
    mask = torch.zeros(batch, steps, dtype=torch.bool, device=device)
    for b in range(batch):
        guard = 0
        while mask[b].float().mean() < min_frac and guard < 20:
            length = int(torch.randint(block_min, block_max + 1, (1,)).item())
            start = int(torch.randint(0, max(1, steps - length), (1,)).item())
            mask[b, start:start + length] = True
            guard += 1
    return mask


class PriorProjector(nn.Module):
    def __init__(self, in_dim=16, d=128):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(in_dim, d), nn.LayerNorm(d), nn.GELU(), nn.Linear(d, d),
        )

    def forward(self, x):
        return self.net(x)


class RevisionPredictor(nn.Module):
    def __init__(self, d=128, hidden=256, dropout=0.1):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(2 * d, hidden), nn.GELU(), nn.Dropout(dropout),
            nn.Linear(hidden, hidden), nn.GELU(), nn.Dropout(dropout),
            nn.Linear(hidden, d),
        )

    def forward(self, context, anchor):
        return self.net(torch.cat([context, anchor], dim=-1))


@torch.no_grad()
def ema_update(target: nn.Module, online: nn.Module, decay: float):
    for tp, op in zip(target.parameters(), online.parameters()):
        tp.data.mul_(decay).add_(op.data, alpha=1.0 - decay)


def make_model(cfg: dict):
    m = cfg["model"]
    ctx = SeqEncoder(d=m["d_model"], layers=m["n_layers"], heads=m["n_heads"],
                     ff=m["ffn_dim"], dropout=m["dropout"], max_len=128)
    tgt = SeqEncoder(d=m["d_model"], layers=m["n_layers"], heads=m["n_heads"],
                     ff=m["ffn_dim"], dropout=m["dropout"], max_len=128)
    tgt.load_state_dict(ctx.state_dict())
    proj = PriorProjector(16, m["d_model"])
    anchor_ema = PriorProjector(16, m["d_model"])
    anchor_ema.load_state_dict(proj.state_dict())
    rev = RevisionPredictor(m["d_model"], m["d_model"] * 2, m["dropout"])
    return ctx, tgt, proj, anchor_ema, rev


def train(args) -> dict:
    cfg = load_config()
    years = tuple(range(args.start_year, args.end_year + 1))
    arrays = load_arrays(years)
    # 训练集逐特征标准化（论文口径）；PE/ROE/市值量纲差异大，不标准化会主导梯度
    all_x = np.concatenate([item["X"] for item in arrays.values()], axis=0)
    feat_mu = np.nanmean(all_x, axis=0).astype(np.float32)
    feat_sd = np.nanstd(all_x, axis=0).astype(np.float32)
    feat_sd = np.where(feat_sd > 1e-8, feat_sd, 1.0).astype(np.float32)
    print(f"[standardize] rows={len(all_x):,} mean/std ready", flush=True)
    samples = pd.read_parquet(INDEX)
    samples["trade_date"] = pd.to_datetime(samples["trade_date"])
    samples = samples[(samples["trade_date"] >= f"{args.start_year}-01-01")
                      & (samples["trade_date"] <= f"{args.end_year}-12-31")]
    if args.limit:
        samples = samples.sample(min(args.limit, len(samples)), random_state=7)
    ds = WindowDataset(samples, arrays, feat_mu=feat_mu, feat_sd=feat_sd)
    print(f"[dataset] samples={len(ds):,} codes={len(arrays)}", flush=True)
    if len(ds) < 32:
        raise SystemExit("样本不足")
    loader = DataLoader(ds, batch_size=args.batch, shuffle=True, drop_last=True, num_workers=0)

    ctx, tgt, proj, anchor_ema, rev = make_model(cfg)
    device = torch.device("mps" if torch.backends.mps.is_available() and not args.cpu else "cpu")
    for module in (ctx, tgt, proj, anchor_ema, rev):
        module.to(device)
    for p in tgt.parameters():
        p.requires_grad_(False)
    for p in anchor_ema.parameters():
        p.requires_grad_(False)
    opt_anchor = torch.optim.AdamW(proj.parameters(), lr=cfg["model"]["lr"], weight_decay=cfg["model"]["weight_decay"])
    opt_rev = torch.optim.AdamW(
        list(ctx.parameters()) + list(rev.parameters()),
        lr=cfg["model"]["lr"], weight_decay=cfg["model"]["weight_decay"])

    step = 0
    t0 = time.time()
    history = []
    for epoch in range(args.epochs):
        for ctx_x, tgt_x, prior in loader:
            ctx_x, tgt_x, prior = ctx_x.to(device), tgt_x.to(device), prior.to(device)
            # 特征/先验里的缺失在模型入口统一置 0（论文：标准化后非有限值置 0）
            ctx_x = torch.nan_to_num(ctx_x, nan=0.0, posinf=0.0, neginf=0.0)
            tgt_x = torch.nan_to_num(tgt_x, nan=0.0, posinf=0.0, neginf=0.0)
            prior = torch.nan_to_num(prior, nan=0.0, posinf=0.0, neginf=0.0)
            with torch.no_grad():
                z_tgt = tgt(tgt_x)
            z_anchor_online = proj(prior)
            loss_anchor = F.mse_loss(z_anchor_online, z_tgt)
            with torch.no_grad():
                z_anchor = anchor_ema(prior)
            mask = random_context_mask(ctx_x.shape[0], ctx_x.shape[1], device)
            h = ctx(ctx_x, mask)
            pred_rev = rev(h, z_anchor)
            disp = (z_tgt - z_anchor).detach()
            disp = (disp - disp.mean(dim=0, keepdim=True)) / (disp.std(dim=0, keepdim=True) + 1e-4)
            disp = disp.clamp(-8, 8)
            loss_rev = F.mse_loss(pred_rev, disp)

            opt_anchor.zero_grad(set_to_none=True)
            loss_anchor.backward()
            opt_anchor.step()
            opt_rev.zero_grad(set_to_none=True)
            loss_rev.backward()
            torch.nn.utils.clip_grad_norm_(list(ctx.parameters()) + list(rev.parameters()), 1.0)
            opt_rev.step()
            ema_update(tgt, ctx, cfg["model"]["target_ema"])
            ema_update(anchor_ema, proj, cfg["model"]["anchor_ema"])

            step += 1
            if step % 5 == 0 or step == 1:
                history.append({"step": step, "anchor": float(loss_anchor), "rev": float(loss_rev)})
                print(f"[{step}] anchor={float(loss_anchor):.4f} rev={float(loss_rev):.4f} "
                      f"{(time.time() - t0) / step:.3f}s/step", flush=True)
            if args.steps and step >= args.steps:
                break
        if args.steps and step >= args.steps:
            break
    out = {"steps": step, "device": str(device), "samples": len(ds), "history": history}
    ckpt = STORE / "checkpoints" / ("smoke.pt" if args.smoke else "jepa.pt")
    ckpt.parent.mkdir(parents=True, exist_ok=True)
    torch.save({
        "ctx": ctx.state_dict(), "tgt": tgt.state_dict(), "proj": proj.state_dict(),
        "anchor_ema": anchor_ema.state_dict(), "rev": rev.state_dict(), "config": cfg,
        "feat_mu": feat_mu, "feat_sd": feat_sd,
    }, ckpt)
    (STORE / "train_smoke.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[done] {ckpt} steps={step}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="P2.74 STOCK-JEPA S2 训练")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--start-year", type=int, default=2020)
    ap.add_argument("--end-year", type=int, default=2022)
    ap.add_argument("--limit", type=int, default=4000)
    ap.add_argument("--epochs", type=int, default=1)
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--cpu", action="store_true")
    args = ap.parse_args()
    train(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
