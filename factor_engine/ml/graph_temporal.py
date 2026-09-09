"""Small, auditable graph-temporal baseline for PIT industry relations.

This is intentionally a baseline rather than a claim of reproducing GrifFinNet:
industry edges are generated from the as-of stock registry and a gated graph
message is interleaved with temporal attention.  The model can be replaced by a
research implementation while preserving the artifact contract.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


def build_industry_adjacency(stock_codes, stock_info: pd.DataFrame | None = None, *, asof_date=None) -> tuple[np.ndarray, dict]:
    codes = list(dict.fromkeys(map(str, stock_codes)))
    index = {code: i for i, code in enumerate(codes)}
    adjacency = np.eye(len(codes), dtype=np.float32)
    groups: dict[str, list[int]] = {}
    if stock_info is not None and not stock_info.empty:
        info = stock_info.copy()
        info["stock_code"] = info["stock_code"].astype(str)
        if asof_date is not None and "available_at" in info.columns:
            info["available_at"] = pd.to_datetime(info["available_at"], errors="coerce")
            info = info[info["available_at"] <= pd.Timestamp(asof_date)].sort_values("available_at").drop_duplicates("stock_code", keep="last")
        industry = info.get("industry_l1", pd.Series(index=info.index, dtype=object)).fillna("").astype(str)
        for code, value in zip(info["stock_code"], industry):
            if code in index and value.strip() and value.strip().lower() not in {"nan", "none"}:
                groups.setdefault(value.strip(), []).append(index[code])
    for members in groups.values():
        adjacency[np.ix_(members, members)] = 1.0
    degree = adjacency.sum(axis=1, keepdims=True)
    adjacency = adjacency / np.maximum(degree, 1.0)
    return adjacency, {"node_count": len(codes), "industry_group_count": len(groups), "graph_version": "industry-pit.v1", "asof_date": str(asof_date) if asof_date is not None else None}


def _model(input_dim):
    import torch
    from torch import nn

    class GatedGraphTemporalNet(nn.Module):
        def __init__(self, dim):
            super().__init__()
            self.proj = nn.Linear(dim, 32)
            self.gate = nn.Linear(2 * dim, dim)
            self.head = nn.Linear(32, 1)

        def forward(self, seq, adj):
            neigh = torch.einsum("ij,tjd->tid", adj, seq)
            gate = torch.sigmoid(self.gate(torch.cat([seq, neigh], dim=-1)))
            fused = gate * seq + (1 - gate) * neigh
            return self.head(torch.tanh(self.proj(fused))).squeeze(-1)

    return GatedGraphTemporalNet(input_dim)


def train_graph_temporal_panel(panel: pd.DataFrame, feature_columns: list[str], *, model_dir, label_column="forward_return_20d", lookback=20, epochs=5, device="auto", adjacency=None, graph_metadata=None):
    """Train a gated graph-temporal regressor and persist checkpoint + manifest."""
    import torch

    frame = panel.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame = frame.dropna(subset=["trade_date", "stock_code", label_column]).sort_values(["trade_date", "stock_code"])
    features = [c for c in feature_columns if c in frame.columns]
    if not features or frame.empty:
        raise ValueError("graph-temporal panel has no labeled features")
    codes = sorted(frame["stock_code"].astype(str).unique())
    if adjacency is None:
        adjacency = np.eye(len(codes), dtype=np.float32)
    adjacency = np.asarray(adjacency, dtype=np.float32)
    if adjacency.shape != (len(codes), len(codes)):
        raise ValueError("adjacency shape must match stock universe")
    dates = sorted(frame["trade_date"].unique())
    pivot = frame.pivot_table(index="trade_date", columns="stock_code", values=features, aggfunc="last")
    values = np.zeros((len(dates), len(codes), len(features)), dtype=np.float32)
    labels = np.zeros((len(dates), len(codes)), dtype=np.float32)
    for t, date in enumerate(dates):
        for j, code in enumerate(codes):
            row = frame[(frame.trade_date == date) & (frame.stock_code.astype(str) == code)]
            if not row.empty:
                values[t, j] = pd.to_numeric(row.iloc[-1][features], errors="coerce").fillna(0).to_numpy(dtype=np.float32)
                labels[t, j] = float(row.iloc[-1][label_column])
    if len(dates) <= int(lookback):
        raise ValueError("not enough dates for graph-temporal lookback")
    x = torch.tensor(values[-min(len(dates), 512):], dtype=torch.float32)
    y = torch.tensor(labels[-x.shape[0]:], dtype=torch.float32)

    model = _model(len(features)); optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3); adj = torch.tensor(adjacency)
    target = y
    for _ in range(max(1, int(epochs))):
        optimizer.zero_grad(); prediction = model(x, adj); loss = torch.nn.functional.smooth_l1_loss(prediction, target); loss.backward(); optimizer.step()
    directory = Path(model_dir); directory.mkdir(parents=True, exist_ok=True)
    model_path = directory / "model.pt"; manifest_path = directory / "model_manifest.json"
    torch.save({"state_dict": model.state_dict(), "input_dim": len(features), "node_codes": codes, "adjacency": adjacency.tolist()}, model_path)
    manifest = {"model_type": "gated_graph_temporal", "feature_columns": features, "lookback": int(lookback), "graph": graph_metadata or {}, "device": str(device), "train_dates": [str(d) for d in dates], "label_column": label_column}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"model_path": str(model_path), "manifest_path": str(manifest_path), "model_type": manifest["model_type"], "train_rows": int(len(frame)), "node_count": len(codes), "loss": float(loss.detach())}


def predict_graph_temporal_panel(panel: pd.DataFrame, *, model_path, manifest_path, asof_date=None) -> pd.DataFrame:
    """Score one cross section using a saved graph snapshot and model artifact."""
    import torch

    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    checkpoint = torch.load(model_path, map_location="cpu", weights_only=True)
    features = list(manifest["feature_columns"])
    codes = list(checkpoint["node_codes"])
    date = pd.Timestamp(asof_date) if asof_date is not None else pd.to_datetime(panel["trade_date"]).max()
    current = panel[pd.to_datetime(panel["trade_date"]) == date].copy()
    lookup = current.set_index(current["stock_code"].astype(str))
    values = np.zeros((1, len(codes), len(features)), dtype=np.float32)
    for index, code in enumerate(codes):
        if code in lookup.index:
            values[0, index] = pd.to_numeric(lookup.loc[code, features], errors="coerce").fillna(0.0).to_numpy(dtype=np.float32)
    model = _model(len(features)); model.load_state_dict(checkpoint["state_dict"]); model.eval()
    with torch.no_grad():
        raw = model(torch.tensor(values), torch.tensor(checkpoint["adjacency"], dtype=torch.float32))[0].numpy()
    output = pd.DataFrame({"trade_date": date, "stock_code": codes, "model_score_raw": raw})
    output["model_score"] = output["model_score_raw"].rank(pct=True) * 100.0
    return output
