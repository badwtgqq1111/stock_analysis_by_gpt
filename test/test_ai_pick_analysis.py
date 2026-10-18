"""逐股 AI 多空分析：覆盖**全部策略**（生产 + 备选）、进每日自动化、K 线页可见。

用户要求（2026-10-10）："不管备选策略还是生产策略我都想进每日自动化，并且每天日历可查。
被选中的每只个股再用 AI 做多空综合分析，评价买入，持有，还是卖出，要求在前端 K 线页面显示。"

守住四件事：
  1. `analyze_strategy_picks_ai.collect` 把 strategies.json 里的**每个**策略（含 candidate 阶段）
     与当日 feishu 最终名单合到一起，并按代码去重、保留全部命中策略；
  2. 没有 API key / 调用失败时，逐股结论是"待分析"而不是丢行（页面显示"待分析"而不是空白）；
  3. 每日自动化里 AI 步骤与候选策略步骤都排在**台账之前**（否则日历上永远是"待分析"/
     候选名单要等第二天），且 K 线页真的挂了 AI 面板、后端真的有逐股接口；
  4. `--skip-existing` 只补"待分析"的股票（同一天 19:00/20:30/22:00 失败重试不重复烧 API）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def ai_module():
    return _load("analyze_ai_under_test", "scripts/analyze_strategy_picks_ai.py")


def _workspace(tmp_path: Path, date: str = "2026-10-09") -> tuple[Path, Path]:
    """造一份最小工作区：config/strategies.json + 两个策略的当日名单 + 当日通知名单。"""
    registry = tmp_path / "config/strategies.json"
    results = tmp_path / "output/results_cn"
    registry.parent.mkdir(parents=True)
    (results / "prod_history").mkdir(parents=True)
    (results / "cand_history").mkdir(parents=True)
    registry.write_text(json.dumps({"version": 1, "strategies": [
        {"id": "cn_ensemble", "name": "模型策略选股", "stage": "production",
         "history_dir": "output/results_cn/prod_history", "selection_source": "cn_ensemble"},
        {"id": "jq_multi_factor", "name": "聚宽多因子（低市值+高ROE）", "stage": "candidate",
         "history_dir": "output/results_cn/cand_history", "selection_source": "cn_jq_multi_factor"},
    ]}, ensure_ascii=False), encoding="utf-8")
    (results / "prod_history" / f"{date}.csv").write_text(
        "trade_date,stock_code,name,rank,score,last_close\n"
        f"{date},600000.SH,浦发银行,3,12.5,10.1\n"
        f"{date},000001.SZ,平安银行,8,9.9,11.2\n", encoding="utf-8")
    (results / "cand_history" / f"{date}.csv").write_text(
        "trade_date,stock_code,rank,score,roe,market_cap\n"
        f"{date},600000.SH,1,0.88,15.2,2100\n"
        f"{date},300750.SZ,2,0.71,22.0,9800\n", encoding="utf-8")
    (results / f"feishu_{date}.json").write_text(json.dumps({
        "trade_date": date, "rows": [
            {"stock_code": "002594.SZ", "name": "比亚迪", "model_rank": 1, "target_weight": 0.3,
             "reason": "PK 胜出"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    return registry, results


def test_collect_covers_production_and_candidate_strategies(ai_module, tmp_path):
    registry, results = _workspace(tmp_path)
    picks = ai_module.collect("2026-10-09", registry=registry, results=results)
    by_code = {p["stock_code"]: p for p in picks}

    assert set(by_code) == {"600000.SH", "000001.SZ", "300750.SZ", "002594.SZ"}
    # 同一只票同时命中生产与备选策略 → 一行、两个策略，而不是两行
    assert sorted(by_code["600000.SH"]["strategies"]) == ["cn_ensemble", "jq_multi_factor"]
    assert by_code["000001.SZ"]["strategies"] == ["cn_ensemble"]
    assert by_code["300750.SZ"]["strategies"] == ["jq_multi_factor"]
    # 通知名单（PK 后最终名单）也必须覆盖到
    assert by_code["002594.SZ"]["name"] == "比亚迪"
    assert by_code["002594.SZ"]["strategies"] == ["cn_ensemble"]
    assert by_code["600000.SH"]["name"] == "浦发银行"
    # 中文策略名要一起带上（喂给模型，避免正文里冒出英文 id）
    assert by_code["600000.SH"]["strategies_zh"] == ["模型策略选股", "聚宽多因子（低市值+高ROE）"]
    assert by_code["002594.SZ"]["strategies_zh"] == ["模型策略选股"]


def test_analyze_without_key_keeps_rows_as_pending(ai_module, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)   # 本机 env 里有真 key，测试必须自洽
    picks = [{"stock_code": "600000.SH", "name": "浦发银行", "strategies": ["cn_ensemble"], "signals": []}]
    rows = ai_module.analyze("2026-10-09", picks, api_key="")
    assert len(rows) == 1, "失败的股票必须保留一行（页面上是「待分析」而不是空白）"
    row = rows[0]
    assert row["action"] == "待分析" and row["status"] == "pending"
    assert row["error"] == "missing_api_key"
    assert row["strategies"] == ["cn_ensemble"]


def test_analyze_normalizes_model_output(ai_module, monkeypatch):
    """模型返回的字段要落成固定结构：action / confidence 夹到 0~1 / risk_flags 列表。"""
    monkeypatch.setattr(ai_module, "_ask", lambda batch, date, key: {
        "600000.SH": {"stock_code": "600000.SH", "action": "买入", "confidence": 1.4,
                      "summary": "低估值+资金流入", "bull_case": "股息率与拨备", "bear_case": "息差压力",
                      "risk_flags": ["息差收窄"], "horizon": "20 个交易日"},
    })
    rows = ai_module.analyze("2026-10-09",
                             [{"stock_code": "600000.SH", "name": "浦发银行",
                               "strategies": ["cn_ensemble"], "signals": []}],
                             api_key="dummy")
    row = rows[0]
    assert row["status"] == "completed" and row["action"] == "买入"
    assert row["confidence"] == 1.0, "confidence 必须夹到 [0,1]"
    assert row["risk_flags"] == ["息差收窄"]


def test_main_skip_existing_only_retries_pending(ai_module, tmp_path, monkeypatch, capsys):
    out = tmp_path / "ai_pick_analysis"
    monkeypatch.setattr(ai_module, "OUT", out)
    monkeypatch.setattr(ai_module, "collect", lambda date, **kw: [
        {"stock_code": "600000.SH", "name": "浦发银行", "strategies": ["cn_ensemble"], "signals": []},
        {"stock_code": "000001.SZ", "name": "平安银行", "strategies": ["cn_ensemble"], "signals": []},
    ])
    monkeypatch.setattr(sys, "argv", ["analyze_strategy_picks_ai.py",
                                      "--date", "2026-10-09", "--fallback-only", "--skip-existing"])

    ai_module.main()
    payload = json.loads((out / "2026-10-09.json").read_text(encoding="utf-8"))
    assert {r["stock_code"] for r in payload["rows"]} == {"600000.SH", "000001.SZ"}
    assert all(r["status"] == "pending" for r in payload["rows"])

    # 把 600000.SH 标成已完成，再跑一次：只应重新处理 000001.SZ，已完成的结论原样保留
    rows = payload["rows"]
    for row in rows:
        if row["stock_code"] == "600000.SH":
            row.update({"status": "completed", "action": "买入", "confidence": 0.7,
                        "summary": "keep me"})
    (out / "2026-10-09.json").write_text(json.dumps({"date": "2026-10-09", "rows": rows},
                                                    ensure_ascii=False), encoding="utf-8")
    capsys.readouterr()
    ai_module.main()
    payload = json.loads((out / "2026-10-09.json").read_text(encoding="utf-8"))
    kept = {r["stock_code"]: r for r in payload["rows"]}["600000.SH"]
    assert kept["status"] == "completed" and kept["summary"] == "keep me"
    assert (out / "2026-10-09.csv").exists()


# ─── 接线：自动化顺序 + 前端 K 线页 + 后端接口 ───────────────────────────────
def test_daily_automation_runs_ai_and_candidates_before_ledger():
    text = (ROOT / "scripts/run_daily_production.sh").read_text(encoding="utf-8")
    # 只看**代码行**：脚本头部注释里也会提到这些脚本名（说明为什么排在台账之前），
    # 直接对全文 index 会命中注释，顺序断言就没意义了。
    code = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
    ai = code.index("scripts/analyze_strategy_picks_ai.py")
    ledger = code.index("uv run python scripts/build_selection_ledger.py")
    jq = code.index("scripts/select_jq_multi_factor.py")
    assert ai < ledger, "AI 步骤必须在台账之前：台账要合并 ai_pick_analysis 的逐股结论"
    assert jq < ledger, "候选策略名单必须在台账之前：否则当天候选要等第二天才进日历"
    assert "DAILY_AI_PICKS" in text and "DAILY_JQ_MULTI_FACTOR" in text, "两个步骤都要有开关"
    assert "--skip-existing" in text
    assert "test_ai_pick_analysis.py" in text, "预检清单要带上这个测试"


def test_kline_page_shows_ai_panel():
    page = (ROOT / "frontend/src/pages/KlinePage.vue").read_text(encoding="utf-8")
    # 2026-10-18：AI 结论与研报证据合并成一张「个股多空」结论卡，K 线页只挂 wrapper。
    wrapper = (ROOT / "frontend/src/components/StockLongShortPanel.vue").read_text(encoding="utf-8")
    assert "StockLongShortPanel" in page and "<StockLongShortPanel" in page
    assert "getStockAi" in wrapper and "多头依据" in wrapper and "空头依据" in wrapper
    assert (ROOT / "frontend/src/components/AiAnalysisPanel.vue").exists()
    panel = (ROOT / "frontend/src/components/AiAnalysisPanel.vue").read_text(encoding="utf-8")
    for token in ("getStockAi", "买入", "持有", "卖出", "bull_case", "bear_case"):
        assert token in panel, f"AI 面板缺 {token}"


def test_daily_picks_page_opens_merged_long_short_drawer():
    """每日策略选股页要能直接看 AI 结论 + 研报证据（2026-10-18 用户要求）。"""
    page = (ROOT / "frontend/src/pages/StockSelection.vue").read_text(encoding="utf-8")
    assert "StockLongShortPanel" in page and "longShortCode" in page
    wrapper = (ROOT / "frontend/src/components/StockLongShortPanel.vue").read_text(encoding="utf-8")
    assert "AI 决策" in wrapper and "研报证据" in wrapper
    assert "getStockAi" in wrapper and "getStockResearch" in wrapper


def test_stock_ai_endpoint_serves_analysis(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend.api import ai_picks
    from backend.main import app

    monkeypatch.setattr(ai_picks, "ANALYSIS_DIR", tmp_path)
    monkeypatch.setattr(ai_picks, "_cache", {"signature": None, "rows": None})
    (tmp_path / "2026-10-09.json").write_text(json.dumps({"date": "2026-10-09", "rows": [
        {"decision_date": "2026-10-09", "stock_code": "600000.SH", "name": "浦发银行",
         "strategies": ["cn_ensemble", "jq_multi_factor"], "action": "买入", "confidence": 0.62,
         "summary": "低估值修复", "bull_case": "股息率高", "bear_case": "息差收窄",
         "risk_flags": ["银行板块拥挤"], "horizon": "20 个交易日", "status": "completed",
         "generated_at": "2026-10-09T18:00:00+08:00"},
    ]}, ensure_ascii=False), encoding="utf-8")
    # 文件名不是日期的杂项文件必须被忽略（否则页面上会出现"日期=<随便什么>"）
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")

    with TestClient(app) as client:
        body = client.get("/api/stocks/600000/ai").json()
        missing = client.get("/api/stocks/999999/ai").json()
        # K 线页的代码写法不统一：带后缀 / 不带后缀 / 带交易所前缀都要命中同一只票
        bare = client.get("/api/stocks/600000/ai").json()
        prefixed = client.get("/api/stocks/sh600000/ai").json()
    assert body["available"] is True and body["n"] == 1
    latest = body["latest"]
    assert latest["action"] == "买入" and latest["stock_code"] == "600000.SH"
    assert latest["strategy_labels"] == ["模型策略选股", "聚宽多因子（低市值+高ROE）"]
    assert latest["risk_flags"] == ["银行板块拥挤"]
    assert bare["n"] == 1 and prefixed["n"] == 1 and prefixed["code"] == "600000.SH"
    assert missing["n"] == 0


def test_calendar_ui_knows_candidate_sources():
    """日历必须认得备选策略来源名：三张展示表 + 图例都要有，否则下拉里露英文 key、
    格子里的字母变成乱序首字母，用户会以为"选股结果里有、日历里没有"。"""
    text = (ROOT / "frontend/src/pages/SelectionCalendar.vue").read_text(encoding="utf-8")
    for src in ("jq_multi_factor", "jq_roe_roa"):
        assert text.count(src) >= 3, f"{src} 要在 SOURCE_LABEL / SOURCE_SHORT / SOURCE_LETTER 里"
    assert "多因子策略" in text
    assert "J=聚宽多因子（备选）" in text and "D=动态诊断候选（备选）" in text


def test_freshness_audit_covers_ai_page():
    text = (ROOT / "scripts/audit_web_freshness.py").read_text(encoding="utf-8")
    assert "_file_stem_max" in text and "ai_pick_analysis" in text
