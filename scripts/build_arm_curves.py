#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给「研究手臂」补**和=中证500 对比的独立回测曲线**（用户 2026-10-15：除了实盘镜像，都应该有对比中证500的收益率回测曲线）。

两条手臂、两种来源，都复用**同一套净值算法** `build_strategy_pool_performance.ledger_curve`
（决策日次日开盘调仓、持有到下一个决策日、分段持有逐段链接），避免"两套净值算法"：

  A. arm_select（base_v1 研究手臂，小市值/IC 加权合成）
     来源：**已有**的 walk-forward 回放产物 `output/research/llm_factor_mining_*/sleeve_oos_v4d/picks.csv`
     （arm=base_v1、每月 5 只等权、2020-01~2026-09，81 个月）—— 不重跑模型，直接用它的逐月名单。
     交叉校验：本脚本算出的日度指标应与该目录 `arms_summary_daily.csv` 的 base_v1 行接近
     （ann 22.9% / 夏普 0.87 / 回撤 −37.6%，那是同一份回放的独立口径）。

  B. arm_concept（概念/板块轮动手臂）
     来源：现跑 `select_concept_arm.run()` 逐月回放（口径写死在该脚本头部：板块层概念动量×资金流−涨停密度，
     个股层反转×资金流×流动性，ST/流动性/非涨停过滤）—— 因为原 `--backtest` 只写汇总、不留逐月序列。

产物（每条手臂一份，登记表 curve_source 指向 nav_engine.csv）：
  output/research/arm_base_v1/curve/{nav_engine.csv,curve.json}        + REPORT.md
  output/research/arm_concept_rotation/curve/{nav_engine.csv,curve.json} + REPORT.md

用法：
  uv run python scripts/build_arm_curves.py --arm-select          # 只做 A（秒级）
  uv run python scripts/build_arm_curves.py --arm-concept --months 60   # 做 B（约 4s/月）
  uv run python scripts/build_arm_curves.py --all
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BARS = ROOT / "assets/data/clean/ohlcv/market=CN"
BENCH = ROOT / "assets/data/meta/cn_benchmark_daily.parquet"
PICKS = ROOT / "output/research/llm_factor_mining_20261007_155107/sleeve_oos_v4d/picks.csv"


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def bench_series() -> pd.Series:
    raw = pd.read_parquet(BENCH)
    raw = raw[raw.ts_code == "000905.SH"].copy()
    raw["trade_date"] = pd.to_datetime(raw["trade_date"])
    return raw.set_index("trade_date")["close"].sort_index()


def bars_for(codes: list[str]) -> pd.DataFrame:
    files = [str(p) for p in BARS.glob("exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet")]
    table = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "open", "close"],
        filter=ds.field("stock_code").isin(codes)).to_pandas()
    table["trade_date"] = pd.to_datetime(table["trade_date"])
    return table.drop_duplicates(["trade_date", "stock_code"])


