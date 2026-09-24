#!/usr/bin/env python
"""生成飞书选股通知：股票 / 名称行业 / 路径与原因 / 模型分排名 / RPS / PK 状态与风险。

输出 Markdown（可直接用于 `lark-cli im +messages-send --markdown`）与 JSON 行数据，
默认写入 `output/results_cn/feishu_<date>.md|.json`。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "output" / "results_cn"
RPS_WINDOWS = (5, 10, 20, 30, 60)


def _market_data(codes: list[str], end_date: str) -> tuple[dict, dict, pd.DataFrame]:
    """stock info（名称/行业）、每票 RPS、成交额中位数。"""
    import sys

    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from data.ingest.service import MarketDataService

    service = MarketDataService()
    warehouse = service.warehouse
    info = warehouse.read_stock_info(stock_codes=codes, market="CN")
    name_map, industry_map = {}, {}
    if info is not None and not info.empty:
        for _, row in info.iterrows():
            code = str(row["stock_code"])
            name_map[code] = str(row.get("name") or "")
            industry = row.get("industry_l2") or row.get("industry_l1") or ""
            industry_map[code] = str(industry)
    start = (pd.to_datetime(end_date) - pd.Timedelta(days=200)).strftime("%Y-%m-%d")
    bars = warehouse.read_ohlcv(
        market="CN", asset_type="equity", frequency="daily", adjust="qfq",
        stock_code=codes, start_date=start, end_date=end_date,
        columns=["stock_code", "trade_date", "close", "amount"],
    )
    rps_map: dict[str, str] = {}
    amount_map: dict[str, float] = {}
    if bars is not None and not bars.empty:
        bars = bars.copy()
        bars["trade_date"] = pd.to_datetime(bars["trade_date"])
        bars = bars.sort_values(["stock_code", "trade_date"])
        closes = bars.pivot_table(index="trade_date", columns="stock_code", values="close", aggfunc="last").sort_index()
        amounts = bars[bars["trade_date"] == pd.to_datetime(end_date)].groupby("stock_code")["amount"].median()
        amount_map = {str(code): float(value) for code, value in amounts.items() if pd.notna(value)}
        ranks = {}
        for window in RPS_WINDOWS:
            if len(closes) > window:
                momentum = closes.iloc[-1] / closes.iloc[-1 - window] - 1.0
                ranks[window] = momentum.rank(pct=True) * 100.0
        for code in closes.columns:
            values = []
            for window in RPS_WINDOWS:
                series = ranks.get(window)
                values.append(str(int(round(float(series.get(code, np.nan))))) if series is not None and pd.notna(series.get(code)) else "-")
            rps_map[str(code)] = "/".join(values)
    return name_map, industry_map, pd.DataFrame({"rps": pd.Series(rps_map), "median_amount": pd.Series(amount_map)})


def _reason_text(row: pd.Series) -> str:
    channel = str(row.get("selection_channel") or "")
    if channel == "model":
        rank = "" if pd.isna(row.get("rank")) else int(row["rank"])
        return f"模型 Top-N（rank {rank}）"
    sleeve = row.get("selection_sleeve")
    sleeve = "" if pd.isna(sleeve) else str(sleeve)
    parts = [f"{str(row.get('signal_type'))}，信号 {row.get('signal_score')}"]
    if pd.notna(row.get("signal_gain_1d")):
        parts.append(f"单日约 +{float(row['signal_gain_1d'])*100:.2f}%")
    if pd.notna(row.get("signal_relative20")):
        parts.append(f"相对行业 20 日 {float(row['signal_relative20'])*100:+.2f}%")
    if pd.notna(row.get("signal_rebound_atr")):
        parts.append(f"反弹 {float(row['signal_rebound_atr']):.2f} ATR")
    if pd.notna(row.get("signal_volume_ratio_20")):
        parts.append(f"量比 {float(row['signal_volume_ratio_20']):.2f}")
    if pd.notna(row.get("signal_stop_price")):
        parts.append(f"止损 {float(row['signal_stop_price'])}")
    prefix = f"{sleeve} sleeve：" if sleeve else ""
    suffix = "（强制）" if channel == "signal_override" else "（仅候选，未强制）"
    return prefix + "，".join(parts) + suffix


def _risk_text(row: pd.Series, weight: float, lots, extra: dict) -> str:
    rps = str(row.get("rps") or "")
    notes = []
    try:
        parts = [int(value) for value in rps.split("/") if value not in {"", "-"}]
    except ValueError:
        parts = []
    if len(parts) == len(RPS_WINDOWS):
        short = float(np.mean(parts[:2]))
        long = float(np.mean(parts[-2:]))
        if short >= 70 and long <= 45:
            notes.append("短期强、长期弱（风格切换风险）")
        elif short <= 45 and long >= 70:
            notes.append("短期弱、长期强")
        elif long <= 45:
            notes.append("RPS 中长期偏弱")
        elif short >= 70 and long >= 70:
            notes.append("RPS 多周期同向偏强")
    amount = row.get("median_amount")
    if pd.notna(amount):
        amount_yi = float(amount) / 1e8
        liquidity = "流动性一般" if amount_yi < 1.0 else ("流动性充足" if amount_yi >= 3.0 else "流动性中等")
        notes.append(f"成交额中位数约 {amount_yi:.2f} 亿元，{liquidity}")
    if weight and weight > 0:
        lots_text = "" if lots is None or pd.isna(lots) else f"，可买 {int(lots)} 手"
        notes.insert(0, f"PK {weight:.2%}{lots_text}")
        scale = (extra.get("scalers") or {}).get(row.get("stock_code"))
        if scale:
            notes.append(f"风控减仓系数 {scale:.2f}")
    else:
        removal = extra.get("removals", {}).get(str(row.get("stock_code")))
        notes.insert(0, f"PK 0 权重；{removal}" if removal else "PK 0 权重")
    return "；".join(notes)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", default=None)
    parser.add_argument("--replay-dir", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--top", type=int, default=12, help="通知里最多展示几只（按池内顺序）")
    args = parser.parse_args()

    source = Path(args.replay_dir) if args.replay_dir else RESULTS
    preselected = pd.read_csv(source / "cn_ensemble_preselected.csv")
    preselected = preselected.loc[:, ~preselected.columns.duplicated()]
    selected = pd.read_csv(source / "cn_ensemble_selected.csv")
    selected = selected.loc[:, ~selected.columns.duplicated()]
    manifest_path = source / "cn_ensemble_portfolio_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    trade_date = args.trade_date or str(preselected["trade_date"].iloc[0])[:10]
    candidate_dates = pd.to_datetime(preselected["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d").dropna().unique()
    candidate_origin_date = str(candidate_dates[0]) if len(candidate_dates) == 1 else None
    output_dir = Path(args.output_dir) if args.output_dir else source

    weights = dict(zip(selected["stock_code"].astype(str), pd.to_numeric(selected["target_weight"], errors="coerce").fillna(0.0)))
    lots = dict(zip(selected["stock_code"].astype(str), pd.to_numeric(selected.get("lots_at_target"), errors="coerce")))
    risk = manifest.get("risk_control") or {}
    lot_summary = manifest.get("lot_execution") or {}

    removal_reasons: dict[str, str] = {}
    for code in lot_summary.get("dropped_unaffordable") or []:
        removal_reasons[str(code)] = "每槽预算买不起一手，被可执行性过滤移除"
    for code in lot_summary.get("dropped_for_lots") or []:
        removal_reasons[str(code)] = "整手修复时移除（目标权重不足一手）"
    for code in lot_summary.get("lifted_for_lots") or []:
        removal_reasons.setdefault(str(code), "整手保底提升到一手")
    for row in lot_summary.get("risk_gate_rejected") or []:
        removal_reasons[str(row.get("stock_code"))] = "风控门槛拒绝：" + ",".join(row.get("reasons") or [])
    extra = {"scalers": (risk.get("scalers") or {}).get("scale_by_name") or {}, "removals": removal_reasons}

    codes = preselected["stock_code"].astype(str).tolist()
    name_map, industry_map, market_frame = _market_data(codes, trade_date)

    rows = []
    ordered = pd.concat([
        preselected[preselected["selection_channel"] == "model"].sort_values("model_score", ascending=False),
        preselected[preselected["selection_channel"] == "signal_override"],
        preselected[preselected["selection_channel"] == "signal_candidate"],
    ]).head(args.top)
    for _, row in ordered.iterrows():
        code = str(row["stock_code"])
        merged = row.copy()
        if code in market_frame.index:
            merged["rps"] = market_frame.loc[code, "rps"]
            merged["median_amount"] = market_frame.loc[code, "median_amount"]
        weight = float(weights.get(code, 0.0) or 0.0)
        rows.append({
            "stock_code": code,
            "name": name_map.get(code, ""),
            "industry": industry_map.get(code, ""),
            "reason": _reason_text(merged),
            "model_score": None if pd.isna(row.get("model_score")) else round(float(row["model_score"]), 2),
            "model_rank": None if pd.isna(row.get("rank")) else int(row["rank"]),
            "rps": str(merged.get("rps") or "-"),
            "target_weight": round(weight, 6),
            "lots": None if pd.isna(lots.get(code)) else int(lots[code]),
            "pk_status": _risk_text(merged, weight, lots.get(code), extra),
        })

    book = [row for row in rows if row["target_weight"] > 0]
    lines = [f"**CN 选股 · {trade_date}**",
             f"候选池 {len(preselected)} 只 → PK 组合 {sum(1 for value in weights.values() if value > 0)} 只；"
             f"毛敞口 {sum(weights.values()):.2%}，事前波动 {risk.get('vol_after')}"
             f"（目标 {risk.get('target_volatility')}，协方差 {risk.get('covariance')}）", ""]
    if candidate_origin_date and candidate_origin_date != trade_date:
        lines += [f"预选来源日：{candidate_origin_date}（沿用候选，非 {trade_date} 当日重选）", ""]
    for row in rows:
        headline = f"**{row['stock_code']}** {row['name']}｜{row['industry']}"
        detail = (f"　路径：{row['reason']}｜模型分/排名：{row['model_score']} / {row['model_rank']}"
                  f"｜RPS(5/10/20/30/60)：{row['rps']}｜{row['pk_status']}")
        lines += [headline, detail, ""]
    lines += [f"组合持仓：{'、'.join(item['stock_code'] for item in book) or '（无）'}"]
    markdown = "\n".join(lines)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"feishu_{trade_date}.md").write_text(markdown + "\n", encoding="utf-8")
    (output_dir / f"feishu_{trade_date}.html").write_text(
        _html_table(trade_date, rows, risk, candidate_origin_date=candidate_origin_date), encoding="utf-8")
    (output_dir / f"feishu_{trade_date}.json").write_text(
        json.dumps({"trade_date": trade_date, "candidate_origin_date": candidate_origin_date,
                    "preselection_carried_forward": bool(candidate_origin_date and candidate_origin_date != trade_date),
                    "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(markdown)
    print(f"\nwritten: {output_dir / f'feishu_{trade_date}.md'}")
    return 0


def _html_table(trade_date: str, rows: list[dict], risk: dict,
                candidate_origin_date: str | None = None) -> str:
    """邮件用 HTML：与运营表相同的六列（股票 / 名称行业 / 路径与原因 / 模型分排名 / RPS / PK 状态与风险）。"""
    header = "".join(
        f'<th style="border:1px solid #d0d7de;padding:6px 10px;background:#f6f8fa;text-align:left">{title}</th>'
        for title in ("股票", "名称/行业", "路径与选择原因", "模型分/排名", "RPS(5/10/20/30/60)", "PK 状态与风险")
    )
    body = []
    for row in rows:
        cells = [
            f"<b>{row['stock_code']}</b>",
            f"{row['name']}<br><span style='color:#57606a'>{row['industry']}</span>",
            row["reason"],
            f"{row['model_score']}<br><span style='color:#57606a'>rank {row['model_rank']}</span>",
            row["rps"],
            row["pk_status"],
        ]
        body.append("<tr>" + "".join(
            f'<td style="border:1px solid #d0d7de;padding:6px 10px;vertical-align:top">{cell}</td>'
            for cell in cells) + "</tr>")
    origin_note = (
        f"<p>预选来源日：{candidate_origin_date}（沿用候选，非当日重选）</p>"
        if candidate_origin_date and candidate_origin_date != trade_date else ""
    )
    return (
        "<html><body style=\"font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;font-size:13px\">"
        f"<h2>CN 选股 · {trade_date}</h2>"
        f"<p>组合 {sum(1 for row in rows if row['target_weight'] > 0)} 只；毛敞口 "
        f"{sum(row['target_weight'] for row in rows):.2%}；事前波动 {risk.get('vol_after')}"
        f"（目标 {risk.get('target_volatility')}，协方差 {risk.get('covariance')}）</p>"
        f"{origin_note}"
        f'<table style="border-collapse:collapse;font-size:12px"><thead><tr>{header}</tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table>"
        "<p style=\"color:#57606a\">RPS 为横截面分位（5/10/20/30/60 交易日）；PK 状态含权重、可买手数与风控处理。</p>"
        "</body></html>"
    )


if __name__ == "__main__":
    raise SystemExit(main())
