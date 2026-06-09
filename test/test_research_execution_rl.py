import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from factor_engine.ml.lightgbm_ranker import LightGBMRankerPipeline
from factor_engine.ml.research import build_purged_cv_report
from factor_engine.ml.shap_monitoring import summarize_portfolio_shap_exposure
from factor_engine.events import build_event_feature_panel
from factor_engine.microstructure import build_intraday_microstructure_features
from factor_engine.portfolio.costs import SupervisedExecutionCostModel, apply_cost_adjusted_scores, build_simulated_tca_report
from factor_engine.rl.execution_simulator import ExecutionOrder, ExecutionSimulator
from factor_engine.rl.imitation import LinearImitationPolicy, build_expert_training_rows
from factor_engine.rl.portfolio_env import PortfolioEnv


def test_cost_adjusted_scores_attach_capacity_and_penalize_score():
    rows = [
        {
            "stock_code": "00001",
            "ranking_score": 80.0,
            "portfolio_weight": 0.05,
            "median_turnover_amount_20d": 1_000_000.0,
            "recent_volatility": 0.25,
        }
    ]

    adjusted = apply_cost_adjusted_scores(rows, initial_capital=100_000)

    assert adjusted[0]["expected_transaction_cost_bps"] > 0
    assert adjusted[0]["liquidity_capacity_score"] > 0
    assert adjusted[0]["cost_adjusted_ranking_score"] < 80.0


def test_simulated_tca_report_computes_shortfall_rows():
    trades = [
        {"stock_code": "00001", "type": "buy", "price": 10.0, "shares": 1000, "gross_amount": 10000.0, "commission": 10.0}
    ]

    report = build_simulated_tca_report(trades)

    assert report[0]["order_id"] == "SIM-000001"
    assert report[0]["implementation_shortfall_bps"] >= 0
    assert report[0]["simulated"] is True


def test_portfolio_env_expert_policy_steps_to_done():
    panel = pd.DataFrame(
        [
            {"trade_date": "2025-01-01", "stock_code": "00001", "ranking_score": 90, "forward_return_20": 0.02, "expected_transaction_cost_bps": 5},
            {"trade_date": "2025-01-01", "stock_code": "00002", "ranking_score": 80, "forward_return_20": -0.01, "expected_transaction_cost_bps": 5},
            {"trade_date": "2025-01-02", "stock_code": "00001", "ranking_score": 70, "forward_return_20": 0.01, "expected_transaction_cost_bps": 5},
            {"trade_date": "2025-01-02", "stock_code": "00002", "ranking_score": 95, "forward_return_20": 0.03, "expected_transaction_cost_bps": 5},
        ]
    )
    env = PortfolioEnv(panel, max_weight=0.50)
    obs = env.reset()
    action = env.expert_policy(obs, top_n=1, max_weight=0.50)

    next_obs, reward, done, info = env.step(action)

    assert done is False
    assert next_obs is not None
    assert np.isfinite(reward)
    assert info["turnover"] > 0


def test_execution_simulator_supports_twap_and_pov():
    bars = pd.DataFrame({"price": [10.0, 10.1, 10.2], "volume": [1000, 2000, 3000]})
    simulator = ExecutionSimulator(bars)
    order = ExecutionOrder(stock_code="00001", side="buy", quantity=600, arrival_price=10.0)

    twap = simulator.schedule(order, algo="twap")
    pov = simulator.schedule(order, algo="pov", max_pov=0.10)

    assert round(float(twap["target_qty"].sum()), 6) == 600.0
    assert round(float(pov["target_qty"].sum()), 6) == 600.0
    assert "implementation_shortfall_bps" in twap.columns


def test_build_purged_cv_report_outputs_fold_metrics():
    rows = []
    for day in pd.date_range("2025-01-01", periods=12, freq="D"):
        for idx in range(4):
            rows.append(
                {
                    "trade_date": day,
                    "stock_code": f"{idx:05d}",
                    "model_score": float(idx),
                    "forward_return_20": float(idx) / 100.0,
                }
            )
    report, summary = build_purged_cv_report(pd.DataFrame(rows), n_splits=3, purge_days=1, embargo_days=1)

    assert len(report) == 3
    assert summary["fold_count"] == 3
    assert summary["rank_ic_mean"] is not None


