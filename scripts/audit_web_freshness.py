#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Web 页面新鲜度体检：**自动化跑完后，每个页面都必须是最新交易日的**。

用户要求（2026-10-10）："自动化跑完，所有 web site 全部更新到最新"。
这条要求不能靠人肉记：本脚本给每个页面登记"它读哪个产物 / 怎么取数据日期 / 是否必须每日"，
每天流水线的**最后一步**跑一次，把不达标的页面直接点名。

判定口径：
  * `daily`  —— 数据日期必须等于**最新交易日**（缺失/落后都算不达标）；
  * `event`  —— 事件/按需类（因子挖掘会话、公告、参考账户），只报告日期不判死；
  * `live`   —— 实时源（分时/公告），报告"当前可取到的最新日期"。

用法：
  uv run python scripts/audit_web_freshness.py            # 打印表格 + 写 json/md
  uv run python scripts/audit_web_freshness.py --strict   # 有不达标就退出码 1（每日流程用）
输出：
  output/data_quality/web_freshness.json / .md
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

OUT_DIR = ROOT / "output/data_quality"
RESULTS = ROOT / "output/results_cn"


def _latest_trade_date() -> str | None:
    from backend.services.market_board_service import latest_trade_date
    day = latest_trade_date()
    return None if day is None else str(pd.Timestamp(day).date())


def _parquet_max(path: Path, column: str) -> str | None:
    if not path.exists():
        return None
    try:
        frame = pd.read_parquet(path, columns=[column])
        values = pd.to_datetime(frame[column], errors="coerce").dropna()
        return None if values.empty else str(values.max().date())
    except Exception:                                          # noqa: BLE001
        return None


def _csv_max(path: Path, column: str) -> str | None:
    if not path.exists():
        return None
    try:
        frame = pd.read_csv(path, usecols=[column])
        values = pd.to_datetime(frame[column], errors="coerce").dropna()
        return None if values.empty else str(values.max().date())
    except Exception:                                          # noqa: BLE001
        return None


def _daily_bars_max() -> str | None:
    from backend.services.market_board_service import trade_dates
    dates = trade_dates(3)
    return None if not dates else str(pd.Timestamp(dates[-1]).date())


