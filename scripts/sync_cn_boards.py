#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""板块（行业/概念/地域）成分与行情同步 —— 补上"板块轮动"研究缺的那份数据。

为什么需要
----------
行业轮动那一轮（`output/research/industry_rotation/REPORT.md`）暴露了两个数据缺口：
  1. **概念/主题板块动量复现不了** —— 仓库只有 110 个 Tushare 行业分类，没有概念板块成分表；
  2. 行业热度只能靠"自己用日线聚合"，缺一个外部口径可交叉验证。

数据源选型（2026-10-09 实测）
------------------------------
  * 东财板块接口（`stock_board_*_em`）全部走 `push2.eastmoney.com/api/qt/clist/get`，
    **本机代理稳定 403/ProxyError**，不可用；
  * 同花顺：`stock_board_industry_summary_ths()`（90 行业，**带净流入/上涨家数/领涨股**，0.2s）与
    `stock_board_concept_name_ths()`（375 概念名+代码，15s）可用；但同花的**成分页 ajax 被 Nginx 403**
    （需要 hexin-v + 更完整的浏览器指纹），所以同花顺拿不到成分；
  * **新浪板块可用且最快**：`stock_sector_spot(新浪行业|概念|地域)` + `stock_sector_detail(label)`
    → 板块列表（含涨跌幅/成交额/公司家数/领涨股）+ **全量成分股**（含个股涨跌幅/成交额/市值/换手），
    255 个板块实测 ~40-80 秒跑完。

因此本脚本的定位：
  * **新浪** = 成分 + 板块行情（概念 175 / 行业 49 / 地域 31）；
  * **同花顺** = 行业级资金流与情绪的独立口径（90 行业，含净流入）+ 更全的概念**名单**（375，无成分）。

产物
----
  assets/data/raw/board_snapshots/taxonomy=<t>/snapshot_<ts>.parquet   原始快照（可追溯）
  assets/data/derived/cn_board_daily/taxonomy=<t>/board_daily.parquet 板块日行情（追加去重）
  assets/data/meta/cn_board_membership.parquet                         最新成分长表（研究直接用）
  output/pipeline_reports/board_sync/<date>.json                       同步台账（家数/耗时/失败清单）

用法
----
  uv run python scripts/sync_cn_boards.py                    # 全部（新浪三个 taxonomy + 同花顺）
  uv run python scripts/sync_cn_boards.py --kind sina_concept  # 只同步某类
  uv run python scripts/sync_cn_boards.py --skip-members      # 只取板块行情，不抓成分（快）
  uv run python scripts/sync_cn_boards.py --session-seconds 600
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
RAW_DIR = ROOT / "assets/data/raw/board_snapshots"
DERIVED_DIR = ROOT / "assets/data/derived/cn_board_daily"
META_DIR = ROOT / "assets/data/meta"
REPORT_DIR = ROOT / "output/pipeline_reports/board_sync"

SINA_INDICATORS = {"sina_industry": "新浪行业", "sina_concept": "概念", "sina_area": "地域"}
MEMBERSHIP_TAXONOMIES = ("sina_industry", "sina_concept", "sina_area")


def _log(report: dict, message: str) -> None:
    print(message, flush=True)
    report.setdefault("log", []).append(f"{datetime.now():%H:%M:%S} {message}")


def _retry(fn, attempts: int = 3, sleep: float = 1.0):
    last = None
    for i in range(attempts):
        try:
            return fn(), None
        except Exception as exc:                       # noqa: BLE001 —— 网络层什么都可能抛
            last = f"{type(exc).__name__}: {exc}"[:200]
            if i < attempts - 1:
                time.sleep(sleep * (i + 1))
    return None, last


def _write_snapshot(taxonomy: str, frame: pd.DataFrame, stamp: str) -> Path:
    directory = RAW_DIR / f"taxonomy={taxonomy}"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"snapshot_{stamp}.parquet"
    frame.to_parquet(path, index=False)
    return path


