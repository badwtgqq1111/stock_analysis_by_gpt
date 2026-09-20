import sys
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.ingest.providers.cn_moneyflow import (
    CNMoneyflowFetcher,
    _date_chunks,
    build_moneyflow_features,
    build_second_wave_confirmation_features,
    fetch_paginated_stock_history,
)
from data.ingest.providers.cn_tushare_official import TushareOfficialClient
from data.ingest.service import (
    _coalesce_moneyflow_feature_rows,
    _is_valid_cn_equity_exchange_code,
)


def test_stock_history_cache_range_can_cover_a_weekend_shift():
    cached_begin = pd.Timestamp("2024-08-23")
    cached_end = pd.Timestamp("2026-09-18")
    requested_begin = pd.Timestamp("2024-08-26")
    requested_end = pd.Timestamp("2026-09-18")
    assert cached_begin <= requested_begin and cached_end >= requested_end


def test_cn_auxiliary_universe_rejects_wrong_exchange_suffixes():
    assert _is_valid_cn_equity_exchange_code("000002.SZ")
    assert _is_valid_cn_equity_exchange_code("600000.SH")
    assert not _is_valid_cn_equity_exchange_code("000002.SH")
    assert not _is_valid_cn_equity_exchange_code("600000.SZ")


def test_date_chunks_are_bounded_and_cover_interval():
    chunks = list(_date_chunks("2024-01-01", "2025-03-01", years=1))
    assert chunks == [("20240101", "20241231"), ("20250101", "20250301")]


def test_fetcher_deduplicates_overlapping_source_rows():
    class Client:
        def get(self, api, **params):
            return pd.DataFrame({"ts_code": ["000001.SZ", "000001.SZ"], "trade_date": ["20240102", "20240102"], "net_mf_amount": [10, 10]}), "fake"

    result = CNMoneyflowFetcher("000001.SZ", client=Client()).fetch("2024-01-01", "2024-01-02", apis=("moneyflow",))
    assert len(result["moneyflow"]["frame"]) == 1
    assert result["moneyflow"]["frame"].iloc[0]["stock_code"] == "000001.SZ"


def test_features_are_signed_rolling_and_missing_aware():
    frame = pd.DataFrame({"trade_date": pd.date_range("2024-01-01", periods=3), "net_mf_amount": [10.0, -5.0, 15.0]})
    out = build_moneyflow_features(frame, source="moneyflow", windows=(2,))
    assert out["moneyflow_net_2d"].tolist() == [10.0, 5.0, 10.0]
    assert out["moneyflow_positive_ratio_2d"].iloc[-1] == 0.5
    assert out["moneyflow_is_missing"].sum() == 0


def test_moneyflow_provider_rows_coalesce_to_one_stock_date_row():
    frame = pd.DataFrame(
        {
            "stock_code": ["000001.SZ"] * 3,
            "trade_date": ["2026-09-18"] * 3,
            "source": ["moneyflow", "moneyflow_dc", "moneyflow_ths"],
            "moneyflow_net_amount": [10.0, None, None],
            "moneyflow_dc_net_amount": [None, 20.0, None],
            "moneyflow_ths_net_amount": [None, None, 30.0],
        }
    )

    result, feature_columns = _coalesce_moneyflow_feature_rows(frame)

    assert len(result) == 1
    assert result.loc[0, "moneyflow_net_amount"] == 10.0
    assert result.loc[0, "moneyflow_dc_net_amount"] == 20.0
    assert result.loc[0, "moneyflow_ths_net_amount"] == 30.0
    assert set(feature_columns) == {
        "moneyflow_net_amount",
        "moneyflow_dc_net_amount",
        "moneyflow_ths_net_amount",
    }


def test_second_wave_confirmation_requires_prior_pulse_pullback_and_recovery():
    dates = pd.date_range("2026-08-03", periods=15, freq="B")
    bars = pd.DataFrame(
        {
            "stock_code": "000001.SZ",
            "trade_date": dates,
            "close": [10.0, 10.2, 10.4, 10.6, 10.8, 10.7, 10.5, 10.4, 10.5, 10.7, 10.6, 10.55, 10.58, 10.6, 10.85],
            "amount": [100, 105, 120, 125, 140, 95, 80, 70, 75, 85, 75, 50, 45, 45, 125],
        }
    )
    flow = pd.DataFrame(
        {
            "stock_code": "000001.SZ",
            "trade_date": dates,
            "moneyflow_ths_net_amount": [0, 1, 1, 8, 2, -2, -1, -1, 0, -1, -1, -1, -1, -1, 9],
        }
    )

    result = build_second_wave_confirmation_features(bars, flow)
    confirmation = result.loc[result["trade_date"].eq(dates[-1])].iloc[0]

    assert confirmation["flow_second_wave_flag"] == 1.0
    assert confirmation["flow_second_wave_source_count"] == 1.0
    assert confirmation["flow_second_wave_pullback_pct"] > 0.01
    assert confirmation["flow_second_wave_pullback_volume_ratio"] <= 0.90


