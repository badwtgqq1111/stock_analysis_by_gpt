"""Utility methods for resource management, progress reporting, and caching."""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from core.constants import (
    VALIDATION_FEATURE_BASE_COLUMNS,
    VALIDATION_FEATURE_CACHE_TTL_SECONDS,
    VALIDATION_OHLCV_BASE_COLUMNS,
)


class UtilsMixin:
    """Utility methods shared across the analyzer."""

    @staticmethod
    def _emit_progress_line(
        *,
        prefix,
        completed,
        total,
        success_count,
        started_at,
        stream=None,
        extra_fields=None,
    ):
        target_stream = stream or sys.stderr
        total = max(int(total or 0), 1)
        completed = max(int(completed or 0), 0)
        success_count = max(int(success_count or 0), 0)
        elapsed = max(time.time() - started_at, 1e-9)
        rate = completed / elapsed if completed > 0 else 0.0
        remaining = max(total - completed, 0)
        eta = remaining / rate if rate > 0 else 0.0
        fields = [
            f"stocks_done={completed}/{total}",
            f"({completed / total:.1%})",
            f"success={success_count}",
        ]
        if extra_fields:
            for name, value in extra_fields:
                fields.append(f"{name}={value}")
        fields.extend(
            [
                f"rate={rate:.1f}/s",
                f"elapsed={elapsed:.1f}s",
                f"eta={eta:.1f}s",
            ]
        )
        print(
            "\r" + prefix + " " + " ".join(fields),
            end="",
            flush=True,
            file=target_stream,
        )

    @staticmethod
    def _signal_freshness_score(latest_signal_date, latest_data_date=None):
        if latest_signal_date is None or pd.isna(latest_signal_date):
            return 0.0, 999
        signal_date = pd.Timestamp(latest_signal_date).tz_localize(None).normalize()
        if latest_data_date is None or pd.isna(latest_data_date):
            reference_date = pd.Timestamp.now("UTC").tz_localize(None).normalize()
        else:
            reference_date = pd.Timestamp(latest_data_date).tz_localize(None).normalize()
        signal_age_days = max(int((reference_date - signal_date).days), 0)
        freshness_score = max(0.0, 100.0 - signal_age_days * 4.0)
        return float(freshness_score), int(signal_age_days)

    @staticmethod
    def _available_memory_bytes():
        try:
            if Path("/proc/meminfo").exists():
                for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                    if line.startswith("MemAvailable:"):
                        parts = line.split()
                        if len(parts) >= 2:
                            return int(parts[1]) * 1024
        except Exception:
            pass
        return None

    @classmethod
    def _resolve_safe_validation_workers(cls, requested_workers, validation_factor_scope="all"):
        requested = int(requested_workers or 0)
        cpu_count = max(int(os.cpu_count() or 1), 1)
        if requested <= 0:
            requested = cpu_count
        available_bytes = cls._available_memory_bytes()
        available_gb = (available_bytes / (1024 ** 3)) if available_bytes else None
        cpu_cap = max(1, cpu_count - 1)
        if str(validation_factor_scope) == "all":
            cpu_cap = min(cpu_cap, 8)
        else:
            cpu_cap = min(cpu_cap, 16)

        if available_gb is None:
            return min(requested, cpu_cap)

        per_worker_gb = 1.5 if str(validation_factor_scope) == "all" else 0.75
        memory_cap = max(1, int(available_gb / per_worker_gb))
        if str(validation_factor_scope) == "all":
            memory_cap = min(memory_cap, 8)
        else:
            memory_cap = min(memory_cap, 16)
        return max(1, min(requested, cpu_cap, memory_cap))

    @classmethod
    def _resolve_safe_analysis_workers(cls, requested_workers, analysis_mode="factor"):
        requested = int(requested_workers or 0)
        cpu_count = max(int(os.cpu_count() or 1), 1)
        if requested <= 0:
            requested = cpu_count
        cpu_cap = max(1, cpu_count - 1)
        if str(analysis_mode) == "factor":
            cpu_cap = min(cpu_cap, 12)
        else:
            cpu_cap = min(cpu_cap, 16)

        available_bytes = cls._available_memory_bytes()
        if available_bytes is None:
            return max(1, min(requested, cpu_cap))

        available_gb = available_bytes / (1024 ** 3)
        per_worker_gb = 1.0 if str(analysis_mode) == "factor" else 0.5
        memory_cap = max(1, int(available_gb / per_worker_gb))
        return max(1, min(requested, cpu_cap, memory_cap))

    @classmethod
    def _resolve_factor_analysis_batch_size(cls, total_stocks, max_workers, analysis_mode="factor"):
        total = max(int(total_stocks or 0), 0)
        workers = max(int(max_workers or 1), 1)
        normalized_mode = str(analysis_mode or "factor").strip().lower()
        if total <= 1 or normalized_mode != "factor":
            return max(1, total)

        available_bytes = cls._available_memory_bytes()
        if available_bytes is None:
            available_gb = None
        else:
            available_gb = available_bytes / (1024 ** 3)

        # Keep multiple waves per worker for smoother progress and more balanced completion.
        target_waves = 3 if total >= workers * 12 else 2
        baseline = int(np.ceil(total / max(workers * target_waves, 1)))

        if available_gb is None:
            memory_cap = 96
        elif available_gb <= 2:
            memory_cap = 16
        elif available_gb <= 4:
            memory_cap = 24
        elif available_gb <= 8:
            memory_cap = 48
        elif available_gb <= 16:
            memory_cap = 96
        else:
            memory_cap = 128

        if total <= workers * 2:
            return max(1, int(np.ceil(total / workers)))

        lower_bound = 8 if total >= workers * 4 else 4
        return max(lower_bound, min(memory_cap, baseline, total))

    @classmethod
    def _resolve_validation_batch_size(cls, validation_factor_scope="all", requested_workers=1):
        available_bytes = cls._available_memory_bytes()
        available_gb = (available_bytes / (1024 ** 3)) if available_bytes else None
        if str(validation_factor_scope) == "all":
            default_batch = 24
            if available_gb is None:
                return default_batch
            if available_gb <= 4:
                return 8
            if available_gb <= 8:
                return 12
            if available_gb <= 16:
                return 16
            return default_batch
        default_batch = 96
        if available_gb is None:
            return default_batch
        if available_gb <= 4:
            return 24
        if available_gb <= 8:
            return 48
        return default_batch

    @classmethod
    def _estimate_safe_validation_stock_count(cls, pool_results, row_safety_fraction=0.30, bytes_per_row=320):
        results = list(pool_results or [])
        if not results:
            return 0

        available_bytes = cls._available_memory_bytes()
        if available_bytes is None:
            return len(results)

        total_rows = sum(max(int(item.get("feature_rows", 0) or 0), 0) for item in results)
        if total_rows <= 0:
            return len(results)

        row_budget = max(int((available_bytes * float(row_safety_fraction)) / max(int(bytes_per_row), 1)), 1)
        if total_rows <= row_budget:
            return len(results)

        avg_rows_per_stock = max(total_rows / max(len(results), 1), 1)
        safe_count = max(32, int(row_budget / avg_rows_per_stock))
        return max(1, min(len(results), safe_count))

    @staticmethod
    def _downsample_validation_pool_results(pool_results, target_count):
        results = list(pool_results or [])
        if target_count is None or target_count <= 0 or len(results) <= target_count:
            return results
        if target_count == 1:
            return [results[0]]

        indices = np.linspace(0, len(results) - 1, num=int(target_count), dtype=int)
        selected = []
        seen = set()
        for index in indices.tolist():
            if index in seen:
                continue
            selected.append(results[index])
            seen.add(index)
        return selected

    @staticmethod
    def _trim_validation_feature_frame(feature_long):
        if feature_long is None or feature_long.empty:
            return pd.DataFrame(columns=VALIDATION_FEATURE_BASE_COLUMNS)

        trimmed = feature_long.copy()
        if "feature_version" not in trimmed.columns:
            trimmed["feature_version"] = "0.1.0"
        if "feature_config_hash" not in trimmed.columns:
            trimmed["feature_config_hash"] = "legacy"
        keep_columns = [column for column in VALIDATION_FEATURE_BASE_COLUMNS if column in trimmed.columns]
        trimmed = trimmed[keep_columns].copy()
        for column in (
            "stock_code",
            "market",
            "exchange",
            "asset_type",
            "frequency",
            "adjust",
            "feature_set",
            "feature_version",
            "feature_config_hash",
            "feature_name",
        ):
            if column in trimmed.columns and trimmed[column].dtype == object:
                trimmed[column] = trimmed[column].astype("category")
        return trimmed

    @staticmethod
    def _trim_validation_ohlcv_frame(ohlcv_frame):
        if ohlcv_frame is None or ohlcv_frame.empty:
            return pd.DataFrame(columns=VALIDATION_OHLCV_BASE_COLUMNS)

        keep_columns = [column for column in VALIDATION_OHLCV_BASE_COLUMNS if column in ohlcv_frame.columns]
        trimmed = ohlcv_frame[keep_columns].copy()
        for column in ("stock_code", "market", "exchange", "asset_type", "frequency", "adjust"):
            if column in trimmed.columns and trimmed[column].dtype == object:
                trimmed[column] = trimmed[column].astype("category")
        return trimmed

    def get_validation_feature_cache_dir(self):
        return self.data_layout.layer_path("meta") / "validation_feature_cache"

    @staticmethod
    def _build_validation_feature_cache_key(
        stock_code,
        days,
        factor_set,
        validation_factor_scope="all",
        validated_feature_names=None,
        feature_version=None,
        feature_config_hash=None,
    ):
        identity = {
            "stock_code": str(stock_code),
            "days": int(days),
            "factor_set": str(factor_set),
            "validation_factor_scope": str(validation_factor_scope),
            "validated_feature_names": [str(item) for item in (validated_feature_names or []) if str(item).strip()],
            "feature_version": str(feature_version or ""),
            "feature_config_hash": str(feature_config_hash or ""),
        }
        cache_key = hashlib.sha1(
            json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:24]
        return cache_key, identity
