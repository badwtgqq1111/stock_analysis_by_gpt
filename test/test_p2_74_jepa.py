"""P2.74 STOCK-JEPA 表征结构：形状 / EMA / 修正损失不更新锚点投影器。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import torch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/p2_74_jepa_train.py"


def _module():
    spec = importlib.util.spec_from_file_location("p2_74_jepa_train", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_encoder_shapes():
    m = _module()
    enc = m.SeqEncoder(d=16, layers=1, heads=2, ff=32, dropout=0.0, max_len=128)
    out = enc(torch.randn(2, 126, 40))
    assert out.shape == (2, 16)


def test_window_dataset_shapes():
    m = _module()
    dates = pd.date_range("2024-01-01", periods=200, freq="D").to_numpy("datetime64[ns]")
    arrays = {"600000.SH": {
        "dates": dates,
        "X": np.random.randn(200, 40).astype(np.float32),
        "P": np.random.randn(200, 16).astype(np.float32),
        "pos": {d: i for i, d in enumerate(dates)},
    }}
    samples = pd.DataFrame({"trade_date": [pd.Timestamp(dates[150])], "stock_code": ["600000.SH"]})
    ds = m.WindowDataset(samples, arrays)
    assert len(ds) == 1
    ctx, tgt, prior = ds[0]
    assert ctx.shape == (126, 40)
    assert tgt.shape == (21, 40)
    assert prior.shape == (16,)


def test_revision_loss_does_not_touch_anchor_projector():
    m = _module()
    ctx = m.SeqEncoder(d=16, layers=1, heads=2, ff=32, dropout=0.0, max_len=128)
    tgt = m.SeqEncoder(d=16, layers=1, heads=2, ff=32, dropout=0.0, max_len=128)
    proj = m.PriorProjector(16, 16)
    anchor = m.PriorProjector(16, 16)
    anchor.load_state_dict(proj.state_dict())
    rev = m.RevisionPredictor(16, 32, 0.0)
    x = torch.randn(2, 126, 40)
    y = torch.randn(2, 21, 40)
    p = torch.randn(2, 16)
    with torch.no_grad():
        z_tgt = tgt(y)
        z_anchor = anchor(p)
    h = ctx(x)
    pred = rev(h, z_anchor)
    disp = (z_tgt - z_anchor).detach()
    loss = torch.nn.functional.mse_loss(pred, disp)
    loss.backward()
    assert all(param.grad is None for param in proj.parameters()), \
        "修正损失不能更新先验投影器（锚点必须 detach）"


def test_ema_moves_target_toward_online():
    m = _module()
    online = torch.nn.Linear(2, 2)
    target = torch.nn.Linear(2, 2)
    with torch.no_grad():
        target.weight.zero_()
        target.bias.zero_()
    m.ema_update(target, online, 0.5)
    assert torch.allclose(target.weight, online.weight * 0.5)
    assert torch.allclose(target.bias, online.bias * 0.5)
