#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Qlib 风格 Alpha158 / Alpha360 因子集。"""

import pandas as pd

from factor_engine.base import BaseFactorSet, FactorSetMetadata
from factor_engine.expressions.operators import (
    corr,
    delta,
    greater,
    less,
    log,
    ref,
    safe_divide,
    ts_idxmax,
    ts_idxmin,
    ts_max,
    ts_mean,
    ts_min,
    ts_quantile,
    ts_rank,
    ts_resi,
    ts_rsquare,
    ts_slope,
    ts_std,
    ts_sum,
)
from factor_engine.registry import register_factor_set


DEFAULT_ALPHA158_CONFIG = {
    "kbar": {},
    "price": {
        "windows": [0],
        "feature": ["OPEN", "HIGH", "LOW", "VWAP"],
    },
    "rolling": {},
}


DEFAULT_ALPHA360_WINDOW = 60


def _prepare_qlib_frame(frame):
    """将 clean 层 OHLCV 数据适配为 Qlib 风格字段。"""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "vwap"])

    working = frame.copy()
    rename_mapping = {
        "trade_date": "trade_date",
        "date": "trade_date",
        "Date": "trade_date",
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume",
    }
    working.rename(columns=rename_mapping, inplace=True)
    if "trade_date" not in working.columns:
        if isinstance(working.index, pd.DatetimeIndex):
            working = working.reset_index().rename(columns={working.index.name or "index": "trade_date"})
        else:
            raise ValueError("input frame missing trade_date column")

    working["trade_date"] = pd.to_datetime(working["trade_date"], errors="coerce")
    working.dropna(subset=["trade_date"], inplace=True)
    working.sort_values("trade_date", inplace=True)
    working.drop_duplicates(subset=["trade_date"], keep="last", inplace=True)

    for column in ["open", "high", "low", "close", "volume"]:
        if column not in working.columns:
            raise ValueError(f"input frame missing required column: {column}")
        working[column] = pd.to_numeric(working[column], errors="coerce")

    working.dropna(subset=["open", "high", "low", "close"], inplace=True)
    working["volume"] = working["volume"].fillna(0.0)
    if "vwap" not in working.columns:
        # 当前 clean 层还没有原生成交额，这里先用 OHLC4 代理价兼容 Qlib 的 VWAP 口径。
        working["vwap"] = (working["open"] + working["high"] + working["low"] + working["close"]) / 4.0
    else:
        working["vwap"] = pd.to_numeric(working["vwap"], errors="coerce")
        working["vwap"] = working["vwap"].fillna(
            (working["open"] + working["high"] + working["low"] + working["close"]) / 4.0
        )

    working.set_index("trade_date", inplace=True)
    working.index.name = "trade_date"
    return working[["open", "high", "low", "close", "volume", "vwap"]]


def _alpha158_feature_names(config):
    """生成 Alpha158 风格字段名。"""
    names = []
    if "kbar" in config:
        names.extend(["KMID", "KLEN", "KMID2", "KUP", "KUP2", "KLOW", "KLOW2", "KSFT", "KSFT2"])

    if "price" in config:
        windows = config["price"].get("windows", range(5))
        features = config["price"].get("feature", ["OPEN", "HIGH", "LOW", "CLOSE", "VWAP"])
        for field in features:
            names.extend([field.upper() + str(window) for window in windows])

    if "volume" in config:
        windows = config["volume"].get("windows", range(5))
        names.extend(["VOLUME" + str(window) for window in windows])

    if "rolling" not in config:
        return names

    windows = config["rolling"].get("windows", [5, 10, 20, 30, 60])
    include = config["rolling"].get("include", None)
    exclude = set(config["rolling"].get("exclude", []))

    def use(operator_name):
        return operator_name not in exclude and (include is None or operator_name in include)

    operator_names = [
        ("ROC", "ROC"),
        ("MA", "MA"),
        ("STD", "STD"),
        ("BETA", "BETA"),
        ("RSQR", "RSQR"),
        ("RESI", "RESI"),
        ("MAX", "MAX"),
        ("LOW", "MIN"),
        ("QTLU", "QTLU"),
        ("QTLD", "QTLD"),
        ("RANK", "RANK"),
        ("RSV", "RSV"),
        ("IMAX", "IMAX"),
        ("IMIN", "IMIN"),
        ("IMXD", "IMXD"),
        ("CORR", "CORR"),
        ("CORD", "CORD"),
        ("CNTP", "CNTP"),
        ("CNTN", "CNTN"),
        ("CNTD", "CNTD"),
        ("SUMP", "SUMP"),
        ("SUMN", "SUMN"),
        ("SUMD", "SUMD"),
        ("VMA", "VMA"),
        ("VSTD", "VSTD"),
        ("WVMA", "WVMA"),
        ("VSUMP", "VSUMP"),
        ("VSUMN", "VSUMN"),
        ("VSUMD", "VSUMD"),
    ]
    for operator_name, prefix in operator_names:
        if use(operator_name):
            names.extend([f"{prefix}{window}" for window in windows])
    return names


