"""News fetcher for HK stocks using Eastmoney via AKShare."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd


@dataclass
class NewsRecord:
    stock_code: str
    title: str
    content: str
    publish_time: datetime
    source: str
    url: str


@dataclass
class FetchResult:
    records: list[NewsRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stocks_fetched: int = 0
    stocks_failed: int = 0


class NewsFetcher:
    """Fetch recent news for HK stocks from Eastmoney via AKShare.

    Supports both sequential (single-stock) and concurrent (batch) fetching.
    """

    def __init__(self, delay: float = 0.3, max_retries: int = 2, max_workers: int = 20):
        self._delay = delay
        self._max_retries = max_retries
        self._max_workers = max_workers

    def fetch_stock_news(self, stock_code: str) -> list[NewsRecord]:
        """Fetch recent news for a single stock (sequential, no lock needed).

        Returns a list of NewsRecord sorted by publish_time descending.
        """
        import akshare as ak

        for attempt in range(self._max_retries + 1):
            try:
                if attempt > 0:
                    time.sleep(self._delay * (attempt + 1))

                code_digits = stock_code.zfill(5)
                df = ak.stock_news_em(symbol=code_digits)

                if df is None or df.empty:
                    return []

                records = []
                for _, row in df.iterrows():
                    try:
                        publish_time = pd.Timestamp(row["发布时间"])
                    except (ValueError, TypeError):
                        publish_time = pd.Timestamp.now()

                    record = NewsRecord(
                        stock_code=stock_code,
                        title=str(row["新闻标题"]),
                        content=str(row["新闻内容"]),
                        publish_time=publish_time,
                        source=str(row.get("文章来源", "")),
                        url=str(row.get("新闻链接", "")),
                    )
                    records.append(record)

                return records

            except Exception as e:
                if attempt == self._max_retries:
                    raise RuntimeError(
                        f"Failed to fetch news for {stock_code}: {e}"
                    ) from e
                time.sleep(self._delay)

        return []

    def fetch_batch(
        self,
        stock_codes: list[str],
        progress_callback=None,
    ) -> FetchResult:
        """Fetch news for multiple stocks using thread pool.

        Args:
            stock_codes: List of HK stock codes (e.g. ['00700', '09988']).
            progress_callback: Optional callable(int current, int total).

        Returns:
            FetchResult with combined records and error info.
        """
        result = FetchResult()
        lock = threading.Lock()
        completed = 0
        total = len(stock_codes)

        def _fetch_one(code: str) -> None:
            nonlocal completed
            try:
                records = self.fetch_stock_news(code)
                with lock:
                    result.records.extend(records)
                    result.stocks_fetched += 1
                    completed += 1
                    if progress_callback:
                        progress_callback(completed, total)
            except Exception as e:
                with lock:
                    result.errors.append(f"{code}: {e}")
                    result.stocks_failed += 1
                    completed += 1
                    if progress_callback:
                        progress_callback(completed, total)

        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            futures = {executor.submit(_fetch_one, code): code for code in stock_codes}
            for _ in as_completed(futures):
                pass  # Progress reported inside _fetch_one via callback

        return result

    def news_to_dataframe(self, records: list[NewsRecord]) -> pd.DataFrame:
        """Convert news records to a DataFrame with date column."""
        if not records:
            return pd.DataFrame(
                columns=[
                    "stock_code", "title", "content",
                    "publish_time", "date", "source", "url",
                ]
            )

        rows = []
        for r in records:
            rows.append({
                "stock_code": r.stock_code,
                "title": r.title,
                "content": r.content,
                "publish_time": r.publish_time,
                "date": r.publish_time.date(),
                "source": r.source,
                "url": r.url,
            })

        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        return df
