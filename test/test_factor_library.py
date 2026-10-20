"""因子库注册表 `scripts/sync_factor_library.py` + `/api/agent/factors`（P2_42 §4.2）。

守住四件事：
  1. **一张表两种 kind**：族 B 的因子（含准入判决与簇冗余）+ 族 A 的条件（与策略池同一份登记）；
  2. **判决与产物一致**：`status=admitted` 的必须正好是 `factor_ic_admitted.json` 里那份清单，
     不能自己再判一次（否则"谁过了门槛"会出现两个口径）；
  3. **可重复**：同样的输入跑两次，id 集合完全一致（只有 `generated_at` / `last_seen` 变）；
  4. 页面用的 `/api/agent/factors` 能按 kind/status/关键词筛、能分页，`/overview` 带总数。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADMITTED = ROOT / "output/research/factor_ic_protocol/factor_ic_admitted.json"
CONFIG = ROOT / "config/strategies.json"
ALLOWED_STATUS = {"admitted", "rejected", "excluded", "proposed", "candidate", "research", "in_use", "retired"}


def _load():
    spec = importlib.util.spec_from_file_location("sync_factor_library_under_test",
                                                  ROOT / "scripts/sync_factor_library.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def lib():
    return _load()


@pytest.fixture(scope="module")
def payload(lib):
    return lib.build_library()


# ─── 1. 形状 ────────────────────────────────────────────────────────────────
def test_library_shape(payload):
    totals = payload["totals"]
    assert totals["n"] == len(payload["factors"]) > 1000
    assert sum(totals["by_kind"].values()) == totals["n"]
    assert sum(totals["by_status"].values()) == totals["n"]
    assert set(totals["by_status"]) <= ALLOWED_STATUS
    assert {"factor", "condition"} <= set(totals["by_kind"]), "两种 kind 都要有（族 A 条件 + 族 B 因子）"
    assert totals["n_admitted"] == totals["by_status"].get("admitted", 0)
    for item in payload["factors"]:
        for field in ("id", "kind", "source", "status", "metrics", "evidence"):
            assert field in item, f"{item.get('id')} 缺字段 {field}"


def test_admitted_matches_the_protocol_list(payload):
    """准入名单是唯一口径：库里的 admitted 集合必须与 factor_ic_admitted.json 逐字一致。"""
    protocol = json.loads(ADMITTED.read_text(encoding="utf-8"))
    expected = set(protocol["admitted_A"]) | set(protocol["admitted_B"])
    got = {item["id"] for item in payload["factors"] if item["status"] == "admitted"}
    assert got == expected, f"多出 {sorted(got - expected)[:5]} / 少了 {sorted(expected - got)[:5]}"
    grades = {item["id"]: item["grade"] for item in payload["factors"] if item["status"] == "admitted"}
    assert grades == {**{n: "A" for n in protocol["admitted_A"]}, **{n: "B" for n in protocol["admitted_B"]}}


def test_verdicts_map_to_status(payload):
    by_id = {item["id"]: item for item in payload["factors"] if item["kind"] == "factor"}
    assert by_id["vr_zscore_60_is_missing"]["status"] == "excluded", "质量列不是因子，不能混进准入"
    assert by_id["dividend_yield_ind_pct_clean"]["status"] == "rejected"
    assert by_id["dividend_yield_ind_pct_clean"]["note"] == "强度不足"
    assert any(item["status"] == "rejected" and item["note"] == "冗余(同簇有更强代表)" for item in payload["factors"]), \
        "簇冗余要能看出来（否则「为什么好因子没进」答不出来）"


def test_conditions_cover_every_rule_like_strategy(payload, ):
    registered = {item["id"] for item in json.loads(CONFIG.read_text(encoding="utf-8"))["strategies"]
                  if item.get("kind") in ("rule_strategy", "research_arm", "factor_candidate")}
    got = {item["id"] for item in payload["factors"] if item["kind"] == "condition"}
    assert got == registered, f"族 A 条件缺 {sorted(registered - got)}"
    stage = {item["id"]: item["status"] for item in payload["factors"] if item["kind"] == "condition"}
    assert stage["arm_select"] == "in_use" and stage["first_ban_lowxi"] == "candidate"


def test_llm_session_factors_are_kept(payload):
    sessions = sorted((ROOT / "output/factor_mining_sessions").glob("*.json"))
    expected = set()
    for path in sessions:
        for factor in json.loads(path.read_text(encoding="utf-8")).get("factors") or []:
            expected.add(factor["name"])
    got = {item["id"] for item in payload["factors"] if item["source"].startswith("llm_session:")}
    assert expected <= got, f"LLM 会话里提议的因子丢了一部分：{sorted(expected - got)}"
    sample = next(item for item in payload["factors"] if item["id"] == "rev_vol")
    assert sample["expression"] and sample["note"], "提议因子要带表达式与经济解释（否则没法复核）"


def test_build_is_repeatable(lib, payload):
    again = lib.build_library()
    assert {item["id"] for item in again["factors"]} == {item["id"] for item in payload["factors"]}
    assert again["totals"] == payload["totals"]


# ─── 2. 落盘 ────────────────────────────────────────────────────────────────
def test_writes_json_and_markdown(lib, tmp_path):
    code = lib.main(["--out", str(tmp_path)])
    assert code == 0
    written = json.loads((tmp_path / "factors.json").read_text(encoding="utf-8"))
    assert written["totals"]["n"] > 1000
    md = (tmp_path / "factors.md").read_text(encoding="utf-8")
    for keyword in ("# 因子库", "本次新准入", "准入口径", "族 A 的条件"):
        assert keyword in md


# ─── 3. 接口 ────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as test_client:
        yield test_client


def test_api_factors_filters_and_pages(client):
    admitted = client.get("/api/agent/factors", params={"status": "admitted", "limit": 5}).json()
    assert admitted["available"] is True
    assert admitted["filtered"] == admitted["totals"]["by_status"]["admitted"] >= 100
    assert len(admitted["items"]) == 5
    assert all(item["status"] == "admitted" and item["grade"] in ("A", "B") for item in admitted["items"])
    page2 = client.get("/api/agent/factors", params={"status": "admitted", "limit": 5, "offset": 5}).json()
    assert [item["id"] for item in page2["items"]] != [item["id"] for item in admitted["items"]]

    conditions = client.get("/api/agent/factors", params={"kind": "condition"}).json()
    assert conditions["filtered"] == conditions["totals"]["by_kind"]["condition"] >= 7
    assert {"first_ban_lowxi", "arm_select", "rot_arm"} <= {item["id"] for item in conditions["items"]}

    search = client.get("/api/agent/factors", params={"q": "moneyflow", "limit": 3}).json()
    assert search["filtered"] > 50
    assert all("moneyflow" in (item["id"] + (item["family"] or "")).lower() for item in search["items"])


def test_api_overview_and_health_expose_the_library(client):
    overview = client.get("/api/agent/overview").json()
    library = overview["factor_library"]
    assert library["available"] is True
    assert library["totals"]["n"] > 1000 and library["totals"]["by_kind"]["condition"] >= 7
    assert library["criteria"]["fdr_q"] == 0.05
    health = client.get("/api/agent/health").json()["factor_library"]
    assert health["exists"] and health["n"] > 1000


def test_daily_script_syncs_the_library():
    script = (ROOT / "scripts/run_daily_production.sh").read_text(encoding="utf-8")
    assert "scripts/sync_factor_library.py" in script, "每日自动化必须刷新因子库"
    assert "DAILY_FACTOR_LIBRARY" in script, "要留开关"
    assert "test/test_factor_library.py" in script, "新测试要进预检清单"


# ─── 4. 分类 + 功能描述 + 性能评价（第四十六轮；每条都必须有）────────────────────
def test_every_factor_has_category_description_and_performance(payload):
    """1,431 条**每一条**都要有：分类（含一句话核心）、功能描述、性能评价一句话。

    背景（用户 2026-10-11）："因子库 1431 条，这些因子 每个是不是都应该有 性能评价 和 功能描述 …
    另外 因子是不是也应该分类，比如 量价，价值，财务，宏观？"
    实现：家族映射（16 家族）+ 名字 token 兜底（9 条）+ 人工 overrides；描述来源如实标注。
    """
    items = payload["factors"]
    assert items, "因子库不能为空"
    for item in items:
        category = item.get("category") or {}
        assert category.get("label"), f"{item['id']} 没有分类"
        assert category.get("core"), f"{item['id']} 的分类没有一句话核心"
        description = item.get("description") or {}
        assert description.get("text"), f"{item['id']} 没有功能描述"
        assert description.get("source") in ("curated", "llm_reason", "name_decoder"), \
            f"{item['id']} 的描述来源没标清楚：{description.get('source')}"
        performance = item.get("performance") or {}
        assert performance.get("summary"), f"{item['id']} 没有性能评价"
    # 分类分布要和条目数对得上；且不允许出现"未识别"的描述
    assert sum(payload["totals"]["by_category"].values()) == len(items)
    missing = [item["id"] for item in items if item["description"]["source"] == "missing"]
    assert not missing, f"这些因子没解码出描述（要在 glossary 里补规则）：{missing[:5]}"
    # 协议层的"判决"不能被当成 LLM 的经济解释（踩过：会写出"LLM 提议：质量列(不参与准入)"）
    mask = next(item for item in items if item["id"].endswith("_is_missing"))
    assert mask["description"]["source"] == "name_decoder"
    assert "LLM 提议" not in mask["description"]["text"]


def test_categories_cover_the_expected_taxonomy(payload):
    """分类口径：量价 / 价值 / 财务质量 / 资金流 / 波动风险 / 流动性 / 结构 / 学术宏观 / 日历 / 其它。"""
    import json as _json
    glossary = _json.loads((ROOT / "config/factor_glossary.json").read_text(encoding="utf-8"))
    labels = {meta["label"] for meta in glossary["categories"].values()}
    assert {"量价", "价值", "财务质量/成长", "资金流/情绪", "波动/风险", "流动性/容量"} <= labels
    assert all(meta["core"] for meta in glossary["categories"].values()), "每类都要有一句话核心"
    counts = payload["totals"]["by_category"]
    assert counts.get("量价", 0) > 500 and counts.get("资金流/情绪", 0) > 100, counts
