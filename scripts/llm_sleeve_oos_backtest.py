#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""L3：把 LLM 因子做成 sleeve，做**组合级** OOS 验证（候选 10 / 买入 5 / 月频 / 含成本）。

为什么还要这一步：`factor_oos_deflated.py` 评的是**因子**（IC/多空组合），而账户里发生的是
"选 10 只候选 → 买 5 只 → 月底调仓 → 扣佣金/印花税/滑点"。因子过门槛 ≠ 组合赚钱，
而且上一轮算出 **PBO=0.603**（挑单一最优有六成概率在样本外掉到中位数以下），
所以这里必须有**组合层面**的证据。

口径（全部可复算，参数化）：
  * 域（PIT 可得）：非 20% 涨跌幅板（剔除 688/300/301/8xx/4xx/920）+ 上市满 1 年 +
    行业内市值分位 ≤ size_max（默认 0.40，即"小市值域"）+ 当月因子与标签齐备；
  * 打分臂：
      base_v1     —— 冻结的 v1 25 列，IC 加权合成（权重只用**扩张窗口**的历史月份拟合）
      llm_new     —— LLM 因子集，符号 + |ICIR| 权重同样只用扩张窗口拟合
      combo_50_50 —— 上面两条 z 分等权混合（不做二次优化，避免再引入拟合）
      random5     —— 域内随机 5 只（种子固定）：检验"有没有超额"的对照
      domain_ew   —— 域内等权全持：小市值域 beta 本身
  * 组合：按分数取前 `--candidates` 只作为候选，最终等权买入前 `--holdings` 只（默认 5），月频调仓；
  * 成本：佣金 2.5bp + 滑点 3bp（买卖双向）+ 印花税 10bp（卖出）→ 单边换手约 21bp 扣减；
  * 收益：用 `fwd20`（20 日前向收益）作为持有期收益；净值逐月复利。
  * 报告 train/test：前 `--train-months` 个月只用于拟合（不进净值），其余为样本外。

用法：
  uv run python scripts/llm_sleeve_oos_backtest.py \
      --run output/research/llm_factor_mining_20261007_155107 \
      --panel assets/data/meta/cn_sleeve_features_long_v2.parquet \
      --factors small_reversal_growth,small_value_cash_quality,small_rev_cash_liquidity,small_cashflow_growth,small_mature_safety
输出：<run>/sleeve_oos/{arms_summary.csv,monthly_returns.csv,folds.csv,holdings_sample.csv,REPORT.md}
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
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import llm_factor_mining as lfm                    # noqa: E402
from factor_oos_deflated import CONTROL_COLS_V1, month_end_snapshot  # noqa: E402

COMMISSION = 2.5e-4
STAMP = 10e-4
SLIPPAGE = 3e-4


def domain_mask(snap: pd.DataFrame, size_max: float, size_col: str = "size_ind_pct",
                st_risk: pd.Series | None = None) -> pd.Series:
    """PIT 可得的小市值域：主板 + 上市满 1 年 + 市值分位 ≤ size_max。

    size_col 默认 `size_ind_pct`（行业内市值分位，覆盖仅 ~36%）；
    面板 v3 起可传 `mktcap_rank`（全市场真市值分位，2018+ 覆盖 82%）→ 域内只数从 ~445 涨到 ~1,170。

    `st_risk`（可选）= **PIT** 的「ST / 退市风险」布尔序列（由 factor_engine.universe_pit 给出），
    传入后域会剔除它 —— 这正是原策略第 2 条过滤，此前回测里完全没做（B2/B13）。
    """
    code = snap["stock_code"].astype(str)
    bad_board = code.str.startswith(("688", "300", "301", "8", "4", "920", "689"))
    listed = snap["listed_days"] if "listed_days" in snap.columns else pd.Series(1e9, index=snap.index)
    if size_col in snap.columns:
        size_ok = snap[size_col] <= size_max
    else:
        size_ok = pd.Series(True, index=snap.index)
    keep = (~bad_board) & (listed >= 365) & size_ok.fillna(False)
    if st_risk is not None:
        keep = keep & ~st_risk.reindex(snap.index).fillna(False).astype(bool)
    return keep


def zscore_within(snap: pd.DataFrame, col: str) -> pd.Series:
    z = snap.groupby("month")[col].rank(pct=True) - 0.5
    sd = z.groupby(snap["month"]).transform("std")
    return ((z / sd.replace(0, np.nan))).fillna(0.0)


