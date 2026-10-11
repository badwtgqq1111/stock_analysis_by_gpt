"""选股日历 + 历史选股/收益回顾 API。

数据来源：`scripts/build_selection_ledger.py` 产出的**选股台账**
（`output/results_cn/selection_ledger.parquet`），台账本身合并了三个真实历史源：
  * `notify`     —— `output/results_cn/feishu_<date>.json`（当日最终名单，含权重/手数/PK 备注）
  * `preselected`—— `output/selection_snapshots/<date>/*/preselected.csv`（归档候选池，跨一年）
  * `survey_raw` / `survey_gate` —— 研究回看名单（门禁前后）

台账已算好 r1/r5/r10/r20 与"相对全市场等权"的超额，本 API 只做聚合与切片（不重算收益）。
台账缺失时返回 `built=false`，并提示重建命令 —— 避免前端展示一份过期数据而不知道。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, HTTPException, Query

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "output" / "results_cn" / "selection_ledger.parquet"
SUMMARY = ROOT / "output" / "results_cn" / "selection_ledger_summary.json"
HORIZONS = (1, 5, 10, 20)

router = APIRouter(prefix="/api/calendar", tags=["calendar"])

# 台账 source → 策略口径（前端「多策略对比」按这个分组显示）
STRATEGY_META = {
    "notify": {"label": "模型策略（PK 后最终名单）", "group": "模型策略"},
    "preselected": {"label": "模型策略（候选池）", "group": "模型策略"},
    "arm": {"label": "小市值/研究手臂（arm_select）", "group": "小市值手臂"},
    "arm_concept": {"label": "行业轮动（概念/板块手臂）", "group": "行业轮动"},
    "rot_arm": {"label": "行业轮动（MST 大类 + 7 因子等权）", "group": "行业轮动"},
    "box_arm": {"label": "箱体突破回踩（结构型候选）", "group": "打板/低吸"},
    "first_ban_lowxi": {"label": "首板低吸（社区策略·反例档案）", "group": "打板/低吸"},
    "jq_multi_factor": {"label": "聚宽多因子（低市值+高ROE）", "group": "多因子策略"},
    "jq_roe_roa": {"label": "动态诊断候选：ROE＋0.25×ROA", "group": "多因子策略"},
    "survey_raw": {"label": "调研回看（原）", "group": "调研"},
    "survey_gate": {"label": "调研回看（门禁）", "group": "调研"},
}
_cache: dict[str, object] = {"mtime": None, "frame": None}


def _load() -> pd.DataFrame:
    if not LEDGER.exists():
        return pd.DataFrame()
    mtime = LEDGER.stat().st_mtime
    if _cache["mtime"] != mtime:
        _cache["frame"] = pd.read_parquet(LEDGER)
        _cache["mtime"] = mtime
    return _cache["frame"]  # type: ignore[return-value]


def _meta() -> dict:
    if SUMMARY.exists():
        try:
            return json.loads(SUMMARY.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def _num(v) -> float | None:
    return float(v) if v is not None and pd.notna(v) else None


def _string(v, default: str = "") -> str:
    return str(v) if v is not None and pd.notna(v) else default


def _row_payload(row: pd.Series) -> dict:
    item = {
        "decision_date": str(row.get("decision_date")), "source": row.get("source"),
        "stock_code": row.get("stock_code"), "name": row.get("name") or "",
        "industry": row.get("industry") or "", "rank": _num(row.get("rank")),
        "model_score": _num(row.get("model_score")), "weight": _num(row.get("weight")),
        "lots": _num(row.get("lots")), "note": row.get("note") or "",
        "reason": row.get("reason_text") or "",
        "stage": row.get("stage") or "", "stage_label": row.get("stage_label") or "",
        "stage_evidence": row.get("stage_evidence") or "",
        "risk": row.get("risk_summary") or "", "risk_notes": row.get("risk_notes_summary") or "",
        "ai_action": _string(row.get("ai_action"), "待分析"),
        "ai_confidence": _num(row.get("ai_confidence")),
        "ai_summary": _string(row.get("ai_summary")),
        "ai_bull_case": _string(row.get("ai_bull_case")),
        "ai_bear_case": _string(row.get("ai_bear_case")),
        "ai_risk_flags": _string(row.get("ai_risk_flags")),
        "ai_horizon": _string(row.get("ai_horizon")),
        "ai_status": _string(row.get("ai_status"), "pending"),
        "ai_generated_at": _string(row.get("ai_generated_at")),
    }
    for h in HORIZONS:
        item[f"r{h}"] = _num(row.get(f"r{h}"))
        item[f"excess_r{h}"] = _num(row.get(f"excess_r{h}"))
    return item


@router.get("/summary")
def summary() -> dict:
    frame = _load()
    meta = _meta()
    if frame.empty:
        return {"built": False, "hint": "uv run python scripts/build_selection_ledger.py",
                "sources": [], "dates": []}
    by_date = (frame.groupby(["decision_date", "source"])
               .agg(n=("stock_code", "size"), n_with_return=("r5", lambda s: int(s.notna().sum())),
                    mean_r5=("r5", "mean"), mean_excess_r5=("excess_r5", "mean"))
               .reset_index())
    dates = sorted(frame["decision_date"].astype(str).unique())
    return {
        "built": True, "meta": meta,
        # 注意：`pd.Timestamp(x, unit="s")` 给的是 **UTC**，页面上会少 8 小时
        # （实测台账本地 08:13 被显示成 00:13）。用 datetime.fromtimestamp 取本地时间。
        "ledger_mtime": datetime.fromtimestamp(LEDGER.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        "sources": sorted(frame["source"].unique().tolist()),
        "source_counts": {k: int(v) for k, v in frame["source"].value_counts().items()},
        "dates": dates,
        "months": sorted({d[:7] for d in dates}),
        "by_date": [{**{k: (None if pd.isna(v) else (round(float(v), 5) if isinstance(v, float) else v))
                        for k, v in row.items()}}
                    for row in by_date.to_dict("records")],
    }


@router.get("/month")
def month(month: str = Query(..., description="YYYY-MM"),
          source: str = Query("", description="留空=全部来源")) -> dict:
    frame = _load()
    if frame.empty:
        raise HTTPException(status_code=404, detail="台账不存在，先跑 scripts/build_selection_ledger.py")
    sub = frame[frame["decision_date"].astype(str).str.startswith(month)]
    if source:
        sub = sub[sub["source"] == source]
    days = []
    for date, group in sub.groupby("decision_date"):
        days.append({
            "date": str(date),
            "per_source": {k: int(v) for k, v in group["source"].value_counts().items()},
            "n": int(len(group)),
            "n_with_return": int(group["r5"].notna().sum()),
            "mean_r1": _num(group["r1"].mean()), "mean_r5": _num(group["r5"].mean()),
            "mean_excess_r5": _num(group["excess_r5"].mean()),
            "mean_r10": _num(group["r10"].mean()), "mean_r20": _num(group["r20"].mean()),
            "win_rate_r5": _num((group["r5"] > 0).mean() if group["r5"].notna().any() else None),
        })
    return {"month": month, "source": source or "all", "days": sorted(days, key=lambda d: d["date"])}


@router.get("/day")
def day(date: str = Query(...), source: str = Query("")) -> dict:
    frame = _load()
    if frame.empty:
        raise HTTPException(status_code=404, detail="台账不存在")
    sub = frame[frame["decision_date"].astype(str) == date]
    if source:
        sub = sub[sub["source"] == source]
    if sub.empty:
        raise HTTPException(status_code=404, detail=f"{date} 没有选股记录")
    sub = sub.sort_values(["source", "rank"], na_position="last")
    # 名称回填。原实现有个逻辑 bug：先用台账里"已经有名字的行"建出 names，
    # 再只拿这些**已经解决了的代码**去查注册表（`_names(list(names))`），
    # 而且用 setdefault 不会覆盖 —— 于是"台账里没名字的行"永远补不上。
    # 实测 preselected 那一半（台账里 name 为空）在日历上一直只有代码没有名称。
    names = {}
    for code, group in sub.groupby("stock_code"):
        value = group["name"].dropna()
        if len(value) and str(value.iloc[0]):
            names[code] = str(value.iloc[0])
    missing = [c for c in sub["stock_code"].astype(str).unique() if not names.get(c)]
    if missing:
        from backend.api.pipeline import _names
        for code, name in _names(missing).items():
            if name:
                names.setdefault(code, name)
    rows = [_row_payload(r) for _, r in sub.iterrows()]
    for r in rows:
        if not r["name"]:
            r["name"] = names.get(r["stock_code"], "")

    # ── 多策略对比（2026-10-09 新增）────────────────────────────────────────
    # 背景：同一个决策日同一只票会在多个源里各出现一行（preselected 是候选池、
    # notify 是 PK 后的最终名单、arm 是研究手臂），平铺列表看起来就是"重复"。
    # 实测 2026-10-08：8 只票 × 2 个源 = 16 行，其实是同一批票。
    # 这里改成"一只票一行 × 每个策略一列"，才是用户要的"每日选股多策略对比"。
    strategies = [k for k, _ in sorted(sub["source"].value_counts().items(),
                                       key=lambda kv: -kv[1])]
    # 展示优先级：最终名单 > 研究手臂 > 候选池 > 其它（用于行级的收益/阶段/原因取数）
    priority = {"notify": 0, "pk": 1, "arm": 2, "preselected": 3}
    compare: dict[str, dict] = {}
    for r in rows:
        entry = compare.setdefault(r["stock_code"], {
            "stock_code": r["stock_code"], "name": r["name"], "industry": r["industry"],
            "model_score": r["model_score"], "cells": {}, "_rank": 99,
        })
        entry["cells"][r["source"]] = {"rank": r["rank"], "weight": r["weight"], "lots": r["lots"]}
        if not entry["name"] and r["name"]:
            entry["name"] = r["name"]
        if entry["industry"] in ("", None) and r["industry"]:
            entry["industry"] = r["industry"]
        if entry["model_score"] is None and r["model_score"] is not None:
            entry["model_score"] = r["model_score"]
        # 行级信息（收益/阶段/原因/风险）取"展示优先级最高的那个源"的那一行
        p_old = priority.get(entry.get("_src", ""), 99)
        p_new = priority.get(r["source"], 9)
        if p_new < p_old:
            entry["_src"] = r["source"]
            for key in ("rank", "lots", "note", "reason", "stage", "stage_label",
                        "stage_evidence", "risk", "risk_notes", "ai_action", "ai_confidence",
                        "ai_summary", "ai_bull_case", "ai_bear_case", "ai_risk_flags",
                        "ai_horizon", "ai_status", "ai_generated_at"):
                entry[key] = r[key]
            for h in HORIZONS:
                entry[f"r{h}"] = r[f"r{h}"]
                entry[f"excess_r{h}"] = r[f"excess_r{h}"]
            entry["_rank"] = entry.get("rank") if entry.get("rank") is not None else 99
    compare_rows = sorted(compare.values(), key=lambda e: (e["_rank"], e["stock_code"]))
    for e in compare_rows:
        e.pop("_rank", None)
        e.pop("_src", None)

    # ── 策略分组（2026-10-09 新增）────────────────────────────────────────
    # 用户要"一眼看出每只票是哪种策略选出来的"。台账里的 source 是流水线内部名
    # （notify/preselected/arm/arm_concept/survey_*），这里映射成三种**策略**：
    #   模型策略（preselect → PK）、小市值/研究手臂（arm_select）、行业轮动（概念手臂）
    # 并把每只票命中的策略名直接写进行里，前端不用再自己拼。
    for e in compare_rows:
        keys = list(e["cells"].keys())
        e["strategy_keys"] = keys
        e["strategy_labels"] = [STRATEGY_META.get(k, {}).get("label", k) for k in keys]
        e["strategy_groups"] = sorted({STRATEGY_META.get(k, {}).get("group", "其它") for k in keys})
        e["strategy_text"] = "+".join(e["strategy_groups"])
    # 注意：`sets` 是后面"源之间的关系"才定义的变量，这里必须自己算一份 ——
    # 第一版直接引用 `sets` 触发 NameError，`/api/calendar/day` 直接 500
    # （页面表现为"选股日历整页报错"）。
    local_sets = {src: set(g["stock_code"].astype(str)) for src, g in sub.groupby("source")}
    # 汇总按**策略组**合并（notify 与 preselected 都属于"模型策略"，分成两行会让人以为有 4 种策略）
    grouped_members: dict[str, set] = {}
    grouped_sources: dict[str, list] = {}
    for key, group_meta in STRATEGY_META.items():
        members = local_sets.get(key)
        if not members:
            continue
        group = group_meta["group"]
        grouped_members.setdefault(group, set()).update(members)
        grouped_sources.setdefault(group, []).append(key)
    strategy_summary = [{"group": group, "n": len(members), "stocks": sorted(members),
                         "sources": grouped_sources.get(group, []),
                         # label 只留策略组名：notify/preselected 的 label 前缀都是"模型策略"，
                         # 拼起来会变成"模型策略 + 模型策略"（实测）。
                         "label": group}
                        for group, members in grouped_members.items()]

    # ── 源之间的关系（2026-10-09 新增）────────────────────────────────────
    # 用户问"明细列表里 notify / preselected 不是重复么？为什么都要展示？" ——
    # 实测答案是"分情况"：全台账里 preselected 覆盖 20 个决策日、notify 覆盖 6 个，
    # **只有 3 天两者同时存在**；其中 2 天几乎不相交（只重叠 1 只），
    # 只有 2026-10-08 那一天两边完全相同（PK 一只没淘汰）。
    # 既然"完全重叠"会让人以为数据重复，就把关系算出来明说，而不是让人猜。
    sets = local_sets          # 与策略分组共用一份，避免重复 groupby
    relations = []
    for a in strategies:
        for b in strategies:
            if a >= b:
                continue
            sa, sb = sets[a], sets[b]
            both, only_a, only_b = len(sa & sb), len(sa - sb), len(sb - sa)
            if sa == sb:
                relation = "identical"
            elif only_a == 0:
                relation = "subset"          # a 完全包含于 b
            elif only_b == 0:
                relation = "superset"
            elif not both:
                relation = "disjoint"
            else:
                relation = "partial"
            relations.append({"a": a, "b": b, "relation": relation,
                              "both": both, "only_a": only_a, "only_b": only_b})

    # 明细行也带上策略字段：用户看的是"每只票是哪种策略选出来的"，
    # 不管他在"明细列表"还是"多策略对比"页签里，都得能直接看到。
    pick_groups = {e["stock_code"]: (e.get("strategy_groups") or []) for e in compare_rows}
    pick_labels = {e["stock_code"]: (e.get("strategy_labels") or []) for e in compare_rows}
    pick_keys = {e["stock_code"]: (e.get("strategy_keys") or []) for e in compare_rows}
    pick_text = {e["stock_code"]: (e.get("strategy_text") or "") for e in compare_rows}
    for r in rows:
        code = str(r["stock_code"])
        r["strategy_groups"] = pick_groups.get(code, [])
        r["strategy_labels"] = pick_labels.get(code, [])
        r["strategy_keys"] = pick_keys.get(code, [])
        r["strategy_text"] = pick_text.get(code, "")

    return {
        "date": date, "source": source or "all", "count": len(rows),
        "strategies": strategies,
        "strategy_meta": STRATEGY_META,
        "strategy_summary": strategy_summary,
        "compare": compare_rows,
        "source_relations": relations,
        "per_source": {k: int(v) for k, v in sub["source"].value_counts().items()},
        "mean": {f"r{h}": _num(sub[f"r{h}"].mean()) for h in HORIZONS},
        "mean_excess": {f"r{h}": _num(sub[f"excess_r{h}"].mean()) for h in HORIZONS},
        "rows": rows,
    }


@router.get("/performance")
def performance(source: str = Query("", description="留空=全部来源；notify=最终名单口径")) -> dict:
    """按决策日聚合的业绩 + 逐期等权复利曲线（每期用该决策日组合的 r5）。"""
    frame = _load()
    if frame.empty:
        raise HTTPException(status_code=404, detail="台账不存在")
    sub = frame[frame["source"] == source] if source else frame
    if sub.empty:
        raise HTTPException(status_code=404, detail=f"来源 {source} 无记录")
    rows, curve, equity, peak, max_dd = [], [], 1.0, 1.0, 0.0
    for date, group in sub.groupby("decision_date"):
        stats = {"date": str(date), "n": int(len(group)),
                 "n_with_return": int(group["r5"].notna().sum())}
        for h in HORIZONS:
            stats[f"mean_r{h}"] = _num(group[f"r{h}"].mean())
            stats[f"mean_excess_r{h}"] = _num(group[f"excess_r{h}"].mean())
            stats[f"win_rate_r{h}"] = _num((group[f"r{h}"] > 0).mean()
                                           if group[f"r{h}"].notna().any() else None)
        rows.append(stats)
        if pd.notna(stats["mean_r5"]):
            equity *= (1 + stats["mean_r5"])
            peak = max(peak, equity)
            max_dd = min(max_dd, equity / peak - 1)
            curve.append({"date": str(date), "equity": round(equity, 6),
                          "period_return": round(stats["mean_r5"], 6)})
    rows.sort(key=lambda r: r["date"])
    valid = sub[sub["r5"].notna()]
    overall = {
        "n_picks": int(len(sub)), "n_dates": int(sub["decision_date"].nunique()),
        "n_evaluated": int(len(valid)),
        **{f"mean_r{h}": _num(sub[f"r{h}"].mean()) for h in HORIZONS},
        **{f"mean_excess_r{h}": _num(sub[f"excess_r{h}"].mean()) for h in HORIZONS},
        **{f"win_rate_r{h}": _num((sub[f"r{h}"] > 0).mean() if sub[f"r{h}"].notna().any() else None)
           for h in HORIZONS},
        "equity_multiple": round(equity, 6), "max_drawdown": round(max_dd, 6),
        "note": "逐期 = 每个决策日等权组合；期与期之间按顺序复利（决策日不连续，仅作相对比较）",
    }
    by_source = []
    for name, group in frame.groupby("source"):
        valid_g = group[group["r5"].notna()]
        by_source.append({
            "source": name, "n_picks": int(len(group)),
            "n_dates": int(group["decision_date"].nunique()),
            "date_range": [str(group["decision_date"].min()), str(group["decision_date"].max())],
            "n_evaluated": int(len(valid_g)),
            "mean_r5": _num(group["r5"].mean()),
            "mean_excess_r5": _num(group["excess_r5"].mean()),
            "win_rate_r5": _num((group["r5"] > 0).mean() if group["r5"].notna().any() else None),
            "mean_r1": _num(group["r1"].mean()),
        })
    by_source.sort(key=lambda r: r["n_dates"], reverse=True)
    by_stage = []
    for stage, group in sub.groupby("stage_label"):
        by_stage.append({
            "stage_label": stage, "n": int(len(group)),
            "mean_r5": _num(group["r5"].mean()), "mean_excess_r5": _num(group["excess_r5"].mean()),
            "win_rate_r5": _num((group["r5"] > 0).mean() if group["r5"].notna().any() else None),
            "mean_r20": _num(group["r20"].mean()),
        })
    by_stage.sort(key=lambda r: -(r["mean_excess_r5"] if r["mean_excess_r5"] is not None else -9))
    return {"source": source or "all", "overall": overall, "by_date": rows, "curve": curve,
            "by_source": by_source, "by_stage": by_stage}