def test_lambdarank_pipeline_runs_end_to_end_on_synthetic_panel():
    dates = pd.date_range("2024-01-01", periods=14, freq="D")
    stocks = [f"{idx:05d}" for idx in range(8)]
    rng = np.random.default_rng(7)
    feature_rows = []
    target_rows = []
    for date in dates:
        for idx, code in enumerate(stocks):
            feature_rows.append(
                {
                    "trade_date": date,
                    "stock_code": code,
                    "f1": rng.normal() + idx / 10.0,
                    "f2": rng.normal(),
                    "industry_l1": "A" if idx < 4 else "B",
                    "market_cap": 100 + idx * 10,
                }
            )
            target_rows.append(
                {
                    "trade_date": date,
                    "stock_code": code,
                    "forward_return_1": rng.normal() + idx / 100.0,
                }
            )
    pipeline = LightGBMRankerPipeline(
        label_horizon=1,
        execution_delay=1,
        min_train_days=5,
        rolling_step=2,
        valid_fraction=0.2,
        objective_mode="lambdarank",
        neutralization_mode="industry_size",
        params={"n_estimators": 5, "num_leaves": 7, "min_child_samples": 1},
    )

    result, metadata = pipeline.fit_predict(
        pd.DataFrame(feature_rows).set_index("trade_date"),
        pd.DataFrame(target_rows).set_index("trade_date"),
    )

    assert not result.empty
    assert metadata["objective"] == "lambdarank"
    assert metadata["preprocess_metadata"]["mode"] == "qlib_robust"
    assert metadata["neutralization_metadata"]["mode"] == "industry_size"


def test_event_feature_panel_respects_available_at_and_rolls_windows():
    events = pd.DataFrame(
        [
            {"stock_code": "1", "available_at": "2025-01-01 10:00", "event_type": "earnings beat policy", "score": 2, "source": "a"},
            {"stock_code": "00001", "available_at": "2025-01-03 10:00", "event_type": "litigation risk", "score": -1, "source": "b"},
        ]
    )

    panel = build_event_feature_panel(events)

    latest = panel[(panel["stock_code"] == "00001") & (panel["trade_date"] == pd.Timestamp("2025-01-03"))].iloc[0]
    assert latest["event_count_5d"] >= 2
    assert latest["positive_event_score_5d"] >= 1
    assert latest["negative_event_score_1d"] >= 1


def test_microstructure_features_aggregate_intraday_bars():
    idx = pd.to_datetime(["2025-01-02 09:30", "2025-01-02 09:31", "2025-01-02 15:59", "2025-01-02 16:00"])
    bars = pd.DataFrame(
        {
            "Open": [10.0, 10.1, 10.2, 10.3],
            "High": [10.2, 10.2, 10.4, 10.5],
            "Low": [9.9, 10.0, 10.1, 10.2],
            "Close": [10.1, 10.2, 10.3, 10.4],
            "Volume": [100, 200, 300, 400],
        },
        index=idx,
    )

    features = build_intraday_microstructure_features(bars, stock_code="00001")

    assert len(features) == 1
    assert features.iloc[0]["intraday_bar_count"] == 4
    assert "large_trade_imbalance" in features.columns


def test_portfolio_shap_exposure_summarizes_family_weights():
    shap_frame = pd.DataFrame(
        [
            {"trade_date": "2025-01-01", "stock_code": "00001", "feature_name": "ROC20", "feature_family": "momentum", "shap_value": 2.0},
            {"trade_date": "2025-01-01", "stock_code": "00002", "feature_name": "PB", "feature_family": "value", "shap_value": -1.0},
        ]
    )
    holdings = [{"stock_code": "00001", "portfolio_weight": 0.05}, {"stock_code": "00002", "portfolio_weight": 0.03}]

    exposure = summarize_portfolio_shap_exposure(shap_frame, holdings)

    assert set(exposure["feature_family"]) == {"momentum", "value"}
    assert exposure["abs_weighted_shap"].sum() > 0


def test_imitation_policy_fits_expert_weights_and_predicts():
    panel = pd.DataFrame(
        [
            {"trade_date": "2025-01-01", "stock_code": "00001", "ranking_score": 90.0, "expected_transaction_cost_bps": 5.0},
            {"trade_date": "2025-01-01", "stock_code": "00002", "ranking_score": 10.0, "expected_transaction_cost_bps": 8.0},
        ]
    )
    training = build_expert_training_rows(panel, top_n=1, max_weight=0.5)
    policy = LinearImitationPolicy.fit(training, ["ranking_score", "expected_transaction_cost_bps"], max_weight=0.5)

    weights = policy.predict_weights(panel)

    assert len(weights) == 2
    assert weights.max() <= 0.5


def test_supervised_execution_cost_model_fits_tca_rows():
    rows = [
        {"participation_rate": 0.01, "impact_bps": 3.0, "commission_bps": 1.0, "implementation_shortfall_bps": 4.0},
        {"participation_rate": 0.04, "impact_bps": 7.0, "commission_bps": 1.0, "implementation_shortfall_bps": 8.0},
    ]

    model = SupervisedExecutionCostModel.fit(rows, ["participation_rate", "impact_bps", "commission_bps"])
    preds = model.predict(rows)

    assert len(preds) == 2
    assert preds[1] > preds[0]
