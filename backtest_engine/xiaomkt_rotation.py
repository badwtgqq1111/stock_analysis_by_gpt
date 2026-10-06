#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""小市值轮动策略（知乎「鳄鱼鱼」2022-10 版）—— CN 流水线阶段实现。

规则映射（文章 -> 代码）
  1. 股票池：中小综指成分股（本地无 PIT 成分，用沪深主板 / 深市 002+003 代理）
  2. 六层过滤：次新（上市<1 年）/ ST / 科创+创业板 / 停牌 / 昨日涨跌停 / 高价股(>80 元)
  3. 调仓：每周二 10:00 -> 周二开盘价成交（本地只有日线，开盘价近似）
  4. 卖出保护：昨日涨停不卖，直到涨停打开
  5. 仓位：等权 top_n（默认 5 只）
  6. 风控：个股 -7% 硬止损 / 股票池等权跌幅 >5% 清仓 / 魔咒月（1/5-2/5、4/5-4/30）空仓
  7. 成本：佣金万 2.5（最低 5 元）+ 印花税千 1（卖出）+ 滑点万 3（双边）

防未来函数：T 日调仓只用 T-1 收盘为止的信息，成交用 T 日开盘价；涨停开盘不买、
跌停开盘不卖、停牌不成交。

只读约定：行情读 clean 层日线 parquet、市值读 valuation_snapshot；阶段唯一的额外汇总是
把上市日历缓存写到 `assets/data/meta/cn_stock_basic.parquet`（存在即直接复用，不联网）。

已知偏差（随报告一并输出，见 stage summary 的 `data_warnings`）：
  * 本地价格面板无退市股（幸存者偏差，方向为高估）；
  * 无 PIT 指数成分，股票池为代理；
  * ST 用「5% 触板占比」代理，偏松；
  * 市值来自估值快照前向填充 + 价格比例还原。
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
import pandas as pd

MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")
SME_PREFIX = ("002", "003")
LIMIT_UP = 0.095
LIMIT_DOWN = -0.095
DAILY_FIELDS = ("open", "high", "low", "close", "volume", "amount")


@dataclass
class RotationConfig:
    start: str = "2015-01-05"
    end: str = ""
    initial_cash: float = 100_000.0
    top_n: int = 5
    candidate_pool: int = 50
    cadence: str = "weekly"
    universe: str = "main"
    max_price: float = 80.0
    min_listing_days: int = 365
    exclude_st: bool = True
    st_filter_mode: str = "band"       # band = 5% 涨跌停板识别；touch = 触板计数；both = 两者取并
    st_fast_window: int = 20           # 快速窗：发现"刚被 ST"的滞后上限（交易日）
    st_exit: bool = True               # 持仓中途被 ST 判定命中 -> 下一个调仓日无条件卖出
    min_price: float = 0.0             # 剔除低价股（元）；1.5 以上可挡面值退市风险段
    min_amount: float = 0.0            # 20 日均成交额下限（元）
    ma_timing_window: int = 0          # >0：股票池等权指数跌破 N 日均线则清仓不买入
    cash_buffer: float = 0.0           # 常备现金比例（0~1），降低整体暴露
    impact_coef: float = 0.0           # 平方根冲击律系数：impact = coef * 日波动 * sqrt(成交额/ADV20)
    max_participation: float = 0.0     # >0：单笔委托额上限 = 该比例 × 20 日均成交额（超限少买，不追）
    execution_bars: int = 0            # >0：用 5min 分时把建仓摊到前 N 根（0 = 开盘价一把成交）
    intraday_root: str = "raw/intraday_baostock"
    intraday_freq: str = "5"
    bar_participation_cap: float = 0.10  # 每根 5min bar 最多吃掉的成交额比例
    use_limitup_hold: bool = True
    use_stop_loss: bool = True
    stop_loss: float = 0.07
    use_market_filter: bool = True
    market_filter: float = 0.05
    market_filter_lookback: int = 5
    use_magic_months: bool = True
    commission: float = 0.00025
    stamp_duty: float = 0.001
    slippage: float = 0.0003
    min_commission: float = 5.0
    cap_ffill_limit: int = 20
    # 幸存者偏差修复：并入退市股日线 + 历史市值（scripts/refresh_cn_delisted_daily.py 产出）
    include_delisted: bool = True
    delisted_panel: str = "meta/cn_delisted_daily.parquet"
    delist_haircut: float = 0.0        # 退市/长期停牌清算折价（0 = 按最后收盘价）
    delist_exit_grace: int = 20


def load_daily_panel(base_dir: Path, universe: str) -> pd.DataFrame:
    return _load_daily_panel(base_dir, universe, include_delisted=False)[0]