@register_factor_set("qlib_alpha360")
class Alpha360FactorSet(BaseFactorSet):
    """Qlib 风格 Alpha360。"""

    name = "qlib_alpha360"
    description = "Qlib-style Alpha360 feature set"

    def transform(self, frame, context=None):
        qlib_frame = _prepare_qlib_frame(frame)
        if qlib_frame.empty:
            return pd.DataFrame()

        close = qlib_frame["close"]
        volume = qlib_frame["volume"]
        window = int(self.config.get("window", DEFAULT_ALPHA360_WINDOW))
        columns = {}

        for lag in range(window - 1, -1, -1):
            columns[f"CLOSE{lag}"] = safe_divide(ref(close, lag), close) if lag else safe_divide(close, close)
        for lag in range(window - 1, -1, -1):
            columns[f"OPEN{lag}"] = (
                safe_divide(ref(qlib_frame["open"], lag), close) if lag else safe_divide(qlib_frame["open"], close)
            )
        for lag in range(window - 1, -1, -1):
            columns[f"HIGH{lag}"] = (
                safe_divide(ref(qlib_frame["high"], lag), close) if lag else safe_divide(qlib_frame["high"], close)
            )
        for lag in range(window - 1, -1, -1):
            columns[f"LOW{lag}"] = safe_divide(ref(qlib_frame["low"], lag), close) if lag else safe_divide(qlib_frame["low"], close)
        for lag in range(window - 1, -1, -1):
            columns[f"VWAP{lag}"] = (
                safe_divide(ref(qlib_frame["vwap"], lag), close) if lag else safe_divide(qlib_frame["vwap"], close)
            )
        for lag in range(window - 1, -1, -1):
            columns[f"VOLUME{lag}"] = safe_divide(ref(volume, lag), volume) if lag else safe_divide(volume, volume)
        return pd.DataFrame(columns, index=qlib_frame.index)

    def metadata(self):
        return FactorSetMetadata(
            name=self.name,
            description=self.description,
            version=self.version,
            assumptions=("VWAP is approximated with OHLC4 when not available in clean layer",),
            extra={"window": int(self.config.get("window", DEFAULT_ALPHA360_WINDOW)), "feature_count": DEFAULT_ALPHA360_WINDOW * 6},
        )