def monthly_ic(snap: pd.DataFrame, col: str) -> pd.Series:
    out = {}
    for month, g in snap.groupby("month"):
        if len(g) < 30:
            continue
        ic = g[col].corr(g["fwd20"], method="spearman")
        if np.isfinite(ic):
            out[month] = float(ic)
    return pd.Series(out)


def pick_weights(score: pd.Series, domain: pd.Series, holdings: int, candidates: int,
                 codes: pd.Series) -> pd.Series:
    """域内按分数取候选 → 前 holdings 只等权。返回 weight Series（**index=stock_code**）。

    必须用 stock_code 做键：snap 的行索引每月不同，用行索引记账会把"同一只股票"当成新股票，
    每月被误判成 100% 换仓（实测把换手算成 2.0/月、成本高估一倍以上）。
    """
    eligible = score[domain].dropna()
    if eligible.empty:
        return pd.Series(dtype=float)
    top = eligible.nlargest(min(candidates, len(eligible))).nlargest(min(holdings, len(eligible)))
    if not len(top):
        return pd.Series(dtype=float)
    return pd.Series(1.0 / len(top), index=codes.loc[top.index].to_numpy())


def portfolio_returns(picks: dict[str, pd.Series], snap: pd.DataFrame) -> pd.DataFrame:
    """按权重序列算月度扣费收益。picks: month → weight Series(index=stock_code)。"""
    months = sorted(picks)
    ret_by_month = {m: g.set_index("stock_code")["fwd20"] for m, g in snap.groupby("month")}
    rows, prev = [], {}
    for i, month in enumerate(months):
        w = picks[month]
        if w.empty:
            continue
        ret = ret_by_month[month].reindex(w.index)
        gross = float((w * ret.fillna(0.0)).sum())
        new = dict(w)
        names = set(prev) | set(new)
        buy = sum(max(new.get(k, 0.0) - prev.get(k, 0.0), 0.0) for k in names)
        sell = sum(max(prev.get(k, 0.0) - new.get(k, 0.0), 0.0) for k in names)
        cost = buy * (COMMISSION + SLIPPAGE) + sell * (COMMISSION + SLIPPAGE + STAMP)
        rows.append({"month": month, "gross": gross, "cost": cost, "net": gross - cost,
                     "one_way_turnover": buy, "turnover": buy + sell, "n_holdings": int(len(w)),
                     "n_names_out": int(len(set(prev) - set(new)))})
        prev = new
    return pd.DataFrame(rows).set_index("month")


