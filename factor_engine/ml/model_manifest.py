"""Model manifest helpers for reproducible LightGBM runs."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from datetime import datetime, timezone
from typing import Any


def _safe_version(module_name: str) -> str | None:
    try:
        module = __import__(module_name)
        return getattr(module, "__version__", None)
    except Exception:
        return None


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        commit = result.stdout.strip()
        return commit or None
    except Exception:
        return None


def feature_config_hash(feature_columns: list[str], extra: dict[str, Any] | None = None) -> str:
    payload = {
        "feature_columns": list(feature_columns or []),
        "extra": extra or {},
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def build_lightgbm_model_manifest(
    *,
    run_id: str | None = None,
    factor_set: str | None = None,
    feature_columns: list[str] | None = None,
    model_metadata: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = dict(model_metadata or {})
    columns = list(feature_columns or metadata.get("feature_columns") or [])
    config_extra = {
        "factor_set": factor_set,
        "feature_preprocess": metadata.get("feature_preprocess"),
        "neutralization_mode": metadata.get("neutralization_mode"),
        "label_method": metadata.get("label_method"),
        "objective": metadata.get("objective"),
        "params": params or {},
    }
    return {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(),
        "python_version": platform.python_version(),
        "lightgbm_version": _safe_version("lightgbm"),
        "pandas_version": _safe_version("pandas"),
        "numpy_version": _safe_version("numpy"),
        "factor_set": factor_set,
        "feature_count": len(columns),
        "feature_columns": columns,
        "feature_hash": feature_config_hash(columns, config_extra),
        "feature_preprocess_config": metadata.get("feature_preprocess"),
        "neutralization_config": {
            "mode": metadata.get("neutralization_mode"),
            "neutralize_label": metadata.get("neutralize_label"),
        },
        "label_config": {
            "label_method": metadata.get("label_method"),
            "label_horizon": metadata.get("label_horizon"),
            "execution_delay": metadata.get("execution_delay"),
        },
        "objective_mode": metadata.get("objective"),
        "oos_metrics": metadata.get("oos_metrics"),
        "pure_oos_prediction_rows": metadata.get("pure_oos_prediction_rows"),
        "latest_inference_prediction_rows": metadata.get("latest_inference_prediction_rows"),
        "top_features": metadata.get("top_features"),
    }
