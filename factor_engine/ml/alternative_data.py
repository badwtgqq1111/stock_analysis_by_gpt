"""Point-in-time CN alternative-evidence import and daily feature materialization."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd


ALT_EVIDENCE_COLUMNS = [
    "evidence_id", "stock_code", "market", "source", "event_type", "title", "raw_text", "url",
    "published_at", "available_at", "ingested_at", "sentiment_score", "attention_score", "quality_status",
]


def normalize_cn_alternative_evidence(frame: pd.DataFrame, *, default_source="manual_import") -> pd.DataFrame:
    """Normalize evidence and preserve publication/availability times for PIT filtering."""
    source = frame.copy() if frame is not None else pd.DataFrame()
    if source.empty:
        return pd.DataFrame(columns=ALT_EVIDENCE_COLUMNS)
    aliases = {"date": "published_at", "datetime": "published_at", "content": "raw_text", "summary": "raw_text"}
    source = source.rename(columns={key: value for key, value in aliases.items() if key in source.columns})
    for column in ("stock_code", "title", "raw_text", "url", "event_type", "source"):
        if column not in source:
            source[column] = ""
    source["market"] = "CN"
    source["source"] = source["source"].replace("", pd.NA).fillna(default_source).astype(str)
    source["event_type"] = source["event_type"].replace("", pd.NA).fillna("news").astype(str)
    source["published_at"] = pd.to_datetime(source.get("published_at"), errors="coerce")
    source["available_at"] = pd.to_datetime(source.get("available_at", source["published_at"]), errors="coerce").fillna(source["published_at"])
    source["ingested_at"] = pd.Timestamp.now("UTC")
    sentiment = source.get("sentiment_score", pd.Series(0.0, index=source.index))
    attention = source.get("attention_score", pd.Series(1.0, index=source.index))
    source["sentiment_score"] = pd.to_numeric(sentiment, errors="coerce").fillna(0.0).clip(-1.0, 1.0)
    source["attention_score"] = pd.to_numeric(attention, errors="coerce").fillna(1.0).clip(lower=0.0)
    source["quality_status"] = source["available_at"].notna().map({True: "valid", False: "invalid"})
    source["evidence_id"] = [hashlib.sha256("|".join(map(str, values)).encode()).hexdigest()[:24] for values in source[["stock_code", "source", "url", "title", "available_at"]].itertuples(index=False, name=None)]
    return source.reindex(columns=ALT_EVIDENCE_COLUMNS).drop_duplicates("evidence_id", keep="last").reset_index(drop=True)


def materialize_alternative_features(evidence: pd.DataFrame, trade_dates: pd.DataFrame) -> pd.DataFrame:
    """Create as-of daily features from evidence that was available by that date."""
    required = {"stock_code", "trade_date"}
    if trade_dates is None or trade_dates.empty or required - set(trade_dates.columns):
        return pd.DataFrame()
    events = evidence.copy() if evidence is not None else pd.DataFrame(columns=ALT_EVIDENCE_COLUMNS)
    if events.empty:
        return pd.DataFrame(columns=["stock_code", "trade_date", "alt_event_count_7d", "alt_sentiment_7d", "alt_attention_7d", "available_at"])
    events["available_at"] = pd.to_datetime(events["available_at"], errors="coerce")
    events = events[events["quality_status"] == "valid"].dropna(subset=["available_at", "stock_code"])
    rows = []
    for item in trade_dates[["stock_code", "trade_date"]].drop_duplicates().itertuples(index=False):
        date = pd.Timestamp(item.trade_date)
        subset = events[(events.stock_code.astype(str) == str(item.stock_code)) & (events.available_at <= date) & (events.available_at > date - pd.Timedelta(days=7))]
        rows.append({"stock_code": item.stock_code, "trade_date": date, "alt_event_count_7d": len(subset),
                     "alt_sentiment_7d": float(subset["sentiment_score"].mean()) if not subset.empty else 0.0,
                     "alt_attention_7d": float(subset["attention_score"].sum()) if not subset.empty else 0.0,
                     "available_at": date})
    return pd.DataFrame(rows)


def write_alternative_data_report(evidence: pd.DataFrame, output_dir="output/alternative_data") -> dict:
    directory = Path(output_dir); directory.mkdir(parents=True, exist_ok=True)
    summary = {"rows": int(len(evidence)), "valid_rows": int((evidence.get("quality_status") == "valid").sum()) if not evidence.empty else 0,
               "sources": evidence["source"].value_counts().to_dict() if not evidence.empty else {}}
    path = directory / "cn_alternative_evidence_report.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"report_path": str(path), **summary}
