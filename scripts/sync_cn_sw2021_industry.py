#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""申万 2021 一级行业成分落地（`assets/data/meta/cn_sw2021_industry.parquet`）。

为什么要这份数据
----------------
行业轮动研究用的是 `cn_stock_basic.industry`（110 个 Tushare 行业，**当前分类、非 PIT**）。
复现文献（MG1999 / Gong2016）时最容易被审稿人挑的两点之一就是"行业分类口径"：
申万一级 31 个行业是 A 股卖方研究的标准口径，且仓库里已有抓取器
（`data/ingest/providers/cn_sw2021.py`，来源乐咕乐股公开页面），只是**一直没有落成 parquet**。
本脚本把它落地，作为行业轮动的第二套口径（对照/稳健性检验用）。

用法：
  uv run python scripts/sync_cn_sw2021_industry.py            # 补齐缺失行业（每次 4 个的小预算）
  uv run python scripts/sync_cn_sw2021_industry.py --dry-run  # 只看目录
输出：
  assets/data/meta/cn_sw2021_industry.parquet（industry_code, industry_name, stock_code, name, snapshot）
  output/pipeline_reports/sw2021_sync/<date>.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

META = ROOT / "assets/data/meta/cn_sw2021_industry.parquet"

# 申万 2021 一级行业（31 个），代码取自申万官方口径；仅在目录页抓取失败时兜底使用
SW2021_LEVEL1_FALLBACK = [
    ("801010.SI", "农林牧渔"), ("801030.SI", "基础化工"), ("801040.SI", "钢铁"),
    ("801050.SI", "有色金属"), ("801080.SI", "电子"), ("801110.SI", "家用电器"),
    ("801120.SI", "食品饮料"), ("801130.SI", "纺织服饰"), ("801140.SI", "轻工制造"),
    ("801150.SI", "医药生物"), ("801160.SI", "公用事业"), ("801170.SI", "交通运输"),
    ("801180.SI", "房地产"), ("801200.SI", "商贸零售"), ("801210.SI", "社会服务"),
    ("801230.SI", "综合"), ("801710.SI", "建筑材料"), ("801720.SI", "建筑装饰"),
    ("801730.SI", "电力设备"), ("801740.SI", "国防军工"), ("801750.SI", "计算机"),
    ("801760.SI", "传媒"), ("801770.SI", "通信"), ("801780.SI", "银行"),
    ("801790.SI", "非银金融"), ("801880.SI", "汽车"), ("801890.SI", "机械设备"),
    ("801950.SI", "煤炭"), ("801960.SI", "石油石化"), ("801970.SI", "环保"),
    ("801980.SI", "美容护理"),
]
REPORT_DIR = ROOT / "output/pipeline_reports/sw2021_sync"


