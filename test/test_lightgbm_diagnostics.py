import sys
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from factor_engine.ml.diagnostics import compute_lightgbm_model_diagnostics
from factor_engine.ml.lightgbm_ranker import LightGBMRankerPipeline
from factor_engine.ml.model_manifest import build_lightgbm_model_manifest, feature_config_hash
from factor_engine.ml.validation import purged_time_series_splits


def test_lightgbm_diagnostics_handles_legacy_csv_without_momentum_columns():
    ranking = pd.DataFrame(
        [
            {"stock_code": "00001", "ranking_score": 80, "overheat_penalty_score": 0},
            {"stock_code": "00002", "ranking_score": 70, "overheat_penalty_score": 20},
        ]
    )
    selected = pd.DataFrame(
        [
            {"stock_code": "00001", "selected": True, "ranking_score": 80, "overheat_penalty_score": 0},
        ]
    )

    diagnostics = compute_lightgbm_model_diagnostics(ranking, selected)

    assert diagnostics["ranking_rows"] == 2
    assert diagnostics["selected_rows"] == 1
    assert diagnostics["red_flags"] == []
    assert "rerun_select_to_export_high_chase_score" in diagnostics["recommendations"]


def test_lightgbm_diagnostics_flags_selected_high_chase_stock():
    ranking = pd.DataFrame(
        [
            {
                "stock_code": "02513",
                "ranking_score": 95,
                "price_return_20d_pct": 45,
                "price_return_60d_pct": 120,
                "price_return_120d_pct": 220,
                "price_position_52w_high": 99,
                "ma60_gap_pct": 70,
                "high_chase_score": 92,
                "overheat_penalty_score": 80,
            },
        ]
    )
    selected = ranking.assign(selected=True)

    diagnostics = compute_lightgbm_model_diagnostics(ranking, selected)

    assert diagnostics["selected_high_chase_rate"] == 1.0
    assert diagnostics["production_gate_pass"] is False
    assert "selected_high_chase_rate_exceeds_20pct" in diagnostics["production_gate_failures"]
    assert "selected_contains_60d_multibagger" in diagnostics["production_gate_failures"]
    assert "selected_contains_120d_multibagger" in diagnostics["production_gate_failures"]
    assert "high_chase_concentration=100.0%_selected_exceeds_20pct" in diagnostics["red_flags"]
    assert "multibagger_60d_present=100.0%" in diagnostics["red_flags"]
    assert "multibagger_120d_present=100.0%" in diagnostics["red_flags"]
    assert diagnostics["selected_high_chase_stocks"][0]["stock_code"] == "02513"


def test_purged_time_series_splits_remove_overlap_and_embargo():
    dates = pd.date_range("2024-01-01", periods=30, freq="D")

    folds = purged_time_series_splits(dates, n_splits=3, purge_days=3, embargo_days=2)

    assert len(folds) == 3
    first = folds[0]
    assert first.test_start == dates[0]
    assert first.test_end == dates[9]
    assert all(date > first.test_end + pd.Timedelta(days=2) for date in first.train_dates)
    second = folds[1]
    assert all(
        date < second.test_start - pd.Timedelta(days=3)
        or date > second.test_end + pd.Timedelta(days=2)
        for date in second.train_dates
    )


def test_model_manifest_feature_hash_is_stable():
    metadata = {
        "feature_columns": ["A", "B"],
        "feature_preprocess": "qlib_robust",
        "neutralization_mode": "industry_size",
        "neutralize_label": True,
        "label_method": "CSRankNorm",
        "label_horizon": 20,
        "execution_delay": 1,
        "objective": "mse",
        "oos_metrics": {"rank_ic_mean": 0.1},
    }

    manifest = build_lightgbm_model_manifest(factor_set="alpha158_hk", model_metadata=metadata)

    assert manifest["feature_count"] == 2
    assert manifest["neutralization_config"]["mode"] == "industry_size"
    assert manifest["label_config"]["label_horizon"] == 20
    assert manifest["feature_hash"] == feature_config_hash(
        ["A", "B"],
        {
            "factor_set": "alpha158_hk",
            "feature_preprocess": "qlib_robust",
            "neutralization_mode": "industry_size",
            "label_method": "CSRankNorm",
            "objective": "mse",
            "params": {},
        },
    )


