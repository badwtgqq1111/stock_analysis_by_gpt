from pydantic import BaseModel


class StockOption(BaseModel):
    code: str
    name: str
    is_selected: bool


class StockListResponse(BaseModel):
    stocks: list[StockOption]


class OhlcvDataPoint(BaseModel):
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    ma5: float | None = None
    ma10: float | None = None
    ma20: float | None = None
    ma60: float | None = None
    vol_ma5: float | None = None
    vol_ma20: float | None = None
    change_pct: float | None = None
    amplitude: float | None = None
    buy_signal: bool = False
    sell_signal: bool = False


class SignalStats(BaseModel):
    buy_count: int
    sell_count: int
    paired_trades: int
    avg_return: float | None = None
    win_rate: float | None = None
    max_win: float | None = None
    max_loss: float | None = None


class ChipData(BaseModel):
    prices: list[float]
    volumes: list[float]
    current_price: float


class OhlcvLatest(BaseModel):
    close: float
    change_pct: float
    total_bars: int


class StockProfile(BaseModel):
    code: str
    name: str = ""
    board: str = ""
    industry: str = ""
    list_date: str | None = None
    list_status: str = ""
    listed_days: int | None = None
    flags: list[str] = []


class TrendStage(BaseModel):
    stage: str
    label: str
    evidence: str
    metrics: dict


class OhlcvResponse(BaseModel):
    code: str
    name: str
    data: list[OhlcvDataPoint]
    signal_stats: SignalStats | None = None
    chips: ChipData | None = None
    latest: OhlcvLatest
    stage: TrendStage | None = None
    profile: StockProfile | None = None


# ─── Selection ───

class SelectionColumn(BaseModel):
    key: str
    title: str


class SelectionRow(BaseModel):
    stock_code: str
    stock_name: str
    ranking_score: float | None = None
    signal_tier: str | None = None
    setup_type: str | None = None
    risk_adjusted_score: float | None = None
    win_rate: float | None = None
    backtest_return: float | None = None


class SelectionResponse(BaseModel):
    rows: list[dict]
    columns: list[SelectionColumn]
    empty: bool = False
    # ── 多数据源（2026-10-08）──
    source: dict | None = None          # {key,label,note,files,trade_date,row_count,supports_shap}
    available: list[dict] = []          # 各源可用性与文件指纹，供页面展示与切换


class ShapFeature(BaseModel):
    name: str
    value: float
    direction: str


class ShapResponse(BaseModel):
    features: list[ShapFeature]


# ─── Factor IC ───

class FactorICSummary(BaseModel):
    factor: str
    mean_ic: float | None = None
    std_ic: float | None = None
    ic_ir: float | None = None
    mean_rank_ic: float | None = None
    ic_positive_rate: float | None = None


class FactorICResponse(BaseModel):
    dates: list[str]
    factors: list[str]
    ic_series: dict[str, list[float | None]]
    rank_ic_series: dict[str, list[float | None]]
    summary: list[FactorICSummary]
    top10: list[FactorICSummary]
    factor_set: str
    horizon: int
    top_n: int = 10


class FeatureImportance(BaseModel):
    factor: str
    importance: float


class ImportanceResponse(BaseModel):
    factor_set: str
    importances: list[FeatureImportance]
    feature_count: int
    train_rows: int


# ─── Portfolio ───

class PortfolioResponse(BaseModel):
    dates: list[str]
    equity: list[float]
    drawdown: list[float]
    metrics: dict[str, str]
    holdings: list[dict]
    description: str
