from .bullish_impulse_cyc import BullishImpulseCYCBuyStrategy
from .bullish_trend_2560 import BullishTrend2560BuyStrategy
from .current_strategy import CurrentStrategy
from .bottom_volume_reversal import BottomVolumeReversalBuyStrategy
from .sell_rules import (
    BollingerUpperStochOrUnifiedSellStrategy,
    StochOverboughtOrUnifiedSellStrategy,
    UnifiedHighVolumeBearishSellStrategy,
)
from .trend_pullback_rebound import TrendPullbackReboundBuyStrategy
from .trend_range_band import TrendRangeBandBuyStrategy


STRATEGY_SUITE = [
    {
        'code': 'current_strategy',
        'name': 'Current Strategy',
        'buy_strategy': CurrentStrategy(),
        'sell_strategy': CurrentStrategy(),
    },
    {
        'code': 'bullish_trend_2560',
        'name': 'Bullish Trend 2560',
        'buy_strategy': BullishTrend2560BuyStrategy(),
        'sell_strategy': UnifiedHighVolumeBearishSellStrategy(),
    },
    {
        'code': 'bullish_impulse_cyc',
        'name': 'Bullish Impulse CYC',
        'buy_strategy': BullishImpulseCYCBuyStrategy(),
        'sell_strategy': UnifiedHighVolumeBearishSellStrategy(),
    },
    {
        'code': 'trend_pullback_rebound',
        'name': 'Trend Pullback Rebound',
        'buy_strategy': TrendPullbackReboundBuyStrategy(),
        'sell_strategy': StochOverboughtOrUnifiedSellStrategy(),
    },
    {
        'code': 'trend_range_band',
        'name': 'Trend Range Band',
        'buy_strategy': TrendRangeBandBuyStrategy(),
        'sell_strategy': BollingerUpperStochOrUnifiedSellStrategy(),
    },
    {
        'code': 'bottom_volume_reversal',
        'name': 'Bottom Volume Reversal',
        'buy_strategy': BottomVolumeReversalBuyStrategy(),
        'sell_strategy': UnifiedHighVolumeBearishSellStrategy(),
    },
]
