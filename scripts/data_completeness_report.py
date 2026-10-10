#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""每日数据完整性报表：把"选股依赖的每一份数据"是否更新到最新交易日，一次性摊开。

为什么需要它（2026-10-08 事故）
-------------------------------
用户问「arm_select / 分时 有在每天的自动化里跑么」时，逐项一查才发现：
每日脚本只覆盖 5 类数据（日线/资金流/分时/特征/模型），**财务、财报、估值、行业分类、
公司行动、筹码都不在里面**。更糟的是"没跑"和"跑了"在日志上长得一样 ——
`backfill_cn_intraday_baostock` 与 `backfill_cn_financials` 的判据都是"文件存在 = 完成"，
补齐之后永远报"全部完成"，实际再也不会更新。

所以除了把缺的接进流程，还需要一个**独立的、只用产物说话的体检器**：
不看"任务有没有 exit 0"，只看"数据本身到没到最新交易日"。

用法：
  uv run python scripts/data_completeness_report.py                # 打印 + 写 output/data_quality/
  uv run python scripts/data_completeness_report.py --as-of 2026-10-08
  uv run python scripts/data_completeness_report.py --strict       # 有 not_ok 时退出码 1（供调度器 gating）
"""
from __future__ import annotations

import argparse
import glob
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "output/data_quality"

# 每项：key → (中文名, 消费者/用途, 更新频率)
# 频率决定"算不算达标"的判据 —— 用同一把尺子量所有数据会得出错误结论：
# 估值快照在本仓库是**月末**快照（105 个文件覆盖 2018-01~2026-09），拿"覆盖到当日"判它必然不达标。
DATASETS = {
    "daily_bars": ("日线 OHLCV", "features / clean_panel / 全部回测", "daily"),
    "adj_factor": ("复权因子", "复权价重建", "daily"),
    "moneyflow": ("资金流", "moneyflow 特征", "daily"),
    "intraday_5min": ("分时 5min", "分时特征（一年窗口）", "daily"),
    "financials": ("财务 / 财报（PIT）", "质量/估值 sleeve、financial_* 列", "report"),
    "valuation": ("估值快照（daily_basic）", "市值/PE/PB 分位", "monthly"),
    "industry": ("行业分类", "行业中性化", "event"),
    "corporate_actions": ("公司行动", "复权/分红除权", "event"),
    "boards": ("板块成分+行情（新浪/同花顺）", "板块轮动、概念热度研究", "daily"),
    "lhb": ("龙虎榜（含机构席位）", "席位/操纵型行业动量研究", "daily"),
    "sw2021": ("申万 2021 一级行业成分", "行业轮动第二口径（稳健性检验）", "event"),
}

# event 类：距上次快照超过这么多天算"太久没更新"
EVENT_STALE_DAYS = 30


@dataclass
class Check:
    key: str
    latest: str | None = None
    as_of: str | None = None
    n: int | None = None
    detail: str = ""
    ok: bool = False
    # True = 该 check 已经用"自己的口径"给出结论（例如分时按个股自己的交易日比、
    # 财报按"是否仍在交易"豁免），主流程不要再用通用的"latest == as_of"覆盖它。
    # 踩过的坑：分时的 latest 是 "4522/4522" 这种计数串，被通用判据一覆盖就成了 ⚠️，
    # 明明"真实落后 0 只"。
    self_judged: bool = False
    extras: dict = field(default_factory=dict)


def _max_date(frame: pd.DataFrame, *cols: str):
    for c in cols:
        if c in frame.columns:
            s = pd.to_datetime(frame[c], errors="coerce").dropna()
            if len(s):
                return s.max()
    return None


def _month_gap(latest_ym: str, as_of_ym: str) -> int:
    """两个 YYYY-MM 相差几个月。"""
    ly, lm = (int(x) for x in latest_ym.split("-"))
    ay, am = (int(x) for x in as_of_ym.split("-"))
    return (ay - ly) * 12 + (am - lm)


def _days_since(stamp_text: str, now: datetime) -> float:
    """事件类数据的"距上次快照多少天"。stamp_text 形如 '2026-09-24 15:23' 或 ISO。"""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return (now - datetime.strptime(stamp_text[:len(fmt) + 2].strip(), fmt)).total_seconds() / 86400
        except ValueError:
            continue
    try:
        return (now - datetime.fromisoformat(stamp_text)).total_seconds() / 86400
    except ValueError:
        return 1e9


def check_daily_bars() -> Check:
    files = sorted(glob.glob(str(ROOT / "assets/data/clean/ohlcv/market=CN/exchange=*/"
                                    "asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet")))
    if not files:
        return Check("daily_bars", detail="找不到日线分区")
    import pyarrow.dataset as ds

    t = ds.dataset(files, format="parquet").to_table(columns=["trade_date", "stock_code"]).to_pandas()
    t["trade_date"] = pd.to_datetime(t["trade_date"])
    last = t["trade_date"].max()
    n = int(t.loc[t["trade_date"] == last, "stock_code"].nunique())
    return Check("daily_bars", latest=str(last.date()), n=n, ok=n > 0,
                 detail=f"最新交易日 {n} 只")


def check_adj_factor() -> Check:
    d = ROOT / "assets/data/raw/adjustment_factor_snapshots"
    files = sorted(d.rglob("*.parquet")) if d.exists() else []
    if not files:
        return Check("adj_factor", detail="目录不存在（跑 scripts/sync_cn_adj_factor.py）")
    # 优先读同步脚本写的 `_coverage.json`：按日快照有上万行也没必要为了"最新日期"
    # 去反序列化几千个 parquet（实测体检因此从 ~20s 掉到瞬时）。
    coverage = d / "market=CN/source=tushare_relay/_coverage.json"
    if coverage.exists():
        try:
            payload = json.loads(coverage.read_text(encoding="utf-8"))
            return Check("adj_factor", latest=payload.get("latest_date"), n=len(files),
                         detail=f"按日快照 {payload.get('by_date_files')} 天 + 逐股 "
                                f"{payload.get('by_stock_files')} 只；"
                                f"最新日 {payload.get('stocks_on_latest')} 只")
        except (OSError, json.JSONDecodeError):
            pass
    cols = ["trade_date"] if "trade_date" in pd.read_parquet(files[-1]).columns else None
    if cols is None:
        return Check("adj_factor", n=len(files), detail=f"{len(files)} 个文件（无 trade_date 列）")
    frame = pd.read_parquet(files[-1], columns=cols)
    return Check("adj_factor", latest=str(_max_date(frame, "trade_date")), n=len(files),
                 detail=f"{len(files)} 个文件")


def check_moneyflow() -> Check:
    d = ROOT / "assets/data/derived/cn_moneyflow_features.parquet"
    if not d.exists():
        return Check("moneyflow", detail="缺少 cn_moneyflow_features.parquet")
    f = pd.read_parquet(d, columns=["trade_date"])
    last = _max_date(f, "trade_date")
    return Check("moneyflow", latest=str(last.date()) if last is not None else None,
                 n=int(len(f)), ok=last is not None, detail=f"{len(f):,} 行")


def _last_daily_date_by_code() -> dict:
    """每只票自己的最后一根日线日期（缓存）。

    "是否落后"必须拿**这只票自己的交易日**去比：停牌/退市/长期停牌的票，日线与分时都停在
    同一天，用全市场 as_of 去卡它们会永远报落后 —— 实测 4,533 只里有 11 只长期不交易的票
    把整项体检卡成 ⚠️（看起来像"分时同步坏了"，其实是判据错了）。
    """
    cache = getattr(_last_daily_date_by_code, "_cache", None)
    if cache is not None:
        return cache
    mapping: dict = {}
    bars_dir = ROOT / "assets/data/clean/ohlcv/market=CN"
    files = sorted(str(p) for p in bars_dir.glob(
        "exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet"))
    if files:
        try:
            import pyarrow.dataset as ds
            frame = ds.dataset(files, format="parquet").to_table(
                columns=["trade_date", "stock_code"]).to_pandas()
            frame["trade_date"] = pd.to_datetime(frame["trade_date"])
            mapping = frame.groupby("stock_code")["trade_date"].max().dt.date.to_dict()
        except Exception:                                       # noqa: BLE001
            mapping = {}
    _last_daily_date_by_code._cache = mapping                   # type: ignore[attr-defined]
    return mapping


def classify_intraday(last_intraday, own_last_daily, as_of) -> str:
    """分时新鲜度分类：`ok` / `lag` / `suspended`。

    `own_last_daily` = 该股自己最后一根日线的日期（没有日线 → None）。
    停牌/退市的票，分时与日线都停在同一天，不能拿全市场 as_of 去卡（实测 11 只长期
    不交易的票把整项体检卡成 ⚠️；真实落后其实是 0）。
    """
    import datetime as _dt
    want = pd.Timestamp(as_of).date()
    own = own_last_daily
    if own is None or own < want:
        return "suspended" if (last_intraday is None or last_intraday >= own) else "lag"
    return "ok" if last_intraday is not None and last_intraday >= want else "lag"


def is_stopped_stock(own_last_daily, as_of) -> bool:
    """该股是否已停止交易（日线停在 as_of 之前）——用于财报"待补"的豁免判定。"""
    return own_last_daily is None or own_last_daily < pd.Timestamp(as_of).date()


def check_intraday(as_of: str) -> Check:
    files = glob.glob(str(ROOT / "assets/data/raw/intraday_baostock/frequency=5min/*.parquet"))
    if not files:
        return Check("intraday_5min", detail="无分时文件")
    codes_file = ROOT / "assets/data/raw/intraday_baostock/_sync/codes.txt"
    target = [c for c in codes_file.read_text().split() if c.strip()] if codes_file.exists() else []
    want = pd.Timestamp(as_of).date()
    last_bar = _last_daily_date_by_code()
    cur = lag = suspended = 0
    for code in target:
        p = ROOT / f"assets/data/raw/intraday_baostock/frequency=5min/{code.replace('.', '_')}.parquet"
        last = (pd.to_datetime(pd.read_parquet(p, columns=["date"])["date"]).max().date()
                if p.exists() else None)
        verdict = classify_intraday(last, last_bar.get(code), as_of)
        cur += int(verdict == "ok")
        lag += int(verdict == "lag")
        suspended += int(verdict == "suspended")
    checked = cur + lag
    return Check("intraday_5min", latest=as_of, n=len(files), ok=lag == 0, self_judged=True,
                 detail=f"已到 {as_of}: {cur}/{max(checked, 1)} 只 / 真实落后 {lag} 只；"
                        f"{suspended} 只已停止交易（豁免）",
                 extras={"lag": lag, "target": max(checked, 1), "suspended": suspended})


def check_financials(as_of: str) -> Check:
    import importlib.util

    spec = importlib.util.spec_from_file_location("fin", ROOT / "scripts/backfill_cn_financials.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                      # type: ignore[union-attr]
    target = [c for c in (mod.STATE_DIR / "codes.txt").read_text().split() if c.strip()]
    want = mod.expected_period(as_of)
    pending = mod.refresh_pending(target, mod.OUT_DIR, today=as_of)
    merged = ROOT / "assets/data/meta/cn_financial_indicators.parquet"
    rows = codes = None
    if merged.exists():
        m = pd.read_parquet(merged, columns=["ts_code", "ann_date"])
        rows, codes = int(len(m)), int(m["ts_code"].nunique())
    # 只把"还在正常交易、却没拿到最新报告期"的票算作待补；停牌/退市的票上游也没有新报告
    # （实测：待补 1 只 = *ST萃华 002731.SZ，其日线止于 2026-08-31、Tushare 侧最新报告期
    # 也只到 2025-09-30 —— 属于公司不再披露，不是我们没抓）。
    last_bar = _last_daily_date_by_code()
    as_of_date = pd.Timestamp(as_of).date()
    active_pending, stopped = [], []
    for code, _ in pending:
        (stopped if is_stopped_stock(last_bar.get(code), as_of) else active_pending).append(code)
    return Check("financials", latest=str(want.date()), n=codes, ok=len(active_pending) == 0,
                 self_judged=True,
                 detail=f"应有报告期 {want.date()}｜待补 {len(pending)} 只"
                        f"（其中 {len(stopped)} 只已停止交易，豁免）｜已合并 {rows:,} 行 / {codes} 只",
                 extras={"pending": len(pending), "pending_active": len(active_pending),
                         "stopped": len(stopped), "rows": rows})


def check_valuation() -> Check:
    d = ROOT / "assets/data/raw/daily_basic_relay"
    files = sorted(d.rglob("*.parquet")) if d.exists() else []
    if not files:
        return Check("valuation", detail="无 daily_basic 快照（[relay_data].enabled=false）")
    import pyarrow.dataset as ds

    t = ds.dataset([str(p) for p in files], format="parquet").to_table(columns=["trade_date"]).to_pandas()
    last = _max_date(t, "trade_date")
    return Check("valuation", latest=str(last.date()) if last is not None else None, n=len(files),
                 detail=f"{len(files)} 个快照文件", ok=last is not None)


def check_industry() -> Check:
    d = ROOT / "assets/data/raw/industry_snapshots"
    files = sorted(d.rglob("*.parquet")) if d.exists() else []
    if not files:
        return Check("industry", detail="无行业快照")
    newest = max(files, key=lambda p: p.stat().st_mtime)
    mt = datetime.fromtimestamp(newest.stat().st_mtime)
    return Check("industry", latest=mt.strftime("%Y-%m-%d %H:%M"), n=len(files),
                 detail=f"{len(files)} 个文件；最新 {newest.name}")


def check_boards() -> Check:
    """板块成分与行情：读派生日表（`board_daily.parquet`）+ 成分长表。"""
    daily = ROOT / "assets/data/derived/cn_board_daily"
    files = sorted(daily.rglob("board_daily.parquet")) if daily.exists() else []
    membership = ROOT / "assets/data/meta/cn_board_membership.parquet"
    if not files:
        return Check("boards", detail="无板块日表")
    frames = []
    for path in files:
        frame = pd.read_parquet(path, columns=["trade_date", "taxonomy", "board"])
        frames.append(frame)
    merged = pd.concat(frames, ignore_index=True)
    latest = pd.to_datetime(merged["trade_date"]).max()
    taxonomies = sorted(merged["taxonomy"].unique().tolist())
    boards = int(merged[merged["trade_date"] == latest].groupby("taxonomy")["board"].nunique().sum())
    rows = int(pd.read_parquet(membership).shape[0]) if membership.exists() else 0
    return Check("boards", latest=latest.strftime("%Y-%m-%d"), n=boards,
                 detail=f"{len(taxonomies)} 类（{', '.join(taxonomies)}）；当日 {boards} 个板块；"
                        f"成分长表 {rows:,} 行")


def check_lhb() -> Check:
    derived = ROOT / "assets/data/derived/cn_lhb/lhb_daily.parquet"
    raw = ROOT / "assets/data/raw/lhb_snapshots"
    days = len(list(raw.glob("date=*/lhb_detail.parquet"))) if raw.exists() else 0
    if not derived.exists():
        return Check("lhb", detail="无龙虎榜派生表")
    frame = pd.read_parquet(derived, columns=["trade_date"])
    latest = pd.to_datetime(frame["trade_date"]).max()
    return Check("lhb", latest=latest.strftime("%Y-%m-%d"), n=days,
                 detail=f"逐日快照 {days} 天；长表 {len(frame):,} 行")


def check_sw2021() -> Check:
    path = ROOT / "assets/data/meta/cn_sw2021_industry.parquet"
    if not path.exists():
        return Check("sw2021", detail="未落地（跑 scripts/sync_cn_sw2021_industry.py）")
    frame = pd.read_parquet(path)
    mt = datetime.fromtimestamp(path.stat().st_mtime)
    industries = int(frame["industry_code"].nunique())
    return Check("sw2021", latest=mt.strftime("%Y-%m-%d %H:%M"), n=industries,
                 detail=f"{industries}/31 个一级行业（乐咕限流，按日渐进补齐）；"
                        f"{frame['stock_code'].nunique()} 只票")


def check_corporate_actions() -> Check:
    d = ROOT / "assets/data/raw/corporate_actions_snapshots"
    files = sorted(d.rglob("*")) if d.exists() else []
    files = [p for p in files if p.is_file()]
    if not files:
        return Check("corporate_actions", detail="无公司行动快照")
    newest = max(files, key=lambda p: p.stat().st_mtime)
    mt = datetime.fromtimestamp(newest.stat().st_mtime)
    return Check("corporate_actions", latest=mt.strftime("%Y-%m-%d %H:%M"), n=len(files),
                 detail=f"{len(files)} 个文件；最新 {newest.name}")


def main() -> int:
    ap = argparse.ArgumentParser(description="每日数据完整性报表")
    ap.add_argument("--as-of", default="", help="基准交易日（默认取日线最新交易日）")
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    ap.add_argument("--strict", action="store_true", help="有 not_ok 时退出码 1")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    bars = check_daily_bars()
    as_of = args.as_of or bars.latest or datetime.now().strftime("%Y-%m-%d")

    checks = [
        bars, check_adj_factor(), check_moneyflow(), check_intraday(as_of),
        check_financials(as_of), check_valuation(), check_industry(), check_corporate_actions(),
        check_boards(), check_lhb(), check_sw2021(),
    ]
    # 按各自的更新频率判定达标（各 check 内部已经给出 ok 的，只有 daily/report 类才采纳）
    now = datetime.now()
    for c in checks:
        c.as_of = as_of
        cadence = DATASETS[c.key][2]
        if cadence == "daily":
            # 日线是**当晚**由流水线写入的，而板块/龙虎榜这些外部数据面是收盘后立刻抓的：
            # 同一天里，"日线基准日"可能还停在前一交易日。两者都算达标，否则每天都会假报警。
            if not c.self_judged:
                accept = {as_of, now.strftime("%Y-%m-%d")}
                c.ok = bool(c.latest) and c.latest[:10] in accept
        elif cadence == "report":
            pass                                  # check_financials 已按"待补只数"给出 ok
        elif cadence == "monthly":
            if c.latest and len(c.latest) >= 10:
                # 月度快照滞后 1~2 个月是正常的（当月的月末还没到）；
                # 但不能允许"latest 反超 as_of"或滞后超过 2 个月。
                gap = _month_gap(c.latest[:7], as_of[:7])
                c.ok = -1 <= gap <= 2
            else:
                c.ok = False
        else:                                     # event
            c.ok = bool(c.latest) and _days_since(c.latest, now) <= EVENT_STALE_DAYS

    cadence_cn = {"daily": "每日", "report": "按披露截止日", "monthly": "每月", "event": "事件驱动"}
    lines = [f"# 数据完整性报表（基准交易日 {as_of}）", "",
             f"生成 {datetime.now():%Y-%m-%d %H:%M}", "",
             "判据按各自的更新频率给：每日类要求覆盖到基准交易日；财报类要求没有"
             "「该披露却没披露」的股票；月度类允许滞后 2 个月；事件类要求 30 天内更新过。", "",
             "| 数据 | 频率 | 消费者 | 最新 | 状态 | 说明 |", "|---|---|---|---|---|---|"]
    for c in checks:
        name, consumer = DATASETS[c.key][0], DATASETS[c.key][1]
        cadence = DATASETS[c.key][2]
        mark = "✅" if c.ok else ("⚠️" if c.latest else "❌")
        lines.append(f"| {name} | {cadence_cn[cadence]} | {consumer} | {c.latest or '—'} | {mark} | {c.detail} |")
    not_ok = [c for c in checks if not c.ok]
    lines += ["", f"**未达标 {len(not_ok)}/{len(checks)}**："
                  + ("、".join(DATASETS[c.key][0] for c in not_ok) if not_ok else "无"), ""]
    report = "\n".join(lines)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "data_completeness.md").write_text(report, encoding="utf-8")
    (out_dir / "data_completeness.json").write_text(json.dumps({
        "as_of": as_of, "generated_at": datetime.now().isoformat(timespec="seconds"),
        "checks": [{"key": c.key, "name": DATASETS[c.key][0], "cadence": DATASETS[c.key][2], "latest": c.latest,
                    "n": c.n, "ok": c.ok, "detail": c.detail, **c.extras} for c in checks],
        "not_ok": [c.key for c in not_ok],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.quiet:
        print(report)
    print(f"[data-completeness] 未达标 {len(not_ok)}/{len(checks)} → {out_dir/'data_completeness.md'}")
    return 1 if (args.strict and not_ok) else 0


if __name__ == "__main__":
    raise SystemExit(main())
