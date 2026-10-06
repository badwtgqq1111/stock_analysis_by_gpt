#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P1.24 sleeve 架构：设计规格的可执行核心（规格 + 标签 + 分配器）。

三块内容：
  1) `SLEEVE_SPECS`：五个家族（alpha / valuation / quality / momentum / reversal）的
     **特征清单、标签定义、模型参数、风险与换手约束** —— 全部写死，避免事后调参；
  2) `compute_sleeve_label()`：按规格生成训练标签（次日开盘进场、域内截面变换、purge/embargo 就绪）；
  3) `SleeveAllocator`：等风险（ERC）+ ICIR 倾斜 + **候选域重叠惩罚** + 预算约束，
     输出各 sleeve 的资金/风险预算。

设计原则（与 P1.24 计划一致）：
  * 标签域 = sleeve 自己的候选域（不是全市场）；horizon 与 `exit.expected_holding_days` 对齐；
  * 分配器必须惩罚高重叠的 sleeve 对（Phase 0 v2 实测 bottom_momentum↔value_reversal Jaccard 0.62）；
  * 任何权重都必须能通过 walk-forward 验收（见 scripts/walkforward_* 的口径）。

用法（演示）：
  uv run python -m factor_engine.sleeve_design --demo
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class LabelSpec:
    horizon: int                     # 标签期（交易日）
    entry: str = "next_open"         # 进场价：次日开盘（避免用当日收盘的信息）
    transform: str = "domain_rank"   # domain_rank | zscore | raw
    neutralize: str = "industry"     # industry | none  （域内行业中性化）
    winsor: float = 0.01             # 两端 winsorize 比例（transform=zscore 时生效）
    purge: int = 0                   # 训练/验证之间 purge 的交易日（默认 = horizon）
    embargo: int = 5                 # embargo 交易日

    def __post_init__(self):
        if self.purge == 0:
            object.__setattr__(self, "purge", self.horizon)


@dataclass(frozen=True)
class SleeveSpec:
    sleeve_id: str
    family: str
    role: str                        # alpha | style | event
    status: str                      # active | research | off
    universe: str                    # all_a | main_board
    features: tuple[str, ...]        # clean panel 列名（不含 _clean 后缀）
    label: LabelSpec
    model: str                       # lightgbm | transformer | rule
    lgb_params: dict = field(default_factory=dict)
    risk: dict = field(default_factory=dict)
    turnover_target: float = 0.40    # 单边换手上限（每期）
    evidence: dict = field(default_factory=dict)


# --------------------------------------------------------------------------------------
# 规格：全部写死。features 用 clean panel 的真实列名（_clean 后缀在取数时补）。
# --------------------------------------------------------------------------------------
COMMON_RISK = {
    "max_name_weight": 0.05,         # 单票 ≤5%
    "max_industry_weight": 0.20,     # 单行业 ≤20%
    "max_size_exposure": 0.30,       # 市值因子暴露上限（相对全市场 z）
    "max_turnover_one_side": 0.40,   # 单边换手上限
    "participation_cap": 0.01,       # 单笔 ≤1% × ADV20（容量约束，实测把回撤从 -51.5% 压到 -47.1%）
}