def run_replay_engine(picks_csv: Path, arm: str, out_dir: Path) -> dict:
    """调用仓库**官方日度执行回放引擎**（停牌/涨跌停/整手/退市/真实费用都建模）。

    为什么不自研：`scripts/replay_daily_execution.py` 已经把「月末选股 → 真实日度持仓与成交」的
    执行假设显式建模（佣金 2.5bp + 滑点 3bp 双向 + 印花税 10bp、100 股整手、涨停不追/跌停不砸、
    连续 20 日无行情按最后价清算），是自己写一遍只会写歪的那类东西（P1_25 §542 记过：月频口径
    系统性高估收益、低估回撤 ⇒ 必须用这一版）。
    """
    import subprocess

    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([sys.executable, str(ROOT / "scripts/replay_daily_execution.py"),
                           "--picks", str(picks_csv), "--arm", arm, "--out-dir", str(out_dir)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"replay_daily_execution 失败：{proc.stderr[-400:]}")
    return json.loads((out_dir / f"{arm}_metrics.json").read_text(encoding="utf-8"))


def to_pool_curve(nav_csv: Path, out_dir: Path, meta: dict, bench: pd.Series) -> dict:
    """把引擎的 `<arm>_nav.csv`（date,nav,...）转成池子认的 nav_engine.csv（asof_date,nav,csi500,n_picks）。"""
    frame = pd.read_csv(nav_csv, parse_dates=["date"]).set_index("date").sort_index()
    b = bench.reindex(frame.index).ffill()
    b = b.dropna()
    frame = frame.reindex(b.index)
    nav = frame["nav"] / frame["nav"].iloc[0]
    bnav = b / b.iloc[0]
    out_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"asof_date": [str(d.date()) for d in nav.index], "nav": nav.to_numpy(),
                  "csi500": bnav.to_numpy(),
                  "n_picks": frame.get("n_pos", pd.Series(0, index=frame.index)).fillna(0).astype(int).to_numpy()}
                 ).to_csv(out_dir / "nav_engine.csv", index=False)
    meta = {**meta, "metrics": metrics(nav), "csi500": metrics(bnav),
            "excess_return": round(float(nav.iloc[-1] / bnav.iloc[-1] - 1), 4),
            "window": f"{nav.index[0].date()} ~ {nav.index[-1].date()}"}
    (out_dir / "curve.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def write_curve(out_dir: Path, nav: pd.Series, bnav: pd.Series, meta: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"asof_date": [str(d.date()) for d in nav.index], "nav": nav.to_numpy(),
                  "csi500": bnav.to_numpy(), "n_picks": [0] * len(nav)}).to_csv(out_dir / "nav_engine.csv", index=False)
    (out_dir / "curve.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def metrics(nav: pd.Series) -> dict:
    if nav.empty:
        return {}
    ret = nav.pct_change().dropna()
    total = float(nav.iloc[-1] - 1)
    years = max(len(nav) / 244.0, 1e-9)
    ann = float((1 + total) ** (1 / years) - 1) if total > -1 else -1.0
    dd = float((nav / nav.cummax() - 1).min())
    vol = float(ret.std() * np.sqrt(244))
    return {"days": int(len(nav)), "total_return": round(total, 4), "annual_return": round(ann, 4),
            "max_drawdown": round(dd, 4), "sharpe": round(ann / vol, 3) if vol > 0 else None,
            "calmar": round(ann / abs(dd), 3) if dd < 0 else None}


def build_arm_select(print_log: bool) -> dict:
    """arm_select（base_v1 研究手臂）：用**已有**的 walk-forward 名单喂官方日度执行回放引擎。"""
    picks = pd.read_csv(PICKS)
    picks = picks[picks["arm"] == "base_v1"].copy()
    if picks.empty:
        raise SystemExit(f"没有 base_v1 的 walk-forward 名单：{PICKS}")
    engine_metrics = run_replay_engine(PICKS, "base_v1", ROOT / "output/research/l3_replay")
    meta = {"strategy_id": "arm_select", "curve_kind": "arm_walkforward_daily_exec",
            "months": int(picks["month"].nunique()),
            "engine": "scripts/replay_daily_execution.py（官方日度执行回放：停牌/涨跌停/整手/退市/真实费用）",
            "engine_metrics": {k: engine_metrics.get(k) for k in
                               ("ann_return", "ann_vol", "sharpe", "max_drawdown", "calmar", "days", "trades")},
            "source": "output/research/llm_factor_mining_20261007_155107/sleeve_oos_v4d/picks.csv（arm=base_v1，逐月 5 只等权）",
            "methodology": "月末选股 → 次日开盘买入（100 股整手）→ 每日盯市 → 下月末换仓；"
                           "佣金 2.5bp + 滑点 3bp 双向 + 印花税 10bp（卖出）；涨停不追/跌停不砸/停牌不可成交/退市按最后价清算。"}
    out = ROOT / "output/research/arm_base_v1/curve"
    meta = to_pool_curve(ROOT / "output/research/l3_replay/base_v1_nav.csv", out, meta, bench_series())
    (out.parent / "REPORT.md").write_text(
        f"# arm_select（base_v1 研究手臂）日度执行回放曲线\n\n"
        f"- 窗口：{meta['window']}（{meta['months']} 个月度决策日，逐月 5 只等权）\n"
        f"- 来源：{meta['source']}\n- 引擎：{meta['engine']}\n"
        f"- 净值：`{out.relative_to(ROOT)}/nav_engine.csv`（口径：{meta['methodology']}）\n"
        f"- 指标：累计 {meta['metrics']['total_return']*100:+.2f}%、年化 {meta['metrics']['annual_return']*100:+.2f}%、"
        f"最大回撤 {meta['metrics']['max_drawdown']*100:.2f}%、夏普 {meta['metrics']['sharpe']}；"
        f"中证500 同窗 累计 {meta['csi500']['total_return']*100:+.2f}% ⇒ 相对 {meta['excess_return']*100:+.2f}%\n", encoding="utf-8")
    if print_log:
        print(f"[arm_select] {meta['window']} 决策日 {meta['months']}｜引擎口径 年化 {meta['engine_metrics']['ann_return']*100:+.2f}% "
              f"回撤 {meta['engine_metrics']['max_drawdown']*100:.2f}% 夏普 {meta['engine_metrics']['sharpe']:.2f}｜"
              f"中证500 相对 {meta['excess_return']*100:+.2f}%", flush=True)
    return meta


def build_arm_concept(months: int, print_log: bool) -> dict:
    """arm_concept（概念/板块轮动手臂）：逐月跑 `select_concept_arm.run()` 取持仓 → dump picks → 走同一个官方引擎。"""
    sca = _load("scripts/select_concept_arm.py", "sca_under_curve")
    bench = bench_series()
    calendar = pd.DatetimeIndex(bench.index)
    # ⚠️ 月末交易日 = 每个月**最后一个**交易日。第一版写成"加 7 天跨月"（= 每个月最后一周的 5 天），
    # 结果 `[-months:]` 只覆盖 5 个月（实测：60 个月参数只回放出 5 个月 ⇒ 曲线窗口被截短）。
    _cal = pd.Series(calendar)
    month_ends = list(_cal.groupby(_cal.dt.to_period("M")).max())[-int(months):]
    month_ends = [pd.Timestamp(d) for d in month_ends]
    rows = []
    for day in month_ends:
        try:
            res = sca.run(str(day.date()), 3, 10, 5, sca.CAPITAL_DEFAULT,
                          stock_mode=sca.DEFAULT_STOCK_MODE, board_mode=sca.DEFAULT_BOARD_MODE,
                          max_per_industry=2)
        except Exception as exc:                                      # noqa: BLE001
            if print_log:
                print(f"    {day.date()} 跳过：{type(exc).__name__}: {str(exc)[:60]}", flush=True)
            continue
        held = res["table"][res["table"]["in_portfolio"]]
        if held.empty:
            continue
        w = 1.0 / len(held)
        for code in held["stock_code"]:
            rows.append({"arm": "arm_concept", "month": day.strftime("%Y-%m"), "stock_code": code, "weight": w})
        if print_log and len(month_ends) and len(rows) % 25 < 5:
            print(f"    已回放 {day.date()}（累计 {len(rows)//5} 个月）", flush=True)
    if not rows:
        raise SystemExit("arm_concept 一个月都没回放出来")
    picks_csv = ROOT / "output/research/arm_concept_rotation/picks.csv"
    picks_csv.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(picks_csv, index=False)
    engine_metrics = run_replay_engine(picks_csv, "arm_concept", ROOT / "output/research/l3_replay")
    meta = {"strategy_id": "arm_concept", "curve_kind": "arm_concept_rotation_daily_exec",
            "months": len(rows) // 5,
            "engine": "scripts/replay_daily_execution.py（官方日度执行回放）",
            "engine_metrics": {k: engine_metrics.get(k) for k in
                               ("ann_return", "ann_vol", "sharpe", "max_drawdown", "calmar", "days", "trades")},
            "source": "scripts/select_concept_arm.py::run()（逐月回放，口径写死在该脚本头部）+ 官方日度执行回放引擎",
            "methodology": "月末决策 → 次日开盘等权买入 5 只 → 每日盯市 → 下月末换仓；执行假设与费用同 arm_select。",
            "known_weakness": "select_concept_arm.py 头部注明：概念动量近 5 年失效（后半段 −1.51%/月）、成分表非 PIT。"}
    out = ROOT / "output/research/arm_concept_rotation/curve"
    meta = to_pool_curve(ROOT / "output/research/l3_replay/arm_concept_nav.csv", out, meta, bench_series())
    (out.parent / "REPORT.md").write_text(
        f"# arm_concept（概念/板块轮动手臂）日度执行回放曲线\n\n"
        f"- 窗口：{meta['window']}（{meta['months']} 个月度决策日，等权 5 只）\n"
        f"- 来源：{meta['source']}\n- 引擎：{meta['engine']}\n"
        f"- 净值：`{out.relative_to(ROOT)}/nav_engine.csv`\n"
        f"- 指标：累计 {meta['metrics']['total_return']*100:+.2f}%、年化 {meta['metrics']['annual_return']*100:+.2f}%、"
        f"最大回撤 {meta['metrics']['max_drawdown']*100:.2f}%、夏普 {meta['metrics']['sharpe']}；"
        f"中证500 同窗 累计 {meta['csi500']['total_return']*100:+.2f}% ⇒ 相对 {meta['excess_return']*100:+.2f}%\n"
        f"- 已知弱点：{meta['known_weakness']}\n", encoding="utf-8")
    if print_log:
        print(f"[arm_concept] {meta['window']} 决策日 {meta['months']}｜引擎口径 年化 {meta['engine_metrics']['ann_return']*100:+.2f}% "
              f"回撤 {meta['engine_metrics']['max_drawdown']*100:.2f}% 夏普 {meta['engine_metrics']['sharpe']:.2f}｜"
              f"中证500 相对 {meta['excess_return']*100:+.2f}%", flush=True)
    return meta


def main() -> int:
    ap = argparse.ArgumentParser(description="研究手臂的独立回测曲线（vs 中证500）")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--arm-select", action="store_true")
    ap.add_argument("--arm-concept", action="store_true")
    ap.add_argument("--months", type=int, default=60, help="arm_concept 回放月数（4s/月）")
    args = ap.parse_args()
    if not (args.all or args.arm_select or args.arm_concept):
        args.all = True
    if args.all or args.arm_select:
        build_arm_select(True)
    if args.all or args.arm_concept:
        build_arm_concept(args.months, True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
