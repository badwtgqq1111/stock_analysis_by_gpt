#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""选股日历复盘：把"所有选股历史"汇总成一份可读报告 + 若干 csv（2026-10-09）。

数据源：output/results_cn/selection_ledger.parquet（scripts/build_selection_ledger.py 生成，
前端「选股日历」读的就是它）。每行 = 某个决策日、某个来源、某只票，已经带上**已实现**的前瞻收益
（r1/r5/r10/r20）与基准/超额（bench_* / excess_*，基准 = 当日全市场等权，见台账脚本注释）。

本脚本回答三个问题：
  1. 选股历史整体长什么样（来源、决策日、覆盖率）；
  2. **哪些股票近期表现较好**（个股榜 + 近期榜 + 平均超额）；
  3. 优化方向：把可量化的特征（模型排名、PK 权重、阶段标签、通道位置、量能、ATR/波动/回撤）
     与已实现超额收益做分层对比，看哪一档真的有钱赚、哪一档是负贡献。

口径与诚实性：
  * 收益是"已实现"的，但**每个来源的样本都很小**（notify 只有 6 个决策日），所有表格都带 n，
    不要把小 n 的均值当结论；
  * 同一只票同一天可能在多个来源里出现（候选池 + PK 名单），个股榜按 (决策日, 代码) **去重**，
    来源取优先级 notify > arm > preselected > survey_gate > survey_raw；
  * 基准 = 当日全市场等权（不是中证全指等真实指数，指数数据在 backfill_cn_benchmark.py 里另有 4 个）。

用法:
  uv run python scripts/selection_history_review.py                   # 默认最近 90 天
  uv run python scripts/selection_history_review.py --recent-days 45 --top 20
输出:
  output/results_cn/selection_review/<run_ts>/{review.md, by_source.csv, by_date.csv,
                                              stock_leaderboard.csv, recent_leaderboard.csv,
                                              segment_vs_excess.csv}
