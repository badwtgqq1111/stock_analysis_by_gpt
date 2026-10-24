#!/usr/bin/env python3
"""概念排行（与行业热度排名同口径）：广度 + 涨幅 + 成交额占比 + 涨停密度。

概念来源（按优先级合并）：
  1. 新浪概念成分（175 个，当前快照，覆盖 3,265 只）
  2. iFinD 概念快照（`cn_concept_members_ifind.parquet`，人工指定概念，如 人形机器人/CPO）
  3. 之后可扩展到 iFinD 全量概念（375 个，逐个查询，约 12 分钟/日）

个股无概念的处理（本脚本只统计概念成员，不强行归类）：
  * 未覆盖股票不参与概念排行；
  * 覆盖率单独输出，供后续用 LLM（DeepSeek）补标或建"无概念"桶。

用法：
  uv run python scripts/concept_ranking.py --realtime
  uv run python scripts/concept_ranking.py --realtime --min-members 10 --top 20
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
MEMBERSHIP = ROOT / "assets/data/meta/cn_board_membership.parquet"
IFIND_MEMBERS = ROOT / "assets/data/derived/cn_concept_members_ifind.parquet"
OUT = ROOT / "output/results_cn/concept_ranking"
INDEX_LIKE = ("科创50", "上证50", "沪深300", "中证500", "中证1000", "创业板指", "深证成指",
              "北证50", "上证指数", "深证综指", "中证全指", "科创板", "创业板综")
FAMILY = {
    "人形机器人": "机器人", "机器人概念": "机器人", "具身智能": "机器人", "智能机器": "机器人",
    "CPO": "光通信", "光模块": "光通信", "存储芯片": "半导体", "华为海思": "半导体",
    "固态电池": "新能源", "锂电池": "新能源", "光伏": "新能源",
}


def realtime(codes: list[str], batch: int = 20) -> pd.DataFrame:
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    rows = []
    for i in range(0, len(codes), batch):
        chunk = codes[i:i + batch]
        syms = [("sh" if c.endswith(".SH") else ("bj" if c.endswith(".BJ") else "sz")) + c[:6] for c in chunk]
        for _ in range(3):
            try:
                r = s.get("https://qt.gtimg.cn/q=" + ",".join(syms), timeout=10)
                r.encoding = "gbk"
                got = 0
                for line in r.text.strip().split(";"):
                    if "=" not in line:
                        continue
                    v = line.split("=", 1)[1].strip().strip('"').split("~")
                    if len(v) > 37 and v[3]:
                        rows.append({"stock_code": v[2] + (".SH" if v[2].startswith(("6", "9")) else ".SZ"),
                                     "name": v[1], "pct": float(v[32]), "amount": float(v[37]) * 1e4})
                        got += 1
                if got:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.3)
    return pd.DataFrame(rows).drop_duplicates("stock_code")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-members", type=int, default=10)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--realtime", action="store_true", help="用实时/收盘报价（默认读最近日线）")
    args = ap.parse_args()

    _cal = ROOT / "assets/data/derived/cn_concept_members_scored.parquet"
    if _cal.exists():
        members = pd.read_parquet(_cal)[["concept", "stock_code", "source"]].drop_duplicates()
    else:
        frames = []
        m = pd.read_parquet(MEMBERSHIP)
        m = m[m.taxonomy == "sina_concept"][["board", "code"]].rename(columns={"board": "concept"})
        m["stock_code"] = m["code"].map(lambda x: x + (".SH" if str(x).startswith(("6", "9")) else ".SZ"))
        m["source"] = "sina"
        frames.append(m[["concept", "stock_code", "source"]])
        if IFIND_MEMBERS.exists():
            f = pd.read_parquet(IFIND_MEMBERS)
            f = f[f.snapshot_date == f.snapshot_date.max()][["concept", "stock_code"]]
            f["source"] = "ifind"
            frames.append(f)
        members = pd.concat(frames, ignore_index=True).drop_duplicates(["concept", "stock_code"])

    codes = sorted(members.stock_code.unique())
    q = realtime(codes)
    if q.empty:
        print("[concept] 实时行情失败")
        return 1
    q["amount"] = q["amount"].fillna(0)
    total_amt = q["amount"].sum()

    df = members.merge(q, on="stock_code", how="inner")
    g = df.groupby("concept")
    rank = pd.DataFrame({
        "n": g.size(),
        "chg": g["pct"].mean(),
        "median": g["pct"].median(),
        "breadth": g["pct"].apply(lambda x: float((x > 0).mean())),
        "limit_up": g["pct"].apply(lambda x: int((x >= 9.8).sum())),
        "amount": g["amount"].sum(),
        "pct_2": g["pct"].apply(lambda x: float((x >= 2).mean())),
    }).reset_index()
    rank = rank[rank["n"] >= args.min_members]
    rank = rank[~rank["concept"].isin(INDEX_LIKE)]
    rank = rank[~rank["concept"].astype(str).str.contains("指数|成指|综指", na=False)]
    rank["family"] = rank["concept"].map(lambda c: FAMILY.get(c, c))
    rank["amount_share"] = rank["amount"] / total_amt
    rank["limit_ratio"] = rank["limit_up"] / rank["n"]

    for col in ["chg", "breadth", "amount_share", "limit_ratio"]:
        rank[f"r_{col}"] = rank[col].rank(pct=True)
    rank["score"] = (0.40 * rank["r_chg"] + 0.30 * rank["r_breadth"]
                     + 0.20 * rank["r_amount_share"] + 0.10 * rank["r_limit_ratio"])
    rank = rank.sort_values("score", ascending=False).reset_index(drop=True)
    rank["rank"] = rank.index + 1
    # family 内只保留最强的一个代表，避免 人形机器人/机器人概念/具身智能 霸榜
    rank["family_rank"] = rank.groupby("family")["score"].rank(ascending=False, method="first")
    rank["is_family_lead"] = rank["family_rank"] == 1

    OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    rank.to_csv(OUT / f"concept_rank_{stamp}.csv", index=False)
    # 展示口径：同一 family 只留最强代表，避免 人形机器人/机器人概念/具身智能 霸榜
    # 展示文件：
    #   latest_all.csv = 全部概念（含同 family 的重复主题），前端排行用
    #   latest_top.csv = family 去重后的前 100，兼容旧调用
    rank.to_csv(OUT / "latest_all.csv", index=False)
    rank[rank["is_family_lead"]].head(100).to_csv(OUT / "latest_top.csv", index=False)

    all_stocks = pd.read_parquet(ROOT / "assets/data/meta/cn_stock_basic.parquet")
    col = "list_status" if "list_status" in all_stocks.columns else "status"
    live = all_stocks[all_stocks[col] == "L"]
    live = live[live.ts_code.str.match(r"^\d{6}\.(SH|SZ)$")]
    covered = len(set(members.stock_code) & set(live.ts_code))
    uncovered = sorted(set(live.ts_code) - set(members.stock_code))
    coverage = {"stocks": int(len(live)), "covered": int(covered), "uncovered": len(uncovered),
                "coverage_pct": round(covered / max(len(live), 1) * 100, 1),
                "uncovered_examples": uncovered[:10],
                "concepts": int(rank.shape[0])}
    (OUT / "coverage.json").write_text(json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8")

    print(rank[rank["is_family_lead"]].head(args.top)[["rank", "concept", "family", "n", "chg", "breadth", "limit_up", "amount_share", "score"]]
          .round(3).to_string(index=False))
    print(json.dumps(coverage, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
