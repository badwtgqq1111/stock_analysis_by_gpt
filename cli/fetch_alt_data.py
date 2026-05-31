"""CLI for alternative data pipeline: fetch news → sentiment → persist to feature layer."""

from __future__ import annotations

import sys
import time


def main_fetch_alt_data(
    stock_limit: int | None = None,
    max_workers: int = 20,
    market: str = "HK",
    show_progress: bool = False,
    persist: bool = False,
):
    """Fetch news sentiment for HK stocks and optionally persist to feature layer.

    Args:
        stock_limit: Max stocks to fetch news for. None = all HK stocks.
        max_workers: Concurrent threads for news fetching (default 20).
        market: Target market (HK).
        show_progress: Show progress bars.
        persist: Write results to the feature layer via MarketDataWarehouse.
    """
    from data.store.layout import DataLayout
    from data.store.warehouse import MarketDataWarehouse
    from alt_data.sentiment import create_sentiment_analyzer
    from alt_data.service import AltDataService

    # Resolve stock universe
    if market == "HK":
        stock_codes = _resolve_hk_universe(stock_limit)
    else:
        print(f"[ERROR] Unsupported market: {market}")
        return 1

    if not stock_codes:
        print("[ERROR] No stocks found for alt data fetch.")
        return 1

    print(f"[INFO] alt_data fetch target: {len(stock_codes)} stocks (max_workers={max_workers})")

    # Init warehouse for persistence
    wh = None
    if persist:
        layout = DataLayout(base_dir="assets/data")
        wh = MarketDataWarehouse(layout, read_only=False)

    # Create sentiment analyzer (tries transformers, falls back to rule-based)
    print("[INFO] Loading sentiment analyzer...")
    analyzer = create_sentiment_analyzer()
    analyzer_type = type(analyzer).__name__
    print(f"[INFO] Sentiment analyzer: {analyzer_type}")

    service = AltDataService(
        sentiment_analyzer=analyzer,
        market_data_service=None,  # We handle persistence directly
        max_workers=max_workers,
    )

    if wh is not None:
        service._mds = wh

    started_at = time.time()

    # Fetch and analyze
    result = service.fetch_and_analyze(
        stock_codes,
        lookback_days=7,
        progress_callback=(
            lambda done, total: _print_progress(done, total, started_at)
            if show_progress else None
        ),
    )

    elapsed = time.time() - started_at
    n_records = len(result.records)
    n_stocks_with_news = (
        result.feature_df["stock_code"].nunique()
        if result.feature_df is not None and not result.feature_df.empty
        else 0
    )

    print(
        f"[INFO] Fetched {n_records} news articles "
        f"across {n_stocks_with_news} stocks in {elapsed:.1f}s"
    )

    if result.feature_df is None or result.feature_df.empty:
        print("[WARN] No news found. Try again later (news updates daily).")
        return 0

    # Print summary
    df = result.feature_df
    print(f"\n[SUMMARY] Daily feature rows: {len(df)}")
    print(f"  Date range: {df['date'].min()} - {df['date'].max()}")
    print(f"  Stocks with news: {df['stock_code'].nunique()}")
    print(f"  Total news count: {df['alt_news_count'].sum():.0f}")
    print(f"  Avg sentiment: {df['alt_sentiment_score'].mean():.3f}")
    print(f"  Top 5 stocks by coverage:")
    top = df.groupby("stock_code")["alt_news_count"].sum().sort_values(ascending=False).head(5)
    for code, cnt in top.items():
        print(f"    {code}: {int(cnt)} articles")

    # Persist
    if persist and wh is not None:
        print("\n[INFO] Persisting to feature layer...")
        long_df = service._to_long_format(df, market=market, exchange="SEHK")
        wh.upsert_features(long_df)
        print(f"[INFO] Persisted {len(long_df)} feature rows (alt_sentiment).")

    return 0


def _resolve_hk_universe(stock_limit: int | None) -> list[str]:
    """Resolve HK stock universe from warehouse, optionally capped."""
    try:
        from data.store.layout import DataLayout
        from data.store.warehouse import MarketDataWarehouse

        layout = DataLayout(base_dir="assets/data")
        wh = MarketDataWarehouse(layout, read_only=True)
        stocks = wh.get_all_stock_codes(market="HK")
        if stocks and stock_limit:
            stocks = stocks[:stock_limit]
        return list(stocks) if stocks is not None else []
    except Exception:
        pass

    return []


def _print_progress(done: int, total: int, started_at: float):
    elapsed = time.time() - started_at
    rate = done / elapsed if elapsed > 0 else 0
    eta = (total - done) / rate if rate > 0 else 0
    pct = done / total * 100
    print(
        f"\r[PROGRESS] alt_data fetch {done}/{total} ({pct:.0f}%) "
        f"rate={rate:.1f}/s elapsed={elapsed:.0f}s eta={eta:.0f}s",
        end="",
        file=sys.stderr,
    )
    if done >= total:
        print(file=sys.stderr)
