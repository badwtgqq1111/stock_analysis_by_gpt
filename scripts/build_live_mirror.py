#!/usr/bin/env python3
"""实盘镜像账户（live mirror）：把真实券商持仓接进每日自动化。

用法：
    uv run python scripts/build_live_mirror.py                 # 快照 + 实时价（盘中自动接管）
    uv run python scripts/build_live_mirror.py --no-live       # 只用配置里的快照价（离线复算）
    uv run python scripts/build_live_mirror.py --print         # 顺带打一张终端摘要

产物：
    output/paper_trading/live_mirror/{snapshot.json,positions.csv,nav.csv,summary.json}
    output/results_cn/cn_live_holdings.csv                （→ /api/selection?source=cn_live_holdings）
    output/live_holdings_history/<asof>.csv               （策略池 / 选股日历的名单归档）

为什么不是把持仓写进 output/paper_trading/account/
--------------------------------------------------
那个账户是「策略从空仓 45,000 按当日名单下单」的前向口径。真实持仓是人工决策 + 已实现
亏损，混进去会让曲线出现一次无解释的跳变，之后再也无法和影子变体做同窗口配对。
所以实盘单开一个账户，只共用**同一套执行假设**（config/execution_assumptions_cn.json）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factor_engine.portfolio.live_mirror import (  # noqa: E402
    ADVICE_RULES,
    account_state,
    advice_rows,
    blend_with_benchmark,
    buy_and_hold_curve,
    calendar_cells,
    curve_stats,
    mark_to_market,
    merge_live_prices,
    resample_curve,
)

SNAPSHOT = ROOT / "config/live_account_holdings.json"
OUT_DIR = ROOT / "output/paper_trading/live_mirror"
SELECTION_CSV = ROOT / "output/results_cn/cn_live_holdings.csv"
HISTORY_DIR = ROOT / "output/live_holdings_history"
PREFLIGHT_NOTE = "口径与模拟盘一致（T+1 开盘成交不在本账户使用：镜像是持仓盯市，不重放建仓）"


def load_snapshot(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def live_quotes(codes: list[str]) -> tuple[dict[str, dict], str]:
    """盘中实时价（腾讯批量快照）。失败就返回空 dict + 原因，绝不抛。"""
    try:
        from backend.services import live_quotes as lq

        payload, error = lq.tencent_quotes(codes)
        if not payload:
            return {}, str(error or "无返回")
        return payload, ""
    except Exception as exc:  # 网络/依赖任何一个出问题都不该让镜像账户跑不出来
        return {}, str(exc)


def load_bars(codes: list[str], start: str = "2026-01-01") -> pd.DataFrame:
    import pyarrow.dataset as ds

    files = [str(p) for year in range(int(start[:4]), datetime.now().year + 1)
             for p in (ROOT / "assets/data/clean/ohlcv/market=CN").glob(
                 f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={year}/*.parquet")]
    if not files:
        return pd.DataFrame(columns=["trade_date", "stock_code", "close"])
    frame = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "close"],
        filter=ds.field("stock_code").isin(codes)).to_pandas()
    if frame.empty:
        return frame
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    return frame[frame["trade_date"] >= pd.Timestamp(start)].drop_duplicates(["trade_date", "stock_code"])


def load_benchmark(start: str = "2020-01-01") -> pd.Series:
    """中证500 收盘价（价格指数，不做分红调整 —— 与策略池曲线同一口径）。"""
    path = ROOT / "assets/data/meta/cn_benchmark_daily.parquet"
    if not path.exists():
        return pd.Series(dtype=float)
    frame = pd.read_parquet(path)
    frame = frame[frame["ts_code"] == "000905.SH"].copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.normalize()
    frame = frame[frame["trade_date"] >= pd.Timestamp(start)].sort_values("trade_date")
    return frame.set_index("trade_date")["close"].astype(float)


def momentum_20d(bars: pd.DataFrame) -> dict[str, float]:
    """20 交易日动量（后复权收盘）。样本不足 21 根的票不给值 —— 不留 nan 到前端。"""
    out: dict[str, float] = {}
    if bars.empty:
        return out
    for code, group in bars.sort_values("trade_date").groupby("stock_code"):
        closes = group["close"].dropna().tolist()
        if len(closes) < 21 or not closes[-21]:
            continue
        out[str(code)] = closes[-1] / closes[-21] - 1.0
    return out


def benchmark_20d(bars: pd.DataFrame) -> dict:
    """中证500 同窗（20 交易日）收益 —— 持仓的 20 日动量要有基准才有意义。

    窗口 = 日线数据的最后一天往前 21 根（与 `momentum_20d` 完全同一套算法、同一天），
    这样「组合 20 日 vs 中证500 20 日」是**同窗同口径**，不是拿 20 日去比别的窗口。
    """
    path = ROOT / "assets/data/meta/cn_benchmark_daily.parquet"
    if bars.empty or not path.exists():
        return {}
    frame = pd.read_parquet(path)
    frame = frame[frame["ts_code"] == "000905.SH"].copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    last = bars["trade_date"].max()
    frame = frame[frame["trade_date"] <= last].sort_values("trade_date")
    closes = frame["close"].dropna().tolist()
    if len(closes) < 21 or not closes[-21]:
        return {}
    return {
        "code": "000905.SH",
        "name": "中证500",
        "window_start": str(frame["trade_date"].iloc[-21].date()),
        "window_end": str(frame["trade_date"].iloc[-1].date()),
        "return_20d": closes[-1] / closes[-21] - 1.0,
    }


def latest_picks() -> tuple[dict[str, int], dict[str, int], str]:
    """最新决策日的生产最终名单 / 候选池 → `{代码: rank}`。"""
    def read(path: Path) -> tuple[dict[str, int], str]:
        if not path.exists():
            return {}, ""
        frame = pd.read_csv(path)
        if frame.empty or "stock_code" not in frame.columns:
            return {}, ""
        date = str(frame["trade_date"].max())[:10] if "trade_date" in frame.columns else ""
        ranks: dict[str, int] = {}
        for i, row in enumerate(frame.sort_values("rank").itertuples(), start=1):
            code = str(row.stock_code)
            try:
                ranks[code] = int(row.rank)
            except (AttributeError, TypeError, ValueError):
                ranks[code] = i
        return ranks, date

    selected, date_a = read(ROOT / "output/results_cn/cn_ensemble_selected.csv")
    preselected, date_b = read(ROOT / "output/results_cn/cn_ensemble_preselected.csv")
    return selected, preselected, (date_a or date_b)


def industry_map(codes: list[str]) -> dict[str, str]:
    path = ROOT / "assets/data/meta/cn_stock_basic.parquet"
    if not path.exists():
        return {}
    frame = pd.read_parquet(path)
    frame = frame[frame["ts_code"].astype(str).isin(codes)]
    return {str(r.ts_code): str(r.industry or "") for r in frame.itertuples()}


def selection_rows(state: dict, advice: pd.DataFrame, picks_date: str, industries: dict[str, str]) -> pd.DataFrame:
    """选股页 / 策略池要的名单形态（列名进 selection_sources._COLS 白名单）。"""
    merged = advice.merge(
        state["positions"][["stock_code", "name", "shares", "cost_price", "last_price", "price_source",
                            "pnl_amount", "market_value"]],
        on=["stock_code", "name"], how="left")
    merged["trade_date"] = state["asof"]
    merged["rank"] = range(1, len(merged) + 1)
    # 这里的 rank 是**按持仓市值权重**排的，不是模型排名 —— 明确写出来，
    # 否则逐股 AI 分析会把「排名第 1」读成模型最看好它（实测第一版就这样）。
    merged["rank_kind"] = "按持仓市值权重排序（不是模型排名）"
    merged["stage"] = "live"
    merged["target_weight"] = merged["weight"]
    merged["last_close"] = merged["last_price"]
    merged["industry"] = merged["stock_code"].map(industries).fillna("")
    merged["note"] = merged["reason"]
    merged["picks_date"] = picks_date
    return merged[[
        "rank", "trade_date", "stock_code", "name", "stage", "industry", "shares", "cost_price",
        "last_price", "last_close", "market_value", "target_weight", "pnl_amount", "pnl_pct",
        "action", "rule", "note", "price_source", "selected_rank", "in_selected", "in_preselected",
        "momentum_20d", "picks_date", "rank_kind",
    ]]


def performance_payload(state: dict, *, years: int = 3, freq_stats=("D", "W", "M"),
                        nav: pd.DataFrame | None = None) -> dict:
    """日/周/月 三个粒度的「当前持仓固定权重 vs 中证500」+ 收益日历（用户 2026-10-12 要求）。

    回填口径写在 `notes` 里：**当前权重、买入持有、不含现金/已实现/成本** ——
    它是"这一篮子现在这个结构过去 N 年跟中证500 比怎样"，不是账户业绩（账户业绩见 nav.csv）。
    """
    codes = [str(r.stock_code) for r in state["positions"].itertuples()]
    weights = {str(r.stock_code): float(r.weight) for r in state["positions"].itertuples()}
    # 2026-10-18 修：快照日是 10-15，但 nav.csv 已经用 10-16 收盘价盯市；
    # 对比曲线却停在 10-15（晚一天）。曲线终点跟 nav 的最新日期走。
    end = pd.Timestamp(state["asof"]).normalize()
    if nav is not None and not nav.empty and "asof_date" in nav.columns:
        end = max(end, pd.Timestamp(nav["asof_date"].max()).normalize())
    start = end - pd.DateOffset(years=years)
    bars = load_bars(codes, start=(start - pd.Timedelta(days=10)).strftime("%Y-%m-%d"))
    bench = load_benchmark(start=(start - pd.Timedelta(days=10)).strftime("%Y-%m-%d"))
    curve = blend_with_benchmark(buy_and_hold_curve(weights, bars, start=start, end=end), bench)
    if curve.empty:
        return {"available": False, "reason": "本地日线不足，算不出回填曲线"}
    daily_stats = curve_stats(curve, freq="D")
    windows = {}
    for key, label, offset in (("m1", "近 1 月", pd.DateOffset(months=1)),
                               ("m3", "近 3 月", pd.DateOffset(months=3)),
                               ("y1", "近 1 年", pd.DateOffset(years=1)),
                               ("y3", f"近 {years} 年", pd.DateOffset(years=years))):
        chunk = curve[curve["trade_date"] >= (end - offset)]
        windows[key] = {"label": label, "stats": curve_stats(chunk.reset_index(drop=True), freq="D")}
    cash = float(state.get("cash") or 0.0)
    realized = float(state.get("realized_pnl") or 0.0)
    payload = {
        "available": True,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "asof": str(end.date()),
        "snapshot_asof": str(state["asof"]),
        "window": {"start": daily_stats.get("start"), "end": daily_stats.get("end"), "years": years},
        "benchmark": {"code": "000905.SH", "name": "中证500", "source": "assets/data/meta/cn_benchmark_daily.parquet"},
        "weight_mode": "当前持仓市值权重（固定不漂移）买入持有",
        "weights": weights,
        "coverage_weight": float(curve["coverage_weight"].iloc[0]),
        "notes": [
            f"回填口径：按**当前权重**在窗口起点买入并持有到最新交易日（不含现金 {cash:.2f} 元、"
            f"不含已实现 {realized:+.2f} 元、不含交易成本）。",
            "它回答「这一篮子现在这个结构，过去 N 年跟中证500 比怎样」——**不是**账户业绩；账户业绩只能从快照日向前累积（见 nav.csv）。",
            "中证500 用价格指数（000905.SH），与策略池曲线同一口径；组合用后复权收盘价。",
        ],
        "stats_daily": daily_stats,
        "windows": windows,
    }
    if nav is not None and not nav.empty:
        first, last = nav.iloc[0], nav.iloc[-1]
        payload["account"] = {
            "start": str(pd.Timestamp(first["asof_date"]).date()),
            "end": str(pd.Timestamp(last["asof_date"]).date()),
            "rows": int(len(nav)),
            "cum_return": float(last.get("cum_return") or 0.0),
            "note": "账户实际：从快照日起逐日盯市，含现金与已实现盈亏；样本不足 30 天时仅作参考。",
        }
    for key, freq in (("daily", "D"), ("weekly", "W"), ("monthly", "M")):
        resampled = curve if freq == "D" else resample_curve(curve, freq)
        dates = [str(pd.Timestamp(d).date()) for d in
                 (resampled["trade_date"] if freq == "D" else resampled["period_end"])]
        payload[key] = {
            "dates": dates,
            "port_nav": [round(float(v), 6) for v in resampled["port_nav"]],
            "bench_nav": [round(float(v), 6) for v in resampled["bench_nav"]],
            "port_ret": [round(float(v), 8) for v in resampled["port_ret"]],
            "bench_ret": [round(float(v), 8) for v in resampled["bench_ret"]],
            "excess_ret": [round(float(v), 8) for v in resampled["excess_ret"]],
            "stats": curve_stats(resampled.reset_index(drop=True), freq=freq),
            "calendar": calendar_cells(curve, freq),
        }
    return payload


def build(*, snapshot_path: Path = SNAPSHOT, out_dir: Path = OUT_DIR, use_live: bool = True,
          selection_csv: Path = SELECTION_CSV, history_dir: Path = HISTORY_DIR,
          years: int = 3) -> dict:
    """跑一次镜像账户。`selection_csv` / `history_dir` 可覆盖 —— 测试里指到 tmp_path，
    免得把真实产物覆盖掉（这是本仓库的常规做法：产物路径都要能被注入）。"""
    snapshot = load_snapshot(snapshot_path)
    codes = [str(p["stock_code"]) for p in snapshot["positions"]]
    quote_error = ""
    if use_live:
        quotes, quote_error = live_quotes(codes)
        if quotes:
            snapshot = merge_live_prices(snapshot, quotes)

    state = account_state(snapshot)
    bars = load_bars(codes)
    mom = momentum_20d(bars)
    bench = benchmark_20d(bars)
    selected, preselected, picks_date = latest_picks()
    advice = advice_rows(state["positions"], selected=selected, preselected=preselected, momentum_20d=mom)

    # 盯市：快照日那一行在**日线还没有当天 bar** 时才用实时/快照价；一旦官方收盘价入库，
    # 就用收盘价（否则 10-12 晚上跑每日自动化时，10-12 那行会一直挂着一个盘中价 —— nav.csv 要能长期回看）。
    asof = pd.Timestamp(state["asof"]).normalize()
    have_official = set(bars.loc[bars["trade_date"] == asof, "stock_code"].astype(str)) if not bars.empty else set()
    synthetic = pd.DataFrame([{"trade_date": asof, "stock_code": str(r.stock_code), "close": float(r.last_price)}
                              for r in state["positions"].itertuples()
                              if str(r.stock_code) not in have_official])
    nav_bars = pd.concat([synthetic, bars[bars["trade_date"] >= asof]], ignore_index=True)
    nav = mark_to_market(snapshot, nav_bars)
    if not nav.empty:
        nav.loc[0, "price_source"] = ("close" if have_official
                                      else str(snapshot.get("price_source") or "snapshot"))

    industries = industry_map(codes)
    rows = selection_rows(state, advice, picks_date, industries)

    # 持仓 20 日 vs 中证500 20 日（同窗）：回答「我的持仓最近跑赢还是跑输大盘」。
    relative: dict = {}
    if bench:
        weights = {str(r.stock_code): float(r.weight) for r in state["positions"].itertuples()}
        port_20d = sum(w * mom.get(code, 0.0) for code, w in weights.items())
        covered = sum(w for code, w in weights.items() if code in mom)
        relative = {
            **bench,
            "portfolio_20d": port_20d if covered else None,
            "coverage_weight": covered,
            "excess_20d": (port_20d - bench["return_20d"]) if covered else None,
            "per_position": {code: {"momentum_20d": mom.get(code),
                                    "excess_20d": (mom[code] - bench["return_20d"]) if code in mom else None}
                             for code in weights},
        }

    asof_str = str(state["asof"])
    out_dir.mkdir(parents=True, exist_ok=True)
    history_dir.mkdir(parents=True, exist_ok=True)
    selection_csv.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "asof": asof_str,
        "broker": state["broker"],
        "account_masked": state["account_masked"],
        "captured_at": state["captured_at"],
        "cash": state["cash"],
        "market_value": round(state["market_value"], 2),
        "total_assets": round(state["total_assets"], 2),
        "cost_basis": round(state["cost_basis"], 2),
        "unrealized_pnl": round(state["unrealized_pnl"], 2),
        "realized_pnl": round(state["realized_pnl"], 2),
        "float_pnl_incl_realized": round(state["float_pnl_incl_realized"], 2),
        "initial_capital_implied": round(state["initial_capital_implied"], 2),
        "cumulative_return": state["cumulative_return"],
        "n_positions": state["n_positions"],
        "top1_weight": state["top1_weight"],
        "top3_weight": state["top3_weight"],
        "equity_weight": state["equity_weight"],
        "max_weight_single_allowed": state["max_weight_single_allowed"],
        "advice_counts": advice["action"].value_counts().to_dict(),
        "advice_rules": ADVICE_RULES,
        "nav_rows": int(len(nav)),
        "nav_first": nav["asof_date"].iloc[0] if not nav.empty else None,
        "nav_last": nav["asof_date"].iloc[-1] if not nav.empty else None,
        "nav_price_source": (str(nav["price_source"].iloc[0]) if not nav.empty and "price_source" in nav.columns
                             else None),
        "observations": max(int(len(nav)) - 1, 0),
        "sample_status": "ok" if len(nav) - 1 >= 30 else "short_sample",
        "price_source": str(snapshot.get("price_source") or "snapshot"),
        "live_quotes_error": quote_error,
        "reconciliation": state["reconciliation"],
        "picks_date": picks_date,
        "relative_20d": relative,
        "selected_codes": sorted(selected),
        "preselected_codes": sorted(preselected),
        "notes": [
            PREFLIGHT_NOTE,
            "快照日之前不回填：真实买入日期未知，回填等于编数据；曲线从快照日起逐日累积。",
            "累计收益的分母是推算初始本金 = 持仓成本 + 现金 + |已实现亏损|（已实现亏损已从账户价值扣掉）。",
            "本账户与交易所模拟盘（output/paper_trading/account/）口径相同、标的来源不同，不要直接比大小。",
        ],
    }
    (out_dir / "snapshot.json").write_text(json.dumps({**snapshot, "computed": {
        "cash": state["cash"], "market_value": round(state["market_value"], 2),
        "total_assets": round(state["total_assets"], 2),
        "initial_capital_implied": round(state["initial_capital_implied"], 2),
        "cumulative_return": state["cumulative_return"], "realized_pnl": state["realized_pnl"],
        "unrealized_pnl": round(state["unrealized_pnl"], 2)}}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    # positions.csv 带上建议列：前端一屏就能看到「持仓 + 建议」，不用再去 join 名单文件。
    positions_out = (state["positions"]
                     .assign(asof_date=asof_str)
                     .merge(advice[["stock_code", "action", "rule", "reason", "momentum_20d",
                                    "in_selected", "in_preselected", "selected_rank"]],
                            on="stock_code", how="left"))
    positions_out.to_csv(out_dir / "positions.csv", index=False)
    nav.to_csv(out_dir / "nav.csv", index=False)
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    # 日/周/月 对比曲线 + 收益日历（用户 2026-10-12 要求）：单独一份产物，页面按需取。
    performance = performance_payload(state, years=years, nav=nav)
    (out_dir / "performance.json").write_text(json.dumps(performance, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
    summary["performance_available"] = bool(performance.get("available"))
    if performance.get("available"):
        summary["performance_stats"] = {
            "daily": performance["daily"]["stats"], "weekly": performance["weekly"]["stats"],
            "monthly": performance["monthly"]["stats"], "windows": {
                k: v["stats"] for k, v in performance["windows"].items()},
        }
        (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
    rows.to_csv(selection_csv, index=False)
    rows.to_csv(history_dir / f"{asof_str}.csv", index=False)
    return {"summary": summary, "positions": state["positions"], "advice": advice, "nav": nav,
            "selection_rows": rows, "performance": performance}


def main() -> int:
    ap = argparse.ArgumentParser(description="实盘镜像账户：快照 + 盯市 + 逐票建议")
    ap.add_argument("--snapshot", default=str(SNAPSHOT))
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--no-live", action="store_true", help="不用实时价，只用配置里的快照价")
    ap.add_argument("--print", action="store_true")
    ap.add_argument("--years", type=int, default=3, help="收益对比回填窗口（年，默认 3）")
    args = ap.parse_args()
    result = build(snapshot_path=Path(args.snapshot), out_dir=Path(args.out),
                   use_live=not args.no_live, years=args.years)
    if args.print:
        s = result["summary"]
        print(f"[live-mirror] {s['asof']} {s['broker']} {s['account_masked']} 价源={s['price_source']}")
        print(f"  总资产 {s['total_assets']:.2f} = 市值 {s['market_value']:.2f} + 现金 {s['cash']:.2f}")
        print(f"  成本 {s['cost_basis']:.2f} / 浮盈 {s['unrealized_pnl']:+.2f} / 已实现 {s['realized_pnl']:+.2f}")
        print(f"  推算初始本金 {s['initial_capital_implied']:.2f} -> 累计 {s['cumulative_return']:+.2%}"
              f"（观测 {s['observations']}，{s['sample_status']}）")
        cols = ["stock_code", "name", "weight", "pnl_pct", "momentum_20d", "action", "rule"]
        print(result["advice"][cols].to_string(index=False,
              float_format=lambda v: f"{v:.4f}"))
        print(f"  nav -> {OUT_DIR.relative_to(ROOT)}/nav.csv；名单 -> {SELECTION_CSV.relative_to(ROOT)}")
        perf = result.get("performance") or {}
        if perf.get("available"):
            for key, label in (("daily", "日"), ("weekly", "周"), ("monthly", "月")):
                st = perf[key]["stats"]
                print(f"  {label}粒度：{st['periods']} 期 {st['start']}~{st['end']}｜组合 {st['total_return']:+.2%}"
                      f" vs 中证500 {st['benchmark_total_return']:+.2%} → 超额 {st['excess_return']:+.2%}"
                      f"｜胜率 {st['win_rate']:.0%}｜最大回撤 {st['max_drawdown']:+.2%}"
                      if st.get("win_rate") is not None else "")
            print(f"  收益日历格数：日 {len(perf['daily']['calendar'])} / 周 {len(perf['weekly']['calendar'])}"
                  f" / 月 {len(perf['monthly']['calendar'])} -> {OUT_DIR.relative_to(ROOT)}/performance.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