def test_run_cli_select_lightgbm_defaults_to_alpha_zoo_hk():
    from cli import main as cli_main

    captured = {}
    original = cli_main.main_select_stocks

    def fake_main_select_stocks(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    cli_main.main_select_stocks = fake_main_select_stocks
    try:
        result = cli_main.run_cli(["select", "--analysis-mode", "lightgbm"])
    finally:
        cli_main.main_select_stocks = original

    assert result == {"ok": True}
    assert captured["factor_set"] == "alpha_zoo_hk"
    assert captured["analysis_mode"] == "lightgbm"
    assert captured["stock_limit"] is None


def test_run_cli_select_passes_stock_limit_to_scan_scope():
    from cli import main as cli_main

    captured = {}
    original = cli_main.main_select_stocks

    def fake_main_select_stocks(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    cli_main.main_select_stocks = fake_main_select_stocks
    try:
        result = cli_main.run_cli(["select", "--analysis-mode", "lightgbm", "--stock-limit", "20"])
    finally:
        cli_main.main_select_stocks = original

    assert result == {"ok": True}
    assert captured["stock_limit"] == 20
    assert captured["validation_stock_limit"] == 20


def test_run_lightgbm_model_diagnostics_reads_feature_importance_payload_json():
    from run import _run_lightgbm_model_diagnostics

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        ranking_csv = tmp / "ranking.csv"
        selected_csv = tmp / "selected.csv"
        feature_json = tmp / "feature_importance.json"
        output_json = tmp / "diagnostics.json"

        ranking = pd.DataFrame(
            [
                {"stock_code": "00001", "ranking_score": 90, "high_chase_score": 10},
                {"stock_code": "00002", "ranking_score": 80, "high_chase_score": 20},
            ]
        )
        ranking.to_csv(ranking_csv, index=False)
        ranking.head(1).to_csv(selected_csv, index=False)
        feature_json.write_text(
            json.dumps(
                {
                    "feature_importance": [
                        {"feature_name": "ROC20", "importance": 10.0},
                        {"feature_name": "STD20", "importance": 5.0},
                    ]
                }
            ),
            encoding="utf-8",
        )

        _run_lightgbm_model_diagnostics(
            SimpleNamespace(
                ranking_csv=str(ranking_csv),
                selected_csv=str(selected_csv),
                feature_importance_json=str(feature_json),
                high_chase_threshold=80.0,
                multibagger_60d_threshold=100.0,
                multibagger_120d_threshold=180.0,
                output_json=str(output_json),
                json=True,
            )
        )

        diagnostics = json.loads(output_json.read_text(encoding="utf-8"))

    assert diagnostics["feature_family_importance"]
    assert diagnostics["top_20_features"][0]["feature_name"] == "ROC20"


def test_lambdarank_failure_is_not_silently_downgraded_to_regression():
    pipeline = LightGBMRankerPipeline(objective_mode="lambdarank")

    def fail_ranking(*_args, **_kwargs):
        raise ValueError("ranking exploded")

    pipeline._fit_predict_ranking = fail_ranking

    try:
        pipeline.fit_predict(pd.DataFrame({"x": []}), pd.DataFrame({"y": []}))
        raised = None
    except RuntimeError as exc:
        raised = exc

    assert raised is not None
    assert "ranking exploded" in str(raised)


def test_lightgbm_abtest_uses_factor_set_specific_exports_and_select_flags():
    import run

    calls = []

    class FakeResult:
        returncode = 0
        stdout = "[ok]"
        stderr = ""

    def fake_run(cmd, capture_output, text):
        calls.append(cmd)
        return FakeResult()

    import subprocess

    original_subprocess_run = subprocess.run
    subprocess.run = fake_run
    try:
        run._run_lightgbm_abtest(
            SimpleNamespace(
                factor_set="alpha158_hk",
                compare="none,industry",
                days=365,
                top_n=10,
                objective_mode="lambdarank",
                model_type="lightgbm",
                max_features=50,
                stock_limit=None,
                stock_codes=["00700", "00005"],
                disable_theme_features=True,
                show_progress=True,
                backtest_date="2026-06-05",
                export_csv="output/test_ab",
                output_json=None,
            )
        )
    finally:
        subprocess.run = original_subprocess_run

    assert len(calls) == 2
    first = calls[0]
    assert "--factor-set" in first
    assert first[first.index("--factor-set") + 1] == "alpha158_hk"
    assert "--model-objective" in first
    assert first[first.index("--model-objective") + 1] == "lambdarank"
    assert "--neutralization-mode" in first
    assert first[first.index("--neutralization-mode") + 1] == "none"
    assert "--no-theme-features" in first
    assert "--stock-codes" in first
    assert "--export-csv" in first
    assert first[first.index("--export-csv") + 1] == "output/test_ab_neutralization_none"


def test_theme_ablation_uses_no_theme_flag_for_without_theme_run():
    import subprocess
    import run

    calls = []

    class FakeResult:
        returncode = 0
        stdout = "[ok]"
        stderr = ""

    original_subprocess_run = subprocess.run

    def fake_run(cmd, capture_output, text):
        calls.append(cmd)
        return FakeResult()

    subprocess.run = fake_run
    try:
        run._run_theme_ablation(
            SimpleNamespace(
                factor_set=None,
                days=365,
                top_n=10,
                objective_mode="regression_csrank",
                model_type="lightgbm",
                neutralization_mode="industry",
                max_features=0,
                stock_limit=None,
                stock_codes=None,
                show_progress=False,
                backtest_date=None,
                overlay_weights=None,
                export_csv="output/theme_test",
                output_json=None,
            )
        )
    finally:
        subprocess.run = original_subprocess_run

    assert len(calls) == 2
    assert "--no-theme-features" not in calls[0]
    assert "--no-theme-features" in calls[1]
    assert calls[0][calls[0].index("--factor-set") + 1] == "alpha_zoo_hk"
    assert calls[1][calls[1].index("--export-csv") + 1] == "output/theme_test_theme_without_theme"
