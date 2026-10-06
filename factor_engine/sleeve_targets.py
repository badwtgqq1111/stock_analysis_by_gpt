#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P1.24 生产层：把训练好的家族腿变成**可交易目标权重**，再由分配器给预算、合并成组合。

流程（对应 P1.24b §2/§5/§6）：
  1. 读长特征面板的最新截面 + 日线（可交易性过滤）；
  2. 每个 sleeve 用落盘模型打分 -> 取 output_breadth 只 -> 腿内等权或 rank 加权（§8.4：宽度 100）；
  3. 乘以该 sleeve 的预算（sleeve_budgets.json）-> 得该腿的贡献权重；
  4. 合并各腿 -> 单票 <= max_name_weight -> 归一化 -> 落盘 output/results_cn/sleeve_targets/。

设计边界（重要）：
  * 本模块只产出目标权重文件，不直接下单，也不改现有选股链路；
  * [selection.sleeve_allocator].enabled 打开后，才由组合层读取该文件；
  * 未通过资格门的 sleeve（预算为 0）不产出权重。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
MIN_AMOUNT = 2e7            # 成交额下限（元）


def _feature_table() -> Path:
    return ROOT / "assets/data/meta/cn_sleeve_features_long.parquet"



def stacked_score_map(per_sleeve: list[pd.DataFrame]) -> pd.Series:
    """把各腿分数按标的聚合（取最大分），供 pk 兼容导出的 model_score 列使用。"""
    frames = [f for f in per_sleeve if not f.empty]
    if not frames:
        return pd.Series(dtype=float)
    stacked = pd.concat(frames, ignore_index=True)
    return stacked.groupby("stock_code")["score"].max()


def load_latest_panel(as_of: str | None = None) -> tuple[pd.DataFrame, str]:
    """返回最新截面（特征 + 可交易性判断所需的 amount/high/low/close），以及该截面日期。"""
    import pyarrow.dataset as ds

    table = _feature_table()
    if not table.exists():
        raise FileNotFoundError(f"缺少长特征面板 {table}；先跑 --stage sleeve_features")
    frame = pd.read_parquet(table)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    if as_of:
        frame = frame[frame["trade_date"] <= pd.Timestamp(as_of)]
    latest = frame["trade_date"].max()
    snapshot = frame[frame["trade_date"] == latest].copy()

    bars_files = sorted(str(p) for p in (ROOT / "assets/data/clean/ohlcv/market=CN").glob(
        "exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year=202[5-6]/*.parquet"))
    bars = ds.dataset(bars_files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "close", "high", "low", "amount"]).to_pandas()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    bars = bars[bars["trade_date"] == latest].drop_duplicates("stock_code")
    snapshot = snapshot.merge(bars, on=["trade_date", "stock_code"], how="left", suffixes=("", "_bar"))
    # 窄组合的分散主要来自行业，必须有行业列；长面板本身不含行业 → 从 registry 关联
    snapshot = snapshot.merge(load_industry_map(), on="stock_code", how="left")
    snapshot["industry_l1"] = snapshot["industry_l1"].fillna("__UNKNOWN__")
    return snapshot, str(latest.date())


def load_industry_map() -> pd.DataFrame:
    import pyarrow.dataset as ds

    files = sorted(str(p) for p in (ROOT / "assets/data/meta/stock_info_registry").glob("**/*.parquet"))
    if not files:
        return pd.DataFrame(columns=["stock_code", "industry_l1"])
    cols = ["stock_code"]
    schema = ds.dataset(files, format="parquet").schema.names
    cols += [c for c in ("industry_l1", "industry_l2", "industry_l3") if c in schema]
    frame = ds.dataset(files, format="parquet").to_table(columns=cols).to_pandas()
    frame["stock_code"] = frame["stock_code"].astype(str)
    # 注意：registry 的 industry_l1 存的是**分类标准名**（如"证监会行业分类"），
    # 真正的行业在 industry_l2/l3；这里统一映射成 industry_l2 供分散约束使用。
    if "industry_l2" in frame.columns:
        frame["industry_l1"] = frame["industry_l2"]
    return frame.dropna(subset=["industry_l1"]).drop_duplicates("stock_code")


def tradable_mask(frame: pd.DataFrame) -> pd.Series:
    amount = pd.to_numeric(frame.get("amount"), errors="coerce")
    close = pd.to_numeric(frame.get("close"), errors="coerce")
    high = pd.to_numeric(frame.get("high"), errors="coerce")
    low = pd.to_numeric(frame.get("low"), errors="coerce")
    has_bar = close.notna() & (close > 0)
    liquid = amount >= MIN_AMOUNT
    one_price = high.eq(low)                      # 一字板：当日不可成交
    return has_bar & liquid & (~one_price.fillna(False))


