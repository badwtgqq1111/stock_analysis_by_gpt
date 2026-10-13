#!/usr/bin/env python3
"""因子审查（Inspector）：对**单条因子**做多维体检并落可复算的档案。

参考（2026-10-12）：FactorMiner（github.com/CharlesJ-ABu/FactorMiner）的 Inspector —— 它的价值在**体检维度**
（滚动 IC / 衰减 / 分位组合 / 换手 / 血缘 + "不虚构 Tearsheet"）。本脚本照这个思路做，但**口径按本仓库**：
  * 数据只来自本地面板 `assets/data/feature/clean_feature_panel`（clean 后列 + `pv_return_{1,5,20}d_clean`）；
  * 每个数字都写清窗口与样本量；样本不足就标 `insufficient`，**不补造**；
  * 与存量字段的冗余用**抽样日期的秩相关**（因子库的 1,366 列全量算太贵，抽样并显式标注）；
  * 输出 `output/factor_library/inspector/<factor_id>.json|md` + 汇总 `index.json`，供页面直读。

指标（每条都给定义与窗口）：
  1. `ic_by_horizon`：h ∈ {1,5,20} 的日频**横截面秩 IC** 序列 → 均值 / ICIR / NW t / 正号占比 / 期数；
  2. `decay`：IC_h1 与 IC_h20 的比值（衰减到多少）——只报比值，不做"半衰期"外推（我们没有多 horizon 的插值依据）；
  3. `rolling_ic`：滚动 12 期的 IC 均值曲线（画图用，不是新指标）；
  4. `quantiles`：按因子分 5 档（等频），每档的**平均未来 20 日收益**、多空(Q5−Q1) 均值与胜率；
  5. `turnover`：Q5 组成分在相邻期之间的换手（成分更替比例，滚动均值）；
  6. `lineage`：面板版本 / 清洗版本 / 日期范围 / 股票数 / 覆盖率 / PIT 有效占比 / 标签定义 / 脚本哈希；
  7. `redundancy`：与**准入因子**（factor_library 里 status=admitted 且面板里存在的列）的抽样秩相关，报最大 |ρ| 与对手 ids。

用法：
    uv run python scripts/factor_inspector.py --factor moneyflow_amount_scale_clean
    uv run python scripts/factor_inspector.py --top 5 --months 24      # 审 factor_library 里 ICIR 最强的 5 条
    uv run python scripts/factor_inspector.py --factor <id> --print --months 12
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LIBRARY = ROOT / "output/factor_library/factors.json"
OUT = ROOT / "output/factor_library/inspector"
HORIZONS = (1, 5, 20)
MIN_NAMES_PER_DATE = 100          # 一天少于这个股票数就不算 IC（避免小样本噪声）
MIN_DATES = 60                    # 少于这么多期就标 insufficient
ROLL_WINDOW = 12
REDUNDANCY_SAMPLE_DATES = 24      # 冗余体检抽多少天（显式标注，省时间）
REDUNDANCY_PEERS = 40             # 冗余对手取准入里 |ICIR| 前 N（全 100 列一起读太重）
REDUNDANCY_MONTHS = 6             # 冗余体检只看最近 N 个月（与全窗口分开标注）


def load_library() -> tuple[list[dict], dict]:
    payload = json.loads(LIBRARY.read_text(encoding="utf-8")) if LIBRARY.exists() else {"factors": []}
    return payload.get("factors", []), payload


def load_panel(columns: list[str], months: int) -> tuple[pd.DataFrame, dict]:
    """读面板的**必要列**（时间范围受 months 限制），并返回数据血缘信息。"""
    from data.ingest.service import MarketDataService

    service = MarketDataService()
    end = datetime.now().strftime("%Y-%m-%d")
    start = (pd.Timestamp(end) - pd.DateOffset(months=months)).strftime("%Y-%m-%d")
    frame, meta = service.read_clean_feature_panel(
        market="CN", factor_set="alpha_zoo_hk", start_date=start, end_date=end,
        feature_columns=columns)
    if isinstance(frame, tuple):                 # 兼容旧签名
        frame, meta = frame
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    return frame, {"start": start, "end": end, "meta": meta if isinstance(meta, dict) else {}}


def attach_forward_returns(panel: pd.DataFrame, *, months: int, horizons=HORIZONS) -> tuple[pd.DataFrame, dict]:
    """给面板挂上**自建的前向收益**（qfq 收盘 shift(-h)/close − 1）。

    为什么不用面板里的 `pv_return_*_clean`：那是**特征列**（过去 h 日收益）。第一版拿它当标签，
    算出 moneyflow_amount_scale 的 h20 IC = +0.2565（而因子库记录的月频 IC 是 **−0.139**）——
    正负完全相反，因为它量的是"因子 vs 过去收益"的相关，不是预测能力。现在改成自建标签。
    """
    import pyarrow.dataset as ds

    if panel.empty:
        return panel, {"status": "empty_panel"}
    end = pd.Timestamp(panel["trade_date"].max())
    start = pd.Timestamp(panel["trade_date"].min())
    lookahead = end + pd.Timedelta(days=int(max(horizons) * 2.2) + 15)
    root = ROOT / "assets/data/clean/ohlcv/market=CN"
    files = [str(path) for year in range(start.year, lookahead.year + 1)
             for path in root.glob(f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={year}/*.parquet")]
    if not files:
        return panel, {"status": "no_bars"}
    bars = ds.dataset(files, format="parquet").to_table(columns=["trade_date", "stock_code", "close"]).to_pandas()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    bars = bars[(bars["trade_date"] >= start - pd.Timedelta(days=10)) & (bars["trade_date"] <= lookahead)]
    wide = (bars.assign(stock_code=bars["stock_code"].astype(str))
            .pivot_table(index="trade_date", columns="stock_code", values="close", aggfunc="last")
            .sort_index())
    frames = [(wide.shift(-horizon) / wide - 1.0).stack().rename(f"fwd_return_{horizon}d") for horizon in horizons]
    merged = pd.concat(frames, axis=1).reset_index()
    merged.columns = ["trade_date", "stock_code", *[f"fwd_return_{h}d" for h in horizons]]
    merged["stock_code"] = merged["stock_code"].astype(str)
    out = panel.assign(stock_code=panel["stock_code"].astype(str)).merge(
        merged, on=["trade_date", "stock_code"], how="left")
    return out, {"status": "ok", "source": "clean/ohlcv qfq close",
                 "definition": "fwd_return_hd = close(t+h)/close(t) - 1",
                 "bars": int(len(bars)), "stocks": int(wide.shape[1]), "horizons": list(horizons)}


def rank_ic(frame: pd.DataFrame, factor: str, ret: str) -> pd.Series:
    """逐日横截面秩 IC（Spearman）：只保留当日有效样本 ≥ MIN_NAMES_PER_DATE 的日期。"""
    out: dict[pd.Timestamp, float] = {}
    for day, chunk in frame.groupby("trade_date"):
        pair = chunk[[factor, ret]].dropna()
        if len(pair) < MIN_NAMES_PER_DATE:
            continue
        if pair[factor].nunique() < 5 or pair[ret].nunique() < 5:
            continue
        out[day] = float(pair[factor].corr(pair[ret], method="spearman"))
    return pd.Series(out).sort_index()


def nw_t(series: pd.Series, lag: int = 1) -> float | None:
    values = series.dropna().to_numpy()
    n = len(values)
    if n < 8:
        return None
    mean = values.mean()
    demeaned = values - mean
    gamma0 = float((demeaned ** 2).sum() / n)
    var = gamma0
    for k in range(1, min(lag, n - 1) + 1):
        gamma = float((demeaned[k:] * demeaned[:-k]).sum() / n)
        var += 2 * (1 - k / (lag + 1)) * gamma
    if var <= 0:
        return None
    return float(mean / np.sqrt(var / n))


def quantiles_and_turnover(frame: pd.DataFrame, factor: str, ret: str, n_groups: int = 5,
                          horizon: int | None = None) -> dict:
    """等频分档 → 每档等权平均未来收益；Q5 组成分更替率 = 换手。"""
    rows: list[dict] = []
    prev_top: set[str] = set()
    turnovers: list[float] = []
    for day, chunk in frame.groupby("trade_date"):
        pair = chunk[["stock_code", factor, ret]].dropna()
        if len(pair) < MIN_NAMES_PER_DATE * n_groups // 5:
            continue
        try:
            labels = pd.qcut(pair[factor], n_groups, labels=False, duplicates="drop")
        except ValueError:
            continue
        if labels.nunique() < n_groups:
            continue
        group_ret = pair.groupby(labels.to_numpy())[ret].mean()
        rows.append({"date": day, **{f"q{int(k) + 1}": float(v) for k, v in group_ret.items()}})
        top = set(pair.loc[labels == labels.max(), "stock_code"].astype(str))
        if prev_top:
            turnovers.append(1 - len(top & prev_top) / max(len(top), 1))
        prev_top = top
    if not rows:
        return {"status": "insufficient", "reason": f"可用日期不足（<{MIN_DATES} 期）"}
    frame_q = pd.DataFrame(rows).set_index("date").sort_index()
    means = {f"q{i}": float(frame_q[f"q{i}"].mean()) for i in range(1, n_groups + 1) if f"q{i}" in frame_q}
    long_short = frame_q[f"q{n_groups}"] - frame_q["q1"]
    return {
        "status": "ok" if len(frame_q) >= MIN_DATES else "short_sample",
        "periods": int(len(frame_q)),
        "n_groups": n_groups,
        "mean_group_return": means,
        "long_short_mean": float(long_short.mean()),
        "long_short_std": float(long_short.std(ddof=1)) if len(long_short) > 1 else None,
        "long_short_win_rate": float((long_short > 0).mean()),
        "top_turnover_mean": float(np.mean(turnovers)) if turnovers else None,
        "unit": f"未来 {horizon or '?'} 日收益的等权平均（原始收益，未扣成本）",
    }


def redundancy(frame: pd.DataFrame, factor: str, peers: list[str]) -> dict:
    """与准入因子的**抽样**秩相关：报最大 |ρ| 与对手。抽样只影响精度，不影响方向（口径显式标注）。"""
    peers = [p for p in peers if p in frame.columns and p != factor]
    if not peers:
        return {"status": "no_peers"}
    dates = sorted(frame["trade_date"].unique())
    sample = dates[:: max(1, len(dates) // REDUNDANCY_SAMPLE_DATES)][:REDUNDANCY_SAMPLE_DATES]
    subset = frame[frame["trade_date"].isin(sample)][["trade_date", factor, *peers]].dropna(subset=[factor])
    best = {"peer": None, "abs_rho": 0.0}
    for peer in peers:
        pair = subset[[factor, peer]].dropna()
        if len(pair) < MIN_NAMES_PER_DATE:
            continue
        rho = abs(float(pair[factor].corr(pair[peer], method="spearman")))
        if rho > best["abs_rho"]:
            best = {"peer": peer, "abs_rho": rho}
    return {"status": "ok", "sampled_dates": len(sample), "peers": len(peers),
            "max_abs_rho": best["abs_rho"], "max_abs_rho_peer": best["peer"],
            "threshold": 0.7, "redundant": bool(best["abs_rho"] > 0.7)}


def inspect(entry: dict, *, months: int, admitted_peers: list[str]) -> dict:
    factor = str(entry.get("id"))
    ret_cols = {h: f"fwd_return_{h}d" for h in HORIZONS}     # 自建，见 attach_forward_returns
    columns = ["stock_code", "trade_date", "pit_valid", "quality_status", factor]
    t0 = time.time()
    panel, panel_meta = load_panel(columns, months)
    panel, label_meta = attach_forward_returns(panel, months=months, horizons=HORIZONS)
    if factor not in panel.columns or (len(panel) and float(panel[factor].notna().mean()) == 0.0):
        return {"status": "missing_column", "factor": factor,
                "reason": (f"面板里没有这一列（{len(panel.columns)} 列已加载）"
                           "；注意请求 `*_clean` 时加载器会 materialize 成全 NaN 列，这里按覆盖率 0 判缺列"),
                "panel": panel_meta}
    # 只保留质量标记为有效的行（面板里的取值是 "valid"；同时容忍 ok/good/空 —— 实测踩过：
    # 早期只白名单 ["ok","good",""] 会把整个面板过滤成 0 行，指标全变 insufficient）。
    if "quality_status" in panel.columns:
        flag = panel["quality_status"].astype(str).str.lower()
        panel = panel[flag.isin(["valid", "ok", "good", ""]) | panel["quality_status"].isna()]

    ic: dict[str, dict] = {}
    ic_series: dict[str, pd.Series] = {}
    for horizon, ret in ret_cols.items():
        series = rank_ic(panel, factor, ret) if ret in panel.columns else pd.Series(dtype=float)
        ic_series[horizon] = series
        if series.empty:
            ic[horizon] = {"status": "insufficient", "periods": 0}
            continue
        mean = float(series.mean())
        std = float(series.std(ddof=1)) if len(series) > 1 else None
        ic[horizon] = {
            "status": "ok" if len(series) >= MIN_DATES else "short_sample",
            "periods": int(len(series)),
            "mean": mean,
            "std": std,
            "icir": (mean / std) if std else None,
            "nw_t": nw_t(series),
            "positive_share": float((series > 0).mean()),
            "window": [str(series.index[0].date()), str(series.index[-1].date())],
        }
    decay = None
    if ic[1].get("mean") and ic[20].get("mean"):
        decay = {"ic_h1": ic[1]["mean"], "ic_h20": ic[20]["mean"],
                 "ratio_h1_over_h20": abs(ic[1]["mean"]) / abs(ic[20]["mean"]) if ic[20]["mean"] else None,
                 "note": "只报 h1/h20 的比值（多 horizon 插值不足，不做半衰期外推）"}

    rolling = {str(h): [{"date": str(d.date()), "ic": float(v)}
                        for d, v in ic_series[h].rolling(ROLL_WINDOW, min_periods=max(3, ROLL_WINDOW // 2))
                        .mean().dropna().items()] for h in HORIZONS}
    quant = (quantiles_and_turnover(panel, factor, ret_cols[20], horizon=20)
             if ret_cols[20] in panel.columns
             else {"status": "insufficient", "reason": "自建前向收益没有 h20（clean/ohlcv 缺数据）"})

    coverage = float(panel[factor].notna().mean()) if len(panel) else None
    pit_share = float(panel["pit_valid"].astype(str).str.lower().isin(["true", "1", "yes"]).mean()) \
        if "pit_valid" in panel.columns else None
    # 冗余：单独读一个**短窗口 + 前 N 个准入因子**的小面板（把 100 列 × 全窗口都读进来太重）
    peer_cols = [p for p in admitted_peers if p != factor][:REDUNDANCY_PEERS]
    redundancy_meta: dict = {"peer_source": f"factor_library status=admitted 里 |ICIR| 前 {REDUNDANCY_PEERS}"}
    peer_frame, _ = load_panel(["stock_code", "trade_date", factor, *peer_cols],
                              months=min(months, REDUNDANCY_MONTHS))
    red = redundancy(peer_frame, factor, peer_cols) if not peer_frame.empty else {"status": "no_panel"}
    red.update(redundancy_meta)

    return {
        "status": "ok",
        "factor": factor,
        "name": entry.get("name") or factor,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_seconds": round(time.time() - t0, 1),
        "window_months": months,
        "ic_by_horizon": ic,
        "decay": decay,
        "rolling_ic": rolling,
        "rolling_window": ROLL_WINDOW,
        "quantiles": quant,
        "redundancy": red,
        "lineage": {
            "panel": panel_meta,
            "rows": int(len(panel)),
            "dates": int(panel["trade_date"].nunique()),
            "stocks": int(panel["stock_code"].nunique()),
            "coverage": coverage,
            "pit_valid_share": pit_share,
            "label": {f"h{h}": ret_cols[h] for h in HORIZONS},
            "label_source": label_meta,
            "label_note": "标签是**自建前向收益**（qfq 收盘 close(t+h)/close(t) − 1，与流水线 label_mode=forward_return 同口径）；未做超额化，读 IC 时按同口径理解",
            "library_metrics": entry.get("metrics") or {},
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
        "notes": [
            f"日频横截面秩 IC，每日至少 {MIN_NAMES_PER_DATE} 只股票；少于 {MIN_DATES} 期标 short_sample。",
            "分位组合是**原始收益**（未扣成本、未做行业/市值中性）；换手只看 Q5 组成分更替。",
            f"冗余体检抽样 {REDUNDANCY_SAMPLE_DATES} 个交易日（全量算 1,366 列太贵），阈值 |ρ|>0.7 判冗余。",
        ],
    }


def to_md(payload: dict) -> str:
    if payload.get("status") != "ok":
        return f"# 因子审查 · {payload.get('factor')}\n\n- 状态：**{payload['status']}**（{payload.get('reason', '')}）\n"
    ic = payload["ic_by_horizon"]
    lines = [f"# 因子审查 · {payload['factor']}", "",
             f"- 窗口：近 {payload['window_months']} 个月；生成于 {payload['generated_at']}（{payload['elapsed_seconds']}s）", "",
             "| horizon | 期数 | 均值 IC | ICIR | NW t | 正号占比 | 窗口 |", "|---|---|---|---|---|---|---|"]
    for h in HORIZONS:
        row = ic.get(h) or {}
        if row.get("status") in (None, "insufficient"):
            lines.append(f"| h{h} | 0 | — | — | — | — | — |")
            continue
        lines.append(f"| h{h} | {row['periods']} | {row['mean']:+.4f} | "
                     f"{row['icir']:+.3f} | {row['nw_t']:+.2f} | {row['positive_share']:.0%} | "
                     f"{row['window'][0]} ~ {row['window'][1]} |")
    if payload.get("decay"):
        d = payload["decay"]
        lines += ["", f"- 衰减：h1 IC {d['ic_h1']:+.4f} / h20 IC {d['ic_h20']:+.4f} ⇒ 比值 {d['ratio_h1_over_h20']:.2f}",
                  f"  （{d['note']}）"]
    q = payload.get("quantiles") or {}
    if q.get("status") in ("ok", "short_sample"):
        lines += ["", f"## 分位组合（{q['periods']} 期，{q['unit']}）", "", "| 档位 | 平均收益 |", "|---|---|"]
        for key, value in q["mean_group_return"].items():
            lines.append(f"| {key.upper()} | {value:+.4%} |")
        lines += ["", f"- 多空 Q5−Q1：**{q['long_short_mean']:+.4%}**（胜率 {q['long_short_win_rate']:.0%}）",
                  f"- Q5 组成分换手（期均）：{q['top_turnover_mean']:.1%}" if q.get("top_turnover_mean") is not None else ""]
    else:
        lines += ["", f"## 分位组合：{q.get('status')}（{q.get('reason', '')}）"]
    red = payload.get("redundancy") or {}
    if red.get("status") == "ok":
        flag = "⚠️ 冗余" if red["redundant"] else "✅ 不冗余"
        lines += ["", f"- 与准入因子最大秩相关：**{red['max_abs_rho']:.3f}**（对手 {red['max_abs_rho_peer']}）"
                      f"｜阈值 {red['threshold']} → {flag}（抽样 {red['sampled_dates']} 天 / {red['peers']} 个对手）"]
    lin = payload["lineage"]
    lines += ["", "## 血缘", "",
              f"- 面板：{lin['panel']['start']} ~ {lin['panel']['end']}；行 {lin['rows']:,} / 日 {lin['dates']} / 股 {lin['stocks']}",
              f"- 覆盖率 {lin['coverage']:.1%}｜PIT 有效占比 {lin['pit_valid_share']:.1%}（None 表示面板无该列）"
              if lin.get("coverage") is not None else "- 覆盖率 —",
              f"- 标签：{lin['label']}（{lin['label_note']}）",
              f"- 审查脚本 sha256：`{lin['script_sha256'][:16]}…`"]
    lines += ["", "## 口径说明", ""] + [f"- {n}" for n in payload["notes"]]
    return "\n".join(line for line in lines if line != "") + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="因子审查（Inspector）")
    ap.add_argument("--factor", help="因子 id（factor_library 里的 id，且必须是 clean 面板的列名）")
    ap.add_argument("--top", type=int, default=0, help="审 factor_library 里 |ICIR| 最强的 N 条")
    ap.add_argument("--months", type=int, default=24, help="回看月数（默认 24）")
    ap.add_argument("--print", action="store_true")
    args = ap.parse_args()
    factors, _ = load_library()
    admitted_peers = sorted(
        (f for f in factors if f.get("status") in ("admitted", "in_use") and (f.get("metrics") or {}).get("icir")),
        key=lambda f: abs(float(f["metrics"]["icir"])), reverse=True)
    admitted_peers = [str(f["id"]) for f in admitted_peers]
    targets: list[dict] = []
    if args.factor:
        targets = [next((f for f in factors if f.get("id") == args.factor), {"id": args.factor})]
    elif args.top:
        # `--top` 只挑**真因子**：排除 *_is_missing（缺失掩码）与带 quality_column 标记的质量列 ——
        # 它们要么常量、要么是"这列今天可不可用"的旗标，算 IC 没有意义（实测第一版就挑出来一堆）。
        ranked = [f for f in factors
                  if (f.get("metrics") or {}).get("icir") is not None
                  and not str(f.get("id", "")).endswith("_is_missing")
                  and not (f.get("metrics") or {}).get("quality_column")
                  and f.get("status") in ("admitted", "in_use", "candidate")]
        ranked.sort(key=lambda f: abs(float(f["metrics"]["icir"])), reverse=True)
        targets = ranked[: args.top]
        # 只审面板里真的有的列（先按 id 判断不了，脚本会在 inspect 里给 missing_column）
    else:
        ap.print_help()
        return 4

    OUT.mkdir(parents=True, exist_ok=True)
    index_path = OUT / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {"factors": {}}
    for entry in targets:
        payload = inspect(entry, months=args.months, admitted_peers=admitted_peers)
        (OUT / f"{payload['factor']}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                                       encoding="utf-8")
        (OUT / f"{payload['factor']}.md").write_text(to_md(payload), encoding="utf-8")
        index["factors"][payload["factor"]] = {
            "status": payload.get("status"),
            "generated_at": payload.get("generated_at"),
            "ic_h20": ((payload.get("ic_by_horizon") or {}).get(20) or {}).get("mean"),
            "icir_h20": ((payload.get("ic_by_horizon") or {}).get(20) or {}).get("icir"),
            "nw_t_h20": ((payload.get("ic_by_horizon") or {}).get(20) or {}).get("nw_t"),
            "long_short_mean": (payload.get("quantiles") or {}).get("long_short_mean"),
            "max_abs_rho": (payload.get("redundancy") or {}).get("max_abs_rho"),
            "redundant": (payload.get("redundancy") or {}).get("redundant"),
        }
        if args.print:
            ic20 = (payload.get("ic_by_horizon") or {}).get(20) or {}
            quant = payload.get("quantiles") or {}
            red = payload.get("redundancy") or {}
            def fmt(value, spec="+.4f"):
                return format(float(value), spec) if isinstance(value, (int, float)) else "—"
            extra = (f" | h20 IC {fmt(ic20.get('mean'))} ICIR {fmt(ic20.get('icir'), '+.3f')}"
                     f" NWt {fmt(ic20.get('nw_t'), '+.2f')}"
                     f" | 多空 {fmt(quant.get('long_short_mean'), '+.2%')}"
                     f" | maxρ {fmt(red.get('max_abs_rho'), '.3f')}")
            print(f"[inspector] {payload['factor']}: {payload.get('status')}"
                  + (extra if ic20.get("status") == "ok" else f" | {ic20.get('status')}"))
            print(f"            -> {OUT.relative_to(ROOT)}/{payload['factor']}.md")
    index["generated_at"] = datetime.now().isoformat(timespec="seconds")
    index["criteria"] = {"min_names_per_date": MIN_NAMES_PER_DATE, "min_dates": MIN_DATES,
                         "roll_window": ROLL_WINDOW, "redundancy_threshold": 0.7,
                         "redundancy_sample_dates": REDUNDANCY_SAMPLE_DATES}
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
