#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LLM 因子的 OOS / 多重检验校正 / 增量信息评估器（P1_25 工单 L1 + L2 落地）。

为什么需要：`scripts/llm_factor_mining.py` 报的是**全样本月频秩 IC + BH-FDR**。
在"让 LLM 一次提 N 个因子、再挑最好的几个"这种流程里，全样本 IC 高只说明"这 N 次里有一次撞上了"，
不能说明可交易。本脚本对同一批候选补齐四件事（全部复用 factor_ic_protocol / llm_factor_mining 的口径）：

  L1-a **purged K 折时间稳定性**：按时间把月切成 K 段（默认 5），逐折算 IC；
        折边界做 purge/embargo=1 个月（标签是 20 日前向收益，相邻月标签有重叠）；
        报"折间同号率"，以及**滚动前向选择**（用前 k-1 折挑最优 → 在第 k 折上测），
        直接量化"选择"这一步在样本外的衰减。
  L1-b **DSR（Deflated Sharpe Ratio, Bailey & López de Prado 2014）**：
        用因子多空组合的月度收益序列算 Sharpe，再按**试验次数 N**（候选因子总数）折算门槛 SR0，
        报 DSR = P(SR_hat > SR0)。
  L1-c **PBO（Probability of Backtest Overfitting, CSCV）**：
        把月度收益矩阵切成 S=10 块，枚举 C(10,5) 组"半样本内 / 半样本外"，
        看样本内最优策略在样本外的排名 → PBO。
  L2   **增量信息**：(1) 把因子对现有 25 列横截面正交化后的**残差 IC**；
        (2) Fama-MacBeth：在"现有列 IC 加权合成"之外，因子系数的均值与 NW t。

用法：
  uv run python scripts/factor_oos_deflated.py --run output/research/llm_factor_mining_20261007_151739
  uv run python scripts/factor_oos_deflated.py --run <run_dir> --folds 5 --pbo-blocks 10 --trials 12
输出（默认 <run_dir>/oos_deflated/）：
  oos_folds.csv        因子 × 折 的 IC
  deflated_summary.csv 每个因子的全部指标 + 准入判定
  pbo_cscv.json        PBO 与 logit 分布
  REPORT.md            人读摘要（含保留意见）
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import llm_factor_mining as lfm          # noqa: E402  复用 load_panel / safe_eval / eval_factor

EULER = 0.5772156649015329

# 增量检验的"控制集"默认 = v1 面板的 25 列（冻结），这样面板升到 v2 之后
# 残差 IC / FM t 仍然回答同一个问题："相对**现有模型特征**有没有增量"。
CONTROL_COLS_V1 = [
    "ret_5", "ret_20", "ret_120", "ret_250", "mom_12m_skip_1m", "rps_20", "rps_60", "pos_52w",
    "ma20_dev", "ma60_dev", "rev_1m", "rev_5d", "vol_20", "mdd_60",
    "pe_ind_pct", "pb_ind_pct", "pe_pb_combo", "roe_ind_pct", "roa_ind_pct",
    "grossprofit_margin_ind_pct", "netprofit_margin_ind_pct", "debt_to_assets_ind_pct",
    "netprofit_yoy", "or_yoy", "ocfps",
]


# ----------------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------------
def month_end_snapshot(merged: pd.DataFrame) -> pd.DataFrame:
    """每月最后一个交易日一条记录（月度调仓、标签不重叠）。"""
    snap = merged.copy()
    snap["month"] = snap["trade_date"].dt.to_period("M").astype(str)
    last = snap.groupby("month")["trade_date"].transform("max")
    return snap[snap["trade_date"] == last].copy()


def perf(returns: pd.Series) -> dict:
    r = returns.dropna().astype(float)
    n = len(r)
    if n < 6:
        return {"n": n, "mean": np.nan, "std": np.nan, "sharpe": np.nan,
                "sharpe_ann": np.nan, "t": np.nan, "skew": np.nan, "kurt": np.nan}
    mean, std = float(r.mean()), float(r.std(ddof=1))
    sr = mean / std if std > 0 else np.nan
    return {"n": n, "mean": mean, "std": std, "sharpe": sr, "sharpe_ann": sr * np.sqrt(12),
            "t": mean / (std / np.sqrt(n)) if std > 0 else np.nan,
            "skew": float(r.skew()), "kurt": float(r.kurtosis()) + 3.0}


