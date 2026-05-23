"""Core package - exports StockAnalyzer and classify_factor."""

from core.analyzer import StockAnalyzer
from core.formatting import classify_factor

__all__ = ["StockAnalyzer", "classify_factor"]