def load_budgets(budget_file: str | Path | None = None) -> tuple[dict, str]:
    path = Path(budget_file) if budget_file else ROOT / "output/research/sleeve_budgets.json"
    if not path.exists():
        return {}, f"missing:{path}"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {k: float(v) for k, v in (payload.get("weights") or {}).items()}, str(path)


def score_sleeve(frame: pd.DataFrame, sleeve_id: str) -> pd.Series:
    """用落盘模型给截面打分；模型缺失时返回 NaN（该腿不产出权重）。"""
    import lightgbm as lgb

    short = sleeve_id.replace("model_", "")
    model_path = ROOT / f"output/model_scores/sleeve_{short}.txt"
    meta_path = ROOT / f"output/model_scores/sleeve_{short}.meta.json"
    if not model_path.exists() or not meta_path.exists():
        return pd.Series(np.nan, index=frame.index)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    cols = [c for c in meta["feature_cols"] if c in frame.columns]
    if not cols:
        return pd.Series(np.nan, index=frame.index)
    booster = lgb.Booster(model_file=str(model_path))
    return pd.Series(booster.predict(frame[cols].to_numpy(dtype=float)), index=frame.index)


def consensus_scores(snapshot: pd.DataFrame, sleeve_ids: list[str],
                     budgets: dict) -> tuple[pd.DataFrame, dict]:
    """多腿**共识分**：每条腿先做横截面分位，再按预算加权平均。

    为什么不用"每条腿各挑 top-k 再合并"：账户只能持 5 只时，单腿 top-5 完全被特质噪声吞没
    （实测 valuation 单腿 top-5 超额为负）；而**多腿共识排名的 top-5** 在长样本上净超额 +0.81%、t 1.90。
    共识分等价于把各腿的排序信息在**同一个横截面上**叠加，是窄宽度下唯一可行的用法。
    """
    parts, used = [], {}
    for sleeve in sleeve_ids:
        budget = float(budgets.get(sleeve, 0.0))
        if budget <= 0:
            continue
        scores = score_sleeve(snapshot, sleeve)
        if scores.notna().sum() == 0:
            continue
        rank_pct = scores.rank(pct=True)
        parts.append((rank_pct * budget).rename(sleeve))
        used[sleeve] = budget
    if not parts:
        return pd.DataFrame(columns=["stock_code", "consensus"]), {}
    frame = pd.concat(parts, axis=1)
    weight_sum = sum(used.values())
    consensus = frame.sum(axis=1, skipna=True) / weight_sum if weight_sum > 0 else frame.mean(axis=1)
    out = snapshot[["stock_code"]].copy()
    out["consensus"] = consensus.to_numpy()
    out["n_sleeves"] = frame.notna().sum(axis=1).to_numpy()
    for col in frame.columns:
        out[f"rank_{col}"] = frame[col].to_numpy()
    return out, used


def select_final(consensus: pd.DataFrame, snapshot: pd.DataFrame, holdings: int,
                 max_per_industry: int, industry_col: str | None,
                 equity: float = 0.0, lot_size: int = 100,
                 cash_buffer: float = 0.05) -> pd.DataFrame:
    """从共识排名里挑最终 N 只：按共识分降序贪心；同一行业至多 max_per_industry 只；
    并在给定账户规模下**只挑买得起整手的票**（否则会被 pk 的可负担性约束清零）。

    equity > 0 时按"每只 slice = equity×(1-cash_buffer)/holdings"判断一手是否买得起，
    并直接算出整手数 lots（A 股 100 股一手）。
    """
    tradable = tradable_mask(snapshot).to_numpy()
    frame = consensus.loc[tradable].dropna(subset=["consensus"]).sort_values("consensus", ascending=False)
    price = snapshot.set_index("stock_code")["close"]
    frame = frame.assign(close=frame["stock_code"].map(price))
    if industry_col and industry_col in snapshot.columns:
        industry = snapshot.set_index("stock_code")[industry_col]
        frame = frame.assign(industry=frame["stock_code"].map(industry).fillna("__UNKNOWN__"))
    else:
        frame = frame.assign(industry="__UNKNOWN__")
    slice_budget = equity * (1 - cash_buffer) / max(holdings, 1) if equity > 0 else 0.0
    picked, counts = [], {}
    for row in frame.itertuples():
        if equity > 0:
            one_lot = float(row.close) * lot_size
            if not np.isfinite(one_lot) or one_lot > slice_budget * 0.95:   # 买不起整手就跳过
                continue
        used = counts.get(row.industry, 0)
        if used >= max_per_industry:
            continue
        counts[row.industry] = used + 1
        picked.append(row)
        if len(picked) >= holdings:
            break
    if not picked:
        return pd.DataFrame(columns=list(frame.columns) + ["target_weight", "lots", "one_lot_value"])
    out = pd.DataFrame([{k: v for k, v in r._asdict().items() if k != "Index"} for r in picked])
    out["one_lot_value"] = out["close"] * lot_size
    if equity > 0:
        out["lots"] = np.maximum((slice_budget / out["one_lot_value"]).astype(int), 1)
        out["target_weight"] = out["lots"] * out["one_lot_value"] / equity
        out["target_weight"] = out["target_weight"] / out["target_weight"].sum()
    else:
        out["lots"] = np.nan
        out["target_weight"] = 1.0 / len(out)
    return out


