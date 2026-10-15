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


# ── 实时价丢失（2026-10-15）：接口只收 40 只、前端一次发 60 只 ⇒ 列表末尾整组没有实时价 ──

def test_live_quotes_are_chunked_to_endpoint_limit():
    """`/api/intraday/quotes` 有硬上限 40；前端必须**分批**，不能一次发完再被截断。

    用户看到的现象：K 线页左侧「动态诊断候选」整组没有实时价，上面两组有
    —— 分组列表变长后，末尾那批代码在第 40 个之后被后端静默砍掉。
    """
    text = (ROOT / "frontend/src/composables/useLiveQuotes.ts").read_text(encoding="utf-8")
    assert "MAX_CODES_PER_REQUEST = 40" in text, "分批大小必须与后端上限一致（否则又会出现尾巴丢价）"
    assert "getIntradayQuotes(wanted.slice(0, 60))" not in text, \
        "旧写法：一次发 60 只、后端只收 40 ⇒ 后 20 只永远没价（注释里提它没关系，代码里不许再有）"
    assert "for (let i = 0; i < wanted.length; i += MAX_CODES_PER_REQUEST)" in text, "没有分批循环"
    assert "Promise.allSettled" in text, "分批要并行且互不拖累（allSettled，别用 all）"
    assert "部分批次失败" in text, "部分批次失败要在 UI 上说出口"


def test_quotes_endpoint_declares_its_limit():
    """后端要把「被截断」说出口（响应带 truncated/requested/missing），调用方才可能发现。"""
    from fastapi.testclient import TestClient

    import sys as _sys

    _sys.path.insert(0, str(ROOT))
    from backend.main import app

    text = (ROOT / "backend/api/intraday.py").read_text(encoding="utf-8")
    assert "MAX_QUOTES_CODES = 40" in text and '"truncated"' in text
    codes = ",".join(f"{i:06d}.SZ" for i in range(1, 46))       # 45 只 ⇒ 必被截断
    with TestClient(app) as client:
        payload = client.get(f"/api/intraday/quotes?codes={codes}").json()
    assert payload["requested"] == 45 and payload["limit"] == 40 and payload["truncated"] is True
    assert len(payload["quotes"]) == 40
    assert {"requested", "truncated", "missing"} <= set(payload)


# ── 名字显示成 "nan"（2026-10-15）：接口把 np.nan 变成了字符串 "nan"，还被存进自选缓存 ──

def test_clean_name_kills_pseudo_names():
    """`str(np.nan or "")` 得到的是字符串 "nan"（np.nan 是真值）—— 这是本次事故的根。

    名字清洗必须把 NaN / None / 空串 / "nan" / "None" 一律清成空串，真名字只去空白。
    """
    import importlib.util
    import sys as _sys

    _sys.path.insert(0, str(ROOT))
    import numpy as np

    spec = importlib.util.spec_from_file_location("selection_sources_under_test",
                                                 ROOT / "backend/services/selection_sources.py")
    module = importlib.util.module_from_spec(spec)
    _sys.modules["selection_sources_under_test"] = module
    spec.loader.exec_module(module)

    assert module._clean_name(np.nan) == "", "np.nan 必须清成空串（不能变成 \"nan\"）"
    assert module._clean_name(None) == "" and module._clean_name("") == ""
    for junk in ("nan", "NaN", "None", "null", " undefined "):
        assert module._clean_name(junk) == "", f"{junk!r} 是伪名字"
    assert module._clean_name("  可孚医疗 ") == "可孚医疗"
    text = (ROOT / "backend/services/selection_sources.py").read_text(encoding="utf-8")
    assert 'str(row.get("name") or "")' not in text, "旧写法会把 NaN 变成字符串 nan"
    assert 'str(row.get("stock_name") or "")' not in text


def test_box_arm_selection_rows_have_real_names():
    """接口返回的每一行都要有**真名字**（这条同时对上游名单生成器与接口兜底生效）。"""
    import sys as _sys

    _sys.path.insert(0, str(ROOT))
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as client:
        payload = client.get("/api/selection?source=cn_box_arm").json()
    rows = payload.get("rows") or []
    assert rows, "box_arm 名单不该为空（每日流程会生成）"
    bad = [r["stock_code"] for r in rows
           if not r.get("stock_name") or str(r["stock_code"] and r["stock_name"]).strip().lower() in
           ("nan", "none", "null")]
    assert not bad, f"这些票的名字还是空的/伪名字：{bad}"


def test_watchlist_store_sanitizes_names():
    """自选缓存（localStorage）读取时要清洗历史脏数据（已经存了 "nan" 的要自愈），写入时也不许再存。"""
    text = (ROOT / "frontend/src/composables/useWatchlist.ts").read_text(encoding="utf-8")
    assert "const JUNK_NAMES = new Set" in text and "export function cleanName" in text
    assert "function readNames()" in text, "读取时必须过一遍清洗（旧数据自愈）"
    assert "names = ref<Record<string, string>>(readNames())" in text
    assert text.count("readNames()") >= 3, "两处监听（同 tab 事件 / 跨 tab storage）也要清洗"
    assert "const cleaned = cleanName(name)" in text, "写入路径要过清洗"
