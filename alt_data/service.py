"""Orchestration service for alternative data pipeline.

Flow:
  1. Fetch news for HK stock universe via AKShare/Eastmoney
  2. Run sentiment analysis on news content
  3. Aggregate per-stock per-day features
  4. Persist to feature layer via MarketDataService
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

from alt_data.news_fetcher import NewsFetcher, NewsRecord
from alt_data.sentiment import (
    SentimentAnalyzer,
    SentimentResult,
    create_sentiment_analyzer,
)

logger = logging.getLogger(__name__)

# Feature names for the alt_sentiment feature group
FEATURE_GROUP = "alt_sentiment"
FEATURE_NAMES = [
    "alt_sentiment_score",        # Mean daily sentiment (-1..1)
    "alt_sentiment_std",          # Std of within-day sentiment
    "alt_news_count",             # Number of news articles today
    "alt_news_abnormal",          # Z-score of news count vs 30-day baseline
    "alt_sentiment_pos_ratio",    # Fraction of positive articles
    "alt_sentiment_neg_ratio",    # Fraction of negative articles
]


@dataclass
class AltDataBatchResult:
    records: list[NewsRecord] = field(default_factory=list)
    sentiment_results: list[SentimentResult] = field(default_factory=list)
    feature_df: pd.DataFrame | None = None


class AltDataService:
    """Orchestrates alt data pipeline: fetch → analyze → features."""

    _instance: AltDataService | None = None

    def __init__(
        self,
        sentiment_analyzer: SentimentAnalyzer | None = None,
        news_fetcher: NewsFetcher | None = None,
        market_data_service=None,  # MarketDataService, injected
        max_workers: int = 20,
    ):
        self._sentiment = sentiment_analyzer or create_sentiment_analyzer()
        self._fetcher = news_fetcher or NewsFetcher(max_workers=max_workers)
        self._mds = market_data_service

    @classmethod
    def get_or_create(cls, max_workers: int = 20) -> AltDataService:
        """Return a cached singleton instance."""
        if cls._instance is None:
            cls._instance = cls(max_workers=max_workers)
        return cls._instance

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_and_analyze(
        self,
        stock_codes: list[str],
        lookback_days: int = 7,
        progress_callback=None,
    ) -> AltDataBatchResult:
        """Fetch news for a batch of stocks and run sentiment analysis.

        Args:
            stock_codes: List of HK stock codes.
            lookback_days: Only keep news within this many days.
            progress_callback: Optional callable(int done, int total) for fetch.

        Returns:
            AltDataBatchResult with records, sentiment, and feature DataFrame.
        """
        # 1. Fetch news
        fetch_result = self._fetcher.fetch_batch(stock_codes, progress_callback)
        records = fetch_result.records

        if not records:
            return AltDataBatchResult()

        # 2. Filter to lookback window
        cutoff = pd.Timestamp.now().date() - timedelta(days=lookback_days)
        records = [
            r for r in records
            if r.publish_time and r.publish_time.date() >= cutoff
        ]

        if not records:
            return AltDataBatchResult()

        # 3. Run sentiment on news content
        texts = [r.content for r in records]
        sentiment_results = self._sentiment.analyze_batch(texts)

        # 4. Build daily features
        feature_df = self._build_daily_features(records, sentiment_results)

        return AltDataBatchResult(
            records=records,
            sentiment_results=sentiment_results,
            feature_df=feature_df,
        )

    def run_and_persist(
        self,
        stock_codes: list[str],
        lookback_days: int = 7,
        market: str = "HK",
        exchange: str = "SEHK",
    ) -> pd.DataFrame | None:
        """Full pipeline: fetch, analyze, persist to feature layer.

        Returns the feature DataFrame, or None if no news found.
        """
        result = self.fetch_and_analyze(stock_codes, lookback_days=lookback_days)

        if result.feature_df is None or result.feature_df.empty:
            return None

        feature_df = result.feature_df

        if self._mds is not None:
            # Convert to long format and persist via MarketDataService
            long_df = self._to_long_format(
                feature_df, market=market, exchange=exchange
            )
            self._mds.upsert_features(long_df)
            logger.info(
                "Persisted %d alt_sentiment feature rows for %d stocks",
                len(long_df), feature_df["stock_code"].nunique(),
            )

        return feature_df

    # ------------------------------------------------------------------
    # Feature engineering
    # ------------------------------------------------------------------

    def _build_daily_features(
        self,
        records: list[NewsRecord],
        sentiments: list[SentimentResult],
    ) -> pd.DataFrame:
        """Aggregate per-stock per-day features from news + sentiment."""
        df = self._fetcher.news_to_dataframe(records)
        df["sentiment_score"] = [s.score for s in sentiments]
        df["is_positive"] = [1 if s.label == "positive" else 0 for s in sentiments]
        df["is_negative"] = [1 if s.label == "negative" else 0 for s in sentiments]

        # Group by stock_code + date
        daily = df.groupby(["stock_code", "date"]).agg(
            alt_sentiment_score=("sentiment_score", "mean"),
            alt_sentiment_std=("sentiment_score", lambda x: np.std(x) if len(x) > 1 else 0.0),
            alt_news_count=("sentiment_score", "count"),
            alt_sentiment_pos_ratio=("is_positive", "mean"),
            alt_sentiment_neg_ratio=("is_negative", "mean"),
        ).reset_index()

        # Compute abnormal news count (z-score vs 30-day trailing baseline)
        daily = self._add_abnormal_flag(daily)

        return daily

    def _add_abnormal_flag(self, daily: pd.DataFrame) -> pd.DataFrame:
        """Compute z-score of news count vs 30-day rolling baseline per stock."""
        daily = daily.sort_values(["stock_code", "date"])

        counts = daily.pivot_table(
            index="date", columns="stock_code", values="alt_news_count"
        ).fillna(0)

        rolling_mean = counts.rolling(window=30, min_periods=5).mean()
        rolling_std = counts.rolling(window=30, min_periods=5).std().replace(0, 1)
        z_scores = (counts - rolling_mean) / rolling_std

        # Map back to daily frame
        z_map = z_scores.stack().rename("alt_news_abnormal").reset_index()
        daily = daily.merge(z_map, on=["date", "stock_code"], how="left")
        daily["alt_news_abnormal"] = daily["alt_news_abnormal"].fillna(0.0)

        return daily

    def _to_long_format(
        self,
        daily: pd.DataFrame,
        market: str = "HK",
        exchange: str = "SEHK",
    ) -> pd.DataFrame:
        """Convert wide daily features to long format for warehouse insertion."""
        value_cols = [c for c in FEATURE_NAMES if c in daily.columns]
        long = daily.melt(
            id_vars=["stock_code", "date"],
            value_vars=value_cols,
            var_name="feature_name",
            value_name="feature_value",
        )
        long["market"] = market
        long["exchange"] = exchange
        long["asset_type"] = "stock"
        long["frequency"] = "1d"
        long["adjust"] = "qfq"
        long["feature_set"] = FEATURE_GROUP
        long["feature_version"] = "0.1.0"
        long["feature_config_hash"] = "alt_sentiment_v1"
        long["source"] = "alt_data"
        long["trade_date"] = pd.to_datetime(long["date"])
        long["ingest_time"] = pd.Timestamp.now()
        long.drop(columns=["date"], inplace=True)

        return long


# ------------------------------------------------------------------
# Convenience: extract sentiment features for LightGBM panel
# ------------------------------------------------------------------

def merge_sentiment_features(
    panel: pd.DataFrame,
    feature_df: pd.DataFrame,
) -> pd.DataFrame:
    """Merge alt_sentiment features into an existing LightGBM panel.

    Args:
        panel: Existing feature panel with trade_date and stock_code columns.
        feature_df: Daily feature DataFrame from AltDataService.

    Returns:
        Panel with alt_sentiment_* columns merged in (NaN where unavailable).
    """
    if feature_df is None or feature_df.empty:
        return panel

    merge_cols = ["stock_code", "date"] + [
        c for c in FEATURE_NAMES if c in feature_df.columns
    ]
    merge_df = feature_df[merge_cols].copy()
    merge_df["trade_date"] = pd.to_datetime(merge_df["date"])
    merge_df.drop(columns=["date"], inplace=True)

    panel = panel.merge(
        merge_df,
        on=["trade_date", "stock_code"],
        how="left",
    )

    # Fill missing sentiment features with neutral values
    for col in merge_df.columns:
        if col in panel.columns and col not in ("trade_date", "stock_code"):
            if "count" in col:
                panel[col] = panel[col].fillna(0)
            elif "abnormal" in col:
                panel[col] = panel[col].fillna(0)
            else:
                panel[col] = panel[col].fillna(0.0)

    return panel
