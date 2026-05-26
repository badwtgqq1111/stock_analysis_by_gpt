"""CLI command: validate_factors - standalone factor validation pipeline."""

from pathlib import Path

import pandas as pd

from analyzer_core import StockAnalyzer
from cli.formatters import _safe_close_analyzer
from cli.helpers import (
    _build_factor_scorecard_ridge,
    _build_validation_cache_key,
    _get_validation_cache_dir,
    _is_usable_validation_scorecard,
    _load_validation_weight_cache,
    _merge_recommended_factor_weights,
    _sanitize_validation_scorecard,
    _write_run_manifest,
    _write_validation_weight_cache,
)


def main_validate_factors(
    days=365,
    factor_set="qlib_alpha158",
    max_workers=1,
    show_progress=False,
    validation_horizons=(1, 5, 10, 20),
    validation_quantiles=5,
    validation_min_observations=5,
    validation_stock_limit=None,
    validation_factor_scope="all",
    refresh_recommended_factor_weights=False,
    export_csv=None,
):
    """独立因子验证：只跑验证流水线，产出权重缓存和因子记分卡，不选股。"""
    print("=" * 80)
    print("港股技术分析系统 - 因子验证（独立模式）")
    print("=" * 80)

    analyzer = StockAnalyzer()
    try:
        cache_path = None
        scorecard_path = None
        validation_stock_codes = analyzer.get_all_stocks()
        if validation_stock_limit is not None:
            validation_stock_codes = validation_stock_codes[: max(int(validation_stock_limit), 0)]
        validated_feature_names = None
        effective_scope = validation_factor_scope or "all"
        if effective_scope == "scoring_only":
            validated_feature_names = analyzer.get_score_factor_names()

        cache_key, cache_identity = _build_validation_cache_key(
            factor_set=factor_set,
            validation_days=days,
            validation_horizons=validation_horizons,
            validation_quantiles=validation_quantiles,
            validation_min_observations=validation_min_observations,
            validation_stock_codes=validation_stock_codes,
            validation_factor_scope=effective_scope,
            validated_feature_names=validated_feature_names,
        )
        cache_dir = _get_validation_cache_dir(analyzer)
        cached_payload = None
        if not refresh_recommended_factor_weights:
            cached_payload = _load_validation_weight_cache(cache_dir, cache_key)

        if cached_payload is not None:
            candidate_scorecard = _sanitize_validation_scorecard(
                pd.DataFrame(cached_payload.get("factor_scorecard") or [])
            )
            if _is_usable_validation_scorecard(candidate_scorecard):
                validation_scorecard = candidate_scorecard
                print(
                    f"[INFO] 已命中验证权重缓存: key={cache_key}, "
                    f"path={cached_payload.get('_cache_path')}"
                )
            else:
                print(
                    f"[WARN] 验证权重缓存已失效，自动重算: key={cache_key}"
                )
                cached_payload = None

        if cached_payload is None:
            if show_progress:
                print(
                    f"[PROGRESS] validation phase=features "
                    f"stocks={len(validation_stock_codes)} workers={max_workers} factor_set={factor_set} "
                    f"scope={effective_scope}"
                )
            validation_report = analyzer.build_factor_validation_report(
                stock_codes=validation_stock_codes,
                days=days,
                factor_set=factor_set,
                horizons=validation_horizons,
                quantiles=validation_quantiles,
                min_observations=validation_min_observations,
                max_workers=max_workers,
                show_progress=show_progress,
                validation_factor_scope=effective_scope,
                validated_feature_names=validated_feature_names,
            )
            if validation_report is None:
                print("[ERROR] 因子验证失败")
                return None
            validation_scorecard = _build_factor_scorecard_ridge(validation_report)
            factor_score_config = _merge_recommended_factor_weights(
                (validation_report.get("metadata") or {}).get("factor_score_config"),
                validation_scorecard,
            )
            cache_payload = {
                "cache_key": cache_key,
                "identity": cache_identity,
                "factor_score_config": factor_score_config,
                "factor_scorecard": validation_scorecard.to_dict(orient="records"),
                "feature_materialization": (validation_report.get("metadata") or {}).get("feature_materialization") or {},
                "created_at": pd.Timestamp.utcnow().isoformat(),
            }
            cache_path = _write_validation_weight_cache(cache_dir, cache_key, cache_payload)
            if cache_path is not None:
                print(f"[OK] 已写入验证权重缓存: {cache_path}")

        if not validation_scorecard.empty:
            validation_scorecard = _sanitize_validation_scorecard(validation_scorecard)
            preview_columns = [
                "feature_name",
                "component",
                "configured_factor_weight",
                "recommended_factor_weight",
                "validation_score",
            ]
            preview_columns = [c for c in preview_columns if c in validation_scorecard.columns]
            print(validation_scorecard[preview_columns].head(10).to_string(index=False))

        if export_csv:
            export_path = Path(export_csv)
            export_path.parent.mkdir(parents=True, exist_ok=True)
            scorecard_path = export_path.with_name(f"{export_path.stem}_scorecard.csv")
            validation_scorecard.to_csv(scorecard_path, index=False, encoding="utf-8-sig")
            print(f"[OK] 已导出因子记分卡: {scorecard_path}")

        factor_materialization = {}
        if cached_payload is not None:
            factor_materialization = dict(cached_payload.get("feature_materialization") or {})
        manifest_path, _ = _write_run_manifest(
            run_type="validate_factors",
            analyzer=analyzer,
            fallback_base=export_csv,
            params={
                "days": int(days),
                "factor_set": factor_set,
                "max_workers": int(max_workers),
                "validation_horizons": [int(item) for item in validation_horizons],
                "validation_quantiles": int(validation_quantiles),
                "validation_min_observations": int(validation_min_observations),
                "validation_stock_limit": None if validation_stock_limit is None else int(validation_stock_limit),
                "validation_factor_scope": effective_scope,
                "refresh_recommended_factor_weights": bool(refresh_recommended_factor_weights),
            },
            artifacts={
                "validation_cache_key": cache_key,
                "validation_cache_path": cached_payload.get("_cache_path") if cached_payload is not None else str(cache_path) if cache_path is not None else None,
                "scorecard_csv_path": str(scorecard_path) if scorecard_path is not None else None,
            },
            factor_materialization=factor_materialization,
            status="cache_hit" if cached_payload is not None else "computed",
        )
        print(f"[OK] 已写入 run manifest: {manifest_path}")
    finally:
        _safe_close_analyzer(analyzer)

    print("\n" + "=" * 80)
    print("因子验证完成！")
    print("=" * 80)
    return {"cache_key": cache_key, "scorecard": validation_scorecard, "manifest_path": str(manifest_path)}
