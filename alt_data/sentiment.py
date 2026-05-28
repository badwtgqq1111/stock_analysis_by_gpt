"""Sentiment analysis for Chinese financial news.

Two implementations:
- TransformersSentimentAnalyzer: HuggingFace model (configurable)
- RuleBasedSentimentAnalyzer: lightweight keyword-based, zero dependencies
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd


@dataclass
class SentimentResult:
    label: str           # "positive" | "negative" | "neutral"
    score: float          # -1.0 (negative) to 1.0 (positive)
    confidence: float     # model confidence, 0-1


class SentimentAnalyzer:
    """Abstract base for sentiment analysis."""

    def analyze(self, text: str) -> SentimentResult:
        raise NotImplementedError

    def analyze_batch(
        self, texts: list[str], batch_size: int = 32
    ) -> list[SentimentResult]:
        raise NotImplementedError

    def score_column(self, texts: pd.Series, batch_size: int = 32) -> pd.Series:
        """Analyze a pandas Series of texts, returning a Series of scores."""
        results = self.analyze_batch(texts.fillna("").tolist(), batch_size=batch_size)
        return pd.Series([r.score for r in results], index=texts.index)


# --- Rule-based analyzer (no model download needed) ---

# Chinese financial sentiment keywords
_POSITIVE_KEYWORDS = [
    "增长", "上涨", "回购", "增持", "盈利", "突破", "创新高",
    "利好", "分红", "派息", "超预期", "扭亏", "中标",
    "签约", "合作", "获批", "上市", "融资", "研发成功",
    "业绩预增", "产能释放", "订单饱满", "毛利率提升",
    "净利", "营收增长", "用户增长", "市占率提升",
    "跑赢大市", "买入评级", "目标价上调", "估值修复",
]

_NEGATIVE_KEYWORDS = [
    "下跌", "减持", "亏损", "暴跌", "创新低", "下滑",
    "利空", "处罚", "调查", "诉讼", "违约", "退市",
    "爆雷", "商誉减值", "计提", "停产", "重组失败",
    "业绩预亏", "裁员", "关闭", "召回", "事故",
    "跌停", "目标价下调", "跑输大市", "减仓",
    "毛利率下降", "现金流紧张", "债务违约", "被ST",
    "限售解禁", "大股东减持", "质押", "平仓",
]

_NEGATION_WORDS = {"不", "无", "未", "没", "难以", "不会", "无法"}


def _clean_text(text: str) -> str:
    if not text or not isinstance(text, str):
        return ""
    # Remove HTML, extra whitespace
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", "", text)
    return text


def _count_keywords(text: str, keywords: list[str]) -> int:
    return sum(1 for kw in keywords if kw in text)


class RuleBasedSentimentAnalyzer(SentimentAnalyzer):
    """Lightweight keyword-based Chinese financial sentiment.

    No model download needed. Useful as fallback and for quick baselines.
    """

    def __init__(self):
        self._pos_pattern = re.compile("|".join(_POSITIVE_KEYWORDS))
        self._neg_pattern = re.compile("|".join(_NEGATIVE_KEYWORDS))

    def analyze(self, text: str) -> SentimentResult:
        text = _clean_text(text)
        if not text:
            return SentimentResult(label="neutral", score=0.0, confidence=1.0)

        pos_count = len(self._pos_pattern.findall(text))
        neg_count = len(self._neg_pattern.findall(text))

        if pos_count == 0 and neg_count == 0:
            return SentimentResult(label="neutral", score=0.0, confidence=0.5)

        total = pos_count + neg_count
        raw_score = (pos_count - neg_count) / total
        confidence = min(total / 10.0, 1.0)

        if raw_score > 0.15:
            label = "positive"
        elif raw_score < -0.15:
            label = "negative"
        else:
            label = "neutral"

        return SentimentResult(label=label, score=raw_score, confidence=confidence)

    def analyze_batch(
        self, texts: list[str], batch_size: int = 32
    ) -> list[SentimentResult]:
        return [self.analyze(t) for t in texts]


# --- Transformers-based analyzer ---

class TransformersSentimentAnalyzer(SentimentAnalyzer):
    """HuggingFace transformers-based sentiment analyzer.

    Args:
        model_name: HuggingFace model ID for text classification.
        device: 'cpu', 'cuda', or None (auto-detect).
        max_length: Max token length for input texts.
    """

    def __init__(
        self,
        model_name: str = "lxyuan/distilbert-base-multilingual-cased-sentiments-student",
        device: str | None = None,
        max_length: int = 256,
    ):
        self._model_name = model_name
        self._max_length = max_length
        self._pipeline = None

        import torch

        if device is None:
            self._device = 0 if torch.cuda.is_available() else -1
        else:
            self._device = device

    @property
    def pipeline(self):
        if self._pipeline is None:
            self._load()
        return self._pipeline

    def _load(self):
        from transformers import pipeline

        self._pipeline = pipeline(
            "sentiment-analysis",
            model=self._model_name,
            device=self._device,
            max_length=self._max_length,
            truncation=True,
        )

    def analyze(self, text: str) -> SentimentResult:
        text = _clean_text(text)
        if not text:
            return SentimentResult(label="neutral", score=0.0, confidence=1.0)

        raw = self.pipeline(text)[0]
        label = raw["label"].lower()
        conf = raw["score"]

        # Normalize label to positive/negative/neutral
        # Handles various model output formats
        score = _label_to_score(label, conf)

        if score > 0.15:
            norm_label = "positive"
        elif score < -0.15:
            norm_label = "negative"
        else:
            norm_label = "neutral"

        return SentimentResult(label=norm_label, score=score, confidence=conf)

    def analyze_batch(
        self, texts: list[str], batch_size: int = 32
    ) -> list[SentimentResult]:
        cleaned = [_clean_text(t) for t in texts]
        results = []

        for i in range(0, len(cleaned), batch_size):
            batch = cleaned[i:i + batch_size]
            batch = [t if t else " " for t in batch]

            raw_results = self.pipeline(batch)
            for r in raw_results:
                label = r["label"].lower()
                conf = r["score"]
                score = _label_to_score(label, conf)
                if score > 0.15:
                    norm_label = "positive"
                elif score < -0.15:
                    norm_label = "negative"
                else:
                    norm_label = "neutral"
                results.append(
                    SentimentResult(label=norm_label, score=score, confidence=conf)
                )

        return results


def _label_to_score(label: str, confidence: float) -> float:
    """Map various model label formats to a -1..1 score."""
    label = label.lower().strip()

    # 5-star format
    if "star" in label or label in ("1", "2", "3", "4", "5"):
        stars = 3
        for s in ["1", "2", "3", "4", "5"]:
            if s in label:
                stars = int(s)
                break
        return (stars - 3) / 2.0 * confidence

    # 3-class format
    if "positive" in label or "pos" in label:
        return confidence
    if "negative" in label or "neg" in label:
        return -confidence

    # Chinese labels
    if label in ("正面", "积极", "利好"):
        return confidence
    if label in ("负面", "消极", "利空"):
        return -confidence

    return 0.0


_CACHED_ANALYZER: SentimentAnalyzer | None = None


def create_sentiment_analyzer(
    model_name: str | None = None,
    force_rule_based: bool = False,
    device: str | None = None,
) -> SentimentAnalyzer:
    """Create a sentiment analyzer, preferring rule-based for quick start.

    Args:
        model_name: HuggingFace model name. If None, uses default multilingual model.
        force_rule_based: If True, skip HuggingFace model and use rules.
        device: 'cpu', 'cuda', or None (auto).
    """
    global _CACHED_ANALYZER

    if force_rule_based:
        return RuleBasedSentimentAnalyzer()

    try:
        analyzer = TransformersSentimentAnalyzer(
            model_name=model_name
            or "lxyuan/distilbert-base-multilingual-cased-sentiments-student",
            device=device,
        )
        # Warm up by accessing pipeline to trigger early download failure
        _ = analyzer.pipeline
        return analyzer
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "Failed to load transformers sentiment model, falling back to rule-based"
        )
        return RuleBasedSentimentAnalyzer()
