import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("run_cn_pipeline", ROOT / "scripts" / "run_cn_pipeline.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_cn_pipeline_config_has_layered_stages() -> None:
    config = MODULE.read_config(ROOT / "config" / "cn_pipeline.toml")

    assert config["stages"]["daily_bars"] is True
    assert config["stages"]["intraday_bars"] is False
    assert config["stages"]["fundamental"] is True
    assert config["stages"]["alternative"] is False
    assert config["stages"]["clean_panel"] is True
    assert config["stages"]["lightgbm"] is True
    assert config["stages"]["transformer"] is True
    assert config["stages"]["cnn"] is False
    assert config["stages"]["model_scores"] is True
    assert config["stages"]["selection"] is True
    assert config["pipeline"]["max_workers_stock_info"] == 8
    assert config["pipeline"]["max_workers_valuation"] == 12
    assert config["pipeline"]["quality_report_dir"] == "output/data_quality"
    assert config["intraday_bars"]["lookback_days"] == 1095


def test_cn_pipeline_has_no_subprocess_command_builder() -> None:
    assert not hasattr(MODULE, "commands_for_stage")


def test_daily_and_intraday_commands_are_separate() -> None:
    config = MODULE.read_config(ROOT / "config" / "cn_pipeline.toml")
    assert config["daily_bars"]["frequencies"] == ["daily"]
    assert config["intraday_bars"]["frequencies"] == ["5min", "15min", "30min", "60min"]
    assert config["intraday_bars"]["derive_intraday_from_1min"] is False
    assert config["intraday_bars"]["data_source"] == "tencent"
    assert config["intraday_bars"]["adjust"] == "raw"
    assert config["intraday_bars"]["max_workers"] == 16
    assert config["intraday_bars"]["derive_intraday_from_base"] is True
    assert config["intraday_bars"]["intraday_base_frequency"] == "5min"


def test_run_stage_calls_service_directly() -> None:
    config = MODULE.read_config(ROOT / "config" / "cn_pipeline.toml")

    class FakeService:
        data_source = "akshare"

        def __init__(self):
            self.calls = []

        def bulk_sync_cn_history(self, **kwargs):
            self.calls.append(("daily", kwargs))
            return {"status": "completed"}

        def get_all_stock_codes(self, **kwargs):
            self.calls.append(("codes", kwargs))
            return ["000001"]

        def generate_factor_set(self, **kwargs):
            self.calls.append(("features", kwargs))
            return {"status": "completed", "stock_count": 1}

    service = FakeService()
    result = MODULE.run_stage("daily_bars", config, service)

    assert result["status"] == "completed"
    assert service.calls[0][0] == "daily"
    assert service.calls[0][1]["frequencies"] == ("daily",)
    assert service.calls[0][1]["show_progress"] is True
    assert service.calls[0][1]["quality_report_dir"] == "output/data_quality"
    assert service.calls[0][1]["complete_data"] is False


def test_features_stage_passes_sparse_valuation_staleness_config() -> None:
    config = MODULE.read_config(ROOT / "config" / "cn_pipeline.toml")

    class FakeService:
        def get_all_stock_codes(self, **kwargs):
            return ["000001.SZ"]

        def generate_factor_set(self, **kwargs):
            self.kwargs = kwargs
            return {"status": "completed"}

    service = FakeService()
    MODULE.run_stage("features", config, service)

    assert service.kwargs["config"]["valuation_hk"]["stale_after_trading_days"] == 20


def test_features_stage_precheck_skips_full_feature_read(monkeypatch, tmp_path) -> None:
    config = MODULE.read_config(ROOT / "config" / "cn_pipeline.toml")
    config["pipeline"]["min_training_stocks"] = 1
    coverage_calls = []

    class FakeService:
        def __init__(self, **kwargs):
            pass

        def close(self):
            pass

    def fake_coverage_report(service, min_rows, adjust, *, include_features=True):
        coverage_calls.append(include_features)
        return {
            "stock_count": 1,
            "ohlcv": {"covered_stock_count": 1},
            "features": {"stock_count": 0},
            "stock_info": {"row_count": 1},
            "financial": {"valuation_stock_count": 1, "financial_stock_count": 0},
            "industry": {"industry_l1_count": 1},
        }

    monkeypatch.setattr(MODULE, "MarketDataService", FakeService)
    monkeypatch.setattr(MODULE, "coverage_report", fake_coverage_report)
    monkeypatch.setattr(MODULE, "run_stage", lambda *args: {"success_count": 1, "skipped_count": 0, "rows_written": 2})
    monkeypatch.setattr(MODULE, "write_report", lambda *args: (tmp_path / "report.json", tmp_path / "report.md"))
    monkeypatch.setattr(MODULE, "read_config", lambda path: config)
    monkeypatch.setattr(sys, "argv", ["run_cn_pipeline.py", "--stage", "features", "--report-dir", str(tmp_path)])

    assert MODULE.main() == 0
    assert coverage_calls == [False, False]


def test_coverage_report_hides_large_per_stock_maps_from_console(capsys) -> None:
    class FakeService:
        def cn_backtest_coverage_report(self, **kwargs):
            return {
                "ohlcv": {"row_count": 2, "row_counts": {"000001.SZ": 2}, "latest_trade_dates": {"000001.SZ": "2026-08-07"}},
            }

    report = MODULE.coverage_report(FakeService(), 120, include_features=False)

    output = capsys.readouterr().out
    assert report["ohlcv"]["row_counts"] == {"000001.SZ": 2}
    assert "row_counts" not in output
    assert "latest_trade_dates" not in output


def test_fundamental_stage_skips_baostock_statements_by_default() -> None:
    config = MODULE.read_config(ROOT / "config" / "cn_pipeline.toml")

    class FakeService:
        data_source = "akshare"

        def refresh_cn_stock_info(self, **kwargs):
            return {"status": "completed"}

        def refresh_cn_baidu_valuation_history(self, **kwargs):
            return {"status": "completed"}

        def refresh_cn_financial_metrics(self, **kwargs):
            raise AssertionError("BaoStock statement refresh should be disabled by default")

        def backfill_cn_industry(self, **kwargs):
            return {"status": "completed"}

    result = MODULE.run_stage("fundamental", config, FakeService())

    assert result["financial_metrics"]["skipped"] is True
    assert result["industry"]["status"] == "completed"


def test_intraday_stage_uses_independent_lookback_window() -> None:
    config = MODULE.read_config(ROOT / "config" / "cn_pipeline.toml")
    config["pipeline"]["end_date"] = "2026-08-09"

    class FakeService:
        def bulk_sync_cn_history(self, **kwargs):
            self.kwargs = kwargs
            return {"status": "completed"}

    service = FakeService()
    MODULE.run_stage("intraday_bars", config, service)

    assert service.kwargs["start_date"] == (date(2026, 8, 9) - timedelta(days=1095)).isoformat()
    assert service.kwargs["end_date"] == "2026-08-09"
    assert service.kwargs["frequencies"] == ("5min", "15min", "30min", "60min")
    assert service.kwargs["derive_intraday_from_1min"] is False
    assert service.kwargs["derive_intraday_from_base"] is True
    assert service.kwargs["intraday_base_frequency"] == "5min"
    assert service.kwargs["data_source"] == "tencent"
    assert service.kwargs["adjust"] == "raw"
    assert service.kwargs["max_workers"] == 16


def test_explicitly_requested_disabled_stage_runs() -> None:
    config = MODULE.read_config(ROOT / "config" / "cn_pipeline.toml")

    assert MODULE.should_run_stage("intraday_bars", "intraday_bars", config) is True
    assert MODULE.should_run_stage("intraday_bars", "all", config) is False


def test_clean_panel_stage_calls_materializer() -> None:
    config = MODULE.read_config(ROOT / "config" / "cn_pipeline.toml")

    class FakeService:
        def materialize_clean_feature_panel(self, **kwargs):
            self.kwargs = kwargs
            return {"status": "completed"}

    service = FakeService()
    result = MODULE.run_stage("clean_panel", config, service)
    assert result["status"] == "completed"
    assert service.kwargs["cleaning_version"] == "p0.2.v1"
    assert service.kwargs["days"] == 365
    assert service.kwargs["feature_batch_size"] == 10
    assert service.kwargs["show_progress"] is True


def test_model_scores_stage_loads_persisted_artifacts() -> None:
    config = MODULE.read_config(ROOT / "config" / "cn_pipeline.toml")

    class FakeService:
        def score_clean_feature_panel_models(self, **kwargs):
            self.kwargs = kwargs
            return {"status": "completed"}

    service = FakeService()
    result = MODULE.run_stage("model_scores", config, service)

    assert result["status"] == "completed"
    assert service.kwargs["lightgbm_model_path"].endswith("model.txt")
    assert service.kwargs["transformer_model_path"].endswith("model.pt")


def test_cnn_stage_calls_service_directly() -> None:
    config = MODULE.read_config(ROOT / "config" / "cn_pipeline.toml")

    class FakeService:
        def train_cnn_clean_panel(self, **kwargs):
            self.kwargs = kwargs
            return {"status": "completed"}

    service = FakeService()
    result = MODULE.run_stage("cnn", config, service)

    assert result["status"] == "completed"
    assert service.kwargs["lookback"] == 60
    assert service.kwargs["device"] == "auto"


def test_selection_stage_uses_persisted_model_scores() -> None:
    config = MODULE.read_config(ROOT / "config" / "cn_pipeline.toml")

    class FakeService:
        def select_persisted_model_scores(self, **kwargs):
            self.kwargs = kwargs
            return {"status": "completed"}

    service = FakeService()
    result = MODULE.run_stage("selection", config, service)

    assert result["status"] == "completed"
    assert service.kwargs["model"] == "ensemble"
    assert service.kwargs["top_n"] == 10


def test_oos_prediction_stage_passes_fold_and_transformer_configuration() -> None:
    config = MODULE.read_config(ROOT / "config" / "cn_pipeline.toml")

    class FakeService:
        def generate_cn_oos_predictions(self, **kwargs):
            self.kwargs = kwargs
            return {"status": "completed"}

    service = FakeService()
    result = MODULE.run_stage("oos_predictions", config, service)

    assert result["status"] == "completed"
    assert service.kwargs["models"] == ("lightgbm",)
    assert service.kwargs["purge_days"] == 20
    assert service.kwargs["transformer_lookback"] == 60


def test_paper_account_and_strategy_label_stages_call_services() -> None:
    config = MODULE.read_config(ROOT / "config" / "cn_pipeline.toml")

    class FakeService:
        def run_cn_paper_account(self, **kwargs):
            self.account = kwargs
            return {"status": "completed"}

        def build_cn_strategy_labels(self, **kwargs):
            self.labels = kwargs
            return {"status": "completed"}

    service = FakeService()
    assert MODULE.run_stage("paper_account", config, service)["status"] == "completed"
    assert MODULE.run_stage("strategy_labels", config, service)["status"] == "completed"
    assert service.account["lot_size"] == 100
    assert service.labels["days"] == 756


def test_fundamental_coverage_is_reported_not_a_hard_gate() -> None:
    report = {
        "stock_count": 10,
        "stock_info": {"row_count": 4},
        "financial": {"valuation_stock_count": 3, "financial_stock_count": 2},
        "industry": {"industry_l1_count": 5},
    }

    detail = MODULE.fundamental_availability(report, 0.80)

    assert "optional fields below target" in detail
