#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""A 股数据链路与回测完整性测试，不依赖外网。"""

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.model import normalize_ohlcv_frame, normalize_stock_info
from data.store import DataLayout, MarketDataWarehouse


class FakeBaoStockResult:
    def __init__(self, fields, rows, error_code="0", error_msg=""):
        self.fields = fields
        self._rows = list(rows)
        self.error_code = error_code
        self.error_msg = error_msg
        self._idx = -1

    def next(self):
        self._idx += 1
        return self._idx < len(self._rows)

    def get_row_data(self):
        return self._rows[self._idx]


class FakeBaoStockModule:
    def __init__(self):
        self.logged_in = False

    def login(self):
        self.logged_in = True
        return SimpleNamespace(error_code="0", error_msg="")

    def logout(self):
        self.logged_in = False

    def query_history_k_data_plus(self, code, fields, start_date, end_date, frequency, adjustflag):
        assert code == "sh.600000"
        assert frequency == "d"
        assert adjustflag == "2"
        return FakeBaoStockResult(
            fields.split(","),
            [
                ["2024-01-02", "sh.600000", "10.0", "10.8", "9.8", "10.5", "1200000", "12600000", "2", "0.8", "1.2", "0"],
                ["2024-01-03", "sh.600000", "10.5", "11.0", "10.4", "10.9", "1300000", "14170000", "2", "0.9", "3.8", "0"],
            ],
        )

    def query_all_stock(self, day=None):
        return FakeBaoStockResult(
            ["code", "tradeStatus", "code_name"],
            [
                ["sh.600000", "1", "浦发银行"],
                ["sz.000001", "1", "平安银行"],
            ],
        )

    def query_stock_industry(self, code=None):
        return FakeBaoStockResult(
            ["code", "code_name", "industry", "industryClassification"],
            [
                ["sh.600000", "浦发银行", "银行", "申万一级行业"],
                ["sz.000001", "平安银行", "银行", "申万一级行业"],
            ],
        )

    def query_stock_basic(self, code=None, code_name=None):
        return FakeBaoStockResult(
            ["code", "code_name", "ipoDate", "outDate", "type", "status"],
            [["sh.600000", "浦发银行", "1999-11-10", "", "1", "1"]],
        )

    def query_profit_data(self, code, year, quarter):
        return FakeBaoStockResult(
            ["code", "pubDate", "statDate", "roeAvg", "npMargin", "gpMargin", "netProfit", "epsTTM", "MBRevenue"],
            [[code, f"{year}-04-30", f"{year}-03-31", "8.5", "18.0", "35.0", "1200000000", "0.55", "6500000000"]],
        )

    def query_operation_data(self, code, year, quarter):
        return FakeBaoStockResult(["code", "pubDate", "statDate"], [[code, f"{year}-04-30", f"{year}-03-31"]])

    def query_growth_data(self, code, year, quarter):
        return FakeBaoStockResult(
            ["code", "pubDate", "statDate", "YOYNI", "YOYEPSBasic"],
            [[code, f"{year}-04-30", f"{year}-03-31", "12.0", "10.0"]],
        )

    def query_balance_data(self, code, year, quarter):
        return FakeBaoStockResult(
            ["code", "pubDate", "statDate", "totalAssets", "totalLiability"],
            [[code, f"{year}-04-30", f"{year}-03-31", "100000000000", "85000000000"]],
        )

    def query_cash_flow_data(self, code, year, quarter):
        return FakeBaoStockResult(
            ["code", "pubDate", "statDate", "CAToAsset"],
            [[code, f"{year}-04-30", f"{year}-03-31", "11.0"]],
        )


def test_baostock_code_conversion_and_result_frame(monkeypatch):
    from data.ingest.providers import cn_baostock

    monkeypatch.setattr(cn_baostock, "bs", FakeBaoStockModule())

    assert cn_baostock.to_baostock_code("600000.SH") == "sh.600000"
    assert cn_baostock.to_baostock_code("000001.SZ") == "sz.000001"
    assert cn_baostock.from_baostock_code("bj.430047") == "430047.BJ"

    frame = cn_baostock.baostock_result_to_frame(
        FakeBaoStockResult(["code", "code_name"], [["sh.600000", "浦发银行"]])
    )

    assert frame.to_dict(orient="records") == [{"code": "sh.600000", "code_name": "浦发银行"}]


