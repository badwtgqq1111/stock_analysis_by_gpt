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


def run_stage(name: str, config: dict, service: MarketDataService) -> dict:
    """Execute one configured stage in-process."""
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
        summary["industry"] = service.backfill_cn_industry(show_progress=True)
        return summary
    if name == "alternative":
        layer = config[name]
        return service.import_cn_alternative_evidence(
            input_path=layer["input_path"], output_dir=layer.get("output_dir", "output/alternative_data"),
            source=layer.get("source", "manual_import"),
        )
    if name == "strategy_labels":
        layer = config[name]
        return service.build_cn_strategy_labels(days=int(layer.get("days", 756)), output_dir=layer.get("output_dir", "output/strategy_labels"))
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
    if name == "paper_outcomes":
        layer = config[name]
        return service.evaluate_cn_paper_outcomes(
            selection_path=layer.get("selection_path", "output/results_cn/cn_ensemble_selected.csv"),
            days=int(layer.get("days", p["days"])),
            horizons=tuple(layer.get("horizons", [1, 5, 20, 60])),
            cost_bps=float(layer.get("cost_bps", 10.0)), benchmark_path=layer.get("benchmark_path") or None,
            output_dir=layer.get("output_dir", "output/paper_trading"),
        )
    if name == "paper_account":
        layer = config[name]
        return service.run_cn_paper_account(
            selection_path=layer.get("selection_path", "output/results_cn/cn_ensemble_selected.csv"), days=int(layer.get("days", 756)),
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
            test_days=layer.get("test_days"), purge_days=int(layer.get("purge_days", 20)), embargo_days=int(layer.get("embargo_days", 0)),
        )
    if name == "oos_predictions":
        layer = config[name]
        feature_quality = config.get("model_features", {})
        return service.generate_cn_oos_predictions(
            models=tuple(layer.get("models", ["lightgbm"])), factor_set=p["factor_set"],
            days=int(layer.get("days", 756)), label_horizon=int(layer.get("label_horizon", 20)),
            cleaning_version=layer.get("cleaning_version", "p0.2.v1"), output_dir=layer.get("output_dir", "output/oos_predictions"),
            n_splits=int(layer.get("n_splits", 5)), min_train_days=int(layer.get("min_train_days", 120)),
            test_days=layer.get("test_days"), purge_days=int(layer.get("purge_days", 20)), embargo_days=int(layer.get("embargo_days", 0)),
            transformer_lookback=int(layer.get("transformer_lookback", 60)), transformer_epochs=int(layer.get("transformer_epochs", 5)),
            transformer_batch_size=int(layer.get("transformer_batch_size", 256)), transformer_max_samples=int(layer.get("transformer_max_samples", 200000)),
            transformer_device=layer.get("transformer_device", "auto"), industry_mapping_path=layer.get("industry_mapping_path") or None,
            min_feature_coverage=float(feature_quality.get("min_feature_coverage", 0.05)),
            drop_constant_features=bool(feature_quality.get("drop_constant_features", True)),
            end_date=str(p["end_date"]) or None,
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
            validation_days=int(layer.get("validation_days", 60)),
            cleaning_version=layer.get("cleaning_version", "p0.2.v1"),
            model_dir=layer.get("model_dir"), warm_start_path=layer.get("warm_start_path"),
            min_stock_count=int(p["min_training_stocks"]),
            embargo_days=int(layer.get("embargo_days", layer.get("label_horizon", 20))),
            min_feature_coverage=float(feature_quality.get("min_feature_coverage", 0.05)),
            drop_constant_features=bool(feature_quality.get("drop_constant_features", True)),
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
            validation_days=int(layer.get("validation_days", 60)),
            lookback=int(layer.get("lookback", 60)), epochs=int(layer.get("epochs", 10)),
            batch_size=int(layer.get("batch_size", 256)), max_samples=int(layer.get("max_samples", 200000)),
            cleaning_version=layer.get("cleaning_version", "p0.2.v1"), model_dir=layer.get("model_dir"),
            min_stock_count=int(p["min_training_stocks"]),
            warm_start_path=layer.get("warm_start_path"), warm_start_manifest_path=layer.get("warm_start_manifest_path"),
            device=layer.get("device", "auto"),
            embargo_days=int(layer.get("embargo_days", layer.get("label_horizon", 20))),
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
        layer = config[name]
        return service.select_persisted_model_scores(
            model_scores_dir=layer.get("model_scores_dir", "output/model_scores"),
            output_dir=layer.get("output_dir", p["export_csv"]),
            model=layer.get("model", "ensemble"), top_n=int(layer.get("top_n", p["top_n"])),
            portfolio_mode=layer.get("portfolio_mode", "topn"), portfolio_constraints=layer.get("portfolio_constraints") or None,
            initial_capital=float(layer.get("initial_capital", 1_000_000.0)),
            show_progress=True,
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
    parser.add_argument("--stage", choices=["all", "daily_bars", "intraday_bars", "fundamental", "alternative", "strategy_labels", "features", "regime", "clean_panel", "lightgbm", "transformer", "cnn", "model_scores", "selection", "paper_outcomes", "paper_account", "graph_temporal", "oos_predictions", "model_comparison"], default="all")
    parser.add_argument("--report-dir", type=Path, default=ROOT / "output" / "pipeline_reports")
    parser.add_argument("--continue-on-error", action="store_true", help="Continue independent data stages after a stage failure")
    args = parser.parse_args()
    config = read_config(args.config if args.config.is_absolute() else ROOT / args.config)
    started = datetime.now().isoformat(timespec="seconds")
    result = {"started_at": started, "config": str(args.config), "stages": [], "result": "failed"}
    stages = ["daily_bars", "intraday_bars", "fundamental", "alternative", "strategy_labels", "features", "regime", "clean_panel", "lightgbm", "transformer", "cnn", "model_scores", "selection", "paper_outcomes", "paper_account", "graph_temporal", "oos_predictions", "model_comparison"] if args.stage == "all" else [args.stage]
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
            if stage in {"features", "clean_panel", "lightgbm", "transformer", "cnn", "model_scores", "selection", "oos_predictions"}:
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
                if stage in {"clean_panel", "lightgbm", "transformer", "cnn", "selection", "oos_predictions"}:
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
                summary = run_stage(stage, config, service)
                item = {"name": stage, "status": "ok", "summary": summary}
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