def _append_daily(taxonomy: str, frame: pd.DataFrame) -> Path:
    """板块日行情：按 (trade_date, board) 去重后追加。"""
    directory = DERIVED_DIR / f"taxonomy={taxonomy}"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "board_daily.parquet"
    if path.exists():
        old = pd.read_parquet(path)
        frame = pd.concat([old, frame], ignore_index=True)
        frame = frame.drop_duplicates(subset=["trade_date", "board"], keep="last")
    frame = frame.sort_values(["trade_date", "board"]).reset_index(drop=True)
    frame.to_parquet(path, index=False)
    return path


def _normalize_board_code(text: str) -> str:
    """板块代码统一成 6 位数字（新浪的 label 是 gn_hwqc / new_blhy；同花顺是 6 位）。"""
    import re
    digits = re.sub(r"\D", "", str(text))
    return digits.zfill(6) if digits else str(text)


def sync_sina(kind: str, report: dict, stamp: str, skip_members: bool,
              session_seconds: float, deadline: float) -> None:
    import akshare as ak

    indicator = SINA_INDICATORS[kind]
    started = time.time()
    listing, error = _retry(lambda: ak.stock_sector_spot(indicator=indicator))
    if listing is None:
        report["sources"][kind] = {"ok": False, "error": error}
        _log(report, f"[{kind}] 板块列表失败：{error}")
        return
    listing["trade_date"] = pd.Timestamp.today().normalize()
    listing["taxonomy"] = kind
    listing["board_code"] = listing["label"].map(_normalize_board_code)
    listing = listing.rename(columns={"板块": "board", "label": "label",
                                      "公司家数": "n_members", "涨跌幅": "pct_chg",
                                      "总成交额": "amount", "个股-涨跌幅": "leader_pct",
                                      "股票名称": "leader_name", "股票代码": "leader_code"})
    keep = ["trade_date", "taxonomy", "board_code", "label", "board", "n_members", "pct_chg",
            "amount", "leader_code", "leader_name", "leader_pct"]
    listing_keep = listing[[c for c in keep if c in listing.columns]]
    _write_snapshot(kind, listing_keep, stamp)
    daily_path = _append_daily(kind, listing_keep)
    report["sources"][kind] = {"ok": True, "boards": int(len(listing_keep)),
                               "daily_path": str(daily_path.relative_to(ROOT)),
                               "seconds": round(time.time() - started, 1)}
    _log(report, f"[{kind}] 板块 {len(listing_keep)} 个（{time.time()-started:.1f}s）→ {daily_path.name}")

    if skip_members or kind not in MEMBERSHIP_TAXONOMIES:
        return

    members_rows = []
    failures: list[dict] = []
    for index, row in listing_keep.iterrows():
        if session_seconds and time.time() > deadline:
            failures.append({"board": row["board"], "error": "session-seconds 到点，剩余板块留待下次"})
            break
        detail, error = _retry(lambda label=row["label"]: ak.stock_sector_detail(sector=label),
                               attempts=2, sleep=0.6)
        if detail is None or detail.empty:
            failures.append({"board": row["board"], "error": error or "空表"})
            continue
        detail = detail.copy()
        detail["taxonomy"] = kind
        detail["board_code"] = row["board_code"]
        detail["board"] = row["board"]
        detail["retrieved_at"] = datetime.now().isoformat(timespec="seconds")
        members_rows.append(detail[[
            "taxonomy", "board_code", "board", "symbol", "code", "name",
            "trade", "changepercent", "amount", "mktcap", "nmc", "turnoverratio", "retrieved_at",
        ]])
    if members_rows:
        members = pd.concat(members_rows, ignore_index=True)
        path = _write_snapshot(f"{kind}_members", members, stamp)
        report["sources"][f"{kind}_members"] = {
            "ok": True, "rows": int(len(members)), "boards": int(members["board"].nunique()),
            "failed_boards": len(failures), "path": str(path.relative_to(ROOT)),
            "seconds": round(time.time() - started, 1),
        }
        _log(report, f"[{kind}] 成分 {len(members):,} 行 / {members['board'].nunique()} 个板块"
                     f"（失败 {len(failures)}）→ {path.name}")
    if failures:
        report["failures"].extend([{"taxonomy": kind, **item} for item in failures])