def test_baostock_history_and_universe_fetchers(monkeypatch):
    from data.ingest.providers import cn_baostock
    from data.ingest.providers.cn_universe import CNMarketListFetcher

    monkeypatch.setattr(cn_baostock, "bs", FakeBaoStockModule())

    history = cn_baostock.CNBaoStockHistoryFetcher("600000.SH").fetch(
        start_date="2024-01-01",
        end_date="2024-01-10",
        adjust="qfq",
    )
    assert list(history.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert len(history) == 2
    assert history.index[0] == pd.Timestamp("2024-01-02")

    universe = CNMarketListFetcher().fetch(limit=1)
    assert universe == [{"code": "600000.SH", "name": "浦发银行"}]


def test_baostock_history_quiet_mode_suppresses_session_output(monkeypatch, capsys):
    from data.ingest.providers import cn_baostock

    class LoudBaoStockModule(FakeBaoStockModule):
        def login(self):
            print("login success!")
            return super().login()

        def logout(self):
            print("logout success!")
            return super().logout()

    monkeypatch.setattr(cn_baostock, "bs", LoudBaoStockModule())

    history = cn_baostock.CNBaoStockHistoryFetcher("600000.SH", verbose=False).fetch(
        start_date="2024-01-01",
        end_date="2024-01-10",
        adjust="qfq",
    )
    captured = capsys.readouterr()

    assert len(history) == 2
    assert "login success!" not in captured.out
    assert "logout success!" not in captured.out


def test_cn_market_list_quiet_mode_suppresses_baostock_session_output(monkeypatch, capsys):
    from data.ingest.providers import cn_baostock
    from data.ingest.providers.cn_universe import CNMarketListFetcher

    class LoudBaoStockModule(FakeBaoStockModule):
        def login(self):
            print("login success!")
            return super().login()

        def logout(self):
            print("logout success!")
            return super().logout()

    monkeypatch.setattr(cn_baostock, "bs", LoudBaoStockModule())

    universe = CNMarketListFetcher(data_source="baostock", verbose=False).fetch(limit=1)
    captured = capsys.readouterr()

    assert universe == [{"code": "600000.SH", "name": "浦发银行"}]
    assert "login success!" not in captured.out
    assert "logout success!" not in captured.out


def test_cn_baostock_daily_bulk_uses_process_pool_selector():
    from data.ingest.service import _chunk_sequence, _should_use_cn_history_process_pool

    assert _should_use_cn_history_process_pool("baostock", ["daily"], include_stock_info=False)
    assert not _should_use_cn_history_process_pool("akshare", ["daily"], include_stock_info=False)
    assert not _should_use_cn_history_process_pool("baostock", ["daily"], include_stock_info=True)
    assert not _should_use_cn_history_process_pool("baostock", ["daily", "1min"], include_stock_info=False)
    assert _chunk_sequence([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
    assert _chunk_sequence([1, 2], 0) == [[1], [2]]


def test_cn_sync_socket_timeout_env(monkeypatch):
    from data.ingest.service import _cn_sync_socket_timeout

    monkeypatch.delenv("CN_SYNC_SOCKET_TIMEOUT", raising=False)
    assert _cn_sync_socket_timeout() == 5.0

    monkeypatch.setenv("CN_SYNC_SOCKET_TIMEOUT", "2.5")
    assert _cn_sync_socket_timeout() == 2.5

    monkeypatch.setenv("CN_SYNC_SOCKET_TIMEOUT", "0")
    assert _cn_sync_socket_timeout() is None

    monkeypatch.setenv("CN_SYNC_SOCKET_TIMEOUT", "bad")
    assert _cn_sync_socket_timeout() == 5.0


def test_cn_sync_requests_default_timeout_injected(monkeypatch):
    from data.ingest import service

    calls = []

    def fake_request(self, method, url, **kwargs):
        calls.append(kwargs)
        return "ok"

    monkeypatch.setattr(service.requests.sessions.Session, "request", fake_request)

    with service._temporary_requests_default_timeout(2.5):
        assert service.requests.get("https://example.test") == "ok"
        assert service.requests.get("https://example.test", timeout=7) == "ok"

    assert calls[0]["timeout"] == 2.5
    assert calls[1]["timeout"] == 7


def test_cn_history_source_priority_uses_baostock_only_as_default_fallback():
    from data.ingest.providers.cn_common import build_source_priority

    assert build_source_priority() == ["tencent", "akshare_sina", "baostock", "akshare_eastmoney"]
    assert build_source_priority("akshare") == ["tencent", "akshare_sina", "baostock", "akshare_eastmoney"]
    assert build_source_priority("baostock")[0] == "baostock"
    assert build_source_priority("eastmoney")[0] == "akshare_eastmoney"


def test_cn_sync_progress_postfix_shows_last_stock_and_source_counts():
    from data.ingest.service import _cn_progress_postfix

    postfix = _cn_progress_postfix(
        success_count=10,
        skipped_count=2,
        failed_count=1,
        row_count=1234,
        rows_written=1000,
        last_stock_code="600000.SH",
        last_source="akshare_sina",
        source_counts={"tencent": 6, "akshare_sina": 3, "baostock": 1, "akshare_eastmoney": 0},
    )

    assert "last=600000.SH:sn" in postfix
    assert "src=tx:6,sn:3,bs:1,em:0" in postfix
    assert "written=1000" in postfix


def test_cn_eastmoney_daily_uses_short_timeout(monkeypatch):
    from data.ingest.providers import cn_history

    calls = []

    class FakeAk:
        @staticmethod
        def stock_zh_a_hist(**kwargs):
            calls.append(kwargs)
            return pd.DataFrame(
                {
                    "日期": ["2024-01-02"],
                    "开盘": [10.0],
                    "收盘": [10.5],
                    "最高": [10.8],
                    "最低": [9.8],
                    "成交量": [1200000],
                }
            )

    monkeypatch.setattr(cn_history, "ak", FakeAk)

    fetcher = cn_history.CNHistoryDataFetcher("600000.SH", data_source="eastmoney", verbose=False)
    frame = fetcher.fetch(start_date="2024-01-02", end_date="2024-01-03", period="daily")

    assert len(frame) == 1
    assert calls[0]["timeout"] == 3


def test_cn_stock_info_falls_back_to_sina_after_eastmoney_error(monkeypatch):
    from data.ingest.providers import cn_info

    def raise_eastmoney():
        raise RuntimeError("eastmoney ssl eof")

    fake_ak = SimpleNamespace(
        stock_zh_a_spot_em=raise_eastmoney,
        stock_zh_a_spot=lambda: pd.DataFrame(
            [
                {
                    "code": "600000",
                    "name": "浦发银行",
                    "trade": "11.23",
                    "settlement": "11.10",
                    "open": "11.12",
                    "high": "11.30",
                    "low": "11.01",
                    "volume": "1200000",
                    "amount": "13476000",
                }
            ]
        ),
    )
    monkeypatch.setattr(cn_info, "ak", fake_ak)
    monkeypatch.setattr(
        cn_info.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("tencent unavailable")),
    )

    fetcher = cn_info.CNStockInfoFetcher("600000.SH", data_source="eastmoney")
    info = fetcher.fetch()

    assert info["name"] == "浦发银行"
    assert info["current_price"] == 11.23
    assert info["daily_turnover"] == 13476000
    assert fetcher.last_successful_source == "akshare_sina"


def test_cn_stock_info_tencent_provider_parses_valuation_fields(monkeypatch):
    from data.ingest.providers import cn_info

    parts = [""] * 88
    parts[0] = "1"
    parts[1] = "浦发银行"
    parts[2] = "600000"
    parts[3] = "8.64"
    parts[4] = "8.70"
    parts[5] = "8.69"
    parts[33] = "8.82"
    parts[34] = "8.59"
    parts[36] = "577862"
    parts[37] = "50256"
    parts[38] = "0.17"
    parts[39] = "5.72"
    parts[44] = "2877.62"
    parts[46] = "0.38"
    parts[72] = "33305838300"
    parts[73] = "33305838300"

    class FakeResponse:
        content = f'v_sh600000="{"~".join(parts)}";'.encode("gbk")

        def raise_for_status(self):
            return None

    monkeypatch.setattr(cn_info.requests, "get", lambda *args, **kwargs: FakeResponse())

    fetcher = cn_info.CNStockInfoFetcher("600000.SH", data_source="tencent")
    info = fetcher.fetch()

    assert info["name"] == "浦发银行"
    assert info["current_price"] == 8.64
    assert info["daily_turnover"] == 502560000.0
    assert info["market_cap"] == 287762000000.0
    assert info["pe_ratio"] == 5.72
    assert info["pb_ratio"] == 0.38
    assert info["total_shares"] == 33305838300
    assert fetcher.last_successful_source == "tencent"


def test_cn_stock_info_baostock_is_last_fallback():
    from data.ingest.providers.cn_info import CNStockInfoFetcher

    fetcher = CNStockInfoFetcher("600000.SH", data_source="baostock")

    assert fetcher.source_priority[-1] == "baostock"
    assert fetcher.source_priority[0] == "tencent"
    assert fetcher.source_priority[1] == "akshare_eastmoney"


def test_cn_stock_info_explicit_eastmoney_keeps_requested_source_first():
    from data.ingest.providers.cn_info import CNStockInfoFetcher

    fetcher = CNStockInfoFetcher("600000.SH", data_source="eastmoney")

    assert fetcher.source_priority[0] == "akshare_eastmoney"
    assert fetcher.source_priority[-1] == "baostock"


def test_bulk_sync_cn_history_skips_stock_info_by_default(monkeypatch):
    from data.ingest.service import MarketDataService

    for key in ("CLICKHOUSE_HOST", "CLICKHOUSE_PORT", "CLICKHOUSE_HTTP_PORT"):
        monkeypatch.delenv(key, raising=False)

    class HistoryOnlyLoader:
        def fetch_history(self, stock_code, start_date=None, end_date=None, num_records=None, adjust="qfq", period="daily"):
            return normalize_ohlcv_frame(
                pd.DataFrame(
                    {
                        "trade_date": pd.to_datetime(["2024-01-02"]),
                        "open": [10.0],
                        "high": [10.8],
                        "low": [9.8],
                        "close": [10.5],
                        "volume": [1200000],
                    }
                ),
                stock_code=stock_code,
                market="CN",
                frequency=period,
                adjust=adjust,
                source="unit_test",
                currency="CNY",
            )

        def fetch_info(self, stock_code):
            raise AssertionError("sync-cn should not fetch stock_info unless include_stock_info=True")

    with tempfile.TemporaryDirectory() as tmp_dir:
        service = MarketDataService(base_dir=tmp_dir)
        service.cn_loader = HistoryOnlyLoader()
        try:
            summary = service.bulk_sync_cn_history(
                stock_codes=["600000.SH"],
                start_date="2024-01-01",
                end_date="2024-01-05",
                max_workers=1,
                show_progress=False,
            )
            assert summary["success_count"] == 1
            assert summary["failed_count"] == 0
            assert summary["rows_written"] == 1
        finally:
            service.close()


def test_bulk_sync_cn_history_flushes_batches_before_completion(monkeypatch):
    from data.ingest.service import MarketDataService

    for key in ("CLICKHOUSE_HOST", "CLICKHOUSE_PORT", "CLICKHOUSE_HTTP_PORT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CN_SYNC_FLUSH_STOCKS", "2")
    monkeypatch.setenv("CN_SYNC_FLUSH_ROWS", "1000")

    class HistoryOnlyLoader:
        def fetch_history(self, stock_code, start_date=None, end_date=None, num_records=None, adjust="qfq", period="daily"):
            return normalize_ohlcv_frame(
                pd.DataFrame(
                    {
                        "trade_date": pd.to_datetime(["2024-01-02"]),
                        "open": [10.0],
                        "high": [10.8],
                        "low": [9.8],
                        "close": [10.5],
                        "volume": [1200000],
                    }
                ),
                stock_code=stock_code,
                market="CN",
                frequency=period,
                adjust=adjust,
                source="unit_test",
                currency="CNY",
            )

    with tempfile.TemporaryDirectory() as tmp_dir:
        service = MarketDataService(base_dir=tmp_dir)
        service.cn_loader = HistoryOnlyLoader()
        original_append = service.warehouse.append_ohlcv
        append_stock_counts = []

        def recording_append(frame, *args, **kwargs):
            append_stock_counts.append(frame["stock_code"].nunique())
            return original_append(frame, *args, **kwargs)

        monkeypatch.setattr(service.warehouse, "append_ohlcv", recording_append)
        try:
            summary = service.bulk_sync_cn_history(
                stock_codes=["600000.SH", "000001.SZ", "000002.SZ"],
                start_date="2024-01-01",
                end_date="2024-01-05",
                max_workers=1,
                show_progress=False,
                compact_after=False,
            )

            assert summary["success_count"] == 3
            assert summary["rows_written"] == 3
            assert summary["write_flush_count"] == 2
            assert summary["source_counts"]["unit_test"] == 3
            assert append_stock_counts == [2, 1]
        finally:
            service.close()


def test_bulk_sync_cn_history_show_progress_uses_quiet_fetcher(monkeypatch, capsys):
    from data.ingest.service import MarketDataService

    for key in ("CLICKHOUSE_HOST", "CLICKHOUSE_PORT", "CLICKHOUSE_HTTP_PORT"):
        monkeypatch.delenv(key, raising=False)

    class QuietAwareLoader:
        def __init__(self):
            self.verbose_values = []

        def fetch_history(
            self,
            stock_code,
            start_date=None,
            end_date=None,
            num_records=None,
            adjust="qfq",
            period="daily",
            verbose=True,
        ):
            self.verbose_values.append(verbose)
            if verbose:
                print(f"[OK] 成功获取 {stock_code} 的逐股日志")
            return normalize_ohlcv_frame(
                pd.DataFrame(
                    {
                        "trade_date": pd.to_datetime(["2024-01-02"]),
                        "open": [10.0],
                        "high": [10.8],
                        "low": [9.8],
                        "close": [10.5],
                        "volume": [1200000],
                    }
                ),
                stock_code=stock_code,
                market="CN",
                frequency=period,
                adjust=adjust,
                source="unit_test",
                currency="CNY",
            )

    with tempfile.TemporaryDirectory() as tmp_dir:
        service = MarketDataService(base_dir=tmp_dir)
        loader = QuietAwareLoader()
        service.cn_loader = loader
        try:
            summary = service.bulk_sync_cn_history(
                stock_codes=["600000.SH", "000001.SZ"],
                start_date="2024-01-01",
                end_date="2024-01-05",
                max_workers=1,
                show_progress=True,
            )
            captured = capsys.readouterr()

            assert summary["success_count"] == 2
            assert loader.verbose_values == [False, False]
            assert "[OK] 成功获取" not in captured.out
            assert "A 股批量下载完成" in captured.out
        finally:
            service.close()


def test_bulk_sync_cn_history_uses_incremental_overlap_when_existing_data(monkeypatch):
    from data.ingest.service import MarketDataService

    for key in ("CLICKHOUSE_HOST", "CLICKHOUSE_PORT", "CLICKHOUSE_HTTP_PORT"):
        monkeypatch.delenv(key, raising=False)

    class RecordingLoader:
        def __init__(self):
            self.start_dates = []

        def fetch_history(
            self,
            stock_code,
            start_date=None,
            end_date=None,
            num_records=None,
            adjust="qfq",
            period="daily",
            verbose=True,
        ):
            self.start_dates.append(start_date)
            return normalize_ohlcv_frame(
                pd.DataFrame(
                    {
                        "trade_date": pd.to_datetime(["2024-01-10"]),
                        "open": [10.0],
                        "high": [10.8],
                        "low": [9.8],
                        "close": [10.5],
                        "volume": [1200000],
                    }
                ),
                stock_code=stock_code,
                market="CN",
                frequency=period,
                adjust=adjust,
                source="unit_test",
                currency="CNY",
            )

    with tempfile.TemporaryDirectory() as tmp_dir:
        service = MarketDataService(base_dir=tmp_dir, data_source="akshare")
        loader = RecordingLoader()
        service.cn_loader = loader
        try:
            service.warehouse.upsert_ohlcv(
                normalize_ohlcv_frame(
                    pd.DataFrame(
                        {
                            "trade_date": pd.to_datetime(["2024-01-08"]),
                            "open": [9.0],
                            "high": [9.8],
                            "low": [8.8],
                            "close": [9.5],
                            "volume": [1000000],
                        }
                    ),
                    stock_code="600000.SH",
                    market="CN",
                    frequency="daily",
                    adjust="qfq",
                    source="unit_test",
                    currency="CNY",
                )
            )
            summary = service.bulk_sync_cn_history(
                stock_codes=["600000.SH"],
                start_date="2014-01-01",
                end_date="2024-01-10",
                max_workers=1,
                show_progress=False,
            )

            assert summary["success_count"] == 1
            assert loader.start_dates == ["2024-01-01"]
        finally:
            service.close()


def test_refresh_cn_stock_info_show_progress_uses_quiet_fetcher(monkeypatch, capsys):
    from data.ingest import service as service_module
    from data.ingest.service import MarketDataService

    for key in ("CLICKHOUSE_HOST", "CLICKHOUSE_PORT", "CLICKHOUSE_HTTP_PORT"):
        monkeypatch.delenv(key, raising=False)

    verbose_values = []

    class FakeInfoFetcher:
        def __init__(self, stock_code, data_source=None, verbose=True):
            self.stock_code = stock_code
            self.last_successful_source = "unit_test_info"
            verbose_values.append(verbose)

        def fetch(self):
            if verbose_values[-1]:
                print(f"[OK] 基本信息获取成功 {self.stock_code}")
            return {
                "name": "浦发银行",
                "current_price": 11.0,
                "amount": 14000000,
                "turnover_rate": 0.8,
                "market_cap": 320000000000,
                "pe_ratio": 6.5,
                "pb_ratio": 0.7,
            }

    monkeypatch.setattr(service_module, "CNStockInfoFetcher", FakeInfoFetcher)

    with tempfile.TemporaryDirectory() as tmp_dir:
        service = MarketDataService(base_dir=tmp_dir)
        try:
            summary = service.refresh_cn_stock_info(
                stock_codes=["600000.SH", "000001.SZ"],
                max_workers=1,
                show_progress=True,
            )
            captured = capsys.readouterr()

            assert summary["success_count"] == 2
            assert verbose_values == [False, False]
            assert "[OK] 基本信息获取成功" not in captured.out
            assert "A 股 stock_info 刷新完成" in captured.out
        finally:
            service.close()


def test_bulk_sync_cn_history_complete_data_runs_metadata_stages(monkeypatch):
    from data.ingest.service import MarketDataService

    for key in ("CLICKHOUSE_HOST", "CLICKHOUSE_PORT", "CLICKHOUSE_HTTP_PORT"):
        monkeypatch.delenv(key, raising=False)

    class HistoryOnlyLoader:
        def fetch_history(self, stock_code, start_date=None, end_date=None, num_records=None, adjust="qfq", period="daily"):
            return normalize_ohlcv_frame(
                pd.DataFrame(
                    {
                        "trade_date": pd.to_datetime(["2024-01-02"]),
                        "open": [10.0],
                        "high": [10.8],
                        "low": [9.8],
                        "close": [10.5],
                        "volume": [1200000],
                    }
                ),
                stock_code=stock_code,
                market="CN",
                frequency=period,
                adjust=adjust,
                source="unit_test",
                currency="CNY",
            )

    with tempfile.TemporaryDirectory() as tmp_dir:
        service = MarketDataService(base_dir=tmp_dir)
        service.cn_loader = HistoryOnlyLoader()
        calls = []

        def fake_stock_info(**kwargs):
            calls.append(("stock_info", kwargs))
            return {"success_count": len(kwargs["stock_codes"]), "failed_count": 0}

        def fake_financial(**kwargs):
            calls.append(("financial", kwargs))
            return {"valuation_snapshot": {"rows": len(kwargs["stock_codes"])}, "failed_count": 0}

        def fake_industry(**kwargs):
            calls.append(("industry", kwargs))
            return {"updated_count": len(kwargs["stock_codes"])}

        service.refresh_cn_stock_info = fake_stock_info
        service.refresh_cn_financial_metrics = fake_financial
        service.backfill_cn_industry = fake_industry
        try:
            summary = service.bulk_sync_cn_history(
                stock_codes=["600000.SH"],
                start_date="2024-01-01",
                end_date="2024-01-05",
                max_workers=1,
                complete_data=True,
                show_progress=False,
            )
            assert summary["success_count"] == 1
            assert [name for name, _ in calls] == ["stock_info", "financial", "industry"]
            assert calls[0][1]["stock_codes"] == ["600000.SH"]
            assert summary["completion"]["failed_stages"] == []
        finally:
            service.close()


def test_cn_service_sync_refresh_and_coverage(monkeypatch):
    from data.ingest import service as service_module
    from data.ingest.service import MarketDataService

    for key in ("CLICKHOUSE_HOST", "CLICKHOUSE_PORT", "CLICKHOUSE_HTTP_PORT"):
        monkeypatch.delenv(key, raising=False)

    class FakeCNLoader:
        def fetch_history(self, stock_code, start_date=None, end_date=None, num_records=None, adjust="qfq", period="daily"):
            return normalize_ohlcv_frame(
                pd.DataFrame(
                    {
                        "trade_date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
                        "open": [10.0, 10.5, 10.8],
                        "high": [10.8, 11.0, 11.2],
                        "low": [9.8, 10.4, 10.6],
                        "close": [10.5, 10.9, 11.0],
                        "volume": [1200000, 1300000, 1250000],
                    }
                ),
                stock_code=stock_code,
                market="CN",
                frequency=period,
                adjust=adjust,
                source="unit_test",
                currency="CNY",
            )

        def fetch_info(self, stock_code):
            return normalize_stock_info(
                {
                    "name": "浦发银行",
                    "current_price": 11.0,
                    "amount": 14000000,
                    "turnover_rate": 0.8,
                    "market_cap": 320000000000,
                    "pe_ratio": 6.5,
                    "pb_ratio": 0.7,
                    "total_shares": 29000000000,
                    "circulating_shares": 29000000000,
                },
                stock_code=stock_code,
                market="CN",
                source="unit_test",
            )

    class FakeInfoFetcher:
        def __init__(self, stock_code, data_source=None):
            self.stock_code = stock_code
            self.last_successful_source = "unit_test_info"

        def fetch(self):
            return {
                "name": "浦发银行",
                "current_price": 11.0,
                "amount": 14000000,
                "turnover_rate": 0.8,
                "market_cap": 320000000000,
                "pe_ratio": 6.5,
                "pb_ratio": 0.7,
                "total_shares": 29000000000,
                "circulating_shares": 29000000000,
            }

    class FakeIndustryFetcher:
        def __init__(self, verbose=True):
            self.verbose = verbose

        def fetch(self, stock_codes=None):
            return pd.DataFrame(
                [
                    {
                        "stock_code": "600000.SH",
                        "name": "浦发银行",
                        "industry_l1": "申万一级行业",
                        "industry_l2": "银行",
                        "industry_source": "baostock",
                    }
                ]
            )

    class FakeFinancialFetcher:
        def __init__(self, stock_code, verbose=True):
            self.stock_code = stock_code
            self.verbose = verbose

        def fetch_latest(self, year=None, quarter=None):
            return {
                "report_date": "2024-03-31",
                "announce_date": "2024-04-30",
                "period_type": "quarterly",
                "revenue": 6500000000,
                "net_profit": 1200000000,
                "roe": 8.5,
                "net_profit_yoy": 12.0,
            }

    monkeypatch.setattr(service_module, "CNStockInfoFetcher", FakeInfoFetcher)
    monkeypatch.setattr(service_module, "CNBaoStockIndustryFetcher", FakeIndustryFetcher)
    monkeypatch.setattr(service_module, "CNBaoStockFinancialFetcher", FakeFinancialFetcher)

    with tempfile.TemporaryDirectory() as tmp_dir:
        service = MarketDataService(base_dir=tmp_dir)
        service.cn_loader = FakeCNLoader()
        try:
            sync_summary = service.bulk_sync_cn_history(
                stock_codes=["600000.SH"],
                start_date="2024-01-01",
                end_date="2024-01-05",
                max_workers=1,
                show_progress=False,
            )
            assert sync_summary["success_count"] == 1
            assert sync_summary["rows_written"] == 3

            info_summary = service.refresh_cn_stock_info(stock_codes=["600000.SH"], max_workers=1)
            assert info_summary["success_count"] == 1

            industry_summary = service.backfill_cn_industry(stock_codes=["600000.SH"])
            assert industry_summary["updated_count"] == 1

            financial_summary = service.refresh_cn_financial_metrics(stock_codes=["600000.SH"], max_workers=1)
            assert financial_summary["valuation_snapshot"]["rows"] == 1
            assert financial_summary["financial_statement_metrics"]["rows"] == 1

            report = service.cn_backtest_coverage_report(stock_codes=["600000.SH"], min_ohlcv_rows=3)
            assert report["market"] == "CN"
            assert report["ohlcv"]["covered_stock_count"] == 1
            assert report["stock_info"]["coverage"]["pb_ratio"]["non_null_count"] == 1
            assert report["industry"]["industry_l2_count"] == 1
            assert report["financial"]["valuation_stock_count"] == 1
            assert report["backtest_ready"] is True, report
        finally:
            service.close()


def test_stock_analyzer_market_cn_reads_cn_partition(monkeypatch):
    from core import StockAnalyzer

    for key in ("CLICKHOUSE_HOST", "CLICKHOUSE_PORT", "CLICKHOUSE_HTTP_PORT"):
        monkeypatch.delenv(key, raising=False)

    with tempfile.TemporaryDirectory() as tmp_assets:
        layout = DataLayout(base_dir=str(Path(tmp_assets) / "data"))
        warehouse = MarketDataWarehouse(layout)
        try:
            warehouse.upsert_ohlcv(
                normalize_ohlcv_frame(
                    pd.DataFrame(
                        {
                            "trade_date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
                            "open": [10.0, 10.5],
                            "high": [10.8, 11.0],
                            "low": [9.8, 10.4],
                            "close": [10.5, 10.9],
                            "volume": [1200000, 1300000],
                        }
                    ),
                    stock_code="600000.SH",
                    market="CN",
                    source="unit_test",
                )
            )
            warehouse.upsert_ohlcv(
                normalize_ohlcv_frame(
                    pd.DataFrame(
                        {
                            "trade_date": pd.to_datetime(["2024-01-02"]),
                            "open": [70.0],
                            "high": [71.0],
                            "low": [69.0],
                            "close": [70.5],
                            "volume": [1000000],
                        }
                    ),
                    stock_code="00005",
                    market="HK",
                    source="unit_test",
                )
            )
        finally:
            warehouse.close()

        cn_analyzer = StockAnalyzer(db_dir=tmp_assets, market="CN")
        try:
            assert cn_analyzer.get_all_stocks() == ["600000.SH"]
            cn_data = cn_analyzer.load_stock_data("600000.SH", days=30, end_date="2024-01-10")
            assert cn_data is not None
            assert len(cn_data) == 2
            assert cn_data["Close"].iloc[-1] == 10.9
        finally:
            cn_analyzer.close()

        hk_analyzer = StockAnalyzer(db_dir=tmp_assets)
        try:
            assert hk_analyzer.get_all_stocks() == ["00005"]
        finally:
            hk_analyzer.close()


def test_generate_factors_accepts_cn_market(monkeypatch):
    from cli import generate_factors as generate_module

    calls = {}

    class FakeService:
        def __init__(self, *args, **kwargs):
            calls["service_init"] = kwargs

        def get_all_stock_codes(self, **kwargs):
            calls["get_all_stock_codes"] = kwargs
            return ["600000.SH", "000001.SZ"]

        def generate_factor_set(self, **kwargs):
            calls["generate_factor_set"] = kwargs
            return {
                "stock_count": len(kwargs["stock_codes"]),
                "success_count": len(kwargs["stock_codes"]),
                "skipped_count": 0,
                "empty_count": 0,
                "error_count": 0,
                "rows_written": 4,
                "warmup_days": 545,
                "results": [],
                "dataset_path": "memory://features",
                "factor_materialization": {},
            }

        def close(self):
            calls["closed"] = True

    monkeypatch.setattr(generate_module, "MarketDataService", FakeService)
    monkeypatch.setattr(
        generate_module,
        "_write_run_manifest",
        lambda **kwargs: (Path("/tmp/fake_manifest.json"), {}),
    )

    result = generate_module.main_generate_factors(
        days=365,
        factor_set="alpha_zoo_hk",
        market="CN",
        stock_limit=1,
    )

    assert result["stock_count"] == 1
    assert calls["get_all_stock_codes"]["market"] == "CN"
    assert calls["generate_factor_set"]["market"] == "CN"
    assert calls["generate_factor_set"]["stock_codes"] == ["600000.SH"]
    assert calls["closed"] is True


def test_select_accepts_cn_market(monkeypatch):
    from cli import select_stocks as select_module

    calls = {}

    class FakeAnalyzer:
        _default_analyze_stock_factors = object()

        def __init__(self, *args, **kwargs):
            calls["analyzer_init"] = kwargs

        def get_all_stocks(self):
            return ["600000.SH", "000001.SZ"]

        def backtest_portfolio(self, **kwargs):
            calls["backtest_portfolio"] = kwargs
            return {
                "analysis_results": [],
                "selected": [],
                "ranking": [],
                "watchlist": [],
                "liquidity_capacity": [],
                "tca_simulated_report": [],
                "top_n": kwargs["top_n"],
                "estimated_portfolio_return": 0.0,
                "estimated_portfolio_win_rate": 0.0,
                "estimated_trade_count": 0,
            }

        def close(self):
            calls["closed"] = True

    monkeypatch.setattr(select_module, "StockAnalyzer", FakeAnalyzer)
    monkeypatch.setattr(
        select_module,
        "_write_run_manifest",
        lambda **kwargs: (Path("/tmp/fake_manifest.json"), {}),
    )

    result = select_module.main_select_stocks(
        market="CN",
        analysis_mode="lightgbm",
        stock_codes=["600000.SH", "000001.SZ"],
        top_n=2,
        min_daily_turnover=None,
    )

    assert result["top_n"] == 2
    assert calls["analyzer_init"]["market"] == "CN"
    assert calls["backtest_portfolio"]["stock_codes"] == ["600000.SH", "000001.SZ"]
    assert calls["closed"] is True
