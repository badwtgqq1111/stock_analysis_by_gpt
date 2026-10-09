#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""影子组合（shadow portfolios）：把"决策逻辑"的对照长期跑起来（2026-10-09）

为什么必须有这个
----------------
`scripts/decision_logic_ab.py` 已经能算对照，但它依赖台账（事后），而且**样本只有
notify 6 个决策日 / preselected 20 个决策日**。"现有决策逻辑是不是失败"这个问题，
用这点样本永远答不出来 —— 缺的不是分析，是**对照数据**。

所以这里做的是：从今天起，每天用**同一套统一执行假设**（`config/execution_assumptions_cn.json`，
T+1 开盘成交 + 佣金/滑点/印花税 + 涨跌停/停牌/退市）在同一个资金规模上并列跑几条规则：

  production            现行＝notify 实际 PK 权重（也是 paper account 的主口径）
  notify_rankcap20      现行 + rank≤20 上限（全部分层假设里唯一通过按决策日符号检验的那条改动）
  pool_top5_rank        候选池按 rank 前 5 等权（"回到最朴素的选股"对照）
  pool_top5_lowvol      池前 5 + 排除底部反转 + ATR14≤4% + 20日波动≤4%（待验证假设）

三条对照的名单直接从 `output/results_cn/selection_ledger.parquet` 重建（台账每天由
`build_selection_ledger.py` 更新），所以历史可以立刻回填到 2025-09，不需要从零累积。

口径诚实性：
  * 这里全部是**模拟**，不是实盘；成本按统一假设算；
  * `production` 是当前生产口径，另外三条是**对照**，不代表推荐；
  * 决策日不固定（周频为主），样本仍在累积 —— summary.json 里带 `decision_dates` 和 `span`，
    少于 20 个决策日不要读结论。

用法:
  uv run python scripts/run_shadow_variants.py
  uv run python scripts/run_shadow_variants.py --variants production,notify_rankcap20
输出:
  output/results_cn/shadow_variants/<variant>.csv          每条规则的选股历史（trade_date, stock_code, target_weight）
  output/paper_trading/shadow/<variant>/{nav,fills,orders,positions,...}.csv + summary
  output/paper_trading/shadow/summary.json                 对照总表（给 API / 前端读）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from factor_engine.portfolio.execution_assumptions import cost_model, load_assumptions  # noqa: E402
from factor_engine.portfolio.paper_account import persist_paper_account, run_paper_account  # noqa: E402

LEDGER = ROOT / "output/results_cn/selection_ledger.parquet"
HIST_DIR = ROOT / "output/results_cn/shadow_variants"
OUT_DIR = ROOT / "output/paper_trading/shadow"
REF_DIR = ROOT / "output/paper_trading/account_reference"     # 参考账户（模拟盘绑定的那条规则）
CONFIG = ROOT / "config/cn_pipeline.toml"
PROD_ACC = ROOT / "output/paper_trading/account"              # 生产口径账户（对照）
BAR_YEARS = (2025, 2026)
TOPN = 5
RANK_CAP = 20
LABELS = {
    "production": "现行（notify 实际 PK 权重）",
    "notify_rankcap20": "现行 + rank≤20 + 不足 5 只用池前补位（等权）",
    "notify_rankcap20_naive": "现行 + rank≤20（不补位，仅归一化）← 反例",
    "pool_top5_rank": "池前 5 等权（周频）",
    "pool_top8_rank": "池前 8 等权（周频）",
    "pool_top10_rank": "池前 10 等权（周频）",
    "pool_top5_biweekly": "池前 5 等权（每两个决策日调一次）",
    "pool_top5_monthly": "池前 5 等权（每月末决策日）",
    "pool_top5_lowvol": "池前 5 + 排底部反转 + 低波动",
}
# "买多少只 + 多久换一次" 是上一轮发现的最大正贡献（池前5低换手 +33% vs 整池 +5.7%），
# 所以宽度（5/8/10）与节奏（每周/每两周/每月）都进影子组合，长期用真实成交数据比。
WIDTHS = (5, 8, 10)
ORDER = ["production", "notify_rankcap20", "notify_rankcap20_naive", "pool_top5_rank", "pool_top8_rank", "pool_top10_rank",
         "pool_top5_biweekly", "pool_top5_monthly", "pool_top5_lowvol"]


def _ew(frame: pd.DataFrame) -> pd.DataFrame:
    f = frame.copy()
    f["target_weight"] = 1.0 / len(f)
    return f[["trade_date", "stock_code", "target_weight"]]