def sync_ths_industry(report: dict, stamp: str) -> None:
    """同花顺行业汇总：带净流入/上涨家数/领涨股，作为行业热度的独立口径。"""
    import akshare as ak

    started = time.time()
    frame, error = _retry(lambda: ak.stock_board_industry_summary_ths())
    if frame is None:
        report["sources"]["ths_industry"] = {"ok": False, "error": error}
        _log(report, f"[ths_industry] 失败：{error}")
        return
    frame = frame.copy()
    frame["trade_date"] = pd.Timestamp.today().normalize()
    frame["taxonomy"] = "ths_industry"
    frame = frame.rename(columns={"板块": "board", "涨跌幅": "pct_chg", "总成交额": "amount",
                                  "净流入": "net_inflow", "上涨家数": "n_up", "下跌家数": "n_down",
                                  "领涨股": "leader_name", "领涨股-涨跌幅": "leader_pct"})
    frame["board_code"] = frame["board"].astype(str)
    keep = ["trade_date", "taxonomy", "board_code", "board", "pct_chg", "amount", "net_inflow",
            "n_up", "n_down", "leader_name", "leader_pct"]
    frame = frame[[c for c in keep if c in frame.columns]]
    _write_snapshot("ths_industry", frame, stamp)
    daily_path = _append_daily("ths_industry", frame)
    report["sources"]["ths_industry"] = {"ok": True, "boards": int(len(frame)),
                                         "daily_path": str(daily_path.relative_to(ROOT)),
                                         "seconds": round(time.time() - started, 1)}
    _log(report, f"[ths_industry] {len(frame)} 个行业（{time.time()-started:.1f}s）→ {daily_path.name}")


def sync_ths_concept(report: dict, stamp: str) -> None:
    """同花顺概念**清单**（375 个，无成分）：比新浪的 175 个概念更全，作为概念覆盖度的对照。"""
    import akshare as ak

    started = time.time()
    frame, error = _retry(lambda: ak.stock_board_concept_name_ths(), attempts=2, sleep=2.0)
    if frame is None:
        report["sources"]["ths_concept_names"] = {"ok": False, "error": error}
        _log(report, f"[ths_concept_names] 失败：{error}")
        return
    frame = frame.copy()
    frame["trade_date"] = pd.Timestamp.today().normalize()
    frame["taxonomy"] = "ths_concept"
    path = _write_snapshot("ths_concept_names", frame, stamp)
    report["sources"]["ths_concept_names"] = {"ok": True, "concepts": int(len(frame)),
                                              "note": "同花顺概念成分页被反爬拦截，仅有名单",
                                              "path": str(path.relative_to(ROOT)),
                                              "seconds": round(time.time() - started, 1)}
    _log(report, f"[ths_concept_names] {len(frame)} 个概念名（{time.time()-started:.1f}s，无成分）")