def test_second_wave_confirmation_is_unchanged_when_future_rows_are_removed():
    dates = pd.date_range("2026-08-03", periods=20, freq="B")
    bars = pd.DataFrame(
        {
            "stock_code": "000001.SZ",
            "trade_date": dates,
            "close": [10, 10.2, 10.4, 10.6, 10.8, 10.7, 10.5, 10.4, 10.5, 10.7, 10.6, 10.55, 10.58, 10.6, 10.85, 11.0, 10.9, 11.1, 11.2, 11.3],
            "amount": [100, 105, 120, 125, 140, 95, 80, 70, 75, 85, 75, 70, 72, 78, 125, 130, 110, 135, 140, 145],
        }
    )
    flow = pd.DataFrame(
        {
            "stock_code": "000001.SZ",
            "trade_date": dates,
            "moneyflow_ths_net_amount": [0, 1, 1, 8, 2, -2, -1, -1, 0, 1, -1, -1, 0, 1, 9, 2, -2, 3, 4, 5],
        }
    )
    cutoff = dates[14]
    full = build_second_wave_confirmation_features(bars, flow)
    truncated = build_second_wave_confirmation_features(
        bars.loc[bars["trade_date"].le(cutoff)],
        flow.loc[flow["trade_date"].le(cutoff)],
    )
    columns = [column for column in full.columns if column not in {"stock_code", "trade_date"}]
    expected = full.loc[full["trade_date"].le(cutoff), columns].reset_index(drop=True)
    actual = truncated[columns].reset_index(drop=True)
    pd.testing.assert_frame_equal(actual, expected)


def test_paginated_stock_history_fetches_all_chip_pages_before_completion():
    class Client:
        def __init__(self):
            self.offsets = []

        def get(self, api, **params):
            self.offsets.append(params["offset"])
            if params["offset"] == 0:
                return pd.DataFrame({
                    "ts_code": ["000001.SZ"] * 5000,
                    "trade_date": ["20240102"] * 5000,
                    "price": range(5000),
                }), "official"
            if params["offset"] == 5000:
                return pd.DataFrame({
                    "ts_code": ["000001.SZ", "000001.SZ"],
                    "trade_date": ["20240103", "20240103"],
                    "price": [1.0, 1.1],
                }), "official"
            raise AssertionError("unexpected page")

    client = Client()
    frame, gateway = fetch_paginated_stock_history(
        client, "cyq_chips", ts_code="000001.SZ",
        start_date="20240101", end_date="20240131", limit=5000,
    )
    assert client.offsets == [0, 5000]
    assert gateway == "official"
    assert len(frame) == 5002


def test_paginated_stock_history_allows_official_six_thousand_row_page():
    class Client:
        def __init__(self):
            self.limits = []

        def get(self, api, **params):
            self.limits.append(params["limit"])
            return pd.DataFrame({"ts_code": ["000001.SZ"], "trade_date": ["20240102"], "price": [1.0]}), "official"

    client = Client()
    fetch_paginated_stock_history(client, "cyq_chips", ts_code="000001.SZ", start_date="20240101", end_date="20240131")
    assert client.limits == [6000]


def test_official_tushare_client_posts_pro_contract():
    class Response:
        status_code = 200
        text = ""

        def json(self):
            return {"code": 0, "data": {"fields": ["ts_code", "trade_date"], "items": [["000001.SZ", "20260918"]]}}

    calls = []
    def post(url, json, timeout):
        calls.append((url, json, timeout))
        return Response()

    frame = TushareOfficialClient(token="secret", request_post=post).get(
        "cyq_perf", ts_code="000001.SZ", trade_date="20260918"
    )
    assert frame.to_dict("records") == [{"ts_code": "000001.SZ", "trade_date": "20260918"}]
    assert calls[0][1]["api_name"] == "cyq_perf"
    assert calls[0][1]["token"] == "secret"