def build_histories(ledger: pd.DataFrame, variants: list[str]) -> dict[str, pd.DataFrame]:
    """从台账重建每条规则的选股历史。台账已经累积了全部决策日，所以历史立刻可用。"""
    pool = ledger[ledger["source"] == "preselected"].copy()
    ntf = ledger[ledger["source"] == "notify"].copy()
    out: dict[str, pd.DataFrame] = {}

    def pick(rows: pd.DataFrame, topn: int = TOPN) -> pd.DataFrame:
        return rows.sort_values(["rank", "model_score"], ascending=[True, False]).head(topn)

    if {"production", "notify_rankcap20", "notify_rankcap20_naive"} & set(variants):
        prod_rows, cap_rows, naive_rows = [], [], []
        pool_by_date = {d: g for d, g in pool.groupby("trade_date")}
        for date, g in ntf.groupby("trade_date"):
            w = g[g["weight"] > 0].copy()
            if w.empty:
                continue
            w["target_weight"] = w["weight"] / w["weight"].sum()
            prod_rows.append(w[["trade_date", "stock_code", "target_weight"]])

            capped = w[w["rank"] <= RANK_CAP]
            if capped.empty:
                # ① 反例口径：不补位，只把剩下的权重重归一化（会造出 100% 单票）
                naive_rows.append(_ew(pick(g)))
                # ② 生产候选口径：rank 合格票 + 池前补位，**最终等权**
                top_up = pick(pool_by_date.get(date, g))
                names = list(dict.fromkeys(list(capped["stock_code"]) + list(top_up["stock_code"])))
                chosen = pd.DataFrame({"trade_date": date, "stock_code": names})
                cap_rows.append(_ew(chosen))
                continue
            if len(capped) >= TOPN:
                # 合格票够用：按 PK 相对权重归一（保留 PK 的排序信息）
                capped = capped.copy()
                capped["target_weight"] = capped["weight"] / capped["weight"].sum()
                cap_rows.append(capped[["trade_date", "stock_code", "target_weight"]])
                naive_rows.append(capped[["trade_date", "stock_code", "target_weight"]])
            else:
                # 合格票不足 5 只：反例口径只归一化（集中度上升），候选口径用池前补位到 5 只并等权
                capped = capped.copy()
                capped["target_weight"] = capped["weight"] / capped["weight"].sum()
                naive_rows.append(capped[["trade_date", "stock_code", "target_weight"]])
                top_up = pick(pool_by_date.get(date, g))
                names = list(dict.fromkeys(list(capped["stock_code"]) + list(top_up["stock_code"])))[:TOPN]
                chosen = pd.DataFrame({"trade_date": date, "stock_code": names})
                cap_rows.append(_ew(chosen))
        if prod_rows:
            out["production"] = pd.concat(prod_rows, ignore_index=True)
        if cap_rows:
            out["notify_rankcap20"] = pd.concat(cap_rows, ignore_index=True)
        if naive_rows:
            out["notify_rankcap20_naive"] = pd.concat(naive_rows, ignore_index=True)

    # ── 宽度：池前 5 / 8 / 10 等权（周频，每个决策日都调） ──
    for width in WIDTHS:
        name = f"pool_top{width}_rank"
        if name not in variants:
            continue
        rows = [_ew(pick(g, width)) for _, g in pool.groupby("trade_date") if not g.empty]
        out[name] = pd.concat(rows, ignore_index=True)

    # ── 节奏：把决策日稀疏化（引擎里"没有新决策＝保持持仓"，所以降频＝少喂决策日） ──
    pool_dates = sorted(pool["trade_date"].unique())

    def sparse(dates: list, every: int | None, monthly: bool) -> list:
        if monthly:
            keep = {}
            for d in dates:
                keep[pd.Timestamp(d).to_period("M")] = d
            return sorted(keep.values())
        return list(dates[::every]) if every else list(dates)

    if "pool_top5_biweekly" in variants:
        keep = set(sparse(pool_dates, 2, False))
        rows = [_ew(pick(g)) for d, g in pool.groupby("trade_date") if d in keep and not g.empty]
        out["pool_top5_biweekly"] = pd.concat(rows, ignore_index=True)

    if "pool_top5_monthly" in variants:
        keep = set(sparse(pool_dates, None, True))
        rows = [_ew(pick(g)) for d, g in pool.groupby("trade_date") if d in keep and not g.empty]
        out["pool_top5_monthly"] = pd.concat(rows, ignore_index=True)

    if "pool_top5_lowvol" in variants:
        # 待验证假设：排除「底部反转尝试」、ATR14 ≤4%、20 日波动率 ≤4%（见 P2_37 复盘 §5.B）
        rows = []
        for _, g in pool.groupby("trade_date"):
            # 与 decision_logic_ab.py 的 pool_top5_lowvol 保持**同一条规则**（含 rank≤20），
            # 否则影子组合和 A/B 会对不上（踩过一次）
            sub = g[(pd.to_numeric(g["rank"], errors="coerce") <= RANK_CAP)
                    & (g["stage_label"] != "底部反转尝试")
                    & (pd.to_numeric(g["atr14_pct"], errors="coerce") <= 4)
                    & (pd.to_numeric(g["vol20_pct"], errors="coerce") <= 4)]
            if sub.empty:
                continue
            rows.append(_ew(pick(sub)))
        out["pool_top5_lowvol"] = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()

    return out


