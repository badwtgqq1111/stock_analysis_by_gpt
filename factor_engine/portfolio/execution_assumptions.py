"""A 股执行假设的**唯一来源**（模拟盘与日频回放共用）。

为什么要抽这一层（2026-10-09）
------------------------------
`factor_engine/portfolio/paper_account.py`（模拟盘：唯一的前向口径）与
`scripts/replay_daily_execution.py`（日频逐笔回放：L3 口径）原本各写各的常数：

    模拟盘  佣金 5bp + 滑点 5bp、**无印花税**、不建模涨跌停/停牌/退市
    回放    佣金 2.5bp + 滑点 3bp + **印花税 10bp（卖出）**、涨停不买、
            跌停/涨停不卖（顺延）、停牌不可交易、连续 20 日无行情强制清算

同一个策略在两套引擎下算出来的净值**不可比**（谁高谁低都可能只是成本口径差异）。
现在两边都从 `config/execution_assumptions_cn.json` 读，想改假设只改那份 JSON；
`scripts/execution_parity_check.py` 会核对两边**实际生效值**是否一致（不一致就退出码非 0）。

未建模（诚实记录，都在 JSON 的 `_note` 里）：
  * ST 5% 涨跌停（需要 PIT 名称变更史才能判断当天是否 ST）；
  * 分红除权对"前收"的影响（用的是后复权/前复权口径的价差近似）；
  * 市场冲击成本（滑点已含，但没有按参与率分档）。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / "config/execution_assumptions_cn.json"

# 兜底值：JSON 缺失/损坏时也保证引擎能跑（数值与 JSON 保持一致）
_FALLBACK = {
    "version": "builtin-fallback",
    "lot_size": 100,
    "costs": {"commission_bps": 2.5, "slippage_bps": 3.0, "stamp_tax_bps_sell": 10.0},
    "price_limit": {
        "main_board": 0.10, "chinext_star": 0.20, "chinext_star_from": "2020-08-24",
        "basis": "execution_price_vs_prev_close", "tolerance": 0.005,
        "on_limit_up": "no_buy", "on_limit_down": "no_sell", "no_sell_on_limit_up": True,
    },
    "suspension": {"buy": "skip", "sell": "hold_last_price", "delist_after_missing_sessions": 20},
    "fill_reference": {"paper_account": "next_session_open", "replay": "decision_close"},
}


@lru_cache(maxsize=4)
def load_assumptions(path: str | None = None) -> dict:
    """读执行假设（带缓存；显式给 path 时不缓存到默认键）。"""
    target = Path(path) if path else DEFAULT_PATH
    if not target.exists():
        return json.loads(json.dumps(_FALLBACK))
    data = json.loads(target.read_text(encoding="utf-8"))
    merged = json.loads(json.dumps(_FALLBACK))
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def assumptions_path(path: str | None = None) -> Path:
    return Path(path) if path else DEFAULT_PATH


def price_limit_ratio(code: str, day, assumptions: dict | None = None) -> float:
    """涨跌停幅度：创业板/科创板 20%（创业板 2020-08-24 起），其余 10%。"""
    cfg = (assumptions or load_assumptions())["price_limit"]
    code = str(code)
    if code.startswith(("300", "301", "688")):
        start = pd.Timestamp(cfg.get("chinext_star_from", "2020-08-24"))
        return float(cfg["chinext_star"]) if pd.Timestamp(day) >= start else float(cfg["main_board"])
    return float(cfg["main_board"])


def _change(price: float, prev_close: float | None) -> float | None:
    if prev_close is None or pd.isna(prev_close) or float(prev_close) <= 0 or pd.isna(price):
        return None
    return float(price) / float(prev_close) - 1.0


def is_limit_up(code: str, day, price: float, prev_close: float | None, assumptions: dict | None = None) -> bool:
    """执行价相对**前收**涨幅达到上限 → 视为涨停（不可买入 / 按策略不卖出）。"""
    cfg = (assumptions or load_assumptions())["price_limit"]
    chg = _change(price, prev_close)
    if chg is None:
        return False
    return bool(chg >= price_limit_ratio(code, day, assumptions) - float(cfg.get("tolerance", 0.005)))


def is_limit_down(code: str, day, price: float, prev_close: float | None, assumptions: dict | None = None) -> bool:
    """执行价相对**前收**跌幅达到下限 → 视为跌停（卖不出）。"""
    cfg = (assumptions or load_assumptions())["price_limit"]
    chg = _change(price, prev_close)
    if chg is None:
        return False
    return bool(chg <= -(price_limit_ratio(code, day, assumptions) - float(cfg.get("tolerance", 0.005))))


def curse_month(day) -> bool:
    """魔咒月：每年 1/5~2/5 与 4/5~4/30（原文"一股都不买"）。"""
    md = (pd.Timestamp(day).month, pd.Timestamp(day).day)
    return (1, 5) <= md <= (2, 5) or (4, 5) <= md <= (4, 30)


def cost_model(assumptions: dict | None = None) -> dict:
    """双向成本（小数，不是 bp）：buy/sell 各自的费率合计。"""
    cfg = (assumptions or load_assumptions())["costs"]
    commission = float(cfg["commission_bps"]) / 10_000
    slippage = float(cfg["slippage_bps"]) / 10_000
    stamp = float(cfg.get("stamp_tax_bps_sell", 0.0)) / 10_000
    return {
        "commission_bps": float(cfg["commission_bps"]),
        "slippage_bps": float(cfg["slippage_bps"]),
        "stamp_tax_bps_sell": float(cfg.get("stamp_tax_bps_sell", 0.0)),
        "commission": commission,
        "slippage": slippage,
        "stamp_tax_sell": stamp,
        "buy_total": commission + slippage,
        "sell_total": commission + slippage + stamp,
    }