def _load_daily_panel(base_dir: Path, universe: str, *, include_delisted: bool,
                      delisted_panel: str = "meta/cn_delisted_daily.parquet") -> tuple[pd.DataFrame, dict]:
    root = base_dir / "clean" / "ohlcv" / "market=CN"
    files = sorted(str(p) for p in root.glob("exchange=*/asset_type=equity/frequency=daily/adjust=qfq/**/*.parquet"))
    if not files:
        raise FileNotFoundError(f"no CN daily qfq parquet under {root}")
    import pyarrow.dataset as ds

    table = ds.dataset(files, format="parquet").to_table(columns=["trade_date", "stock_code", *DAILY_FIELDS])
    daily = table.to_pandas().rename(columns={"trade_date": "d", "stock_code": "code"})
    daily["d"] = pd.to_datetime(daily["d"])
    daily["market_cap"] = pd.NA
    delisted_meta = {"included": False, "rows": 0, "codes": 0}
    if include_delisted:
        path = base_dir / delisted_panel
        if path.exists():
            extra = pd.read_parquet(path).rename(columns={"trade_date": "d", "stock_code": "code"})
            extra["d"] = pd.to_datetime(extra["d"])
            extra = extra[[c for c in ("d", "code", *DAILY_FIELDS, "market_cap") if c in extra.columns]]
            if "market_cap" not in extra.columns:
                extra["market_cap"] = pd.NA
            delisted_meta = {
                "included": True,
                "rows": int(len(extra)),
                "codes": int(extra["code"].nunique()),
                "window": [str(extra["d"].min().date()), str(extra["d"].max().date())],
                "source": str(path.relative_to(base_dir)),
            }
            daily = pd.concat([daily, extra], ignore_index=True)
        else:
            delisted_meta = {"included": False, "rows": 0, "codes": 0, "missing": str(path)}
    daily = daily[daily["code"].str.len() == 9]
    prefixes = SME_PREFIX if universe == "sme" else MAIN_PREFIX
    daily = daily[daily["code"].str[:3].isin(prefixes)].copy()
    daily = daily.drop_duplicates(subset=["d", "code"], keep="first")
    volume = daily["volume"].where(daily["volume"] > 0)
    daily["vwap"] = daily["amount"] / volume
    calendar = daily.groupby("d")["code"].size()
    calendar = calendar[calendar >= 200].index
    return daily[daily["d"].isin(calendar)].copy(), delisted_meta


def load_valuation_panel(base_dir: Path) -> tuple[pd.DataFrame, str]:
    """市值快照：ClickHouse 优先，Parquet 镜像回退（回退原因写进报告）。"""
    try:
        import clickhouse_connect

        client = clickhouse_connect.get_client(
            host=os.environ.get("CLICKHOUSE_HOST", "localhost"),
            port=int(os.environ.get("CLICKHOUSE_HTTP_PORT", "8123")),
            username=os.environ.get("CLICKHOUSE_USER", "default"),
            password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
            database=os.environ.get("CLICKHOUSE_DATABASE", "quant"),
        )
        frame = client.query_df(
            "select trade_date as d, stock_code as code, market_cap from valuation_snapshot_meta "
            "where market_cap is not null and market='CN' and asset_type='equity' and market_cap>0"
        )
        frame["d"] = pd.to_datetime(frame["d"])
        source = "clickhouse:valuation_snapshot_meta"
    except Exception as exc:  # 凭据缺失或服务不可用时退化为本地镜像
        files = sorted(str(p) for p in (base_dir / "meta" / "valuation_snapshot" / "market=CN").glob("**/*.parquet"))
        if not files:
            raise RuntimeError(f"no valuation snapshot (clickhouse failed: {exc}; no parquet mirror)") from exc
        import pyarrow.dataset as ds

        frame = ds.dataset(files, format="parquet").to_table(
            columns=["trade_date", "stock_code", "market_cap"]
        ).to_pandas().rename(columns={"trade_date": "d", "stock_code": "code"})
        frame["d"] = pd.to_datetime(frame["d"])
        frame = frame[frame["market_cap"].notna() & (frame["market_cap"] > 0)]
        source = f"parquet_mirror(fallback: {type(exc).__name__})"
    return frame, source


