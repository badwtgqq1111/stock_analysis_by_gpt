"""CLI commands: factor_report and signal_report."""

import json
from pathlib import Path

import pandas as pd

from core.constants import DEFAULT_FACTOR_SET
from core import StockAnalyzer
from cli.formatters import _safe_close_analyzer
from cli.helpers import (
    _build_factor_scorecard_ridge,
    _merge_recommended_factor_weights,
    _write_run_manifest,
)


def main_factor_report(
    days=365,
    factor_set=DEFAULT_FACTOR_SET,
    export_csv=None,
    max_workers=1,
    show_progress=False,
    horizons=(1, 5, 10, 20),
    quantiles=5,
    min_observations=5,
    stock_limit=None,
    validation_factor_scope=None,
):
    """输出全市场因子验证报告。"""
    print("=" * 80)
    print(f"港股技术分析系统 - 因子验证报告 {factor_set}")
    print("=" * 80)

    analyzer = StockAnalyzer()
    try:
        stock_codes = analyzer.get_all_stocks()
        if stock_limit is not None:
            stock_codes = stock_codes[: max(int(stock_limit), 0)]
        effective_validation_factor_scope = validation_factor_scope or "all"
        report = analyzer.build_factor_validation_report(
            stock_codes=stock_codes,
            days=days,
            factor_set=factor_set,
            horizons=horizons,
            quantiles=quantiles,
            min_observations=min_observations,
            max_workers=max_workers,
            show_progress=show_progress,
            validation_factor_scope=effective_validation_factor_scope,
            validated_feature_names=(
                analyzer.get_score_factor_names() if effective_validation_factor_scope == "scoring_only" else None
            ),
        )
    finally:
        _safe_close_analyzer(analyzer)

    if report is None:
        print("[ERROR] 因子验证报告生成失败")
        return None
    metadata = report.get("metadata", {})
    factor_scorecard = _build_factor_scorecard_ridge(report)
    factor_score_config = _merge_recommended_factor_weights(
        metadata.get("factor_score_config"),
        factor_scorecard,
    )
    if isinstance(report.get("metadata"), dict):
        report["metadata"]["factor_score_config"] = factor_score_config
    metadata = report.get("metadata", {})
    stock_summary = report.get("stock_summary", pd.DataFrame())

    print(f"\n[INFO] 因子集: {metadata.get('factor_set')}")
    print(f"[INFO] 样本股票数: {metadata.get('success_count', 0)} / {metadata.get('stock_count', 0)}")
    print(f"[INFO] horizons: {metadata.get('horizons')}")
    print(f"[INFO] quantiles: {metadata.get('quantiles')}, min_observations: {metadata.get('min_observations')}")
    print(
        f"[INFO] validation_factor_scope: {metadata.get('validation_factor_scope', 'all')}, "
        f"validated_feature_count: {len(metadata.get('validated_feature_names') or []) if metadata.get('validated_feature_names') else 'all'}"
    )

    if not stock_summary.empty:
        print(
            "[INFO] 样本内均值: "
            f"mean_ic={stock_summary['mean_ic'].mean():.4f}, "
            f"mean_rank_ic={stock_summary['mean_rank_ic'].mean():.4f}, "
            f"mean_spread={stock_summary['mean_spread'].mean():.4f}, "
            f"mean_turnover={stock_summary['mean_turnover'].mean():.4f}"
        )

    if not factor_scorecard.empty:
        print("\nTop 因子质量:")
        preview_columns = [
            "feature_name",
            "component",
            "configured_factor_weight",
            "recommended_factor_weight",
            "mean_rank_ic",
            "mean_spread",
            "mean_turnover",
            "validation_score",
        ]
        preview_columns = [column for column in preview_columns if column in factor_scorecard.columns]
        print(factor_scorecard[preview_columns].head(15).to_string(index=False))

    stock_summary_path = None
    factor_coverage_path = None
    factor_scorecard_path = None
    ic_summary_path = None
    quantile_summary_path = None
    long_short_summary_path = None
    turnover_summary_path = None
    decay_summary_path = None
    metadata_file = None
    if export_csv:
        export_path = Path(export_csv)
        export_path.parent.mkdir(parents=True, exist_ok=True)
        outputs = {
            "stock_summary": report.get("stock_summary", pd.DataFrame()),
            "factor_coverage": report.get("factor_coverage", pd.DataFrame()),
            "factor_scorecard": factor_scorecard,
            "ic_summary": report.get("ic_summary", pd.DataFrame()),
            "quantile_summary": report.get("quantile_summary", pd.DataFrame()),
            "long_short_summary": report.get("long_short_summary", pd.DataFrame()),
            "turnover_summary": report.get("turnover_summary", pd.DataFrame()),
            "decay_summary": report.get("decay_summary", pd.DataFrame()),
        }
        for name, frame in outputs.items():
            output_file = export_path.with_name(f"{export_path.stem}_{name}.csv")
            frame.to_csv(output_file, index=False, encoding="utf-8-sig")
            if name == "stock_summary":
                stock_summary_path = output_file
            elif name == "factor_coverage":
                factor_coverage_path = output_file
            elif name == "factor_scorecard":
                factor_scorecard_path = output_file
            elif name == "ic_summary":
                ic_summary_path = output_file
            elif name == "quantile_summary":
                quantile_summary_path = output_file
            elif name == "long_short_summary":
                long_short_summary_path = output_file
            elif name == "turnover_summary":
                turnover_summary_path = output_file
            elif name == "decay_summary":
                decay_summary_path = output_file
            print(f"[OK] 已导出 {name}: {output_file}")

        metadata_file = export_path.with_name(f"{export_path.stem}_metadata.json")
        metadata_file.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"[OK] 已导出 metadata: {metadata_file}")

    manifest_path, _ = _write_run_manifest(
        run_type="factor_report",
        analyzer=analyzer,
        fallback_base=export_csv,
        params={
            "days": int(days),
            "factor_set": factor_set,
            "max_workers": int(max_workers),
            "show_progress": bool(show_progress),
            "horizons": [int(item) for item in horizons],
            "quantiles": int(quantiles),
            "min_observations": int(min_observations),
            "stock_limit": None if stock_limit is None else int(stock_limit),
            "validation_factor_scope": effective_validation_factor_scope,
        },
        artifacts={
            "stock_summary_csv_path": str(stock_summary_path) if stock_summary_path is not None else None,
            "factor_coverage_csv_path": str(factor_coverage_path) if factor_coverage_path is not None else None,
            "factor_scorecard_csv_path": str(factor_scorecard_path) if factor_scorecard_path is not None else None,
            "ic_summary_csv_path": str(ic_summary_path) if ic_summary_path is not None else None,
            "quantile_summary_csv_path": str(quantile_summary_path) if quantile_summary_path is not None else None,
            "long_short_summary_csv_path": str(long_short_summary_path) if long_short_summary_path is not None else None,
            "turnover_summary_csv_path": str(turnover_summary_path) if turnover_summary_path is not None else None,
            "decay_summary_csv_path": str(decay_summary_path) if decay_summary_path is not None else None,
            "metadata_json_path": str(metadata_file) if metadata_file is not None else None,
        },
        factor_materialization=metadata.get("feature_materialization") or {},
        status="ok",
    )
    print(f"[OK] 已写入 run manifest: {manifest_path}")

    print("\n" + "=" * 80)
    print("因子验证报告完成！")
    print("=" * 80)
    return {
        **report,
        "factor_scorecard": factor_scorecard,
        "manifest_path": str(manifest_path),
    }


