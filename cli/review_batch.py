"""CLI command: review_batch - review a previous scan batch by batch_id."""

from pathlib import Path

import pandas as pd

from data.ingest.service import MarketDataService


def main_review_batch(batch_id, export_csv=None):
    """按 batch_id 回看某次全港股扫描结果。"""
    print("=" * 80)
    print(f"港股技术分析系统 - 扫描批次复盘 {batch_id}")
    print("=" * 80)

    service = MarketDataService()
    try:
        frame = service.get_signal_frame(
            market="HK",
            signal_set="all_hk_topn",
            batch_id=batch_id,
        )
    finally:
        service.close()

    if frame is None or frame.empty:
        print(f"[ERROR] 未找到批次 {batch_id} 的扫描结果")
        return None

    ranking_df = frame[frame["signal_type"] == "ranking"].copy()
    selected_df = frame[frame["signal_type"] == "selected"].copy()
    watchlist_df = frame[frame["signal_type"] == "watchlist"].copy()
    ranking_avg_score = float(ranking_df["score"].mean()) if not ranking_df.empty and "score" in ranking_df.columns else 0.0
    selected_avg_score = float(selected_df["score"].mean()) if not selected_df.empty and "score" in selected_df.columns else 0.0
    watchlist_avg_score = float(watchlist_df["score"].mean()) if not watchlist_df.empty and "score" in watchlist_df.columns else 0.0
    summary_df = pd.DataFrame(
        [
            {
                "batch_id": batch_id,
                "ranking_count": len(ranking_df),
                "selected_count": len(selected_df),
                "watchlist_count": len(watchlist_df),
                "ranking_avg_score": ranking_avg_score,
                "selected_avg_score": selected_avg_score,
                "watchlist_avg_score": watchlist_avg_score,
            }
        ]
    )

    print(f"\n[INFO] 批次号: {batch_id}")
    print(f"[INFO] ranking 数量: {len(ranking_df)}")
    print(f"[INFO] selected 数量: {len(selected_df)}")
    print(f"[INFO] watchlist 数量: {len(watchlist_df)}")
    print(f"[INFO] 平均评分: ranking={ranking_avg_score:.1f}, selected={selected_avg_score:.1f}, watchlist={watchlist_avg_score:.1f}")

    if export_csv:
        export_path = Path(export_csv)
        export_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path = export_path.with_name(f"{export_path.stem}_summary.csv")
        ranking_path = export_path.with_name(f"{export_path.stem}_ranking.csv")
        selected_path = export_path.with_name(f"{export_path.stem}_selected.csv")
        watchlist_path = export_path.with_name(f"{export_path.stem}_watchlist.csv")

        summary_df.to_csv(summary_path, index=False, encoding="utf-8-sig")
        ranking_df.to_csv(ranking_path, index=False, encoding="utf-8-sig")
        selected_df.to_csv(selected_path, index=False, encoding="utf-8-sig")
        watchlist_df.to_csv(watchlist_path, index=False, encoding="utf-8-sig")

        print(f"[OK] 已导出批次 summary: {summary_path}")
        print(f"[OK] 已导出批次 ranking: {ranking_path}")
        print(f"[OK] 已导出批次 selected: {selected_path}")
        print(f"[OK] 已导出批次 watchlist: {watchlist_path}")

    print("\n当前持有建议:")
    for _, row in selected_df.sort_values(["rank_position", "stock_code"]).iterrows():
        print(f"- {row['stock_code']}")

    print("\n" + "=" * 80)
    print("批次复盘完成！")
    print("=" * 80)
    return {
        "batch_id": batch_id,
        "summary": summary_df,
        "ranking": ranking_df,
        "selected": selected_df,
        "watchlist": watchlist_df,
    }
