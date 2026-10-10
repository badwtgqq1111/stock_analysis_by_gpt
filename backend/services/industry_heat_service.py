"""行业热度指数：分行业排名 + 行业内涨跌排名（每日/每周/每月）。

数据来源（全部本地，无网络依赖）：
  * 行业归属 `assets/data/meta/cn_stock_basic.parquet` 的 `industry`（110 个行业、覆盖 5,577 只；
    与选股链路/榜单页用的是同一份口径）。另有 `assets/data/raw/industry_snapshots/`
    的证监会分类快照可作交叉核对，本模块不用它以免两套口径混在一张榜里。
  * 行情 `backend/services/market_board_service.py` 的日线窗口（前复权）—— 涨跌幅、成交额、
    涨停/连板（含 PIT ST 标记）。
  * 资金流 `assets/data/derived/cn_moneyflow_features.parquet`（tushare 口径净流入额，万元）。

热度指数（口径写死在 `HEAT_WEIGHTS`，前端会把权重显示出来）：
    热度 = 100 × Σ 权重ᵢ × zᵢ   （zᵢ = 各分项在**行业之间**的横截面 z 分数）
  分项：区间涨跌幅(0.30) / 上涨家数占比(0.15) / 资金流强度=净流入÷成交额(0.25)
        / 涨停密度=涨停股票日÷成分股÷天数(0.20) / 放量程度=成交额÷上一同长度窗口(0.10)
  再按横截面**分位**映射到 0~100（`heat`），同时给出原始加权 z（`heat_z`）与
  相对上一个同长度窗口的变化（`heat_delta`，即"升温/降温"）。
  用分位而不是裸 z 当"指数"：单日极端值（某行业一只票 20% 涨停）不会把整张榜顶端拉爆。

「热度」不是预测：它只回答"这个行业最近在不在风口上"，与未来收益的关系必须单独验证
（`scripts/` 里目前没有行业层面的前瞻检验，前端会在说明里写明这一点）。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

from backend.services import market_board_service as mbs

ROOT = Path(__file__).resolve().parents[2]
MONEYFLOW_PANEL = ROOT / "assets/data/derived/cn_moneyflow_features.parquet"

PERIODS = {"daily": ("每日", 1), "weekly": ("每周（近 5 个交易日）", 5), "monthly": ("每月（近 20 个交易日）", 20)}
HEAT_WEIGHTS = {
    "ret": 0.30,          # 区间涨跌幅（行业中位）
    "up_ratio": 0.15,     # 区间上涨家数占比
    "flow_intensity": 0.25,   # 净流入 ÷ 区间成交额
    "zt_density": 0.20,   # 涨停股票日 ÷ 成分股 ÷ 天数
    "volume_boost": 0.10,     # 区间成交额 ÷ 上一同长度窗口
}
UNCLASSIFIED = "未分类"

_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple, tuple[float, object]] = {}


def _cached(key: tuple, ttl: float, builder):
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = builder()
    with _CACHE_LOCK:
        _CACHE[key] = (now, value)
    return value


def cache_clear() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _num(value, nd: int = 2):
    """NaN/inf → None，其余四舍五入；保证 JSON 里不出现 NaN（浏览器会解析失败）。"""
    try:
        if value is None or pd.isna(value):
            return None
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return round(value, nd)


# ─────────────────────────────────────────────────────────────────────────────
# 数据装配
# ─────────────────────────────────────────────────────────────────────────────
def _bars(days: int = 90) -> pd.DataFrame:
    """日线窗口 + 涨停/连板/ST 标记（复用 market_board_service，同一个 TTL 缓存）。"""
    return mbs.enriched_daily(days)


def _moneyflow(start: str, end: str, source: str = "moneyflow") -> pd.DataFrame:
    """区间资金流（万元）。按 (trade_date, stock_code) 取一行——面板一行一个 source。"""
    source = source if source in mbs.MONEYFLOW_SOURCES else "moneyflow"

    def build():
        import pyarrow.dataset as ds
        dataset = ds.dataset(str(MONEYFLOW_PANEL), format="parquet")
        table = dataset.to_table(
            columns=["trade_date", "stock_code", f"{source}_net_amount"],
            filter=(ds.field("trade_date") >= pd.Timestamp(start))
                   & (ds.field("trade_date") <= pd.Timestamp(end))
                   & (ds.field("source") == source),
        )
        frame = table.to_pandas()
        if frame.empty:
            return frame
        frame["trade_date"] = pd.to_datetime(frame["trade_date"])
        return frame.rename(columns={f"{source}_net_amount": "net_amount"})

    return _cached(("mf", start, end, source), 300.0, build)


def _industry_map() -> pd.Series:
    """ts_code → 行业名（缺失归"未分类"）。"""
    basics = mbs.basic_frame()
    if basics.empty:
        return pd.Series(dtype=object)
    industry = basics["industry"].astype(str).replace({"nan": "", "None": ""}).fillna("")
    industry = industry.where(industry != "", UNCLASSIFIED)
    return industry


def build_frame(period: str = "daily", days: int = 90, source: str = "moneyflow") -> dict:
    """把"个股 → 行业 → 行业榜"的整条链路算出来（页面与 drill-down 共用一份结果）。"""
    if period not in PERIODS:
        period = "daily"
    span = PERIODS[period][1]

    bars = _bars(days) if span <= 20 else _bars(days + 20)
    if bars.empty:
        return {"available": False, "period": period, "rows": [], "hint": "本地日线为空"}
    dates = sorted(bars["trade_date"].unique())
    if len(dates) < span + 1:
        return {"available": False, "period": period, "rows": [],
                "hint": f"本地日线只有 {len(dates)} 个交易日，算不出 {PERIODS[period][0]} 区间"}
    last, base = pd.Timestamp(dates[-1]), pd.Timestamp(dates[-1 - span])
    prev_last, prev_base = (pd.Timestamp(dates[-1 - span]), pd.Timestamp(dates[-1 - 2 * span])
                            if len(dates) >= 2 * span + 1 else (None, None))

    industry = _industry_map()
    window = bars[bars["trade_date"].isin([last, base])]
    pivot = window.pivot_table(index="stock_code", columns="trade_date", values="close", aggfunc="last")
    if last not in pivot.columns or base not in pivot.columns:
        return {"available": False, "period": period, "rows": [], "hint": "窗口内缺少收盘价"}

    today = bars[bars["trade_date"] == last].set_index("stock_code")
    period_ret = (pivot[last] / pivot[base] - 1.0).dropna()
    # 金额/涨停/成交额：只统计区间内
    sub = bars[bars["trade_date"].isin(dates[-span:])]
    amount_sum = sub.groupby("stock_code")["amount"].sum()
    zt_days = sub.groupby("stock_code")["is_limit_up"].sum()

    stocks = pd.DataFrame({
        "stock_code": period_ret.index,
        "period_ret": period_ret.to_numpy(),
    })
    stocks["industry"] = stocks["stock_code"].map(industry).fillna(UNCLASSIFIED)
    stocks["name"] = stocks["stock_code"].map(today["name"]) if "name" in today else ""
    stocks["market"] = stocks["stock_code"].map(today["market"]) if "market" in today else ""
    stocks["close"] = stocks["stock_code"].map(today["close"])
    stocks["pct_chg"] = stocks["stock_code"].map(today["pct_chg"])
    stocks["boards"] = stocks["stock_code"].map(today["boards"]).fillna(0)
    stocks["is_limit_up"] = stocks["stock_code"].map(today["is_limit_up"]).fillna(False)
    stocks["st_pit"] = stocks["stock_code"].map(today["st_pit"]).fillna(False) if "st_pit" in today else False
    stocks["amount"] = stocks["stock_code"].map(amount_sum)
    stocks["zt_days"] = stocks["stock_code"].map(zt_days).fillna(0)

    flow = _moneyflow(str(pd.Timestamp(dates[-span]).date()), str(last.date()), source)
    if not flow.empty:
        net = flow.groupby("stock_code")["net_amount"].sum()
        stocks["net_amount"] = stocks["stock_code"].map(net)          # 万元
    else:
        stocks["net_amount"] = np.nan

    # 放量程度：本区间成交额 ÷ 上一个同长度区间
    volume_boost = None
    if prev_base is not None:
        prev_sub = bars[bars["trade_date"].isin(dates[-2 * span:-span])]
        if not prev_sub.empty:
            prev_amount = prev_sub.groupby("stock_code")["amount"].sum()
            stocks["amount_prev"] = stocks["stock_code"].map(prev_amount)
            volume_boost = True

    ranked = stocks[stocks["industry"] != UNCLASSIFIED].copy()
    if ranked.empty:
        return {"available": False, "period": period, "rows": [], "hint": "行业映射为空"}

    grouped = ranked.groupby("industry")
    table = pd.DataFrame({
        "n": grouped.size(),
        "ret": grouped["period_ret"].median(),
        "ret_mean": grouped["period_ret"].mean(),
        "up_ratio": grouped["period_ret"].apply(lambda s: float((s > 0).mean())),
        "amount": grouped["amount"].sum(),
        "net_amount": grouped["net_amount"].sum(),
        "zt_days": grouped["zt_days"].sum(),
        "boards_ge2": grouped.apply(lambda g: int((g["boards"] >= 2).sum()), include_groups=False),
        "leaders": grouped.apply(
            lambda g: "; ".join(g.nlargest(3, "period_ret")["name"].astype(str).tolist()),
            include_groups=False),
    })
    if volume_boost:
        prev_amount = grouped["amount_prev"].sum()
        table["volume_boost"] = (table["amount"] / prev_amount.replace(0, np.nan)).fillna(1.0)
    else:
        table["volume_boost"] = 1.0

    # 单位必须先统一：面板净流入是**万元**、成交额是**元**，直接相除会得到 1e-4 量级
    # （实测被四舍五入成 0.000，整条"资金流强度"分项静默失效）。
    table["flow_intensity"] = (table["net_amount"] * 1e4) / table["amount"].replace(0, np.nan)
    table["zt_density"] = table["zt_days"] / table["n"] / span
    table = table.fillna({"flow_intensity": 0.0, "zt_density": 0.0, "ret": 0.0, "up_ratio": 0.0})

    # 热度：分项横截面 z → 加权 → 横截面分位（0~100）
    z = pd.DataFrame(index=table.index)
    for key in HEAT_WEIGHTS:
        column = table[key].astype(float)
        std = column.std(ddof=0)
        z[key] = (column - column.mean()) / std if std and np.isfinite(std) and std > 0 else 0.0
    table["heat_z"] = sum(z[key] * weight for key, weight in HEAT_WEIGHTS.items())
    table["heat"] = (table["heat_z"].rank(pct=True) * 100).round(1)

    # 上一个同长度窗口的热度（用于"升温/降温"）
    delta = None
    if prev_base is not None and prev_last is not None and len(dates) >= 2 * span + 1:
        delta = _heat_at(bars, dates, span * 2, span, industry, source)
    if delta is not None:
        table["heat_prev"] = delta.reindex(table.index)
        table["heat_delta"] = (table["heat"] - table["heat_prev"]).round(1)
    else:
        table["heat_prev"] = np.nan
        table["heat_delta"] = np.nan

    table = table.reset_index().rename(columns={"index": "industry"})
    table = table.sort_values("heat", ascending=False).reset_index(drop=True)
    table["rank"] = table.index + 1

    market = {
        "date": last.strftime("%Y-%m-%d"),
        "base_date": base.strftime("%Y-%m-%d"),
        "span": span,
        "ret": round(float(ranked["period_ret"].median()), 5),
        "up_ratio": round(float((ranked["period_ret"] > 0).mean()), 4),
        "n_stocks": int(len(ranked)),
        "n_industries": int(table.shape[0]),
        "n_unclassified": int((stocks["industry"] == UNCLASSIFIED).sum()),
        "net_amount_wan": float(ranked["net_amount"].sum(skipna=True)),
        "zt_days": int(ranked["zt_days"].sum()),
        "amount": float(ranked["amount"].sum()),
        "source": source,
    }
    return {"available": True, "period": period, "indices": table, "stocks": stocks, "market": market}


def _heat_at(bars: pd.DataFrame, dates: list, offset: int, span: int,
             industry: pd.Series, source: str) -> pd.Series | None:
    """历史某个同长度窗口的行业热度（只算热度的分位，用于计算"热度变化"）。"""
    if len(dates) < offset + 1:
        return None
    last, base = pd.Timestamp(dates[-1 - offset]), pd.Timestamp(dates[-1 - offset - span])
    window = bars[bars["trade_date"].isin([last, base])]
    pivot = window.pivot_table(index="stock_code", columns="trade_date", values="close", aggfunc="last")
    if last not in pivot.columns or base not in pivot.columns:
        return None
    ret = (pivot[last] / pivot[base] - 1.0).dropna()
    sub = bars[bars["trade_date"].isin(dates[len(dates) - offset - span:len(dates) - offset])]
    frame = pd.DataFrame({"stock_code": ret.index, "ret": ret.to_numpy()})
    frame["industry"] = frame["stock_code"].map(industry).fillna(UNCLASSIFIED)
    frame = frame[frame["industry"] != UNCLASSIFIED]
    if frame.empty:
        return None
    grouped = frame.groupby("industry")
    amount = sub.groupby("stock_code")["amount"].sum()
    zt = sub.groupby("stock_code")["is_limit_up"].sum()
    table = pd.DataFrame({
        "ret": grouped["ret"].median(),
        "up_ratio": grouped["ret"].apply(lambda s: float((s > 0).mean())),
        "amount": grouped["stock_code"].apply(lambda s: float(amount.reindex(s).sum())),
        "zt_density": grouped["stock_code"].apply(lambda s: float(zt.reindex(s).sum())) / grouped.size() / span,
    })
    table["flow_intensity"] = 0.0
    table["volume_boost"] = 1.0
    z = pd.DataFrame(index=table.index)
    for key in HEAT_WEIGHTS:
        column = table[key].astype(float)
        std = column.std(ddof=0)
        z[key] = (column - column.mean()) / std if std and np.isfinite(std) and std > 0 else 0.0
    composite = sum(z[key] * weight for key, weight in HEAT_WEIGHTS.items())
    return (composite.rank(pct=True) * 100).round(1)


def industry_heat(period: str = "daily", limit: int = 60, source: str = "moneyflow",
                  min_n: int = 0) -> dict:
    """行业热度榜（对外 API 用）。`min_n` 用来滤掉成分股过少的小行业（4~7 只的行业
    中位数噪声很大，热度榜常被它们占满榜首）。"""
    built = build_frame(period, source=source)
    if not built.get("available"):
        return built
    table = built["indices"]
    if min_n:
        table = table[table["n"] >= int(min_n)]
        if table.empty:
            return {"available": False, "period": period, "rows": [],
                    "hint": f"没有成分数 ≥ {min_n} 的行业"}
    rows = []
    for _, r in table.head(int(limit)).iterrows():
        rows.append({
            "rank": int(r["rank"]),
            "industry": str(r["industry"]),
            "heat": float(r["heat"]),
            "heat_z": round(float(r["heat_z"]), 3),
            "heat_prev": None if pd.isna(r["heat_prev"]) else float(r["heat_prev"]),
            "heat_delta": None if pd.isna(r["heat_delta"]) else float(r["heat_delta"]),
            "ret": round(float(r["ret"]), 5),
            "ret_mean": round(float(r["ret_mean"]), 5),
            "up_ratio": round(float(r["up_ratio"]), 4),
            "n": int(r["n"]),
            "net_amount_wan": round(float(r["net_amount"]), 1),
            "net_amount_yi": mbs.yi(float(r["net_amount"])),
            "flow_intensity": round(float(r["flow_intensity"]), 5),
            "zt_days": int(r["zt_days"]),
            "zt_density": round(float(r["zt_density"]), 5),
            "boards_ge2": int(r["boards_ge2"]),
            "amount": float(r["amount"]),
            "volume_boost": round(float(r["volume_boost"]), 4),
            "leaders": str(r["leaders"]),
        })
    return {
        "available": True,
        "period": built["period"],
        "period_label": PERIODS[built["period"]][0],
        "weights": [{"key": k, "weight": w, "label": LABELS[k]} for k, w in HEAT_WEIGHTS.items()],
        "market": built["market"],
        "rows": rows,
        "n_total": int(len(table)),
    }


LABELS = {
    "ret": "区间涨跌幅",
    "up_ratio": "上涨家数占比",
    "flow_intensity": "净流入÷成交额",
    "zt_density": "涨停密度",
    "volume_boost": "放量程度",
}


def industry_stocks(industry: str, period: str = "daily", direction: str = "up",
                    limit: int = 50, source: str = "moneyflow") -> dict:
    """行业内个股涨跌排名（drill-down）。"""
    built = build_frame(period, source=source)
    if not built.get("available"):
        return built
    stocks = built["stocks"]
    sub = stocks[stocks["industry"] == industry].copy()
    if sub.empty:
        return {"available": False, "industry": industry, "rows": [],
                "hint": f"没有行业「{industry}」（或该行业全部为未分类）"}
    sub = sub.dropna(subset=["period_ret"])
    sub = sub.sort_values("period_ret", ascending=(direction == "down"))

    # 行业龙头 / 次龙头：按**流通市值**在行业内排名（用户看热力图时最直觉的口径：
    # "这个行业里谁最大"）。只作为**标签**展示，不改变排序（排序仍是区间涨跌幅）。
    # 注意：把选股口径改成"买龙头"在本仓库 12 组网格回放里全部更差
    # （见 scripts/select_concept_arm.py 的模块 docstring），所以这里只标、不推荐。
    mktcap, _stamp = _mktcap_lookup()
    cap_rank: dict[str, int] = {}
    # 踩坑：第一版写 `caps.sort_values(...).index` —— 那是 DataFrame 的**行标签**（0,1,2…），
    # 不是股票代码，于是 cap_rank 的键全成了 "0"/"1"/"2"，get(stock_code) 永远 None，
    # 页面上一个"龙一"标签都不会出现。必须按**列值**遍历。
    caps = (sub[["stock_code"]]
            .assign(mktcap=sub["stock_code"].map(mktcap))
            .dropna(subset=["mktcap"])
            .sort_values("mktcap", ascending=False))
    for order, code in enumerate(caps["stock_code"], 1):
        cap_rank[str(code)] = order

    rows = []
    for i, (_, r) in enumerate(sub.head(int(limit)).iterrows(), 1):
        code = str(r["stock_code"])
        rows.append({
            "rank": i,
            "stock_code": code,
            "name": str(r["name"] or ""),
            "market": str(r["market"] or ""),
            "mktcap_yi": _num(mktcap.get(code), 2),
            "leader_rank": cap_rank.get(code),      # 1=龙一 2=龙二，None=未进前二
            "period_ret": round(float(r["period_ret"]), 5),
            "pct_chg": None if pd.isna(r["pct_chg"]) else round(float(r["pct_chg"]), 5),
            "close": None if pd.isna(r["close"]) else round(float(r["close"]), 3),
            "amount": None if pd.isna(r["amount"]) else float(r["amount"]),
            "net_amount_wan": None if pd.isna(r.get("net_amount")) else float(r["net_amount"]),
            "net_amount_yi": None if pd.isna(r.get("net_amount")) else mbs.yi(float(r["net_amount"])),
            "boards": int(r["boards"] or 0),
            "is_limit_up": bool(r["is_limit_up"]),
            "is_st": bool(r["st_pit"]),
        })
    stats = {
        "n": int(len(sub)),
        "ret": round(float(sub["period_ret"].median()), 5),
        "up_ratio": round(float((sub["period_ret"] > 0).mean()), 4),
        "net_amount_yi": mbs.yi(float(sub["net_amount"].sum(skipna=True))),
    }
    return {"available": True, "industry": industry, "period": built["period"],
            "period_label": PERIODS[built["period"]][0], "stats": stats,
            "market": built["market"], "rows": rows, "n_total": int(len(sub))}

# ─────────────────────────────────────────────────────────────────────────────
# 热力图（a-share-heatmap 风格：面积=市值、颜色=涨跌幅、可下钻到个股）
# ─────────────────────────────────────────────────────────────────────────────
def _mktcap_lookup() -> tuple[dict, str | None]:
    """代码 → 流通市值（亿元）。

    市值用 Tushare relay 的 **daily_basic 月频快照**（实测最新 2026-09-30，5,572 只）：
    市值本身变化慢，日频快照没必要；涨跌用的仍是当日/区间收益。
    v4 面板的 circ_mktcap 作为兜底（只有 domain 内 3,000 多只）。
    """
    def build():
        folder = ROOT / "assets/data/raw/daily_basic_relay"
        files = sorted(folder.glob("month=*.parquet")) if folder.exists() else []
        result: dict[str, float] = {}
        stamp = None
        if files:
            frame = pd.read_parquet(files[-1], columns=["ts_code", "trade_date", "circ_mv", "total_mv"])
            frame["trade_date"] = pd.to_datetime(frame["trade_date"])
            latest = frame[frame["trade_date"] == frame["trade_date"].max()]
            column = "circ_mv" if latest["circ_mv"].notna().any() else "total_mv"
            for code, value in zip(latest["ts_code"].astype(str), latest[column]):
                if pd.notna(value):
                    result[code] = float(value) / 1e4          # 万元 → 亿元
            stamp = str(latest["trade_date"].max().date())
        if len(result) < 1000:                                 # 兜底：v4 面板（domain 内）
            panel = ROOT / "assets/data/meta/cn_sleeve_features_long_v4.parquet"
            if panel.exists():
                import pyarrow.dataset as ds
                dataset = ds.dataset(str(panel), format="parquet")
                table = dataset.to_table(columns=["trade_date", "stock_code", "circ_mktcap"])
                frame = table.to_pandas()
                frame["trade_date"] = pd.to_datetime(frame["trade_date"])
                latest = frame[frame["trade_date"] == frame["trade_date"].max()]
                for code, value in zip(latest["stock_code"].astype(str), latest["circ_mktcap"]):
                    if pd.notna(value):
                        result.setdefault(code, float(value) / 1e8)
                stamp = stamp or str(latest["trade_date"].max().date())
        return result, stamp

    return _cached(("mktcap",), 1800.0, build)


def heatmap(period: str = "daily", market: str = "", limit_per_industry: int = 400) -> dict:
    """行业 → 成分股 的两级热力图数据（面积=流通市值，颜色=区间涨跌幅）。"""
    built = build_frame(period)
    if not built.get("available"):
        return built
    stocks = built["stocks"].copy()
    if market:
        stocks = stocks[stocks["market"] == market]
    mktcap, mktcap_date = _mktcap_lookup()
    stocks["mktcap_yi"] = stocks["stock_code"].map(mktcap)
    missing = int(stocks["mktcap_yi"].isna().sum())
    median_cap = float(stocks["mktcap_yi"].median() or 20.0)
    stocks["mktcap_yi"] = stocks["mktcap_yi"].fillna(median_cap)     # 缺市值用中位数兜底，避免整块消失

    industries = []
    for name, group in stocks.groupby("industry"):
        group = group.sort_values("mktcap_yi", ascending=False)
        rows = []
        for _, r in group.head(int(limit_per_industry)).iterrows():
            rows.append({
                "stock_code": str(r["stock_code"]), "name": str(r["name"] or ""),
                "ret": _num(r["period_ret"], 5), "pct_chg": _num(r["pct_chg"], 5),
                "close": _num(r["close"], 3), "mktcap_yi": _num(r["mktcap_yi"], 2),
                "amount": _num(r["amount"], 0), "boards": int(r["boards"] or 0),
                "is_limit_up": bool(r["is_limit_up"]),
            })
        industries.append({
            "industry": str(name), "n": int(len(group)),
            "ret": _num(group["period_ret"].median(), 5),
            "mktcap_yi": _num(group["mktcap_yi"].sum(), 2),
            "amount": _num(group["amount"].sum(), 0),
            "up_ratio": _num(float((group["period_ret"] > 0).mean()), 4),
            "stocks": rows,
        })
    industries.sort(key=lambda item: -(item["mktcap_yi"] or 0))
    return {
        "available": True, "period": period,
        "period_label": PERIODS[period][0],
        "date": built["market"]["date"], "base_date": built["market"]["base_date"],
        "span": built["market"]["span"], "market_filter": market,
        "mktcap_date": mktcap_date, "mktcap_missing": missing,
        "industries": industries,
        "totals": {
            "n_stocks": int(len(stocks)), "n_industries": int(len(industries)),
            "mktcap_yi": _num(stocks["mktcap_yi"].sum(), 2),
            "up": int((stocks["period_ret"] > 0).sum()), "down": int((stocks["period_ret"] < 0).sum()),
            "flat": int((stocks["period_ret"] == 0).sum()),
        },
        "notes": [
            "面积 = 流通市值（Tushare daily_basic 月频快照），颜色 = 区间涨跌幅（A 股口径：红涨绿跌）。",
            "点行业格子下钻看成分股；点个股直接进它的 K 线页。",
        ],
    }