def rebuild_membership_meta(stamp: str, report: dict) -> None:
    """把最新的成分快照合并成研究可直接读的一份长表。"""
    frames = []
    for kind in MEMBERSHIP_TAXONOMIES:
        pattern = RAW_DIR / f"taxonomy={kind}_members" / "snapshot_*.parquet"
        files = sorted(pattern.parent.glob(pattern.name)) if pattern.parent.exists() else []
        if not files:
            continue
        frame = pd.read_parquet(files[-1])
        frame["snapshot"] = files[-1].stem.replace("snapshot_", "")
        frames.append(frame)
    if not frames:
        _log(report, "[membership] 没有可用成分快照，跳过")
        return
    merged = pd.concat(frames, ignore_index=True)
    path = META_DIR / "cn_board_membership.parquet"
    merged.to_parquet(path, index=False)
    report["membership"] = {"path": str(path.relative_to(ROOT)), "rows": int(len(merged)),
                            "taxonomies": sorted(merged["taxonomy"].unique().tolist()),
                            "boards": int(merged.groupby(["taxonomy", "board"]).ngroups),
                            "stocks": int(merged["code"].astype(str).nunique())}
    _log(report, f"[membership] {len(merged):,} 行 / {merged['board'].nunique()} 个板块 / "
                 f"{merged['code'].nunique()} 只票 → {path.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="同步板块（新浪行业/概念/地域 + 同花顺行业/概念名单）")
    parser.add_argument("--kind", default="all",
                        choices=["all", *SINA_INDICATORS.keys(), "ths_industry", "ths_concept"])
    parser.add_argument("--skip-members", action="store_true", help="只取板块行情，不抓成分股")
    parser.add_argument("--session-seconds", type=float, default=900.0,
                        help="本次最长运行秒数（到点停止抓成分，事件不丢：下次接着抓）")
    parser.add_argument("--report-date", default="")
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    deadline = time.time() + args.session_seconds if args.session_seconds else float("inf")
    report: dict = {"started_at": datetime.now().isoformat(timespec="seconds"), "sources": {},
                    "failures": [], "kind": args.kind, "skip_members": args.skip_members}
    started = time.time()

    # `--kind all` 必须把同花顺两条也带上：第一版漏了，跑完只有新浪的 6 类源，
    # 台账里 ths_industry（带净流入的行业口径）静默缺失。
    if args.kind == "all":
        kinds = [*SINA_INDICATORS, "ths_industry", "ths_concept"]
    else:
        kinds = [args.kind]
    for kind in kinds:
        if kind in SINA_INDICATORS:
            sync_sina(kind, report, stamp, args.skip_members, args.session_seconds, deadline)
        elif kind == "ths_industry":
            sync_ths_industry(report, stamp)
        elif kind == "ths_concept":
            sync_ths_concept(report, stamp)

    rebuild_membership_meta(stamp, report)

    report["seconds"] = round(time.time() - started, 1)
    report["ok"] = all(v.get("ok") for v in report["sources"].values()) if report["sources"] else False
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    date_key = args.report_date or f"{datetime.now():%Y-%m-%d}"
    out = REPORT_DIR / f"{date_key}.json"
    # 一天可能被调用多次（只同步某一类 / 重试失败项）。第一版每次覆盖同名文件，
    # 结果只能看到"最后一次跑的那一类"、前面成功的源在台账里消失 → 这里按 source 合并累积。
    if out.exists():
        try:
            previous = json.loads(out.read_text(encoding="utf-8"))
            merged_sources = dict(previous.get("sources", {}))
            merged_sources.update(report["sources"])
            report["sources"] = merged_sources
            report["runs"] = (previous.get("runs") or []) + [
                {"at": report["started_at"], "kind": args.kind, "seconds": report["seconds"],
                 "sources": sorted(report["sources"].keys())}]
        except (OSError, json.JSONDecodeError):
            pass
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (REPORT_DIR / f"{date_key}_{stamp}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    ok_sources = [k for k, v in report["sources"].items() if v.get("ok")]
    bad_sources = [k for k, v in report["sources"].items() if not v.get("ok")]
    print(f"[boards] 完成：成功 {len(ok_sources)} 类 {ok_sources}"
          f"{'；失败 ' + str(bad_sources) if bad_sources else ''}；总耗时 {report['seconds']}s")
    print(f"[boards] 台账 {out.relative_to(ROOT)}")
    return 0 if ok_sources else 1


if __name__ == "__main__":
    raise SystemExit(main())