def main() -> int:
    parser = argparse.ArgumentParser(description="申万 2021 一级行业成分落地")
    parser.add_argument("--dry-run", action="store_true", help="只抓目录，不抓成分")
    parser.add_argument("--sleep", type=float, default=1.0)
    parser.add_argument("--force", action="store_true", help="忽略已有快照，全量重抓")
    parser.add_argument("--max-per-run", type=int, default=4,
                        help="每次最多补几个行业（默认 4）。实测：乐咕对这批页面有稳定的失败率，"
                             "31 个一级行业里 12 个反复 RuntimeError（加长 sleep、重试 3 次仍失败，"
                             "判定为页面级限制而非限流）；限量的小预算避免每天在这上面浪费几分钟")
    args = parser.parse_args()

    from data.ingest.providers.cn_sw2021 import SW2021IndustryFetcher

    started = time.time()
    fetcher = SW2021IndustryFetcher(sleep_seconds=args.sleep, attempts=3)
    try:
        catalog = fetcher.fetch_first_level_catalog()
    except Exception as exc:                                   # noqa: BLE001
        # 目录页偶发反爬（实测 akshare 的 level1Items 元素缺失 → AttributeError）。
        # 申万 2021 一级行业代码是稳定公开的，这里用内置清单兜底，
        # 保证"补齐缺失行业"这件事不会因为目录页抽风而彻底停摆。
        catalog = pd.DataFrame(SW2021_LEVEL1_FALLBACK, columns=["行业代码", "行业名称"])
        print(f"[sw2021] 目录页失败（{type(exc).__name__}），使用内置 31 个一级行业清单", flush=True)
    print(f"[sw2021] 一级行业目录 {len(catalog)} 个（{time.time()-started:.1f}s）", flush=True)
    if args.dry_run:
        print(catalog.head(8).to_string(index=False), flush=True)
        return 0

    frames, failures = [], []
    # 增量语义：只抓"上一份 parquet 里没有的行业"，并且**合并**而不是覆盖 ——
    # 第一版每次全量覆盖，实测 31 个行业里有 12 个被乐咕的限流挡掉（RuntimeError），
    # 结果一次失败的运行会把上一次抓好的覆盖成更差的一份。
    done_codes: set[str] = set()
    if META.exists():
        try:
            done_codes = set(pd.read_parquet(META, columns=["industry_code"])["industry_code"].astype(str))
        except Exception:                                      # noqa: BLE001
            done_codes = set()
    todo_all = [(str(r["行业代码"]), str(r["行业名称"])) for _, r in catalog.iterrows()
                if args.force or str(r["行业代码"]) not in done_codes]
    todo = todo_all[: max(int(args.max_per_run), 1)]
    print(f"[sw2021] 已有 {len(done_codes)} 个行业；待补 {len(todo_all)} 个，本次抓 {len(todo)} 个",
          flush=True)
    for code, name in todo:
        try:
            # provider 的 fetch() 是"按股票代码找行业"，这里直接抓单个行业成分更直接
            composition = fetcher._read_html_table(
                f"https://legulegu.com/stockdata/index-composition?industryCode={code}")
            composition = composition.iloc[:, :6].copy()
            composition.columns = [str(c) for c in composition.columns][:6]
            # 在**循环内**就统一成最终 schema：否则与旧 parquet concat 后会出现两列同名
            # （旧表已有 stock_code、新表还是"股票代码"），merged["stock_code"] 变成 DataFrame、
            # `.str.zfill` 直接抛 AttributeError（实测踩过）。
            code_col = next((c for c in composition.columns if "股票代码" in c or c == "stock_code"),
                            composition.columns[0])
            name_col = next((c for c in composition.columns
                             if "股票简称" in c or "股票名称" in c or c == "name"), composition.columns[1])
            composition = composition.rename(columns={code_col: "stock_code", name_col: "name"})
            composition["industry_code"] = code
            composition["industry_name"] = name
            composition["snapshot"] = f"{datetime.now():%Y-%m-%d}"
            keep_cols = ["industry_code", "industry_name", "stock_code", "name", "snapshot"]
            frames.append(composition[[c for c in keep_cols if c in composition.columns]])
            time.sleep(max(args.sleep, 0.0))
        except Exception as exc:                               # noqa: BLE001
            failures.append({"industry": name, "code": code,
                             "error": f"{type(exc).__name__}: {exc}"[:160]})
            print(f"[sw2021] {name} 失败：{type(exc).__name__}", flush=True)

    if not frames and not META.exists():
        print("[sw2021] 全部失败且没有历史文件，未写出", flush=True)
        return 1
    frames_to_concat = list(frames)
    if META.exists():
        frames_to_concat.append(pd.read_parquet(META))          # 合并（旧的保留，新的覆盖同行业）
    merged = pd.concat(frames_to_concat, ignore_index=True) if frames_to_concat else pd.DataFrame()
    keep = ["industry_code", "industry_name", "stock_code", "name", "snapshot"]
    for column in keep:
        if column not in merged.columns:
            merged[column] = pd.NA
    merged = merged[keep].copy()
    merged["stock_code"] = merged["stock_code"].astype(str).str.zfill(6)
    merged["snapshot"] = merged["snapshot"].fillna(f"{datetime.now():%Y-%m-%d}")
    merged = merged.drop_duplicates(subset=["industry_code", "stock_code"], keep="last")
    META.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(META, index=False)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    covered = sorted(set(merged["industry_code"].astype(str)))
    missing = [name for code, name in SW2021_LEVEL1_FALLBACK if code not in covered]
    report = {"started_at": datetime.now().isoformat(timespec="seconds"),
              "coverage": f"{len(covered)}/{len(SW2021_LEVEL1_FALLBACK)}",
              "missing_industries": missing,
              "seconds": round(time.time() - started, 1),
              "industries": int(merged["industry_code"].nunique()),
              "stocks": int(merged["stock_code"].nunique()), "rows": int(len(merged)),
              "failures": failures, "path": str(META.relative_to(ROOT))}
    (REPORT_DIR / f"{datetime.now():%Y-%m-%d}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[sw2021] {report['industries']} 个行业 / {report['stocks']} 只票 / {report['rows']} 行"
          f"（失败 {len(failures)}；{report['seconds']}s）→ {META.relative_to(ROOT)}", flush=True)
    return 0 if not failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
