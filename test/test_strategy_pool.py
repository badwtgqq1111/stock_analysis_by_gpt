"""策略池：声明式登记 + 运行时状态 + 候选登记（2026-10-10）。

背景（用户要求）："应该有个策略池，比如现有 模型选股 / arm_select / 行业轮动，
后面还应该有其他策略进入策略池，agent 挖掘好的新策略应该进入策略池。"

守住四件事：
  1. `config/strategies.json` 的登记是**可用**的（必填字段齐、产物路径在仓库内、id 不重复）；
  2. `scripts/sync_strategy_pool.py` 能把三个生产策略的运行时状态扫出来且都到最新交易日；
  3. 候选登记幂等（同 id 覆盖，不堆同义条目），阶段可切换；
  4. `/api/strategies`、每日自动化、新鲜度体检三处**互相接得上**
     （新增策略必须同时进这三处，否则又会出现"配置里有一个、页面上没有、体检也查不到"）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CONFIG = ROOT / "config/strategies.json"


def _load_module():
    """按路径加载 scripts/sync_strategy_pool.py（scripts/ 不是包）。"""
    spec = importlib.util.spec_from_file_location(
        "sync_strategy_pool_under_test", ROOT / "scripts/sync_strategy_pool.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def pool_module(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "CANDIDATE_DIR", tmp_path / "candidates")
    return module


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as test_client:
        yield test_client


# ─── 1. 声明层 ──────────────────────────────────────────────────────────────
def test_config_registers_three_production_strategies():
    payload = json.loads(CONFIG.read_text(encoding="utf-8"))
    strategies = payload.get("strategies") or []
    ids = [item["id"] for item in strategies]
    assert len(ids) == len(set(ids)), "策略 id 不能重复"
    for expected in ("cn_ensemble", "arm_select", "arm_concept"):
        assert expected in ids, f"现有三个策略必须登记：缺 {expected}"
    for item in strategies:
        for field in ("id", "name", "group", "stage", "entry", "artifacts"):
            assert item.get(field), f"{item.get('id')} 缺字段 {field}"
        # live（2026-10-12 新增）= 实盘镜像：真实券商持仓，登记在池里是为了
        # 「选股页 / K 线自选 / 策略池」都能看到它，但它**不是策略**（`kind=live_account`），
        # 不下单、不参与生产权重（见 test/test_live_mirror.py）。
        assert item["stage"] in ("production", "research", "candidate", "retired", "live")
        for rel in item["artifacts"]:
            assert (ROOT / rel).exists(), f"产物路径不存在（登记错了？）：{rel}"
        if item.get("history_dir"):
            assert (ROOT / item["history_dir"]).exists(), f"历史快照目录不存在：{item['history_dir']}"


# ─── 2. 运行时层 ────────────────────────────────────────────────────────────
def test_build_pool_reports_fresh_production_strategies(pool_module):
    pool = pool_module.build_pool()
    assert pool["reference_trade_date"], "取不到基准交易日（行情服务与基准指数都不可用）"
    production = [s for s in pool["strategies"] if s["stage"] == "production"]
    assert len(production) == 3
    from test._daily_state import midrun_grace, previous_trade_date

    reference = pool["reference_trade_date"]
    allowed = reference
    if midrun_grace():        # P2.54：运行中当天名单还没生成 ⇒ 退到上一交易日
        allowed = previous_trade_date(reference) or reference
    for item in production:
        assert item["latest_date"], f"{item['id']} 没有扫到数据日期"
        assert item["n_picks"], f"{item['id']} 没有扫到名单条数"
        assert item["latest_date"] >= allowed, \
            f"{item['id']} 最新 {item['latest_date']} 落后于 {allowed}"
        if not midrun_grace():
            assert item["health"] == "ok"
    # 运行中允许"当天还没生成名单"的中间态（否则每晚跑预检都会假红）
    if not midrun_grace():
        assert pool["totals"]["stale_production"] == []
        assert pool["totals"]["fresh"] is True
    assert pool["totals"]["n_evidence"] > 0
    assert all(row["path"].startswith("output/research/") for row in pool["evidence"])


def test_is_fresh_allows_leading_reference(pool_module):
    """产物日期 `>=` 基准交易日即算新鲜（2026-10-11 修：`==` 会把"超前一天"误判成 stale）。

    事故现场：2026-10-11 早上 `cn_ensemble` 最新 2026-10-10、基准 `latest_trade_date()` 停在 2026-10-09
    —— 严格相等 ⇒ `--strict` 非 0 ⇒ 预检硬门红 ⇒ 当天每日自动化被挡（数据态问题被当成代码问题）。
    """
    assert pool_module.is_fresh("2026-10-10", "2026-10-09") is True    # 超前：新鲜
    assert pool_module.is_fresh("2026-10-09", "2026-10-09") is True    # 同日：新鲜
    assert pool_module.is_fresh("2026-10-08", "2026-10-09") is False   # 落后：不新鲜
    assert pool_module.is_fresh(None, "2026-10-09") is False           # 没扫到日期
    assert pool_module.is_fresh("2026-10-09", None) is False           # 取不到基准


def test_artifact_date_comes_from_column_not_mtime(pool_module, tmp_path):
    """数据日期必须按**产物里的日期列**取：改 mtime（重跑一次旧产物）不能让它显示成今天。"""
    import os
    import time
    stale = tmp_path / "stale.csv"
    stale.write_text("trade_date,stock_code\n2020-01-02,600000.SH\n", encoding="utf-8")
    os.utime(stale, (time.time(), time.time()))
    info = pool_module._artifact_info(str(stale))
    assert info["latest_date"] == "2020-01-02"
    assert info["rows"] == 1


# ─── 3. 候选层：登记幂等 + 阶段切换 ─────────────────────────────────────────
def test_register_candidate_is_idempotent_and_stage_switchable(pool_module):
    payload = {
        "id": "llm-factor-demo",
        "name": "LLM 因子：demo",
        "summary": "演示",
        "metrics": {"ic_mean": 0.03},
        "evidence": [str(ROOT / "output/research/ic_rubric_audit/REPORT.md")],
    }
    first = pool_module.register_candidate(payload)
    second = pool_module.register_candidate({**payload, "summary": "演示 v2"})
    assert first == second, "同 id 重复登记应指向同一个文件（不堆同义条目）"
    stored = json.loads(first.read_text(encoding="utf-8"))
    assert stored["stage"] == "candidate"
    assert stored["evidence"] == ["output/research/ic_rubric_audit/REPORT.md"], \
        "绝对路径必须落盘成仓库相对路径"
    path, updated = pool_module.set_stage("llm-factor-demo", "research")
    assert path == first and updated["stage"] == "research"
    with pytest.raises(SystemExit):
        pool_module.set_stage("llm-factor-demo", "不存在的阶段")


def test_pool_snapshot_lists_registered_candidate(pool_module):
    pool_module.register_candidate({"id": "cand-x", "name": "候选 X", "origin": "agent"})
    pool = pool_module.build_pool()
    assert "cand-x" in [c["id"] for c in pool["candidates"]]
    assert pool["totals"]["n_candidates"] >= 1


# ─── 4. 接线：页面 / API / 每日自动化 / 新鲜度体检 ──────────────────────────
def test_api_returns_pool_and_registers_candidate(client):
    body = client.get("/api/strategies").json()
    assert body["totals"]["n_production"] == 3
    assert {s["id"] for s in body["strategies"]} >= {"cn_ensemble", "arm_select", "arm_concept"}
    assert body["notes"], "页面上要能看到口径说明"

    payload = {"name": "测试候选（API）", "id": "api-cand-test", "summary": "由测试登记"}
    resp = client.post("/api/strategies/candidates", json=payload)
    assert resp.status_code == 200 and resp.json()["ok"]
    try:
        pool = client.get("/api/strategies").json()
        assert "api-cand-test" in [c["id"] for c in pool["candidates"]]
        move = client.post("/api/strategies/api-cand-test/stage", json={"stage": "research"})
        assert move.status_code == 200 and move.json()["stage"] == "research"
        bad = client.post("/api/strategies/api-cand-test/stage", json={"stage": "nonsense"})
        assert bad.status_code == 400
    finally:
        (ROOT / "output/strategy_pool/candidates/api-cand-test.json").unlink(missing_ok=True)
        client.get("/api/strategies")


def test_daily_script_syncs_pool_before_freshness_audit():
    text = (ROOT / "scripts/run_daily_production.sh").read_text(encoding="utf-8")
    assert "scripts/sync_strategy_pool.py --strict" in text
    pool_at = text.index("sync_strategy_pool.py --strict")
    audit_at = text.index("audit_web_freshness.py --strict")
    assert pool_at < audit_at, "策略池必须排在 Web 页面新鲜度体检之前"


def test_freshness_audit_covers_strategy_pool_page():
    text = (ROOT / "scripts/audit_web_freshness.py").read_text(encoding="utf-8")
    assert "策略池" in text
    assert "_strategy_pool_min_date" in text
    assert "min(dates)" in text, "取 min 而不是 max：有任何一个生产策略落后都要算不达标"


def test_frontend_wires_pool_page_and_heatmap():
    """前端接线：导航有策略池、路由有 /strategies、热力图组件挂在行业页上。"""
    assert "'策略池'" in (ROOT / "frontend/src/App.vue").read_text(encoding="utf-8")
    assert "'/strategies'" in (ROOT / "frontend/src/router/index.ts").read_text(encoding="utf-8")
    page = (ROOT / "frontend/src/pages/StrategyPool.vue").read_text(encoding="utf-8")
    assert "config/strategies.json" in page
    assert "registerStrategyCandidate" in (ROOT / "frontend/src/api/client.ts").read_text(encoding="utf-8")

    industry = (ROOT / "frontend/src/pages/IndustryHeat.vue").read_text(encoding="utf-8")
    assert "IndustryHeatmap" in industry
    assert "getIndustryHeatmap" in industry
    heatmap = (ROOT / "frontend/src/components/IndustryHeatmap.vue").read_text(encoding="utf-8")
    assert "squarify" in heatmap


def test_factor_mining_page_can_register_candidates():
    """因子挖掘已并入「投研执行台 ?tab=mining」（2026-10-11），但「入池」出口必须还在。"""
    panel = (ROOT / "frontend/src/components/FactorMiningPanel.vue").read_text(encoding="utf-8")
    assert "registerStrategyCandidate" in panel, "挖掘面板必须有「入池」出口"
    assert "#/strategies" in panel
    console = (ROOT / "frontend/src/pages/AgentConsole.vue").read_text(encoding="utf-8")
    assert "FactorMiningPanel" in console, "执行台里必须挂上挖掘面板"
    router = (ROOT / "frontend/src/router/index.ts").read_text(encoding="utf-8")
    assert "{ path: '/factor-mining', redirect: '/agent?tab=mining' }" in router, "老书签必须重定向，不能 404"


def test_watchlist_is_shared_between_pages():
    """自选股必须是**一份**状态：K 线侧栏、选股日历、行业热度、选股结果共用 composable。"""
    composable = (ROOT / "frontend/src/composables/useWatchlist.ts").read_text(encoding="utf-8")
    assert "kline.watchlist.v1" in composable
    for rel in ("frontend/src/components/StockWatchlist.vue",
                "frontend/src/pages/SelectionCalendar.vue",
                "frontend/src/pages/IndustryHeat.vue",
                "frontend/src/pages/StockSelection.vue"):
        assert "useWatchlist" in (ROOT / rel).read_text(encoding="utf-8"), \
            f"{rel} 必须用共享自选（否则又会出现'各页各一份'）"
    calendar = (ROOT / "frontend/src/pages/SelectionCalendar.vue").read_text(encoding="utf-8")
    assert "只看自选" in calendar
    assert ".ok { color: var(--accent-up); }" in calendar, "A 股口径：红涨绿跌"


# ─── 卡片折叠规则（2026-10-10 用户要求）──────────────────────────────────────
def test_pool_collapse_rule_is_significance_gated():
    """用户要求："策略池里的候补策略显著为负的，默认折叠，有正向收益的和中证500 对比收益曲线。"

    改版后折叠逻辑抽在 `components/strategyDetail.ts`（公共显示项模块的一部分），页面只消费它：
      * 默认折叠 = 负收益 **且样本足够（≥30 个收益观测）** 且日度 t ≤ -1.5（"显著为负"的粗口径）；
      * 正期望 / 样本不足 → 一律展开；正期望卡片显示与中证500 的对比曲线。
    为什么必须带样本量门槛：生产策略的"名单跟踪"常常只有 1~2 个观测（-0.46% 这种），
    拿它当"显著为负"把生产卡也折叠掉，页面就废了（第一版实测踩到）。
    """
    module = (ROOT / "frontend/src/components/strategyDetail.ts").read_text(encoding="utf-8")
    for token in ("shouldCollapse", "isThin", "minObsForJudgement", "significantT", "dailyT"):
        assert token in module, f"折叠规则缺 {token}（应在 strategyDetail.ts 里）"
    assert "curve.observations < RULES.minObsForJudgement" in module
    assert "curve.t <= -RULES.significantT" in module
    page = (ROOT / "frontend/src/pages/StrategyPool.vue").read_text(encoding="utf-8")
    assert "shouldCollapse" in page, "列表/详情都要用同一条规则"
    assert "curveCollapsed" in page, "详情里负期望的曲线默认折叠、可手动展开"
    # 曲线组件仍是"有正向收益就展示曲线"的载体
    perf = (ROOT / "frontend/src/components/StrategyPerformance.vue").read_text(encoding="utf-8")
    assert "benchmark_name" in perf and "个收益观测" in perf


def test_pool_api_exposes_curve_direction_for_the_collapse_rule():
    """折叠规则吃的是 `performance.<curve>.metrics.total_return` 与 `nav` —— 接口必须给到。"""
    path = ROOT / "output/strategy_pool/performance.json"
    if not path.exists():
        import pytest
        pytest.skip("还没跑过 build_strategy_pool_performance.py")
    payload = json.loads(path.read_text(encoding="utf-8"))
    negative = [ident for ident, row in payload["strategies"].items()
                if (row.get("research") or row.get("tracking") or {}).get("metrics", {}).get("total_return", 0) < 0]
    assert "first_ban_lowxi" in negative, "负期望策略必须能被识别出来（否则不会折叠）"
    research = payload["strategies"]["first_ban_lowxi"]["research"]
    assert len(research["nav"]) > 100 and research["metrics"]["total_return"] < -0.5
    rot = payload["strategies"]["rot_arm"]["research"]
    assert rot["metrics"]["total_return"] > 0, "rot_arm 应为正期望（默认展开并画曲线）"


# ─── 布局改版：左侧列表 + 右侧详情 / 公共显示项 / 优劣 / 对比曲线（2026-10-10 第十二轮）──
def test_pool_page_uses_master_detail_layout():
    """用户要求："策略池前端做成左侧策略列表，点击显示详细…整体布局再优化，层次感更分明"。"""
    page = (ROOT / "frontend/src/pages/StrategyPool.vue").read_text(encoding="utf-8")
    assert 'class="pool-layout"' in page, "要有左右两栏容器"
    assert 'class="pool-side"' in page and 'class="pool-main"' in page
    assert "strategy-item" in page and "select(s.id)" in page, "左侧列表点击切换详情"
    assert "selectedId" in page and "current" in page, "详情由选中项驱动"
    assert "strategy-grid" not in page, "旧的卡片网格应已被列表+详情取代"
    # 层次：概览 → 公共指标 → 优劣 → 单策略曲线 → 多策略对比 → 配置与产物
    for section in ("公共指标", "优劣", "与中证500 对比", "多策略对比", "配置与产物"):
        assert section in page, f"缺分区：{section}"


def test_common_display_fields_are_extracted_for_every_strategy():
    """公共显示项抽在 strategyDetail.ts：身份 / 新鲜度 / 曲线 / 配置 / 优劣 五块，所有策略同一套。"""
    module = (ROOT / "frontend/src/components/strategyDetail.ts").read_text(encoding="utf-8")
    for block in ("identity", "freshness", "curve", "config", "verdict"):
        assert block in module, f"公共显示项缺 {block}"
    for rule in ("minObsForJudgement", "minObsForConfidence", "significantT", "bigDrawdown"):
        assert rule in module, f"判定阈值缺 {rule}"
    assert "dailyT" in module and "shouldCollapse" in module and "primaryCurve" in module
    # 优劣必须由数据规则给出（带数字原因），不是手写文案
    for item in ("显著负期望", "跑输中证500", "样本不足一年", "回撤偏大", "仍是候选", "样本充足"):
        assert item in module, f"优劣规则缺 {item}"


def test_multi_strategy_compare_aligns_to_common_window():
    """多策略对比曲线：先取所有曲线的**日期交集**，再各自归一到窗口起点=1（否则对比是假的）。"""
    comp = (ROOT / "frontend/src/components/StrategyCurveCompare.vue").read_text(encoding="utf-8")
    assert "windowRange" in comp and "s.dates[0]).sort()" in comp, "要算共同窗口"
    assert "series.nav[i] / base" in comp, "要在共同窗口内归一到 1"
    assert "KIND_LABEL" in comp, "图例要标出每条线的口径（毛收益/含成本/逐笔模拟）"
    assert "__benchmark__" in comp, "基准单独画，不能混进策略图例"
    page = (ROOT / "frontend/src/pages/StrategyPool.vue").read_text(encoding="utf-8")
    assert "StrategyCurveCompare" in page and "compareSeries" in page
    assert "compareIds" in page and "toggleCompare" in page, "要能勾选参与对比的策略"


def test_pool_list_significance_marks_and_annual_default():
    """列表负期望保留显著性规则；默认详情跟随年化排序第一项。"""
    page = (ROOT / "frontend/src/pages/StrategyPool.vue").read_text(encoding="utf-8")
    assert "shouldCollapse(detailOf(s).curve)" in page, "列表负期望标记要用显著规则"
    assert "const ranked = visible.value" in page, "默认详情应与年化排序后的列表第一项一致"
    assert "filterTabs" in page and "正期望" in page and "负期望" in page, "左侧要有按方向筛选"


# ─── 结构型手臂（box_arm）进池 + 风险调整后收益规则（2026-10-10 第十三轮）──────
def test_box_arm_registered_with_honest_sensitivity_note():
    """用户要求："继续"（把上一轮的结构型信号接成候选）。它必须**连着参数敏感性一起**进池。"""
    config = json.loads((ROOT / "config/strategies.json").read_text(encoding="utf-8"))
    entry = next((s for s in config["strategies"] if s["id"] == "box_arm"), None)
    assert entry, "config/strategies.json 必须登记 box_arm"
    assert entry["stage"] == "candidate"
    assert entry.get("no_tracking") is True, "语义是次日开盘买/当日卖 → 不做名单跟踪"
    assert entry["curve_source"]["kind"] == "strategy_rule_net"
    assert "参数极敏感" in entry["summary"] and "weekly×10" in entry["summary"], \
        "摘要必须写明相邻口径的失败结果（否则只看最优格就是自欺）"
    script = (ROOT / "scripts/select_box_breakout_arm.py").read_text(encoding="utf-8")
    assert "parameter_sensitivity" in script and "sensitivity_note" in script
    payload = json.loads((ROOT / "output/research/box_breakout/curve/curve.json").read_text(encoding="utf-8")) \
        if (ROOT / "output/research/box_breakout/curve/curve.json").exists() else {}
    if payload:
        sensitivity = {(x["cadence"], x["holdings"]): x["total_return_simple"] for x in payload["parameter_sensitivity"]}
        assert sensitivity, "敏感性表不能为空"
        assert all(v < 0 for v in sensitivity.values()), f"相邻口径都应为负（实测）：{sensitivity}"


def test_verdict_flags_weak_risk_adjusted_returns():
    """卡玛与夏普都 < 0.5（且样本≥一年）→ 记「风险调整后收益偏弱」；否则会有"正期望"但风险很差的卡混过去。"""
    module = (ROOT / "frontend/src/components/strategyDetail.ts").read_text(encoding="utf-8")
    assert "weakRiskAdjusted" in module and "风险调整后收益偏弱" in module
    assert "curve.calmar < RULES.weakRiskAdjusted" in module and "curve.sharpe < RULES.weakRiskAdjusted" in module
    path = ROOT / "output/strategy_pool/performance.json"
    if not path.exists():
        import pytest
        pytest.skip("还没跑过 build_strategy_pool_performance.py")
    row = json.loads(path.read_text(encoding="utf-8"))["strategies"].get("box_arm")
    if not row or not row.get("research"):
        import pytest
        pytest.skip("box_arm 还没有曲线")
    metrics = row["research"]["metrics"]
    assert metrics["total_return"] > 0 and metrics["max_drawdown"] <= -0.30
    assert metrics["calmar"] is not None and metrics["calmar"] < 0.5


# ─── 方向徽章 / 反例档案 / 侧栏排序（2026-10-10 第十五轮）──────────────────────
def test_source_tabs_and_watchlist_groups_carry_verdict_badges():
    """用户要求："继续"（① 正期望/负期望徽章 ② 负期望也进来源但标反例 ③ 侧栏排序）。

    徽章必须由**同一套判定**驱动（`strategyDetail.ts` 的 headline），否则"策略池说正期望、
    选股页说样本不足"这种自相矛盾迟早出现。
    """
    comp = (ROOT / "frontend/src/composables/usePoolVerdicts.ts").read_text(encoding="utf-8")
    assert "strategyDetail(" in comp and "selection_source" in comp, "来源→判定映射要复用策略池判定"
    assert "verdicts" in comp and "60_000" in comp, "要有模块级缓存，别每页各拉一次还各算一套"

    selection = (ROOT / "frontend/src/pages/StockSelection.vue").read_text(encoding="utf-8")
    assert "usePoolVerdicts" in selection and "tab-verdict" in selection
    assert "verdicts[opt.key]" in selection, "来源 tab 要按 source 显示徽章"

    helper = (ROOT / "frontend/src/components/watchlistStrategies.ts").read_text(encoding="utf-8")
    assert "GroupVerdict" in helper and "sortGroups" in helper and "GroupSortKey" in helper
    watchlist = (ROOT / "frontend/src/components/StockWatchlist.vue").read_text(encoding="utf-8")
    assert "sortedGroups" in watchlist and "group-sort" in watchlist and "g-verdict" in watchlist
    assert "按相对中证500" in watchlist and "按日度 t" in watchlist, "排序键要有累计/超额/t"


def test_negative_card_is_kept_as_counterexample_source():
    """负期望的策略也要能查（反例档案），但来源说明里必须写明"不要据此下单"。"""
    config = json.loads((ROOT / "config/strategies.json").read_text(encoding="utf-8"))
    entry = next((s for s in config["strategies"] if s["id"] == "first_ban_lowxi"), None)
    assert entry and entry["selection_source"] == "cn_first_ban_lowxi"
    assert entry["ledger_sources"] == ["first_ban_lowxi"]
    from backend.services.selection_sources import SOURCES
    spec = SOURCES["cn_first_ban_lowxi"]
    assert "反例档案" in spec["note"] and "不要" in spec["note"]
    from backend.api.calendar import STRATEGY_META
    assert "first_ban_lowxi" in STRATEGY_META
    ledger = (ROOT / "scripts/build_selection_ledger.py").read_text(encoding="utf-8")
    assert "read_first_ban_lowxi_history" in ledger and '"first_ban_lowxi"' in ledger
    page = (ROOT / "frontend/src/pages/SelectionCalendar.vue").read_text(encoding="utf-8")
    assert page.count("first_ban_lowxi") >= 4 and "F=first_ban_lowxi" in page
