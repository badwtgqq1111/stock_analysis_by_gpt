"""Point-in-time event feature engineering for daily ML alpha panels."""

from __future__ import annotations

import numpy as np
import pandas as pd


POSITIVE_KEYWORDS = ("positive", "upgrade", "beat", "tailwind", "approval", "guidance_up", "contract", "政策利好", "中标", "增持")
NEGATIVE_KEYWORDS = ("negative", "downgrade", "miss", "litigation", "risk", "guidance_down", "default", "处罚", "诉讼", "减持")
LITIGATION_KEYWORDS = ("litigation", "lawsuit", "investigation", "penalty", "诉讼", "调查", "处罚", "罚款")
POLICY_KEYWORDS = ("policy", "regulation", "subsidy", "政策", "补贴", "指引")
EARNINGS_KEYWORDS = ("earnings", "profit", "revenue", "guidance", "业绩", "盈利", "收入")
MANAGEMENT_KEYWORDS = ("management", "ceo", "cfo", "director", "管理层", "董事", "任命", "辞任")


def _coerce_events(events: pd.DataFrame) -> pd.DataFrame:
    if events is None or events.empty:
        return pd.DataFrame()
    frame = events.copy()
    if "stock_code" not in frame.columns:
        raise ValueError("events must include stock_code")
    if "available_at" not in frame.columns:
        if "publish_time" in frame.columns:
            frame["available_at"] = frame["publish_time"]
        elif "event_time" in frame.columns:
            frame["available_at"] = frame["event_time"]
        elif "event_date" in frame.columns:
            frame["available_at"] = frame["event_date"]
        else:
            raise ValueError("events must include available_at/publish_time/event_time/event_date")
    frame["available_at"] = pd.to_datetime(frame["available_at"], errors="coerce")
    frame.dropna(subset=["available_at"], inplace=True)
    frame["trade_date"] = frame["available_at"].dt.normalize()
    frame["stock_code"] = frame["stock_code"].astype(str).str.zfill(5)
    text_cols = [col for col in ("event_type", "title", "summary", "tag", "theme") if col in frame.columns]
    frame["_event_text"] = frame[text_cols].fillna("").astype(str).agg(" ".join, axis=1) if text_cols else ""
    event_score = frame.get("event_score", frame.get("score"))
    if event_score is None:
        event_score = pd.Series(0.0, index=frame.index)
    frame["event_score"] = pd.to_numeric(event_score, errors="coerce").fillna(0.0)
    evidence_quality = frame.get("evidence_quality", frame.get("confidence"))
    if evidence_quality is None:
        evidence_quality = pd.Series(0.5, index=frame.index)
    frame["evidence_quality"] = pd.to_numeric(evidence_quality, errors="coerce").fillna(0.5)
    return frame


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = str(text).lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def build_event_feature_panel(
    events: pd.DataFrame,
    *,
    stock_codes: list[str] | None = None,
    start_date=None,
    end_date=None,
    windows: tuple[int, ...] = (1, 5, 20),
) -> pd.DataFrame:
    """Build daily event features with available_at discipline."""
    frame = _coerce_events(events)
    if frame.empty:
        return pd.DataFrame()
    if stock_codes:
        allowed = {str(code).zfill(5) for code in stock_codes}
        frame = frame.loc[frame["stock_code"].isin(allowed)].copy()
    if start_date is not None:
        frame = frame.loc[frame["trade_date"] >= pd.Timestamp(start_date).normalize()]
    if end_date is not None:
        frame = frame.loc[frame["trade_date"] <= pd.Timestamp(end_date).normalize()]
    if frame.empty:
        return pd.DataFrame()

    text = frame["_event_text"]
    frame["positive_flag"] = text.map(lambda value: _contains_any(value, POSITIVE_KEYWORDS)).astype(float)
    frame["negative_flag"] = text.map(lambda value: _contains_any(value, NEGATIVE_KEYWORDS)).astype(float)
    frame["policy_flag"] = text.map(lambda value: _contains_any(value, POLICY_KEYWORDS)).astype(float)
    frame["earnings_flag"] = text.map(lambda value: _contains_any(value, EARNINGS_KEYWORDS)).astype(float)
    frame["litigation_flag"] = text.map(lambda value: _contains_any(value, LITIGATION_KEYWORDS)).astype(float)
    frame["management_flag"] = text.map(lambda value: _contains_any(value, MANAGEMENT_KEYWORDS)).astype(float)
    frame["source"] = frame.get("source", "unknown")
    agg_map = {
        "event_count": ("stock_code", "size"),
        "positive_event_score": ("positive_flag", "sum"),
        "negative_event_score": ("negative_flag", "sum"),
        "policy_tailwind_score": ("policy_flag", "sum"),
        "earnings_guidance_score": ("earnings_flag", "sum"),
        "litigation_risk_score": ("litigation_flag", "sum"),
        "management_change_score": ("management_flag", "sum"),
        "evidence_quality": ("evidence_quality", "mean"),
        "source_diversity": ("source", "nunique"),
        "event_score": ("event_score", "sum"),
    }
    if "duplicate_cluster_id" in frame.columns:
        agg_map["duplicate_cluster_count"] = ("duplicate_cluster_id", "nunique")
    else:
        frame["duplicate_cluster_id"] = ""
        agg_map["duplicate_cluster_count"] = ("duplicate_cluster_id", lambda s: 0)
    daily = frame.groupby(["trade_date", "stock_code"]).agg(**agg_map).reset_index()
    all_dates = pd.date_range(daily["trade_date"].min(), daily["trade_date"].max(), freq="D")
    outputs = []
    sum_cols = [
        "event_count",
        "positive_event_score",
        "negative_event_score",
        "policy_tailwind_score",
        "earnings_guidance_score",
        "litigation_risk_score",
        "management_change_score",
        "event_score",
        "duplicate_cluster_count",
    ]
    mean_cols = ["evidence_quality"]
    max_cols = ["source_diversity"]
    numeric_cols = [col for col in daily.columns if col not in {"trade_date", "stock_code"}]
    for code, group in daily.groupby("stock_code"):
        indexed = group.set_index("trade_date").reindex(all_dates)
        indexed["stock_code"] = code
        indexed[numeric_cols] = indexed[numeric_cols].fillna(0.0)
        for window in windows:
            for col in sum_cols:
                if col in indexed.columns:
                    indexed[f"{col}_{window}d"] = indexed[col].rolling(window, min_periods=1).sum()
            for col in mean_cols:
                if col in indexed.columns:
                    indexed[f"{col}_{window}d"] = indexed[col].rolling(window, min_periods=1).mean()
            for col in max_cols:
                if col in indexed.columns:
                    indexed[f"{col}_{window}d"] = indexed[col].rolling(window, min_periods=1).max()
            for col in numeric_cols:
                if f"{col}_{window}d" in indexed.columns:
                    continue
                indexed[f"{col}_{window}d"] = indexed[col].rolling(window, min_periods=1).sum()
        outputs.append(indexed.reset_index().rename(columns={"index": "trade_date"}))
    result = pd.concat(outputs, ignore_index=True, sort=False)
    return result


def event_features_to_long(frame: pd.DataFrame, *, feature_set: str = "event_daily", feature_version: str = "v1") -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    id_cols = {"trade_date", "stock_code"}
    feature_cols = [col for col in frame.columns if col not in id_cols]
    long = frame.melt(id_vars=["trade_date", "stock_code"], value_vars=feature_cols, var_name="feature_name", value_name="feature_value")
    long["market"] = "HK"
    long["frequency"] = "daily"
    long["feature_set"] = feature_set
    long["feature_version"] = feature_version
    long["available_at"] = pd.to_datetime(long["trade_date"], errors="coerce")
    return long
