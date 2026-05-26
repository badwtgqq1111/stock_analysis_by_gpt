"""CLI command: generate_factors - standalone factor materialization pipeline."""

from pathlib import Path

import pandas as pd

from cli.helpers import _write_run_manifest
from data.ingest.service import MarketDataService


def main_generate_factors(
    days=365,
    factor_set="qlib_alpha158",
    stock_limit=None,
    max_workers=1,
    show_progress=False,
    export_csv=None,
):
    """独立批量生成因子并落库到 feature 层。"""
    print("=" * 80)
    print("港股技术分析系统 - 因子生成（独立模式）")
    print("=" * 80)

    service = MarketDataService()
    try:
        stock_codes = service.get_all_stock_codes(
            market="HK",
            asset_type="equity",
            frequency="daily",
            adjust="qfq",
        )
        if stock_limit is not None:
            stock_codes = stock_codes[: max(int(stock_limit), 0)]

        generation_result = service.generate_factor_set(
            stock_codes=stock_codes,
            factor_set=factor_set,
            market="HK",
            frequency="daily",
            adjust="qfq",
            days=days,
            max_workers=max_workers,
            show_progress=show_progress,
        )

        print(
            f"[INFO] 股票数: {generation_result.get('stock_count', 0)}, "
            f"新计算: {generation_result.get('success_count', 0)}, "
            f"跳过复用: {generation_result.get('skipped_count', 0)}, "
            f"空结果: {generation_result.get('empty_count', 0)}, "
            f"错误: {generation_result.get('error_count', 0)}"
        )
        print(f"[INFO] 写入 feature 行数: {generation_result.get('rows_written', 0)}")

        summary_path = None
        if export_csv:
            export_path = Path(export_csv)
            export_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path = export_path.with_name(f"{export_path.stem}_summary.csv")
            pd.DataFrame(generation_result.get("results") or []).to_csv(
                summary_path,
                index=False,
                encoding="utf-8-sig",
            )
            print(f"[OK] 已导出生成摘要: {summary_path}")

        manifest_path, _ = _write_run_manifest(
            run_type="generate_factors",
            analyzer=service,
            fallback_base=export_csv,
            params={
                "days": int(days),
                "factor_set": factor_set,
                "stock_limit": None if stock_limit is None else int(stock_limit),
                "max_workers": int(max_workers),
                "market": "HK",
                "frequency": "daily",
                "adjust": "qfq",
                "warmup_days": int(generation_result.get("warmup_days", 0)),
            },
            artifacts={
                "summary_csv_path": str(summary_path) if summary_path is not None else None,
                "feature_dataset_path": generation_result.get("dataset_path"),
                "stock_count": int(generation_result.get("stock_count", 0)),
                "success_count": int(generation_result.get("success_count", 0)),
                "skipped_count": int(generation_result.get("skipped_count", 0)),
                "rows_written": int(generation_result.get("rows_written", 0)),
            },
            factor_materialization=generation_result.get("factor_materialization") or {},
            status="ok",
        )
        print(f"[OK] 已写入 run manifest: {manifest_path}")
    finally:
        service.close()

    print("\n" + "=" * 80)
    print("因子生成完成！")
    print("=" * 80)
    generation_result["manifest_path"] = str(manifest_path)
    generation_result["summary_csv_path"] = str(summary_path) if summary_path is not None else None
    return generation_result
