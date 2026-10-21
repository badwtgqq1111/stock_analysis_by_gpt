#!/usr/bin/env python3
"""P2.75 候选池二次排序（策略选出的 100+ 只票）。

这不是"再挖一个 alpha"，而是**在已经入选的候选池内**做条件排序：
  * 策略先验（被几条策略选中 / 原始 rank）
  * AI 决策（买/持/卖 + 置信度）
  * 研报证据（偏多/偏空 + 引用强度 + 核对级别）
  * 威科夫（WSO + 当日事件 + 60 日区间位置）
  * 筹码（获利盘、集中度、筹码峰距离）

所有分量先在**当日候选池内**做截面 z-score（winsorize 2%/98%），缺失记 NaN，
综合分按"可用分量权重重新归一"计算，并输出 coverage；不把缺失硬凑成 0。

用法：
  python3 scripts/p2_75_candidate_rank.py --date 2026-10-16
  python3 scripts/p2_75_candidate_rank.py --date 2026-10-16 --top 30

产物：
  output/research/p2_75_candidate_rank/<date>.csv
  output/research/p2_75_candidate_rank/<date>.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services.data_service import load_cn_stock_ohlcv  # noqa: E402
from backend.services import wyckoff_service as wy  # noqa: E402
from backend.services import selection_sources  # noqa: E402

AI_DIR = ROOT / "output/results_cn/ai_pick_analysis"
REGISTRY = ROOT / "config/strategies.json"
MONEYFLOW = ROOT / "assets/data/derived/cn_moneyflow_features.parquet"
DAILY_BASIC_DIR = ROOT / "assets/data/raw/daily_basic_relay"
BYNEWLT = ROOT / "output/results_cn/bynewlt_research/dossier"
PUBLIC = ROOT / "output/results_cn/cn_public_research/dossier"
OUT = ROOT / "output/research/p2_75_candidate_rank"

# 初始权重：先验是"选股模型已经说过的话"，权重不宜最高；AI/研报是前向增量，
# 先给中等权重；威科夫/筹码是历史可回测的结构信号。上线前必须用 walk-forward 重估。
WEIGHTS = {"prior": 0.20, "ai": 0.20, "research": 0.15, "wyckoff": 0.25, "chip": 0.20}
# 资金流 + 个股多空：用户要求"按权重合成一个排序"，不是两个独立模式。
# 50/50 是初始先验；上线前同样要用前向/历史验证重估（目前没有验证，仅作观察排序）。
FLOW_LONGSHORT_WEIGHTS = {"flow": 0.5, "longshort": 0.5}

ACTION_MAP = {"买入": 1.0, "持有": 0.0, "卖出": -1.0}
STANCE_MAP = {"偏多": 1.0, "中性": 0.0, "偏空": -1.0}


def load_universe(date: str) -> dict[str, dict]:
    """K 线左侧"全部策略"的同一口径：每条策略最新名单的并集（不是只看 AI 名单）。

    2026-10-18 事故：侧栏 124 只，综合排名只有 109 只 —— 因为排序只读了 AI 名单，
    而 box/wyckoff/moneyflow/donchian 等名单是在 AI 之后才刷新的。这里直接按
    `config/strategies.json` 的 history_dir/selection_source 取名单，和侧栏一致。

    2026-10-20 事故：`cn_ensemble` 的 history 文件只存 PK 后 6 只，侧栏却把
    preselected 8 只与 selected 6 只合并；结果 301230.SZ 在侧栏可见、排名里没有。
    当源的最新交易日正好等于目标日时，优先走 `selection_sources.load_source`，
    复用 `/api/selection` 的合并口径；历史日则仍读当日 history，避免未来数据泄漏。
    """
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    picks: dict[str, dict] = {}
    for s in registry.get("strategies", []):
        history = s.get("history_dir")
        source = s.get("selection_source")
        if not history or not source or s.get("stage") == "retired":
            continue
        rows = []
        try:
            live = selection_sources.load_source(source)
        except Exception:
            live = None
        if live and str(live.get("trade_date") or "") == date:
            rows = [(str(r.get("stock_code") or "").strip().upper(), str(r.get("stock_name") or ""))
                    for r in live.get("rows") or []]
        else:
            folder = (ROOT / history) if not Path(history).is_absolute() else Path(history)
            files = sorted(folder.glob("*.csv"))
            if not files:
                continue
            preferred = folder / f"{date}.csv"
            path = preferred if preferred.exists() else files[-1]
            try:
                frame = pd.read_csv(path)
            except (OSError, ValueError):
                continue
            col = "stock_code" if "stock_code" in frame.columns else (
                "code" if "code" in frame.columns else frame.columns[0])
            name_col = "stock_name" if "stock_name" in frame.columns else (
                "name" if "name" in frame.columns else None)
            rows = [(str(row.get(col) or "").strip().upper(),
                     str(row.get(name_col) or "") if name_col else "")
                    for _, row in frame.iterrows()]
        for code, name in rows:
            if not code:
                continue
            entry = picks.setdefault(code, {"name": "", "strategies": []})
            if name and not entry["name"]:
                entry["name"] = name
            if s["id"] not in entry["strategies"]:
                entry["strategies"].append(s["id"])
    return picks


def load_flow_ratios(date: str) -> pd.DataFrame:
    """资金流 5d/20d ÷ 流通市值（同一单位：万元），PIT 取决策日及以前最近值。

    用户口径："资金流(5d/20d/总流通对比)" —— 不只看净流入绝对值，而是看它在
    流通市值里的占比，避免大票天然流入大、小票天然流入小。
    """
    try:
        mf = pd.read_parquet(MONEYFLOW, columns=[
            "trade_date", "stock_code", "source", "moneyflow_net_5d", "moneyflow_net_20d"])
    except (OSError, ValueError):
        return pd.DataFrame(columns=["code", "flow_5d_ratio", "flow_20d_ratio"])
    mf = mf[(mf["source"] == "moneyflow") & (mf["trade_date"] <= pd.Timestamp(date))]
    if mf.empty:
        return pd.DataFrame(columns=["code", "flow_5d_ratio", "flow_20d_ratio"])
    mf = mf.sort_values("trade_date").groupby("stock_code").tail(1).copy()
    files = sorted(DAILY_BASIC_DIR.glob("*.parquet"))
    cap_map: dict[str, float] = {}
    for path in reversed(files):
        month = path.stem.replace("month=", "")
        if pd.Period(month, freq="M") > pd.Period(pd.Timestamp(date), freq="M"):
            continue
        try:
            db = pd.read_parquet(path, columns=["ts_code", "circ_mv"])
        except (OSError, ValueError):
            continue
        cap_map = {str(c): float(v) for c, v in zip(db["ts_code"], db["circ_mv"]) if pd.notna(v)}
        break
    mf["code"] = mf["stock_code"].astype(str)
    mf["circ_mv"] = mf["code"].map(cap_map)
    mf["flow_5d_ratio"] = mf["moneyflow_net_5d"] / mf["circ_mv"].replace(0, np.nan)
    mf["flow_20d_ratio"] = mf["moneyflow_net_20d"] / mf["circ_mv"].replace(0, np.nan)
    return mf[["code", "flow_5d_ratio", "flow_20d_ratio"]]


def _zscore(series: pd.Series) -> pd.Series:
    s = series.astype(float)
    if s.notna().sum() < 3:
        return pd.Series(np.nan, index=s.index)
    lo, hi = s.quantile(0.02), s.quantile(0.98)
    s = s.clip(lo, hi)
    std = s.std(ddof=0)
    return (s - s.mean()) / std if std and std > 0 else pd.Series(0.0, index=s.index)


def _chip_stats(bars: pd.DataFrame, n_bins: int = 50, with_change: bool = True) -> dict:
    if bars is None or len(bars) < 20:
        return {}
    recent = bars.tail(120)
    close = recent["Close"].to_numpy(float)
    high = recent["High"].to_numpy(float)
    low = recent["Low"].to_numpy(float)
    vol = recent["Volume"].to_numpy(float)
    cur = float(close[-1])
    lo = float(low.min()) * 0.95
    hi = float(high.max()) * 1.05
    if not (hi > lo):
        return {}
    centers = lo + (hi - lo) * (np.arange(n_bins) + 0.5) / n_bins
    vols = np.zeros(n_bins)
    n = len(recent)
    for i in range(n):
        if not (high[i] > low[i]) or not (vol[i] > 0):
            continue
        weight = math.exp(-0.02 * (n - 1 - i))
        mask = (centers >= low[i]) & (centers <= high[i])
        k = int(mask.sum())
        if k:
            vols[mask] += vol[i] * weight / k
    total = vols.sum()
    if total <= 0:
        return {}
    vols = vols / total * 100.0
    profit = float(vols[centers <= cur].sum())
    avg = float((centers * vols).sum() / 100.0)
    order = np.argsort(centers)
    cum = 0.0
    p5, p95 = float(centers[order[0]]), float(centers[order[-1]])
    for idx in order:
        cum += vols[idx]
        if cum <= 5:
            p5 = float(centers[idx])
        if cum <= 95:
            p95 = float(centers[idx])
    peak = float(centers[int(np.argmax(vols))])
    concentration = (p95 - p5) / avg if avg > 0 else 0.0
    out = {
        "profit_pct": profit,
        "avg_cost": avg,
        "p5": p5,
        "p95": p95,
        "concentration": concentration,
        "width": concentration,
        "peak": peak,
        "peak_dist": (cur - peak) / peak if peak > 0 else 0.0,
    }
    if with_change and len(bars) >= 140:
        prev = _chip_stats(bars.iloc[:-20], n_bins=n_bins, with_change=False)
        if prev.get("width"):
            out["width_chg_20"] = out["width"] / prev["width"] - 1.0
    return out


def _research_score(code: str) -> tuple[float | None, str | None, str | None]:
    for folder in (BYNEWLT, PUBLIC):
        path = folder / f"{code}.json"
        if not path.exists():
            continue
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        stance = STANCE_MAP.get(str(d.get("stance") or ""))
        if stance is None:
            return None, d.get("stance"), None
        strength = min(1.0, (len(d.get("bull") or []) + len(d.get("bear") or [])
                             + len(d.get("citations") or [])) / 12.0)
        level = (d.get("verification") or {}).get("level")
        weight = 1.0 if level == "agent_checked" else 0.8
        return stance * (0.5 + 0.5 * strength) * weight, d.get("stance"), level
    return None, None, None


def _wyckoff_score(bars: pd.DataFrame) -> tuple[float | None, float | None, float | None]:
    if bars is None or len(bars) < 60:
        return None, None, None
    try:
        read = wy.wyckoff_read(bars)
        cloud = wy.event_cloud(bars)
    except Exception:
        return None, None, None
    wso = (cloud.get("score") or {}).get("wso")
    top = (cloud.get("cloud") or [None])[0]
    event = (float(top["side"]) * float(top["prob"])) if top else 0.0
    recent = bars.tail(60)
    lo, hi = float(recent["Low"].min()), float(recent["High"].max())
    pos = (float(bars["Close"].iloc[-1]) - lo) / (hi - lo) if hi > lo else 0.5
    position = 1.0 - 2.0 * pos          # 低位（吸筹）为正，高位（派发）为负
    if wso is None:
        return None, None, pos
    return 0.5 * float(wso) + 0.3 * event + 0.2 * position, float(wso), pos


def _load_ai_rows(date: str) -> tuple[dict[str, dict], dict]:
    """AI 是可选增强层：当日 AI 任务失败时仍应生成排名，只降低 coverage。"""
    path = AI_DIR / f"{date}.json"
    if not path.exists():
        return {}, {"available": False, "path": str(path), "reason": "没有当日 AI 名单"}
    try:
        rows = json.loads(path.read_text(encoding="utf-8")).get("rows") or []
    except (OSError, json.JSONDecodeError) as exc:
        return {}, {"available": False, "path": str(path), "reason": f"AI 名单读取失败：{exc}"}
    return {str(r.get("stock_code")): r for r in rows}, {
        "available": True, "path": str(path), "rows": len(rows)}


def rank(date: str, top: int = 30) -> dict:
    ai_map, ai_meta = _load_ai_rows(date)
    picks = load_universe(date)
    # 以侧栏策略并集为准；AI 名单只提供 AI 分量。
    # 手工分析（watchlist_manual）不在策略侧栏里，不能因此被塞进综合排名。
    universe = sorted(picks)
    records = []
    for code in universe:
        row = ai_map.get(code) or {}
        pick = picks.get(code) or {}
        bars = load_cn_stock_ohlcv(code, days=260)
        if bars is None or bars.empty:
            continue
        chips = _chip_stats(bars)
        wy_score, wso, pos = _wyckoff_score(bars)
        res_score, stance, level = _research_score(code)
        action = str(row.get("action") or "")
        conf = row.get("confidence")
        ai = ACTION_MAP.get(action)
        ai_score = None if ai is None else ai * (0.5 + 0.5 * float(conf or 0.5))
        records.append({
            "code": code,
            "name": row.get("name") or pick.get("name") or "",
            "strategies": pick.get("strategies") or row.get("strategies") or [],
            "n_strategies": len(pick.get("strategies") or row.get("strategies") or []),
            "ai_action": action,
            "ai_conf": conf,
            "ai": ai_score,
            "research_stance": stance,
            "research_level": level,
            "research": res_score,
            "wso": wso,
            "wyckoff_pos": pos,
            "wyckoff": wy_score,
            "profit_pct": chips.get("profit_pct"),
            "avg_cost": chips.get("avg_cost"),
            "concentration": chips.get("concentration"),
            "chip_width": chips.get("width"),
            "chip_width_chg_20": chips.get("width_chg_20"),
            "peak": chips.get("peak"),
            "peak_dist": chips.get("peak_dist"),
        })
    frame = pd.DataFrame(records)
    if frame.empty:
        raise SystemExit("没有候选")
    # 名称兜底（AI 名单里部分票 name 为空）
    try:
        basic = pd.read_parquet(ROOT / "assets/data/meta/cn_stock_basic.parquet",
                                columns=["ts_code", "name"]).drop_duplicates("ts_code")
        name_map = dict(zip(basic["ts_code"].astype(str), basic["name"].astype(str)))
        frame["name"] = [
            (str(n) if str(n) not in ("", "nan", "None") else name_map.get(str(c), ""))
            for c, n in zip(frame["code"], frame["name"])
        ]
    except Exception:
        pass
    # 资金流排名：5d/20d 净流入 ÷ 流通市值
    flow = load_flow_ratios(date)
    if not flow.empty:
        frame = frame.merge(flow, on="code", how="left")
    else:
        frame["flow_5d_ratio"] = np.nan
        frame["flow_20d_ratio"] = np.nan
    frame["z_flow5"] = _zscore(frame["flow_5d_ratio"])
    frame["z_flow20"] = _zscore(frame["flow_20d_ratio"])
    flow_avail = (frame["z_flow5"].notna().astype(float) + frame["z_flow20"].notna().astype(float))
    frame["flow_score"] = (frame["z_flow5"].fillna(0.0) + frame["z_flow20"].fillna(0.0)) / flow_avail.replace(0, np.nan)

    # 先验：共识策略数（原始 rank 只在单策略名单里有意义，跨策略不可比）
    frame["prior"] = frame["n_strategies"].astype(float)
    # 筹码：获利盘用"中性最优"的倒 U（两头都是压力），集中度越低越好，价在峰上方加分
    profit = frame["profit_pct"].astype(float) / 100.0
    frame["chip"] = (
        0.4 * (1.0 - (profit - 0.5).abs() / 0.5)
        + 0.3 * (-frame["concentration"].astype(float))
        + 0.3 * np.clip(frame["peak_dist"].astype(float) / 0.10, -1, 1)
    )
    for key in ("prior", "ai", "research", "wyckoff", "chip"):
        frame[f"z_{key}"] = _zscore(frame[key])

    total_w = 0.0
    weighted = pd.Series(0.0, index=frame.index)
    for key, weight in WEIGHTS.items():
        col = frame[f"z_{key}"]
        weighted += col.fillna(0.0) * weight
        total_w += col.notna().astype(float) * weight
    frame["coverage"] = total_w / sum(WEIGHTS.values())
    frame["composite"] = weighted / total_w.replace(0, np.nan)
    # 拆成两层：core 是历史可回测部分，overlay 是 AI/研报（只能前向验证）
    core_keys = ("prior", "wyckoff", "chip")
    overlay_keys = ("ai", "research")
    core_w = sum(WEIGHTS[k] for k in core_keys)
    overlay_w = sum(WEIGHTS[k] for k in overlay_keys)
    core_avail = sum(frame[f"z_{k}"].notna().astype(float) * WEIGHTS[k] for k in core_keys)
    overlay_avail = sum(frame[f"z_{k}"].notna().astype(float) * WEIGHTS[k] for k in overlay_keys)
    frame["core_score"] = sum(frame[f"z_{k}"].fillna(0.0) * WEIGHTS[k] for k in core_keys) / core_avail.replace(0, np.nan)
    frame["overlay_score"] = sum(frame[f"z_{k}"].fillna(0.0) * WEIGHTS[k] for k in overlay_keys) / overlay_avail.replace(0, np.nan)
    # 个股多空（AI 决策 + 研报证据合并）就是增强层分数；中性 AI 记 0，由研报补足
    frame["longshort_score"] = frame["overlay_score"]
    # 资金流 + 个股多空按权重合成一个分数（两者先各自再 z 一次，保证量纲一致）
    frame["z_flow"] = _zscore(frame["flow_score"])
    frame["z_longshort"] = _zscore(frame["longshort_score"])
    combo_avail = (
        frame["z_flow"].notna().astype(float) * FLOW_LONGSHORT_WEIGHTS["flow"]
        + frame["z_longshort"].notna().astype(float) * FLOW_LONGSHORT_WEIGHTS["longshort"]
    )
    frame["flow_longshort_score"] = (
        frame["z_flow"].fillna(0.0) * FLOW_LONGSHORT_WEIGHTS["flow"]
        + frame["z_longshort"].fillna(0.0) * FLOW_LONGSHORT_WEIGHTS["longshort"]
    ) / combo_avail.replace(0, np.nan)
    frame["risk_flags"] = [
        ",".join(flag for flag, cond in (
            ("ai_sell", r.ai_action == "卖出"),
            ("research_bear", r.research_stance == "偏空"),
            ("overheated", (r.profit_pct or 0) > 90 and (r.wyckoff_pos or 0) > 0.8),
        ) if cond)
        for r in frame.itertuples()
    ]
    frame = frame.sort_values("composite", ascending=False).reset_index(drop=True)
    frame["rank"] = np.arange(1, len(frame) + 1)

    OUT.mkdir(parents=True, exist_ok=True)
    csv_path = OUT / f"{date}.csv"
    json_path = OUT / f"{date}.json"
    frame.to_csv(csv_path, index=False)
    payload = {
        "date": date,
        "weights": WEIGHTS,
        "flow_longshort_weights": FLOW_LONGSHORT_WEIGHTS,
        "ai_source": ai_meta,
        "n": int(len(frame)),
        "rows": frame.replace({np.nan: None}).to_dict(orient="records"),
        "notes": [
            "综合分 = 当日候选池内各分量 z-score 的加权平均（缺失按可用权重重新归一）。",
            "AI/研报是前向增量，不能用于历史回测；历史可回测的是 prior + wyckoff + chip。",
            "筹码获利盘采用倒 U：极高获利盘（抛压）与极低获利盘（套牢）都扣分。",
        ],
    }
    # 把核心层历史验证结果一起带上（没有验证产物就明说"未验证"）
    summary_path = OUT / "core_walkforward_summary.json"
    if summary_path.exists():
        try:
            s = json.loads(summary_path.read_text(encoding="utf-8"))
            mean_ic = s.get("mean_ic")
            payload["validation"] = {
                "status": "failed" if (mean_ic is not None and mean_ic <= 0) else "passed",
                "mean_ic": mean_ic,
                "ic_t": s.get("ic_t"),
                "n_dates": s.get("n_dates"),
                "note": "核心层首次历史评估；未通过前综合分仅作实验排序，不作为交易信号",
            }
        except (OSError, json.JSONDecodeError):
            pass
    else:
        payload["validation"] = {"status": "missing", "note": "尚未跑核心层历史验证"}
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[rank] {date} n={len(frame)} -> {csv_path}")
    cols = ["rank", "code", "name", "composite", "coverage", "ai_action", "research_stance", "wso", "profit_pct", "concentration"]
    print(frame[cols].head(top).to_string(index=False))
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description="P2.75 候选池二次排序")
    ap.add_argument("--date", default="2026-10-16")
    ap.add_argument("--top", type=int, default=30)
    args = ap.parse_args()
    rank(args.date, args.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