def main_signal_report(
    days=365,
    export_csv=None,
    max_workers=1,
    show_progress=False,
    horizons=(20, 40, 60),
    stock_limit=None,
    signal_recipes=None,
    signal_cooldown_days=20,
    signal_event_policy="first",
):
    """输出全市场信号 recipe 验证报告。"""
    print("=" * 80)
    print("港股技术分析系统 - 信号配方验证报告")
    print("=" * 80)

    analyzer = StockAnalyzer(signal_recipes=signal_recipes)
    try:
        stock_codes = analyzer.get_all_stocks()
        if stock_limit is not None:
            stock_codes = stock_codes[: max(int(stock_limit), 0)]
        report = analyzer.build_signal_recipe_report(
            stock_codes=stock_codes,
            days=days,
            signal_recipes=signal_recipes,
            horizons=horizons,
            max_workers=max_workers,
            show_progress=show_progress,
            signal_cooldown_days=signal_cooldown_days,
            signal_event_policy=signal_event_policy,
        )
    finally:
        _safe_close_analyzer(analyzer)

    if report is None:
        print("[ERROR] 信号配方验证报告生成失败")
        return None

    metadata = report.get("metadata", {})
    summary = report.get("summary", pd.DataFrame())
    events = report.get("events", pd.DataFrame())
    events_raw = report.get("events_raw", pd.DataFrame())

    print(f"\n[INFO] 样本股票数: {metadata.get('stock_count', 0)}")
    print(f"[INFO] 原始触发事件数: {metadata.get('raw_event_count', metadata.get('event_count', 0))}")
    print(f"[INFO] 合并后事件数: {metadata.get('event_count', 0)}")
    print(f"[INFO] signal_recipes: {metadata.get('signal_recipes')}")
    print(f"[INFO] horizons: {metadata.get('horizons')}")
    print(f"[INFO] signal_cooldown_days: {metadata.get('signal_cooldown_days')}, signal_event_policy: {metadata.get('signal_event_policy')}")

    if not summary.empty:
        print("\n信号表现摘要:")
        print(summary.head(20).to_string(index=False))
    else:
        print("[WARN] 未发现有效信号事件")

    if export_csv:
        export_path = Path(export_csv)
        export_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path = export_path.with_name(f"{export_path.stem}_signal_summary.csv")
        events_path = export_path.with_name(f"{export_path.stem}_signal_events.csv")
        raw_events_path = export_path.with_name(f"{export_path.stem}_signal_events_raw.csv")
        metadata_path = export_path.with_name(f"{export_path.stem}_metadata.json")
        summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
        events.to_csv(events_path, index=False, encoding="utf-8-sig")
        events_raw.to_csv(raw_events_path, index=False, encoding="utf-8-sig")
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"[OK] 已导出信号摘要: {summary_path}")
        print(f"[OK] 已导出合并信号事件: {events_path}")
        print(f"[OK] 已导出原始信号事件: {raw_events_path}")
        print(f"[OK] 已导出元数据: {metadata_path}")

    print("\n" + "=" * 80)
    print("信号配方验证报告完成！")
    print("=" * 80)
    return report
