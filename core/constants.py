"""Constants used across the analyzer core modules."""

DEFAULT_FACTOR_SET = "qlib_alpha158"
DEFAULT_FACTOR_SCORE_CONFIG = {
    "trend": {
        "MA5": {"weight": 0.14, "higher_is_better": False},
        "MA20": {"weight": 0.16, "higher_is_better": False},
        "MA60": {"weight": 0.10, "higher_is_better": False},
        "MAX20": {"weight": 0.10, "higher_is_better": False},
        "MAX60": {"weight": 0.08, "higher_is_better": False},
        "RSV20": {"weight": 0.10, "higher_is_better": True},
        "CNTD20": {"weight": 0.10, "higher_is_better": True},
        "SUMD20": {"weight": 0.12, "higher_is_better": True},
    },
    "quality": {
        "VMA20": {"weight": 0.16, "higher_is_better": False},
        "VSTD20": {"weight": 0.08, "higher_is_better": False},
        "WVMA20": {"weight": 0.10, "higher_is_better": False},
        "VSUMD20": {"weight": 0.12, "higher_is_better": True},
        "CORD20": {"weight": 0.10, "higher_is_better": True},
        "CNTP20": {"weight": 0.08, "higher_is_better": True},
        "SUMP20": {"weight": 0.12, "higher_is_better": True},
        "RSQR60": {"weight": 0.10, "higher_is_better": True},
        "RESI20": {"weight": 0.14, "higher_is_better": False},
    },
    "risk": {
        "STD20": {"weight": 0.38, "higher_is_better": False},
        "STD60": {"weight": 0.26, "higher_is_better": False},
        "WVMA20": {"weight": 0.18, "higher_is_better": False},
        "VSTD20": {"weight": 0.18, "higher_is_better": False},
    },
    "validated": {},
    "weights": {
        "trend_score": 0.40,
        "quality_score": 0.30,
        "risk_score": 0.15,
        "validated_score": 0.15,
    },
}


# Factor-to-component classification rules based on Alpha158 operator semantics
FACTOR_CLASSIFICATION_RULES = {
    "trend": {
        "operators": {
            "MA", "ROC", "BETA", "MAX", "MIN", "RSV", "IMAX", "IMIN", "IMXD",
            "RANK", "QTLU", "QTLD",
        },
        "ta_indicators": {
            "TA_MACD_DIF", "TA_MACD_DEA", "TA_MACD_HIST",
            "TA_ADX", "TA_ADXR", "TA_PLUS_DI", "TA_MINUS_DI",
            "TA_AROON_UP", "TA_AROON_DOWN", "TA_AROONOSC",
            "TA_TRIX", "TA_APO", "TA_PPO", "TA_MOM", "TA_ROC",
            "TA_KAMA",
        },
        "price_fields": {"OPEN", "HIGH", "LOW", "CLOSE", "VWAP"},
        "description": "Price trend and momentum factors",
    },
    "quality": {
        "operators": {
            "VMA", "WVMA", "VSUMP", "VSUMN", "VSUMD", "CORR", "CORD",
            "CNTP", "CNTN", "CNTD", "SUMP", "SUMN", "SUMD", "RSQR", "RESI",
        },
        "kbar_operators": {
            "KMID", "KLEN", "KMID2", "KUP", "KUP2", "KLOW", "KLOW2", "KSFT", "KSFT2",
        },
        "ta_indicators": {
            "TA_OBV", "TA_AD", "TA_ADOSC", "TA_MFI", "TA_BOP",
            "TA_STOCH_K", "TA_STOCH_D", "TA_ULTOSC",
        },
        "volume_prefix": "VOLUME",
        "description": "Volume-price relationship and quality factors",
    },
    "risk": {
        "operators": {"STD", "VSTD"},
        "ta_indicators": {
            "TA_ATR", "TA_NATR", "TA_TRANGE",
            "TA_BBANDS_PCT_B", "TA_BBANDS_WIDTH",
        },
        "description": "Volatility and risk factors",
    },
    "sentiment": {
        "ta_indicators": {
            "TA_RSI", "TA_STOCHRSI_K", "TA_STOCHRSI_D",
            "TA_WILLR", "TA_CCI", "TA_CMO",
        },
        "description": "Sentiment and overbought/oversold factors",
    },
}

VALIDATION_FEATURE_BASE_COLUMNS = [
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
]

VALIDATION_OHLCV_BASE_COLUMNS = [
    "trade_date",
    "stock_code",
    "market",
    "exchange",
    "asset_type",
    "frequency",
    "adjust",
    "close",
]

VALIDATION_FEATURE_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