def targets_for_sleeve(snapshot: pd.DataFrame, sleeve_id: str, budget: float,
                       breadth: int, weighting: str = "equal") -> pd.DataFrame:
    """（保留）单腿目标权重：域内取 breadth 只 -> 腿内权重 -> 乘预算。窄账户请改用 consensus_scores。"""
    empty = pd.DataFrame(columns=["stock_code", "sleeve", "score", "weight_in_sleeve", "target_weight"])
    if budget <= 0:
        return empty
    scores = score_sleeve(snapshot, sleeve_id)
    valid = scores.notna() & tradable_mask(snapshot)
    picked = snapshot.loc[valid].assign(score=scores[valid]).nlargest(breadth, "score")
    if picked.empty:
        return empty
    if weighting == "rank":
        ranks = picked["score"].rank(pct=True)
        inner = (ranks / ranks.sum()).to_numpy()
    else:
        inner = np.full(len(picked), 1.0 / len(picked))
    out = pd.DataFrame({
        "stock_code": picked["stock_code"].to_numpy(),
        "sleeve": sleeve_id,
        "score": picked["score"].to_numpy(),
        "weight_in_sleeve": inner,
    })
    out["target_weight"] = out["weight_in_sleeve"] * budget
    return out


def combine_targets(per_sleeve: list[pd.DataFrame], max_name_weight: float = 0.05) -> pd.DataFrame:
    frames = [f for f in per_sleeve if not f.empty]
    if not frames:
        return pd.DataFrame(columns=["stock_code", "target_weight", "sleeves", "n_sleeves"])
    stacked = pd.concat(frames, ignore_index=True)
    merged = stacked.groupby("stock_code", as_index=False).agg(
        target_weight=("target_weight", "sum"),
        sleeves=("sleeve", lambda s: ",".join(sorted(set(s)))),
        n_sleeves=("sleeve", lambda s: int(len(set(s)))),
    )
    for _ in range(200):
        over = merged["target_weight"] > max_name_weight + 1e-12
        if not over.any():
            break
        excess = float((merged.loc[over, "target_weight"] - max_name_weight).sum())
        merged.loc[over, "target_weight"] = max_name_weight
        free = ~over
        if merged.loc[free, "target_weight"].sum() <= 0:
            break
        merged.loc[free, "target_weight"] += excess * (
            merged.loc[free, "target_weight"] / merged.loc[free, "target_weight"].sum())
    merged["target_weight"] = merged["target_weight"] / merged["target_weight"].sum()
    return merged.sort_values("target_weight", ascending=False).reset_index(drop=True)


