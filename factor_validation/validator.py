#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""IC / RankIC / 分组收益 / 换手率 / 衰减验证。"""

import math
from pathlib import Path
import shutil
import tempfile

import duckdb
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


class FactorValidationAccumulator:
    def __init__(self, horizons, quantiles, min_observations, include_validation_frame=False, include_membership=False):
        self.horizons = tuple(int(item) for item in horizons)
        self.quantiles = int(quantiles)
        self.min_observations = int(min_observations)
        self.include_validation_frame = bool(include_validation_frame)
        self.include_membership = bool(include_membership)
        self._rows_written = 0
        self._temp_dir = None
        self._database_path = None
        self._conn = None
        self._table_created = False

    def update(self, validation_batch):
        if validation_batch is None or validation_batch.empty:
            return
        working = validation_batch.copy()
        working["trade_date"] = pd.to_datetime(working["trade_date"], errors="coerce")
        working.dropna(subset=["trade_date", "stock_code", "feature_name", "feature_value"], inplace=True)
        if working.empty:
            return

        self._rows_written += len(working)
        conn = self._ensure_connection()
        conn.register("validation_batch_df", working)
        if not self._table_created:
            conn.execute("CREATE TABLE validation_batches AS SELECT * FROM validation_batch_df")
            self._table_created = True
        else:
            conn.execute("INSERT INTO validation_batches SELECT * FROM validation_batch_df")
        conn.unregister("validation_batch_df")

    def _load_all_batches(self):
        if self._rows_written <= 0 or not self._table_created:
            return pd.DataFrame()
        conn = self._ensure_connection()
        return conn.execute(
            "SELECT * FROM validation_batches ORDER BY trade_date, feature_set, feature_name, stock_code"
        ).fetch_df()

    def _ensure_connection(self):
        if self._conn is not None:
            return self._conn
        if self._temp_dir is None:
            self._temp_dir = Path(tempfile.mkdtemp(prefix="factor_validation_"))
        if self._database_path is None:
            self._database_path = self._temp_dir / "validation.duckdb"
        self._conn = duckdb.connect(str(self._database_path))
        return self._conn

    def _iter_trade_dates(self):
        if self._rows_written <= 0 or not self._table_created:
            return []
        conn = self._ensure_connection()
        rows = conn.execute(
            "SELECT DISTINCT trade_date FROM validation_batches ORDER BY trade_date"
        ).fetchall()
        return [row[0] for row in rows if row and row[0] is not None]

    def _load_trade_date_batch(self, trade_date):
        if self._rows_written <= 0 or not self._table_created:
            return pd.DataFrame()
        conn = self._ensure_connection()
        batch = conn.execute(
            """
            SELECT *
            FROM validation_batches
            WHERE trade_date = ?
            ORDER BY feature_set, feature_name, stock_code
            """,
            [trade_date],
        ).fetch_df()
        return FactorValidator._coerce_validation_batch_types(batch)

    def finalize(self, validator):
        if self.include_validation_frame:
            return self._finalize_full_frame(validator)
        return self._finalize_streaming(validator)

    def _finalize_full_frame(self, validator):
        validation_frame = self._load_all_batches()
        if validation_frame.empty:
            return validator._empty_validation_result(validation_frame)
        quantile_membership_by_date = validator.calculate_quantile_membership_by_date(validation_frame)
        ic_frames = []
        quantile_frames = []
        long_short_frames = []
        for horizon in self.horizons:
            ic_frames.append(validator.calculate_ic_by_date(validation_frame, horizon=horizon))
            quantile_by_date, long_short_by_date = validator.calculate_quantile_returns_by_date(
                validation_frame,
                quantile_membership_by_date=quantile_membership_by_date,
                horizon=horizon,
            )
            quantile_frames.append(quantile_by_date)
            long_short_frames.append(long_short_by_date)
        ic_by_date = pd.concat(ic_frames, ignore_index=True) if ic_frames else pd.DataFrame()
        quantile_returns_by_date = pd.concat(quantile_frames, ignore_index=True) if quantile_frames else pd.DataFrame()
        long_short_by_date = pd.concat(long_short_frames, ignore_index=True) if long_short_frames else pd.DataFrame()
        turnover_by_date = validator.calculate_turnover_by_date(quantile_membership_by_date)
        ic_summary = validator.summarize_ic(ic_by_date)
        long_short_summary = validator.summarize_long_short(long_short_by_date)
        result = {
            "validation_frame": validation_frame,
            "ic_by_date": ic_by_date,
            "ic_summary": ic_summary,
            "quantile_returns_by_date": quantile_returns_by_date,
            "quantile_membership_by_date": (
                quantile_membership_by_date if self.include_membership
                else validator._empty_quantile_membership_frame()
            ),
            "quantile_summary": validator.summarize_quantiles(quantile_returns_by_date),
            "long_short_by_date": long_short_by_date,
            "long_short_summary": long_short_summary,
            "turnover_by_date": turnover_by_date,
            "turnover_summary": validator.summarize_turnover(turnover_by_date),
            "decay_summary": validator.summarize_decay(ic_summary, long_short_summary),
        }
        self.cleanup()
        return result

    def _finalize_streaming(self, validator):
        trade_dates = self._iter_trade_dates()
        if not trade_dates:
            return validator._empty_validation_result(validator._empty_validation_frame())

        conn = self._ensure_connection()
        horizons = [int(h) for h in self.horizons]
        q = int(self.quantiles)
        min_obs = int(self.min_observations)
        min_required = max(min_obs, q)
        validation_frame = validator._empty_validation_frame()

        table_cols = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name='validation_batches'"
            ).fetchall()
        }
        working_horizons = [h for h in horizons if f"forward_return_{h}" in table_cols]
        if not working_horizons:
            for table_name in ("quantile_membership", "temp_merged"):
                conn.execute(f"DROP TABLE IF EXISTS {table_name}")
            return validator._empty_validation_result(validation_frame)

        # =========================================================================
        # 1. Bulk IC / RankIC across all dates, features, and horizons in one query
        # =========================================================================
        ic_parts = []
        for h in working_horizons:
            target = f"forward_return_{h}"
            ic_parts.append(f"""
                SELECT
                    feature_set,
                    feature_name,
                    trade_date,
                    {h} AS horizon,
                    COUNT(*) AS observation_count,
                    COUNT(DISTINCT feature_value) AS feature_unique,
                    COUNT(DISTINCT {target}) AS target_unique,
                    CORR(feature_value, {target}) AS ic,
                    CORR(feature_rank, target_rank) AS rank_ic
                FROM (
                    SELECT
                        feature_set, feature_name, trade_date, feature_value, {target},
                        RANK() OVER w_f AS feature_rank,
                        RANK() OVER w_t AS target_rank
                    FROM validation_batches
                    WHERE feature_value IS NOT NULL AND {target} IS NOT NULL
                    WINDOW
                        w_f AS (PARTITION BY feature_set, feature_name, trade_date ORDER BY feature_value),
                        w_t AS (PARTITION BY feature_set, feature_name, trade_date ORDER BY {target})
                ) ranked
                GROUP BY feature_set, feature_name, trade_date
                HAVING COUNT(*) >= {min_obs}
                    AND COUNT(DISTINCT feature_value) > 1
                    AND COUNT(DISTINCT {target}) > 1
            """)
        ic_by_date = conn.execute(" UNION ALL ".join(ic_parts)).fetch_df()
        ic_by_date = FactorValidator._postprocess_ic_frame(ic_by_date)

        # =========================================================================
        # 2. Bulk quantile membership → DuckDB temp table
        # =========================================================================
        membership_sql = f"""
            CREATE OR REPLACE TEMP TABLE quantile_membership AS
            WITH dedup AS (
                SELECT
                    feature_set, feature_name, trade_date, stock_code,
                    MAX(feature_value) AS feature_value
                FROM validation_batches
                WHERE stock_code IS NOT NULL AND feature_value IS NOT NULL
                GROUP BY feature_set, feature_name, trade_date, stock_code
            ),
            counted AS (
                SELECT *,
                    COUNT(*) OVER (PARTITION BY feature_set, feature_name, trade_date) AS obs_count
                FROM dedup
            )
            SELECT
                feature_set, feature_name, trade_date, stock_code,
                NTILE({q}) OVER (PARTITION BY feature_set, feature_name, trade_date ORDER BY feature_value) AS quantile
            FROM counted
            WHERE obs_count >= {min_required}
        """
        conn.execute(membership_sql)

        # =========================================================================
        # 3. Bulk quantile returns + long-short per horizon
        # =========================================================================
        quantile_frames = []
        long_short_frames = []
        for h in working_horizons:
            target = f"forward_return_{h}"
            qr_sql = f"""
                WITH returns AS (
                    SELECT
                        feature_set, feature_name, trade_date, stock_code,
                        MAX({target}) AS {target}
                    FROM validation_batches
                    WHERE {target} IS NOT NULL
                    GROUP BY feature_set, feature_name, trade_date, stock_code
                ),
                joined AS (
                    SELECT m.feature_set, m.feature_name, m.trade_date, m.quantile, r.{target}
                    FROM quantile_membership m
                    INNER JOIN returns r
                        ON m.feature_set = r.feature_set
                        AND m.feature_name = r.feature_name
                        AND m.trade_date = r.trade_date
                        AND m.stock_code = r.stock_code
                ),
                counted AS (
                    SELECT *,
                        COUNT(*) OVER (PARTITION BY feature_set, feature_name, trade_date) AS obs_count
                    FROM joined
                ),
                agg AS (
                    SELECT
                        feature_set, feature_name, trade_date, quantile,
                        AVG({target}) AS mean_return,
                        COUNT(*) AS observation_count
                    FROM counted
                    WHERE obs_count >= {min_required}
                    GROUP BY feature_set, feature_name, trade_date, quantile
                )
                SELECT feature_set, feature_name, trade_date, {h} AS horizon, quantile,
                       mean_return, observation_count
                FROM agg
            """
            qr_df = conn.execute(qr_sql).fetch_df()
            if not qr_df.empty:
                qr_df["trade_date"] = pd.to_datetime(qr_df["trade_date"], errors="coerce")
                qr_df.sort_values(["feature_set", "feature_name", "trade_date", "quantile"], inplace=True)
                qr_df.reset_index(drop=True, inplace=True)
            quantile_frames.append(qr_df)

            ls_sql = f"""
                WITH returns AS (
                    SELECT
                        feature_set, feature_name, trade_date, stock_code,
                        MAX({target}) AS {target}
                    FROM validation_batches
                    WHERE {target} IS NOT NULL
                    GROUP BY feature_set, feature_name, trade_date, stock_code
                ),
                joined AS (
                    SELECT m.feature_set, m.feature_name, m.trade_date, m.quantile, r.{target}
                    FROM quantile_membership m
                    INNER JOIN returns r
                        ON m.feature_set = r.feature_set
                        AND m.feature_name = r.feature_name
                        AND m.trade_date = r.trade_date
                        AND m.stock_code = r.stock_code
                ),
                counted AS (
                    SELECT *,
                        COUNT(*) OVER (PARTITION BY feature_set, feature_name, trade_date) AS obs_count
                    FROM joined
                ),
                agg AS (
                    SELECT
                        feature_set, feature_name, trade_date, quantile,
                        AVG({target}) AS mean_return
                    FROM counted
                    WHERE obs_count >= {min_required}
                    GROUP BY feature_set, feature_name, trade_date, quantile
                ),
                extrema AS (
                    SELECT feature_set, feature_name, trade_date,
                        MAX(quantile) AS top_quantile,
                        MIN(quantile) AS bottom_quantile
                    FROM agg
                    GROUP BY feature_set, feature_name, trade_date
                )
                SELECT
                    e.feature_set, e.feature_name, e.trade_date,
                    {h} AS horizon,
                    e.top_quantile, e.bottom_quantile,
                    q_top.mean_return - q_bottom.mean_return AS spread
                FROM extrema e
                INNER JOIN agg q_top
                    ON e.feature_set = q_top.feature_set
                    AND e.feature_name = q_top.feature_name
                    AND e.trade_date = q_top.trade_date
                    AND e.top_quantile = q_top.quantile
                INNER JOIN agg q_bottom
                    ON e.feature_set = q_bottom.feature_set
                    AND e.feature_name = q_bottom.feature_name
                    AND e.trade_date = q_bottom.trade_date
                    AND e.bottom_quantile = q_bottom.quantile
            """
            ls_df = conn.execute(ls_sql).fetch_df()
            if not ls_df.empty:
                ls_df["trade_date"] = pd.to_datetime(ls_df["trade_date"], errors="coerce")
                ls_df.sort_values(["feature_set", "feature_name", "trade_date"], inplace=True)
                ls_df.reset_index(drop=True, inplace=True)
            long_short_frames.append(ls_df)

        # =========================================================================
        # 4. Bulk turnover
        # =========================================================================
        turnover_sql = f"""
            WITH membership AS (
                SELECT DISTINCT feature_set, feature_name, trade_date, quantile, stock_code
                FROM quantile_membership
            ),
            counts AS (
                SELECT feature_set, feature_name, quantile, trade_date,
                    COUNT(DISTINCT stock_code) AS current_count
                FROM membership
                GROUP BY feature_set, feature_name, quantile, trade_date
            ),
            with_prev AS (
                SELECT *,
                    LAG(current_count) OVER w AS prev_count,
                    LAG(trade_date) OVER w AS prev_trade_date
                FROM counts
                WINDOW w AS (PARTITION BY feature_set, feature_name, quantile ORDER BY trade_date)
            ),
            retained AS (
                SELECT d1.feature_set, d1.feature_name, d1.quantile, d1.trade_date,
                    COUNT(DISTINCT d1.stock_code) AS retained_count
                FROM membership d1
                INNER JOIN membership d2
                    ON d1.feature_set = d2.feature_set
                    AND d1.feature_name = d2.feature_name
                    AND d1.quantile = d2.quantile
                    AND d1.stock_code = d2.stock_code
                INNER JOIN with_prev wp
                    ON d1.feature_set = wp.feature_set
                    AND d1.feature_name = wp.feature_name
                    AND d1.quantile = wp.quantile
                    AND d1.trade_date = wp.trade_date
                    AND d2.trade_date = wp.prev_trade_date
                GROUP BY d1.feature_set, d1.feature_name, d1.quantile, d1.trade_date
            )
            SELECT
                wp.feature_set, wp.feature_name, wp.trade_date, wp.quantile,
                wp.prev_count, wp.current_count,
                COALESCE(r.retained_count, 0) AS retained_count,
                wp.current_count - COALESCE(r.retained_count, 0) AS entered_count,
                wp.prev_count - COALESCE(r.retained_count, 0) AS exited_count,
                1.0 - COALESCE(r.retained_count, 0)::DOUBLE / GREATEST(wp.prev_count, wp.current_count) AS turnover_rate
            FROM with_prev wp
            LEFT JOIN retained r
                ON wp.feature_set = r.feature_set
                AND wp.feature_name = r.feature_name
                AND wp.quantile = r.quantile
                AND wp.trade_date = r.trade_date
            WHERE wp.prev_count IS NOT NULL
            ORDER BY feature_set, feature_name, quantile, trade_date
        """
        turnover_by_date = conn.execute(turnover_sql).fetch_df()
        if not turnover_by_date.empty:
            turnover_by_date["trade_date"] = pd.to_datetime(turnover_by_date["trade_date"], errors="coerce")
            for col in ("prev_count", "current_count", "retained_count", "entered_count", "exited_count"):
                if col in turnover_by_date.columns:
                    turnover_by_date[col] = turnover_by_date[col].astype(int)
        else:
            turnover_by_date = validator._empty_turnover_frame()

        # =========================================================================
        # 5. Output quantile membership
        # =========================================================================
        if self.include_membership:
            membership_out = conn.execute(
                "SELECT * FROM quantile_membership ORDER BY feature_set, feature_name, trade_date, quantile, stock_code"
            ).fetch_df()
            if not membership_out.empty:
                membership_out["trade_date"] = pd.to_datetime(membership_out["trade_date"], errors="coerce")
            quantile_membership_by_date = membership_out
        else:
            quantile_membership_by_date = validator._empty_quantile_membership_frame()

        conn.execute("DROP TABLE IF EXISTS quantile_membership")

        # =========================================================================
        # 6. Assemble result
        # =========================================================================
        quantile_returns_by_date = (
            pd.concat(quantile_frames, ignore_index=True)
            if quantile_frames
            else validator._empty_quantile_frame()
        )
        long_short_by_date = (
            pd.concat(long_short_frames, ignore_index=True)
            if long_short_frames
            else validator._empty_long_short_frame()
        )

        ic_summary = validator.summarize_ic(ic_by_date)
        long_short_summary = validator.summarize_long_short(long_short_by_date)
        result = {
            "validation_frame": validation_frame,
            "ic_by_date": ic_by_date,
            "ic_summary": ic_summary,
            "quantile_returns_by_date": quantile_returns_by_date,
            "quantile_membership_by_date": quantile_membership_by_date,
            "quantile_summary": validator.summarize_quantiles(quantile_returns_by_date),
            "long_short_by_date": long_short_by_date,
            "long_short_summary": long_short_summary,
            "turnover_by_date": turnover_by_date,
            "turnover_summary": validator.summarize_turnover(turnover_by_date),
            "decay_summary": validator.summarize_decay(ic_summary, long_short_summary),
        }
        self.cleanup()
        return result

    def cleanup(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        self._database_path = None
        self._table_created = False
        if self._temp_dir is not None:
            try:
                shutil.rmtree(self._temp_dir, ignore_errors=True)
            except Exception:
                pass
            self._temp_dir = None


class FactorValidator:
    """对 feature 层因子做基础验证。"""

    DEFAULT_HORIZONS = (1, 5, 10, 20)

    def __init__(self, horizons=None, quantiles=5, min_observations=5):
        self.horizons = tuple(int(item) for item in (horizons or self.DEFAULT_HORIZONS))
        self.quantiles = int(quantiles)
        self.min_observations = int(min_observations)

    def _empty_validation_frame(self):
        return pd.DataFrame(columns=["trade_date", "stock_code", "feature_set", "feature_name", "feature_value"])

    def _empty_ic_frame(self):
        return pd.DataFrame(
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

    def _empty_ic_summary_frame(self):
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

    def _empty_quantile_frame(self):
        return pd.DataFrame(
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

    def _empty_quantile_membership_frame(self):
        return pd.DataFrame(
            columns=[
                "feature_set",
                "feature_name",
                "trade_date",
                "stock_code",
                "quantile",
            ]
        )

    def _empty_quantile_summary_frame(self):
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

    def _empty_long_short_frame(self):
        return pd.DataFrame(
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

    def _empty_long_short_summary_frame(self):
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

    def _empty_turnover_frame(self):
        return pd.DataFrame(
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

    def _empty_turnover_summary_frame(self):
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

    def _empty_decay_frame(self):
        return pd.DataFrame(
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

    def _empty_validation_result(self, validation_frame=None):
        return {
            "validation_frame": validation_frame if validation_frame is not None else self._empty_validation_frame(),
            "ic_by_date": self._empty_ic_frame(),
            "ic_summary": self._empty_ic_summary_frame(),
            "quantile_returns_by_date": self._empty_quantile_frame(),
            "quantile_membership_by_date": self._empty_quantile_membership_frame(),
            "quantile_summary": self._empty_quantile_summary_frame(),
            "long_short_by_date": self._empty_long_short_frame(),
            "long_short_summary": self._empty_long_short_summary_frame(),
            "turnover_by_date": self._empty_turnover_frame(),
            "turnover_summary": self._empty_turnover_summary_frame(),
            "decay_summary": self._empty_decay_frame(),
        }

    def _normalize_validation_result(self, result):
        normalized = dict(result or {})
        frame_columns = {
            "validation_frame": ["feature_set", "feature_name", "stock_code"],
            "ic_by_date": ["feature_set", "feature_name"],
            "ic_summary": ["feature_set", "feature_name"],
            "quantile_returns_by_date": ["feature_set", "feature_name"],
            "quantile_membership_by_date": ["feature_set", "feature_name", "stock_code"],
            "quantile_summary": ["feature_set", "feature_name"],
            "long_short_by_date": ["feature_set", "feature_name"],
            "long_short_summary": ["feature_set", "feature_name"],
            "turnover_by_date": ["feature_set", "feature_name"],
            "turnover_summary": ["feature_set", "feature_name"],
            "decay_summary": ["feature_set", "feature_name"],
        }
        sort_columns = {
            "validation_frame": ["feature_set", "feature_name", "trade_date", "stock_code"],
            "ic_by_date": ["feature_set", "feature_name", "trade_date", "horizon"],
            "ic_summary": ["feature_set", "feature_name", "horizon"],
            "quantile_returns_by_date": ["feature_set", "feature_name", "trade_date", "horizon", "quantile"],
            "quantile_membership_by_date": ["feature_set", "feature_name", "trade_date", "quantile", "stock_code"],
            "quantile_summary": ["feature_set", "feature_name", "horizon", "quantile"],
            "long_short_by_date": ["feature_set", "feature_name", "trade_date", "horizon"],
            "long_short_summary": ["feature_set", "feature_name", "horizon"],
            "turnover_by_date": ["feature_set", "feature_name", "quantile", "trade_date"],
            "turnover_summary": ["feature_set", "feature_name", "quantile"],
            "decay_summary": ["feature_set", "feature_name", "horizon"],
        }
        numeric_columns = {
            "ic_by_date": ["horizon", "observation_count"],
            "ic_summary": ["horizon", "valid_dates"],
            "quantile_returns_by_date": ["horizon", "quantile", "observation_count"],
            "quantile_membership_by_date": ["quantile"],
            "quantile_summary": ["horizon", "quantile", "valid_dates"],
            "long_short_by_date": ["horizon", "top_quantile", "bottom_quantile"],
            "long_short_summary": ["horizon", "valid_dates"],
            "turnover_by_date": ["quantile", "prev_count", "current_count", "retained_count", "entered_count", "exited_count"],
            "turnover_summary": ["quantile", "valid_dates"],
            "decay_summary": ["horizon", "base_horizon"],
        }
        float_columns = {
            "validation_frame": ["feature_value"],
            "ic_by_date": ["ic", "ic_pvalue", "rank_ic", "rank_ic_pvalue"],
            "ic_summary": [
                "mean_ic", "std_ic", "ic_ir", "ic_positive_rate", "ic_tstat", "ic_pvalue",
                "mean_rank_ic", "std_rank_ic", "rank_ic_ir", "rank_ic_positive_rate",
                "rank_ic_tstat", "rank_ic_pvalue",
            ],
            "quantile_returns_by_date": ["mean_return"],
            "quantile_summary": ["mean_return", "std_return", "avg_observation_count", "std_error"],
            "long_short_by_date": ["spread"],
            "long_short_summary": ["mean_spread", "std_spread", "spread_ir", "positive_rate"],
            "turnover_by_date": ["turnover_rate"],
            "turnover_summary": ["mean_turnover", "std_turnover", "min_turnover", "max_turnover"],
            "decay_summary": ["mean_ic", "mean_rank_ic", "mean_spread", "ic_decay_ratio", "rank_ic_decay_ratio", "spread_decay_ratio"],
        }
        for key, columns in frame_columns.items():
            normalized[key] = self._coerce_identifier_columns_to_string(normalized.get(key), columns)
            normalized[key] = self._coerce_trade_date_column(normalized[key])
            normalized[key] = self._coerce_integer_columns(normalized[key], numeric_columns.get(key, []))
            normalized[key] = self._coerce_float_columns(normalized[key], float_columns.get(key, []))
            normalized[key] = self._sort_frame(normalized[key], sort_columns.get(key, []))
        return normalized

    def validate(self, feature_frame, ohlcv_frame, progress_callback=None):
        """输出验证明细、IC 汇总和分组收益结果。"""
        if progress_callback is None:
            progress_callback = lambda _message: None

        progress_callback("validation frame")
        validation_frame = self.build_validation_frame(feature_frame, ohlcv_frame)
        if validation_frame.empty:
            return self._empty_validation_result(validation_frame)

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

        return self._normalize_validation_result({
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
        })

    def validate_streaming(
        self,
        batches,
        progress_callback=None,
        include_validation_frame=False,
        include_membership=False,
    ):
        if progress_callback is None:
            progress_callback = lambda _message: None

        accumulator = FactorValidationAccumulator(
            horizons=self.horizons,
            quantiles=self.quantiles,
            min_observations=self.min_observations,
            include_validation_frame=include_validation_frame,
            include_membership=include_membership,
        )
        progress_callback("stream ingest")
        for batch in batches or []:
            feature_frame = (batch or {}).get("feature_frame")
            ohlcv_frame = (batch or {}).get("ohlcv_frame")
            validation_batch = self.build_validation_frame(feature_frame, ohlcv_frame)
            accumulator.update(validation_batch)
        progress_callback("stream finalize")
        return self._normalize_validation_result(accumulator.finalize(self))

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

        valid_ic = (result["valid_dates"] > 1) & result["std_ic"].notna() & (result["std_ic"] > 1e-12)
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

        valid_rank = (result["valid_dates"] > 1) & result["std_rank_ic"].notna() & (result["std_rank_ic"] > 1e-12)
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

    def calculate_turnover_step(self, current_membership, previous_membership_groups=None):
        """按单日 membership 增量计算换手率。"""
        empty_turnover = self._empty_turnover_frame()
        previous_membership_groups = dict(previous_membership_groups or {})

        if current_membership is None or current_membership.empty:
            return empty_turnover, previous_membership_groups

        membership = current_membership[
            ["feature_set", "feature_name", "trade_date", "quantile", "stock_code"]
        ].dropna(subset=["trade_date", "stock_code"]).drop_duplicates()
        if membership.empty:
            return empty_turnover, previous_membership_groups

        trade_date = pd.to_datetime(membership["trade_date"].iloc[0], errors="coerce")
        rows = []
        grouped = membership.groupby(["feature_set", "feature_name", "quantile"], dropna=False)
        next_membership_groups = {}
        for key, group in grouped:
            current_codes = set(group["stock_code"].astype(str))
            next_membership_groups[key] = current_codes
            previous_codes = set(previous_membership_groups.get(key, set()))
            if not previous_codes:
                continue

            prev_count = len(previous_codes)
            current_count = len(current_codes)
            retained_count = len(previous_codes & current_codes)
            denominator = max(prev_count, current_count)
            turnover_rate = 1.0 - retained_count / denominator if denominator > 0 else np.nan
            rows.append(
                {
                    "feature_set": key[0],
                    "feature_name": key[1],
                    "trade_date": trade_date,
                    "quantile": int(key[2]),
                    "prev_count": int(prev_count),
                    "current_count": int(current_count),
                    "retained_count": int(retained_count),
                    "entered_count": int(current_count - retained_count),
                    "exited_count": int(prev_count - retained_count),
                    "turnover_rate": turnover_rate,
                }
            )

        result = pd.DataFrame(rows) if rows else empty_turnover
        if not result.empty:
            result.sort_values(["feature_set", "feature_name", "quantile", "trade_date"], inplace=True)
            result.reset_index(drop=True, inplace=True)
        return result, next_membership_groups

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
                std_return=("mean_return", lambda s: s.std(ddof=1)),
                avg_observation_count=("observation_count", "mean"),
                valid_dates=("trade_date", "nunique"),
            )
            .reset_index()
        )
        result["std_error"] = np.where(
            result["valid_dates"] > 0,
            result["std_return"] / np.sqrt(result["valid_dates"].astype(float)),
            np.nan,
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

    @staticmethod
    def _postprocess_ic_frame(ic_df):
        """为 DuckDB 批量计算出的 IC 结果补充 p-value 列。"""
        if ic_df is None or ic_df.empty:
            return pd.DataFrame(
                columns=[
                    "feature_set", "feature_name", "trade_date", "horizon",
                    "observation_count", "ic", "ic_pvalue", "rank_ic", "rank_ic_pvalue",
                ]
            )
        working = ic_df.copy()
        working["ic"] = np.clip(pd.to_numeric(working["ic"], errors="coerce"), -1.0, 1.0)
        working["rank_ic"] = np.clip(pd.to_numeric(working["rank_ic"], errors="coerce"), -1.0, 1.0)

        valid_mask = working["ic"].notna() & working["rank_ic"].notna()
        df_val = working["observation_count"].astype(int) - 2
        ic_abs = np.abs(working["ic"])
        ic_t = np.where(
            valid_mask & (df_val > 0) & (ic_abs < 1.0),
            working["ic"] * np.sqrt(df_val / (1.0 - np.square(working["ic"]))),
            np.nan,
        )
        rank_ic_abs = np.abs(working["rank_ic"])
        rank_ic_t = np.where(
            valid_mask & (df_val > 0) & (rank_ic_abs < 1.0),
            working["rank_ic"] * np.sqrt(df_val / (1.0 - np.square(working["rank_ic"]))),
            np.nan,
        )
        working["ic_pvalue"] = np.where(
            valid_mask,
            np.where(ic_abs >= 1.0, 0.0, 2.0 * stats.t.sf(np.abs(ic_t), df_val)),
            np.nan,
        )
        working["rank_ic_pvalue"] = np.where(
            valid_mask,
            np.where(rank_ic_abs >= 1.0, 0.0, 2.0 * stats.t.sf(np.abs(rank_ic_t), df_val)),
            np.nan,
        )
        result = working[
            ["feature_set", "feature_name", "trade_date", "horizon",
             "observation_count", "ic", "ic_pvalue", "rank_ic", "rank_ic_pvalue"]
        ].copy()
        result["trade_date"] = pd.to_datetime(result["trade_date"], errors="coerce")
        result.sort_values(["feature_set", "feature_name", "trade_date"], inplace=True)
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

    @staticmethod
    def _coerce_validation_batch_types(validation_batch):
        if validation_batch is None or validation_batch.empty:
            return pd.DataFrame()

        working = validation_batch.copy()
        working["trade_date"] = pd.to_datetime(working["trade_date"], errors="coerce")
        if "feature_set" in working.columns:
            working["feature_set"] = working["feature_set"].astype("string")
        if "feature_name" in working.columns:
            working["feature_name"] = working["feature_name"].astype("string")
        if "stock_code" in working.columns:
            working["stock_code"] = working["stock_code"].astype("string")
        if "feature_value" in working.columns:
            working["feature_value"] = pd.to_numeric(working["feature_value"], errors="coerce")
        for column in working.columns:
            if column.startswith("forward_return_"):
                working[column] = pd.to_numeric(working[column], errors="coerce")
        return working

    @staticmethod
    def _coerce_identifier_columns_to_string(frame, columns):
        if frame is None or frame.empty:
            return frame
        working = frame.copy()
        for column in columns:
            if column in working.columns:
                working[column] = working[column].astype("string")
        return working

    @staticmethod
    def _coerce_trade_date_column(frame):
        if frame is None or frame.empty or "trade_date" not in frame.columns:
            return frame
        working = frame.copy()
        working["trade_date"] = pd.to_datetime(working["trade_date"], errors="coerce")
        return working

    @staticmethod
    def _coerce_integer_columns(frame, columns):
        if frame is None or frame.empty:
            return frame
        working = frame.copy()
        for column in columns:
            if column not in working.columns:
                continue
            working[column] = pd.to_numeric(working[column], errors="coerce")
            if not working[column].isna().any():
                working[column] = working[column].astype("int64")
        return working

    @staticmethod
    def _coerce_float_columns(frame, columns):
        if frame is None or frame.empty:
            return frame
        working = frame.copy()
        for column in columns:
            if column in working.columns:
                working[column] = pd.to_numeric(working[column], errors="coerce")
        return working

    @staticmethod
    def _sort_frame(frame, columns):
        if frame is None or frame.empty:
            return frame
        available = [column for column in columns if column in frame.columns]
        if not available:
            return frame
        working = frame.sort_values(available).reset_index(drop=True)
        return working

    # ------------------------------------------------------------------
    # Alphalens-borrowed metrics
    # ------------------------------------------------------------------

    def compute_factor_rank_autocorrelation(self, feature_frame, period=1):
        """因子排序自相关 — 衡量因子排序稳定性。

        Args:
            feature_frame: long-format DataFrame with [trade_date, stock_code, feature_name, feature_value]
            period: lag in trading days

        Returns:
            pd.DataFrame with columns [feature_name, period, mean_autocorrelation, valid_dates]
        """
        working = self._coerce_feature_frame(feature_frame)
        if working.empty:
            return pd.DataFrame(columns=["feature_name", "period", "mean_autocorrelation", "valid_dates"])

        rows = []
        for (feature_set, feature_name), group in working.groupby(["feature_set", "feature_name"], dropna=False):
            try:
                pivot = group.pivot_table(
                    index="trade_date", columns="stock_code", values="feature_value", aggfunc="last"
                )
                pivot = pivot.sort_index()
                if pivot.shape[1] < 2 or len(pivot) <= period:
                    continue
                ranked = pivot.rank(axis=1, method="average")
                shifted = ranked.shift(period)
                autocorr = ranked.iloc[period:].corrwith(shifted.iloc[period:], axis=1)
                rows.append({
                    "feature_name": feature_name,
                    "period": int(period),
                    "mean_autocorrelation": float(autocorr.mean()) if autocorr.notna().any() else np.nan,
                    "valid_dates": autocorr.notna().sum(),
                })
            except Exception:
                continue

        if not rows:
            return pd.DataFrame(columns=["feature_name", "period", "mean_autocorrelation", "valid_dates"])
        result = pd.DataFrame(rows)
        result.sort_values("mean_autocorrelation", ascending=False, inplace=True)
        result.reset_index(drop=True, inplace=True)
        return result

    @staticmethod
    def compute_monthly_ic_heatmap(ic_by_date):
        """IC 月度热力图数据 — 年×月 IC 均值矩阵。

        Args:
            ic_by_date: DataFrame from calculate_ic_by_date() with [trade_date, feature_name, horizon, ic, rank_ic]

        Returns:
            pd.DataFrame with columns [feature_name, horizon, year, month, mean_ic, mean_rank_ic]
        """
        if ic_by_date is None or ic_by_date.empty:
            return pd.DataFrame(columns=["feature_name", "horizon", "year", "month", "mean_ic", "mean_rank_ic"])

        working = ic_by_date.copy()
        working["trade_date"] = pd.to_datetime(working["trade_date"], errors="coerce")
        working["year"] = working["trade_date"].dt.year
        working["month"] = working["trade_date"].dt.month

        group_cols = ["feature_name", "horizon", "year", "month"]
        result = (
            working.groupby(group_cols, dropna=False)
            .agg(mean_ic=("ic", "mean"), mean_rank_ic=("rank_ic", "mean"))
            .reset_index()
        )
        return result

    def compute_factor_alpha_beta(self, feature_frame, ohlcv_frame, target_horizon=5):
        """因子组合 alpha/beta — OLS 回归因子收益对截面均值收益。

        对每个因子，计算每日因子加权多空组合收益，再对截面均值收益做 OLS。

        Args:
            feature_frame: long-format feature DataFrame
            ohlcv_frame: OHLCV DataFrame with [trade_date, stock_code, close]
            target_horizon: forward return horizon

        Returns:
            pd.DataFrame with [feature_name, annual_alpha, beta, alpha_tstat, valid_dates]
        """
        validation_frame = self.build_validation_frame(feature_frame, ohlcv_frame)
        if validation_frame.empty:
            return pd.DataFrame(columns=["feature_name", "annual_alpha", "beta", "alpha_tstat", "valid_dates"])

        target_col = f"forward_return_{int(target_horizon)}"
        if target_col not in validation_frame.columns:
            return pd.DataFrame(columns=["feature_name", "annual_alpha", "beta", "alpha_tstat", "valid_dates"])

        rows = []
        for feature_name, group in validation_frame.groupby("feature_name", dropna=False):
            try:
                pivot = group.pivot_table(
                    index="trade_date", columns="stock_code", values="feature_value", aggfunc="last"
                )
                ret_pivot = group.pivot_table(
                    index="trade_date", columns="stock_code", values=target_col, aggfunc="last"
                )
                common_dates = pivot.index.intersection(ret_pivot.index)
                if len(common_dates) < 10:
                    continue

                daily_factor_returns = []
                daily_market_returns = []
                for trade_date in common_dates:
                    fv = pivot.loc[trade_date].dropna()
                    rv = ret_pivot.loc[trade_date].dropna()
                    common_stocks = fv.index.intersection(rv.index)
                    if len(common_stocks) < self.min_observations:
                        continue
                    fv = fv.loc[common_stocks]
                    rv = rv.loc[common_stocks]
                    weights = (fv - fv.mean()) / fv.std().replace(0, 1)
                    daily_factor_returns.append((weights * rv).sum() / weights.abs().sum())
                    daily_market_returns.append(rv.mean())

                if len(daily_factor_returns) < 10:
                    continue

                factor_series = pd.Series(daily_factor_returns)
                market_series = pd.Series(daily_market_returns)
                slope, intercept, r_value, p_value, _ = stats.linregress(market_series, factor_series)
                resid = factor_series - (intercept + slope * market_series)
                resid_se = resid.std(ddof=1) / np.sqrt(len(resid))
                alpha_tstat = intercept / resid_se if resid_se > 0 else np.nan

                rows.append({
                    "feature_name": feature_name,
                    "annual_alpha": float(intercept * 252),
                    "beta": float(slope),
                    "alpha_tstat": float(alpha_tstat),
                    "valid_dates": len(daily_factor_returns),
                })
            except Exception:
                continue

        if not rows:
            return pd.DataFrame(columns=["feature_name", "annual_alpha", "beta", "alpha_tstat", "valid_dates"])
        result = pd.DataFrame(rows)
        result.sort_values("annual_alpha", ascending=False, inplace=True)
        result.reset_index(drop=True, inplace=True)
        return result

    # ------------------------------------------------------------------
    # Factor correlation, Fama-MacBeth, monotonicity
    # ------------------------------------------------------------------

    def compute_factor_correlation_matrix(self, validation_frame):
        """因子截面相关性矩阵。

        Args:
            validation_frame: DataFrame with [trade_date, stock_code, feature_name, feature_value]

        Returns:
            (corr_matrix, corr_long) where corr_matrix is factor×factor DataFrame
            and corr_long has [factor_a, factor_b, correlation].
        """
        if validation_frame is None or validation_frame.empty:
            empty = pd.DataFrame(columns=["factor_a", "factor_b", "correlation"])
            return pd.DataFrame(), empty

        pivot = validation_frame.pivot_table(
            index=["trade_date", "stock_code"],
            columns="feature_name",
            values="feature_value",
            aggfunc="last",
        )
        if pivot.empty or pivot.shape[1] < 2:
            empty = pd.DataFrame(columns=["factor_a", "factor_b", "correlation"])
            return pd.DataFrame(), empty

        corr_matrix = pivot.corr()
        corr_long = corr_matrix.where(np.triu(np.ones(corr_matrix.shape, dtype=bool), k=1)).stack().reset_index()
        corr_long.columns = ["factor_a", "factor_b", "correlation"]
        corr_long = corr_long[corr_long["factor_a"] != corr_long["factor_b"]]
        corr_long.sort_values("correlation", ascending=False, inplace=True)
        corr_long.reset_index(drop=True, inplace=True)
        return corr_matrix, corr_long

    def compute_fama_macbeth(self, validation_frame, target_horizon=5):
        """Fama-MacBeth 两阶段回归检验因子显著性。

        Pass 1: 每日横截面回归 forward_return ~ factor_value
        Pass 2: 系数时序均值 t 检验

        Args:
            validation_frame: DataFrame with [trade_date, stock_code, feature_name, feature_value, forward_return_*]
            target_horizon: which forward return column to use

        Returns:
            pd.DataFrame with [feature_name, fm_coef, fm_tstat, fm_pvalue, valid_dates]
        """
        target_col = f"forward_return_{int(target_horizon)}"
        if validation_frame is None or validation_frame.empty or target_col not in validation_frame.columns:
            return pd.DataFrame(columns=["feature_name", "fm_coef", "fm_tstat", "fm_pvalue", "valid_dates"])

        rows = []
        for feature_name, group in validation_frame.groupby("feature_name", dropna=False):
            daily_coefs = []
            for trade_date, day_data in group.groupby("trade_date", dropna=False):
                clean = day_data[[target_col, "feature_value"]].dropna()
                if len(clean) < self.min_observations:
                    continue
                X = clean[["feature_value"]].values.astype(np.float64)
                X_with_intercept = np.column_stack([np.ones(len(X)), X])
                y = clean[target_col].values.astype(np.float64)
                try:
                    coef, *_ = np.linalg.lstsq(X_with_intercept, y, rcond=None)
                    daily_coefs.append(float(coef[1]))
                except np.linalg.LinAlgError:
                    continue

            if len(daily_coefs) < 10:
                continue

            coef_series = pd.Series(daily_coefs)
            mean_coef = coef_series.mean()
            se = coef_series.std(ddof=1) / np.sqrt(len(coef_series))
            tstat = mean_coef / se if se > 0 else np.nan
            pvalue = 2 * stats.t.sf(abs(tstat), len(coef_series) - 1) if not np.isnan(tstat) else np.nan

            rows.append({
                "feature_name": feature_name,
                "fm_coef": float(mean_coef),
                "fm_tstat": float(tstat) if not np.isnan(tstat) else np.nan,
                "fm_pvalue": float(pvalue) if not np.isnan(pvalue) else np.nan,
                "valid_dates": len(daily_coefs),
            })

        if not rows:
            return pd.DataFrame(columns=["feature_name", "fm_coef", "fm_tstat", "fm_pvalue", "valid_dates"])
        result = pd.DataFrame(rows)
        result.sort_values("fm_tstat", ascending=False, key=lambda s: s.abs(), inplace=True)
        result.reset_index(drop=True, inplace=True)
        return result

    @staticmethod
    def compute_monotonicity(quantile_summary):
        """量化分组收益单调性。

        Args:
            quantile_summary: DataFrame from summarize_quantiles() with [feature_name, horizon, quantile, mean_return]

        Returns:
            pd.DataFrame with [feature_name, horizon, monotonicity_spearman, monotonicity_score]
            where monotonicity_score is 0-1 (1 = perfect monotonic).
        """
        if quantile_summary is None or quantile_summary.empty:
            return pd.DataFrame(columns=["feature_name", "horizon", "monotonicity_spearman", "monotonicity_score"])

        rows = []
        for (feature_name, horizon), group in quantile_summary.groupby(["feature_name", "horizon"], dropna=False):
            clean = group.dropna(subset=["mean_return"])
            if clean["quantile"].nunique() < 3:
                continue
            sorted_group = clean.sort_values("quantile")
            spearman_corr, _ = stats.spearmanr(sorted_group["quantile"], sorted_group["mean_return"])
            score = (abs(spearman_corr) + 1) / 2 if not np.isnan(spearman_corr) else np.nan
            rows.append({
                "feature_name": feature_name,
                "horizon": horizon,
                "monotonicity_spearman": float(spearman_corr) if not np.isnan(spearman_corr) else np.nan,
                "monotonicity_score": float(score) if not np.isnan(score) else np.nan,
            })

        if not rows:
            return pd.DataFrame(columns=["feature_name", "horizon", "monotonicity_spearman", "monotonicity_score"])
        result = pd.DataFrame(rows)
        result.sort_values("monotonicity_score", ascending=False, inplace=True)
        result.reset_index(drop=True, inplace=True)
        return result
