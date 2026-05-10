#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""IC / RankIC / 分组收益 / 换手率 / 衰减验证。"""

import math

import numpy as np
import pandas as pd
from scipy import stats


FEATURE_METADATA_COLUMNS = {
    "trade_date",
    "stock_code",
    "market",
    "exchange",
    "asset_type",
    "frequency",
    "adjust",
    "feature_set",
    "feature_name",
    "feature_value",
    "source",
    "ingest_time",
}


class FactorValidator:
    """对 feature 层因子做基础验证。"""

    DEFAULT_HORIZONS = (1, 5, 10, 20)

    def __init__(self, horizons=None, quantiles=5, min_observations=5):
        self.horizons = tuple(int(item) for item in (horizons or self.DEFAULT_HORIZONS))
        self.quantiles = int(quantiles)
        self.min_observations = int(min_observations)

    def validate(self, feature_frame, ohlcv_frame, progress_callback=None):
        """输出验证明细、IC 汇总和分组收益结果。"""
        if progress_callback is None:
            progress_callback = lambda _message: None

        progress_callback("validation frame")
        validation_frame = self.build_validation_frame(feature_frame, ohlcv_frame)
        if validation_frame.empty:
            empty_ic = pd.DataFrame(
                columns=[
                    "feature_set",
                    "feature_name",
                    "trade_date",
                    "horizon",
                    "observation_count",
                    "ic",
                    "ic_pvalue",
                    "rank_ic",
                    "rank_ic_pvalue",
                ]
            )
            empty_ic_summary = pd.DataFrame(
                columns=[
                    "feature_set",
                    "feature_name",
                    "horizon",
                    "valid_dates",
                    "mean_ic",
                    "std_ic",
                    "ic_ir",
                    "ic_positive_rate",
                    "ic_tstat",
                    "ic_pvalue",
                    "mean_rank_ic",
                    "std_rank_ic",
                    "rank_ic_ir",
                    "rank_ic_positive_rate",
                    "rank_ic_tstat",
                    "rank_ic_pvalue",
                ]
            )
            empty_quantile = pd.DataFrame(
                columns=[
                    "feature_set",
                    "feature_name",
                    "trade_date",
                    "horizon",
                    "quantile",
                    "mean_return",
                    "observation_count",
                ]
            )
            empty_quantile_membership = pd.DataFrame(
                columns=[
                    "feature_set",
                    "feature_name",
                    "trade_date",
                    "stock_code",
                    "quantile",
                ]
            )
            empty_quantile_summary = pd.DataFrame(
                columns=[
                    "feature_set",
                    "feature_name",
                    "horizon",
                    "quantile",
                    "mean_return",
                    "avg_observation_count",
                    "valid_dates",
                ]
            )
            empty_long_short = pd.DataFrame(
                columns=[
                    "feature_set",
                    "feature_name",
                    "trade_date",
                    "horizon",
                    "top_quantile",
                    "bottom_quantile",
                    "spread",
                ]
            )
            empty_long_short_summary = pd.DataFrame(
                columns=[
                    "feature_set",
                    "feature_name",
                    "horizon",
                    "mean_spread",
                    "std_spread",
                    "spread_ir",
                    "positive_rate",
                    "valid_dates",
                ]
            )
            empty_turnover = pd.DataFrame(
                columns=[
                    "feature_set",
                    "feature_name",
                    "trade_date",
                    "quantile",
                    "prev_count",
                    "current_count",
                    "retained_count",
                    "entered_count",
                    "exited_count",
                    "turnover_rate",
                ]
            )
            empty_turnover_summary = pd.DataFrame(
                columns=[
                    "feature_set",
                    "feature_name",
                    "quantile",
                    "mean_turnover",
                    "std_turnover",
                    "min_turnover",
                    "max_turnover",
                    "valid_dates",
                ]
            )
            empty_decay = pd.DataFrame(
                columns=[
                    "feature_set",
                    "feature_name",
                    "horizon",
                    "mean_ic",
                    "mean_rank_ic",
                    "mean_spread",
                    "ic_decay_ratio",
                    "rank_ic_decay_ratio",
                    "spread_decay_ratio",
                    "base_horizon",
                ]
            )
            return {
                "validation_frame": validation_frame,
                "ic_by_date": empty_ic,
                "ic_summary": empty_ic_summary,
                "quantile_returns_by_date": empty_quantile,
                "quantile_membership_by_date": empty_quantile_membership,
                "quantile_summary": empty_quantile_summary,
                "long_short_by_date": empty_long_short,
                "long_short_summary": empty_long_short_summary,
                "turnover_by_date": empty_turnover,
                "turnover_summary": empty_turnover_summary,
                "decay_summary": empty_decay,
            }

        progress_callback("quantile membership")
        quantile_membership_by_date = self.calculate_quantile_membership_by_date(validation_frame)
        ic_frames = []
        quantile_frames = []
        long_short_frames = []
        for horizon in self.horizons:
            progress_callback(f"horizon {int(horizon)} ic")
            ic_frames.append(self.calculate_ic_by_date(validation_frame, horizon=horizon))
            progress_callback(f"horizon {int(horizon)} quantiles")
            quantile_by_date, long_short_by_date = self.calculate_quantile_returns_by_date(
                validation_frame,
                quantile_membership_by_date=quantile_membership_by_date,
                horizon=horizon,
            )
            quantile_frames.append(quantile_by_date)
            long_short_frames.append(long_short_by_date)

        progress_callback("turnover")
        ic_by_date = pd.concat(ic_frames, ignore_index=True) if ic_frames else pd.DataFrame()
        quantile_returns_by_date = pd.concat(quantile_frames, ignore_index=True) if quantile_frames else pd.DataFrame()
        long_short_by_date = pd.concat(long_short_frames, ignore_index=True) if long_short_frames else pd.DataFrame()
        turnover_by_date = self.calculate_turnover_by_date(quantile_membership_by_date)
        progress_callback("summaries")
        ic_summary = self.summarize_ic(ic_by_date)
        long_short_summary = self.summarize_long_short(long_short_by_date)

        return {
            "validation_frame": validation_frame,
            "ic_by_date": ic_by_date,
            "ic_summary": ic_summary,
            "quantile_returns_by_date": quantile_returns_by_date,
            "quantile_membership_by_date": quantile_membership_by_date,
            "quantile_summary": self.summarize_quantiles(quantile_returns_by_date),
            "long_short_by_date": long_short_by_date,
            "long_short_summary": long_short_summary,
            "turnover_by_date": turnover_by_date,
            "turnover_summary": self.summarize_turnover(turnover_by_date),
            "decay_summary": self.summarize_decay(ic_summary, long_short_summary),
        }

    def build_validation_frame(self, feature_frame, ohlcv_frame):
        """将因子值与前瞻收益拼接成验证底表。"""
        feature_long = self._coerce_feature_frame(feature_frame)
        if feature_long.empty:
            return feature_long

        returns_frame = self.compute_forward_returns(ohlcv_frame, horizons=self.horizons)
        if returns_frame.empty:
            return pd.DataFrame()

        merge_keys = [
            column
            for column in ["trade_date", "stock_code", "market", "exchange", "asset_type", "frequency", "adjust"]
            if column in feature_long.columns and column in returns_frame.columns
        ]
        merged = feature_long.merge(
            returns_frame,
            on=merge_keys,
            how="inner",
        )
        merged.sort_values(["feature_set", "feature_name", "trade_date", "stock_code"], inplace=True)
        merged.reset_index(drop=True, inplace=True)
        return merged

    @classmethod
    def compute_forward_returns(cls, ohlcv_frame, horizons=None):
        """按股票计算多周期前瞻收益。"""
        if ohlcv_frame is None or ohlcv_frame.empty:
            return pd.DataFrame()

        working = cls._coerce_ohlcv_frame(ohlcv_frame)
        if working.empty:
            return working

        horizon_values = tuple(int(item) for item in (horizons or cls.DEFAULT_HORIZONS))
        identity_columns = [
            column
            for column in ["stock_code", "market", "exchange", "asset_type", "frequency", "adjust"]
            if column in working.columns
        ]
        working.sort_values(identity_columns + ["trade_date"], inplace=True)
        group_keys = identity_columns if identity_columns else ["stock_code"]

        for horizon in horizon_values:
            future_close = working.groupby(group_keys, dropna=False)["close"].shift(-horizon)
            working[f"forward_return_{horizon}"] = future_close / working["close"] - 1.0

        keep_columns = identity_columns + ["trade_date"] + [f"forward_return_{horizon}" for horizon in horizon_values]
        working = working[keep_columns].copy()
        working.reset_index(drop=True, inplace=True)
        return working

    def calculate_ic_by_date(self, validation_frame, horizon):
        """按交易日计算横截面 IC / RankIC。"""
        empty_result = pd.DataFrame(
            columns=[
                "feature_set",
                "feature_name",
                "trade_date",
                "horizon",
                "observation_count",
                "ic",
                "ic_pvalue",
                "rank_ic",
                "rank_ic_pvalue",
            ]
        )
        if validation_frame is None or validation_frame.empty:
            return empty_result

        target_column = f"forward_return_{int(horizon)}"
        if target_column not in validation_frame.columns:
            raise ValueError(f"validation frame missing target column: {target_column}")

        group_keys = ["feature_set", "feature_name", "trade_date"]
        sample = validation_frame[group_keys + ["feature_value", target_column]].dropna(
            subset=["feature_value", target_column]
        )
        if sample.empty:
            return empty_result

        grouped = sample.groupby(group_keys, dropna=False)
        summary = grouped.agg(
            observation_count=("feature_value", "size"),
            feature_unique=("feature_value", "nunique"),
            target_unique=(target_column, "nunique"),
            sum_x=("feature_value", "sum"),
            sum_y=(target_column, "sum"),
            sum_xx=("feature_value", lambda s: np.square(s).sum()),
            sum_yy=(target_column, lambda s: np.square(s).sum()),
            sum_xy=(target_column, lambda s: (s * sample.loc[s.index, "feature_value"]).sum()),
        ).reset_index()

        valid_mask = (
            (summary["observation_count"] >= self.min_observations)
            & (summary["feature_unique"] > 1)
            & (summary["target_unique"] > 1)
        )

        numerator = summary["observation_count"] * summary["sum_xy"] - summary["sum_x"] * summary["sum_y"]
        denominator_term = np.clip(
            (summary["observation_count"] * summary["sum_xx"] - np.square(summary["sum_x"]))
            * (summary["observation_count"] * summary["sum_yy"] - np.square(summary["sum_y"])),
            a_min=0,
            a_max=None,
        )
        denominator = np.sqrt(denominator_term)
        summary["ic"] = np.where(valid_mask & (denominator > 0), numerator / denominator, np.nan)
        summary["ic"] = np.clip(summary["ic"], -1.0, 1.0)

        rank_sample = sample.copy()
        rank_sample["feature_rank"] = grouped["feature_value"].rank(method="average")
        rank_sample["target_rank"] = grouped[target_column].rank(method="average")
        rank_grouped = rank_sample.groupby(group_keys, dropna=False)
        rank_summary = rank_grouped.agg(
            sum_x=("feature_rank", "sum"),
            sum_y=("target_rank", "sum"),
            sum_xx=("feature_rank", lambda s: np.square(s).sum()),
            sum_yy=("target_rank", lambda s: np.square(s).sum()),
            sum_xy=("target_rank", lambda s: (s * rank_sample.loc[s.index, "feature_rank"]).sum()),
        ).reset_index()
        summary = summary.merge(rank_summary, on=group_keys, how="left", suffixes=("", "_rank"))

        rank_numerator = (
            summary["observation_count"] * summary["sum_xy_rank"] - summary["sum_x_rank"] * summary["sum_y_rank"]
        )
        rank_denominator_term = np.clip(
            (summary["observation_count"] * summary["sum_xx_rank"] - np.square(summary["sum_x_rank"]))
            * (summary["observation_count"] * summary["sum_yy_rank"] - np.square(summary["sum_y_rank"])),
            a_min=0,
            a_max=None,
        )
        rank_denominator = np.sqrt(rank_denominator_term)
        summary["rank_ic"] = np.where(valid_mask & (rank_denominator > 0), rank_numerator / rank_denominator, np.nan)
        summary["rank_ic"] = np.clip(summary["rank_ic"], -1.0, 1.0)

        df = summary["observation_count"] - 2
        ic_abs = np.abs(summary["ic"])
        ic_t = np.where(
            valid_mask & summary["ic"].notna() & (df > 0) & (ic_abs < 1),
            summary["ic"] * np.sqrt(df / (1 - np.square(summary["ic"]))),
            np.nan,
        )
        rank_ic_abs = np.abs(summary["rank_ic"])
        rank_ic_t = np.where(
            valid_mask & summary["rank_ic"].notna() & (df > 0) & (rank_ic_abs < 1),
            summary["rank_ic"] * np.sqrt(df / (1 - np.square(summary["rank_ic"]))),
            np.nan,
        )
        summary["ic_pvalue"] = np.where(
            valid_mask & summary["ic"].notna(),
            np.where(ic_abs >= 1, 0.0, 2 * stats.t.sf(np.abs(ic_t), df)),
            np.nan,
        )
        summary["rank_ic_pvalue"] = np.where(
            valid_mask & summary["rank_ic"].notna(),
            np.where(rank_ic_abs >= 1, 0.0, 2 * stats.t.sf(np.abs(rank_ic_t), df)),
            np.nan,
        )

        result = summary[
            group_keys + ["observation_count", "ic", "ic_pvalue", "rank_ic", "rank_ic_pvalue"]
        ].copy()
        result["horizon"] = int(horizon)
        result = result[
            [
                "feature_set",
                "feature_name",
                "trade_date",
                "horizon",
                "observation_count",
                "ic",
                "ic_pvalue",
                "rank_ic",
                "rank_ic_pvalue",
            ]
        ]
        result["trade_date"] = pd.to_datetime(result["trade_date"], errors="coerce")
        result.sort_values(["feature_set", "feature_name", "trade_date"], inplace=True)
        result.reset_index(drop=True, inplace=True)
        return result

    def summarize_ic(self, ic_by_date):
        """汇总 IC / RankIC 统计。"""
        if ic_by_date is None or ic_by_date.empty:
            return pd.DataFrame(
                columns=[
                    "feature_set",
                    "feature_name",
                    "horizon",
                    "valid_dates",
                    "mean_ic",
                    "std_ic",
                    "ic_ir",
                    "ic_positive_rate",
                    "ic_tstat",
                    "ic_pvalue",
                    "mean_rank_ic",
                    "std_rank_ic",
                    "rank_ic_ir",
                    "rank_ic_positive_rate",
                    "rank_ic_tstat",
                    "rank_ic_pvalue",
                ]
            )

        working = ic_by_date.copy()
        working["ic"] = pd.to_numeric(working["ic"], errors="coerce")
        working["rank_ic"] = pd.to_numeric(working["rank_ic"], errors="coerce")
        group_keys = ["feature_set", "feature_name", "horizon"]

        ic_summary = working.groupby(group_keys, dropna=False)["ic"].agg(
            valid_dates="count",
            mean_ic="mean",
            std_ic=lambda s: s.std(ddof=1),
            ic_positive_rate=lambda s: (s > 0).mean(),
        )
        rank_summary = working.groupby(group_keys, dropna=False)["rank_ic"].agg(
            mean_rank_ic="mean",
            std_rank_ic=lambda s: s.std(ddof=1),
            rank_ic_positive_rate=lambda s: (s > 0).mean(),
        )
        result = ic_summary.join(rank_summary).reset_index()

        valid_ic = (result["valid_dates"] > 1) & result["std_ic"].notna() & (result["std_ic"] != 0)
        result["ic_ir"] = np.where(
            valid_ic,
            result["mean_ic"] / result["std_ic"] * np.sqrt(result["valid_dates"]),
            np.nan,
        )
        result["ic_tstat"] = np.where(
            valid_ic,
            result["mean_ic"] / (result["std_ic"] / np.sqrt(result["valid_dates"])),
            np.nan,
        )
        result["ic_pvalue"] = np.where(
            valid_ic,
            2 * stats.t.sf(np.abs(result["ic_tstat"]), result["valid_dates"] - 1),
            np.nan,
        )

        valid_rank = (result["valid_dates"] > 1) & result["std_rank_ic"].notna() & (result["std_rank_ic"] != 0)
        result["rank_ic_ir"] = np.where(
            valid_rank,
            result["mean_rank_ic"] / result["std_rank_ic"] * np.sqrt(result["valid_dates"]),
            np.nan,
        )
        result["rank_ic_tstat"] = np.where(
            valid_rank,
            result["mean_rank_ic"] / (result["std_rank_ic"] / np.sqrt(result["valid_dates"])),
            np.nan,
        )
        result["rank_ic_pvalue"] = np.where(
            valid_rank,
            2 * stats.t.sf(np.abs(result["rank_ic_tstat"]), result["valid_dates"] - 1),
            np.nan,
        )

        result.sort_values(["feature_set", "feature_name", "horizon"], inplace=True)
        result.reset_index(drop=True, inplace=True)
        return result

    def calculate_quantile_membership_by_date(self, validation_frame):
        """按交易日计算因子分组归属。"""
        empty_membership = pd.DataFrame(
            columns=[
                "feature_set",
                "feature_name",
                "trade_date",
                "stock_code",
                "quantile",
            ]
        )
        if validation_frame is None or validation_frame.empty:
            return empty_membership

        sample = validation_frame[
            ["feature_set", "feature_name", "trade_date", "stock_code", "feature_value"]
        ].dropna(subset=["stock_code", "feature_value"]).drop_duplicates(
            subset=["feature_set", "feature_name", "trade_date", "stock_code"],
            keep="last",
        )
        if sample.empty:
            return empty_membership

        group_keys = ["feature_set", "feature_name", "trade_date"]
        observation_count = sample.groupby(group_keys, dropna=False)["stock_code"].transform("size")
        minimum_required = max(self.min_observations, self.quantiles)
        sample = sample.loc[observation_count >= minimum_required].copy()
        if sample.empty:
            return empty_membership

        rank_pct = sample.groupby(group_keys, dropna=False)["feature_value"].rank(method="first", pct=True)
        sample["quantile"] = np.ceil(rank_pct * self.quantiles).clip(1, self.quantiles).astype(int)

        result = sample[["feature_set", "feature_name", "trade_date", "stock_code", "quantile"]].copy()
        result["trade_date"] = pd.to_datetime(result["trade_date"], errors="coerce")
        result.sort_values(["feature_set", "feature_name", "trade_date", "quantile", "stock_code"], inplace=True)
        result.reset_index(drop=True, inplace=True)
        return result

    def calculate_quantile_returns_by_date(self, validation_frame, horizon, quantile_membership_by_date=None):
        """按交易日计算分组收益与多空价差。"""
        empty_quantile = pd.DataFrame(
            columns=[
                "feature_set",
                "feature_name",
                "trade_date",
                "horizon",
                "quantile",
                "mean_return",
                "observation_count",
            ]
        )
        empty_long_short = pd.DataFrame(
            columns=[
                "feature_set",
                "feature_name",
                "trade_date",
                "horizon",
                "top_quantile",
                "bottom_quantile",
                "spread",
            ]
        )
        if validation_frame is None or validation_frame.empty:
            return empty_quantile, empty_long_short

        target_column = f"forward_return_{int(horizon)}"
        if target_column not in validation_frame.columns:
            raise ValueError(f"validation frame missing target column: {target_column}")

        memberships = quantile_membership_by_date
        if memberships is None:
            memberships = self.calculate_quantile_membership_by_date(validation_frame)
        if memberships.empty:
            return empty_quantile, empty_long_short

        returns_frame = validation_frame[
            ["feature_set", "feature_name", "trade_date", "stock_code", target_column]
        ].dropna(subset=[target_column]).drop_duplicates(
            subset=["feature_set", "feature_name", "trade_date", "stock_code"],
            keep="last",
        )

        joined = memberships.merge(
            returns_frame,
            on=["feature_set", "feature_name", "trade_date", "stock_code"],
            how="inner",
        )
        if joined.empty:
            return empty_quantile, empty_long_short

        group_keys = ["feature_set", "feature_name", "trade_date"]
        minimum_required = max(self.min_observations, self.quantiles)
        observation_count = joined.groupby(group_keys, dropna=False)["stock_code"].transform("size")
        joined = joined.loc[observation_count >= minimum_required].copy()
        if joined.empty:
            return empty_quantile, empty_long_short

        quantile_frame = (
            joined.groupby(group_keys + ["quantile"], dropna=False)[target_column]
            .agg(mean_return="mean", observation_count="count")
            .reset_index()
        )
        quantile_frame["horizon"] = int(horizon)
        quantile_frame = quantile_frame[
            [
                "feature_set",
                "feature_name",
                "trade_date",
                "horizon",
                "quantile",
                "mean_return",
                "observation_count",
            ]
        ]

        extrema = quantile_frame.groupby(group_keys, dropna=False).agg(
            top_quantile=("quantile", "max"),
            bottom_quantile=("quantile", "min"),
        ).reset_index()
        top_rows = extrema.merge(
            quantile_frame,
            left_on=group_keys + ["top_quantile"],
            right_on=group_keys + ["quantile"],
            how="left",
        ).rename(columns={"mean_return": "top_mean_return"})
        bottom_rows = extrema.merge(
            quantile_frame,
            left_on=group_keys + ["bottom_quantile"],
            right_on=group_keys + ["quantile"],
            how="left",
        ).rename(columns={"mean_return": "bottom_mean_return"})
        long_short_frame = top_rows[group_keys + ["horizon", "top_quantile", "top_mean_return"]].merge(
            bottom_rows[group_keys + ["bottom_quantile", "bottom_mean_return"]],
            on=group_keys,
            how="inner",
        )
        long_short_frame["spread"] = long_short_frame["top_mean_return"] - long_short_frame["bottom_mean_return"]
        long_short_frame = long_short_frame[
            [
                "feature_set",
                "feature_name",
                "trade_date",
                "horizon",
                "top_quantile",
                "bottom_quantile",
                "spread",
            ]
        ]

        quantile_frame["trade_date"] = pd.to_datetime(quantile_frame["trade_date"], errors="coerce")
        quantile_frame.sort_values(["feature_set", "feature_name", "trade_date", "quantile"], inplace=True)
        quantile_frame.reset_index(drop=True, inplace=True)

        long_short_frame["trade_date"] = pd.to_datetime(long_short_frame["trade_date"], errors="coerce")
        long_short_frame.sort_values(["feature_set", "feature_name", "trade_date"], inplace=True)
        long_short_frame.reset_index(drop=True, inplace=True)
        return quantile_frame, long_short_frame

    def calculate_turnover_by_date(self, quantile_membership_by_date):
        """按交易日计算分组换手率。"""
        empty_turnover = pd.DataFrame(
            columns=[
                "feature_set",
                "feature_name",
                "trade_date",
                "quantile",
                "prev_count",
                "current_count",
                "retained_count",
                "entered_count",
                "exited_count",
                "turnover_rate",
            ]
        )
        if quantile_membership_by_date is None or quantile_membership_by_date.empty:
            return empty_turnover

        membership = quantile_membership_by_date[
            ["feature_set", "feature_name", "trade_date", "quantile", "stock_code"]
        ].drop_duplicates()
        if membership.empty:
            return empty_turnover

        counts = (
            membership.groupby(["feature_set", "feature_name", "quantile", "trade_date"], dropna=False)["stock_code"]
            .nunique()
            .reset_index(name="current_count")
            .sort_values(["feature_set", "feature_name", "quantile", "trade_date"])
        )
        counts["prev_count"] = counts.groupby(["feature_set", "feature_name", "quantile"], dropna=False)[
            "current_count"
        ].shift(1)

        previous_membership = membership.rename(columns={"trade_date": "prev_trade_date"})
        previous_dates = counts[["feature_set", "feature_name", "quantile", "trade_date"]].copy()
        previous_dates["prev_trade_date"] = counts.groupby(
            ["feature_set", "feature_name", "quantile"],
            dropna=False,
        )["trade_date"].shift(1)
        previous_dates.dropna(subset=["prev_trade_date"], inplace=True)

        retention = previous_dates.merge(
            membership,
            on=["feature_set", "feature_name", "quantile", "trade_date"],
            how="inner",
        ).merge(
            previous_membership,
            on=["feature_set", "feature_name", "quantile", "prev_trade_date", "stock_code"],
            how="inner",
        )
        retained = (
            retention.groupby(["feature_set", "feature_name", "quantile", "trade_date"], dropna=False)["stock_code"]
            .nunique()
            .reset_index(name="retained_count")
        )

        result = counts.merge(
            retained,
            on=["feature_set", "feature_name", "quantile", "trade_date"],
            how="left",
        )
        result = result[result["prev_count"].notna()].copy()
        if result.empty:
            return empty_turnover

        result["prev_count"] = result["prev_count"].astype(int)
        result["retained_count"] = result["retained_count"].fillna(0).astype(int)
        result["entered_count"] = result["current_count"] - result["retained_count"]
        result["exited_count"] = result["prev_count"] - result["retained_count"]
        denominator = np.maximum(result["prev_count"], result["current_count"])
        result["turnover_rate"] = 1.0 - result["retained_count"] / denominator

        result["trade_date"] = pd.to_datetime(result["trade_date"], errors="coerce")
        result.sort_values(["feature_set", "feature_name", "quantile", "trade_date"], inplace=True)
        result.reset_index(drop=True, inplace=True)
        return result[
            [
                "feature_set",
                "feature_name",
                "trade_date",
                "quantile",
                "prev_count",
                "current_count",
                "retained_count",
                "entered_count",
                "exited_count",
                "turnover_rate",
            ]
        ]

    def summarize_quantiles(self, quantile_returns_by_date):
        """汇总分组收益表现。"""
        if quantile_returns_by_date is None or quantile_returns_by_date.empty:
            return pd.DataFrame(
                columns=[
                    "feature_set",
                    "feature_name",
                    "horizon",
                    "quantile",
                    "mean_return",
                    "avg_observation_count",
                    "valid_dates",
                ]
            )

        result = (
            quantile_returns_by_date.groupby(
                ["feature_set", "feature_name", "horizon", "quantile"],
                dropna=False,
            )
            .agg(
                mean_return=("mean_return", "mean"),
                avg_observation_count=("observation_count", "mean"),
                valid_dates=("trade_date", "nunique"),
            )
            .reset_index()
        )
        result.sort_values(["feature_set", "feature_name", "horizon", "quantile"], inplace=True)
        result.reset_index(drop=True, inplace=True)
        return result

    def summarize_long_short(self, long_short_by_date):
        """汇总顶底分组价差。"""
        if long_short_by_date is None or long_short_by_date.empty:
            return pd.DataFrame(
                columns=[
                    "feature_set",
                    "feature_name",
                    "horizon",
                    "mean_spread",
                    "std_spread",
                    "spread_ir",
                    "positive_rate",
                    "valid_dates",
                ]
            )

        result = (
            long_short_by_date.groupby(["feature_set", "feature_name", "horizon"], dropna=False)["spread"]
            .agg(
                mean_spread="mean",
                std_spread=lambda s: s.std(ddof=1),
                positive_rate=lambda s: (pd.to_numeric(s, errors="coerce") > 0).mean(),
                valid_dates="count",
            )
            .reset_index()
        )
        valid_mask = (result["valid_dates"] > 1) & result["std_spread"].notna() & (result["std_spread"] != 0)
        result["spread_ir"] = np.where(
            valid_mask,
            result["mean_spread"] / result["std_spread"] * np.sqrt(result["valid_dates"]),
            np.nan,
        )
        result.sort_values(["feature_set", "feature_name", "horizon"], inplace=True)
        result.reset_index(drop=True, inplace=True)
        return result

    def summarize_turnover(self, turnover_by_date):
        """汇总分组换手率。"""
        if turnover_by_date is None or turnover_by_date.empty:
            return pd.DataFrame(
                columns=[
                    "feature_set",
                    "feature_name",
                    "quantile",
                    "mean_turnover",
                    "std_turnover",
                    "min_turnover",
                    "max_turnover",
                    "valid_dates",
                ]
            )

        result = (
            turnover_by_date.groupby(["feature_set", "feature_name", "quantile"], dropna=False)
            .agg(
                mean_turnover=("turnover_rate", "mean"),
                std_turnover=("turnover_rate", "std"),
                min_turnover=("turnover_rate", "min"),
                max_turnover=("turnover_rate", "max"),
                valid_dates=("trade_date", "nunique"),
            )
            .reset_index()
        )
        result.sort_values(["feature_set", "feature_name", "quantile"], inplace=True)
        result.reset_index(drop=True, inplace=True)
        return result

    def summarize_decay(self, ic_summary, long_short_summary):
        """汇总不同 horizon 下的衰减曲线。"""
        empty_decay = pd.DataFrame(
            columns=[
                "feature_set",
                "feature_name",
                "horizon",
                "mean_ic",
                "mean_rank_ic",
                "mean_spread",
                "ic_decay_ratio",
                "rank_ic_decay_ratio",
                "spread_decay_ratio",
                "base_horizon",
            ]
        )
        if ic_summary is None or ic_summary.empty:
            return empty_decay

        working = ic_summary.merge(
            long_short_summary[["feature_set", "feature_name", "horizon", "mean_spread"]]
            if long_short_summary is not None and not long_short_summary.empty
            else pd.DataFrame(columns=["feature_set", "feature_name", "horizon", "mean_spread"]),
            on=["feature_set", "feature_name", "horizon"],
            how="left",
        )
        rows = []
        grouped = working.groupby(["feature_set", "feature_name"], dropna=False)
        for (feature_set, feature_name), group in grouped:
            ordered = group.sort_values("horizon").reset_index(drop=True)
            base_horizon = int(ordered["horizon"].iloc[0])
            base_ic_abs = abs(ordered["mean_ic"].iloc[0]) if pd.notna(ordered["mean_ic"].iloc[0]) else pd.NA
            base_rank_ic_abs = abs(ordered["mean_rank_ic"].iloc[0]) if pd.notna(ordered["mean_rank_ic"].iloc[0]) else pd.NA
            base_spread_abs = abs(ordered["mean_spread"].iloc[0]) if pd.notna(ordered["mean_spread"].iloc[0]) else pd.NA
            for _, row in ordered.iterrows():
                rows.append(
                    {
                        "feature_set": feature_set,
                        "feature_name": feature_name,
                        "horizon": int(row["horizon"]),
                        "mean_ic": row["mean_ic"],
                        "mean_rank_ic": row["mean_rank_ic"],
                        "mean_spread": row.get("mean_spread", pd.NA),
                        "ic_decay_ratio": self._safe_ratio(abs(row["mean_ic"]), base_ic_abs),
                        "rank_ic_decay_ratio": self._safe_ratio(abs(row["mean_rank_ic"]), base_rank_ic_abs),
                        "spread_decay_ratio": self._safe_ratio(abs(row.get("mean_spread", pd.NA)), base_spread_abs),
                        "base_horizon": base_horizon,
                    }
                )

        result = pd.DataFrame(rows) if rows else empty_decay
        if not result.empty:
            result.sort_values(["feature_set", "feature_name", "horizon"], inplace=True)
            result.reset_index(drop=True, inplace=True)
        return result

    @staticmethod
    def _information_ratio(mean_value, std_value, observation_count):
        if observation_count <= 1:
            return pd.NA
        if pd.isna(mean_value) or pd.isna(std_value) or std_value == 0:
            return pd.NA
        return mean_value / std_value * math.sqrt(observation_count)

    @staticmethod
    def _safe_ratio(value, base_value):
        if pd.isna(value) or pd.isna(base_value) or base_value == 0:
            return pd.NA
        return value / base_value

    @staticmethod
    def _ttest_1samp(series):
        clean = pd.to_numeric(series, errors="coerce").dropna()
        if len(clean) <= 1:
            return pd.NA, pd.NA
        if clean.nunique() <= 1:
            return pd.NA, pd.NA
        result = stats.ttest_1samp(clean, popmean=0.0, nan_policy="omit")
        return result.statistic, result.pvalue

    @staticmethod
    def _coerce_feature_frame(feature_frame):
        if feature_frame is None or feature_frame.empty:
            return pd.DataFrame(columns=["trade_date", "stock_code", "feature_set", "feature_name", "feature_value"])

        working = feature_frame.copy()
        rename_mapping = {
            "date": "trade_date",
            "Date": "trade_date",
            "datetime": "trade_date",
            "factor_name": "feature_name",
            "name": "feature_name",
            "factor_value": "feature_value",
            "value": "feature_value",
        }
        working.rename(columns=rename_mapping, inplace=True)

        if "trade_date" not in working.columns:
            if isinstance(working.index, pd.DatetimeIndex):
                working = working.reset_index().rename(columns={working.index.name or "index": "trade_date"})
            else:
                raise ValueError("feature frame missing trade_date column")

        if {"feature_name", "feature_value"}.issubset(working.columns):
            long_frame = working.copy()
        else:
            target_feature_columns = [column for column in working.columns if column not in FEATURE_METADATA_COLUMNS]
            if not target_feature_columns:
                raise ValueError("feature frame missing feature columns")
            id_vars = [column for column in working.columns if column not in target_feature_columns]
            long_frame = working.melt(
                id_vars=id_vars,
                value_vars=target_feature_columns,
                var_name="feature_name",
                value_name="feature_value",
            )

        if "feature_set" not in long_frame.columns:
            long_frame["feature_set"] = "default"
        if "stock_code" not in long_frame.columns:
            raise ValueError("feature frame missing stock_code column")

        long_frame["trade_date"] = pd.to_datetime(long_frame["trade_date"], errors="coerce")
        long_frame["feature_value"] = pd.to_numeric(long_frame["feature_value"], errors="coerce")
        long_frame.dropna(subset=["trade_date", "stock_code", "feature_name", "feature_value"], inplace=True)
        long_frame.sort_values(["feature_set", "feature_name", "trade_date", "stock_code"], inplace=True)
        long_frame.reset_index(drop=True, inplace=True)
        return long_frame

    @staticmethod
    def _coerce_ohlcv_frame(ohlcv_frame):
        if ohlcv_frame is None or ohlcv_frame.empty:
            return pd.DataFrame(columns=["trade_date", "stock_code", "close"])

        working = ohlcv_frame.copy()
        rename_mapping = {
            "date": "trade_date",
            "Date": "trade_date",
            "datetime": "trade_date",
            "Close": "close",
        }
        working.rename(columns=rename_mapping, inplace=True)
        if "trade_date" not in working.columns:
            if isinstance(working.index, pd.DatetimeIndex):
                working = working.reset_index().rename(columns={working.index.name or "index": "trade_date"})
            else:
                raise ValueError("ohlcv frame missing trade_date column")
        if "stock_code" not in working.columns:
            raise ValueError("ohlcv frame missing stock_code column")
        if "close" not in working.columns:
            raise ValueError("ohlcv frame missing close column")

        working["trade_date"] = pd.to_datetime(working["trade_date"], errors="coerce")
        working["close"] = pd.to_numeric(working["close"], errors="coerce")
        working.dropna(subset=["trade_date", "stock_code", "close"], inplace=True)
        working.sort_values(["stock_code", "trade_date"], inplace=True)
        working.reset_index(drop=True, inplace=True)
        return working
