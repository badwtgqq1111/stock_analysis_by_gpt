"""P2.72 个股研报调研（Bynewlt）管线 + API + 前端接线。

守住四件事：
  1. 管线脚本的**名单采集**与 K 线页自选股分组同一口径（strategies.json 里带
     selection_source + history_dir 的策略），代码归一化不允许漏掉 `sh600519` 这类写法；
  2. 调用结果**分类**正确（未收录/空/成功/被拒/待确认），状态机不把"没证据"当"有证据"；
  3. API 只读盘：有台账的票给到状态与证据摘要，没记录的票给 `not_in_scope`，不允许 500；
  4. 前端三等接好线：K 线页引用了面板、client 导出了取数函数、面板引用了 API 与引用明细。

本测试**不联网、不发任何计费调用**（classify 用本地假 payload）。
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/bynewlt_stock_research.py"
STORE = ROOT / "output/results_cn/bynewlt_research"


def _pipeline():
    """按路径加载管线脚本（scripts/ 不是包）。"""
    spec = importlib.util.spec_from_file_location("bynewlt_stock_research", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as test_client:
        yield test_client


# ─── 1. 管线：名单采集 + 归一化 + 请求模板 ───────────────────────────────────
def test_norm_code_handles_common_spellings():
    module = _pipeline()
    assert module.norm_code("600519") == "600519.SH"
    assert module.norm_code("000538") == "000538.SZ"
    assert module.norm_code("430047") == "430047.BJ"
    assert module.norm_code("sh600519") == "600519.SH"
    assert module.norm_code("300014.SZ") == "300014.SZ"
    assert module.norm_code(" 830799.bj ") == "830799.BJ"
    assert module.norm_code("") == ""


def test_collect_picks_matches_watchlist_scope():
    module = _pipeline()
    collected = module.collect_picks()
    picks, files = collected["picks"], collected["files"]
    assert picks, "策略名单并集不能为空（config/strategies.json + output/*_history）"
    assert files, "必须记录用了哪些名单文件，便于口径追溯"
    for code, entry in picks.items():
        assert re.match(r"^\d{6}\.(SH|SZ|BJ)$", code), code
        assert entry["strategies"], code
        assert isinstance(entry["priority"], int)
    # 与 ai_picks/选股台同一只票的口径：至少有一个已知策略在其中
    assert any("arm" in s or "jq" in s or "cn_ensemble" in s or "live_holdings" in s
               for entry in picks.values() for s in entry["strategies"])


def test_build_args_template_locks_company_and_window():
    module = _pipeline()
    args = module.build_args({"name": "中国石化"}, "600028.SH", "2025-01-01")
    assert args["company"] == "中国石化"
    assert args["companyCode"] == "600028.SH"
    assert args["companyMarket"] == "A股"
    assert args["mode"] == "analysis"
    assert args["since"] == "2025-01-01"
    assert args["limit"] == 20 and args["maxPerReport"] == 5  # 与技能检索参考的 analysis 默认配额一致
    assert "主要风险" in args["question"] and "盈利预测" in args["question"]


def test_classify_status_machine_without_network():
    module = _pipeline()
    record = {"name": "某票"}

    def payload(status, returned=0, state="completed"):
        return {"state": state, "request_id": "req-1",
                "result": {"structuredContent": {"status": status, "stats": {"returnedItems": returned}}}}

    assert module.classify(payload("company_not_found"), 0, record, "600028.SH", {})[0] == module.ST_NOT_FOUND
    assert module.classify(payload("ok", returned=0), 0, record, "600028.SH", {})[0] == module.ST_EMPTY
    assert module.classify(payload("ok", returned=5), 0, record, "600028.SH", {})[0] == module.ST_FETCHED
    rejected = {"state": "rejected", "request_id": "req-2", "result": {"error": "quota"}}
    assert module.classify(rejected, 1, record, "600028.SH", {})[0] == module.ST_REJECTED
    assert module.classify(None, 2, record, "600028.SH", {})[0] == module.ST_UNKNOWN


def test_state_refresh_is_pure_and_marks_out_of_list():
    module = _pipeline()
    state = {"stocks": {"999999.SZ": {"status": "fetched", "calls_used": 1, "requests": []}}}
    refreshed = module.refresh_state_from_picks(state)
    assert refreshed["stocks"]["999999.SZ"].get("out_of_list") is True  # 旧票不删、标出名单
    assert all(r["status"] for r in refreshed["stocks"].values())
    totals = module.totals(refreshed)
    assert totals["calls_used"] >= 1


# ─── 2. 证据档（若已拉取）：结构自检 ─────────────────────────────────────────
def test_evidence_archive_shape_when_present():
    sample = STORE / "evidence/600028.SH.json"
    if not sample.exists():
        pytest.skip("尚未拉取样本证据（先跑 scripts/bynewlt_stock_research.py fetch）")
    archive = json.loads(sample.read_text(encoding="utf-8"))
    assert archive["code"] == "600028.SH"
    assert archive["args"]["companyCode"] == "600028.SH"
    assert archive["status"]
    for ev in archive["evidence"]:
        assert ev.get("ref"), "每条证据必须带 ref（引用/核对都靠它）"
        assert ev.get("quote") or ev.get("claim")


# ─── 3. API：只读盘 + 状态如实 ───────────────────────────────────────────────
def test_api_serves_known_stock(client):
    resp = client.get("/api/stocks/600028.SH/research")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "600028.SH"
    assert body["in_scope"] is True
    assert body["state"] in {"queued", "fetching", "fetched", "empty", "not_found",
                             "error", "unknown", "rejected", "composed"}
    if body["dossier"] is not None:
        assert body["dossier"]["code"] == "600028.SH"
        assert body["state"] == "composed"
    else:
        assert body["hint"], "无合成稿时必须给下一步提示，不能空白"


def test_api_unknown_stock_returns_not_in_scope(client):
    resp = client.get("/api/stocks/000001.SZ/research")
    assert resp.status_code == 200
    body = resp.json()
    assert body["in_scope"] is False
    assert body["state"] in {"not_in_scope", "composed"}  # 已合成过的票允许单独存在
    assert body["dossier"] is None or body["state"] == "composed"


def test_api_normalizes_bare_and_prefixed_code(client):
    for spelling in ("600028", "sh600028"):
        resp = client.get(f"/api/stocks/{spelling}/research")
        assert resp.status_code == 200
        assert resp.json()["code"] == "600028.SH"


# ─── 4. 前端接线（源码级契约）────────────────────────────────────────────────
def test_frontend_wiring():
    kline = (ROOT / "frontend/src/pages/KlinePage.vue").read_text(encoding="utf-8")
    client_ts = (ROOT / "frontend/src/api/client.ts").read_text(encoding="utf-8")
    panel = (ROOT / "frontend/src/components/StockResearchPanel.vue").read_text(encoding="utf-8")
    wrapper = (ROOT / "frontend/src/components/StockLongShortPanel.vue").read_text(encoding="utf-8")
    assert "StockLongShortPanel" in kline and "<StockLongShortPanel" in kline
    assert "getStockResearch" in wrapper and "研报证据" in wrapper
    assert "getStockResearch" in client_ts and "/research`" in client_ts
    assert "getStockResearch" in panel and "引用明细" in panel


# ─── 5. 多空稿：提示词 / 引文核对 / 晋级 ─────────────────────────────────────
def _fake_archive() -> dict:
    return {
        "code": "600028.SH", "name": "中国石化", "status": "ok",
        "args": {"since": "2025-01-01"},
        "evidence": [
            {"ref": "a:1", "broker": "Citi", "reportDate": "2026-09-08", "page": 1,
             "reportTitle": "China Petroleum 3Q preview",
             "quote": "We maintain Sell on Sinopec with a target price of Rmb5.20, "
                      "implying 12% downside from the current price."},
            {"ref": "a:2", "broker": "CICC", "reportDate": "2026-08-30", "page": 3,
             "reportTitle": "2026 中报点评",
             "quote": "公司上半年归母净利润同比下降 33.7%，主要受炼油毛利收窄影响。"},
        ],
    }


def test_build_prompt_numbers_evidence_and_truncates():
    module = _pipeline()
    archive = _fake_archive()
    prompt = module.build_prompt(archive, max_chars=40)
    assert "[E1]" in prompt and "[E2]" in prompt
    assert "Citi" in prompt and "CICC" in prompt
    assert "…" in prompt                      # 超长原文被截断，但保留编号
    assert "600028.SH" in prompt


def test_verify_citations_drops_unbound_quotes_and_keeps_unique_ids():
    module = _pipeline()
    archive = _fake_archive()
    draft = {
        "citations": [
            {"id": "E1", "quote": "We maintain Sell on Sinopec"},
            {"id": "E1", "quote": "implying 12% downside"},
            {"id": "E2", "quote": "归母净利润同比下降 33.7%"},
            {"id": "E1", "quote": "这句原文里没有"},
            {"id": "E9", "quote": "越界"},
        ],
        "bull": [{"point": "低估值", "refs": ["E1", "E9"]}],
        "bear": [{"point": "炼油毛利收窄", "refs": ["E2"]}],
        "risks": [{"point": "无依据", "refs": ["E9"]}],
    }
    out = module.verify_citations(archive, draft)
    v = out["verification"]
    assert v["checked"] == 5 and v["verified"] == 3
    assert [c["id"] for c in out["citations"]] == ["E1", "E1b", "E2"]
    assert out["citations"][0]["broker"] == "Citi"
    assert out["citations"][2]["reportTitle"] == "2026 中报点评"
    assert out["bull"][0]["refs"] == ["E1"]     # 越界编号剔除，合法引用保留
    assert out["bear"][0]["refs"] == ["E2"]
    assert out["risks"] == []


def test_promote_writes_dossier_and_marks_composed(tmp_path, monkeypatch):
    module = _pipeline()
    monkeypatch.setattr(module, "STORE", tmp_path)
    monkeypatch.setattr(module, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(module, "DRAFT_DIR", tmp_path / "dossier_draft")
    monkeypatch.setattr(module, "DOSSIER_DIR", tmp_path / "dossier")
    (tmp_path / "dossier_draft").mkdir()
    (tmp_path / "dossier_draft" / "600028.SH.json").write_text(json.dumps({
        "code": "600028.SH", "name": "中国石化", "stance": "偏空",
        "bull": [], "bear": [{"point": "毛利收窄", "refs": ["E2"]}],
        "citations": [{"id": "E2", "quote": "归母净利润同比下降 33.7%"}],
        "verification": {"level": "auto_checked", "checked": 1, "verified": 1},
    }, ensure_ascii=False), encoding="utf-8")
    import types
    rc = module.promote(types.SimpleNamespace(code="600028", level="agent_checked"))
    assert rc == 0
    dossier = json.loads((tmp_path / "dossier" / "600028.SH.json").read_text(encoding="utf-8"))
    assert dossier["verification"]["level"] == "agent_checked"
    assert dossier["promoted_at"]
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["stocks"]["600028.SH"]["status"] == "composed"