"""
from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "output/results_cn/selection_ledger.parquet"
BASIC = ROOT / "assets/data/meta/cn_stock_basic.parquet"
OUT_ROOT = ROOT / "output/results_cn/selection_review"
SOURCE_PRIORITY = ["notify", "arm", "preselected", "survey_gate", "survey_raw"]
HORIZONS = (1, 5, 10, 20)


# ── 从文本字段里抠数：台账里的 stage_evidence / risk_metrics_calc 是给人看的句子 ──
def _num(text, pattern: str) -> float:
    if not isinstance(text, str):
        return np.nan
    m = re.search(pattern + r"\s*(-?\d+(?:\.\d+)?)", text)
    return float(m.group(1)) if m else np.nan


def _cn_names() -> dict:
    """`ts_code → 名称`。台账里 survey_* 两个来源的名称**从来没回填过**（实测填充率 0%），
    190 个代码全都能从注册表补上 —— 这里补，不改台账本身。"""
    if not BASIC.exists():
        return {}
    frame = pd.read_parquet(BASIC, columns=["ts_code", "name"]).drop_duplicates("ts_code")
    return dict(zip(frame["ts_code"].astype(str), frame["name"].fillna("").astype(str)))


def enrich(frame: pd.DataFrame, dedup: bool = True) -> pd.DataFrame:
    out = frame.copy()
    names = _cn_names()
    missing = out["name"].isna()
    if missing.any() and names:
        out.loc[missing, "name"] = out.loc[missing, "stock_code"].astype(str).map(names).fillna("")
        out["name"] = out["name"].replace({"": np.nan})
    out["decision_date"] = pd.to_datetime(out["decision_date"], errors="coerce")
    out["channel_pct"] = out["stage_evidence"].map(lambda t: _num(t, r"通道位置"))
    out["pctile_52w"] = out["stage_evidence"].map(lambda t: _num(t, r"52\s*周分位"))
    out["vol_ratio_5_20"] = out["stage_evidence"].map(lambda t: _num(t, r"量能\s*5/20\s*日"))
    out["vol20_pct"] = out["risk_metrics_calc"].map(lambda t: _num(t, r"20日波动率"))
    out["dd60_pct"] = out["risk_metrics_calc"].map(lambda t: _num(t, r"60日最大回撤"))
    out["atr14_pct"] = out["risk_metrics_calc"].map(lambda t: _num(t, r"ATR14"))
    out["weight"] = pd.to_numeric(out["weight"], errors="coerce")
    out["rank"] = pd.to_numeric(out["rank"], errors="coerce")
    out["model_score"] = pd.to_numeric(out["model_score"], errors="coerce")
    for h in HORIZONS:
        out[f"r{h}"] = pd.to_numeric(out[f"r{h}"], errors="coerce")
        if f"excess_r{h}" in out.columns:
            out[f"excess_r{h}"] = pd.to_numeric(out[f"excess_r{h}"], errors="coerce")
    out["_pri"] = out["source"].map({s: i for i, s in enumerate(SOURCE_PRIORITY)}).fillna(99)
    out["_all_sources"] = out.groupby(["decision_date", "stock_code"])["source"].transform(
        lambda s: "|".join(sorted(set(s))))
    if not dedup:
        # 规则级 A/B 需要**每个来源自己的行**：去重会把 preselected 的行换成 notify 的行，
        # 2026-10-08 因此整整少一天（实测）。所以这里允许关掉。
        return out.drop(columns=["_pri"]).sort_values(["decision_date", "stock_code"]).reset_index(drop=True)
    # 同一决策日同一只票只留一条（来源优先级），免得个股榜把候选池+名单重复计数
    return (out.sort_values("_pri").drop_duplicates(["decision_date", "stock_code"], keep="first")
            .drop(columns=["_pri"]).sort_values(["decision_date", "stock_code"]).reset_index(drop=True))


def bucket_table(frame: pd.DataFrame, col: str, bins: list[float], labels: list[str]) -> pd.DataFrame:
    sub = frame.dropna(subset=[col, "r5"]).copy()
    if sub.empty:
        return pd.DataFrame()
    sub["bucket"] = pd.cut(sub[col], bins=bins, labels=labels, include_lowest=True)
    rows = []
    for name, g in sub.groupby("bucket", observed=True):
        rows.append({"特征档": str(name), "n": len(g),
                     "r5均值": float(g["r5"].mean()), "超额均值": float(g["excess_r5"].mean()),
                     "胜率": float((g["r5"] > 0).mean()),
                     "r20均值": float(g["r20"].mean()) if g["r20"].notna().any() else np.nan,
                     "决策日数": int(g["decision_date"].nunique())})
    return pd.DataFrame(rows)


def fmt_pct(x, nd=2):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "—"
    return f"{x * 100:+.{nd}f}%"


def main() -> int:
    ap = argparse.ArgumentParser(description="选股历史复盘")
    ap.add_argument("--recent-days", type=int, default=90, help="近期窗口（日历日，相对最新决策日）")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    raw = pd.read_parquet(LEDGER)
    ledger = enrich(raw, dedup=True)      # 榜单/分层：同一天同一票只算一次
    latest = ledger["decision_date"].max()
    recent_from = latest - pd.Timedelta(days=int(args.recent_days))
    recent = ledger[ledger["decision_date"] >= recent_from]

    out_dir = Path(args.out_dir) if args.out_dir else OUT_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. 来源汇总 ─────────────────────────────────────────────
    src_rows = []
    for s in SOURCE_PRIORITY:
        g = ledger[ledger["source"] == s]
        if g.empty:
            continue
        per = g.dropna(subset=["r5"]).groupby("decision_date")["r5"].mean()
        chained = float((1 + per).prod() - 1) if len(per) else np.nan
        src_rows.append({
            "来源": s, "行数": len(g), "决策日": int(g["decision_date"].nunique()),
            "首次": g["decision_date"].min().strftime("%Y-%m-%d"),
            "最近": g["decision_date"].max().strftime("%Y-%m-%d"),
            "可算r5比例": f"{g['r5'].notna().mean():.0%}",
            "r1均值": fmt_pct(g["r1"].mean()), "r5均值": fmt_pct(g["r5"].mean()),
            "r10均值": fmt_pct(g["r10"].mean()), "r20均值": fmt_pct(g["r20"].mean()),
            "超额r5均值": fmt_pct(g["excess_r5"].mean()),
            "逐股胜率(r5)": f"{(g['r5'] > 0).mean():.0%}" if g["r5"].notna().any() else "—",
            "按日胜率(r5)": f"{(per > 0).mean():.0%}" if len(per) else "—",
            "串联累计(r5)": fmt_pct(chained),
        })
    by_source = pd.DataFrame(src_rows)
    by_source.to_csv(out_dir / "by_source.csv", index=False)

    # ── 2. 决策日明细（最近优先） ────────────────────────────────
    date_rows = []
    for (d, s), g in ledger.groupby(["decision_date", "source"]):
        g5 = g.dropna(subset=["r5"])
        date_rows.append({
            "决策日": d.strftime("%Y-%m-%d"), "来源": s, "只数": len(g),
            "r1": fmt_pct(g["r1"].mean()), "r5": fmt_pct(g["r5"].mean()),
            "超额r5": fmt_pct(g["excess_r5"].mean()),
            "r5胜率": f"{(g5['r5'] > 0).mean():.0%}" if len(g5) else "—",
            "r10": fmt_pct(g["r10"].mean()) if g["r10"].notna().any() else "—",
            "r20": fmt_pct(g["r20"].mean()) if g["r20"].notna().any() else "—",
            "名称": "、".join(g["name"].dropna().astype(str).head(6)),
        })
    by_date = pd.DataFrame(date_rows).sort_values("决策日", ascending=False)
    by_date.to_csv(out_dir / "by_date.csv", index=False)

    # ── 3. 个股榜（全历史，去重后） ──────────────────────────────
    def leaderboard(frame: pd.DataFrame) -> pd.DataFrame:
        rows = []
        for code, g in frame.groupby("stock_code"):
            g5 = g.dropna(subset=["r5"])
            last = g["decision_date"].max()
            last_row = g[g["decision_date"] == last].iloc[0]
            rows.append({
                "代码": code, "名称": last_row.get("name") or "",
                "上榜次数": len(g), "首次": g["decision_date"].min().strftime("%Y-%m-%d"),
                "最近": last.strftime("%Y-%m-%d"),
                "来源": last_row["_all_sources"],
                "最近状态": last_row.get("stage_label"),
                "r1均值": g["r1"].mean(), "r5均值": g5["r5"].mean() if len(g5) else np.nan,
                "r10均值": g["r10"].mean() if g["r10"].notna().any() else np.nan,
                "r20均值": g["r20"].mean() if g["r20"].notna().any() else np.nan,
                "超额r5均值": g5["excess_r5"].mean() if len(g5) else np.nan,
                "r5胜率": float((g5["r5"] > 0).mean()) if len(g5) else np.nan,
                "可算r5次数": len(g5),
            })
        out = pd.DataFrame(rows)
        return out.sort_values(["可算r5次数", "超额r5均值"], ascending=False)

    board_all = leaderboard(ledger)
    board_all.to_csv(out_dir / "stock_leaderboard.csv", index=False)

    board_recent = leaderboard(recent)
    board_recent = board_recent.rename(columns={"上榜次数": "窗口内上榜次数", "首次": "窗口内首次",
                                                "最近": "窗口内最近"})
    board_recent.to_csv(out_dir / "recent_leaderboard.csv", index=False)

    # 近期榜（近期窗口内至少 1 次上榜，按超额排序）
    recent_rank = (board_recent.dropna(subset=["超额r5均值"])
                   .sort_values("超额r5均值", ascending=False))
    recent_losers = recent_rank.tail(args.top).iloc[::-1]

    # 全历史里的"常客"：上榜 >= 3 次且可算 r5 >= 2 次
    regulars = board_all[(board_all["上榜次数"] >= 3) & (board_all["可算r5次数"] >= 2)]

    # ── 4. 特征分层（优化方向的统计支撑） ───────────────────────
    seg_frames = []

    def add_seg(title: str, table: pd.DataFrame):
        if table is None or table.empty:
            return
        table = table.copy()
        table.insert(0, "分层维度", title)
        seg_frames.append(table)

    stage = (ledger.dropna(subset=["r5"]).groupby("stage_label")
             .agg(n=("r5", "size"), **{"r5均值": ("r5", "mean"), "超额均值": ("excess_r5", "mean"),
                                       "胜率": ("r5", lambda s: float((s > 0).mean())),
                                       "r20均值": ("r20", "mean"),
                                       "决策日数": ("decision_date", "nunique")})
             .reset_index().rename(columns={"stage_label": "特征档"}))
    stage = stage.rename(columns={"n": "n"}).assign(n=lambda d: d["n"].astype(int))
    add_seg("阶段（stage_label）", stage)

    add_seg("60日通道位置", bucket_table(ledger, "channel_pct",
                                        [-100, 20, 40, 60, 80, 100],
                                        ["0-20%（低位）", "20-40%", "40-60%", "60-80%", "80-100%（高位）"]))
    add_seg("量能 5/20 日", bucket_table(ledger, "vol_ratio_5_20",
                                        [0, 0.8, 1.0, 1.3, 2.0, 99],
                                        ["<0.8（缩量）", "0.8-1.0", "1.0-1.3", "1.3-2.0", ">2.0（放量）"]))
    add_seg("ATR14（%）", bucket_table(ledger, "atr14_pct", [0, 2, 3, 4, 6, 99],
                                      ["<2", "2-3", "3-4", "4-6", ">6"]))
    add_seg("20日波动率（%）", bucket_table(ledger, "vol20_pct", [0, 2, 3, 4, 6, 99],
                                          ["<2", "2-3", "3-4", "4-6", ">6"]))
    add_seg("60日最大回撤（%）", bucket_table(ledger, "dd60_pct", [-100, -30, -20, -10, 0, 100],
                                            ["<-30（深跌）", "-30~-20", "-20~-10", "-10~0", ">0"]))
    add_seg("模型 rank", bucket_table(ledger[ledger["source"].isin(["preselected", "notify"])], "rank",
                                     [0, 3, 6, 10, 20, 10 ** 9],
                                     ["1-3", "4-6", "7-10", "11-20", ">20"]))
    add_seg("52周分位", bucket_table(ledger, "pctile_52w", [0, 20, 40, 60, 80, 100],
                                    ["0-20%", "20-40%", "40-60%", "60-80%", "80-100%"]))

    # PK 权重是否有预测力：notify 里 weight>0（真下单）vs weight==0（只是名单）
    ntf = ledger[ledger["source"].isin(["notify", "arm"])].dropna(subset=["weight"])
    if not ntf.empty:
        ntf = ntf.copy()
        ntf["特征档"] = np.where(ntf["weight"] > 0, "PK 给了权重（真下单）", "PK 0 权重（只看不买）")
        add_seg("PK 权重", (ntf.dropna(subset=["r5"]).groupby("特征档")
                          .agg(n=("r5", "size"), **{"r5均值": ("r5", "mean"),
                                                    "超额均值": ("excess_r5", "mean"),
                                                    "胜率": ("r5", lambda s: float((s > 0).mean())),
                                                    "r20均值": ("r20", "mean"),
                                                    "决策日数": ("decision_date", "nunique")})
                          .reset_index()))

    seg = pd.concat(seg_frames, ignore_index=True) if seg_frames else pd.DataFrame()
    if not seg.empty:
        seg = seg[["分层维度", "特征档", "n", "决策日数", "r5均值", "超额均值", "胜率", "r20均值"]]
        seg.to_csv(out_dir / "segment_vs_excess.csv", index=False)

    # ── 4b. 关键假设的"按决策日"稳健性（同一票连续上榜 ⇒ 行与行不独立，必须按日聚合再看） ──
    def sign_test(mask: pd.Series, name: str) -> dict:
        sub = ledger[mask & ledger["r5"].notna()]
        rest = ledger[(~mask) & ledger["r5"].notna()]
        per_day_in = sub.groupby("decision_date")["excess_r5"].mean()
        per_day_out = rest.groupby("decision_date")["excess_r5"].mean()
        common = per_day_in.index.intersection(per_day_out.index)
        if len(common) < 2:
            return {"假设": name, "覆盖决策日": len(common), "结论": "决策日太少，不判"}
        diff = (per_day_in[common] - per_day_out[common]).dropna()
        worse = int((diff < 0).sum())
        better = int((diff > 0).sum())
        n = worse + better
        # 双侧符号检验（二项），n 小时只作参考
        from math import comb
        p_two = 2 * sum(comb(n, k) for k in range(0, min(worse, better) + 1)) / (2 ** n) if n else 1.0
        return {"假设": name, "覆盖决策日": int(n),
                "该档更差的天数": worse, "该档更好的天数": better,
                "中位差(该档-其余)": float(diff.median()),
                "符号检验p": round(min(p_two, 1.0), 3),
                "结论": "同向（更差）" if worse / max(n, 1) >= 0.7 else
                        ("同向（更好）" if better / max(n, 1) >= 0.7 else "不一致／样本不足")}

    hypotheses = [
        ("阶段=底部反转尝试（超额为负）", ledger["stage_label"].eq("底部反转尝试")),
        ("60日通道位置 <20%（低位）", ledger["channel_pct"] < 20),
        ("ATR14 >4%", ledger["atr14_pct"] > 4),
        ("20日波动率 >4%", ledger["vol20_pct"] > 4),
        ("量能 5/20 日 1.3~2.0", ledger["vol_ratio_5_20"].between(1.3, 2.0)),
        ("52周分位 >=80%", ledger["pctile_52w"] >= 80),
        ("模型 rank >20", ledger["rank"] > 20),
        ("阶段=顶部区域（涨透/滞涨）", ledger["stage_label"].eq("顶部区域（涨透/滞涨）")),
    ]
    robust = pd.DataFrame([sign_test(mask, name) for name, mask in hypotheses])
    robust.to_csv(out_dir / "hypothesis_sign_test.csv", index=False)

    # ── 5. markdown 报告 ────────────────────────────────────────
    lines: list[str] = []
    lines.append(f"# 选股日历复盘（全部选股历史）\n")
    lines.append(f"- 台账：`{LEDGER.relative_to(ROOT)}`（前端「选股日历」的数据源）")
    lines.append(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}｜决策日 **{ledger['decision_date'].nunique()}** 个"
                 f"｜台账原始 **{len(raw):,}** 行（同一天多来源）｜按 (决策日,代码) 去重后 **{len(ledger):,}** 组")
    lines.append(f"- 区间：{ledger['decision_date'].min():%Y-%m-%d} ~ {latest:%Y-%m-%d}"
                 f"｜\"近期\"窗口：最近 **{args.recent_days}** 天（≥ {recent_from:%Y-%m-%d}）")
    lines.append("- 收益口径：rN = 未来 N 个交易日收盘收益（**已实现**）；超额 = 减去当日**全市场等权**基准。\n")

    lines.append("## 1. 来源汇总\n")
    lines.append(by_source.to_markdown(index=False))
    lines.append("\n> 提醒：`preselected`（候选池）样本最长；`notify` 是真正下单口径但只有 6 个决策日，"
                 "均值随时会翻脸，别单看它。\n")

    lines.append(f"## 2. 个股榜 ── 全历史（按可算 r5 次数、超额排序，取前 {args.top}）\n")
    lines.append(board_all.head(args.top).to_markdown(index=False, floatfmt=".4f"))

    lines.append(f"\n## 3. 近期表现较好的个股（{recent_from:%Y-%m-%d} 之后上榜，按超额 r5 排序，前 {args.top}）\n")
    if recent_rank.empty:
        lines.append("（近期窗口内没有可算 r5 的记录）")
    else:
        lines.append(recent_rank.head(args.top).to_markdown(index=False, floatfmt=".4f"))
    repeat = recent_rank[recent_rank["窗口内上榜次数"] >= 2]
    lines.append(f"\n### 3b. 其中窗口内**反复上榜（≥2 次）**的（更值得看，单次上榜的高光多半是运气）\n")
    lines.append(repeat.head(args.top).to_markdown(index=False, floatfmt=".4f") if not repeat.empty else "（无）")
    lines.append('\n> 窗口内只上榜 1 次的票占多数，它的「均值」= 单次收益，**不能当「这只票好」的证据**。\n')

    lines.append(f"\n## 4. 近期表现较差的个股（同期，按超额 r5 从低到高，前 {args.top}）\n")
    if recent_losers.empty:
        lines.append("（无）")
    else:
        lines.append(recent_losers.to_markdown(index=False, floatfmt=".4f"))

    lines.append("\n## 5. 反复上榜的票（上榜 ≥3 次且可算 r5 ≥2 次）\n")
    lines.append(regulars.to_markdown(index=False, floatfmt=".4f") if not regulars.empty else "（无）")

    lines.append("\n## 6. 特征 × 已实现超额（分层，n = 行数）\n")
    if seg.empty:
        lines.append("（无可分层数据）")
    else:
        for dim, g in seg.groupby("分层维度", sort=False):
            lines.append(f"### {dim}\n")
            show = g.drop(columns=["分层维度"]).copy()
            for c in ["r5均值", "超额均值", "r20均值"]:
                show[c] = show[c].map(lambda v: fmt_pct(v))
            show["胜率"] = show["胜率"].map(lambda v: "—" if pd.isna(v) else f"{v * 100:.0f}%")
            lines.append(show.to_markdown(index=False))
            lines.append("")

    lines.append("## 6b. 关键假设的稳健性（按决策日聚合 + 符号检验）\n")
    lines.append("行与行不独立（同一只票会连续多日上榜），所以把每个假设缩成"
                 "「每个决策日：该档超额均值 − 其余档超额均值」，再看同向天数。\n")
    lines.append(robust.to_markdown(index=False, floatfmt=".4f"))
    lines.append("\n> `符号检验p` 是双侧精确二项检验；决策日只有 15~30 个，p<0.05 才算有点样子，"
                 "其余一律当**待验证假设**。\n")

    lines.append(f"## 7. 决策日明细（最近 25 条）\n")
    lines.append(by_date.head(25).to_markdown(index=False))

    (out_dir / "review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[review] 决策日 {ledger['decision_date'].nunique()} 个 / {len(ledger):,} 行 → {out_dir}")
    print(f"[review] 近期可排序个股 {len(recent_rank)} 只；常客 {len(regulars)} 只；分层 {len(seg)} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
