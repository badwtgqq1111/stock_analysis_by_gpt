"""灵感来源 / 知识库注册表（P2.51）：来源只给方向、不给证据；模板不许悬空引用。

守四件事：
  1. 注册表每条都要**能用**：id/name/region/kind/what/use/caution/entry 齐备（坑必须写，不许只写好处）；
  2. 用户点名的来源必须在册：QuantPedia / Seeking Alpha / FRED / **聚宽 JoinQuant**；
  3. 模板 → 来源的引用必须闭合（`template.source` 一定能在 sources 里找到，否则页面上"用它的方向"会指向空气）；
  4. 前端面板真的用了这些数据（来源 chips / 详情 / 用它的方向 / 大输入框），且预检清单带上本测试。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/research_sources.json"
PANEL = ROOT / "frontend/src/components/FactorMiningPanel.vue"
REQUIRED_FIELDS = ("id", "name", "region", "kind", "what", "use", "caution", "entry")


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as test_client:
        yield test_client


def test_registry_entries_are_usable():
    payload = json.loads(CONFIG.read_text(encoding="utf-8"))
    sources = payload["sources"]
    assert len(sources) >= 12, "来源太少（用户已说过'模板有点少'）"
    ids = [s["id"] for s in sources]
    assert len(ids) == len(set(ids)), f"来源 id 重复：{ids}"
    for item in sources:
        for field in REQUIRED_FIELDS:
            assert item.get(field), f"{item.get('id')} 缺 {field}"
        assert isinstance(item["use"], list) and item["use"], f"{item['id']} 的 use 要是非空数组"
        assert len(item["caution"]) > 10, f"{item['id']} 没写清坑（caution 太短）"
        assert item.get("url") or item.get("entry"), f"{item['id']} 既没有 url 也没有 entry"


def test_user_named_sources_are_registered():
    payload = json.loads(CONFIG.read_text(encoding="utf-8"))
    ids = {s["id"] for s in payload["sources"]}
    for expected in ("quantpedia", "seekingalpha", "fred", "joinquant"):
        assert expected in ids, f"用户点名的来源缺 {expected}"
    fred = next(s for s in payload["sources"] if s["id"] == "fred")
    assert "PIT" in fred["caution"] or "vintage" in fred["caution"].lower(), "FRED 必须写明修订/前视偏差的坑"
    join = next(s for s in payload["sources"] if s["id"] == "joinquant")
    assert "样本外" in join["caution"], "聚宽必须写明样本外衰减"


def test_templates_reference_registered_sources():
    payload = json.loads(CONFIG.read_text(encoding="utf-8"))
    ids = {s["id"] for s in payload["sources"]}
    templates = payload["templates"]
    assert len(templates) >= 10, f"方向模板太少（现在 {len(templates)} 条）"
    labels = [t["label"] for t in templates]
    assert len(labels) == len(set(labels)), "模板 label 重复"
    for t in templates:
        assert len(t["direction"]) > 30, f"{t['label']} 的方向太短，等于没写"
        assert t.get("source") in ids, f"{t['label']} 指向未注册来源 {t.get('source')}"


def test_api_matches_registry_and_context_includes_templates(client):
    api = client.get("/api/agent/research-sources").json()
    payload = json.loads(CONFIG.read_text(encoding="utf-8"))
    assert api["counts"]["sources"] == len(payload["sources"])
    assert api["counts"]["templates"] == len(payload["templates"])
    assert set(api["by_region"]) == {s["region"] for s in payload["sources"]}
    assert api["note"], "注册表要带纪律说明（来源只给方向不给证据）"
    ctx = client.get("/api/agent/factors/context").json()
    assert len(ctx["templates"]) >= 10 and all(t.get("source") for t in ctx["templates"])
    assert len(ctx.get("research_sources") or []) >= 12


def test_panel_uses_registry_and_has_bigger_composer():
    panel = PANEL.read_text(encoding="utf-8")
    for marker in ("src-regions", "src-chips", "src-detail", "用它的方向", "getResearchSources",
                   "sourceShort", "templateOfSource"):
        assert marker in panel, f"面板缺 {marker}"
    assert 'rows="5"' in panel, "输入框要变大（rows=5）"
    assert "min-height: 120px" in panel, "输入框要有最小高度"
    daily = (ROOT / "scripts/run_daily_production.sh").read_text(encoding="utf-8")
    assert "test_research_sources.py" in daily, "新测试要进预检清单"
    assert (ROOT / "docs/todo/P2_51_research_sources.md").exists(), "要有 P2.51 知识库文档"