def _reference_variant(cli_value: str | None) -> str:
    """参考口径：CLI > config[paper_account].reference_variant > pool_top10_rank。"""
    if cli_value:
        return cli_value
    try:
        import tomllib
        cfg = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
        return str(cfg.get("paper_account", {}).get("reference_variant") or "pool_top10_rank")
    except Exception:
        return "pool_top10_rank"


def _nav_frame(path: Path) -> pd.Series | None:
    if not path.exists():
        return None
    frame = pd.read_csv(path)
    frame["asof_date"] = frame["asof_date"].astype(str).str.slice(0, 10)
    return frame.set_index("asof_date")["nav"]


def write_consistency(reference: str, history: pd.DataFrame) -> dict:
    """把"参考账户和谁一致、和谁不一致"变成可查证的数字，而不是口头保证。"""
    ref = _nav_frame(REF_DIR / "nav.csv")
    shadow = _nav_frame(OUT_DIR / reference / "nav.csv")
    prod = _nav_frame(PROD_ACC / "nav.csv")
    payload: dict = {"reference_variant": reference, "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    if ref is not None and shadow is not None:
        common = ref.index.intersection(shadow.index)
        payload["vs_shadow_same_variant"] = {
            "days": int(len(common)),
            "max_abs_nav_diff": round(float((ref[common] - shadow[common]).abs().max()), 10),
            "identical": bool(np.allclose(ref[common].to_numpy(), shadow[common].to_numpy(), atol=1e-9)),
        }
    prod_hist = ROOT / "output/results_cn/paper_selection_history.csv"
    if prod_hist.exists() and prod is not None and ref is not None:
        prod_codes = set(pd.read_csv(prod_hist)["stock_code"].astype(str))
        ref_codes = set(history["stock_code"].astype(str))
        prod_days = set(pd.read_csv(prod_hist)["trade_date"].astype(str).str.slice(0, 10))
        ref_days = set(history["trade_date"].astype(str).str.slice(0, 10))
        payload["vs_production_account"] = {
            "production_decision_dates": len(prod_days), "reference_decision_dates": len(ref_days),
            "common_decision_dates": len(prod_days & ref_days),
            "production_last_nav": round(float(prod.iloc[-1]), 2),
            "reference_last_nav": round(float(ref.iloc[-1]), 2),
            "same_codes": len(prod_codes & ref_codes), "production_codes": len(prod_codes),
            "reference_codes": len(ref_codes),
            "note": ("口径相同（同一引擎 + 同一份 execution_assumptions_cn.json），"
                     "差别只在**选股名单**：生产账户用 paper_selection_history.csv（含归因回填的等权兜底日），"
                     "参考账户用影子组合的 <变体>.csv。两者净值不可直接比大小。"),
        }
    REF_DIR.mkdir(parents=True, exist_ok=True)
    (REF_DIR / "consistency.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def price_bars() -> pd.DataFrame:
    import pyarrow.dataset as ds
    files = [str(p) for y in BAR_YEARS for p in (ROOT / "assets/data/clean/ohlcv/market=CN").glob(
        f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={y}/*.parquet")]
    bars = ds.dataset(files, format="parquet").to_table(
        columns=["stock_code", "trade_date", "open", "close"]).to_pandas()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    return bars[bars["trade_date"] >= "2025-06-01"]


def main() -> int:
    ap = argparse.ArgumentParser(description="影子组合（决策逻辑的长期对照）")
    ap.add_argument("--variants", default=",".join(ORDER))
    ap.add_argument("--capital", type=float, default=45_000.0)
    ap.add_argument("--reference", default=None, help="参考账户绑定的变体（默认读 config[paper_account].reference_variant）")
    args = ap.parse_args()
    wanted = [v.strip() for v in args.variants.split(",") if v.strip()]
    wanted = [v for v in ORDER if v in wanted] or ORDER

    import importlib.util

    spec = importlib.util.spec_from_file_location("selection_history_review",
                                                  ROOT / "scripts/selection_history_review.py")
    review = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(review)

    # dedup=False：各来源各自的选股行（否则候选池的行会被 notify 的行顶掉）
    ledger = review.enrich(pd.read_parquet(LEDGER), dedup=False)
    histories = build_histories(ledger, wanted)
    bars = price_bars()
    HIST_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    rows, curves = [], {}
    for name in wanted:
        sel = histories.get(name)
        if sel is None or sel.empty:
            print(f"[shadow] {name}: 没有可用于回放的决策日，跳过")
            continue
        sel = sel.dropna(subset=["trade_date", "stock_code"])
        sel.to_csv(HIST_DIR / f"{name}.csv", index=False)
        result = run_paper_account(sel, bars, initial_capital=args.capital,
                                   strategy_version=f"shadow-{name}")
        persisted = persist_paper_account(result, output_dir=str(OUT_DIR / name), assumptions=load_assumptions())
        nav = result["nav"]
        curve = (nav[["asof_date", "nav", "drawdown"]]
                 .assign(asof_date=nav["asof_date"].astype(str).str.slice(0, 10)))
        # 只保留从第一个决策日开始的曲线（前面是空仓的平线，会把曲线压扁）
        first_decision = str(pd.to_datetime(sel["trade_date"]).min())[:10]
        curve = curve[curve["asof_date"] >= first_decision]
        curves[name] = curve
        label = LABELS.get(name, name)
        ret = nav["nav"].pct_change().dropna()
        fills = result["fills"]
        traded_value = float((fills["quantity"] * fills["price"]).sum()) if not fills.empty else 0.0
        costs = cost_model()
        fees_paid = float(fills["commission"].sum()) if not fills.empty else 0.0
        slippage_est = traded_value * float(costs["slippage"])
        rows.append({
            "variant": name, "label": label,
            "traded_value": round(traded_value, 2),
            "fees_paid": round(fees_paid, 2),          # 佣金 + 印花税（滑点体现在成交价里）
            "slippage_est": round(slippage_est, 2),
            "cost_total_est": round(fees_paid + slippage_est, 2),
            "cost_bps_of_capital": round((fees_paid + slippage_est) / args.capital * 10_000, 1),
            "decision_dates": int(sel["trade_date"].nunique()),
            "first_decision": first_decision,
            "last_decision": str(pd.to_datetime(sel["trade_date"]).max())[:10],
            "last_nav_date": str(nav["asof_date"].iloc[-1])[:10],
            "final_nav": persisted["final_nav"], "total_return": round(persisted["final_nav"] / args.capital - 1, 4),
            "max_drawdown": persisted["max_drawdown"],
            "ann_vol": round(float(ret.std() * np.sqrt(252)), 4) if len(ret) > 1 else None,
            "fills": int(len(result["fills"])), "orders": int(len(result["orders"])),
            "max_single_weight": round(float(sel.groupby("trade_date")["target_weight"].max().max()), 4),
            "min_names_per_date": int(sel.groupby("trade_date").size().min()),
            "curve": [{"date": r.asof_date, "nav": round(float(r.nav), 2), "drawdown": round(float(r.drawdown), 4)}
                      for r in curve.itertuples()],
        })
        reference = _reference_variant(args.reference)
        if name == reference:
            # 参考账户 = 同一条规则的"一等公民"副本（清单/成交/净值都落在这里，供前端与同花顺导出使用）
            persist_paper_account(result, output_dir=str(REF_DIR), assumptions=load_assumptions())
            (REF_DIR / "variant.json").write_text(
                json.dumps({"variant": name, "label": label, "history": f"output/results_cn/shadow_variants/{name}.csv",
                            "source": "scripts/run_shadow_variants.py"}, ensure_ascii=False, indent=2),
                encoding="utf-8")
            print(f"[shadow] ★ 参考账户已绑定 {name} → {REF_DIR.relative_to(ROOT)}")
        print(f"[shadow] {name:<20} 决策日 {sel['trade_date'].nunique():>2} "
              f"期末 {persisted['final_nav']:>10,.2f} 回撤 {persisted['max_drawdown']:+.2%} "
              f"成交 {len(result['fills'])} 笔")

    reference = _reference_variant(args.reference)
    if reference in histories and not histories[reference].empty:
        write_consistency(reference, histories[reference])
    else:
        print(f"[shadow] 警告：参考变体 {reference} 这次没有历史，跳过一致性核对")

    summary = {
        "reference_variant": reference,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "capital": args.capital, "topn": TOPN, "rank_cap": RANK_CAP,
        "assumptions_version": load_assumptions().get("version"),
        "costs": cost_model(),
        "caveats": ("全部为模拟：T+1 开盘成交、按统一执行假设计成本；decision_dates 少于一二十个不要读结论；"
                    "production 是现行口径，其余是**对照**，不代表推荐。"),
        "variants": rows,
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if rows:
        table = pd.DataFrame([{k: v for k, v in r.items() if k != "curve"} for r in rows])
        print()
        print(table.to_string(index=False))
    print(f"\n[shadow] 产物 → {OUT_DIR}/summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