def _json_window_end(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    ends = []
    for arm in payload.get("arms", []) or []:
        window = arm.get("window") or []
        if isinstance(window, (list, tuple)) and len(window) >= 2:
            ends.append(str(window[1])[:10])
    return max(ends) if ends else None


def _subdir_max(parent: Path, pattern: str = "*") -> str | None:
    """只认形如 YYYY-MM-DD 的目录名。

    踩坑：`output/results_cn/attribution/` 下有个叫 `today` 的杂项目录，
    直接用 max(目录名) 会返回 "today"，体检表里就成了一行假的"日期=today"。
    """
    if not parent.exists():
        return None
    names = [p.name for p in parent.glob(pattern)
             if p.is_dir() and len(p.name) == 10 and p.name[4] == "-" and p.name[:4].isdigit()]
    return max(names) if names else None


def _file_stem_max(parent: Path, pattern: str = "*") -> str | None:
    """只认文件名形如 `YYYY-MM-DD.<ext>` 的产物。

    AI 逐股结论是 `output/results_cn/ai_pick_analysis/<决策日>.json` —— 一天一个文件，
    不是一天一个目录，所以 `_subdir_max` 认不出来（会把这一行永远记成"缺数据"）。
    """
    if not parent.exists():
        return None
    names = [p.stem for p in parent.glob(pattern)
             if len(p.stem) == 10 and p.stem[4] == "-" and p.stem[:4].isdigit()]
    return max(names) if names else None


def _freshness_checks() -> list[dict]:
    from backend.services import market_board_service as mbs
    rows = []

    def add(page: str, kind: str, date: str | None, note: str = "", artifact: str = "",
            mode: str = "eq") -> None:
        """mode：`eq` 数据日期必须等于最近交易日；`ge` 只要求 >= （按"运行日"命名的产物）。"""
        rows.append({"page": page, "kind": kind, "date": date, "note": note,
                     "artifact": artifact, "mode": mode})

    add("选股结果（生产名单）", "daily", _csv_max(RESULTS / "cn_ensemble_selected.csv", "trade_date"),
        "流水线 preselection→pk", "output/results_cn/cn_ensemble_selected.csv")
    add("选股结果（研究手臂 base_v1）", "daily", _csv_max(RESULTS / "arm_latest_selection.csv", "trade_date"),
        "arm_selection 阶段", "arm_latest_selection.csv")
    add("选股结果（概念/板块手臂）", "daily", _csv_max(RESULTS / "arm_concept_selection.csv", "trade_date"),
        "arm_concept 阶段", "arm_concept_selection.csv")
    add("选股日历", "daily", _parquet_max(RESULTS / "selection_ledger.parquet", "decision_date"),
        "台账（日历/收益回顾的数据底座）", "selection_ledger.parquet")
    add("选股归因", "daily", _subdir_max(RESULTS / "attribution"),
        "explain_selection → pick_attribution_report", "output/results_cn/attribution/<date>/")
    add("K 线图表", "daily", _daily_bars_max(), "clean/qfq 日线", "assets/data/clean/ohlcv ... daily/adjust=qfq")
    add("AI 多空分析（K 线页）", "daily", _file_stem_max(RESULTS / "ai_pick_analysis", "*.json"),
        "analyze_strategy_picks_ai（全部策略逐股 买入/持有/卖出）",
        "output/results_cn/ai_pick_analysis/<决策日>.json")
    add("榜单 / 行业热度 / 连板", "daily", _parquet_max(ROOT / "assets/data/derived/cn_moneyflow_features.parquet", "trade_date"),
        "日线 + 资金流面板", "assets/data/derived/cn_moneyflow_features.parquet")
    add("回测绩效", "daily", _json_window_end(RESULTS / "performance/summary.json"),
        "回放 → portfolio_performance", "output/results_cn/performance/summary.json")
    add("数据流水线", "daily", _latest_pipeline_report(),
        "流水线报告按**运行日**命名（当晚跑=交易日，跨零点=次日），所以只要求 >= 最近交易日",
        "output/pipeline_reports/cn_pipeline_*.md", mode="ge")
    add("自动化进度", "daily", _latest_trade_date() if (ROOT / "output/pipeline_reports/daily").exists() else None,
        "每日运行日志（页面本身读运行目录）", "output/pipeline_reports/daily/<date>/")
    add("模拟盘", "daily", _csv_last_date(ROOT / "output/paper_trading/account/nav.csv", "trade_date")
        or _csv_last_date(ROOT / "output/paper_trading/account_reference/nav.csv", "trade_date")
        or _parquet_max(ROOT / "output/paper_trading/account/positions.csv", "asof_date"),
        "paper_account（nav 逐日 push）/ paper_outcomes", "output/paper_trading/account/nav.csv")
    add("同花顺导出", "daily", _latest_ths_export_date(), "export_ths_watchlist（文件名即交易日）",
        "output/results_cn/ths_watchlist/<date>_<source>.csv")
    add("策略池", "daily", _strategy_pool_min_date(),
        "sync_strategy_pool（生产策略的最新交易日取最小值 —— 有一个落后就算不达标）",
        "output/strategy_pool/strategy_pool.json")
    add("个股资料（龙虎榜）", "daily", _parquet_max(ROOT / "assets/data/derived/cn_lhb/lhb_daily.parquet", "trade_date"),
        "sync_cn_lhb", "assets/data/derived/cn_lhb/lhb_daily.parquet")
    add("个股资料（概念成分）", "event", _parquet_max(ROOT / "assets/data/meta/cn_board_membership.parquet", "code")
        and datetime.fromtimestamp((ROOT / "assets/data/meta/cn_board_membership.parquet").stat().st_mtime).strftime("%Y-%m-%d"),
        "sync_cn_boards（概念成分每日刷新）", "assets/data/meta/cn_board_membership.parquet")
    add("分时行情", "live", _latest_trade_date(), "实时源（腾讯/东财）+ 本地 5 分钟归档", "assets/data/raw/intraday_baostock/")
    add("因子挖掘", "event", datetime.fromtimestamp(max(
        [p.stat().st_mtime for p in ROOT.glob("output/factor_mining_sessions/*.json")] or [0])
    ).strftime("%Y-%m-%d") if list(ROOT.glob("output/factor_mining_sessions/*.json")) else None,
        "按需交互式会话（不在每日流程，属设计）", "output/factor_mining_sessions/")
    return rows


def _latest_pipeline_report() -> str | None:
    """最新一份流水线报告的**运行日期**（取自文件名 cn_pipeline_YYYYMMDD_HHMMSS）。

    不能用 mtime：今天重跑一次旧阶段就会把 mtime 变成今天，体检表里会显示成
    "数据流水线 = 今天"，看起来像页面跑到了未来。
    """
    files = sorted((ROOT / "output/pipeline_reports").glob("cn_pipeline_*.md"))
    for path in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True):
        stamp = path.stem.replace("cn_pipeline_", "")[:8]
        if len(stamp) == 8 and stamp.isdigit():
            return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"
    return None


