#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用 L3 的**最优选股手臂**选出"最后一天"（最新交易日）的票 —— 接入 cn-data-pipeline。

背景：`P1_25 §9/§10/§15~§19` 在组合级验证里发现，`base_v1`（v1 面板 25 列的 IC 加权合成）
在逐笔级回放里是最强的一只手臂（30.3% / Sharpe 1.60 / 回撤 −17.2%），而 LLM sleeve 与各种
止损/择时规则都是负贡献。本脚本把**这只手臂**接到流水线里，按最新交易日给出：
  候选 10 只 → 最终等权买入 5 只（与 `[selection]` 的候选/持仓口径一致），并给出 45,000 元账户下的整手可买数量。

与生产链路的关系：这是**研究手臂**，不替代 `preselection → pk`（那条链路用 LightGBM/Transformer 模型分数）。
产物写到 `output/results_cn/arm_latest_selection.csv`，与生产名单分开存放，便于对照。

用法:
  uv run python scripts/select_latest_via_arm.py                     # 默认最新交易日、base_v1 手臂
  uv run python scripts/select_latest_via_arm.py --arm llm_new --factors a,b,c
  uv run python scripts/select_latest_via_arm.py --as-of 2026-09-30
输出:
  output/results_cn/arm_latest_selection.csv   候选与持仓（带名称/板块/市值分位/手数）
  output/results_cn/arm_latest_selection.json  口径与来源说明
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
sys.path.insert(0, str(ROOT / "scripts"))

PANEL = ROOT / "assets/data/meta/cn_sleeve_features_long_v4.parquet"
OUT = ROOT / "output/results_cn"
CN_BASIC = ROOT / "assets/data/meta/cn_stock_basic.parquet"
BARS = ROOT / "assets/data/clean/ohlcv/market=CN"
CAPITAL_DEFAULT = 45_000.0
BAD_BOARD = ("688", "300", "301", "8", "4", "920", "689")


def latest_snapshot(panel: pd.DataFrame, as_of: str | None) -> tuple[pd.DataFrame, pd.Timestamp]:
    if as_of:
        day = pd.Timestamp(as_of)
        dates = panel.index[panel["trade_date"] == day]
        if len(dates) == 0:
            raise SystemExit(f"{as_of} 不是面板里的交易日")
        return panel[panel["trade_date"] == day].copy(), day
    day = panel["trade_date"].max()
    return panel[panel["trade_date"] == day].copy(), day


