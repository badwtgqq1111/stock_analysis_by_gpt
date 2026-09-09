"""Versioned paper-account dataset contracts."""

PAPER_ORDER_FIELDS = ["order_id", "run_id", "account_id", "strategy_version", "stock_code", "decision_time", "executable_from", "side", "target_value", "status"]
PAPER_FILL_FIELDS = [*PAPER_ORDER_FIELDS, "fill_time", "quantity", "price", "commission", "slippage_bps", "participation_rate"]
PAPER_POSITION_FIELDS = ["account_id", "run_id", "asof_date", "stock_code", "quantity", "market_value", "close"]
PAPER_NAV_FIELDS = ["account_id", "run_id", "asof_date", "cash", "market_value", "nav", "drawdown", "daily_return"]
