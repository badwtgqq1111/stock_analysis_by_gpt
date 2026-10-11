"""首板+回调+企稳+低吸（聚宽社区「年化79%」）复现研究：三个坑 + 产物契约。

用户要求（2026-10-10）："llm 因子挖掘看一下这个策略。"
本文件钉住复现过程中踩到、且**每一个都会让结论反转**的地方：

  1. 面板里 `busted` 表示"收盘没封住"，而候选池是 `is_zt==1`（收盘封住）→ 在该池内恒为 False；
     拿它当"日内炸板回封"，漏斗会直接断在 0 个样本；
  2. 5 分钟归档的 `date` 列是 `datetime64[us]`，跟 `datetime.date` 比较**恒不命中**（实测 0 行）；
  3. T+1 卖出模拟的涨停价/止损线必须以 **T 日收盘** 为基准（文章写 `high_limit/1.1` = 当日昨收）。
     第一版误用 T 日的前收（T-1 收盘），等于把次日"涨停"门槛压低约 10% →
     67% 的样本被误判成"次日涨停"、策略收益被严重高估。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
PANEL = ROOT / "output/research/zt_board/zt_board_panel.parquet"
INTRADAY = ROOT / "assets/data/raw/intraday_baostock/frequency=5min"


def _load():
    spec = importlib.util.spec_from_file_location(
        "first_ban_lowxi_under_test", ROOT / "scripts/first_ban_lowxi_study.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_busted_is_always_false_inside_the_limit_up_pool():
    """候选池（is_zt==1）里 `busted` 恒为 False —— 不能用它表达"日内炸板回封"（会得 0 样本）。"""
    if not PANEL.exists():
        import pytest
        pytest.skip("还没有跑过 scripts/build_zt_board_features.py")
    frame = pd.read_parquet(PANEL, columns=["is_zt", "busted", "touched_limit", "open_limit", "boards"])
    pool = frame[frame["is_zt"] == 1]
    assert pool["busted"].sum() == 0, "涨停池里 busted 必须恒为 False（它表示收盘没封住）"
    assert (pool["touched_limit"] == 1).mean() > 0.9, "涨停样本几乎都触及过涨停（换手板口径可用）"
    text = (ROOT / "scripts/first_ban_lowxi_study.py").read_text(encoding="utf-8")
    assert "c_busted" in text and "恒为 False" in text, "脚本里要写明这个坑并换成换手板口径"


def test_intraday_date_filter_uses_datetime_not_date_objects():
    """5 分钟归档的 date 是 datetime64，与 datetime.date 比较恒不命中（实测 0 行）。"""
    files = sorted(INTRADAY.glob("*.parquet"))
    if not files:
        import pytest
        pytest.skip("没有 5 分钟归档")
    import pyarrow.parquet as pq
    frame = pq.ParquetFile(files[0]).read(columns=["date"]).to_pandas()
    assert pd.api.types.is_datetime64_any_dtype(frame["date"]), "date 列是 datetime64"
    target = frame["date"].iloc[0].normalize()
    assert int((frame["date"] == target.date()).sum()) == 0, "用 date 对象比较必须是 0 命中（这就是坑）"
    assert int((frame["date"].dt.normalize() == target).sum()) > 0, "归一成 Timestamp 才能命中"
    text = (ROOT / "scripts/first_ban_lowxi_study.py").read_text(encoding="utf-8")
    assert 'pd.to_datetime(frame["date"], errors="coerce").dt.normalize()' in text


def test_t_plus_1_limit_price_uses_t_close_not_previous_close():
    """T+1 的涨停价/止损线以 **T 日收盘** 为基准（用 T-1 收盘会低估约 10%，把收益抬飞）。"""
    text = (ROOT / "scripts/first_ban_lowxi_study.py").read_text(encoding="utf-8")
    call = "intraday_sell_return(str(row[\"stock_code\"]), row[\"next_date\"], float(row[\"close\"]),"
    assert call in text, "T+1 卖出模拟必须传 T 日收盘作为参考价"
    assert 'float(row["prev_close"]),\n                                          float(row["next_open"])' not in text


def test_study_outputs_document_the_data_gaps():
    path = ROOT / "output/research/first_ban_lowxi/study.json"
    if not path.exists():
        import pytest
        pytest.skip("还没跑过 scripts/first_ban_lowxi_study.py")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["n_zt_samples"] > 10000
    funnel = payload["funnel_open_next_ret"]
    assert len(funnel) >= 6 and funnel[0]["name"].startswith("①")
    # 文章口径的每一条都必须是**负期望**（这是复现结论，不是断言"应该"）
    assert all(row["mean"] < 0 for row in funnel if row.get("n"))
    assert payload["final_combo"]["n"] > 1000
    assert payload["final_combo"]["open_next_ret"]["net_after_cost"] < 0
    gaps = " ".join(payload["data_gaps"])
    for token in ("集合竞价", "5 分钟", "分钟级"):
        assert token in gaps, f"数据缺口必须写清楚：{token}"
    rank = (payload.get("intraday") or {}).get("llm_rank") or {}
    if rank.get("top_decile"):
        assert rank["top_decile"]["mean"] < 0.005, "LLM 最强维度叠上去也只是盈亏平衡线附近"
        assert rank["top_decile"]["net_after_cost"] < 0


# ─── 曲线（前端策略池展示）：2026-10-10 第九轮 ────────────────────────────────
def test_curve_matches_the_strategy_intraday_semantics_not_paper_account():
    """曲线必须按策略原始口径（T+1 开盘买、当日卖、隔夜空仓），**不能**用模拟盘引擎
    （它是"持有到下一个决策日"的语义，会把隔夜跳空算进收益、方向都可能反）。"""
    text = (ROOT / "scripts/first_ban_lowxi_curve.py").read_text(encoding="utf-8")
    # 只看**代码行**：模块 docstring 里正解释"为什么不能用 run_paper_account"，那不是调用
    body = text.split('"""', 2)[2] if text.count('"""') >= 2 else text
    code = "\n".join(line for line in body.splitlines() if not line.strip().startswith("#"))
    assert "run_paper_account(" not in code, "不要用模拟盘引擎跑这条策略"
    assert "study.intraday_sell_return(" in text, "卖出必须走文章的日内规则"
    assert "隔夜空仓" in text or "隔日空仓" in text
    config = json.loads((ROOT / "config/strategies.json").read_text(encoding="utf-8"))
    entry = next((s for s in config["strategies"] if s["id"] == "first_ban_lowxi"), None)
    assert entry, "策略池必须登记 first_ban_lowxi"
    assert entry.get("no_tracking") is True, "持仓语义不同 → 不做名单跟踪（否则卡片上是一条方向相反的线）"
    assert entry["curve_source"]["nav_csv"].endswith("nav_engine.csv")
    assert entry["curve_source"]["kind"] == "strategy_rule_net"


def test_pool_card_shows_the_negative_curve():
    path = ROOT / "output/strategy_pool/performance.json"
    if not path.exists():
        import pytest
        pytest.skip("还没跑过 build_strategy_pool_performance.py")
    payload = json.loads(path.read_text(encoding="utf-8"))
    row = payload["strategies"].get("first_ban_lowxi")
    assert row, "绩效快照里要有 first_ban_lowxi"
    assert not row["tracking"].get("nav"), "tracking 必须为空（no_tracking）"
    research = row.get("research")
    assert research and research["status"] == "ok"
    assert research["kind"] == "strategy_rule_net" and research["observations"] > 200
    assert research["benchmark_code"] == "000905.SH"
    # 曲线与结论一致：负收益、跑输中证500
    assert research["metrics"]["total_return"] < -0.5
    assert research["benchmark_metrics"]["total_return"] > 0
    assert research["excess_return"] < -0.5


# ─── 「长期横盘→首板→回调→箱体突破→再回调」是不是同一个逻辑（2026-10-10 第十轮）──
def test_box_breakout_signal_is_pit_and_uses_only_past_window():
    """结构型信号必须是 PIT：箱体上沿只能用 **当日之前** 的 60 日高点，否则就是自己给自己喂答案。"""
    text = (ROOT / "scripts/box_breakout_study.py").read_text(encoding="utf-8")
    assert 'rolling(60, min_periods=40).max().shift(1)' in text, "箱体上沿必须 shift(1)（不含当日）"
    assert 'rolling(60, min_periods=40).sum().shift(1)' in text, "横盘期无涨停的计数也要 shift(1)"
    assert "_nw_t(" in text, "显著性要按回踩日聚合成日度组合再做 NW（横截面相关会把 t 放大）"


def test_box_breakout_conclusion_matches_the_published_numbers():
    """结论契约：结构型毛期望显著为正、但扣费后≈0；文章盘口型各层仍为负 —— 两者不是同一逻辑。"""
    path = ROOT / "output/research/box_breakout/study.json"
    if not path.exists():
        import pytest
        pytest.skip("还没跑过 scripts/box_breakout_study.py")
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = {row["name"]: row for row in payload["comparisons"]}
    structural = next(row for name, row in rows.items() if name.startswith("④ 结构型："))
    assert structural["n"] > 1000
    assert structural["mean"] > 0.002 and structural["win"] > 0.5
    cluster = payload["daily_cluster"]
    assert cluster["nw_t"] > 2.5, "结构型日度 NW t 要显著为正"
    assert abs(cluster["net_after_cost"]) < 0.002, "扣费后基本归零（这正是结论）"
    for name, row in rows.items():
        if name.startswith(("①", "②", "③")):
            assert row["mean"] < 0, f"{name} 应为负期望（文章漏斗）"
    holding = {row["name"]: row for row in payload["holding_variants"]}
    assert any("持有 5 天" in name and row["net_after_cost"] > 0 for name, row in holding.items()), \
        "降换手（持有 5 天）扣费后应为正 —— 说明命门在换手而不是信号方向"