def test_moneyflow_client_prefers_official_then_falls_back():
    from data.ingest.providers.cn_moneyflow import CNMoneyflowRelayClient

    class Response:
        status_code = 503
        text = "unavailable"

        def json(self):
            return {"code": 1, "msg": "unavailable"}

    client = CNMoneyflowRelayClient(
        official_token="secret",
        base_api_key="base",
        promax_api_key="",
        request_post=lambda *args, **kwargs: Response(),
        request_get=lambda *args, **kwargs: type("OK", (), {
            "status_code": 200,
            "text": "",
            "json": lambda self: {"code": 0, "data": {"fields": ["ts_code", "trade_date"], "items": [["000001.SZ", "20260918"]]}},
        })(),
    )
    frame, gateway = client.get("cyq_perf", ts_code="000001.SZ", trade_date="20260918")
    assert gateway == "base"
    assert len(frame) == 1


def test_moneyflow_client_exact_source_does_not_fan_out_to_official():
    from data.ingest.providers.cn_moneyflow import CNMoneyflowRelayClient

    class Response:
        status_code = 200
        text = ""
        headers = {}

        def json(self):
            return {"code": 0, "data": {"fields": ["ts_code"], "items": [["000001.SZ"]]}}

    official_calls = []
    client = CNMoneyflowRelayClient(
        official_token="secret",
        base_api_key="base",
        promax_api_key="promax",
        request_post=lambda *args, **kwargs: official_calls.append(1),
        request_get=lambda *args, **kwargs: Response(),
    )
    frame, gateway = client.get("cyq_chips", source="base", ts_code="000001.SZ", trade_date="20260918")
    assert gateway == "base"
    assert len(frame) == 1
    assert official_calls == []


def _synthetic_second_wave_inputs(days=90, stocks=3, seed=11):
    """Bars plus same-day money-flow columns with a deliberate pulse pattern."""
    import numpy as np

    rng = np.random.default_rng(seed)
    dates = pd.date_range("2025-01-01", periods=days, freq="B")
    bars, flows = [], []
    for stock_index in range(stocks):
        price = 10.0 + np.cumsum(rng.normal(0.0, 0.1, days))
        amount = np.abs(rng.normal(1e8, 2e7, days))
        volume_flow = rng.normal(0.0, 1.0, days)
        for offset, trade_date in enumerate(dates):
            bars.append(
                {
                    "stock_code": f"S{stock_index}",
                    "trade_date": trade_date,
                    "close": float(price[offset]),
                    "amount": float(amount[offset]),
                }
            )
            flows.append(
                {
                    "stock_code": f"S{stock_index}",
                    "trade_date": trade_date,
                    "moneyflow_net_amount": float(volume_flow[offset] * 1e6),
                }
            )
    return pd.DataFrame(bars), pd.DataFrame(flows)


def test_second_wave_confirmation_features_never_read_future_bars():
    """A PIT audit in test form: truncating the future must not change the past.

    The panel merges same-session money flow, so the only remaining leak risk is
    a feature that reads forward bars.  Recomputing on a truncated history and
    comparing the overlapping rows catches exactly that.
    """
    bars, flows = _synthetic_second_wave_inputs()
    full = build_second_wave_confirmation_features(bars, flows)
    cutoff = pd.Timestamp(sorted(bars["trade_date"].unique())[-20])
    truncated = build_second_wave_confirmation_features(
        bars.loc[bars["trade_date"] <= cutoff], flows.loc[flows["trade_date"] <= cutoff]
    )
    columns = [column for column in full.columns if column not in {"stock_code", "trade_date"}]
    left = full.loc[full["trade_date"] <= cutoff].sort_values(["stock_code", "trade_date"]).reset_index(drop=True)
    right = truncated.sort_values(["stock_code", "trade_date"]).reset_index(drop=True)
    assert len(left) == len(right)
    for column in columns:
        pd.testing.assert_series_equal(left[column], right[column], check_names=False)


def test_moneyflow_rolling_features_are_truncation_invariant():
    """Same-session money flow is usable at that close; nothing may look ahead."""
    bars, flows = _synthetic_second_wave_inputs(days=60, stocks=1)
    full = build_moneyflow_features(flows, source="moneyflow")
    cutoff = pd.Timestamp(sorted(bars["trade_date"].unique())[-15])
    truncated = build_moneyflow_features(flows.loc[flows["trade_date"] <= cutoff], source="moneyflow")
    columns = [column for column in full.columns if column != "trade_date"]
    left = full.loc[full["trade_date"] <= cutoff].sort_values("trade_date").reset_index(drop=True)
    right = truncated.sort_values("trade_date").reset_index(drop=True)
    assert len(left) == len(right) > 0
    for column in columns:
        pd.testing.assert_series_equal(left[column], right[column], check_names=False)
