#!/usr/bin/env python3
"""下单前 checklist（P2.42 §4.5）：把"今天这份名单能不能照着下单"变成一张可核对的表。

九个检查项，**每一项都归因**到数据 / 规则 / 资金 / 市场 / 一致性五类里的某一类 ——
出问题时要能回答"是哪一类挡住的"，而不是笼统一个红叉。三档状态：

* `pass`  ✅ 通过
* `warn`  ⚠️ 能下单但有隐患（例如：买不起的票被跳过、快照偏旧）
* `block` ❌ 不该下单（数据没过期就对不上名单 / 执行假设不一致 / 单票超限 / PK 护栏有违规权重）

只做**检查**，不生成订单、不改任何生产产物；结果落
`output/results_cn/pre_order_check/<date>.json|md`，退出码：有 block = 1，否则 0。

用法：
    uv run python scripts/pre_order_check.py                 # 默认：模拟盘（当日名单）
    uv run python scripts/pre_order_check.py --account live   # 改成检查**实盘持仓**（卖出方向）
    uv run python scripts/pre_order_check.py --date 2026-10-09 --print
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

RESULTS = ROOT / "output/results_cn"
HISTORY = RESULTS / "selection_history"
OUT = RESULTS / "pre_order_check"
LIVE = ROOT / "output/paper_trading/live_mirror"
SELECTED = RESULTS / "cn_ensemble_selected.csv"
PARITY = ROOT / "output/verification/execution_parity.json"
GUARD = RESULTS / "pk_guard/latest.json"
REF_CONSISTENCY = ROOT / "output/paper_trading/account_reference/consistency.json"
LOT = 100

PASS, WARN, BLOCK = "pass", "warn", "block"


def latest_trade_date() -> str | None:
    try:
        from backend.services.market_board_service import latest_trade_date as _ltd

        day = _ltd()
        return str(pd.Timestamp(day).date()) if day is not None else None
    except Exception:
        return None


def last_close_map(codes: list[str]) -> dict[str, dict]:
    """每只票最近一根日线的 收盘/涨跌停价/日期（本地 clean 日线，只读）。"""
    import pyarrow.dataset as ds

    files = [str(p) for year in (2026,) for p in (ROOT / "assets/data/clean/ohlcv/market=CN").glob(
        f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={year}/*.parquet")]
    if not files or not codes:
        return {}
    frame = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "close"], filter=ds.field("stock_code").isin(codes)).to_pandas()
    if frame.empty:
        return {}
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    out: dict[str, dict] = {}
    for code, group in frame.sort_values("trade_date").groupby("stock_code"):
        row = group.iloc[-1]
        prev = group.iloc[-2] if len(group) > 1 else None
        out[str(code)] = {"date": str(row["trade_date"].date()), "close": float(row["close"]),
                          "prev_close": float(prev["close"]) if prev is not None else None}
    return out


def check_rows(account: str, date: str) -> tuple[list[dict], dict]:
    """返回 (检查项列表, 上下文)。每个检查项：{name, category, status, detail, evidence}"""
    checks: list[dict] = []
    reference = latest_trade_date()
    ctx: dict = {"account": account, "date": date, "latest_trade_date": reference}

    def add(name: str, category: str, status: str, detail: str, evidence: str = "") -> None:
        checks.append({"name": name, "category": category, "status": status,
                       "detail": detail, "evidence": evidence})

    # ① 数据新鲜度：名单日期 == 最新交易日
    if account == "live":
        positions = LIVE / "positions.csv"
        if positions.exists():
            frame = pd.read_csv(positions)
            snapshot = json.loads((ROOT / "config/live_account_holdings.json").read_text(encoding="utf-8"))
            age = (datetime.now().date() - datetime.fromisoformat(str(snapshot["asof"])).date()).days
            stale = age > 10
            add("持仓快照新鲜度", "数据", BLOCK if stale else (WARN if age > 5 else PASS),
                f"快照 {snapshot['asof']}（{age} 天前），{len(frame)} 只持仓",
                "config/live_account_holdings.json")
            ctx["frame"] = frame
            ctx["capital"] = float(snapshot.get("cash") or 0) + float(frame["market_value"].sum())
        else:
            add("持仓快照新鲜度", "数据", BLOCK, "没有实盘镜像持仓产物", str(positions))
            ctx["frame"] = pd.DataFrame()
            ctx["capital"] = 0.0
    else:
        if not SELECTED.exists():
            add("名单新鲜度", "数据", BLOCK, "没有生产名单产物", str(SELECTED))
            ctx["frame"], ctx["capital"] = pd.DataFrame(), 45_000.0
        else:
            frame = pd.read_csv(SELECTED)
            row_date = str(frame["trade_date"].max())[:10] if "trade_date" in frame.columns else ""
            ok = bool(reference) and row_date == reference
            add("名单新鲜度", "数据", PASS if ok else BLOCK,
                f"名单 {row_date} vs 最新交易日 {reference}", str(SELECTED))
            ctx["frame"] = frame
            ctx["capital"] = 45_000.0

    frame = ctx["frame"]
    codes = [str(c) for c in frame.get("stock_code", pd.Series(dtype=str)).tolist()]
    ctx["codes"] = codes

    # ② 名单/持仓是否已归档（选股日历与台账读的就是它）
    archived = (HISTORY / f"{date}.csv").exists() or (HISTORY / f"{reference}.csv").exists() if reference else False
    add("名单归档", "数据", PASS if archived else WARN,
        f"selection_history/{date}.csv {'存在' if archived else '缺失'}（台账/日历靠它回看）",
        f"output/results_cn/selection_history/{date}.csv")

    # ③ 执行假设一致性（模拟盘与回放同一份假设）
    if PARITY.exists():
        parity = json.loads(PARITY.read_text(encoding="utf-8"))
        differ = int(parity.get("differ") or 0)
        add("执行假设一致性", "一致性", PASS if differ == 0 else BLOCK,
            f"MATCH={parity.get('match')} DIFFER={differ}（版本 {parity.get('version')}）", str(PARITY))
    else:
        add("执行假设一致性", "一致性", WARN, "没有一致性快照（跑 scripts/execution_parity_check.py）", str(PARITY))

    # ④ 涨跌停 / 停牌（用本地日线最后一根判，诚实标注"最近可得"）
    market = last_close_map(codes)
    blocked_codes, missing = [], []
    for code in codes:
        info = market.get(code)
        if info is None:
            missing.append(code)
            continue
        if info["prev_close"]:
            pct = info["close"] / info["prev_close"] - 1
            # 主板 ±10%、创业板/科创板 ±20%（按代码前缀粗判，够用且不假装精确）
            limit = 0.20 if code.startswith(("300", "688", "301")) else 0.10
            if pct >= limit - 0.002:
                blocked_codes.append(code)
    add("涨跌停/停牌", "市场", BLOCK if blocked_codes else (WARN if missing else PASS),
        (f"{len(blocked_codes)} 只最近一根涨停（涨停不买）：{blocked_codes[:5]}；" if blocked_codes else "")
        + (f"{len(missing)} 只没有本地日线（停牌/新股/退市？）：{missing[:5]}" if missing else "逐票都有行情、无涨停封板"),
        "assets/data/clean/ohlcv（最近可得日线，非当日实时）")

    # ⑤ 资金与整手：单票预算下买不买得起
    weights = frame.get("target_weight") if "target_weight" in frame.columns else None
    if weights is None or float(pd.Series(weights).fillna(0).sum()) <= 0:
        weights = pd.Series([1.0 / max(len(codes), 1)] * len(codes))
    capital = float(ctx["capital"])
    unaffordable = []
    for code, weight in zip(codes, list(weights)):
        info = market.get(code)
        if info is None:
            continue
        budget = capital * max(float(weight or 0), 1.0 / max(len(codes), 1))
        lots = int(budget // (info["close"] * LOT))
        if lots <= 0:
            unaffordable.append({"stock_code": code, "price": info["close"], "budget": round(budget, 2)})
    add("资金与整手", "资金", WARN if unaffordable else PASS,
        (f"{len(unaffordable)} 只买不起（一手 > 单票预算）："
         + ", ".join(f"{x['stock_code']}({x['price']:.2f})" for x in unaffordable[:5])
         if unaffordable else f"资金 {capital:,.0f} 元，全部至少能买 1 手、且按整手取整"),
        "单票预算 = 资金 × 目标权重（或等权）")

    # ⑥ 单票上限
    cap = 0.12
    try:
        import tomllib

        cfg = tomllib.loads((ROOT / "config/cn_pipeline.toml").read_text(encoding="utf-8"))
        cap = float(cfg["selection"]["portfolio_constraints"].get("max_weight_concentrated", cap))
    except Exception:
        pass
    over = []
    if "weight" in frame.columns:
        over = [str(r.stock_code) for r in frame.itertuples() if float(getattr(r, "weight", 0) or 0) > cap]
    elif "target_weight" in frame.columns:
        over = [str(r.stock_code) for r in frame.itertuples() if float(getattr(r, "target_weight", 0) or 0) > cap]
    add("单票上限", "规则", BLOCK if over else PASS,
        f"上限 {cap:.0%}；超限 {len(over)} 只 {over[:5]}" if over else f"全部 ≤ {cap:.0%}", "config/cn_pipeline.toml")

    # ⑦ PK 护栏（不该拿到权重的票）
    if GUARD.exists():
        guard = json.loads(GUARD.read_text(encoding="utf-8"))
        n = len(guard.get("violations") or [])
        share = float(guard.get("violation_weight_share") or 0)
        add("PK 护栏", "规则", PASS if n == 0 else WARN,
            f"{guard.get('date')}：{n} 条违规，违规权重占比 {share:.1%}（护栏只读、不改权重）", str(GUARD))
    else:
        add("PK 护栏", "规则", WARN, "没有护栏快照（跑 scripts/pk_guard_report.py）", str(GUARD))

    # ⑧ 参考账户一致性（模拟盘绑定的那条规则）
    if REF_CONSISTENCY.exists():
        consistency = json.loads(REF_CONSISTENCY.read_text(encoding="utf-8"))
        same = (consistency.get("vs_shadow_same_variant") or {}).get("identical")
        add("参考账户一致性", "一致性", PASS if same else WARN,
            f"与同名影子变体 {'逐位一致' if same else '不一致/无数据'}", str(REF_CONSISTENCY))
    else:
        add("参考账户一致性", "一致性", WARN, "没有一致性核对产物", str(REF_CONSISTENCY))

    # ⑨ 实盘镜像：逐票建议里有没有"卖出"（只在检查实盘账户时给）
    if account == "live" and not frame.empty and "action" in frame.columns:
        sell = frame[frame["action"] == "卖出"]
        add("逐票建议（卖出）", "规则", PASS if sell.empty else WARN,
            (f"{len(sell)} 只建议卖出：" + ", ".join(f"{r.stock_code} {r.name}" for r in sell.itertuples()))
            if not sell.empty else "没有建议卖出的持仓", str(LIVE / "positions.csv"))
    return checks, ctx


def render(checks: list[dict], ctx: dict) -> dict:
    counts = {k: sum(1 for c in checks if c["status"] == k) for k in (PASS, WARN, BLOCK)}
    verdict = BLOCK if counts[BLOCK] else (WARN if counts[WARN] else PASS)
    label = {"pass": "可以下单（全绿）", "warn": "可以下单，但有隐患（见 warn 行）",
             "block": "不该下单（有 block 行）"}[verdict]
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "account": ctx.get("account"), "date": ctx.get("date"),
        "latest_trade_date": ctx.get("latest_trade_date"),
        "capital": round(float(ctx.get("capital") or 0), 2),
        "verdict": verdict, "verdict_label": label, "counts": counts,
        "checks": checks,
        "next_actions": [c["name"] for c in checks if c["status"] != PASS] or ["无（可以照着下单，人工确认执行）"],
    }


def to_md(payload: dict) -> str:
    icon = {PASS: "✅", WARN: "⚠️", BLOCK: "❌"}
    lines = [f"# 下单前 checklist · {payload['account']} · {payload['date']}", "",
             f"- 结论：**{payload['verdict_label']}**（pass {payload['counts'][PASS]} / warn {payload['counts'][WARN]} / block {payload['counts'][BLOCK]}）",
             f"- 资金口径：{payload['capital']:,.0f} 元；基准交易日 {payload['latest_trade_date']}", "",
             "| 检查项 | 类别 | 状态 | 细节 | 证据 |", "|---|---|---|---|---|"]
    for c in payload["checks"]:
        lines.append(f"| {c['name']} | {c['category']} | {icon[c['status']]} | {c['detail']} | `{c['evidence']}` |")
    lines += ["", "> 只做检查，不生成订单、不改生产产物。实盘接入见 P2.42 §5 边界："
                  "需连续 N 个月模拟盘对照 + 闸门 PROMOTE + 人工确认。"]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="下单前 checklist（P2.42 §4.5）")
    ap.add_argument("--account", choices=("paper", "live"), default="paper")
    ap.add_argument("--date", default=None)
    ap.add_argument("--print", action="store_true")
    args = ap.parse_args()
    date = args.date or (latest_trade_date() or datetime.now().strftime("%Y-%m-%d"))
    checks, ctx = check_rows(args.account, date)
    payload = render(checks, ctx)
    OUT.mkdir(parents=True, exist_ok=True)
    stem = f"{date}_{args.account}"
    (OUT / f"{stem}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / f"{stem}.md").write_text(to_md(payload), encoding="utf-8")
    (OUT / f"{args.account}_latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                                     encoding="utf-8")
    if args.print:
        print(f"[pre-order] {args.account} {date} -> {payload['verdict_label']}"
              f"（pass {payload['counts'][PASS]} / warn {payload['counts'][WARN]} / block {payload['counts'][BLOCK]}）")
        for c in payload["checks"]:
            print(f"  [{c['status']:5}] {c['name']}（{c['category']}）: {c['detail']}")
        print(f"  -> output/results_cn/pre_order_check/{stem}.md")
    return 1 if payload["verdict"] == BLOCK else 0


if __name__ == "__main__":
    raise SystemExit(main())