def ls_returns(snap: pd.DataFrame, col: str, groups: int = 5) -> pd.Series:
    """多空组合月度收益：因子分位前 1/groups 减后 1/groups（等权）。"""
    out: dict[str, float] = {}
    for month, g in snap.groupby("month"):
        x = g[col].rank(pct=True)
        long_ = g.loc[x >= 1.0 - 1.0 / groups, "fwd20"].mean()
        short_ = g.loc[x <= 1.0 / groups, "fwd20"].mean()
        if np.isfinite(long_) and np.isfinite(short_):
            out[month] = float(long_ - short_)
    return pd.Series(out).sort_index()


def monthly_rank_ic(snap: pd.DataFrame, col: str) -> pd.Series:
    ics = {}
    for month, g in snap.groupby("month"):
        if len(g) < 50:
            continue
        ic = g[col].corr(g["fwd20"], method="spearman")
        if np.isfinite(ic):
            ics[month] = float(ic)
    return pd.Series(ics).sort_index()


def newey_west_t(series: pd.Series, lag: int = 1) -> float:
    v = series.dropna().to_numpy(dtype=float)
    n = len(v)
    if n < 4:
        return float("nan")
    mean = v.mean()
    d = v - mean
    var = float((d ** 2).mean())
    for k in range(1, min(lag, n - 1) + 1):
        var += 2.0 * (1.0 - k / (lag + 1.0)) * float((d[k:] * d[:-k]).mean())
    if var <= 1e-18:
        return float("nan")
    return float(mean / np.sqrt(var / n))


def deflated_sharpe(sr: float, n_obs: int, skew: float, kurt: float,
                    trials: int, var_sr: float) -> tuple[float, float]:
    """Bailey & López de Prado (2014) DSR。返回 (DSR, SR0 门槛)；trials<2 时门槛为 0。"""
    if not np.isfinite(sr) or n_obs < 6:
        return float("nan"), float("nan")
    if trials < 2 or not np.isfinite(var_sr) or var_sr <= 0:
        sr0 = 0.0
    else:
        z1 = stats.norm.ppf(1.0 - 1.0 / trials)
        z2 = stats.norm.ppf(1.0 - 1.0 / (trials * np.e))
        sr0 = float(np.sqrt(var_sr) * ((1.0 - EULER) * z1 + EULER * z2))
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if denom <= 0:
        return float("nan"), sr0
    z = (sr - sr0) * np.sqrt(max(n_obs - 1, 1)) / np.sqrt(denom)
    return float(stats.norm.cdf(z)), sr0


def ledger_trials(root: Path | None = None) -> dict:
    """累计"试验台账"：扫**所有** LLM 挖掘 run 的 proposals.json，按 (name, expression) 去重计数。

    为什么需要：DSR 的门槛 SR0 随试验次数 N 上升。只按单次 run 的候选数（8/12）算，
    等于假装"历史上只提过这十几个想法"，DSR 会系统性偏乐观。
    """
    root = root or (ROOT / "output" / "research")
    seen: set[tuple[str, str]] = set()
    runs = 0
    for run_dir in sorted(root.glob("llm_factor_mining_*")):
        proposals = list(run_dir.glob("round_*/proposals.json"))
        if not proposals:
            continue
        runs += 1
        for path in proposals:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for item in payload:
                seen.add((str(item.get("name", "")), str(item.get("expression", ""))))
    return {"runs": runs, "unique_proposals": len(seen)}


