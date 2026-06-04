"""Automated stock selection report generation via LLM.

Fetches real-time financial data, builds a structured prompt, and generates
a comprehensive per-stock analysis saved to docs/report/.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import requests

from core.llm.client import LLMClient

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

SYSTEM_PROMPT = """你是一位资深量化分析师，负责审核 AI 选股系统的输出。

对每只股票，请基于提供的数据做简洁分析，输出格式如下：

## {股票代码} {股票名称}

| 维度 | 数据 | 评价 |
|------|------|------|
| 基本面 | PE/PB/市值 | 估值判断 |
| 技术面 | 胜率/Setup/趋势 | 信号质量 |
| 风险 | 回撤/过热/趋势惩罚 | 风险提示 |

**判断**：一句话总结，给出 强烈推荐/推荐/可买入/谨慎/跳过 评级和理由。

---

最后给出汇总：

## 汇总

| 梯队 | 股票 | 理由 |
|------|------|------|

**组合建议**：等权买入哪些、跳过哪些、仓位建议。

注意：
- 亏损或负PE要标注风险
- PB>10 在小盘股中常见但需提示
- 换手率为0可能是停牌或数据缺失
- 过热(overheat>50)的股票追高风险大
- 使用中文，简洁有力"""


def _pad_code(code: str | int) -> str:
    return str(int(code)).zfill(5)


def fetch_stock_info_batch(
    stock_codes: list[str],
    max_workers: int = 10,
) -> dict[str, dict[str, str]]:
    """Fetch name, PE, PB, market cap for a batch of HK stocks."""

    def _fetch_one(code: str) -> tuple[str, dict[str, str]]:
        try:
            url = f"https://qt.gtimg.cn/q=r_hk{code}"
            r = requests.get(url, headers={"User-Agent": UA}, timeout=10)
            r.encoding = "gbk"
            m = re.search(r'"(.+)"', r.text)
            if not m:
                return code, {}
            fields = m.group(1).split("~")
            return code, {
                "name": fields[1] if len(fields) > 1 else "?",
                "price": fields[3] if len(fields) > 3 else "?",
                "change_pct": fields[32] if len(fields) > 32 else "?",
                "pe": fields[39] if len(fields) > 39 else "?",
                "pb": fields[43] if len(fields) > 43 else "?",
                "mkt_cap": fields[44] if len(fields) > 44 else "?",
                "turnover": fields[47] if len(fields) > 47 else "?",
            }
        except Exception:
            return code, {}

    result: dict[str, dict[str, str]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_one, c): c for c in stock_codes}
        for future in as_completed(futures):
            code, info = future.result()
            if info:
                result[code] = info
    return result


def build_stock_table(
    selected: list[dict[str, Any]],
    stock_info: dict[str, dict[str, str]],
) -> str:
    """Build a markdown table of selected stocks for the LLM prompt."""
    lines = [
        "| 代码 | 名称 | 价格 | 涨跌 | PE | PB | 市值(亿) | 换手率 | 胜率 | Setup | 预期3M | 回撤罚分 | 过热罚分 | 趋势罚分 |",
        "|------|------|------|------|-----|-----|------|------|------|------|------|------|------|------|",
    ]
    for item in selected:
        code = _pad_code(item.get("stock_code", ""))
        info = stock_info.get(code, {})
        lines.append(
            f"| {code} | {info.get('name','?')} | {info.get('price','?')} | {info.get('change_pct','?')}% | "
            f"{info.get('pe','?')} | {info.get('pb','?')} | {info.get('mkt_cap','?')} | {info.get('turnover','?')} | "
            f"{item.get('win_rate',0):.1f}% | {item.get('setup_type','?')} | "
            f"{item.get('expected_3m_score',0):.1f} | "
            f"{item.get('drawdown_penalty_score',0):.0f} | {item.get('overheat_penalty_score',0):.0f} | "
            f"{item.get('downtrend_penalty_score',0):.0f} |"
        )
    return "\n".join(lines)


def build_industry_attribution_prompt(portfolio_result: dict[str, Any]) -> str:
    """Build a compact Core/Overlay attribution section for the LLM prompt."""
    selected = portfolio_result.get("selected", [])
    attribution = portfolio_result.get("industry_attribution_table", [])
    if not selected and not attribution:
        return ""

    lines = [
        "## 行业内 Alpha / 行业机会归因",
        "",
        f"- 组合行业 HHI: {portfolio_result.get('industry_hhi', 'N/A')}",
        f"- 已投资仓位归一化 HHI: {portfolio_result.get('industry_hhi_invested', 'N/A')}",
        "",
        "| 代码 | 行业 | Core Alpha | Overlay机会 | Bucket | 入选层 | 行业预算原因 |",
        "|------|------|------------|-------------|--------|--------|--------------|",
    ]
    for item in selected:
        code = _pad_code(item.get("stock_code", ""))
        lines.append(
            f"| {code} | {item.get('industry_l2') or item.get('industry_l1') or '?'} | "
            f"{float(item.get('industry_alpha_score', 0) or 0):.1f} | "
            f"{float(item.get('industry_opportunity_score', 0) or 0):.1f} | "
            f"{item.get('industry_timing_bucket', '?')} | "
            f"{item.get('selection_layer', '?')} | "
            f"{item.get('industry_budget_reason', '?')} |"
        )

    if attribution:
        lines.extend([
            "",
            "行业归因摘要：",
            "",
            "| 行业 | 候选 | 入选 | 平均Alpha | 平均机会 | Bucket |",
            "|------|------|------|----------|----------|--------|",
        ])
        for item in attribution[:10]:
            lines.append(
                f"| {item.get('industry', '?')} | {item.get('eligible_count', 0)} | "
                f"{item.get('selected_count', 0)} | "
                f"{float(item.get('avg_industry_alpha_score', 0) or 0):.1f} | "
                f"{float(item.get('avg_industry_opportunity_score', 0) or 0):.1f} | "
                f"{item.get('industry_timing_bucket', '?')} |"
            )
    return "\n".join(lines)


def generate_selection_report(
    portfolio_result: dict[str, Any],
    *,
    api_key: str | None = None,
    model: str | None = None,
    formula_version: str = "v11",
    extra_context: str = "",
) -> str:
    """Generate a comprehensive stock selection analysis report.

    Args:
        portfolio_result: Output from backtest_hk_market / backtest_portfolio.
        api_key: DeepSeek API key.
        model: DeepSeek model name (default: deepseek-v4-pro).
        formula_version: Label for the ranking formula version.
        extra_context: Additional context to include in the prompt.

    Returns:
        Full markdown report text.
    """
    selected = portfolio_result.get("selected", [])
    watchlist = portfolio_result.get("watchlist", [])
    if not selected:
        raise ValueError("No selected stocks in portfolio_result")

    all_codes = [_pad_code(s.get("stock_code", "")) for s in selected]
    all_codes += [_pad_code(w.get("stock_code", "")) for w in watchlist]
    all_codes = list(dict.fromkeys(all_codes))

    stock_info = fetch_stock_info_batch(all_codes)

    selected_table = build_stock_table(selected, stock_info)
    watchlist_table = build_stock_table(watchlist, stock_info)
    industry_attribution_section = build_industry_attribution_prompt(portfolio_result)

    params = portfolio_result.get("params", {})
    est_return = portfolio_result.get("estimated_portfolio_return", "N/A")
    est_wr = portfolio_result.get("estimated_portfolio_win_rate", "N/A")

    user_prompt = f"""## 运行参数