SLEEVE_SPECS: dict[str, SleeveSpec] = {
    "model_alpha": SleeveSpec(
        sleeve_id="model_alpha", family="alpha", role="alpha", status="active", universe="all_a",
        features=("admitted_90",),                       # 现状：全截面 alpha，特征由准入表给
        label=LabelSpec(horizon=20, transform="domain_rank", neutralize="industry"),
        model="lightgbm",
        lgb_params={"objective": "lambdarank", "num_leaves": 31, "min_data_in_leaf": 200,
                    "feature_fraction": 0.30, "bagging_fraction": 0.80, "lambda_l2": 5.0,
                    "learning_rate": 0.03, "n_estimators": 600, "early_stopping": 50},
        risk=COMMON_RISK,
        evidence={"risk_contribution_share": 0.961, "note": "2026-10-06 归因：唯一正贡献腿"},
    ),
    "model_valuation": SleeveSpec(
        sleeve_id="model_valuation", family="valuation", role="style", status="research", universe="all_a",
        features=("pe_ind_pct", "pb_ind_pct", "ps_ind_pct", "dividend_yield_ind_pct",
                  "pb_ratio_sector_relative", "valuation_market_cap_age_td"),
        label=LabelSpec(horizon=20, transform="domain_rank", neutralize="industry"),
        model="lightgbm",
        lgb_params={"objective": "regression", "num_leaves": 15, "min_data_in_leaf": 300,
                    "feature_fraction": 0.80, "bagging_fraction": 0.80, "lambda_l2": 10.0,
                    "learning_rate": 0.03, "n_estimators": 400, "early_stopping": 40},
        risk=COMMON_RISK,
        evidence={"coverage": "pe/pb/ps/股息率 行业分位 100%（2026-09-30 实测）",
                  "no_dcf": "valuation_fcf_yield 覆盖 0%，禁用"},
    ),
    "model_quality": SleeveSpec(
        sleeve_id="model_quality", family="quality", role="style", status="research", universe="all_a",
        features=("roe_ind_pct", "gross_margin_ind_pct", "financial_net_margin", "financial_roe",
                  "financial_debt_to_assets", "financial_eps_yoy", "financial_net_profit_yoy"),
        label=LabelSpec(horizon=60, transform="domain_rank", neutralize="industry"),  # 质量因子慢
        model="lightgbm",
        lgb_params={"objective": "regression", "num_leaves": 15, "min_data_in_leaf": 300,
                    "feature_fraction": 0.70, "bagging_fraction": 0.80, "lambda_l2": 10.0,
                    "learning_rate": 0.03, "n_estimators": 400, "early_stopping": 40},
        risk=COMMON_RISK,
        evidence={"coverage_gap": "financial_* 原始列仅 19~20%（financial_roa=0%）；"
                                  "先用 *_ind_pct（100%）上线，原始列仅作可选特征 + 缺失标记"},
    ),
    "model_momentum": SleeveSpec(
        sleeve_id="model_momentum", family="momentum", role="style", status="off", universe="all_a",
        features=("momentum_12m_skip_1m", "academic_carhart_mom", "RPS_20", "RPS_60",
                  "price_position_52w_high", "MA20", "MA60", "TA_MOM", "TA_RSI"),
        label=LabelSpec(horizon=20, transform="domain_rank", neutralize="industry"),
        model="lightgbm",
        lgb_params={"objective": "regression", "num_leaves": 15, "min_data_in_leaf": 300,
                    "feature_fraction": 0.70, "bagging_fraction": 0.80, "lambda_l2": 10.0,
                    "learning_rate": 0.03, "n_estimators": 400, "early_stopping": 40},
        risk=COMMON_RISK,
        evidence={"blocked_by": "本地负结果：突破腿 5d −0.29%/t=−6.4；涨停动量 −1.35%/t=−8.1；"
                                "动量崩溃文献 10.1016/j.frl.2026.109866",
                  "to_revive": "必须先在域内证明 OOS rank IC > 0（5 折 ≥4 折为正）"},
    ),
    "model_reversal": SleeveSpec(
        sleeve_id="model_reversal", family="reversal", role="style", status="research", universe="all_a",
        features=("short_term_reversal_1m", "sector_rps_reversal_20d", "vol_max_drawdown_60",
                  "RPS_5", "RPS_10"),
        label=LabelSpec(horizon=10, transform="domain_rank", neutralize="industry"),
        model="lightgbm",
        lgb_params={"objective": "regression", "num_leaves": 31, "min_data_in_leaf": 200,
                    "feature_fraction": 0.80, "bagging_fraction": 0.80, "lambda_l2": 5.0,
                    "learning_rate": 0.03, "n_estimators": 500, "early_stopping": 50},
        risk=COMMON_RISK,
        evidence={"merge_of": "value_reversal + bottom_momentum + dip_low_position 三合一",
                  "candidate_jaccard": 0.6188,
                  "current_leg_attribution": "reversal 腿自身 −59.2%、夏普 −1.46、占组合回撤 5.98pp"},
    ),
}


