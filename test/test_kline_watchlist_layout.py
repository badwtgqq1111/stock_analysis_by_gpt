"""K 线页左侧「自选股」列表的排版回归锁（用户 2026-10-13：「自选股票名称看不见」）。

事故：`.rows .name` 写的是 `flex: 1`（= flex-basis:0），而一行里的 code / 来源标签 / 实时价
又都不可压缩 ⇒ **名称是第一个被压到 0px 的列**。实测（Playwright 量 DOM，1500×1100）：
83 行里 **42 行名称宽度 ≤1px**（来源标签越长越惨；一只票命中多条策略时标签会拼成一长串）。
修法：给名称一个下限（`min-width`），让来源标签去收缩省略；多策略命中时标签只在末尾加 `+N`。

本文件把"名称列必须有下限""标签不得无限增长""行上要有 title 兜底"三条钉住 ——
它们都不是"能不能跑"的问题，而是"看不见"的问题，只有断言能防回归。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WATCHLIST = ROOT / "frontend/src/components/StockWatchlist.vue"
STRATEGIES = ROOT / "frontend/src/components/watchlistStrategies.ts"


def test_name_column_has_min_width():
    text = WATCHLIST.read_text(encoding="utf-8")
    rule = next(line for line in text.splitlines() if ".rows .name" in line)
    assert "min-width" in rule, f"名称列没有下限（又会被挤成 0 宽）：{rule.strip()}"
    assert "flex: 1;" not in rule, f"名称列不能再用 `flex: 1`（flex-basis:0 ⇒ 第一个被压扁）：{rule.strip()}"
    assert "text-overflow: ellipsis" in text


def test_tag_column_is_the_one_that_shrinks():
    text = WATCHLIST.read_text(encoding="utf-8")
    rule = next(line for line in text.splitlines() if ".rows .tag" in line)
    assert "flex: 0 1 auto" in rule and "min-width: 0" in rule, f"来源标签必须可收缩：{rule.strip()}"


def test_row_has_title_fallback():
    text = WATCHLIST.read_text(encoding="utf-8")
    assert ":title=\"`${row.code} ${row.name" in text, "行上少了 title（省略时必须能悬停看全称）"


def test_multi_strategy_tag_is_capped():
    text = STRATEGIES.read_text(encoding="utf-8")
    assert "baseTag" in text and "+${extra}" in text, "一只票命中多条策略时标签仍会无限增长"
    assert "existing.tag += ` · ${g.label}`" not in text, "旧的拼接写法又回来了（会把名称挤没）"