- 公式版本: {formula_version}
- 模式: {params.get('analysis_mode', 'lightgbm')}
- 因子集: {params.get('factor_set', '?')}
- 回溯: {params.get('days', '?')}天
- 组合估算收益: {est_return}%
- 组合估算胜率: {est_wr}%

## Top {len(selected)} 持仓

{selected_table}

## 观察名单

{watchlist_table}

{industry_attribution_section}

{extra_context}

请对每只持仓股票做详细分析，给出买入建议和风险提示，最后汇总。"""

    client = LLMClient(api_key=api_key, model=model)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    response = client.chat_with_retry(messages, temperature=0.5, max_tokens=4096)
    return response


def save_report(report_text: str, report_dir: str | Path = "docs/report") -> Path:
    """Save the report to docs/report/{date}_{version}_llm.md.

    Returns the path to the saved report.
    """
    report_path = Path(report_dir)
    report_path.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Find existing reports for today to increment counter
    existing = sorted(report_path.glob(f"{today}_*_llm.md"))
    suffix = f"_{len(existing) + 1}" if existing else ""

    filename = f"{today}{suffix}_llm.md"
    filepath = report_path / filename

    filepath.write_text(report_text, encoding="utf-8")
    return filepath


def run_auto_report(
    portfolio_result: dict[str, Any],
    *,
    api_key: str | None = None,
    model: str | None = None,
    formula_version: str = "v11",
    report_dir: str | Path = "docs/report",
    extra_context: str = "",
) -> Path | None:
    """End-to-end: generate LLM report and save to disk.

    Returns the file path, or None on failure.
    """
    try:
        report = generate_selection_report(
            portfolio_result,
            api_key=api_key,
            model=model,
            formula_version=formula_version,
            extra_context=extra_context,
        )
    except Exception as exc:
        print(f"[LLM] Report generation failed: {exc}")
        return None

    path = save_report(report, report_dir=report_dir)
    print(f"[LLM] Report saved to {path}")
    return path