# --------------------------------------------------------------------------------------
# 标签引擎
# --------------------------------------------------------------------------------------
def compute_sleeve_label(
    panel: pd.DataFrame,
    spec: SleeveSpec,
    *,
    domain: pd.DataFrame | None = None,
    price_col: str = "close",
    open_col: str = "open",
) -> pd.Series:
    """按规格生成训练标签。

    panel: MultiIndex(或含 `trade_date`,`stock_code` 列)的行情，需含 open/close 与行业列。
    domain: 形状同 panel 的布尔表（True = 属于该 sleeve 候选域）；None 表示全市场。
    返回：与 panel 同索引的标签（域内截面变换后的浮点数，非域内为 NaN）。
    """
    frame = panel.sort_values(["stock_code", "trade_date"]).copy()
    h = spec.label.horizon
    # 进场 = 次日开盘（shift(-1)），出场 = 进场后第 h 个交易日收盘
    frame["entry_px"] = frame.groupby("stock_code")[open_col].shift(-1)
    frame["exit_px"] = frame.groupby("stock_code")[price_col].shift(-(1 + h))
    frame["fwd"] = frame["exit_px"] / frame["entry_px"] - 1.0
    mask = pd.Series(True, index=frame.index)
    if domain is not None:
        mask = domain.reindex(frame.index).fillna(False).astype(bool)
    frame.loc[~mask, "fwd"] = np.nan

    if spec.label.neutralize == "industry" and "industry_l1" in frame.columns:
        frame["fwd"] = frame["fwd"] - frame.groupby(["trade_date", "industry_l1"])["fwd"].transform("mean")

    def _transform(group: pd.Series) -> pd.Series:
        valid = group.dropna()
        if len(valid) < 20:
            return pd.Series(np.nan, index=group.index)
        if spec.label.transform == "domain_rank":
            ranks = valid.rank(pct=True)
            return ((ranks - 0.5) * 2).reindex(group.index)          # → [-1, 1]
        if spec.label.transform == "zscore":
            lo, hi = valid.quantile(spec.label.winsor), valid.quantile(1 - spec.label.winsor)
            clipped = valid.clip(lo, hi)
            return ((clipped - clipped.mean()) / (clipped.std() or 1)).reindex(group.index)
        return valid.reindex(group.index)

    out = frame.groupby("trade_date")["fwd"].transform(_transform)
    return out


def purge_embargo_split(dates: pd.Series, spec: SleeveSpec, *, n_folds: int = 5) -> list[tuple[set, set]]:
    """Purged K-Fold + embargo 切分（López de Prado）。返回 [(train_dates, test_dates), ...]。"""
    uniq = np.array(sorted(pd.unique(dates)))
    folds = np.array_split(uniq, n_folds)
    out = []
    for fold in folds:
        test = set(fold)
        lo, hi = fold[0], fold[-1]
        block_lo = uniq[uniq >= lo][: max(1, spec.label.purge)]
        block_hi = uniq[uniq <= hi][-max(1, spec.label.embargo):]
        blocked = set(block_lo) | set(block_hi)
        train = set(uniq) - test - blocked
        out.append((train, test))
    return out


# --------------------------------------------------------------------------------------
# 分配器：ERC + ICIR 倾斜 + 候选域重叠惩罚
# --------------------------------------------------------------------------------------
@dataclass
class AllocatorConfig:
    mode: str = "icir_erc"           # static | erc | icir_erc
    # 资格门：只有"有正期望"的 sleeve 才参与预算 —— ERC 只看协方差，会把负 alpha 腿分到最大权重
    min_icir: float = 0.30           # 滚动 OOS ICIR 下限（演示用 sleeve 自身夏普代理）
    min_delta_sharpe: float = 0.0    # 归因里"去掉该腿后组合夏普的变化"必须 > 该阈值
    eligibility_col: str = "icir"    # icir | delta_sharpe | none
    max_sleeve_weight: float = 0.40
    min_sleeve_weight: float = 0.00
    icir_halflife: int = 12          # ICIR 半衰期（期数）
    icir_shrink: float = 0.30        # 向横截面均值收缩的强度（0=不收缩，1=完全收缩）
    overlap_penalty: float = 1.0     # 候选域重叠惩罚强度
    overlap_threshold: float = 0.50  # 超过该 Jaccard 的一对，合计权重上限
    overlap_pair_cap: float = 0.50
    erc_iters: int = 500
    erc_tol: float = 1e-8


