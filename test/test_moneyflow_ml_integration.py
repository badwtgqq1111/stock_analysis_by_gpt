"""Research-only ML strategy wiring; preserve timing and performance semantics."""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from scripts import moneyflow_ml as ml
from scripts import build_strategy_pool_performance as perf
from backend.services import strategy_registry as registry

ROOT = Path(__file__).resolve().parents[1]


def test_export_has_no_future_label_and_marks_retrospective_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(ml, "OUT", tmp_path)
    monkeypatch.setattr(ml, "ROOT", tmp_path)
    rows = pd.DataFrame({"trade_date": pd.to_datetime(["2026-10-14"]),
                         "stock_code": ["000001.SZ"], "score": [.2], "close": [10.],
                         "next_open": [10.1], "label": [.99]})
    result = ml.export_selection_artifacts(rows, pd.DataFrame(), variant="test_ml")
    exported = pd.read_csv(tmp_path / "test_ml_selection.csv")
    assert result["rows"] == result["dates"] == 1
    assert "label" not in exported
    assert exported.record_kind.iloc[0] == "retrospective_execution_filtered"
    assert "次日开盘后筛选" in exported.note.iloc[0]
    assert (tmp_path / "test_ml_history/2026-10-14.csv").exists()


def _curve(tmp_path, monkeypatch):
    monkeypatch.setattr(perf, "ROOT", tmp_path)
    dates = pd.bdate_range("2026-01-05", periods=3)
    bench = pd.Series([100., 102., 103.], index=dates)
    p = tmp_path / "curve.csv"
    pd.DataFrame({"trade_date": dates, "ml_full_nb": [1., 1.2, 1.1],
                  "csi500": [1., 1.02, 1.03]}).to_csv(p, index=False)
    return p, bench


def test_multicolumn_replay_uses_declared_columns_and_methodology(tmp_path, monkeypatch):
    p, bench = _curve(tmp_path, monkeypatch)
    r = perf.replay(p, bench, nav_column="ml_full_nb", benchmark_column="csi500",
                    methodology="10日持有；每边15.5bp")
    assert r["observations"] == 2
    assert r["metrics"]["total_return"] == pytest.approx(.1)
    assert r["metrics"]["annual_return"] is None
    assert r["methodology"] == "10日持有；每边15.5bp"


@pytest.mark.parametrize("invalid", ["missing", "nan", "zero", "mismatch", "calendar"])
def test_replay_validates_actual_benchmark(tmp_path, monkeypatch, invalid):
    p, bench = _curve(tmp_path, monkeypatch)
    f = pd.read_csv(p)
    if invalid == "missing":
        f = f.drop(columns="csi500")
    elif invalid == "calendar":
        bench = bench.iloc[:-1]
    else:
        f.loc[1, "csi500"] = {"nan": np.nan, "zero": 0., "mismatch": 2.}[invalid]
    f.to_csv(p, index=False)
    with pytest.raises(ValueError):
        perf.replay(p, bench, nav_column="ml_full_nb", benchmark_column="csi500")


def test_registered_as_research_with_selection_and_kline_history():
    spec = registry.spec("moneyflow_ml_nb")
    assert spec["stage"] == "research" and spec["refresh"]["mode"] == "skip"
    assert not spec["curve"]["tracking"]
    wiring = registry.consumers(spec)
    assert wiring["selection_page"]["wired"]
    assert wiring["kline_watchlist"]["wired"]
    assert wiring["selection_calendar"]["wired"]
    assert not wiring["daily_refresh"]["wired"]


def test_real_selection_source_and_ledger_visible_to_kline():
    from backend.services.selection_sources import load_source
    from backend.api.stock_info import _selection_block
    from scripts.build_selection_ledger import read_moneyflow_ml_nb_history
    source = load_source("cn_moneyflow_ml_nb")
    assert source["row_count"] == 5 and source["trade_date"] == "2026-10-14"
    assert "label" not in [c["key"] for c in source["columns"]]
    rows = read_moneyflow_ml_nb_history()
    assert len(rows) == 2584
    assert len({r["decision_date"] for r in rows}) == 531
    code = source["rows"][0]["stock_code"]
    block = _selection_block(code)
    assert any(r["source"] == "moneyflow_ml_nb" for r in block["rows"])


def test_standard_sharpe_is_not_legacy_cagr_divided_by_volatility():
    spec = registry.spec("moneyflow_ml_nb")["curve"]["replay"]
    raw = pd.read_parquet(ROOT / "assets/data/meta/cn_benchmark_daily.parquet")
    raw = raw[raw.ts_code == "000905.SH"].copy()
    raw["trade_date"] = pd.to_datetime(raw.trade_date)
    bench = raw.set_index("trade_date").close.sort_index()
    r = perf.replay(ROOT / spec["nav_csv"], bench, nav_column="ml_full_nb", benchmark_column="csi500")
    assert r["metrics"]["total_return"] == pytest.approx(2.4903, abs=.0001)
    assert r["metrics"]["max_drawdown"] == pytest.approx(-.5440, abs=.0001)
    assert r["metrics"]["annual_return"] == pytest.approx(.8098, abs=.0001)
    assert r["metrics"]["sharpe_rf0"] == pytest.approx(1.0379, abs=.0001)


def test_retrospective_ml_curve_visible_but_excluded_from_rankings():
    from scripts import sync_strategy_pool as pool
    snapshot = pool.build_pool()
    row = next(x for x in snapshot["strategies"] if x["id"] == "moneyflow_ml_nb")
    assert row["ranking_eligible"] is False
    assert row["performance"]["research"]["status"] == "ok"
    assert row["performance"]["research"]["nav"]
    assert "事后筛选" in row["annual_rank_reason"]
    assert not any(k in row for k in ("annual_rank", "return_rank", "excess_rank"))
    for key in ("by_annual_return", "by_total_return", "by_excess_return"):
        assert not any(x["id"] == "moneyflow_ml_nb" for x in snapshot["rankings"][key])


def test_ranking_opt_out_clears_old_ranks_and_keeps_curve():
    from scripts import sync_strategy_pool as pool
    curve = {"nav": [1., 2.], "observations": 300,
             "metrics": {"annual_return": 99., "total_return": 99.}}
    row = {"id": "retrospective", "ranking_eligible": False,
           "performance": {"research": curve}, "annual_rank": {"rank": 1},
           "return_rank": {"rank": 1}, "excess_rank": {"rank": 1}}
    result = pool._return_ranking([row])
    assert result["by_annual_return"] == result["by_total_return"] == result["by_excess_return"] == []
    assert row["performance"]["research"] is curve
    assert not any(k in row for k in ("annual_rank", "return_rank", "excess_rank"))
