"""策略注册表 — 模型驱动单一策略.

Signal → Strategy → Decision 架构下, 信号来自 LightGBM 连续分数,
策略统一为 Top-K + 末位淘汰, 不再需要多策略对比。
"""

STRATEGY_SUITE = [
    {
        "code": "model_driven",
        "name": "LightGBM Model-Driven Strategy",
    },
]