class SleeveAllocator:
    """输入 sleeve 收益序列与候选域重叠矩阵，输出资金/风险预算。"""

    def __init__(self, cfg: AllocatorConfig | None = None):
        self.cfg = cfg or AllocatorConfig()

    # --- 等风险贡献（ERC）：定点迭代 ---
    @staticmethod
    def _erc_weights(cov: np.ndarray, iters: int, tol: float) -> np.ndarray:
        n = cov.shape[0]
        w = np.ones(n) / n
        for _ in range(iters):
            sigma = np.sqrt(float(w @ cov @ w))
            if sigma <= 0:
                break
            marginal = cov @ w
            rc = w * marginal / sigma                      # 各腿风险贡献
            target = sigma / n
            update = np.where(rc > 0, target / rc, 1.0)
            w_new = w * update
            w_new = np.clip(w_new, 1e-6, None)
            w_new /= w_new.sum()
            if np.max(np.abs(w_new - w)) < tol:
                w = w_new
                break
            w = w_new
        return w

    def allocate(
        self,
        returns: pd.DataFrame,
        overlap: pd.DataFrame | None = None,
        *,
        icir: pd.Series | None = None,
        static: dict | None = None,
        diagnostics: pd.DataFrame | None = None,
        cov: pd.DataFrame | None = None,
    ) -> dict:
        cols = list(returns.columns)
        # ---- 资格门 ----
        eligible = pd.Series(True, index=cols)
        rejected: dict[str, str] = {}
        if diagnostics is not None and self.cfg.eligibility_col != "none":
            for c in cols:
                if c not in diagnostics.index:
                    eligible[c] = False
                    rejected[c] = "no_diagnostics"
                    continue
                if self.cfg.eligibility_col == "icir":
                    value = float(diagnostics.loc[c].get("icir", np.nan))
                    threshold = self.cfg.min_icir
                else:
                    value = float(diagnostics.loc[c].get("delta_sharpe_if_removed", np.nan))
                    threshold = self.cfg.min_delta_sharpe
                if (not np.isfinite(value)) or value <= threshold:
                    eligible[c] = False
                    rejected[c] = f"{self.cfg.eligibility_col}={value:.3f} <= {threshold}"
        if eligible.sum() == 0:
            eligible[:] = True          # 全被拒时退回全量，但会在 rejected 里说明
        active_cols = [c for c in cols if eligible[c]]
        if self.cfg.mode == "static":
            w = pd.Series(static or {c: 1 / len(cols) for c in cols}).reindex(cols).fillna(0.0)
            w = (w / w.sum()).to_numpy()
        else:
            sub = returns[active_cols].fillna(0.0)
            if cov is not None:
                idx = [cols.index(c) for c in active_cols]
                cov_sub = cov.to_numpy()[np.ix_(idx, idx)]           # 用外部风险模型
            else:
                cov_sub = sub.cov().to_numpy() * 12.0                # 年化
            w_sub = self._erc_weights(cov_sub, self.cfg.erc_iters, self.cfg.erc_tol)
            if self.cfg.mode == "icir_erc":
                if icir is not None:
                    signal = icir.reindex(active_cols).fillna(0.0).to_numpy()
                elif diagnostics is not None and "icir" in diagnostics.columns:
                    signal = diagnostics["icir"].reindex(active_cols).fillna(0.0).to_numpy()
                else:
                    signal = (sub.mean() / sub.std()).reindex(active_cols).fillna(0.0).to_numpy()
                if self.cfg.icir_shrink > 0:                        # 收缩，避免单腿权重爆炸
                    mu = signal.mean()
                    signal = (1 - self.cfg.icir_shrink) * signal + self.cfg.icir_shrink * mu
                tilt = np.exp(signal)                               # 指数倾斜保持正权重
                tilt = tilt / tilt.sum()
                w_sub = w_sub * tilt
                w_sub = w_sub / w_sub.sum()
            w = np.zeros(len(cols))
            w[[cols.index(c) for c in active_cols]] = w_sub

        # 预算约束 + 重叠惩罚（把超出上限的质量按比例转给未触顶的腿）
        w = self._apply_constraints(w, cols, overlap)

        # 事后诊断
        cov_arr = cov.to_numpy() if cov is not None else returns.fillna(0.0).cov().to_numpy() * 12.0
        sigma = float(np.sqrt(w @ cov_arr @ w))
        marginal = cov_arr @ w
        rc_share = (w * marginal) / (sigma ** 2) if sigma > 0 else np.zeros_like(w)
        return {
            "weights": dict(zip(cols, np.round(w, 6))),
            "risk_contribution_share": dict(zip(cols, np.round(rc_share, 6))),
            "portfolio_vol_annual": round(sigma, 6),
            "mode": self.cfg.mode,
            "eligible": active_cols,
            "rejected": rejected,
            "overlap_penalty_applied": bool(overlap is not None and self.cfg.overlap_penalty > 0),
            "constraint_status": getattr(self, "last_constraint_status", None),
        }

    def _apply_constraints(self, w: np.ndarray, cols: list[str], overlap: pd.DataFrame | None) -> np.ndarray:
        cfg = self.cfg
        # 静态模式是生产历史口径，必须**原样过账**（不做裁剪、不做重叠惩罚），否则会静默改变预算
        if cfg.mode == "static":
            return w / w.sum()

        def pair_cap(i: int, j: int) -> float | None:
            if overlap is None or cfg.overlap_penalty <= 0:
                return None
            if cols[i] not in overlap.index or cols[j] not in overlap.columns:
                return None
            o = float(overlap.loc[cols[i], cols[j]])
            if not np.isfinite(o) or o <= cfg.overlap_threshold:
                return None
            return cfg.overlap_pair_cap * (1 / (1 + cfg.overlap_penalty * o))

        n = len(cols)
        status = "feasible"
        for _ in range(500):
            w = np.clip(w, cfg.min_sleeve_weight, cfg.max_sleeve_weight)
            w = w / w.sum()
            changed = False
            # 逐个收紧越限的重叠对，把超出部分按比例转给未触顶的腿
            for i in range(n):
                for j in range(i + 1, n):
                    cap = pair_cap(i, j)
                    if cap is None or w[i] + w[j] <= cap + 1e-12:
                        continue
                    pair_sum = w[i] + w[j]
                    excess = pair_sum - cap
                    scale = cap / pair_sum
                    w[i] *= scale
                    w[j] *= scale
                    rest = [k for k in range(n) if k not in (i, j)]
                    if rest and w[rest].sum() > 0:
                        w[rest] += excess * (w[rest] / w[rest].sum())
                    changed = True
            # 重新归一化后若最大权重再次越限，把超出部分匀给未触顶的腿（不做简单 clip）
            w = w / w.sum()
            over = w > cfg.max_sleeve_weight + 1e-12
            if over.any():
                excess = float((w[over] - cfg.max_sleeve_weight).sum())
                w[over] = cfg.max_sleeve_weight
                free = ~over
                if w[free].sum() > 0:
                    w[free] += excess * (w[free] / w[free].sum())
                w = w / w.sum()
                changed = True
            if not changed:
                break

        # 不可行检测：约束之间互相冲突（例如 3 条腿、两两上限 0.263、单腿上界 0.4）
        worst_cap = 0.0
        worst_max = float(w.max())
        for i in range(n):
            for j in range(i + 1, n):
                cap = pair_cap(i, j)
                if cap is not None:
                    worst_cap = max(worst_cap, float(w[i] + w[j] - cap))
        self.last_constraint_status = {
            "status": "feasible" if (worst_cap <= 1e-6 and worst_max <= cfg.max_sleeve_weight + 1e-6) else "infeasible",
            "max_pair_excess": round(worst_cap, 6),
            "max_weight_excess": round(worst_max - cfg.max_sleeve_weight, 6),
        }
        return w