def pbo_cscv(returns: pd.DataFrame, blocks: int = 10) -> dict:
    """组合对称交叉验证（CSCV）算 PBO。returns: 月 × 策略。"""
    T = len(returns)
    S = blocks - (blocks % 2)
    if T < S * 2 or returns.shape[1] < 2:
        return {"pbo": float("nan"), "n_combos": 0, "note": "样本不足或候选少于 2 个"}
    splits = np.array_split(np.arange(T), S)
    combos = list(itertools.combinations(range(S), S // 2))
    logits = []
    for combo in combos:
        is_idx = np.concatenate([splits[i] for i in combo])
        oos_idx = np.concatenate([splits[i] for i in range(S) if i not in combo])
        is_r, oos_r = returns.iloc[is_idx], returns.iloc[oos_idx]
        is_sr = is_r.mean() / is_r.std(ddof=1)
        oos_sr = oos_r.mean() / oos_r.std(ddof=1)
        best = is_sr.idxmax()
        rank = int((oos_sr < oos_sr[best]).sum())          # 0 = 最差
        omega = (rank + 1.0) / (len(oos_sr) + 1.0)
        logits.append(float(np.log(omega / (1.0 - omega))))
    arr = np.array(logits)
    return {"pbo": float((arr < 0).mean()), "n_combos": len(logits), "blocks": S,
            "logit_mean": float(arr.mean()), "logit_median": float(np.median(arr)),
            "logit_std": float(arr.std(ddof=1)) if len(arr) > 1 else 0.0}


def zscore_cols(snap: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """横截面（按月）秩→标准化的 25 列，缺失填 0（中性）。"""
    def _std(s: pd.Series) -> pd.Series:
        sd = s.std()
        return (s - s.mean()) / sd if np.isfinite(sd) and sd > 1e-12 else s * 0.0

    out = {}
    for c in cols:
        z = snap.groupby("month")[c].rank(pct=True) - 0.5
        out[c] = z.groupby(snap["month"]).transform(_std)
    return pd.DataFrame(out, index=snap.index).fillna(0.0)


def cross_sectional_residual(snap: pd.DataFrame, zf: pd.DataFrame, col: str) -> pd.Series:
    """按月把因子横截面回归到现有列上，取残差（纯增量信息）。"""
    resid = pd.Series(index=snap.index, dtype=float)
    for month, g in snap.groupby("month"):
        idx = g.index
        X = np.column_stack([np.ones(len(idx)), zf.loc[idx].to_numpy()])
        y = g[col].to_numpy(dtype=float)
        ok = np.isfinite(y)
        if ok.sum() < 30:
            continue
        try:
            beta, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        except np.linalg.LinAlgError:
            continue
        resid.loc[idx[ok]] = y[ok] - X[ok] @ beta
    return resid


def fm_factor_coef(snap: pd.DataFrame, zf: pd.DataFrame, composite: pd.Series, col: str) -> pd.Series:
    """Fama-MacBeth：每月横截面 fwd ~ 1 + z_composite + z_factor，收集 factor 的系数。"""
    coefs = {}
    for month, g in snap.groupby("month"):
        idx = g.index
        X = np.column_stack([np.ones(len(idx)), composite.loc[idx].to_numpy(),
                             g[col].to_numpy(dtype=float)])
        y = g["fwd20"].to_numpy(dtype=float)
        ok = np.isfinite(X).all(axis=1) & np.isfinite(y)
        if ok.sum() < 50:
            continue
        try:
            beta, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        except np.linalg.LinAlgError:
            continue
        coefs[month] = float(beta[2])
    return pd.Series(coefs).sort_index()


# ----------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="LLM 因子 OOS/DSR/PBO/增量信息评估")
    ap.add_argument("--run", required=True, help="llm_factor_mining_* 输出目录")
    ap.add_argument("--panel", default="", help="特征面板路径；留空=与挖掘脚本一致（v1）")
    ap.add_argument("--control-cols", default="",
                    help="增量检验的控制列（逗号分隔）；留空=v1 的 25 列（冻结口径）")
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--groups", type=int, default=5, help="多空分位组数")
    ap.add_argument("--pbo-blocks", type=int, default=10)
    ap.add_argument("--trials", type=int, default=0, help="试验次数 N（默认=候选因子总数）")
    ap.add_argument("--trials-from-ledger", action="store_true",
                    help="用累计试验台账（扫全部 llm_factor_mining_* run 的去重提议数）作为 N")
    ap.add_argument("--t-min", type=float, default=3.0, help="Harvey-Liu-Zhu 门槛")
    ap.add_argument("--dsr-min", type=float, default=0.95)
    ap.add_argument("--resid-ic-min", type=float, default=0.02)
    ap.add_argument("--fm-t-min", type=float, default=2.0)
    ap.add_argument("--out-dir", default="")
    args = ap.parse_args()

    run_dir = Path(args.run)
    if not run_dir.exists():
        raise SystemExit(f"run 目录不存在：{run_dir}")
    out_dir = Path(args.out_dir) if args.out_dir else run_dir / "oos_deflated"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1) 候选因子（来自每轮 proposals.json）
    proposals: list[dict] = []
    for path in sorted(run_dir.glob("round_*/proposals.json")):
        proposals.extend(json.loads(path.read_text(encoding="utf-8")))
    seen, uniq = set(), []
    for p in proposals:
        key = str(p.get("name", "")) + "|" + str(p.get("expression", ""))
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    proposals = uniq
    ledger = ledger_trials()
    if args.trials_from_ledger:
        trials = args.trials or max(int(ledger["unique_proposals"]), 2)
    else:
        trials = args.trials or max(len(proposals), 2)
    print(f"[oos] 候选因子 {len(proposals)} 个；试验次数 N={trials}"
          f"（累计台账：{ledger['runs']} 个 run / {ledger['unique_proposals']} 条去重提议"
          f"{'，本轮按台账 N' if args.trials_from_ledger else '，本轮按 run 内 N'}）", flush=True)

    # 2) 面板 + 因子值
    merged, fields = lfm.load_panel(args.start, args.panel or None)
    print(f"[oos] 面板 {len(merged):,} 行 / {merged['stock_code'].nunique()} 只 / "
          f"{merged['trade_date'].nunique()} 个交易日 | 面板字段 {len(fields)} 个 "
          f"（{Path(args.panel) if args.panel else 'v1'}）", flush=True)
    if args.control_cols:
        controls = [c.strip() for c in args.control_cols.split(",") if c.strip()]
    else:
        controls = [c for c in CONTROL_COLS_V1 if c in set(fields)]
    print(f"[oos] 增量检验控制集 {len(controls)} 列（残差 IC / FM t 都相对这组列）", flush=True)

    snap = month_end_snapshot(merged)
    months = sorted(snap["month"].unique())
    print(f"[oos] 月末快照 {len(snap):,} 行 / {len(months)} 个月（{months[0]}~{months[-1]}）", flush=True)

    for p in proposals:
        vals, err = lfm.safe_eval(str(p.get("expression", "")), merged, fields)
        p["_values"] = vals
        p["_error"] = err
    valid = [p for p in proposals if p["_values"] is not None]
    print(f"[oos] 可求值 {len(valid)}/{len(proposals)}", flush=True)

    # 因子值列注入快照
    for p in valid:
        snap[p["name"]] = pd.Series(p["_values"]).reindex(merged.index).loc[snap.index].to_numpy()

    # 3) 月度 IC 序列 + 多空收益
    ic_series = {p["name"]: monthly_rank_ic(snap, p["name"]) for p in valid}
    ls_mat = pd.DataFrame({p["name"]: ls_returns(snap, p["name"], args.groups) for p in valid}).sort_index()

    # 4) purged K 折 + 滚动前向选择
    splits = [list(b) for b in np.array_split(np.array(months), args.folds)]
    fold_rows, oos_pick_rows = [], []
    for p in valid:
        s = ic_series[p["name"]]
        for k, block in enumerate(splits, start=1):
            vals = s.reindex([m for m in block if m in s.index]).dropna()
            fold_rows.append({"name": p["name"], "fold": k, "months": len(vals),
                              "ic_mean": float(vals.mean()) if len(vals) else np.nan,
                              "ic_std": float(vals.std()) if len(vals) else np.nan})
    folds_df = pd.DataFrame(fold_rows)
    for k in range(2, args.folds + 1):
        train_months = [m for b in splits[:k - 1] for m in b]
        test_months = splits[k - 1]
        train_months_purged = train_months[:-1] if len(train_months) > 1 else train_months  # purge 1 月
        best, best_icir = None, -np.inf
        for p in valid:
            s = ic_series[p["name"]].reindex(train_months_purged).dropna()
            if len(s) < 6 or s.std() == 0:
                continue
            icir = abs(s.mean() / s.std())
            if icir > best_icir:
                best, best_icir = p["name"], icir
        if best is None:
            continue
        test = ic_series[best].reindex(test_months).dropna()
        oos_pick_rows.append({"fold": k, "picked_on_train": best, "train_icir": float(best_icir),
                              "test_months": len(test), "test_ic_mean": float(test.mean()) if len(test) else np.nan})
    picks_df = pd.DataFrame(oos_pick_rows)

    # 5) DSR（N=试验次数）+ PBO（CSCV）
    var_sr = float(ls_mat.apply(lambda c: c.mean() / c.std(ddof=1)).var(ddof=1))
    pbo = pbo_cscv(ls_mat, args.pbo_blocks)

    # 6) 增量信息：现有列 z 分 + IC 加权合成（权重只用前 70% 月份，避免自证）
    zf = zscore_cols(snap, controls)
    ic_fields = pd.DataFrame({c: monthly_rank_ic(snap, c) for c in controls}).sort_index()
    cut = max(int(len(months) * 0.7), 12)
    train_months = months[:cut]
    w = ic_fields.reindex(train_months).mean().dropna()
    w = w / (w.abs().sum() or 1.0)
    composite = (zf[list(w.index)] * w).sum(axis=1)
    comp_ic = monthly_rank_ic(snap.assign(_comp=composite.to_numpy()), "_comp")

    rows = []
    for p in valid:
        name = p["name"]
        full = lfm.eval_factor(merged, p["_values"])                 # 与挖掘脚本同口径
        folds_p = folds_df[folds_df["name"] == name].sort_values("fold")
        ic_list = folds_p["ic_mean"].to_numpy()
        sign_same = float(np.mean(np.sign(ic_list) == np.sign(full["ic_mean"])))
        resid = cross_sectional_residual(snap, zf, name)
        resid_ic_series = monthly_rank_ic(snap.assign(_r=resid.to_numpy()), "_r")
        resid_ic_mean = float(resid_ic_series.mean())
        resid_ic_t = newey_west_t(resid_ic_series)
        fm = fm_factor_coef(snap, zf, composite, name)
        r = perf(ls_mat[name])
        dsr, sr0 = deflated_sharpe(r["sharpe"], r["n"], r["skew"], r["kurt"], trials, var_sr)
        dsr_ledger, sr0_ledger = deflated_sharpe(r["sharpe"], r["n"], r["skew"], r["kurt"],
                                                 max(int(ledger["unique_proposals"]), 2), var_sr)
        fm_t = newey_west_t(fm)
        fails = []
        if abs(full["nw_t"]) < args.t_min:
            fails.append(f"NW t<{args.t_min:g}")
        if sign_same < 0.8:
            fails.append("折间同号<80%")
        if dsr < args.dsr_min:
            fails.append(f"DSR<{args.dsr_min:g}")
        if resid_ic_mean < args.resid_ic_min:
            fails.append(f"残差IC<{args.resid_ic_min:g}")
        if abs(fm_t) < args.fm_t_min:
            fails.append(f"|FM t|<{args.fm_t_min:g}")
        rows.append({
            "name": name, "expression": p["expression"],
            "ic_full": full["ic_mean"], "icir_full": full["icir"], "nw_t_full": full["nw_t"],
            "pos_ratio": full["pos_ratio"],
            "fold_ic_mean": float(np.nanmean(ic_list)), "fold_ic_std": float(np.nanstd(ic_list)),
            "fold_sign_same": sign_same, "fold_ic_min": float(np.nanmin(ic_list)),
            "ls_sharpe_ann": r["sharpe_ann"], "ls_t": r["t"], "ls_n_months": r["n"],
            "sr0_threshold": sr0, "dsr": dsr,
            "dsr_cumulative_ledger": dsr_ledger, "sr0_cumulative_ledger": sr0_ledger,
            "resid_ic": resid_ic_mean, "resid_ic_t": resid_ic_t,
            "resid_ic_drop": float(full["ic_mean"] - resid_ic_mean),
            "fm_beta_mean": float(fm.mean()) if len(fm) else np.nan, "fm_t": fm_t,
            "keep": not fails, "fail_reasons": "；".join(fails),
        })

    # 参照尺：现有 25 列的 IC 加权合成（同样过一遍多空组合与 DSR），给 LLM 因子一把尺子
    snap["_comp"] = composite.to_numpy()
    comp_perf = perf(ls_returns(snap, "_comp", args.groups))
    comp_dsr, comp_sr0 = deflated_sharpe(comp_perf["sharpe"], comp_perf["n"], comp_perf["skew"],
                                         comp_perf["kurt"], trials, var_sr)
    comp_row = {"name": "_existing_composite", "expression": "现有 25 列 IC 加权合成（权重只用前 70% 月份）",
                "ic_full": float(comp_ic.mean()), "icir_full": float(comp_ic.mean() / comp_ic.std()),
                "nw_t_full": newey_west_t(comp_ic), "pos_ratio": float((comp_ic > 0).mean()),
                "fold_ic_mean": np.nan, "fold_ic_std": np.nan, "fold_sign_same": np.nan,
                "fold_ic_min": np.nan, "ls_sharpe_ann": comp_perf["sharpe_ann"], "ls_t": comp_perf["t"],
                "ls_n_months": comp_perf["n"], "sr0_threshold": comp_sr0, "dsr": comp_dsr,
                "resid_ic": np.nan, "resid_ic_t": np.nan, "resid_ic_drop": np.nan,
                "fm_beta_mean": np.nan, "fm_t": np.nan, "keep": False,
                "fail_reasons": "基准行（非候选，仅参照）"}
    summary = pd.concat([pd.DataFrame(rows), pd.DataFrame([comp_row])], ignore_index=True)
    summary = summary.sort_values("dsr", ascending=False)

    folds_df.to_csv(out_dir / "oos_folds.csv", index=False)
    summary.to_csv(out_dir / "deflated_summary.csv", index=False)
    picks_df.to_csv(out_dir / "walkforward_picks.csv", index=False)
    (out_dir / "pbo_cscv.json").write_text(json.dumps(
        {"pbo": pbo, "trials": trials, "n_candidates": len(valid), "groups": args.groups,
         "var_sharpe_across_candidates": var_sr,
         "trials_ledger": ledger, "trials_used": trials,
         "composite_ic_mean": float(comp_ic.mean()), "composite_ic_nw_t": newey_west_t(comp_ic),
         "composite_ls_sharpe_ann": comp_perf["sharpe_ann"], "composite_dsr": comp_dsr,
         "composite_sr0_threshold": comp_sr0,
         "control_cols": controls,
         "composite_weights": {k: round(float(v), 4) for k, v in w.items()},
         "thresholds": {"t_min": args.t_min, "dsr_min": args.dsr_min,
                        "resid_ic_min": args.resid_ic_min, "fm_t_min": args.fm_t_min}},
        ensure_ascii=False, indent=2), encoding="utf-8")

    kept = summary[summary["keep"]]
    lines = [
        "# LLM 因子：OOS / DSR / PBO / 增量信息评估（P1_25 L1+L2）",
        "",
        f"生成：{datetime.now():%Y-%m-%d %H:%M}　| run: `{run_dir}`　| 输出: `{out_dir}`",
        "",
        f"- 候选因子 {len(valid)} 个（试验次数 N={trials}；累计台账 N={ledger['unique_proposals']}，"
        f"见每行 `dsr_cumulative_ledger`）；月末快照 {len(months)} 个月（{months[0]}~{months[-1]}）",
        f"- purged {args.folds} 折（折边界 purge/embargo=1 月）；多空组合 = 因子前 {100//args.groups}% 减后 {100//args.groups}%（等权）",
        f"- **PBO(CSCV, {pbo.get('blocks')} 块 / {pbo.get('n_combos')} 组)** = {pbo.get('pbo'):.3f}"
        f"　| 候选间 Sharpe 方差 = {var_sr:.4f}",
        f"- 参照尺（现有 25 列 IC 加权合成，权重只用前 70% 月份）：月度 IC = {comp_ic.mean():.4f}，"
        f"NW t = {newey_west_t(comp_ic):.2f}，多空年化 SR = {comp_perf['sharpe_ann']:.2f}，DSR = {comp_dsr:.4f}",
        "",
        "## 准入判定（门槛：|NW t|≥%.1f，折间同号≥80%%，DSR≥%.2f，残差 IC≥%.2f，|FM t|≥%.1f）"
        % (args.t_min, args.dsr_min, args.resid_ic_min, args.fm_t_min),
        "",
        f"**通过 {len(kept)}/{len(summary)}**",
        "",
    ]
    if not summary.empty:
        lines += ["| 因子 | IC(全样本) | ICIR | NW t | 折间同号 | 折 IC 最小 | 多空年化SR | SR0门槛 | DSR | 残差IC | 残差IC t | FM t | 准入 | 未过原因 |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for _, r_ in summary.iterrows():
            def fmt(v, spec, dash="—"):
                return format(v, spec) if isinstance(v, (int, float)) and np.isfinite(v) else dash
            sign_same = f"{r_['fold_sign_same']:.0%}" if np.isfinite(r_['fold_sign_same']) else "—"
            lines.append(
                f"| `{r_['name']}` | {fmt(r_['ic_full'], '.4f')} | {fmt(r_['icir_full'], '.3f')} | "
                f"{fmt(r_['nw_t_full'], '.2f')} | {sign_same} | {fmt(r_['fold_ic_min'], '.4f')} | "
                f"{fmt(r_['ls_sharpe_ann'], '.2f')} | {fmt(r_['sr0_threshold'], '.3f')} | "
                f"{fmt(r_['dsr'], '.4f')} | {fmt(r_['resid_ic'], '.4f')} | {fmt(r_['resid_ic_t'], '.2f')} | "
                f"{fmt(r_['fm_t'], '.2f')} | {'✅' if r_['keep'] else '—'} | {r_['fail_reasons']} |")
        lines.append("")
    if not picks_df.empty:
        lines += ["## 滚动前向选择（用前 k-1 折挑最优 → 第 k 折测试）", "",
                  "| 测试折 | 训练期挑中 | 训练 ICIR | 该折测试 IC |", "|---|---|---|---|"]
        for _, r_ in picks_df.iterrows():
            lines.append(f"| {int(r_['fold'])} | `{r_['picked_on_train']}` | {r_['train_icir']:.3f} | "
                         f"{r_['test_ic_mean']:.4f} |")
        lines.append("")
    lines += ["## 保留意见", "",
              "1. 本脚本评的是**因子本身**（IC / 多空组合），不是最终策略；没扣交易成本、没加涨跌停与流动性约束。",
              "2. DSR/PBO 用**候选因子数**当试验次数，是最松的下界：如果 LLM 历史上还提过别的方向，真实 N 更大、门槛更高。",
              "3. 多空组合等权、全市场（未按小市值域过滤）；与 `[selection]` 的 10 选 5 落地口径不同，别把这里的 Sharpe 当成账户业绩。",
              "4. 残差 IC / FM t 只控制**面板里现有的 25 列**；模型外的新信息（公告、舆情等）不在控制集内。",
              "5. 结论是**单市场单段样本**（A 股 2018 起）——不是跨市场证据。", ""]
    (out_dir / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"[oos] PBO={pbo.get('pbo'):.3f}；通过 {len(kept)}/{len(summary)}；输出 {out_dir}", flush=True)
    if not summary.empty:
        cols = ["name", "ic_full", "fold_sign_same", "ls_sharpe_ann", "dsr", "resid_ic", "fm_t", "keep"]
        print(summary[cols].to_string(index=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