def load_listing_calendar(base_dir: Path, *, allow_relay: bool = True) -> tuple[pd.Series, str]:
    """上市日期：本地快照 -> Tushare 中继刷新落盘 -> 面板首根 bar 兜底。"""
    path = base_dir / "meta" / "cn_stock_basic.parquet"
    frame = None
    if path.exists():
        frame = pd.read_parquet(path)
        source = f"local:{path.relative_to(base_dir)}"
    elif allow_relay:
        try:
            from data.ingest.providers.cn_tushare_relay import CNTushareRelayClient

            client = CNTushareRelayClient()
            pages: list = []
            for status in ("L", "D", "P"):
                offset = 0
                while True:
                    page, _gateway = client.get("stock-basic", limit=2000, offset=offset, list_status=status)
                    if page is None or page.empty:
                        break
                    pages.append(page.assign(list_status=status))
                    offset += len(page)
                    if len(page) < 2000 or offset > 20000:
                        break
            if not pages:
                raise RuntimeError("relay returned no stock-basic rows")
            frame = pd.concat(pages, ignore_index=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            frame.to_parquet(path, index=False)
            source = f"relay->{path.relative_to(base_dir)}"
        except Exception as exc:
            return pd.Series(dtype="datetime64[ns]"), f"derived_from_first_bar(relay failed: {type(exc).__name__})"
    else:
        return pd.Series(dtype="datetime64[ns]"), "derived_from_first_bar(local snapshot missing)"

    frame = frame.copy()
    frame["code"] = frame["ts_code"].str.replace("^T", "", regex=True)
    frame["list_dt"] = pd.to_datetime(frame["list_date"], format="%Y%m%d", errors="coerce")
    series = frame.dropna(subset=["list_dt"]).drop_duplicates("code").set_index("code")["list_dt"]
    return series, source


def build_panel(base_dir: Path, cfg: RotationConfig) -> tuple[dict, dict]:
    daily, delisted_meta = _load_daily_panel(
        base_dir, cfg.universe, include_delisted=cfg.include_delisted, delisted_panel=cfg.delisted_panel
    )
    valuation, valuation_source = load_valuation_panel(base_dir)
    extra_cap = daily[["d", "code", "market_cap"]].dropna(subset=["market_cap"]) if "market_cap" in daily else None
    if extra_cap is not None and not extra_cap.empty:
        valuation = pd.concat([valuation, extra_cap], ignore_index=True)
        valuation_source = f"{valuation_source} + delisted_daily_basic({len(extra_cap)}行)"
    listing_map, listing_source = load_listing_calendar(base_dir)

    fields = {col: daily.pivot_table(index="d", columns="code", values=col, aggfunc="last")
              for col in (*DAILY_FIELDS, "vwap")}
    dates = fields["close"].index
    codes = list(fields["close"].columns)
    close = fields["close"]

    ret = close.pct_change(fill_method=None)
    limit_up = ((ret >= LIMIT_UP) & (close >= fields["high"] - 1e-9)).fillna(False)
    limit_down = ((ret <= LIMIT_DOWN) & (close <= fields["low"] + 1e-9)).fillna(False)
    abs_ret = ret.abs()
    hit5 = ((abs_ret >= 0.048) & (abs_ret <= 0.055)
            & ((close >= fields["high"] - 1e-9) | (close <= fields["low"] + 1e-9)))
    hit10 = ((abs_ret >= 0.095) & ((close >= fields["high"] - 1e-9) | (close <= fields["low"] + 1e-9)))
    hit5_60 = hit5.rolling(60, min_periods=30).mean()
    hit10_60 = hit10.rolling(60, min_periods=30).mean()
    # ST 判定：
    #   band  —— ST/*ST 涨跌幅被限制在 ±5%，因此过去 120 个交易日内「最大单日涨跌幅 ≤5.2%」
    #            即可判定该窗口处于 5% 板（比数触板日稳健得多）；
    #   touch —— 5% 触板占比显著高于 10% 触板占比（旧口径，偏松，保留做对照）。
    # 快速窗让「刚被 ST」在上限个交易日内被发现；120 日窗口覆盖长期 5% 板。
    fast = max(int(cfg.st_fast_window), 3)
    band_5pct_fast = abs_ret.rolling(fast, min_periods=max(3, fast // 4)).max() <= 0.052
    band_5pct = abs_ret.rolling(120, min_periods=60).max() <= 0.052
    touch_5pct = (hit5_60 > 0.02) & (hit5_60 > 3 * hit10_60)
    if cfg.st_filter_mode == "band":
        st_proxy = band_5pct | band_5pct_fast
    elif cfg.st_filter_mode == "touch":
        st_proxy = touch_5pct | band_5pct_fast
    else:
        st_proxy = band_5pct | band_5pct_fast | touch_5pct
    st_proxy = st_proxy.fillna(False)
    amount_ma = fields["amount"].rolling(20, min_periods=10).mean()
    vol20 = ret.rolling(20, min_periods=10).std()
    # 股票池等权指数（用于趋势择时）：每日对全部有 bar 的标的取等权收益再累乘
    universe_index = (1 + ret.mean(axis=1, skipna=True).fillna(0.0)).cumprod()

    first_bar = close.apply(lambda s: s.first_valid_index())
    list_date = pd.Series({c: listing_map.get(c, first_bar[c]) for c in codes})
    age_days = pd.DataFrame(
        (dates.values[:, None] - list_date.values[None, :]).astype("timedelta64[D]").astype(int),
        index=dates, columns=codes,
    )

    cap = valuation.pivot_table(index="d", columns="code", values="market_cap", aggfunc="last")
    cap = cap.reindex(index=dates, columns=codes)
    anchor = cap.notna()
    vwap = fields["vwap"]
    cap_ff = cap.ffill(limit=cfg.cap_ffill_limit)
    vwap_anchor = vwap.where(anchor).ffill(limit=cfg.cap_ffill_limit)
    ratio = (vwap / vwap_anchor).where(lambda x: np.isfinite(x) & (x > 0))
    cap_est = (cap_ff * ratio).where(lambda x: x > 0)

    panel = {
        **fields,
        "dates": dates,
        "codes": codes,
        "ret": ret,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "st_proxy": st_proxy,
        "amount_ma20": amount_ma,
        "vol20": vol20,
        "universe_index": universe_index,
        "universe_index_np": universe_index.to_numpy(),
        "age_days": age_days,
        "cap": cap_est,
        "cap_cov": cap_est.notna(),
        "_base_dir": str(base_dir),
    }
    meta = {
        "valuation_source": valuation_source,
        "listing_source": listing_source,
        "delisted_panel": delisted_meta,
        "bars": int(len(daily)),
        "codes": len(codes),
        "dates": [str(dates[0].date()), str(dates[-1].date())],
    }
    return panel, meta


def trading_days(panel: dict, cfg: RotationConfig) -> list:
    dates = panel["dates"]
    start = pd.Timestamp(cfg.start)
    end = pd.Timestamp(cfg.end) if cfg.end else dates[-1]
    return [d for d in dates if start <= d <= end]


def rebalance_dates(panel: dict, cfg: RotationConfig) -> set:
    days = trading_days(panel, cfg)
    if cfg.cadence == "daily":
        return set(days)
    by_week: dict = {}
    for d in days:
        iso = d.isocalendar()
        by_week.setdefault((iso.year, iso.week), []).append(d)
    out = set()
    for _, week in by_week.items():
        tuesday = [x for x in week if x.weekday() == 1]
        if tuesday:
            out.add(tuesday[0])
        else:
            after = [x for x in week if x.weekday() > 1]
            out.add(after[0] if after else week[-1])
    return out


def in_magic_month(ts: pd.Timestamp) -> bool:
    md = (ts.month, ts.day)
    return ((1, 5) <= md <= (2, 5)) or ((4, 5) <= md <= (4, 30))


def _sell(gross: float, cfg: RotationConfig) -> float:
    net = gross * (1 - cfg.slippage)
    return net - max(net * cfg.commission, cfg.min_commission) - net * cfg.stamp_duty


def _buy_total(amount: float, cfg: RotationConfig) -> float:
    return amount + max(amount * cfg.commission, cfg.min_commission)


def _impact_rate(order_amount: float, adv: float, vol: float, cfg: RotationConfig) -> float:
    """平方根冲击律：participation=委托额/20日均额，impact = coef × 日波动 × sqrt(participation)。"""
    if cfg.impact_coef <= 0 or not np.isfinite(adv) or adv <= 0 or not np.isfinite(vol) or vol <= 0:
        return 0.0
    participation = min(max(order_amount / adv, 0.0), 1.0)
    return float(cfg.impact_coef * vol * math.sqrt(participation))


def selection_mask(panel: dict, cfg: RotationConfig, i: int) -> np.ndarray:
    """第 i 个交易日横截面上通过六层过滤的候选掩码（只用第 i 日及之前的信息）。"""
    a = _arrays(panel)
    cap, cov, close = a["cap"], a["cap_cov"], a["close"]
    listed, up, dn, st = a["age_days"], a["limit_up"], a["limit_down"], a["st_proxy"]
    valid = cov[i] & (cap[i] > 0) & (close[i] > 0) & (close[i] <= cfg.max_price)
    valid &= listed[i] >= cfg.min_listing_days
    valid &= ~up[i] & ~dn[i]
    if cfg.min_price > 0:
        valid &= close[i] >= cfg.min_price
    if cfg.min_amount > 0:
        valid &= np.nan_to_num(a["amount_ma20"][i], nan=0.0) >= cfg.min_amount
    if cfg.exclude_st:
        valid &= ~st[i]
    return valid


def ranked_targets(panel: dict, cfg: RotationConfig, i: int) -> list[str]:
    valid = selection_mask(panel, cfg, i)
    codes = panel["codes"]
    cap_row = _arrays(panel)["cap"][i]
    idx = np.where(valid)[0]
    if idx.size == 0:
        return []
    order = idx[np.argsort(cap_row[idx], kind="stable")]
    return [codes[j] for j in order[: min(cfg.top_n, cfg.candidate_pool)]]


def _arrays(panel: dict) -> dict:
    """把面板的 DataFrame 字段缓存成 numpy 数组（同一面板多次回测时避免重复转换）。"""
    cache = panel.get("_arrays")
    if cache is None:
        cache = {k: v.to_numpy() for k, v in panel.items() if isinstance(v, pd.DataFrame)}
        panel["_arrays"] = cache
    return cache


def load_intraday_bars(base_dir: Path, cfg: RotationConfig) -> dict:
    """把 5min 分时按 (日期, 代码) 装进字典：[(vwap, amount), ...]，用于盘内建仓模拟。"""
    if cfg.execution_bars <= 0:
        return {}
    root = base_dir / cfg.intraday_root / f"frequency={cfg.intraday_freq}min"
    files = sorted(str(p) for p in root.glob("*.parquet"))
    if not files:
        print(f"[xiaomkt_rotation] 未找到分时数据 {root}，回退到开盘价成交", flush=True)
        return {}
    frames = []
    for path in files:
        frames.append(pd.read_parquet(path, columns=["date", "time", "stock_code", "close", "volume", "amount"]))
    bars = pd.concat(frames, ignore_index=True)
    for col in ("close", "volume", "amount"):
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    bars["d"] = pd.to_datetime(bars["date"])
    bars = bars.sort_values(["stock_code", "d", "time"])
    out: dict = {}
    for (code, day), group in bars.groupby(["stock_code", "d"], sort=False):
        vwap = (group["amount"] / group["volume"].where(group["volume"] > 0)).to_numpy(dtype=float)
        out[(day, code)] = (np.nan_to_num(vwap, nan=np.nan), group["amount"].to_numpy(dtype=float))
    return out


def _intraday_fill(bars, target_amount: float, cfg: RotationConfig, *, max_bars: int):
    """在盘内前 max_bars 根 bar 里按每根参与率上限吃单，返回 (lots, vwap, bars_used, filled_amount)。"""
    vwap, amount = bars
    remaining = target_amount
    shares = 0.0
    value = 0.0
    used = 0
    for k in range(min(max_bars, len(vwap))):
        if not np.isfinite(vwap[k]) or vwap[k] <= 0:
            continue
        take = min(remaining, cfg.bar_participation_cap * float(amount[k]))
        if take <= 0:
            continue
        lots = int(take / (vwap[k] * 100))
        if lots <= 0:
            continue
        shares += lots * 100
        value += lots * 100 * vwap[k]
        remaining -= lots * 100 * vwap[k]
        used = k + 1
        if remaining <= vwap[k] * 100:
            break
    if shares <= 0:
        return 0, float("nan"), 0, 0.0
    return int(shares // 100), value / shares, used, value


def run_backtest(panel: dict, cfg: RotationConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    dates = panel["dates"]
    codes = panel["codes"]
    idx = {d: i for i, d in enumerate(dates)}
    code_i = {c: j for j, c in enumerate(codes)}
    arrays = _arrays(panel)
    open_np, low_np, close_np = arrays["open"], arrays["low"], arrays["close"]
    ret_np, up_np, dn_np = arrays["ret"], arrays["limit_up"], arrays["limit_down"]
    st_np = arrays["st_proxy"]
    adv_np = arrays["amount_ma20"]
    vol_np = arrays["vol20"]
    uni_index = panel["universe_index_np"]
    if cfg.ma_timing_window > 0:
        uni_series = pd.Series(uni_index, index=dates)
        uni_ma = uni_series.rolling(int(cfg.ma_timing_window), min_periods=max(5, int(cfg.ma_timing_window) // 4)).mean()
        ma_np = uni_ma.to_numpy()
    else:
        ma_np = None

    days = trading_days(panel, cfg)
    reb = rebalance_dates(panel, cfg)
    cash = cfg.initial_cash
    positions: dict[str, dict] = {}
    last_price: dict[str, float] = {}
    equity_rows: list = []
    trade_rows: list = []
    blocked = False
    intraday = load_intraday_bars(Path(panel.get("_base_dir", ".")), cfg) if cfg.execution_bars > 0 else {}
    intraday_used = 0

    for d in days:
        i = idx[d]
        if d in reb:
            blocked = False
            t = i - 1
            sel: list = []
            if t >= 0 and not (cfg.use_magic_months and in_magic_month(d)):
                sel = ranked_targets(panel, cfg, t)
            if cfg.use_market_filter and t >= 1:
                window = ret_np[max(0, i - max(cfg.market_filter_lookback, 1)): i]
                avg = np.nanmean(window, axis=1)
                if len(avg) >= 2 and np.nanprod(1 + avg) - 1 <= -cfg.market_filter:
                    blocked = True
            if ma_np is not None and t >= 0 and np.isfinite(ma_np[t]) and np.isfinite(uni_index[t]):
                blocked = blocked or bool(uni_index[t] < ma_np[t])       # 股票池跌破均线 -> 清仓空仓
            sel_set = set(sel)
            st_now = panel["st_proxy"].to_numpy()

            for c in list(positions):
                j = code_i[c]
                if c in sel_set and not blocked:
                    continue
                px = open_np[i, j]
                if not np.isfinite(px) or px <= 0:
                    continue
                reason = "market_filter" if blocked else "rebalance"
                if cfg.st_exit and bool(st_now[t, j]):
                    reason = "st_onset"            # 持仓中途被 ST/退市风险警示，无条件卖出
                if cfg.use_limitup_hold and bool(up_np[t, j]):
                    continue
                if px <= close_np[t, j] * (1 + LIMIT_DOWN):
                    continue
                gross = positions[c]["lots"] * 100 * px
                impact = _impact_rate(gross, adv_np[i, j], vol_np[i, j], cfg)
                if impact:
                    px = px * (1 - impact)
                    gross = positions[c]["lots"] * 100 * px
                cash += _sell(gross, cfg)
                trade_rows.append({"date": str(d.date()), "code": c, "side": "SELL", "price": round(px, 4),
                                   "lots": positions[c]["lots"], "reason": reason})
                positions.pop(c)

            if sel and not blocked:
                per = cash * (1 - cfg.cash_buffer) / len(sel)
                for c in [x for x in sel if x not in positions]:
                    j = code_i[c]
                    px = open_np[i, j]
                    if not np.isfinite(px) or px <= 0:
                        continue
                    if px >= close_np[t, j] * (1 + LIMIT_UP):
                        continue
                    if cfg.max_participation > 0:
                        adv = adv_np[i, j]
                        if np.isfinite(adv) and adv > 0:
                            per = min(per, cfg.max_participation * adv)   # 容量约束：不追超过 ADV 比例的仓位
                    bars = intraday.get((d, c)) if intraday else None
                    if bars is not None:
                        # 盘内建仓：按每根 5min 参与率上限吃单，成交价 = 实际加权价
                        lots, fill_px, bars_used, filled_value = _intraday_fill(
                            bars, per, cfg, max_bars=cfg.execution_bars
                        )
                        if lots <= 0:
                            continue
                        total = _buy_total(filled_value, cfg)
                        if total > cash:
                            continue
                        cash -= total
                        positions[c] = {"lots": lots, "entry": fill_px, "date": str(d.date())}
                        trade_rows.append({"date": str(d.date()), "code": c, "side": "BUY",
                                           "price": round(fill_px, 4), "lots": lots,
                                           "reason": f"intraday_{bars_used}bars"})
                        intraday_used += 1
                        continue
                    impact = _impact_rate(per, adv_np[i, j], vol_np[i, j], cfg)
                    px = px * (1 + impact)
                    lots = int(per // (px * (1 + cfg.slippage) * 100))
                    if lots <= 0:
                        continue
                    amount = px * lots * 100
                    total = _buy_total(amount, cfg)
                    if total > cash:
                        continue
                    cash -= total
                    positions[c] = {"lots": lots, "entry": px * (1 + cfg.slippage), "date": str(d.date())}
                    trade_rows.append({"date": str(d.date()), "code": c, "side": "BUY", "price": round(px, 4),
                                       "lots": lots, "reason": "rebalance"})

        for c in list(positions):
            j = code_i[c]
            lo, op, cl = low_np[i, j], open_np[i, j], close_np[i, j]
            entry = positions[c]["entry"]
            if np.isfinite(cl):
                last_price[c] = cl
                positions[c]["last_bar_idx"] = i
            elif i - positions[c].get("last_bar_idx", i) >= cfg.delist_exit_grace and cfg.include_delisted:
                # 退市/长期停牌：宽限期内没有 bar 即按最后收盘价折价清算
                px = last_price.get(c, entry) * (1 - cfg.delist_haircut)
                if np.isfinite(px) and px > 0:
                    gross = positions[c]["lots"] * 100 * px
                    cash += _sell(gross, cfg)
                    trade_rows.append({"date": str(d.date()), "code": c, "side": "SELL", "price": round(px, 4),
                                       "lots": positions[c]["lots"], "reason": "delisted"})
                    positions.pop(c)
                    last_price.pop(c, None)
                    continue
            if cfg.use_stop_loss and np.isfinite(lo) and lo <= entry * (1 - cfg.stop_loss):
                stop_px = entry * (1 - cfg.stop_loss)
                px = min(op, stop_px) if np.isfinite(op) else stop_px
                if np.isfinite(px) and px > 0:
                    gross = positions[c]["lots"] * 100 * px
                    impact = _impact_rate(gross, adv_np[i, j], vol_np[i, j], cfg)
                    if impact:
                        px = px * (1 - impact)
                        gross = positions[c]["lots"] * 100 * px
                    cash += _sell(gross, cfg)
                    trade_rows.append({"date": str(d.date()), "code": c, "side": "SELL", "price": round(px, 4),
                                       "lots": positions[c]["lots"], "reason": "stop_loss"})
                    positions.pop(c)

        equity = cash + sum(p["lots"] * 100 * last_price.get(c, p["entry"]) for c, p in positions.items())
        equity_rows.append({"date": d, "equity": equity, "cash": cash, "n_pos": len(positions)})

    return pd.DataFrame(equity_rows).set_index("date"), pd.DataFrame(trade_rows)


def summarize(equity: pd.DataFrame, trades: pd.DataFrame, cfg: RotationConfig) -> dict:
    value = equity["equity"].astype(float)
    years = (value.index[-1] - value.index[0]).days / 365.25
    daily = value.pct_change().dropna()
    vol = float(daily.std() * math.sqrt(244)) if len(daily) else float("nan")
    weekly = value.resample("W").last().pct_change().dropna()
    per_year = value.resample("YE").last().pct_change()
    if len(per_year):
        per_year.iloc[0] = value.resample("YE").last().iloc[0] / cfg.initial_cash - 1
    return {
        "start": str(value.index[0].date()),
        "end": str(value.index[-1].date()),
        "years": round(years, 2),
        "initial_cash": cfg.initial_cash,
        "final": float(value.iloc[-1]),
        "total_return": float(value.iloc[-1] / value.iloc[0] - 1),
        "cagr": float((value.iloc[-1] / value.iloc[0]) ** (1 / years) - 1) if years > 0 else float("nan"),
        "max_drawdown": float((value / value.cummax() - 1).min()),
        "annual_vol": vol,
        "sharpe": float(daily.mean() * 244 / vol) if vol and vol > 0 else float("nan"),
        "weekly_win_rate": float((weekly > 0).mean()) if len(weekly) else float("nan"),
        "n_trades": int(len(trades)),
        "per_year": {str(k.year): float(v) for k, v in per_year.items()},
    }


def latest_signal(panel: dict, cfg: RotationConfig) -> dict:
    """最近一个可决策日（T-1）的目标持仓，用于下一个调仓日的委托清单。"""
    dates = panel["dates"]
    i = max(len(dates) - 2, 0)
    targets = ranked_targets(panel, cfg, i)
    cap_row = panel["cap"].iloc[i]
    close_row = panel["close"].iloc[i]
    next_day = dates[min(i + 1, len(dates) - 1)]
    return {
        "as_of": str(dates[i].date()),
        "next_session": str(next_day.date()),
        "magic_month_block": bool(in_magic_month(next_day)),
        "targets": [
            {"code": c, "market_cap": float(cap_row.get(c, float("nan"))), "close": float(close_row.get(c, float("nan")))}
            for c in targets
        ],
    }


def run_xiaomkt_rotation_stage(layer: dict, pipeline: dict, base_dir: Path) -> dict:
    """流水线阶段入口：回测 + 输出最新一期目标持仓与已知偏差。"""
    cfg = RotationConfig(
        start=str(layer.get("start_date") or pipeline.get("start_date") or "2015-01-05"),
        end=str(layer.get("end_date") or pipeline.get("end_date") or ""),
        initial_cash=float(layer.get("initial_cash", 100_000.0)),
        top_n=int(layer.get("top_n", 5)),
        candidate_pool=int(layer.get("candidate_pool", 50)),
        cadence=str(layer.get("cadence", "weekly")),
        universe=str(layer.get("universe", "main")),
        max_price=float(layer.get("max_price", 80.0)),
        min_listing_days=int(layer.get("min_listing_days", 365)),
        exclude_st=bool(layer.get("exclude_st", True)),
        st_filter_mode=str(layer.get("st_filter_mode", "band")),
        st_fast_window=int(layer.get("st_fast_window", 20)),
        st_exit=bool(layer.get("st_exit", True)),
        min_price=float(layer.get("min_price", 0.0)),
        min_amount=float(layer.get("min_amount", 0.0)),
        ma_timing_window=int(layer.get("ma_timing_window", 0)),
        cash_buffer=float(layer.get("cash_buffer", 0.0)),
        impact_coef=float(layer.get("impact_coef", 0.0)),
        max_participation=float(layer.get("max_participation", 0.0)),
        use_limitup_hold=bool(layer.get("use_limitup_hold", True)),
        use_stop_loss=bool(layer.get("use_stop_loss", True)),
        stop_loss=float(layer.get("stop_loss", 0.07)),
        use_market_filter=bool(layer.get("use_market_filter", True)),
        market_filter=float(layer.get("market_filter", 0.05)),
        market_filter_lookback=int(layer.get("market_filter_lookback", 5)),
        use_magic_months=bool(layer.get("use_magic_months", True)),
        commission=float(layer.get("commission", 0.00025)),
        stamp_duty=float(layer.get("stamp_duty", 0.001)),
        slippage=float(layer.get("slippage", 0.0003)),
        min_commission=float(layer.get("min_commission", 5.0)),
        cap_ffill_limit=int(layer.get("cap_ffill_limit", 20)),
        include_delisted=bool(layer.get("include_delisted", True)),
        delisted_panel=str(layer.get("delisted_panel", "meta/cn_delisted_daily.parquet")),
        delist_haircut=float(layer.get("delist_haircut", 0.5)),
        delist_exit_grace=int(layer.get("delist_exit_grace", 20)),
    )
    output_dir = Path(layer.get("output_dir", "output/results_cn/xiaomkt_rotation"))
    if not output_dir.is_absolute():
        output_dir = Path(base_dir).resolve().parent.parent / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    panel, meta = build_panel(Path(base_dir), cfg)
    equity, trades = run_backtest(panel, cfg)
    if equity.empty:
        return {"status": "failed", "detail": "empty equity curve", **meta}
    stats = summarize(equity, trades, cfg)
    signal = latest_signal(panel, cfg)

    equity.to_parquet(output_dir / "equity.parquet")
    trades.to_csv(output_dir / "trades.csv", index=False)
    (output_dir / "metrics.json").write_text(
        json.dumps({"config": asdict(cfg), "metrics": stats, **meta}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    pd.DataFrame(signal["targets"]).to_csv(output_dir / "latest_signal.csv", index=False)

    print(f"[xiaomkt_rotation] {stats['start']} -> {stats['end']} | CAGR {stats['cagr']:.2%} | "
          f"MDD {stats['max_drawdown']:.2%} | final {stats['final']:,.0f} | trades {stats['n_trades']}", flush=True)
    print(f"[xiaomkt_rotation] 市值源 {meta['valuation_source']} | 上市日历 {meta['listing_source']} | "
          f"最新信号日 {signal['as_of']}", flush=True)
    for row in signal["targets"]:
        print(f"[xiaomkt_rotation]   {row['code']}  市值≈{row['market_cap']:.1f}亿  收盘 {row['close']:.2f}", flush=True)

    warnings = []
    if meta["delisted_panel"].get("included"):
        warnings.append(
            f"已并入退市股（{meta['delisted_panel']['codes']} 只 / {meta['delisted_panel']['rows']} 行）；"
            f"退市清算折价 {cfg.delist_haircut:.0%}，宽限 {cfg.delist_exit_grace} 个交易日"
        )
    else:
        warnings.append(f"价格面板未包含退市股（幸存者偏差，结果偏高）：{meta['delisted_panel']}")
    warnings += [
        f"股票池为代理（无 PIT 指数成分）：{cfg.universe}",
        "ST 用 5% 触板占比代理（偏松）",
        "市值 = 估值快照前向填充 × 价格比例还原",
    ]
    return {
        "status": "completed",
        "window": [stats["start"], stats["end"]],
        "universe": cfg.universe,
        "top_n": cfg.top_n,
        "cadence": cfg.cadence,
        "metrics": stats,
        "latest_signal": signal,
        "outputs": {
            "dir": str(output_dir),
            "metrics": str(output_dir / "metrics.json"),
            "trades": str(output_dir / "trades.csv"),
            "equity": str(output_dir / "equity.parquet"),
            "latest_signal": str(output_dir / "latest_signal.csv"),
        },
        "data_warnings": warnings,
        **meta,
    }