@register_factor_set("qlib_alpha158")
class Alpha158FactorSet(BaseFactorSet):
    """Qlib 风格 Alpha158。"""

    name = "qlib_alpha158"
    description = "Qlib-style Alpha158 feature set"

    def transform(self, frame, context=None):
        qlib_frame = _prepare_qlib_frame(frame)
        if qlib_frame.empty:
            return pd.DataFrame()

        config = DEFAULT_ALPHA158_CONFIG | self.config
        close = qlib_frame["close"]
        open_ = qlib_frame["open"]
        high = qlib_frame["high"]
        low = qlib_frame["low"]
        volume = qlib_frame["volume"]
        vwap = qlib_frame["vwap"]
        columns = {}

        if "kbar" in config:
            price_span = high - low
            columns["KMID"] = safe_divide(close - open_, open_)
            columns["KLEN"] = safe_divide(price_span, open_)
            columns["KMID2"] = safe_divide(close - open_, price_span)
            columns["KUP"] = safe_divide(high - greater(open_, close), open_)
            columns["KUP2"] = safe_divide(high - greater(open_, close), price_span)
            columns["KLOW"] = safe_divide(less(open_, close) - low, open_)
            columns["KLOW2"] = safe_divide(less(open_, close) - low, price_span)
            columns["KSFT"] = safe_divide(2 * close - high - low, open_)
            columns["KSFT2"] = safe_divide(2 * close - high - low, price_span)

        if "price" in config:
            price_windows = config["price"].get("windows", range(5))
            features = config["price"].get("feature", ["OPEN", "HIGH", "LOW", "CLOSE", "VWAP"])
            price_map = {
                "OPEN": open_,
                "HIGH": high,
                "LOW": low,
                "CLOSE": close,
                "VWAP": vwap,
            }
            for field in features:
                series = price_map[field.upper()]
                for window in price_windows:
                    column_name = field.upper() + str(window)
                    columns[column_name] = safe_divide(ref(series, window), close) if window else safe_divide(series, close)

        if "volume" in config:
            volume_windows = config["volume"].get("windows", range(5))
            for window in volume_windows:
                column_name = "VOLUME" + str(window)
                columns[column_name] = safe_divide(ref(volume, window), volume) if window else safe_divide(volume, volume)

        if "rolling" in config:
            rolling_windows = config["rolling"].get("windows", [5, 10, 20, 30, 60])
            include = config["rolling"].get("include", None)
            exclude = set(config["rolling"].get("exclude", []))

            def use(operator_name):
                return operator_name not in exclude and (include is None or operator_name in include)

            close_change = delta(close, 1)
            volume_change = delta(volume, 1)
            positive_close_change = close_change.clip(lower=0)
            negative_close_change = (-close_change).clip(lower=0)
            positive_volume_change = volume_change.clip(lower=0)
            negative_volume_change = (-volume_change).clip(lower=0)
            absolute_close_change = close_change.abs()
            absolute_volume_change = volume_change.abs()
            weighted_return = (safe_divide(close, ref(close, 1)) - 1).abs() * volume

            for window in rolling_windows:
                if use("ROC"):
                    columns[f"ROC{window}"] = safe_divide(ref(close, window), close)
                if use("MA"):
                    columns[f"MA{window}"] = safe_divide(ts_mean(close, window), close)
                if use("STD"):
                    columns[f"STD{window}"] = safe_divide(ts_std(close, window), close)
                if use("BETA"):
                    columns[f"BETA{window}"] = safe_divide(ts_slope(close, window), close)
                if use("RSQR"):
                    columns[f"RSQR{window}"] = ts_rsquare(close, window)
                if use("RESI"):
                    columns[f"RESI{window}"] = safe_divide(ts_resi(close, window), close)
                if use("MAX"):
                    columns[f"MAX{window}"] = safe_divide(ts_max(high, window), close)
                if use("LOW"):
                    columns[f"MIN{window}"] = safe_divide(ts_min(low, window), close)
                if use("QTLU"):
                    columns[f"QTLU{window}"] = safe_divide(ts_quantile(close, window, 0.8), close)
                if use("QTLD"):
                    columns[f"QTLD{window}"] = safe_divide(ts_quantile(close, window, 0.2), close)
                if use("RANK"):
                    columns[f"RANK{window}"] = ts_rank(close, window)
                if use("RSV"):
                    columns[f"RSV{window}"] = safe_divide(
                        close - ts_min(low, window),
                        ts_max(high, window) - ts_min(low, window),
                    )
                if use("IMAX"):
                    columns[f"IMAX{window}"] = safe_divide(ts_idxmax(high, window), window)
                if use("IMIN"):
                    columns[f"IMIN{window}"] = safe_divide(ts_idxmin(low, window), window)
                if use("IMXD"):
                    columns[f"IMXD{window}"] = safe_divide(ts_idxmax(high, window) - ts_idxmin(low, window), window)
                if use("CORR"):
                    columns[f"CORR{window}"] = corr(close, log(volume + 1), window)
                if use("CORD"):
                    columns[f"CORD{window}"] = corr(
                        safe_divide(close, ref(close, 1)),
                        log(safe_divide(volume, ref(volume, 1)) + 1),
                        window,
                    )
                if use("CNTP"):
                    columns[f"CNTP{window}"] = ts_mean((close > ref(close, 1)).astype(float), window)
                if use("CNTN"):
                    columns[f"CNTN{window}"] = ts_mean((close < ref(close, 1)).astype(float), window)
                if use("CNTD"):
                    columns[f"CNTD{window}"] = ts_mean((close > ref(close, 1)).astype(float), window) - ts_mean(
                        (close < ref(close, 1)).astype(float), window
                    )
                if use("SUMP"):
                    columns[f"SUMP{window}"] = safe_divide(
                        ts_sum(positive_close_change, window),
                        ts_sum(absolute_close_change, window),
                    )
                if use("SUMN"):
                    columns[f"SUMN{window}"] = safe_divide(
                        ts_sum(negative_close_change, window),
                        ts_sum(absolute_close_change, window),
                    )
                if use("SUMD"):
                    columns[f"SUMD{window}"] = safe_divide(
                        ts_sum(positive_close_change, window) - ts_sum(negative_close_change, window),
                        ts_sum(absolute_close_change, window),
                    )
                if use("VMA"):
                    columns[f"VMA{window}"] = safe_divide(ts_mean(volume, window), volume)
                if use("VSTD"):
                    columns[f"VSTD{window}"] = safe_divide(ts_std(volume, window), volume)
                if use("WVMA"):
                    columns[f"WVMA{window}"] = safe_divide(ts_std(weighted_return, window), ts_mean(weighted_return, window))
                if use("VSUMP"):
                    columns[f"VSUMP{window}"] = safe_divide(
                        ts_sum(positive_volume_change, window),
                        ts_sum(absolute_volume_change, window),
                    )
                if use("VSUMN"):
                    columns[f"VSUMN{window}"] = safe_divide(
                        ts_sum(negative_volume_change, window),
                        ts_sum(absolute_volume_change, window),
                    )
                if use("VSUMD"):
                    columns[f"VSUMD{window}"] = safe_divide(
                        ts_sum(positive_volume_change, window) - ts_sum(negative_volume_change, window),
                        ts_sum(absolute_volume_change, window),
                    )

        expected_columns = _alpha158_feature_names(config)
        return pd.DataFrame(columns, index=qlib_frame.index).reindex(columns=expected_columns)

    def metadata(self):
        config = DEFAULT_ALPHA158_CONFIG | self.config
        return FactorSetMetadata(
            name=self.name,
            description=self.description,
            version=self.version,
            assumptions=("VWAP is approximated with OHLC4 when not available in clean layer",),
            extra={"feature_count": len(_alpha158_feature_names(config)), "config": config},
        )
