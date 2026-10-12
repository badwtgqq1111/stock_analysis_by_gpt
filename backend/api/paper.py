"""模拟盘（paper account）API。

这是**唯一的前向口径**：从 2026-09-09 起，按当天实际最终名单、以 T+1 开盘价成交、
扣佣金+滑点+印花税、整手买入、建模涨跌停/停牌/退市，逐日盯市 —— 不是回测，
是"如果照着下单会怎样"。

执行假设**与日频回放共用** config/execution_assumptions_cn.json（2026-10-09 统一），
本 API 把生效假设与一致性快照一并发给前端，免得"净值是哪个口径算的"说不清。

产物：output/paper_trading/account/{nav,positions,fills,orders,paper_account_summary}.csv
      output/paper_trading/account/execution_assumptions.json（当期假设快照）
      output/paper_trading/shadow/summary.json（决策逻辑对照：现行 vs 池前5/8/10 vs 降频 vs rank 上限）
      output/results_cn/pk_guard/latest.json（PK 护栏：不该拿到权重的票）
      output/results_cn/promotion_check/<ts>/verdicts.csv（上线闸门：PROMOTE / KEEP / REJECT）
      output/paper_trading/account_reference/（**参考账户**：模拟盘绑定的那条规则，含一致性核对）
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
from fastapi import APIRouter

from backend.api.forward import parity_snapshot
from factor_engine.portfolio.execution_assumptions import cost_model, load_assumptions
from backend.services.ttl_cache import ttl_cache
from backend.services import live_quotes
from factor_engine.portfolio.live_mirror import realtime_view

ROOT = Path(__file__).resolve().parents[2]
ACC = ROOT / "output/paper_trading/account"
HISTORY = ROOT / "output/results_cn/paper_selection_history.csv"

router = APIRouter(prefix="/api/paper", tags=["paper"])


SHADOW = ROOT / "output/paper_trading/shadow"


def _shadow_summary() -> dict:
    """影子组合（决策逻辑对照）：现行 vs 对照规则，同一执行假设、同一资金。

    这是"现有决策逻辑行不行"的唯一长期证据来源 —— 没有对照就只能看单条曲线的噪音。
    """
    path = SHADOW / "summary.json"
    if not path.exists():
        return {"available": False}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"available": False}
    variants = payload.get("variants", [])
    base = next((v for v in variants if v.get("variant") == "production"), None)
    rows = []
    for v in variants:
        rows.append({k: v.get(k) for k in
                     ("variant", "label", "decision_dates", "first_decision", "last_decision",
                      "last_nav_date", "final_nav", "total_return", "max_drawdown", "ann_vol",
                      "fills", "orders")}
                    | {"vs_production": (round((v.get("total_return") or 0) - (base.get("total_return") or 0), 4)
                                         if base and v.get("variant") != "production" else None)})
    return {"available": True, "generated_at": payload.get("generated_at"),
            "capital": payload.get("capital"), "caveats": payload.get("caveats"),
            "assumptions_version": payload.get("assumptions_version"),
            "variants": rows,
            "curves": {v["variant"]: v.get("curve", []) for v in variants}}


GUARD = ROOT / "output/results_cn/pk_guard"


def _guard_snapshot() -> dict:
    """PK 护栏：最新决策日的"不该拿到权重却拿到了"的票 + 全历史违规率。

    只读快照，不改生产权重（脚本：scripts/pk_guard_report.py）。
    """
    latest = GUARD / "latest.json"
    if not latest.exists():
        return {"available": False}
    try:
        payload = json.loads(latest.read_text(encoding="utf-8"))
    except Exception:
        return {"available": False}
    history = {"days": 0, "violation_days": 0, "median_violation_weight_share": None}
    hist_path = GUARD / "history.csv"
    if hist_path.exists():
        try:
            frame = pd.read_csv(hist_path)
            bad = frame[frame["status"] == "violations"]
            history = {"days": int(len(frame)), "violation_days": int(len(bad)),
                       "median_violation_weight_share": (round(float(bad["violation_weight_share"].median()), 4)
                                                         if len(bad) else 0.0)}
        except Exception:
            pass
    return {"available": True, "date": payload.get("date"), "status": payload.get("status"),
            "rank_cap": payload.get("rank_cap"), "n_holdings": payload.get("n_holdings"),
            "max_weight": payload.get("max_weight"), "top1_code": payload.get("top1_code"),
            "top1_name": payload.get("top1_name"),
            "median_rank_of_holdings": payload.get("median_rank_of_holdings"),
            "violations": payload.get("violations", []),
            "violation_weight_share": payload.get("violation_weight_share"),
            "generated_at": payload.get("generated_at"), "history": history}


PROMOTION = ROOT / "output/results_cn/promotion_check"


def _promotion_snapshot() -> dict:
    """上线闸门快照：影子组合里每条候选改动"能不能进生产"的判定（脚本：scripts/promotion_check.py）。

    只读。样本不够时它给 KEEP —— 这是刻意设计（宁可不动生产）。
    """
    if not PROMOTION.exists():
        return {"available": False}
    dirs = sorted((p for p in PROMOTION.iterdir() if p.is_dir()), reverse=True)
    if not dirs:
        return {"available": False}
    path = dirs[0] / "verdicts.csv"
    if not path.exists():
        return {"available": False}
    try:
        frame = pd.read_csv(path)
    except Exception:
        return {"available": False}
    cols = ["variant", "label", "vs_baseline", "decision_dates", "common_periods", "better_periods",
            "worse_periods", "median_diff_vs_baseline", "sign_p", "total_return", "max_drawdown",
            "max_single_weight", "min_names_per_date", "verdict", "reason"]
    rows = frame[[c for c in cols if c in frame.columns]].where(pd.notna(frame), None).to_dict("records")
    return {"available": True, "generated_at": dirs[0].name,
            "counts": {k: int(v) for k, v in frame["verdict"].value_counts().items()} if "verdict" in frame else {},
            "rows": rows}


REF_ACC = ROOT / "output/paper_trading/account_reference"
LIVE_MIRROR = ROOT / "output/paper_trading/live_mirror"


def _live_mirror() -> dict:
    """实盘镜像账户（2026-10-12 新增）：真实券商持仓的盯市 + 逐票建议。

    与「生产账户」「参考账户」并列，但**不是一个东西**：
      * 生产/参考账户 = 策略名单驱动的前向模拟（从空仓 45,000 起）；
      * 实盘镜像 = 真实账户的持仓截面（人工录入 config/live_account_holdings.json），
        不下单、不参与权重，只回答「我现在的持仓模型怎么看」。
    所以这里单独返回，前端也单独一栏展示，绝不把三条曲线混在一张图里比大小。
    """
    summary_path = LIVE_MIRROR / "summary.json"
    if not summary_path.exists():
        return {"available": False}
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception:
        return {"available": False}
    positions = _records(LIVE_MIRROR / "positions.csv")
    nav = _records(LIVE_MIRROR / "nav.csv")
    snapshot_path = ROOT / "config/live_account_holdings.json"
    cleared: list[dict] = []
    if snapshot_path.exists():
        try:
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            cleared = [{"stock_code": str(x.get("stock_code")), "name": str(x.get("name") or ""),
                        "realized_pnl": float(x.get("realized_pnl") or 0.0)}
                       for x in (snapshot.get("cleared_positions") or [])]
        except Exception:
            cleared = []
    # 快照年龄：持仓是人工维护的，长期不更新就该在页面上直接提示（而不是拿旧仓位当现状）。
    age_days = None
    try:
        age_days = (datetime.now().date() - datetime.fromisoformat(str(summary["asof"])).date()).days
    except Exception:
        age_days = None
    return {
        "available": True,
        "generated_at": summary.get("generated_at"),
        "asof": summary.get("asof"),
        "broker": summary.get("broker"),
        "account_masked": summary.get("account_masked"),
        "captured_at": summary.get("captured_at"),
        "snapshot_age_days": age_days,
        "price_source": summary.get("price_source"),
        "live_quotes_error": summary.get("live_quotes_error") or "",
        "cash": summary.get("cash"),
        "market_value": summary.get("market_value"),
        "total_assets": summary.get("total_assets"),
        "cost_basis": summary.get("cost_basis"),
        "unrealized_pnl": summary.get("unrealized_pnl"),
        "realized_pnl": summary.get("realized_pnl"),
        "float_pnl_incl_realized": summary.get("float_pnl_incl_realized"),
        "initial_capital_implied": summary.get("initial_capital_implied"),
        "cumulative_return": summary.get("cumulative_return"),
        "n_positions": summary.get("n_positions"),
        "top1_weight": summary.get("top1_weight"),
        "top3_weight": summary.get("top3_weight"),
        "max_weight_single_allowed": summary.get("max_weight_single_allowed"),
        "advice_counts": summary.get("advice_counts") or {},
        "advice_rules": summary.get("advice_rules") or {},
        "observations": summary.get("observations"),
        "sample_status": summary.get("sample_status"),
        "relative_20d": summary.get("relative_20d") or {},
        "picks_date": summary.get("picks_date"),
        "selected_codes": summary.get("selected_codes") or [],
        "preselected_codes": summary.get("preselected_codes") or [],
        "notes": summary.get("notes") or [],
        "reconciliation": summary.get("reconciliation") or {},
        "positions": positions,
        "cleared_positions": cleared,
        "performance_available": bool(summary.get("performance_available")),
        "performance_stats": summary.get("performance_stats") or {},
        "curve": [{"date": r.get("asof_date"), "nav": r.get("nav"), "cum_return": r.get("cum_return"),
                   "drawdown": r.get("drawdown"), "price_source": r.get("price_source")} for r in nav],
    }


@router.get("/live/realtime")
@ttl_cache(2.0, name="live_realtime")
def live_realtime() -> dict:
    """**盘中实时**的实盘账户视图（用户 2026-10-12："实盘的要求每天数据都是实时更新的"）。

    与 `/api/paper` 里那份的差别：那份读的是**盘后落盘**的产物（`positions.csv` / `summary.json`），
    价格是生成时刻的价；这一份每次用**腾讯批量快照**把持仓价换成实时价，再用**同一套算术**
    （`live_mirror.account_state`）重算市值 / 权重 / 浮盈 / 推算初始本金 / 累计 / 集中度。
    拿不到实时价（非交易时段、源不可用）就退回盘后价并把 `price_source` 标成 `disk`，不填 0。

    TTL 2 秒：一个页面 + 多个标签页共享，不会把实时源打穿；前端 15 秒一轮够用。
    """
    positions_path = LIVE_MIRROR / "positions.csv"
    if not positions_path.exists():
        return {"available": False, "reason": "还没有 positions.csv（跑 scripts/build_live_mirror.py）"}
    positions = pd.read_csv(positions_path)
    if positions.empty:
        return {"available": False, "reason": "持仓为空"}
    snapshot_path = ROOT / "config/live_account_holdings.json"
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"available": False, "reason": f"读不到持仓快照：{exc}"}
    cleared = float(sum(float(x.get("realized_pnl") or 0.0)
                        for x in (snapshot.get("cleared_positions") or [])))
    codes = [str(c) for c in positions["stock_code"].tolist()]
    session = live_quotes.trading_phase()
    quotes: dict[str, dict] = {}
    quote_error = ""
    if session.get("live"):
        quotes, quote_error = live_quotes.tencent_quotes(codes)
    else:
        quote_error = f"非交易时段（{session.get('phase')}），用盘后价"
    view = realtime_view(positions, quotes, cash=float(snapshot.get("cash") or 0.0),
                         cleared_pnl=cleared, asof=str(snapshot.get("asof") or ""))
    view.update({
        "available": True,
        "session": session,
        "price_source": "live" if quotes else "disk",
        "quote_error": quote_error,
        "updated_at": datetime.now().strftime("%H:%M:%S"),
        "benchmark_code": "000905.SH",
        # 逐股建议等"一天只变一次"的字段来自盘后产物（positions.csv），实时那部分只改价格口径。
        "advice_source": str(positions_path.relative_to(ROOT)),
    })
    return view


@router.get("/live/performance")
@ttl_cache(30.0, name="live_performance")
def live_performance() -> dict:
    """实盘持仓的「日/周/月 vs 中证500」曲线 + 收益日历（`build_live_mirror.py` 预计算）。

    单独一个接口而不是塞进 `/api/paper`：这份 payload 有 700+ 个日频点 + 700+ 个日历格，
    挂在主接口上会让模拟盘页每次刷新都白带几百 KB。页面按需取，30 秒 TTL。
    """
    path = LIVE_MIRROR / "performance.json"
    if not path.exists():
        return {"available": False,
                "reason": "还没有 performance.json；跑 uv run python scripts/build_live_mirror.py"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"available": False, "reason": f"读取失败：{exc}"}


def _reference_account() -> dict:
    """参考账户：模拟盘绑定的那条规则（第三十七轮）。

    与影子组合里同名变体**同一份名单历史**，所以净值必然逐位相同 —— consistency.json 每次核对。
    它与「生产账户」（output/paper_trading/account/）**执行口径相同、选股名单不同**，
    两个净值不要直接比大小。
    """
    summary_path = REF_ACC / "paper_account_summary.json"
    if not summary_path.exists():
        return {"available": False}
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        variant = json.loads((REF_ACC / "variant.json").read_text(encoding="utf-8"))
    except Exception:
        return {"available": False}
    consistency = {}
    if (REF_ACC / "consistency.json").exists():
        try:
            consistency = json.loads((REF_ACC / "consistency.json").read_text(encoding="utf-8"))
        except Exception:
            consistency = {}
    nav = _records(REF_ACC / "nav.csv")
    fills = _records(REF_ACC / "fills.csv")
    # 曲线从第一个决策日开始截（前面是空仓平线）
    first = (consistency.get("vs_production_account") or {}).get("reference_decision_dates")
    history = ROOT / "output/results_cn/shadow_variants" / f"{variant.get('variant')}.csv"
    live_from = None
    if history.exists():
        import pandas as _pd
        live_from = str(_pd.read_csv(history, usecols=["trade_date"])["trade_date"].min())[:10]
    curve = [{"date": r.get("asof_date"), "nav": r.get("nav"), "drawdown": r.get("drawdown")}
             for r in nav if live_from is None or str(r.get("asof_date")) >= live_from]
    return {
        "available": True, "variant": variant.get("variant"), "label": variant.get("label"),
        "history": variant.get("history"), "first_decision": live_from,
        "final_nav": summary.get("final_nav"), "max_drawdown": summary.get("max_drawdown"),
        "nav_rows": summary.get("nav_rows"),
        "curve": curve,
        "positions": _records_latest(REF_ACC / "positions.csv"),
        "fills": _round_records(fills[-40:], {"price": 4, "commission": 4}),
        "n_fills": len(fills),
        "consistency": consistency,
    }


def _public_assumptions() -> dict:
    """给前端的假设快照：去掉 `_doc` / `_note` 这类注释键（它们只给人看，不必进接口）。"""
    def strip(node):
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if not k.startswith("_")}
        return node

    return strip(load_assumptions())


def _forward_hint() -> dict:
    """长样本口径的入口摘要（详细在 /api/forward-validation）：告诉前端"另一条口径有多少决策日"。"""
    path = ROOT / "output/results_cn/forward_validation/summary.json"
    if not path.exists():
        return {"available": False}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"available": False}
    sources = payload.get("sources", {})
    return {"available": bool(sources), "horizon_days": payload.get("horizon_days"),
            "sources": {k: {"decision_dates": v.get("decision_dates"), "window": v.get("window"),
                            "mean_period_return": v.get("mean_period_return"),
                            "win_rate_periods": v.get("win_rate_periods")} for k, v in sources.items()}}


def _records(path: Path, tail: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    frame = pd.read_csv(path)
    if tail:
        frame = frame.tail(tail)
    return frame.where(pd.notna(frame), None).to_dict("records")


def _records_latest(path: Path) -> list[dict]:
    """只取最后一个 asof_date 的持仓。"""
    if not path.exists():
        return []
    frame = pd.read_csv(path)
    if frame.empty or "asof_date" not in frame.columns:
        return []
    latest = frame["asof_date"].astype(str).max()
    frame = frame[frame["asof_date"].astype(str) == latest]
    return frame.where(pd.notna(frame), None).to_dict("records")


def _round_records(rows: list[dict], cols: dict) -> list[dict]:
    """价格/佣金留 4 位小数 —— 原始 float 会带出 31.034475000000004 这种噪声。"""
    for r in rows:
        for col, nd in cols.items():
            if isinstance(r.get(col), (int, float)):
                r[col] = round(float(r[col]), nd)
    return rows


@router.get("")
def paper_status() -> dict:
    nav = _records(ACC / "nav.csv")
    summary = {}
    if (ACC / "paper_account_summary.json").exists():
        summary = json.loads((ACC / "paper_account_summary.json").read_text(encoding="utf-8"))

    # 净值曲线从**首个决策日**开始截：账户在 2024-09 就建了，但直到第一个决策日之前
    # 都是"没买任何东西"的平坦 45,000 —— 496 行里有 470+ 行是平的，
    # 画出来会把真正有信息的那 20 天压成一条竖线（实测第一版就是这样）。
    live_from = None
    if HISTORY.exists():
        h = pd.read_csv(HISTORY, usecols=["trade_date"])
        live_from = str(h["trade_date"].min())
    curve = [
        {"date": r.get("asof_date"), "nav": r.get("nav"), "drawdown": r.get("drawdown")}
        for r in nav
        if live_from is None or str(r.get("asof_date")) >= live_from
    ]

    history_dates = 0
    window = [live_from, None]
    if HISTORY.exists():
        h = pd.read_csv(HISTORY, usecols=["trade_date"])
        history_dates = int(h["trade_date"].nunique())
        window = [str(h["trade_date"].min()), str(h["trade_date"].max())]

    first_nav = nav[0].get("nav") if nav else None
    last_nav = nav[-1].get("nav") if nav else None
    return {
        "available": bool(nav),
        # 用 fromtimestamp 取本地时间：`pd.Timestamp(x, unit="s")` 给的是 UTC，会少 8 小时
        # （这个坑在 calendar.py 上踩过一次）
        "generated_at": datetime.fromtimestamp((ACC / "nav.csv").stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        if (ACC / "nav.csv").exists() else None,
        "explain": {
            "title": "这是什么？",
            "what": ("**唯一的「前向」口径**：从 2026-09-09 起，按**当天实际最终名单**下单，"
                     "以 T+1 开盘价成交、整手买入，扣佣金(万2.5)+滑点(万3)+印花税(千1，卖出)，"
                     "并建模涨跌停（涨停不买、跌停卖不出）、停牌（持仓按最后价盯市）与退市强制清算。"
                     "它不是回测——回测是把历史重算一遍，这里是「照着当天的名单真的下单会怎样」。"),
            "why_short": (f"历史只有 **{history_dates} 个决策日**（{window[0]} ~ {window[1]}）——"
                          "因为最终名单 `cn_ensemble_selected.csv` 此前**每天被覆盖、没有归档**，"
                          "2026-10-09 才补上归档，所以样本很短。"),
            "not_what": "不是实盘（没有真钱、没有滑点外的市场冲击）；样本仅一个月，不构成统计结论。",
        },
        "account": {
            "initial_capital": 45000.0,
            "final_nav": summary.get("final_nav"),
            "max_drawdown": summary.get("max_drawdown"),
            "nav_rows": summary.get("nav_rows"),
            "window": window,
            "decision_dates": history_dates,
        },
        "curve": curve,
        # 执行假设（与回放统一）+ 一致性快照：前端把这两块并排展示，口径不再靠人记
        "assumptions": _public_assumptions(),
        "parity": parity_snapshot(),
        "shadow": _shadow_summary(),
        "guard": _guard_snapshot(),
        "promotion": _promotion_snapshot(),
        "reference": _reference_account(),
        # 实盘镜像（真实持仓）：与上面三条曲线**不可直接比大小**，见函数注释。
        "live": _live_mirror(),
        "forward": _forward_hint(),
        # 持仓只展示**最新一天**的：原来 tail(40) 会把好几天的持仓混在一起，
        # 看起来像"同时持有 40 只"（实测第一版如此）。
        "positions": _records_latest(ACC / "positions.csv"),
        "fills": _round_records(_records(ACC / "fills.csv"), {"price": 4, "commission": 4}),
        "orders": _records(ACC / "orders.csv"),
    }