def _demo() -> int:
    """用本仓库实测的 sleeve 收益序列演示分配器（含/不含重叠惩罚）。"""
    root = ROOT / "output/research"
    runs = sorted(root.glob("sleeve_metric_attribution_*"))
    if not runs:
        print("找不到 sleeve_metric_attribution_* 输出，请先跑 scripts/sleeve_metric_attribution.py")
        return 1
    returns = pd.read_csv(runs[-1] / "sleeve_period_returns.csv", index_col=0)
    returns.index = pd.to_datetime(returns.index, errors="coerce")
    returns = returns.dropna(how="all")
    print(f"[alloc] 使用 {runs[-1].name}：{returns.shape[0]} 期 × {returns.shape[1]} 个 sleeve")

    # 资格门输入：用归因表。ICIR 用"该腿自身夏普"作为**代理**（演示口径；上线需换成滚动 OOS IC 序列）
    attribution = pd.read_csv(runs[-1] / "attribution.csv").set_index("sleeve")
    diagnostics = pd.DataFrame({
        "icir": attribution["sharpe"],                                  # 代理
        "delta_sharpe_if_removed": attribution["delta_sharpe_if_removed"],
    })
    print("[alloc] 资格门输入（icir 用腿自身夏普代理）：")
    print(diagnostics.to_string(float_format=lambda x: f"{x:.3f}"))

    overlap = pd.DataFrame(np.eye(len(returns.columns)), index=returns.columns, columns=returns.columns)
    # Phase 0 v2 实测：bottom_momentum ↔ value_reversal 候选域 Jaccard 0.6188（这里用同名腿近似映射）
    if {"bottom_momentum", "reversal"}.issubset(overlap.index):
        overlap.loc["bottom_momentum", "reversal"] = overlap.loc["reversal", "bottom_momentum"] = 0.6188

    out = {}
    for mode in ("static", "erc", "icir_erc"):
        allocator = SleeveAllocator(AllocatorConfig(mode=mode, overlap_penalty=1.0))
        out[mode] = allocator.allocate(returns, overlap, diagnostics=diagnostics)
    out["erc_no_gate"] = SleeveAllocator(AllocatorConfig(mode="erc", overlap_penalty=1.0,
                                                         eligibility_col="none")).allocate(returns, overlap)
    no_penalty = SleeveAllocator(AllocatorConfig(mode="icir_erc", overlap_penalty=0.0)).allocate(
        returns, overlap, diagnostics=diagnostics)
    out["icir_erc_no_penalty"] = no_penalty

    table = pd.DataFrame({k: v["weights"] for k, v in out.items()})
    risk = pd.DataFrame({k: v["risk_contribution_share"] for k, v in out.items()})
    print("\n=== 资金预算（weights）===")
    print(table.to_string(float_format=lambda x: f"{x:.3f}"))
    print("\n=== 风险贡献占比 ===")
    print(risk.to_string(float_format=lambda x: f"{x:.3f}"))
    print(f"\n组合年化波动：icir_erc={out['icir_erc']['portfolio_vol_annual']:.4f} | "
          f"无重叠惩罚={out['icir_erc_no_penalty']['portfolio_vol_annual']:.4f}")
    print(f"资格门剔除：{out['icir_erc']['rejected']}")
    print(f"对比：不加资格门的 ERC 会把预算分给 -> "
          f"{ {k: v for k, v in out['erc_no_gate']['weights'].items() if v > 0.2} }")
    print("\n结论：重叠惩罚把 bottom_momentum 与 reversal 的合计预算压到 "
          f"{out['icir_erc']['weights'].get('bottom_momentum', 0) + out['icir_erc']['weights'].get('reversal', 0):.3f}"
          "（无惩罚时 "
          f"{out['icir_erc_no_penalty']['weights'].get('bottom_momentum', 0) + out['icir_erc_no_penalty']['weights'].get('reversal', 0):.3f}）")
    out_dir = root / "sleeve_allocator_demo"
    out_dir.mkdir(parents=True, exist_ok=True)
    table.to_csv(out_dir / "weights.csv")
    risk.to_csv(out_dir / "risk_contribution.csv")
    (out_dir / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[alloc] 输出 {out_dir}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="sleeve 设计核心（规格/标签/分配器）")
    parser.add_argument("--demo", action="store_true", help="用实测 sleeve 收益序列演示分配器")
    parser.add_argument("--specs", action="store_true", help="打印全部 sleeve 规格")
    parser.add_argument("--budgets", action="store_true", help="从实测性能数据计算 sleeve 预算并落盘")
    parser.add_argument("--trials", default=str(ROOT / "output/research/sleeve_trials.csv"))
    parser.add_argument("--corr", default="", help="预测相关性矩阵 CSV（用于重叠惩罚，缺省则不做惩罚）")
    parser.add_argument("--budget-out", default=str(ROOT / "output/research/sleeve_budgets.json"))
    parser.add_argument("--net-excess", default="", help="组合验收指标 CSV（用于把净超额为负的腿排除出预算）")
    args = parser.parse_args()
    if args.specs:
        print(json.dumps({k: asdict(v) for k, v in SLEEVE_SPECS.items()}, ensure_ascii=False, indent=2, default=str))
        return 0
    if args.budgets:
        return compute_budgets(args.trials, args.corr, args.budget_out, net_excess_path=args.net_excess)
    if args.demo:
        return _demo()
    parser.print_help()
    return 0