def run_sleeve_targets_stage(layer: dict, pipeline: dict, base_dir: Path) -> dict:
    """流水线阶段入口：产出可交易目标权重文件（不接触现有选股链路）。"""
    as_of = layer.get("as_of") or None
    # 账户现实约束：候选 10 只、最终买入 5 只（宽度 100 是**统计口径**，不是持仓口径）
    candidate_pool = int(layer.get("candidate_pool", 10))
    holdings = int(layer.get("holdings", 5))
    max_per_industry = int(layer.get("max_per_industry", 1))
    industry_col = str(layer.get("industry_col", "industry_l2")) or None
    equity = float(layer.get("equity", 0.0) or 0.0)          # 账户规模（0 = 不按整手约束）
    lot_size = int(layer.get("lot_size", 100) or 100)
    cash_buffer = float(layer.get("cash_buffer", 0.05) or 0.05)
    budgets, budget_source = load_budgets(layer.get("budget_file"))
    snapshot, snap_date = load_latest_panel(as_of)
    sleeve_ids = list(layer.get("sleeves") or
                      ["model_valuation", "model_quality", "model_momentum", "model_reversal"])
    consensus, used_budgets = consensus_scores(snapshot, sleeve_ids, budgets)
    summary = [{"sleeve": sleeve, "budget": float(budgets.get(sleeve, 0.0)),
                "used_in_consensus": sleeve in used_budgets} for sleeve in sleeve_ids]
    # 候选 10 只（供审阅）
    tradable = tradable_mask(snapshot)
    candidates = (consensus.loc[tradable].dropna(subset=["consensus"])
                  .sort_values("consensus", ascending=False).head(candidate_pool).copy())
    # 最终 5 只（行业分散 + 等权 20%）
    final = select_final(consensus, snapshot, holdings, max_per_industry, industry_col,
                         equity=equity, lot_size=lot_size, cash_buffer=cash_buffer)
    combined = final.copy()
    out_dir = Path(base_dir).resolve().parent.parent / str(layer.get("output_dir", "output/results_cn/sleeve_targets"))
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = snap_date.replace("-", "")
    path = out_dir / f"sleeve_targets_{stamp}.csv"
    combined.to_csv(path, index=False)
    candidates.to_csv(out_dir / f"sleeve_candidates_{stamp}.csv", index=False)
    # pk 兼容导出：让现有 pk 阶段可以直接消费（[selection].preselection_path 指向本文件即可）
    pk_ready = combined.copy()
    pk_ready.insert(0, "trade_date", pd.Timestamp(snap_date))
    score_map = candidates.set_index("stock_code")["consensus"]
    pk_ready["model_score"] = pk_ready["stock_code"].map(score_map).fillna(0.0)
    pk_ready["ensemble_score"] = pk_ready["model_score"]
    pk_ready["rank"] = range(1, len(pk_ready) + 1)
    pk_ready["trade_weight"] = pk_ready["target_weight"]
    pk_ready["portfolio_mode"] = "sleeve_allocator"
    pk_ready["constraint_status"] = "sleeve_combined"
    pk_ready["selection_channel"] = "sleeve"
    rank_cols = [c for c in pk_ready.columns if c.startswith("rank_")]
    if rank_cols:
        # 记录"最支持它的那条腿"（该腿横截面分位最高），供归因使用
        pk_ready["selection_sleeve"] = pk_ready[rank_cols].idxmax(axis=1).str.replace("rank_", "", regex=False)
    else:
        pk_ready["selection_sleeve"] = "sleeve_consensus"
    pk_ready = pk_ready[["trade_date", "stock_code", "model_score", "ensemble_score", "rank",
                         "target_weight", "trade_weight", "portfolio_mode", "constraint_status",
                         "selection_channel", "selection_sleeve"]]
    pk_path = Path(base_dir).resolve().parent.parent / "output/results_cn/sleeve_preselected.csv"
    pk_ready.to_csv(pk_path, index=False)
    (out_dir / f"sleeve_targets_{stamp}.meta.json").write_text(
        json.dumps({
            "as_of": snap_date, "candidate_pool": candidate_pool, "holdings": holdings,
            "max_per_industry": max_per_industry, "budget_source": budget_source,
            "budgets": budgets, "sleeves": summary, "n_names": int(len(combined)),
            "top_holdings": combined.head(10).to_dict("records"),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[sleeve_targets] 截面 {snap_date} | 全市场 {len(snapshot)} 只 | 候选 {len(candidates)} 只 | "
          f"最终持仓 {len(combined)} 只 | 预算源 {budget_source}", flush=True)
    for row in summary:
        flag = "参与共识" if row["used_in_consensus"] else "预算为 0，排除"
        print(f"[sleeve_targets]   {row['sleeve']:18s} 预算 {row['budget']:.3f} -> {flag}", flush=True)
    if len(candidates):
        print(f"[sleeve_targets] 候选（共识分降序）："
              f"{', '.join(candidates['stock_code'].head(candidate_pool))}", flush=True)
    if len(combined):
        detail = " | ".join(f"{r.stock_code}({r.lots:.0f}手,{r.target_weight:.1%})"
                            for r in combined.itertuples() if hasattr(r, "lots"))
        print(f"[sleeve_targets] 最终 {len(combined)} 只："
              f"{detail or ', '.join(combined['stock_code'])}", flush=True)
    return {
        "status": "completed" if len(combined) else "failed",
        "as_of": snap_date, "n_names": int(len(combined)), "candidate_pool": candidate_pool,
        "holdings": holdings, "max_per_industry": max_per_industry, "budget_source": budget_source, "sleeves": summary,
        "out_file": str(path), "pk_compatible_file": str(pk_path),
        "note": "只产出目标权重文件；是否进入选股链路取决于 [selection.sleeve_allocator].enabled",
    }