def main() -> int:
    ap = argparse.ArgumentParser(description="用 L3 最优手臂选最新一天的票")
    ap.add_argument("--panel", default=str(PANEL))
    ap.add_argument("--snapshot", default="latest", choices=["latest", "month_end"],
                    help="选股基准日：latest=面板最新交易日（**每日**用，默认）；"
                         "month_end=最近一个月末（研究口径，与 IC 权重同频）")
    ap.add_argument("--as-of", default="")
    ap.add_argument("--arm", default="base_v1", choices=["base_v1", "llm_new"])
    ap.add_argument("--factors", default="small_reversal_growth,small_value_cash_quality,small_rev_cash_liquidity,small_cashflow_growth,small_mature_safety")
    ap.add_argument("--candidates", type=int, default=10)
    ap.add_argument("--holdings", type=int, default=5)
    ap.add_argument("--size-max", type=float, default=0.40)
    ap.add_argument("--capital", type=float, default=CAPITAL_DEFAULT)
    ap.add_argument("--train-start", default="2018-01-01")
    ap.add_argument("--st-mode", default="pit", choices=["pit", "current", "off"],
                    help="ST 过滤口径：pit=按 as-of 日期还原当时名称（走 cn_stock_namechange.parquet，"
                         "历史回看正确）；current=按基础表当前名称（非 PIT，只能用于今天选票）；off=不过滤")
    ap.add_argument("--st-anchor", default="start", choices=["start", "ann"],
                    help="PIT 口径下用哪个日期锚定：start=名称生效日（保守、不前视）；ann=公告日（更激进）")
    ap.add_argument("--namechange", default="", help="namechange 区间表路径（留空=默认 meta 路径）")
    ap.add_argument("--robust-months", type=int, default=24,
                    help="权重稳健性对照：另用最近 N 个月拟合一次，报两份 top-N 的重合度")
    args = ap.parse_args()

    import llm_factor_mining as lfm
    import llm_sleeve_oos_backtest as l3
    from factor_oos_deflated import CONTROL_COLS_V1, month_end_snapshot
    from factor_engine.universe_pit import is_risk_name

    panel_path = Path(args.panel)
    merged, fields = lfm.load_panel(args.train_start, str(panel_path))
    # `snap`（月末快照）只用于**拟合 IC 权重**；选股基准日走 `day_snap`。
    # 2026-10-09 修：原来 day_snap 直接取月末快照的最后一个月 —— 于是 10 月上旬跑出来的
    # "最新名单"其实是 **9 月末** 的（实测 trade_date=2026-09-30），前端日历里自然看不到
    # 当天的 arm_select。现在默认按面板最新交易日选，研究口径可用 --snapshot month_end 还原。
    snap = month_end_snapshot(merged)
    months = sorted(snap["month"].unique())

    def _day_frame(day: pd.Timestamp) -> pd.DataFrame:
        """取某一天的面板行，并补上与 month_end_snapshot 同款的 `month` 列
        （`zscore_within` / `monthly_ic` 都按它分组）。"""
        frame = merged[merged["trade_date"] == day].copy()
        frame["month"] = frame["trade_date"].dt.to_period("M").astype(str)
        return frame

    if args.as_of:
        snap_date = pd.Timestamp(args.as_of)
        day_snap = _day_frame(snap_date)
    elif args.snapshot == "month_end":
        day_snap = snap[snap["month"] == months[-1]].copy()
        snap_date = pd.Timestamp(day_snap["trade_date"].iloc[0])
    else:
        snap_date = pd.Timestamp(merged["trade_date"].max())
        day_snap = _day_frame(snap_date)
    if len(day_snap) == 0:
        raise SystemExit(f"面板在 {snap_date:%Y-%m-%d} 没有数据")
    print(f"[arm-select] 面板 {panel_path.name}｜快照日 {snap_date:%Y-%m-%d}｜{len(day_snap)} 只", flush=True)

    field_set = set(fields.tolist())
    codes = day_snap["stock_code"].astype(str)
    # 原策略六层过滤第 2 条：ST 与退市风险股一律排除（保命第一条件）。
    # 注意：基础表只有**当前名称**，不是 PIT —— 历史回看时无法还原"当时是不是 ST"（见 P1_25 §17 遗留）。
    st_names: dict[str, str] = {}
    if CN_BASIC.exists():
        basic = pd.read_parquet(CN_BASIC, columns=["ts_code", "name"]).drop_duplicates("ts_code")
        st_names = dict(zip(basic["ts_code"].astype(str), basic["name"].astype(str)))
    current_mask = codes.map(lambda c: is_risk_name(st_names.get(c, "")))
    pit_names: dict[str, str] = {}
    st_note = ""
    if args.st_mode == "current":
        st_mask = current_mask
        st_note = "ST 过滤按基础表「当前」名称（非 PIT）→ 只能用于选今天；历史回看须改用 --st-mode pit"
        print(f"[arm-select] ST/退市风险剔除 {int(st_mask.sum())} 只（按基础表「当前」名称 → 非 PIT）", flush=True)
    elif args.st_mode == "pit":
        from factor_engine.universe_pit import NAME_CHANGE, load_namechange, risk_asof

        nc_path = Path(args.namechange) if args.namechange else NAME_CHANGE
        nc = load_namechange(nc_path)
        if len(nc) == 0:
            st_mask = current_mask
            st_note = (f"PIT 区间表缺失（{nc_path}）→ 已回退到「当前名称」口径；"
                       f"跑 scripts/backfill_cn_namechange.py 生成后重跑即可 PIT 化")
            print(f"[arm-select] ⚠ 未找到 namechange 区间表 {nc_path} → 回退 current 口径", flush=True)
        else:
            # 注意：codes 的 index 是 merged 的行号，不是股票代码 —— risk_asof 的 index 是 ts_code，
            # 必须按「代码→布尔」的映射回填，直接 reindex 会得到全 NaN（早期版本在这里踩过）。
            res = risk_asof(nc, list(pd.unique(codes)), snap_date, anchor=args.st_anchor)
            st_mask = codes.map(res["is_risk"].to_dict()).fillna(False).astype(bool)
            covered_mask = codes.map(res["covered"].to_dict()).fillna(False).astype(bool)
            pit_names = res["pit_name"].to_dict()
            cov = float(covered_mask.mean())
            n_both = int((st_mask & current_mask).sum())
            only_pit = int((st_mask & ~current_mask).sum())
            only_now = int((~st_mask & current_mask).sum())
            st_note = (f"ST 过滤已 PIT 化：按 {snap_date:%Y-%m-%d} 当日名称判定"
                       f"（anchor={args.st_anchor}，区间表 {nc['ts_code'].nunique()} 只 / 覆盖 {cov:.1%}）")
            print(
                f"[arm-select] ST/退市风险剔除 {int(np.sum(st_mask))} 只（PIT，as-of {snap_date:%Y-%m-%d}）"
                f"｜覆盖率 {cov:.1%}｜与「当前名称」口径比对：两者都剔 {n_both}、"
                f"仅 PIT 剔 {only_pit}、仅当前剔 {only_now}",
                flush=True,
            )
    else:
        st_mask = pd.Series(False, index=codes.index)
        st_note = "未过滤 ST（--st-mode off）"
        print("[arm-select] 未过滤 ST（--st-mode off）", flush=True)
    domain = ~codes.str.startswith(BAD_BOARD)
    domain &= ~st_mask
    domain &= day_snap["listed_days"].fillna(0) >= 365
    domain &= (day_snap["mktcap_rank"] <= args.size_max).fillna(False)
    print(f"[arm-select] 域内候选池 {int(domain.sum())} 只（主板+上市满1年+市值分位≤{args.size_max:.0%}）", flush=True)

    # 注意：day_snap 的 index 是 merged 的行号（百万级），**不是**股票代码 ——
    # 必须用 code_map 把行号映射成 stock_code（第一版直接把行号当代码输出，出现 7058253 这种"代码"）。
    code_map = {idx: str(c) for idx, c in zip(day_snap.index, day_snap["stock_code"])}
    rank_map = dict(zip(day_snap["stock_code"].astype(str), day_snap["mktcap_rank"]))

    if args.arm == "base_v1":
        z = {c: l3.zscore_within(day_snap, c) for c in CONTROL_COLS_V1 if c in field_set}
        ic = pd.DataFrame({c: l3.monthly_ic(snap, c) for c in z}).sort_index()
        # 训练窗口必须按**选股日所在的月**切，而不是按月列表的最后一项切：
        # 日频口径下 months[-1] 是上个月末（9 月），再排除它等于连 9 月的数据都丢了。
        cur_month = str(day_snap["month"].iloc[0])
        train = [m for m in months if m < cur_month]
        w = ic.reindex(train).mean()
        w = w / (w.abs().sum() or 1.0)
        score = sum(z[c] * float(w.get(c, 0.0)) for c in w.index if abs(w[c]) > 1e-9)
        score = pd.Series(score, index=day_snap.index).fillna(0.0)
        source = f"base_v1：v1 面板 25 列 IC 加权合成（权重用 {len(train)} 个历史月拟合）"
    else:
        chosen = [f.strip() for f in args.factors.split(",") if f.strip()]
        z = {}
        for name in chosen:
            col = f"_{name}"
            match = [p for p in json.loads((Path("output/research/llm_factor_mining_20261007_155107/round_1/proposals.json")).read_text(encoding="utf-8"))]
            vals = None
            for rnd in (1, 2):
                path = Path(f"output/research/llm_factor_mining_20261007_155107/round_{rnd}/proposals.json")
                for item in json.loads(path.read_text(encoding="utf-8")):
                    if item.get("name") == name:
                        series, err = lfm.safe_eval(str(item.get("expression", "")), merged, fields)
                        vals = series
            if vals is None:
                raise SystemExit(f"因子 {name} 不在 run 里")
            day_snap[col] = pd.Series(vals).reindex(merged.index).loc[day_snap.index].to_numpy()
            z[col] = l3.zscore_within(day_snap, col)
        ic = pd.DataFrame({c: l3.monthly_ic(snap, c) for c in z}).sort_index()
        cur_month = str(day_snap["month"].iloc[0])
        train = [m for m in months if m < cur_month]
        w = ic.reindex(train).mean()
        icir = ic.reindex(train).apply(lambda s: s.mean() / s.std() if s.std() else 0.0)
        w = (np.sign(w) * icir.abs()).dropna()
        w = w / (w.abs().sum() or 1.0)
        score = sum(z[c] * float(w.get(c, 0.0)) for c in w.index)
        score = pd.Series(score, index=day_snap.index).fillna(0.0)
        source = f"llm_new：{len(w)} 个 LLM 因子（符号+ICIR 加权，用 {len(train)} 个历史月拟合）"

    domain_score = score[domain].dropna()
    top = domain_score.nlargest(min(args.candidates, len(domain_score)))
    held = top.head(min(args.holdings, len(top)))

    # ---- (5) 权重稳健性：另用最近 N 个月重拟一次，比较 top-N 重合度 ----
    robust = {}
    if args.arm == "base_v1" and len(months) > args.robust_months:
        cur_month = str(day_snap["month"].iloc[0])
        train_recent = [m for m in months if m < cur_month][-args.robust_months:]
        w2 = ic.reindex(train_recent).mean()
        w2 = w2 / (w2.abs().sum() or 1.0)
        score2 = sum(z[c] * float(w2.get(c, 0.0)) for c in w2.index if abs(w2[c]) > 1e-9)
        score2 = pd.Series(score2, index=day_snap.index).fillna(0.0)
        top2 = score2[domain].dropna().nlargest(min(args.candidates, len(domain_score)))
        overlap = len(set(top.index) & set(top2.index))
        robust = {"weights_months_full": len(ic.reindex([m for m in months if m < cur_month]).dropna(how="all")),
                  "weights_months_recent": len(train_recent), "top_overlap": overlap,
                  "top_overlap_pct": round(overlap / max(len(top), 1), 3),
                  "top_recent_only": [str(c) for c in [code_map2[i] for i in top2.index]] if (code_map2 := code_map) else []}
        print(f"[arm-select] 权重稳健性：全历史 vs 最近 {len(train_recent)} 个月，top{len(top)} 重合 "
              f"{overlap}/{len(top)}（{robust['top_overlap_pct']:.0%}）", flush=True)

    # 名称 / 板块 / 收盘价 / 整手可买
    names, boards, industries = {}, {}, {}
    if CN_BASIC.exists():
        basic = pd.read_parquet(CN_BASIC, columns=["ts_code", "name", "market", "industry"])
        basic = basic.drop_duplicates("ts_code").set_index("ts_code")
        for c in [code_map[i] for i in top.index]:
            if c in basic.index:
                names[c] = str(basic.at[c, "name"])
                boards[c] = str(basic.at[c, "market"])
                industries[c] = str(basic.at[c, "industry"])
    import pyarrow.dataset as ds
    files = [str(p) for y in (snap_date.year - 1, snap_date.year)
             for p in BARS.glob(f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={y}/*.parquet")]
    bars = ds.dataset(files, format="parquet").to_table(columns=["trade_date", "stock_code", "close"]).to_pandas()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    last_px = (bars[bars["trade_date"] == snap_date].drop_duplicates("stock_code")
               .set_index("stock_code")["close"].to_dict())
    # ---- (3) 下单前预检：当日是否有行情（停牌）+ 是否涨停（买不进）----
    prev_day = bars[bars["trade_date"] < snap_date]["trade_date"].max()
    prev_px = (bars[bars["trade_date"] == prev_day].drop_duplicates("stock_code")
               .set_index("stock_code")["close"].to_dict())
    def _limit_pct(code: str) -> float:
        if code.startswith(("300", "301", "688")):
            return 0.20 if snap_date >= pd.Timestamp("2020-08-24") else 0.10
        return 0.10
    def precheck(code: str) -> tuple[bool, bool]:
        px, prev = last_px.get(code), prev_px.get(code)
        if px is None:
            return False, False                      # 停牌/无行情 → 不可下单
        if prev:
            chg = float(px) / float(prev) - 1
            if chg >= _limit_pct(code) - 0.005:
                return False, True                   # 涨停 → 不追高
        return True, False

    rows = []
    budget = args.capital / max(len(held), 1)
    held_codes = {code_map[i] for i in held.index}
    for label in top.index:
        code = code_map[label]
        px = last_px.get(code)
        tradable, limit_up = precheck(code)
        lots = int(budget / (px * 100)) if (px and tradable) else 0
        rows.append({
            "trade_date": snap_date.strftime("%Y-%m-%d"), "stock_code": code,
            "name": names.get(code, ""), "board": boards.get(code, ""), "industry": industries.get(code, ""),
            "score": round(float(domain_score[label]), 6),
            "rank": int(list(top.index).index(label) + 1),
            "in_portfolio": code in held_codes,
            "target_weight": round(1.0 / len(held), 4) if code in held_codes else 0.0,
            "last_close": px, "tradable": bool(tradable), "limit_up": bool(limit_up),
            "lots_at_capital": lots, "planned_amount": round(lots * (px or 0) * 100, 0),
            "pit_name": pit_names.get(code) or "",
            "mktcap_rank": (None if rank_map.get(str(code)) is None
                            else round(float(rank_map[str(code)]), 4)),
        })
    table = pd.DataFrame(rows)
    OUT.mkdir(parents=True, exist_ok=True)
    csv_path = OUT / "arm_latest_selection.csv"
    table.to_csv(csv_path, index=False)

    # 历史归档：`arm_latest_selection.csv` 每天被覆盖，回顾不了"上周二选的是哪 5 只"。
    # 这里额外存一份按交易日命名的快照（同一天重复跑就覆盖当天那份），
    # 供前端「选股日历」/回看使用（P2_26 §4 P1-1 的延伸）。
    hist_dir = OUT / "arm_selection_history"
    hist_dir.mkdir(parents=True, exist_ok=True)
    hist_path = hist_dir / f"{snap_date:%Y-%m-%d}.csv"
    table.to_csv(hist_path, index=False)
    (OUT / "arm_latest_selection.json").write_text(json.dumps({
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "snapshot_date": snap_date.strftime("%Y-%m-%d"), "arm": args.arm, "source": source,
        "panel": str(panel_path), "domain_size": int(domain.sum()),
        "candidates": args.candidates, "holdings": args.holdings, "capital": args.capital,
        "note": "研究手臂（base_v1/llm_new），**不替代** preselection→pk 的生产名单；对照用",
        "st_mode": args.st_mode,
        "st_anchor": args.st_anchor if args.st_mode == "pit" else None,
        "st_pit_caveat": st_note,
        "pit_columns": "pit_name=该股在快照日的实际证券名称（--st-mode pit 时有值）",
        "order_precheck": "tradable/lots_at_capital 已考虑当日停牌与涨停（涨停不追高 → lots=0）",
        "weight_robustness": robust,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[arm-select] 候选 {len(top)} 只 / 持仓 {len(held)} 只 → {csv_path}"
          f"（归档 {hist_path.relative_to(ROOT)}）", flush=True)
    print(table[["rank", "stock_code", "name", "board", "industry", "score", "last_close",
                 "lots_at_capital", "in_portfolio"]].to_string(index=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