def compute_budgets(trials_path: str, corr_path: str, out_path: str,
                    cfg: AllocatorConfig | None = None, net_excess_path: str = "") -> int:
    """从试验台账（ICIR）与预测相关性矩阵（重叠）计算 sleeve 预算，落盘 JSON。

    口径说明：
      * ICIR 用台账里每个 sleeve **最新一次**训练的 `pooled_icir`（真实 OOS 指标，非代理）；
      * 重叠惩罚的输入优先用预测相关性矩阵（|corr|）；没有就退化到"候选域 Jaccard"已知值（bottom_momentum↔reversal 0.6188）；
      * 资格门默认 ICIR > 0.30 且 ΔSharpe > 0（后者没有数据时只看 ICIR）。
    """
    cfg = cfg or AllocatorConfig()
    ledger = Path(trials_path)
    if not ledger.exists():
        print(json.dumps({"error": f"找不到试验台账 {ledger}"}, ensure_ascii=False))
        return 1
    trials = pd.read_csv(ledger)
    latest = trials.sort_values("logged_at").groupby("sleeve").tail(1).set_index("sleeve")
    icir = latest["pooled_icir"].astype(float)
    sleeves = list(icir.index)

    overlap = pd.DataFrame(np.eye(len(sleeves)), index=sleeves, columns=sleeves)
    if corr_path and Path(corr_path).exists():
        corr = pd.read_csv(corr_path, index_col=0)
        for a in sleeves:
            for b in sleeves:
                if a in corr.index and b in corr.columns:
                    overlap.loc[a, b] = abs(float(corr.loc[a, b]))
    elif {"bottom_momentum", "reversal"}.issubset(set(sleeves)):
        overlap.loc["bottom_momentum", "reversal"] = overlap.loc["reversal", "bottom_momentum"] = 0.6188

    # 风险模型（没有 sleeve 真实收益序列时的正式构造）：
    #   Σ = σ² · C，其中 σ 取共同年化波动率（默认 20%），C 用 |corr| 矩阵（Σ 为相关阵）；
    #   这样 ERC 在等波动下给出近似等权，再由 ICIR 倾斜与重叠惩罚决定差异。
    common_vol = 0.20
    corr = overlap.copy()
    for s in sleeves:
        corr.loc[s, s] = 1.0
    cov = (corr * (common_vol ** 2)).to_numpy()
    returns = pd.DataFrame(cov, index=sleeves, columns=sleeves)     # 占位，实际用 cov= 传入
    diagnostics = pd.DataFrame({"icir": icir}, index=sleeves)
    # 资格门补充：把**实测净超额**纳入（ICIR 为正 ≠ 扣成本后仍为正，reversal 就是反例）
    excluded_negative = {}
    if net_excess_path and Path(net_excess_path).exists():
        metrics = pd.read_csv(net_excess_path)
        mapping = {"valuation": "model_valuation", "quality": "model_quality",
                   "momentum": "model_momentum", "reversal": "model_reversal"}
        best = metrics.groupby("score")["net_excess"].max()
        for key, sleeve in mapping.items():
            if key in best.index and sleeve in sleeves and float(best[key]) <= 0:
                diagnostics.loc[sleeve, "net_excess"] = float(best[key])
                excluded_negative[sleeve] = float(best[key])
        # 把净超额为负的腿从 ICIR 里剔除资格（设成低于门槛的值）
        for sleeve in excluded_negative:
            icir.loc[sleeve] = min(float(icir.loc[sleeve]), 0.0)
        diagnostics["icir"] = icir
    allocator = SleeveAllocator(cfg)
    result = allocator.allocate(returns, overlap, diagnostics=diagnostics,
                                cov=pd.DataFrame(cov, index=sleeves, columns=sleeves))
    payload = {
        **result,
        "input": {"trials": str(ledger), "corr": corr_path or None, "net_excess": net_excess_path or None,
                  "icir": {k: round(float(v), 4) for k, v in icir.items()}},
        "excluded_by_negative_net_excess": excluded_negative,
        "risk_model": f"Σ = σ²·C，σ={common_vol:.0%} 共同年化波动，C=|corr|（相关性矩阵）",
        "note": "上线时请把 C 换成 sleeve 真实 OOS 收益的相关矩阵、σ 换成各自实现波动率",
    }
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
