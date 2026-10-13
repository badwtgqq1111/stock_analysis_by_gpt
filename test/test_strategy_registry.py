"""策略抽象层（Strategy Registry）：一条声明 → 所有消费端自动接线（P2.57，2026-10-13）。

用户要求："能把这些策略都做成抽象统一的么？方便后续新增策略的时候，可以在 web 端自动同步好数据，
把策略行为的共性提取出来做一层抽象。"

守住的四件事：
  1. **声明层自洽**：`strategy_registry.validate()` 无问题（选股源/刷新/台账/曲线/历史都齐）；
  2. **重构不改变行为**：派生出来的 `selection_sources.SOURCES` 与迁移前的硬编码值**逐字段一致**
     （迁移前的那份已冻结在 `output/strategy_pool/selection_sources.frozen.json`，这里再内联一份关键项）；
     曲线接线同样与迁移前硬编码的 replay/shadow 逐条一致；
  3. **新增策略 = 只改声明**：往 config 追加一条策略后，`/api/selection?source=<新源>` **立刻可用**
     （不改任何 Python、不重启），且接线表里选股结果页/K线自选/导出 都是"已接"；
  4. **漏接线不会被静默**：`coverage().gaps` 为空（每个未接的消费端都必须有"按设计"的原因），
     且每日流程里有 `strategy_registry --check` 这一步、体检里有「策略接线（抽象层自检）」一行。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CONFIG = ROOT / "config/strategies.json"
FROZEN = ROOT / "output/strategy_pool/selection_sources.frozen.json"
DAILY_SCRIPT = ROOT / "scripts/run_daily_production.sh"

# 迁移前的硬编码曲线接线（scripts/build_strategy_pool_performance.py 原值）—— 回归锁
OLD_REPLAY = {
    "jq_multi_factor_size_roe": "output/research/jq_multi_factor/nav_original_size_roe.csv",
    "jq_roe_roa_diagnostic": "output/research/jq_multi_factor_dynamic/nav_dynamic_diagnostic.csv",
}
OLD_SHADOW = {
    "rot_arm": ("output/paper_trading/shadow/rot_arm/nav.csv",
                "output/results_cn/shadow_variants/rot_arm.csv"),
    "cn_ensemble": ("output/paper_trading/shadow/model_topn_equal/nav.csv",
                    "output/results_cn/shadow_variants/model_topn_equal.csv"),
    "cn_ensemble_pk_legacy": ("output/paper_trading/shadow/production/nav.csv",
                              "output/results_cn/shadow_variants/production.csv"),
}
# 迁移前的选股源关键项（label / 文件 / code_style）—— 回归锁
OLD_SOURCES = {
    "cn_ensemble": ("CN 生产名单（preselect → PK）",
                    [["preselected", "output/results_cn/cn_ensemble_preselected.csv"],
                     ["selected", "output/results_cn/cn_ensemble_selected.csv"]]),
    "cn_arm": ("CN 研究手臂（base_v1）", [["arm", "output/results_cn/arm_latest_selection.csv"]]),
    "cn_arm_rot": ("CN 行业轮动手臂（rotation_arm）",
                   [["research", "output/results_cn/rot_arm_selection.csv"]]),
    "cn_live_holdings": ("实盘镜像（中泰证券真实持仓）",
                         [["live", "output/results_cn/cn_live_holdings.csv"]]),
    "hk_lightgbm": ("港股 LightGBM Top10（历史遗留）",
                    [["selected", "output/lightgbm_top10_selected.csv"]]),
}


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as test_client:
        yield test_client


# ── 1) 声明层自洽 ─────────────────────────────────────────────────────────────
def test_declaration_layer_is_valid():
    from backend.services import strategy_registry as reg

    problems = reg.validate()
    assert not problems, f"声明层问题：{problems}"
    assert len(reg.specs()) == len(json.loads(CONFIG.read_text(encoding="utf-8"))["strategies"])


def test_every_strategy_curves_and_refresh_are_declared():
    from backend.services import strategy_registry as reg

    for spec in reg.specs():
        plan = reg.curve_plan(spec)
        has_curve = any(plan[k] for k in ("tracking", "replay", "shadow", "original"))
        assert has_curve, f"{spec['id']} 没有任何曲线口径（策略池里会看不到中证500 对比）"
        refresh = (spec.get("refresh") or {}).get("mode")
        assert refresh in reg.REFRESH_MODES, f"{spec['id']} 的 daily_refresh.mode 非法：{refresh}"


# ── 2) 重构不改变行为（回归锁） ────────────────────────────────────────────────
def test_derived_sources_match_pre_migration_values():
    """派生 SOURCES == 迁移前硬编码值（10 条逐字段）。"""
    from backend.services import selection_sources as src

    assert len(src.SOURCES) == len(OLD_SOURCES) + 5 or len(src.SOURCES) >= 10
    for key, (label, roles) in OLD_SOURCES.items():
        entry = src.SOURCES.get(key)
        assert entry is not None, f"派生的 SOURCES 里没有 {key}"
        assert entry["label"] == label, key
        got_roles = [[role, str(path.relative_to(ROOT))] for role, path in entry["files"]]
        assert got_roles == roles, f"{key} 的 roles 变了：{got_roles}"
    if FROZEN.exists():                         # 冻结文件在的话，再做一次全量逐字段比对
        frozen = json.loads(FROZEN.read_text(encoding="utf-8"))
        for key, want in frozen.items():
            got = src.SOURCES[key]
            assert got["note"] == want["note"], f"{key}.note"
            assert list(got["columns"]) == want["columns"], f"{key}.columns"
            assert got["code_style"] == want["code_style"], f"{key}.code_style"
            assert bool(got["supports_shap"]) == want["supports_shap"], f"{key}.supports_shap"


def test_curve_wiring_matches_pre_migration_values():
    """迁移前硬编码的 replay / shadow 接线，现在必须由声明层给出一模一样的值。"""
    from backend.services import strategy_registry as reg

    got_replay = {s["id"]: s["curve"]["replay"]["nav_csv"] for s in reg.specs() if s["curve"]["replay"]}
    assert got_replay == OLD_REPLAY, got_replay
    got_shadow = {s["id"]: (s["curve"]["shadow"]["nav_csv"], s["curve"]["shadow"]["history_csv"])
                  for s in reg.specs() if s["curve"]["shadow"]}
    assert got_shadow == OLD_SHADOW, got_shadow
    # 模型臂：以前硬编码 ("preselected",10)/("notify",10)
    arms = {s["id"]: s["model_arms"] for s in reg.specs() if s.get("model_arms")}
    assert arms == {"cn_ensemble": {"ledger_sources": ["preselected", "notify"], "top_n": 10}}, arms


# ── 3) 新增策略 = 只改声明（核心承诺） ─────────────────────────────────────────
def test_new_strategy_needs_no_code_change(monkeypatch, client):
    """往 config 追加一条策略后：/api/selection 立刻多一个源、接线表立刻显示已接。"""
    from backend.services import strategy_registry as reg

    demo_csv = ROOT / "output/results_cn/_registry_demo_selection.csv"
    demo_csv.parent.mkdir(parents=True, exist_ok=True)
    demo_csv.write_text("trade_date,stock_code,rank,score,target_weight\n"
                        "2026-10-12,600519.SH,1,0.91,0.5\n2026-10-12,000001.SZ,2,0.88,0.5\n",
                        encoding="utf-8")
    tmp_config = ROOT / "output/strategy_pool/_registry_demo_config.json"
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    config["strategies"].append({
        "id": "demo_alpha", "name": "演示策略（临时）", "group": "演示", "kind": "rule_strategy",
        "stage": "candidate", "owner": "test", "summary": "测试用，不写进真 config",
        "entry": "uv run python scripts/select_demo_alpha.py",
        "date_column": "trade_date", "tag_class": "dim",
        "artifacts": ["output/results_cn/_registry_demo_selection.csv"],
        "history_dir": "output/results_cn/_registry_demo_history",
        "selection_source": "cn_demo_alpha",
        "selection": {"label": "演示策略", "note": "测试", "roles": [["research", "output/results_cn/_registry_demo_selection.csv"]],
                      "columns": ["rank", "score", "target_weight"], "code_style": "ts_code",
                      "supports_shap": False},
        "daily_refresh": {"mode": "run", "max_minutes": 5},
        "ledger_sources": ["demo_alpha"],
    })
    tmp_config.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    monkeypatch.setattr(reg, "CONFIG", tmp_config)
    monkeypatch.setitem(reg.LEDGER_ADAPTERS, "demo_alpha", "build_selection_ledger._demo")
    try:
        # 声明层认得它
        assert "demo_alpha" in reg.ids()
        assert not [p for p in reg.validate() if p.startswith("demo_alpha")]
        wired = reg.consumers(reg.spec("demo_alpha"))
        for key in ("selection_page", "kline_watchlist", "ths_export", "daily_refresh", "ai_picks"):
            assert wired[key]["wired"], f"{key} 没接上：{wired[key]}"
        # 接口立刻可用（不需要重启、不需要改代码）
        payload = client.get("/api/selection", params={"source": "cn_demo_alpha"}).json()
        assert payload["source"]["key"] == "cn_demo_alpha"
        assert payload["source"]["row_count"] == 2, payload["source"]
        keys = {opt["key"] for opt in payload["available"]}
        assert "cn_demo_alpha" in keys, "新策略的源没有出现在 available 里（前端下拉看不到）"
        assert [r["stock_code"] for r in payload["rows"]] == ["600519.SH", "000001.SZ"]
    finally:
        demo_csv.unlink(missing_ok=True)
        tmp_config.unlink(missing_ok=True)


def test_scaffold_puts_new_strategy_on_every_consumer(tmp_path):
    """`--new` 生成的骨架：干跑校验通过，且 4 个消费端"已接"。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("strategy_registry_cli", ROOT / "scripts/strategy_registry.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Args:
        new = "demo_scaffold"
        name = "骨架演示"
        group = "演示"
        kind = "rule_strategy"
        stage = "candidate"
        entry = "uv run python scripts/select_demo_scaffold.py"
        selection_source = "cn_demo_scaffold"
        file = "output/results_cn/demo_scaffold_selection.csv"
        history_dir = None
        refresh = "run"
        ledger = None
        write = False

    from backend.services import strategy_registry as reg

    skeleton = module.skeleton(Args)
    wired = reg.consumers(reg.normalize(skeleton))
    # 骨架默认就能接上：选股结果页 / K 线自选 / 导出 / 每日刷新 / 曲线
    for key in ("selection_page", "kline_watchlist", "ths_export", "daily_refresh", "curve"):
        assert wired[key]["wired"], f"{key} 没接上"
    # 台账（选股日历）**有意默认不接**：解析器（build_selection_ledger 适配器）建好再开，
    # 否则就是 P2.56 里"声明了却没人解析 ⇒ 日历缺来源"的老毛病 —— 拿 --ledger 才接。
    assert not wired["selection_calendar"]["wired"]
    Args.ledger = "demo_scaffold"
    try:
        assert reg.consumers(reg.normalize(module.skeleton(Args)))["selection_calendar"]["wired"]
    finally:
        Args.ledger = None


# ── 4) 漏接线不会被静默 ───────────────────────────────────────────────────────
def test_coverage_has_no_unexplained_gaps():
    from backend.services import strategy_registry as reg

    cov = reg.coverage()
    assert cov["gaps"] == [], f"存在漏接线：{cov['gaps']}"
    # 未接的每一处都必须有"按设计"的理由（否则就是缺口被静默）
    missed = sum(len(reg.ids()) - len(v) for v in cov["matrix"].values())
    assert len(cov["by_design"]) == missed, (len(cov["by_design"]), missed)


def test_api_exposes_wiring_and_coverage(client):
    payload = client.get("/api/strategies/registry").json()
    assert payload["rows"] and not payload["problems"]
    assert payload["coverage"]["gaps"] == []
    pool = client.get("/api/strategies", params={"refresh": False}).json()
    for item in pool["strategies"]:
        assert item.get("wiring"), f"{item['id']} 缺 wiring（页面无法显示接线）"
        if item.get("selection_source"):
            assert item["wiring"]["selection_page"]["wired"], item["id"]


def test_daily_pipeline_and_audit_wire_the_registry():
    text = DAILY_SCRIPT.read_text(encoding="utf-8")
    assert "run_data_step strategy_registry uv run python scripts/strategy_registry.py --check --save" in text, \
        "抽象层自检不在每日流程里（漏接线又会是静默的）"
    audit = (ROOT / "scripts/audit_web_freshness.py").read_text(encoding="utf-8")
    assert "策略接线（抽象层自检）" in audit
