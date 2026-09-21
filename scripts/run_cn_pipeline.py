#!/usr/bin/env python3
"""Run the CN data/model pipeline with stage gates and coverage reports."""

from __future__ import annotations

import argparse
import json
import sys
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10; uv provides tomli in the project env.
    import tomli as tomllib
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "cn_pipeline.toml"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.ingest.service import MarketDataService


def read_config(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def coverage_report(
    service: MarketDataService,
    min_rows: int,
    adjust: str = "qfq",
    *,
    include_features: bool = True,
    feature_set: str | None = None,
) -> dict:
    """Read coverage directly from the warehouse instead of invoking run.py."""
    report = service.cn_backtest_coverage_report(
        min_ohlcv_rows=min_rows,
        adjust=adjust,
        frequency="daily",
        include_features=include_features,
        feature_set=feature_set,
    )
    # The full per-stock maps are retained in the persisted pipeline report,
    # but printing 5,000+ entries makes the interactive stage look stalled.
    console_report = dict(report)
    console_ohlcv = dict(report.get("ohlcv", {}))
    console_ohlcv.pop("row_counts", None)
    console_ohlcv.pop("latest_trade_dates", None)
    console_ohlcv.pop("excluded_stock_codes", None)
    console_report["ohlcv"] = console_ohlcv
    print(json.dumps(console_report, ensure_ascii=False, indent=2, default=str), flush=True)
    return report


def with_profile(layer: dict, profile: dict | None) -> dict:
    """Apply an account profile (capital, slots, model slots, price cap) to a selection layer."""
    if not profile:
        return layer
    layer = dict(layer)
    if profile.get("initial_capital"):
        layer["initial_capital"] = float(profile["initial_capital"])
    constraints = dict(layer.get("portfolio_constraints") or {})
    if profile.get("max_holdings"):
        constraints["max_holdings"] = int(profile["max_holdings"])
    if profile.get("sleeve_slots_max") is not None:
        constraints["sleeve_slots_max"] = int(profile["sleeve_slots_max"])
    layer["portfolio_constraints"] = constraints
    if profile.get("model_slots"):
        layer["preselection_model_slots"] = int(profile["model_slots"])
    affordability = dict(layer.get("affordability") or {})
    affordability["enabled"] = True
    affordability["equity"] = float(layer.get("initial_capital", 1_000_000.0))
    if profile.get("max_price"):
        affordability["max_price"] = profile["max_price"]
    layer["affordability"] = affordability
    return layer


def replay_dir(layer: dict, export_default: str, as_of_date, profile: str | None) -> str:
    """Route replay outputs away from the production files."""
    output_dir = layer.get("output_dir", export_default)
    suffix = str(as_of_date).replace("-", "")
    if profile:
        suffix = f"{suffix}_{profile}"
    return f"{output_dir}/replay_{suffix}"

def run_stage(name: str, config: dict, service: MarketDataService, *, force_rebalance: bool = False,
              as_of_date: str | None = None, profile: str | None = None) -> dict:
    """Execute one configured stage in-process."""
    profiles = dict(((config.get("selection") or {}).get("profiles") or {}))
    p = config["pipeline"]
    if name in {"daily_bars", "intraday_bars"}:
        layer = config[name]
        end_date = str(p["end_date"]) or None
        start_date = str(p["start_date"])
        if name == "intraday_bars" and layer.get("lookback_days") is not None:
            reference_date = datetime.fromisoformat(end_date).date() if end_date else datetime.now().date()
            start_date = (reference_date - timedelta(days=int(layer["lookback_days"]))).isoformat()
        return service.bulk_sync_cn_history(
            start_date=start_date,
            end_date=end_date,
            adjust=str(layer.get("adjust", p.get("adjust", "qfq"))),
            max_workers=int(layer.get("max_workers", p["max_workers_sync"])),
            data_source=layer.get("data_source") or None,
            skip_existing=bool(layer.get("skip_existing", True)),
            frequencies=tuple(layer["frequencies"]),
            derive_intraday_from_1min=bool(layer.get("derive_intraday_from_1min", False)),
            derive_intraday_from_base=bool(layer.get("derive_intraday_from_base", False)),
            intraday_base_frequency=layer.get("intraday_base_frequency"),
            complete_data=bool(layer.get("complete_data", False)),
            show_progress=True,
            quality_report_dir=p.get("quality_report_dir", "output/data_quality"),
        )
    if name == "fundamental":
        summary = {
            "stock_info": service.refresh_cn_stock_info(
                max_workers=int(p["max_workers_stock_info"]),
                data_source=service.data_source,
                show_progress=True,
            ),
            "valuation_history": service.refresh_cn_baidu_valuation_history(
                start_date=str(p["start_date"]),
                end_date=str(p["end_date"]) or None,
                max_workers=int(p["max_workers_valuation"]),
                period=str(p.get("valuation_history_period", "全部")),
                show_progress=True,
            ),
        }
        relay_data = config.get("relay_data", {})
        if bool(relay_data.get("enabled", False)):
            summary["relay_daily_basic"] = service.refresh_cn_tushare_daily_basic(
                start_date=relay_data.get("start_date") or p["start_date"],
                end_date=relay_data.get("end_date") or p.get("end_date") or None,
                max_workers=int(p["max_workers_valuation"]),
                show_progress=True,
            )
        else:
            summary["relay_daily_basic"] = {"skipped": True, "reason": "disabled by relay_data.enabled"}
        if bool(relay_data.get("adjustment_factors_enabled", False)):
            summary["relay_adjustment_factors"] = service.refresh_cn_tushare_adjustment_factors(
                start_date=relay_data.get("start_date") or p["start_date"],
                end_date=relay_data.get("end_date") or p.get("end_date") or None,
                max_workers=int(p["max_workers_valuation"]),
                show_progress=True,
            )
        else:
            summary["relay_adjustment_factors"] = {"skipped": True, "reason": "disabled by relay_data.adjustment_factors_enabled"}
        if bool(p.get("financial_metrics_enabled", False)):
            summary["financial_metrics"] = service.refresh_cn_financial_metrics(
                max_workers=int(p["max_workers_financial"]),
                lookback_quarters=int(p.get("financial_lookback_quarters", 1)),
                show_progress=True,
            )
        else:
            summary["financial_metrics"] = {
                "skipped": True,
                "reason": "disabled by pipeline.financial_metrics_enabled",
            }
        industry = config.get("industry", {})
        summary["industry"] = service.backfill_cn_industry(
            show_progress=True,
            taxonomy=industry.get("taxonomy", "sw2021"),
            min_coverage=float(industry.get("min_coverage", 0.95)),
        )
        if summary["industry"].get("status") != "completed":
            raise RuntimeError(
                f"CN industry coverage gate failed: {summary['industry'].get('coverage', 0):.1%} "
                f"< {summary['industry'].get('min_coverage', 0):.1%}"
            )
        return summary
    if name == "moneyflow":
        layer = config.get("moneyflow", {})
        moneyflow_end = layer.get("end_date") or p.get("end_date") or None
        moneyflow_start = layer.get("start_date") or None
        if not moneyflow_start and layer.get("lookback_days"):
            reference = datetime.fromisoformat(str(moneyflow_end)).date() if moneyflow_end else datetime.now().date()
            moneyflow_start = (reference - timedelta(days=int(layer["lookback_days"]))).isoformat()
        base_summary = service.refresh_cn_moneyflow(
            start_date=moneyflow_start or p.get("start_date"), end_date=moneyflow_end,
            max_workers=int(layer.get("max_workers", 4)), batch_years=int(layer.get("batch_years", 1)),
            fetch_standard=bool(layer.get("fetch_standard", True)), fetch_dc=bool(layer.get("fetch_dc", True)),
            fetch_ths=bool(layer.get("fetch_ths", True)),
            fetch_top_list=bool(layer.get("fetch_top_list", False)),
            fetch_top_inst=bool(layer.get("fetch_top_inst", False)),
            show_progress=True, require_daily_bar_match=bool(layer.get("require_daily_bar_match", True)),
            min_match_ratio=float(layer.get("min_match_ratio", 0.98)), raw_dir=layer.get("raw_dir"),
            feature_path=layer.get("feature_path"),
            fetch_mode=layer.get("fetch_mode", "trade_date"),
        )
        aux_enabled = any(bool(layer.get(key, False)) for key in ("fetch_daily_basic", "fetch_cyq_perf", "fetch_cyq_chips", "fetch_hm_detail"))
        if not aux_enabled:
            return base_summary
        aux_summary = service.refresh_cn_moneyflow_aux(
            start_date=moneyflow_start or p.get("start_date"),
            end_date=moneyflow_end,
            max_workers=int(layer.get("max_workers", 4)),
            fetch_daily_basic=bool(layer.get("fetch_daily_basic", False)),
            fetch_cyq_perf=bool(layer.get("fetch_cyq_perf", False)),
            fetch_cyq_chips=bool(layer.get("fetch_cyq_chips", False)),
            fetch_hm_detail=bool(layer.get("fetch_hm_detail", False)),
            cyq_chips_stock_limit=int(layer.get("cyq_chips_stock_limit", 0) or 0),
            cyq_chips_max_workers=int(layer.get("cyq_chips_max_workers", 2) or 2),
            cyq_chips_read_timeout=float(layer.get("cyq_chips_read_timeout", 8) or 8),
            cyq_chips_requests_per_minute=int(layer.get("cyq_chips_requests_per_minute", 180) or 180),
            cyq_chips_base_requests_per_minute=int(layer.get("cyq_chips_base_requests_per_minute", 180) or 180),
            cyq_chips_promax_requests_per_minute=int(layer.get("cyq_chips_promax_requests_per_minute", 180) or 180),
            raw_dir=layer.get("raw_dir"),
            fetch_mode=layer.get("fetch_mode", "trade_date"),
            show_progress=True,
        )
        base_summary["auxiliary_sources"] = aux_summary
        return base_summary
    if name == "cyq_chips":
        layer = config.get("moneyflow", {})
        moneyflow_end = layer.get("end_date") or p.get("end_date") or None
        moneyflow_start = layer.get("start_date") or None
        if not moneyflow_start and layer.get("lookback_days"):
            reference = datetime.fromisoformat(str(moneyflow_end)).date() if moneyflow_end else datetime.now().date()
            moneyflow_start = (reference - timedelta(days=int(layer["lookback_days"]))).isoformat()
        return service.refresh_cn_moneyflow_aux(
            start_date=moneyflow_start or p.get("start_date"), end_date=moneyflow_end,
            max_workers=int(layer.get("max_workers", 4)),
            fetch_cyq_chips=True,
            cyq_chips_stock_limit=int(layer.get("cyq_chips_stock_limit", 0) or 0),
            cyq_chips_max_workers=int(layer.get("cyq_chips_max_workers", 2) or 2),
            cyq_chips_read_timeout=float(layer.get("cyq_chips_read_timeout", 8) or 8),
            cyq_chips_requests_per_minute=int(layer.get("cyq_chips_requests_per_minute", 180) or 180),
            cyq_chips_base_requests_per_minute=int(layer.get("cyq_chips_base_requests_per_minute", 180) or 180),
            cyq_chips_promax_requests_per_minute=int(layer.get("cyq_chips_promax_requests_per_minute", 180) or 180),
            raw_dir=layer.get("raw_dir"), fetch_mode=layer.get("fetch_mode", "trade_date"), show_progress=True,
        )
    if name == "alternative":
        layer = config[name]
        return service.import_cn_alternative_evidence(
            input_path=layer["input_path"], output_dir=layer.get("output_dir", "output/alternative_data"),
            source=layer.get("source", "manual_import"),
        )
    if name == "strategy_labels":
        layer = config[name]
        label_keys = {
            "entry_delay", "path_horizon", "startup_low_window", "startup_min_rise",
            "startup_max_rise", "startup_max_return_60d", "startup_max_high_distance",
            "take_profit", "stop_loss", "limit_up_threshold",
        }
        label_kwargs = {key: layer[key] for key in label_keys if key in layer}
        return service.build_cn_strategy_labels(
            days=int(layer.get("days", 756)), output_dir=layer.get("output_dir", "output/strategy_labels"),
            **label_kwargs,
        )
    if name == "features":
        stock_codes = service.get_all_stock_codes(
            market=p["market"], asset_type="equity", frequency="daily", adjust=p.get("adjust", "qfq")
        )
        return service.generate_factor_set(
            stock_codes=stock_codes,
            factor_set=p["factor_set"],
            market=p["market"],
            frequency="daily",
            adjust=p.get("adjust", "qfq"),
            days=int(p["days"]),
            max_workers=int(p["max_workers_factors"]),
            show_progress=True,
            config=config.get("factor_config") or None,
        )
    if name == "regime":
        layer = config[name]
        return service.build_cn_market_regime(
            days=int(layer.get("days", 756)), end_date=str(p["end_date"]) or None,
            min_stocks=int(layer.get("min_stocks", 20)), trend_window=int(layer.get("trend_window", 60)),
            breadth_window=int(layer.get("breadth_window", 20)),
            volatility_window=int(layer.get("volatility_window", 20)),
            hysteresis_days=int(layer.get("hysteresis_days", 3)), version=str(layer.get("version", "regime.v1")),
            output_dir=layer.get("output_dir", "output/regime"),
        )
    if name == "exits":
        layer = config.get(name, {})
        return service.plan_cn_exit_rules(
            holdings_path=layer.get("holdings_path", "config/holdings_cn.csv"),
            market=p["market"], adjust=p.get("adjust", "qfq"),
            days=int(layer.get("days", 260)),
            model_scores_dir=layer.get("model_scores_dir", "output/model_scores"),
            model=layer.get("model", config.get("selection", {}).get("model", "ensemble")),
            ensemble_weights=layer.get("ensemble_weights") or config.get("selection", {}).get("ensemble_weights") or None,
            output_dir=layer.get("output_dir", p.get("export_csv", "output/results_cn")),
            rules_config=layer.get("rules") or None,
            cash=layer.get("cash"),
            show_progress=True,
        )
    if name == "paper_outcomes":
        layer = with_profile(config[name], profiles.get(profile) if profile else None)
        selection_path = layer.get("selection_path", "output/results_cn/cn_ensemble_selected.csv")
        if as_of_date:
            replay = replay_dir(config.get("selection", {}), "output/results_cn", as_of_date, profile)
            candidate = Path(replay) / "cn_ensemble_selected.csv"
            if candidate.is_file():
                selection_path = str(candidate)
                layer["output_dir"] = f"{layer.get('output_dir', 'output/paper_trading')}/replay_{str(as_of_date).replace('-', '')}_{profile or 'default'}"
                layer["output_dir"] = f"{layer.get('output_dir', 'output/paper_trading')}/replay_{str(as_of_date).replace('-', '')}_{profile or 'default'}"
        return service.evaluate_cn_paper_outcomes(
            selection_path=selection_path,
            days=int(layer.get("days", p["days"])),
            horizons=tuple(layer.get("horizons", [1, 5, 20, 60])),
            cost_bps=float(layer.get("cost_bps", 10.0)), benchmark_path=layer.get("benchmark_path") or None,
            output_dir=layer.get("output_dir", "output/paper_trading"),
        )
    if name == "paper_account":
        layer = with_profile(config[name], profiles.get(profile) if profile else None)
        selection_path = layer.get("selection_path", "output/results_cn/cn_ensemble_selected.csv")
        if as_of_date:
            replay = replay_dir(config.get("selection", {}), "output/results_cn", as_of_date, profile)
            candidate = Path(replay) / "cn_ensemble_selected.csv"
            if candidate.is_file():
                selection_path = str(candidate)
        return service.run_cn_paper_account(
            selection_path=selection_path, days=int(layer.get("days", 756)),
            account_id=layer.get("account_id", "cn_default"), strategy_version=layer.get("strategy_version", "v1"),
            initial_capital=float(layer.get("initial_capital", 1_000_000.0)), commission_bps=float(layer.get("commission_bps", 5.0)),
            slippage_bps=float(layer.get("slippage_bps", 5.0)), lot_size=int(layer.get("lot_size", 100)), output_dir=layer.get("output_dir", "output/paper_trading"),
        )
    if name == "graph_temporal":
        layer = config[name]
        return service.train_cn_graph_temporal(
            factor_set=p["factor_set"], days=int(layer.get("days", p["days"])), lookback=int(layer.get("lookback", 20)),
            epochs=int(layer.get("epochs", 5)), model_dir=layer.get("model_dir", "output/models/cn/graph_temporal/alpha_zoo_hk"),
            cleaning_version=layer.get("cleaning_version", "p0.2.v1"), end_date=str(p["end_date"]) or None,
        )
    if name == "model_comparison":
        layer = config[name]
        return service.evaluate_cn_model_comparison(
            prediction_paths=layer.get("prediction_paths", {}), output_dir=layer.get("output_dir", "output/evaluations"),
            prefix=layer.get("prefix", "cn_model_comparison"), target_col=layer.get("target_col", "forward_return_20d"),
            n_splits=int(layer.get("n_splits", 5)), min_train_days=int(layer.get("min_train_days", 120)),
            test_days=layer.get("test_days"), purge_days=int(layer.get("purge_days", 20)),
            # A path label needs a 60-session *purge* unless configured
            # otherwise.  An explicit zero embargo must remain zero: applying
            # 60 here skips most of the already-scarce OOS blocks.
            embargo_days=int(layer.get("embargo_days", 0)),
            label_path=layer.get("label_path") or None,
            top_k=layer.get("top_k"),
            horizon_days=layer.get("horizon_days"),
            overlap_correction=str(layer.get("overlap_correction", "split_1_over_h")),
            commission_bps=float(layer.get("commission_bps", 5.0)),
            slippage_bps=float(layer.get("slippage_bps", 5.0)),
            stamp_duty_bps=float(layer.get("stamp_duty_bps", 5.0)),
            cost_bps=layer.get("cost_bps"),
            industry_column=str(layer.get("industry_column", "industry_l1")),
        )
    if name == "oos_predictions":
        layer = config[name]
        meta = dict(config.get("meta_labeling") or {})
        feature_quality = config.get("model_features", {})
        neutralization = dict(config.get("neutralization") or {})
        meta_min_probability = meta.get("min_probability")
        if meta_min_probability is not None and float(meta_min_probability) <= 0.0:
            meta_min_probability = None
        meta_features = [str(value) for value in (meta.get("features") or [])] or None
        return service.generate_cn_oos_predictions(
            models=tuple(layer.get("models", ["lightgbm"])), factor_set=p["factor_set"],
            days=int(layer.get("days", 756)), label_horizon=int(layer.get("label_horizon", 20)),
            label_mode=layer.get("label_mode", "forward_return"), startup_only=bool(layer.get("startup_only", False)),
            label_path_horizon=int(layer.get("path_horizon", 60)),
            preserve_startup_context=bool(layer.get("preserve_startup_context", False)),
            cleaning_version=layer.get("cleaning_version", "p0.2.v1"), output_dir=layer.get("output_dir", "output/oos_predictions"),
            n_splits=int(layer.get("n_splits", 5)), min_train_days=int(layer.get("min_train_days", 120)),
            test_days=layer.get("test_days"), purge_days=int(layer.get("purge_days", 20)),
            embargo_days=int(layer.get("embargo_days", 0)),
            transformer_lookback=int(layer.get("transformer_lookback", 60)), transformer_epochs=int(layer.get("transformer_epochs", 5)),
            transformer_batch_size=int(layer.get("transformer_batch_size", 256)), transformer_max_samples=int(layer.get("transformer_max_samples", 200000)),
            transformer_device=layer.get("transformer_device", "auto"), industry_mapping_path=layer.get("industry_mapping_path") or None,
            transformer_max_feature_pairs=int(layer.get("transformer_max_feature_pairs", 128)),
            transformer_protected_features=tuple(layer.get("protected_features", [])),
            transformer_seeds=[int(seed) for seed in (layer.get("transformer_seeds") or [0])],
            transformer_checkpoint_metric=str(layer.get("checkpoint_metric", "ic")),
            transformer_seed_ensemble=str(layer.get("seed_ensemble", "average")),
            prediction_stride=int(layer.get("prediction_stride", 1)),
            min_feature_coverage=float(feature_quality.get("min_feature_coverage", 0.05)),
            drop_constant_features=bool(feature_quality.get("drop_constant_features", True)),
            end_date=str(p["end_date"]) or None,
            show_progress=True,
            meta_labeling=bool(meta.get("enabled", False)) or bool(layer.get("meta_labeling", False)),
            meta_features=meta_features,
            meta_label_column=str(meta.get("label_column", "label_tb_class")),
            meta_candidate_quantile=float(meta.get("candidate_quantile", 0.10)),
            meta_act_quantile=meta.get("act_quantile", 0.50),
            meta_min_probability=meta_min_probability,
            meta_inner_share=float(meta.get("inner_share", 0.30)),
            meta_inner_purge_days=int(meta.get("inner_purge_days", 20)),
            meta_validation_share=float(meta.get("validation_share", 0.30)),
            meta_purge_days=int(meta.get("purge_days", 20)),
            meta_n_estimators=int(meta.get("n_estimators", 300)),
            meta_learning_rate=float(meta.get("learning_rate", 0.05)),
            meta_num_leaves=int(meta.get("num_leaves", 31)),
            meta_max_depth=int(meta.get("max_depth", 5)),
            meta_min_child_samples=int(meta.get("min_child_samples", 50)),
            meta_reg_lambda=float(meta.get("reg_lambda", 10.0)),
            meta_random_state=int(meta.get("random_state", 42)),
            meta_gate_mode=str(meta.get("gate_mode", "learned")),
            meta_rule_column=str(meta.get("rule_column", "dist_from_120d_low")),
            meta_rule_threshold=float(meta.get("rule_threshold", 0.15)),
            meta_rule_direction=str(meta.get("rule_direction", "le")),
            meta_top_k=int(meta.get("top_k", 20)),
            meta_evaluation_dir=str(meta.get("evaluation_dir", "output/evaluations")),
            meta_evaluation_prefix=str(meta.get("evaluation_prefix") or "") or None,
            meta_realized_return_column=str(meta.get("realized_return_column", "forward_excess_return_20d")),
            meta_commission_bps=float(meta.get("commission_bps", 5.0)),
            meta_slippage_bps=float(meta.get("slippage_bps", 5.0)),
            meta_stamp_duty_bps=float(meta.get("stamp_duty_bps", 5.0)),
            feature_profile=str(feature_quality.get("profile", "full")),
            feature_include_patterns=[str(value) for value in (feature_quality.get("include_features") or [])],
            feature_exclude_patterns=[str(value) for value in (feature_quality.get("exclude_features") or [])],
            feature_include_families=[str(value) for value in (feature_quality.get("include_families") or [])],
            feature_exclude_families=[str(value) for value in (feature_quality.get("exclude_families") or [])],
            neutralization=neutralization or None,
        )
    if name == "clean_panel":
        layer = config[name]
        return service.materialize_clean_feature_panel(
            market=p["market"], factor_set=p["factor_set"], adjust=p.get("adjust", "qfq"),
            days=int(layer.get("days", p["days"])),
            end_date=str(p["end_date"]) or None,
            cleaning_version=layer.get("cleaning_version", "p0.2.v1"),
            report_dir=p.get("quality_report_dir", "output/data_quality"),
            feature_batch_size=int(layer.get("feature_batch_size", 10)),
            factor_config=config.get("factor_config") or None,
            moneyflow_path=(config.get("moneyflow") or {}).get("feature_path"),
            show_progress=True,
        )
    if name == "lightgbm":
        layer = config[name]
        feature_quality = config.get("model_features", {})
        return service.train_lightgbm_clean_panel(
            market=p["market"], factor_set=p["factor_set"], adjust=p.get("adjust", "qfq"),
            days=int(layer.get("days", p["days"])),
            end_date=str(p["end_date"]) or None,
            label_horizon=int(layer.get("label_horizon", 20)),
            label_mode=layer.get("label_mode", "forward_return"), startup_only=bool(layer.get("startup_only", False)),
            label_path_horizon=int(layer.get("path_horizon", 60)),
            preserve_startup_context=bool(layer.get("preserve_startup_context", True)),
            validation_days=int(layer.get("validation_days", 60)),
            cleaning_version=layer.get("cleaning_version", "p0.2.v1"),
            model_dir=layer.get("model_dir"), warm_start_path=layer.get("warm_start_path"),
            min_stock_count=int(p["min_training_stocks"]),
            embargo_days=(60 if str(layer.get("label_mode", "forward_return")).startswith("path") and not int(layer.get("embargo_days", 0) or 0) else int(layer.get("embargo_days", layer.get("label_horizon", 20)))),
            min_feature_coverage=float(feature_quality.get("min_feature_coverage", 0.05)),
            drop_constant_features=bool(feature_quality.get("drop_constant_features", True)),
            n_estimators=int(layer.get("n_estimators", 500)),
            learning_rate=float(layer.get("learning_rate", 0.05)),
            num_leaves=int(layer.get("num_leaves", 64)),
            max_depth=int(layer.get("max_depth", 8)),
            min_child_samples=int(layer.get("min_child_samples", 30)),
            reg_lambda=float(layer.get("reg_lambda", 10.0)),
            early_stopping_rounds=int(layer.get("early_stopping_rounds", 0)),
            min_trees=int(layer.get("min_trees", 100)),
            eval_metric=str(layer.get("eval_metric", "daily_ic")),
            show_progress=True,
        )
    if name == "transformer":
        layer = config[name]
        feature_quality = config.get("model_features", {})
        return service.train_transformer_clean_panel(
            market=p["market"], factor_set=p["factor_set"], adjust=p.get("adjust", "qfq"),
            days=int(layer.get("days", p["days"])),
            end_date=str(p["end_date"]) or None,
            label_horizon=int(layer.get("label_horizon", 20)),
            label_mode=layer.get("label_mode", "forward_return"), startup_only=bool(layer.get("startup_only", False)),
            label_path_horizon=int(layer.get("path_horizon", 60)),
            preserve_startup_context=bool(layer.get("preserve_startup_context", True)),
            validation_days=int(layer.get("validation_days", 60)),
            lookback=int(layer.get("lookback", 60)), epochs=int(layer.get("epochs", 10)),
            batch_size=int(layer.get("batch_size", 256)), max_samples=int(layer.get("max_samples", 200000)),
            max_feature_pairs=int(layer.get("max_feature_pairs", 128)),
            protected_features=tuple(layer.get("protected_features", [])),
            learning_rate=float(layer.get("learning_rate", 1e-3)),
            d_model=int(layer.get("d_model", 64)), nhead=int(layer.get("nhead", 4)),
            num_layers=int(layer.get("num_layers", 2)),
            seeds=[int(seed) for seed in (layer.get("seeds") or [0])],
            checkpoint_metric=str(layer.get("checkpoint_metric", "ic")),
            seed_ensemble=str(layer.get("seed_ensemble", "average")),
            cleaning_version=layer.get("cleaning_version", "p0.2.v1"), model_dir=layer.get("model_dir"),
            min_stock_count=int(p["min_training_stocks"]),
            warm_start_path=layer.get("warm_start_path"), warm_start_manifest_path=layer.get("warm_start_manifest_path"),
            device=layer.get("device", "auto"),
            embargo_days=(60 if str(layer.get("label_mode", "forward_return")).startswith("path") and not int(layer.get("embargo_days", 0) or 0) else int(layer.get("embargo_days", layer.get("label_horizon", 20)))),
            min_feature_coverage=float(feature_quality.get("min_feature_coverage", 0.05)),
            drop_constant_features=bool(feature_quality.get("drop_constant_features", True)),
            show_progress=True,
        )
    if name == "cnn":
        layer = config[name]
        feature_quality = config.get("model_features", {})
        return service.train_cnn_clean_panel(
            market=p["market"], factor_set=p["factor_set"], adjust=p.get("adjust", "qfq"),
            days=int(layer.get("days", p["days"])), end_date=str(p["end_date"]) or None,
            label_horizon=int(layer.get("label_horizon", 20)), validation_days=int(layer.get("validation_days", 60)),
            lookback=int(layer.get("lookback", 60)), epochs=int(layer.get("epochs", 10)),
            batch_size=int(layer.get("batch_size", 256)), max_samples=int(layer.get("max_samples", 200000)),
            max_feature_pairs=int(layer.get("max_feature_pairs", 128)),
            protected_features=tuple(layer.get("protected_features", [])),
            channels=int(layer.get("channels", 64)), kernel_size=int(layer.get("kernel_size", 3)),
            num_layers=int(layer.get("num_layers", 3)), cleaning_version=layer.get("cleaning_version", "p0.2.v1"),
            model_dir=layer.get("model_dir"), min_stock_count=int(p["min_training_stocks"]),
            device=layer.get("device", "auto"), embargo_days=int(layer.get("embargo_days", layer.get("label_horizon", 20))),
            min_feature_coverage=float(feature_quality.get("min_feature_coverage", 0.05)),
            drop_constant_features=bool(feature_quality.get("drop_constant_features", True)),
            show_progress=True,
        )
    if name == "model_scores":
        layer = config[name]
        return service.score_clean_feature_panel_models(
            market=p["market"], factor_set=p["factor_set"], adjust=p.get("adjust", "qfq"),
            days=int(layer.get("days", p["days"])), end_date=str(p["end_date"]) or None,
            cleaning_version=layer.get("cleaning_version", "p0.2.v1"),
            lightgbm_model_path=layer.get("lightgbm_model_path"), lightgbm_manifest_path=layer.get("lightgbm_manifest_path"),
            transformer_model_path=layer.get("transformer_model_path"), transformer_manifest_path=layer.get("transformer_manifest_path"),
            transformer_device=layer.get("transformer_device", "auto"),
            cnn_model_path=layer.get("cnn_model_path"), cnn_manifest_path=layer.get("cnn_manifest_path"),
            cnn_device=layer.get("cnn_device", "auto"),
            output_dir=layer.get("output_dir", "output/model_scores"),
            min_cross_section_coverage=float(layer.get("min_cross_section_coverage", 0.95)),
            show_progress=True,
        )
    if name == "selection":
        layer = with_profile(config[name], profiles.get(profile) if profile else None)
        output_dir = layer.get("output_dir", p["export_csv"])
        if as_of_date:
            output_dir = replay_dir(layer, p["export_csv"], as_of_date, profile)
        return service.select_persisted_model_scores(
            model_scores_dir=layer.get("model_scores_dir", "output/model_scores"),
            output_dir=output_dir,
            model=layer.get("model", "ensemble"), top_n=int(layer.get("top_n", p["top_n"])),
            portfolio_mode=layer.get("portfolio_mode", "topn"), portfolio_constraints=layer.get("portfolio_constraints") or None,
            initial_capital=float(layer.get("initial_capital", 1_000_000.0)),
            signal_config=layer.get("signals") or None,
            ensemble_weights=layer.get("ensemble_weights") or None,
            affordability=layer.get("affordability") or None,
            rebalance_stride_days=int(layer.get("rebalance_stride_days", 1) or 1),
            force_rebalance=force_rebalance,
            show_progress=True,
            as_of_date=as_of_date,
            startup_gate=layer.get("startup_gate") or None,
        )
    if name == "preselection":
        layer = with_profile(config.get("selection", {}), profiles.get(profile) if profile else None)
        output_dir = layer.get("output_dir", p["export_csv"])
        if as_of_date:
            output_dir = replay_dir(layer, p["export_csv"], as_of_date, profile)
        return service.select_persisted_model_scores(
            model_scores_dir=layer.get("model_scores_dir", "output/model_scores"),
            output_dir=output_dir,
            model=layer.get("model", "ensemble"), top_n=int(layer.get("preselection_model_slots", p.get("preselection_model_slots", 4))),
            portfolio_mode="topn", portfolio_constraints=layer.get("portfolio_constraints") or None,
            initial_capital=float(layer.get("initial_capital", 1_000_000.0)),
            signal_config=layer.get("signals") or None,
            ensemble_weights=layer.get("ensemble_weights") or None,
            affordability=layer.get("affordability") or None,
            rebalance_stride_days=int(layer.get("rebalance_stride_days", 1) or 1),
            force_rebalance=force_rebalance, show_progress=True, preselection_only=True,
            as_of_date=as_of_date, startup_gate=layer.get("startup_gate") or None,
        )
    if name == "pk":
        layer = with_profile(config.get("selection", {}), profiles.get(profile) if profile else None)
        # risk-control settings travel with the sizing constraints
        if layer.get("risk_control"):
            constraints = dict(layer.get("portfolio_constraints") or {})
            constraints.update(dict(layer.get("risk_control") or {}))
            layer["portfolio_constraints"] = constraints
        output_dir = layer.get("output_dir", p["export_csv"])
        if as_of_date:
            output_dir = replay_dir(layer, p["export_csv"], as_of_date, profile)
        preselected_path = layer.get("preselection_path", "output/results_cn/cn_ensemble_preselected.csv")
        if as_of_date:
            preselected_path = f"{output_dir}/cn_ensemble_preselected.csv"
        # Re-run the same optimizer on the preselection pool.  Signal floors
        # and max-holdings constraints are applied only at this final PK step.
        return service.select_persisted_model_scores(
            model_scores_dir=layer.get("model_scores_dir", "output/model_scores"),
            output_dir=output_dir,
            model=layer.get("model", "ensemble"), top_n=int(layer.get("preselection_model_slots", p.get("preselection_model_slots", 4))),
            portfolio_mode=layer.get("portfolio_mode", "mean_variance_cost_aware"),
            portfolio_constraints=layer.get("portfolio_constraints") or None,
            initial_capital=float(layer.get("initial_capital", 1_000_000.0)),
            signal_config=layer.get("signals") or None,
            ensemble_weights=layer.get("ensemble_weights") or None,
            affordability=layer.get("affordability") or None,
            rebalance_stride_days=1, force_rebalance=True, show_progress=True,
            candidate_path=str(Path(preselected_path).resolve()),
            as_of_date=as_of_date,
        )
    return {}


def fundamental_availability(report: dict, minimum: float) -> str:
    total = int(report.get("stock_count", 0))
    if total == 0:
        return "CN universe is empty"
    info = int(report.get("stock_info", {}).get("row_count", 0)) / total
    valuation = int(report.get("financial", {}).get("valuation_stock_count", 0)) / total
    financial = int(report.get("financial", {}).get("financial_stock_count", 0)) / total
    industry = int(report.get("industry", {}).get("industry_l1_count", 0)) / total
    ratios = {"stock_info": info, "valuation": valuation, "financial": financial, "industry_l1": industry}
    below = ", ".join(f"{key}={value:.1%}" for key, value in ratios.items() if value < minimum)
    detail = f"target={minimum:.1%}; " + ", ".join(f"{key}={value:.1%}" for key, value in ratios.items())
    return detail if not below else detail + f"; optional fields below target: {below}"


def enough_ohlcv_samples(report: dict, minimum: int) -> tuple[bool, str]:
    count = int(report.get("ohlcv", {}).get("covered_stock_count", 0))
    return count >= minimum, f"eligible OHLCV stocks={count}; minimum={minimum}"


def enough_feature_samples(report: dict, minimum: int) -> tuple[bool, str]:
    count = int(report.get("features", {}).get("stock_count", 0))
    return count >= minimum, f"eligible feature stocks={count}; minimum={minimum}"


def should_run_stage(stage: str, requested_stage: str, config: dict) -> bool:
    """Respect default profile flags only for an all-stage run."""
    return requested_stage != "all" or bool(config.get("stages", {}).get(stage, True))


def write_report(report: dict, report_dir: Path) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = report_dir / f"cn_pipeline_{stamp}.json"
    md_path = report_dir / f"cn_pipeline_{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    lines = [f"# CN Pipeline Report ({report['started_at']})", "", f"Result: **{report['result']}**", "", "| Stage | Status | Detail |", "|---|---|---|"]
    for item in report["stages"]:
        summary = item.get("summary") or {}
        detail = item.get("detail", "")
        if summary.get("failed_count"):
            detail = f"{detail} failed={summary['failed_count']}"
        if item.get("name") == "fundamental":
            failed_parts = []
            for sub_name, sub_result in summary.items():
                if isinstance(sub_result, dict) and sub_result.get("failed_count"):
                    failed_parts.append(f"{sub_name}={sub_result['failed_count']}")
            if failed_parts:
                detail = f"{detail} failures: {', '.join(failed_parts)}"
        if item.get("name") == "selection":
            signals = summary.get("signals") or {}
            if signals.get("enabled"):
                forced = ", ".join(signals.get("forced_codes") or []) or "none"
                detail = (
                    f"signals recipes={','.join(signals.get('recipes') or [])} "
                    f"hits={signals.get('hit_count')} forced={forced} "
                    f"min_weight={signals.get('forced_min_weight')}"
                )
        if item.get("name") == "model_comparison":
            comparison = summary.get("comparison", {})
            rankings = comparison.get("ranking", [])
            if rankings:
                winner = rankings[0]
                detail = (
                    f"winner={winner.get('model')} RankIC={winner.get('rank_ic_mean')} "
                    f"IR={winner.get('rank_ic_ir')} common_rows={comparison.get('common_universe_rows')}"
                )
        lines.append(f"| {item['name']} | {item['status']} | {detail} |")
        if summary.get("failed"):
            for failure in summary["failed"][:10]:
                lines.append(f"|  |  | `{failure.get('code', '')}`: {str(failure.get('error', ''))[:180]} |")
    lines.extend(["", f"JSON: `{json_path}`"])
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run CN data, feature, model, OOS evaluation and selection stages.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="TOML pipeline configuration")
    parser.add_argument("--stage", choices=["all", "daily_bars", "moneyflow", "cyq_chips", "intraday_bars", "fundamental", "alternative", "strategy_labels", "features", "regime", "clean_panel", "lightgbm", "transformer", "cnn", "model_scores", "preselection", "selection", "pk", "paper_outcomes", "paper_account", "exits", "graph_temporal", "oos_predictions", "model_comparison"], default="all")
    parser.add_argument("--report-dir", type=Path, default=ROOT / "output" / "pipeline_reports")
    parser.add_argument("--force-rebalance", action="store_true", help="Ignore rebalance_stride_days and re-select now")
    parser.add_argument("--trade-date", default=None,
                        help="Replay a historical decision date (YYYY-MM-DD): model cross section, regime row "
                             "and price windows are taken as of that date and outputs go to "
                             "<selection.output_dir>/replay_<date>/")
    parser.add_argument("--profile", default=None,
                        help="Account profile from [selection.profiles.<name>] (capital, holdings, model slots, price cap)")
    parser.add_argument("--continue-on-error", action="store_true", help="Continue independent data stages after a stage failure")
    args = parser.parse_args()
    config = read_config(args.config if args.config.is_absolute() else ROOT / args.config)
    started = datetime.now().isoformat(timespec="seconds")
    result = {"started_at": started, "config": str(args.config), "stages": [], "result": "failed"}
    stages = ["daily_bars", "moneyflow", "intraday_bars", "fundamental", "alternative", "strategy_labels", "features", "regime", "clean_panel", "lightgbm", "transformer", "cnn", "model_scores", "preselection", "pk", "paper_outcomes", "paper_account", "exits", "graph_temporal", "oos_predictions", "model_comparison"] if args.stage == "all" else [args.stage]
    last_coverage: dict | None = None
    blocked = False
    pipeline_config = config["pipeline"]
    service = MarketDataService(
        base_dir=pipeline_config.get("base_dir", "./assets/data"),
        data_source=pipeline_config.get("data_source", "akshare"),
    )
    try:
        for stage in stages:
            # A stage explicitly selected on the command line is an operator
            # request and must run even when it is disabled in the default all-stage profile.
            if not should_run_stage(stage, args.stage, config):
                result["stages"].append({"name": stage, "status": "skipped", "detail": "disabled in config"})
                continue
            if stage == "alternative" and not config.get("alternative", {}).get("input_path"):
                result["stages"].append({"name": stage, "status": "skipped", "detail": "set alternative.input_path to a local PIT evidence CSV"})
                continue
            if stage in {"features", "clean_panel", "lightgbm", "transformer", "cnn", "model_scores", "preselection", "pk", "selection", "oos_predictions"}:
                if last_coverage is None:
                    try:
                        print("[PIPELINE] checking CN coverage (aggregated, no full feature load)...", flush=True)
                        coverage_kwargs = {"include_features": stage not in {"features", "model_scores"}}
                        if stage not in {"features", "model_scores"}:
                            coverage_kwargs["feature_set"] = pipeline_config.get("factor_set")
                        last_coverage = coverage_report(
                            service,
                            int(pipeline_config["min_ohlcv_rows"]),
                            pipeline_config.get("adjust", "qfq"),
                            **coverage_kwargs,
                        )
                    except Exception as exc:
                        blocked = True
                        result["stages"].append({"name": stage, "status": "blocked", "detail": f"CN coverage report failed: {exc}"})
                        if not args.continue_on_error:
                            break
                        continue
                minimum_stocks = int(pipeline_config["min_training_stocks"])
                ready, detail = enough_ohlcv_samples(last_coverage, minimum_stocks)
                if not ready:
                    blocked = True
                    result["stages"].append({"name": stage, "status": "blocked", "detail": detail})
                    if not args.continue_on_error:
                        break
                    continue
                availability = fundamental_availability(last_coverage, float(pipeline_config["min_fundamental_coverage"]))
                if stage in {"clean_panel", "lightgbm", "transformer", "cnn", "preselection", "pk", "selection", "oos_predictions"}:
                    ready, detail = enough_feature_samples(last_coverage, minimum_stocks)
                    if not ready:
                        blocked = True
                        result["stages"].append({"name": stage, "status": "blocked", "detail": detail})
                        if not args.continue_on_error:
                            break
                        continue
                result["stages"].append({"name": f"{stage}_availability", "status": "info", "detail": availability})

            print(f"\n[PIPELINE] stage={stage} (in-process)", flush=True)
            try:
                summary = run_stage(stage, config, service, force_rebalance=bool(args.force_rebalance),
                                    as_of_date=args.trade_date, profile=args.profile)
                stage_status = "ok"
                if stage == "moneyflow" and isinstance(summary, dict):
                    # Preserve degraded data-quality state at the orchestration
                    # layer; a completed function call is not the same as a
                    # complete, model-safe dataset.
                    stage_status = str(summary.get("status") or "ok")
                item = {"name": stage, "status": stage_status, "summary": summary}
                if stage == "model_comparison":
                    comparison = summary.get("comparison", {}) if isinstance(summary, dict) else {}
                    common_rows = comparison.get("common_universe_rows")
                    if common_rows is not None:
                        print(f"[MODEL_COMPARISON] common_universe_rows={common_rows:,}", flush=True)
                    for model in comparison.get("models", []):
                        print(
                            f"[MODEL_COMPARISON] model={model.get('model')} "
                            f"RankIC={model.get('rank_ic_mean')} RankIC_IR={model.get('rank_ic_ir')} "
                            f"IC={model.get('ic_mean')} IC_IR={model.get('ic_ir')} "
                            f"TopQMean={model.get('top_quantile_return_mean')} "
                            f"turnover={model.get('turnover_mean')}",
                            flush=True,
                        )
                    print(f"[MODEL_COMPARISON] report={summary.get('markdown')}", flush=True)
            except Exception as exc:
                blocked = True
                print(f"[ERROR] stage={stage}: {exc}", flush=True)
                item = {"name": stage, "status": "failed", "error": str(exc)}
            result["stages"].append(item)
            if item["status"] == "failed" and not args.continue_on_error:
                break
            try:
                include_features = stage != "features"
                print("[PIPELINE] refreshing CN coverage summary...", flush=True)
                refresh_kwargs = {"include_features": include_features}
                if stage != "features":
                    refresh_kwargs["feature_set"] = pipeline_config.get("factor_set")
                last_coverage = coverage_report(
                    service,
                    int(pipeline_config["min_ohlcv_rows"]),
                    pipeline_config.get("adjust", "qfq"),
                    **refresh_kwargs,
                )
                if stage == "features":
                    completed_features = int(summary.get("success_count", 0)) + int(summary.get("skipped_count", 0))
                    last_coverage["features"] = {
                        "row_count": int(summary.get("rows_written", 0)),
                        "stock_count": completed_features,
                    }
                item["coverage"] = last_coverage
            except Exception as exc:
                blocked = True
                item["coverage"] = {"error": str(exc)}
                if not args.continue_on_error:
                    break
    finally:
        service.close()

    result["result"] = "blocked" if blocked else "ok"
    json_path, md_path = write_report(result, args.report_dir if args.report_dir.is_absolute() else ROOT / args.report_dir)
    print(f"\nReport JSON: {json_path}\nReport Markdown: {md_path}")
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
