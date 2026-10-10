"""数据体检的判据（回归：别再把"已停止交易"的票算成数据落后）。

背景（2026-10-09 排查）：体检报未达标 3 项，逐项查下来
  * `adj_factor`：**不是缺数据** —— Tushare relay 的 adj_factor 接口可用，
    是配置里 `[relay_data].adjustment_factors_enabled` 从来没开；
  * `intraday_5min`：4,522/4,533 只已到最新交易日，**真实落后 0 只**，
    剩下 11 只是停牌/退市（日线与分时都停在同一天），是判据用全市场 as_of 去卡个股；
  * `financials`：待补 1 只 = *ST萃华（002731.SZ，日线止于 2026-08-31），
    上游 Tushare 侧最新报告期也只到 2025-09-30 —— 公司不再披露，不是我们没抓。
本文件把这三条判据固化下来，防止以后又被"更严格"的通用规则覆盖。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import data_completeness_report as dcr  # noqa: E402

AS_OF = "2026-10-08"


def test_intraday_classify_exempts_suspended_stocks():
    d = lambda s: pd.Timestamp(s).date()
    # 正常票：分时追到 as_of → ok；没追到 → lag
    assert dcr.classify_intraday(d(AS_OF), d(AS_OF), AS_OF) == "ok"
    assert dcr.classify_intraday(d("2026-09-30"), d(AS_OF), AS_OF) == "lag"
    # 停牌/退市：自己最后一根日线就早于 as_of → 分时停在同一天算豁免
    assert dcr.classify_intraday(d("2026-08-31"), d("2026-08-31"), AS_OF) == "suspended"
    # 停牌票但分时比自己的日线还旧 → 仍算真实落后
    assert dcr.classify_intraday(d("2026-08-01"), d("2026-08-31"), AS_OF) == "lag"
    # 从没有过分时文件（None）且已停牌 → 豁免；正常票没有分时文件 → 落后
    assert dcr.classify_intraday(None, d("2026-08-31"), AS_OF) == "suspended"
    assert dcr.classify_intraday(None, d(AS_OF), AS_OF) == "lag"


def test_financials_exemption_criteria():
    d = lambda s: pd.Timestamp(s).date()
    assert dcr.is_stopped_stock(d("2026-08-31"), AS_OF) is True      # 已停止交易 → 豁免
    assert dcr.is_stopped_stock(None, AS_OF) is True                  # 没有日线 → 豁免
    assert dcr.is_stopped_stock(d(AS_OF), AS_OF) is False             # 正常交易 → 必须补


def test_adj_factor_check_reads_coverage_file(tmp_path, monkeypatch):
    base = tmp_path / "assets/data/raw/adjustment_factor_snapshots/market=CN/source=tushare_relay"
    (base / "date=20261008").mkdir(parents=True)
    pd.DataFrame({"stock_code": ["000001.SZ"], "trade_date": [pd.Timestamp(AS_OF)],
                  "adj_factor": [142.07]}).to_parquet(base / "date=20261008/adj_factor.parquet")
    (base / "_coverage.json").write_text(json.dumps({
        "latest_date": AS_OF, "stocks_on_latest": 5572, "by_date_files": 8, "by_stock_files": 600,
    }), encoding="utf-8")
    monkeypatch.setattr(dcr, "ROOT", tmp_path)
    check = dcr.check_adj_factor()
    assert check.latest == AS_OF
    assert "5572" in check.detail and "按日快照 8 天" in check.detail


def test_check_dataclass_has_self_judged_flag():
    """分时/财报的结论由各自口径给出，主流程不得用 latest==as_of 覆盖。"""
    check = dcr.Check("intraday_5min", latest=AS_OF, ok=True, self_judged=True)
    assert check.self_judged is True
    assert dcr.Check("boards").self_judged is False


def test_daily_automation_wires_adj_factor():
    text = (ROOT / "scripts/run_daily_production.sh").read_text(encoding="utf-8")
    assert "run_data_step adj_factor uv run python scripts/sync_cn_adj_factor.py" in text
    assert "DAILY_ADJ_FACTOR" in text


@pytest.mark.skipif(not (ROOT / "config/cn_pipeline.toml").exists(), reason="配置缺失")
def test_relay_adj_factor_flag_is_enabled():
    text = (ROOT / "config/cn_pipeline.toml").read_text(encoding="utf-8")
    assert "adjustment_factors_enabled = true" in text, \
        "adj_factor 同步依赖这个开关；曾长期为 false 导致体检一直报未达标"
