"""收益日历的列对齐 + 分时面板的股票标头（用户 2026-10-13 的两条反馈）。

① 用户："2026-10-12 不是星期一么？这个日期是不是排错了啊"
   现场（Playwright 量 DOM，:8000 的实盘镜像页）：2026-10 的格子落点是
       8 → 「四」✓  9 → 「五」✓  12 → 「六」✗（应该是下一行的「一」）
   根因：`calendarBlocks` 只在月初补一次空格（pads）然后**顺序填充**，
   而格子只有交易日 —— 周末/节假日缺席 ⇒ 后面的日期整体左移。
   修法：每个格子按**自己那天是周几**显式占列（`grid-column`），缺口自然留白。

② 用户："K 线选股 点中某只股票的时候，分时线看不到股票标头呢"
   修法：`KlinePage` 的「当日分时」面板加 `.intraday-head`（代码 / 名称 / 最新价 ±涨跌幅 / 行业·板块·分时标的 / 时段），
   实盘镜像页的「当前」pill 也带上名称。

本文件是这两条的文件级回归锁（DOM 级取证在 output/verification/p1_93_*/check_*.txt 里）。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "frontend/src/pages/LiveAccount.vue"
KLINE = ROOT / "frontend/src/pages/KlinePage.vue"


# ── ① 收益日历：按星期占列 ───────────────────────────────────────────────────
def test_earnings_calendar_places_cells_by_weekday():
    text = LIVE.read_text(encoding="utf-8")
    assert "function weekdayCol(" in text, "缺少按星期取列号的函数"
    assert "(new Date(`${period}T00:00:00`).getDay() + 6) % 7" in text, \
        "列号必须是「周一起算」（getDay(): 周日=0 → +6 再取模）"
    assert "gridColumn: String(cell.col)" in text, "格子没有按 col 显式占列"


def test_earnings_calendar_stopped_sequential_padding():
    text = LIVE.read_text(encoding="utf-8")
    assert 'class="cal-pad"' not in text, "还在用「月初补空格 + 顺序填充」的方式（缺交易日就会错位）"
    assert "pads: 0" in text, "块结构里应把 pads 归零，避免两种定位方式混用"


# ── ② 分时标头 ───────────────────────────────────────────────────────────────
def test_kline_intraday_panel_has_stock_header():
    text = KLINE.read_text(encoding="utf-8")
    assert 'class="intraday-head"' in text, "分时面板缺少股票标头"
    head = text[text.index('class="intraday-head"'):]
    head = head[: head.index("</div>")]
    assert "data?.code" in head and "data?.name" in head, "标头必须显示代码与名称"
    assert "intraday.latest.price" in head and "intraday.latest.change_pct" in head, "标头必须显示最新价与涨跌幅"


def test_live_page_current_pill_shows_name():
    text = LIVE.read_text(encoding="utf-8")
    assert "const pickedName = computed(" in text, "缺少当前票名称的计算属性"
    assert "<span v-if=\"pickedName\">{{ pickedName }}</span>" in text, "「当前」pill 没有显示名称"
    assert "2026-10-13" in text, "缺少这次修复的原因注释（后来的人才知道为什么这么写）"