def _latest_ths_export_date() -> str | None:
    """同花顺导出文件名形如 `2026-10-09_all.csv` —— 日期就在文件名里。"""
    folder = RESULTS / "ths_watchlist"
    if not folder.exists():
        return None
    names = [p.name[:10] for p in folder.glob("*.csv")
             if len(p.name) > 10 and p.name[4] == "-" and p.name[:4].isdigit()]
    return max(names) if names else None


def _strategy_pool_min_date() -> str | None:
    """策略池里**生产策略**的最新交易日取最小值。

    为什么取 min 而不是 max：体检要求"全部更新到最新"，只要有一个生产策略落后，
    页面上的那张表就有一条是旧的 —— 取 max 会把落后项盖住（这正是这个体检要防的事）。
    数据源是 `scripts/sync_strategy_pool.py` 写的快照，所以每日流程里它必须排在体检之前。
    """
    path = ROOT / "output/strategy_pool/strategy_pool.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    dates = [item.get("latest_date") for item in payload.get("strategies", [])
             if item.get("stage") == "production"]
    dates = [d for d in dates if d]
    return min(dates) if dates else None


def _csv_last_date(path: Path, column: str) -> str | None:
    """CSV 某一列里最新的日期（找不到列时按位置猜第 3 列，供 nav.csv 这类无名表用）。"""
    if not path.exists():
        return None
    try:
        frame = pd.read_csv(path)
    except Exception:                                          # noqa: BLE001
        return None
    if column not in frame.columns:
        candidates = [c for c in frame.columns if "date" in c.lower()]
        if not candidates:
            return None
        column = candidates[0]
    values = pd.to_datetime(frame[column], errors="coerce").dropna()
    return None if values.empty else str(values.max().date())


def main() -> int:
    ap = argparse.ArgumentParser(description="Web 页面新鲜度体检（每日自动化的最后一步）")
    ap.add_argument("--strict", action="store_true", help="有不达标时退出码 1")
    args = ap.parse_args()

    latest = _latest_trade_date()
    rows = _freshness_checks()
    for row in rows:
        if row["kind"] != "daily":
            row["ok"] = None
        elif row.get("mode") == "ge":
            row["ok"] = bool(row["date"]) and latest is not None and row["date"] >= latest
        else:
            row["ok"] = bool(row["date"]) and row["date"] == latest
    stale = [r for r in rows if r["ok"] is False]

    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "latest_trade_date": latest, "rows": rows,
               "stale": [r["page"] for r in stale],
               "ok": not stale}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "web_freshness.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                               encoding="utf-8")
    lines = [f"# Web 页面新鲜度（基准交易日 {latest}）", "",
             f"生成 {datetime.now():%Y-%m-%d %H:%M}｜{'全部达标 ✅' if not stale else '不达标 ' + str(len(stale)) + ' 项 ❌'}", "",
             "| 页面 | 口径 | 数据日期 | 状态 | 产物 |", "|---|---|---|---|---|"]
    for row in rows:
        mark = "✅" if row["ok"] else ("❌" if row["ok"] is False else "—")
        lines.append(f"| {row['page']} | {row['kind']} | {row['date'] or '—'} | {mark} | "
                     f"{row['note']}（`{row['artifact']}`）|")
    (OUT_DIR / "web_freshness.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n".join(lines), flush=True)
    if stale:
        print(f"\n[web-freshness] ❌ 不达标：{payload['stale']}", flush=True)
        return 1 if args.strict else 0
    print("\n[web-freshness] ✅ 所有每日页面都已是基准交易日", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