def load_daily_returns(start: str, panel: str | None = None) -> pd.DataFrame:
    """日收益矩阵（含退市股）：clean qfq 日线 ∪ 退市表。

    为什么需要它：月频 `fwd20` 口径**结构性看不见退市末段**（退市前最后 20 个交易日没有前瞻收益），
    逐日盯市才能把这段暴跌算进组合曲线（见 P1_25 §14 / 结论台账 A11、B9）。
    """
    import pyarrow.dataset as ds
    years = range(int(start[:4]), 2027)
    files = [str(p) for y in years
             for p in (ROOT / "assets/data/clean/ohlcv/market=CN").glob(
                 f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={y}/*.parquet")]
    bars = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "close"]).to_pandas()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    bars = bars[bars["trade_date"] >= pd.Timestamp(start)]
    dl_path = ROOT / "assets/data/meta/cn_delisted_daily.parquet"
    if dl_path.exists():
        dl = pd.read_parquet(dl_path, columns=["stock_code", "trade_date", "close"])
        dl["trade_date"] = pd.to_datetime(dl["trade_date"])
        dl = dl[dl["trade_date"] >= pd.Timestamp(start)]
        bars = pd.concat([bars, dl], ignore_index=True)
    bars = bars.drop_duplicates(["trade_date", "stock_code"], keep="last")
    px = bars.pivot_table(index="trade_date", columns="stock_code", values="close", aggfunc="last").sort_index()
    return px.pct_change()


def daily_portfolio_returns(picks: dict[str, pd.Series], daily_ret: pd.DataFrame,
                            decision_dates: dict[str, pd.Timestamp] | None = None,
                            cost_bp: float = 21.0) -> pd.Series:
    """按月度持仓、**逐日盯市**结算：每个月的持仓从**该月末决策日**持有到下一个决策日。

    ⚠️ 实测坑（2026-10-07）：第一版把窗口取成"月份第一天 → 下月第一天"，而持仓是**月末快照**选出来的，
    等于把持仓整体挪前一个月 —— 同一组票出现"日频 −5.3% vs fwd20 +22.5%"的荒谬对比。
    修正：窗口起点 = 该月的**决策日**（月末快照的 trade_date），与月频 fwd20 起算点一致。

    缺失行情（停牌/退市后）按 0 收益处理（即"按最后价格持有"），报告里注明这一假设。
    """
    months = sorted(picks)
    out = []
    prev: dict[str, float] = {}
    fallback = {m: pd.Timestamp(m + "-01") for m in months}
    dd = decision_dates or fallback
    for i, month in enumerate(months):
        w = picks[month]
        if w.empty:
            continue
        start_ts = dd.get(month, fallback[month])
        start = daily_ret.index[daily_ret.index >= start_ts]
        if len(start) == 0:
            continue
        end_ts = dd.get(months[i + 1], None) if i + 1 < len(months) else None
        end = end_ts if end_ts is not None else daily_ret.index[-1]
        window = daily_ret.loc[(daily_ret.index >= start[0]) & (daily_ret.index < end)]
        if window.empty:
            continue
        codes = [c for c in w.index if c in window.columns]
        if not codes:
            continue
        weights = w.loc[codes] / w.loc[codes].sum()
        port = (window[codes].fillna(0.0) * weights).sum(axis=1)
        # 换手成本在调仓日扣一次（与月频口径一致：单边换手 × 21bp）
        new = dict(weights)
        names = set(prev) | set(new)
        buy = sum(max(new.get(k, 0.0) - prev.get(k, 0.0), 0.0) for k in names)
        sell = sum(max(prev.get(k, 0.0) - new.get(k, 0.0), 0.0) for k in names)
        cost = buy * (COMMISSION + SLIPPAGE) + sell * (COMMISSION + SLIPPAGE + STAMP)
        port.iloc[0] = port.iloc[0] - cost
        out.append(port)
        prev = new
    return pd.concat(out).sort_index() if out else pd.Series(dtype=float)


def daily_metrics(r: pd.Series) -> dict:
    r = r.dropna()
    if len(r) < 30:
        return {}
    nav = (1 + r).cumprod()
    dd = float((nav / nav.cummax() - 1).min())
    ann = float(nav.iloc[-1] ** (252 / len(r)) - 1)
    vol = float(r.std() * np.sqrt(252))
    return {"days": int(len(r)), "ann_return": ann, "ann_vol": vol,
            "sharpe": float(r.mean() / r.std() * np.sqrt(252)) if r.std() else np.nan,
            "max_drawdown": dd, "calmar": float(ann / abs(dd)) if dd < 0 else np.nan}


def metrics(net: pd.Series) -> dict:
    r = net.dropna()
    if len(r) < 6:
        return {}
    nav = (1 + r).cumprod()
    dd = float((nav / nav.cummax() - 1).min())
    ann = float(nav.iloc[-1] ** (12 / len(r)) - 1)
    vol = float(r.std(ddof=1) * np.sqrt(12))
    sharpe = float(r.mean() / r.std(ddof=1) * np.sqrt(12)) if r.std(ddof=1) > 0 else np.nan
    return {"months": int(len(r)), "total_return": float(nav.iloc[-1] - 1), "ann_return": ann,
            "ann_vol": vol, "sharpe": sharpe, "max_drawdown": dd,
            "calmar": float(ann / abs(dd)) if dd < 0 else np.nan,
            "win_rate": float((r > 0).mean()), "avg_month": float(r.mean())}


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM 因子的 sleeve 组合级 OOS 回测")
    ap.add_argument("--run", required=True)
    ap.add_argument("--panel", default="assets/data/meta/cn_sleeve_features_long_v2.parquet")
    ap.add_argument("--factors", default="",
                    help="逗号分隔的因子名；留空=该 run 的 factors_best.csv 全部")
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--size-max", type=float, default=0.40)
    ap.add_argument("--size-col", default="size_ind_pct",
                    help="市值口径列：size_ind_pct（行业内分位，v2）/ mktcap_rank（真市值全市场分位，v3）")
    ap.add_argument("--candidates", type=int, default=10)
    ap.add_argument("--holdings", type=int, default=5)
    ap.add_argument("--train-months", type=int, default=24)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--dump-picks", action="store_true",
                    help="把每个月的持仓写成 picks_<arm>.csv，供逐笔级回放脚本消费")
    ap.add_argument("--mark-daily", action="store_true",
                    help="额外按日频逐日盯市结算（能捕捉退市末段；月频 fwd20 看不见）")
    ap.add_argument("--st-filter", default="none", choices=["none", "pit", "current"],
                    help="域内 ST/退市风险过滤口径：none=不过滤（历史默认）；"
                         "pit=按 as-of 日期还原当时名称（cn_stock_namechange）；"
                         "current=按基础表当前名称（非 PIT，仅作对照）")
    args = ap.parse_args()

    run_dir = Path(args.run)
    out_dir = Path(args.out_dir) if args.out_dir else run_dir / "sleeve_oos"
    out_dir.mkdir(parents=True, exist_ok=True)

    merged, fields = lfm.load_panel(args.start, args.panel)
    snap = month_end_snapshot(merged)
    months = sorted(snap["month"].unique())
    print(f"[l3] 面板 {len(merged):,} 行 / 月末快照 {len(months)} 个月；"
          f"域：{args.size_col} ≤ {args.size_max}", flush=True)

    # 因子表达式
    proposals = []
    for p in sorted(run_dir.glob("round_*/proposals.json")):
        proposals.extend(json.loads(p.read_text(encoding="utf-8")))
    wanted = [f.strip() for f in args.factors.split(",") if f.strip()]
    if not wanted:
        best = run_dir / "factors_best.csv"
        wanted = list(pd.read_csv(best)["name"].astype(str)) if best.exists() else []
    chosen = [p for p in proposals if str(p.get("name")) in set(wanted)]
    print(f"[l3] LLM 因子 {len(chosen)} 个：{[p['name'] for p in chosen]}", flush=True)

    for p in chosen:
        vals, err = lfm.safe_eval(str(p["expression"]), merged, fields)
        p["_ok"] = vals is not None
        if vals is not None:
            snap[p["name"]] = pd.Series(vals).reindex(merged.index).loc[snap.index].to_numpy()

    # ---- ST / 退市风险过滤（原策略第 2 条；此前回测完全没做 → B2/B13）----
    st_risk = None
    if args.st_filter != "none":
        from factor_engine.universe_pit import is_risk_name, load_namechange, risk_for_frame

        if args.st_filter == "pit":
            nc = load_namechange()
            if len(nc) == 0:
                raise SystemExit("--st-filter pit 需要 assets/data/meta/cn_stock_namechange.parquet"
                                 "（先跑 scripts/backfill_cn_namechange.py）")
            res = risk_for_frame(nc, snap, code_col="stock_code", date_col="trade_date")
            st_risk = res["is_risk"]
            print(f"[l3] ST(PIT) 过滤：月末快照 {len(snap)} 行中剔除 {int(st_risk.sum())} 只"
                  f"｜名称覆盖率 {float(res['covered'].mean()):.1%}", flush=True)
        else:
            basic_path = Path("assets/data/meta/cn_stock_basic.parquet")
            basic = (pd.read_parquet(basic_path, columns=["ts_code", "name"])
                     .drop_duplicates("ts_code").set_index("ts_code")["name"].to_dict())
            st_risk = snap["stock_code"].astype(str).map(lambda c: is_risk_name(basic.get(c, "")))
            print(f"[l3] ST(current，非 PIT) 过滤：剔除 {int(st_risk.sum())} 只", flush=True)

    domain = domain_mask(snap, args.size_max, args.size_col, st_risk=st_risk)
    snap["_domain"] = domain
    print(f"[l3] 域内平均每月 {int(domain.groupby(snap['month']).sum().mean())} 只"
          f"（st_filter={args.st_filter}）", flush=True)

    # 每月 z 分（域内）
    zcols = {c: zscore_within(snap, c) for c in list(fields)}
    zllm = {p["name"]: zscore_within(snap, p["name"]) for p in chosen if p["_ok"]}
    # 注意：lfm.load_panel 返回的 fields 是 pd.Series（索引是 0..n-1），
    # `c in fields` 会退化成"按索引查"→ 恒为 False，导致这里静默取空、base_v1 分数全零
    # （实测症状：llm_new 与 combo_50_50 每月收益完全相同）。必须用集合判断。
    field_set = set(fields.tolist() if hasattr(fields, "tolist") else list(fields))
    ic_v1 = pd.DataFrame({c: monthly_ic(snap, c) for c in CONTROL_COLS_V1 if c in field_set}).sort_index()
    ic_llm = pd.DataFrame({p["name"]: monthly_ic(snap, p["name"]) for p in chosen if p["_ok"]}).sort_index()

    rng = np.random.default_rng(args.seed)
    arms: dict[str, dict[str, pd.Series]] = {a: {} for a in
                                             ("base_v1", "llm_new", "combo_50_50", "random5", "domain_ew")}
    for i, month in enumerate(months):
        train = months[:i]
        g = snap[snap["month"] == month]
        dom = g["_domain"]
        if dom.sum() < args.holdings:
            continue
        # 随机与域内等权不需要拟合
        idx = g.index[dom]
        rng_choice = rng.choice(idx.to_numpy(), size=min(args.holdings, len(idx)), replace=False)
        codes = g["stock_code"].astype(str)
        arms["random5"][month] = pd.Series(1.0 / len(rng_choice), index=codes.loc[rng_choice].to_numpy())
        arms["domain_ew"][month] = pd.Series(1.0 / len(idx), index=codes.loc[idx].to_numpy())
        if len(train) < max(args.train_months, 12):
            continue
        w1 = ic_v1.reindex(train).mean()
        w1 = w1 / (w1.abs().sum() or 1.0)
        score1 = sum(zcols[c].loc[g.index] * float(w1.get(c, 0.0)) for c in w1.index if abs(w1[c]) > 1e-9)
        score1 = pd.Series(score1, index=g.index).fillna(0.0)
        arms["base_v1"][month] = pick_weights(score1, dom, args.holdings, args.candidates, codes)
        if zllm:
            w2 = ic_llm.reindex(train).mean()
            icir = ic_llm.reindex(train).apply(lambda s: s.mean() / s.std() if s.std() else 0.0)
            w2 = (np.sign(w2) * icir.abs()).dropna()
            w2 = w2 / (w2.abs().sum() or 1.0)
            score2 = sum(zllm[c].loc[g.index] * float(w2.get(c, 0.0)) for c in w2.index if abs(w2[c]) > 1e-9)
            score2 = pd.Series(score2, index=g.index).fillna(0.0)
            arms["llm_new"][month] = pick_weights(score2, dom, args.holdings, args.candidates, codes)
            z1 = zscore_within(g.assign(_s1=score1.to_numpy()), "_s1")
            z2 = zscore_within(g.assign(_s2=score2.to_numpy()), "_s2")
            combo = pd.Series((z1 + z2).to_numpy(), index=g.index)
            arms["combo_50_50"][month] = pick_weights(combo, dom, args.holdings, args.candidates, codes)

    monthly, summary, folds = {}, [], []
    test_months = months[max(args.train_months, 12):]
    for arm, picks in arms.items():
        if not picks:
            continue
        df = portfolio_returns(picks, snap)
        monthly[arm] = df
        test = df.loc[[m for m in df.index if m in set(test_months)], "net"]
        full = df["net"]
        summary.append({"arm": arm, **{f"test_{k}": v for k, v in metrics(test).items()},
                        **{f"full_{k}": v for k, v in metrics(full).items()},
                        "avg_one_way_turnover": float(df["one_way_turnover"].mean()),
                        "avg_names_replaced": float(df["n_names_out"].mean()),
                        "avg_cost_per_month": float(df["cost"].mean())})
        # 分折（把样本外月份切 3 段）
        for k, block in enumerate(np.array_split(np.array(test_months), 3), start=1):
            seg = df.loc[[m for m in df.index if m in set(block)], "net"]
            m = metrics(seg)
            if m:
                folds.append({"arm": arm, "fold": k, **m})

    if args.dump_picks:
        rows = []
        for arm, picks_for_arm in arms.items():
            for month, w in picks_for_arm.items():
                for code, weight in w.items():
                    rows.append({"arm": arm, "month": month, "stock_code": code, "weight": float(weight)})
        if rows:
            pd.DataFrame(rows).to_csv(out_dir / "picks.csv", index=False)
            print(f"[l3] 已导出持仓 {len(rows):,} 行 → {out_dir / 'picks.csv'}", flush=True)

    daily_summary = []
    if args.mark_daily:
        daily = load_daily_returns(args.start)
        print(f"[l3] 日收益矩阵 {daily.shape[0]} 天 × {daily.shape[1]} 只（含退市股）", flush=True)
        # 决策日：月末快照的最后一个交易日（与 fwd20 起算点一致）
        decision_dates = {m: pd.Timestamp(str(g["trade_date"].max()))
                          for m, g in snap.groupby("month")}
        for arm, picks_for_arm in arms.items():
            if not picks_for_arm:
                continue
            daily_r = daily_portfolio_returns(picks_for_arm, daily, decision_dates)
            m = daily_metrics(daily_r)
            if m:
                daily_summary.append({"arm": arm, **{f"daily_{k}": v for k, v in m.items()}})
        if daily_summary:
            ds_df = pd.DataFrame(daily_summary)
            print(ds_df.to_string(index=False), flush=True)
            ds_df.to_csv(out_dir / "arms_summary_daily.csv", index=False)

    summ = pd.DataFrame(summary).sort_values("test_sharpe", ascending=False)
    if daily_summary:
        summ = summ.merge(pd.DataFrame(daily_summary), on="arm", how="left")
    pd.concat({k: v for k, v in monthly.items()}, names=["arm"]).to_csv(out_dir / "monthly_returns.csv")
    summ.to_csv(out_dir / "arms_summary.csv", index=False)
    pd.DataFrame(folds).to_csv(out_dir / "folds.csv", index=False)
    (out_dir / "config.json").write_text(json.dumps(vars(args), ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# L3 组合级 OOS 回测（候选 10 / 买入 5 / 月频 / 含成本）", "",
             f"生成 {datetime.now():%Y-%m-%d %H:%M} | run `{run_dir}` | 域 size_ind_pct≤{args.size_max}",
             f"LLM 因子：{', '.join(p['name'] for p in chosen if p['_ok'])}", "",
             f"样本外从 {test_months[0]} 起（前 {max(args.train_months,12)} 个月仅用于拟合权重/符号）；"
             f"成本=佣金{COMMISSION*1e4:.1f}bp+滑点{SLIPPAGE*1e4:.1f}bp(双向)+印花{STAMP*1e4:.1f}bp(卖出)", "",
             "| 臂 | 样本外月数 | 样本外年化 | Sharpe | 最大回撤 | Calmar | 月胜率 | 全样本年化 | 全样本回撤 | 单边换手/月 | 月均换名 |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for _, r in summ.iterrows():
        lines.append(f"| {r['arm']} | {r.get('test_months', 0)} | {r.get('test_ann_return', float('nan')):.2%} | "
                     f"{r.get('test_sharpe', float('nan')):.2f} | {r.get('test_max_drawdown', float('nan')):.2%} | "
                     f"{r.get('test_calmar', float('nan')):.2f} | {r.get('test_win_rate', float('nan')):.1%} | "
                     f"{r.get('full_ann_return', float('nan')):.2%} | {r.get('full_max_drawdown', float('nan')):.2%} | "
                     f"{r.get('avg_one_way_turnover', float('nan')):.2f} | {r.get('avg_names_replaced', float('nan')):.1f} |")
    lines += ["", "## 分折（样本外切 3 段）", "", "| 臂 | 折 | 月数 | 年化 | Sharpe | 最大回撤 |", "|---|---|---|---|---|---|"]
    for row in folds:
        lines.append(f"| {row['arm']} | {row['fold']} | {row['months']} | {row['ann_return']:.2%} | "
                     f"{row['sharpe']:.2f} | {row['max_drawdown']:.2%} |")
    lines += ["", "## 保留意见", "",
              "1. 域里 `size_ind_pct` 早期覆盖只有 ~30%（行业分类齐全才有值），域会偏小；"
              "`scripts/backfill_cn_daily_basic_relay.py` 正在补真实市值（月末交易日优先）。",
              "2. 没有停牌/涨跌停/ST 的 PIT 标记：停牌日因缺行被隐含排除，ST 未过滤。",
              "3. 收益用 20 日前向收益近似月度持有期；除权除息由 qfq 价格处理。",
              "4. 权重只做等权，没有组合优化；`combo_50_50` 是固定 50/50，不是最优混合。", ""]
    (out_dir / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(summ[["arm", "test_months", "test_ann_return", "test_sharpe", "test_max_drawdown", "avg_one_way_turnover", "avg_names_replaced"]]
          .to_string(index=False), flush=True)
    print(f"[l3] 输出 {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
