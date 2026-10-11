"""全市场榜单（涨跌 / 资金流）与连板梯队的数据层。

给两个页面共用：`/api/rankings/*`（每日/周 涨跌与资金流排行）与
`/api/limitup/*`（一板/两板/三板/四板 连板记录统计）。

数据来源（**本地优先**，网络只做增强，断网也要能出数）：

  * 日线（前复权）`assets/data/clean/ohlcv/market=CN/exchange=*/.../frequency=daily/adjust=qfq/`
    → 涨跌幅、涨停/连板判定。2026 年两份分区合计约 96 万行，列裁剪 + trade_date 谓词下推，
      读一次 ~0.5s，用 TTL 缓存挡住重复扫描。
  * 资金流面板 `assets/data/derived/cn_moneyflow_features.parquet`
    → 三个口径的净流入额（tushare `moneyflow` / 东财 `moneyflow_dc` / 同花顺 `moneyflow_ths`），
      并且面板里**已经算好 5/10/20 日累计**（`*_net_5d` 等），所以"周资金流排行"不需要再自己求和。
  * 基础档案 `assets/data/meta/cn_stock_basic.parquet` → 名称 / 行业 / 板块 / 上市日。
  * 分时 `assets/data/raw/intraday_baostock/frequency=5min/<code>.parquet`（收盘后由
    `scripts/sync_cn_intraday_full.py --refresh` 增量补齐；盘中要看实时得走 live）。

口径与坑（都实测过，不要再踩）：

  1. **资金流面板一行一个 (日期, 股票, source)** —— 同一只票每天有 3 行（三个来源各一）。
     不做 `source` 过滤就会把同一只票重复算 3 次、榜单直接错乱。
  2. 资金流金额单位是**万元**（tushare/东财/同花顺三家都是），对外统一折算成**亿元**。
  3. 涨停幅度按板块判定：主板 10%、创业板/科创板 20%、北交所 30%、ST 5%。
     仓库 `factor_engine/portfolio/execution_assumptions.price_limit_ratio` 只覆盖前两档
     （它服务的是"能不能成交"，不认北交所），这里必须补全，否则北交所会因为 30% 的票
     永远算不上涨停（实测 2026-10-09 涨停池里就有北交所票）。
  4. 日线是**前复权**：分红除权日的"涨跌幅"与交易所口径（不复权前收）会有零点几到一点几个
     百分点的差。这与仓库其它模块（回放/模拟盘）用的是同一套近似，前端会写明"复权口径"。
  5. 判定涨停用 `涨跌幅 >= 上限 - tolerance`（默认 0.5%），与
     `scripts/replay_daily_execution.py` 同一套容差，避免 9.98% 的板被漏掉。
"""
from __future__ import annotations

import threading
import time
from datetime import date as _date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DAILY_DIR = ROOT / "assets/data/clean/ohlcv/market=CN"
MONEYFLOW_PANEL = ROOT / "assets/data/derived/cn_moneyflow_features.parquet"
BASIC_PATH = ROOT / "assets/data/meta/cn_stock_basic.parquet"
INTRADAY_5MIN_DIR = ROOT / "assets/data/raw/intraday_baostock/frequency=5min"

# 涨停容差的下限/上限。
#
# 为什么要"按价格浮动"的容差（2026-10-09 实测标定）：
#   交易所的涨停价是 `round(前收 × (1+上限), 2)`，四舍五入到分。所以真实涨幅
#   会**系统性偏离**名义上限 —— 越便宜的票偏得越多：3.13 元的安泰集团涨停价
#   3.44（3.13×1.1=3.443，进位），实际涨幅只有 9.9042%；用固定 0.5% 容差会把
#   一批「9.5%~9.9% 但确实封在涨停价上」的票算漏，也会把「9.6% 但离涨停还差
#   一分钱」的票算多。用 `半跳(=0.005) ÷ 前收` 近似这个舍入误差：
#     3 元票 → 0.17%，50 元票 → 0.01%。
#   实测（2026-10-08 与东财涨停池 43 只逐只对账）：本地判定 44 只，
#   命中 42、多报 2（均为按 10% 涨停价封板、但东财池不收录 ST 的票）、
#   漏报 1（920627.BJ 力王股份，本地日线里根本没有这只票）。
LIMIT_TOL_MIN = 0.0002
LIMIT_TOL_MAX = 0.004
_TICK = 0.005

# 资金流三口径：前端下拉显示名 ↔ 面板列前缀
MONEYFLOW_SOURCES = {
    "moneyflow": "tushare 净流入额",
    "moneyflow_dc": "东财主力净额",
    "moneyflow_ths": "同花顺净额",
}

_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple, tuple[float, object]] = {}


def _cached(key: tuple, ttl: float, builder):
    """极简 TTL 缓存。

    不直接 `lru_cache` 的原因：数据文件每天都会被流水线重写，
    进程级永久缓存会一直吐旧数据（本项目已经因为"缓存住旧 index.html"白屏过一次，
    这里不重蹈覆辙）。ttl 到期即重算，文件更新最多滞后 ttl 秒。
    """
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
    """给测试/排障用：清掉所有 TTL 缓存。"""
    with _CACHE_LOCK:
        _CACHE.clear()


# ─────────────────────────────────────────────────────────────────────────────
# 日线
# ─────────────────────────────────────────────────────────────────────────────
def _daily_files(years: list[int]) -> list[str]:
    files: list[str] = []
    for year in years:
        files += [str(p) for p in DAILY_DIR.glob(
            f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={year}/*.parquet")]
    return sorted(files)


def _daily_dataset(years: list[int]):
    import pyarrow.dataset as ds
    files = _daily_files(years)
    if not files:
        return None, []
    return ds.dataset(files, format="parquet"), files


def trade_dates(days: int = 400) -> list[pd.Timestamp]:
    """最近的交易日列表（升序）。只读 trade_date 一列，~0.3s。"""
    def build():
        import pyarrow.dataset as ds
        years = [_date.today().year, _date.today().year - 1]
        dataset, files = _daily_dataset(years)
        if dataset is None:
            return []
        table = dataset.to_table(columns=["trade_date"])
        stamps = pd.to_datetime(table.column("trade_date").to_pandas()).dt.normalize()
        return sorted(pd.Index(stamps).unique())

    dates = _cached(("trade_dates", days), 300.0, build)
    return dates[-int(days):]


def latest_trade_date() -> pd.Timestamp | None:
    dates = trade_dates()
    return dates[-1] if dates else None


def load_daily(days: int = 90) -> pd.DataFrame:
    """最近 `days` 个交易日的全市场日线（前复权）。

    返回列：trade_date / stock_code / open / high / low / close / volume / amount。
    `prev_close` 与 `pct_chg` 由 `with_limit_flags` 在**同一窗口内**用 shift 得到，
    所以窗口第一天没有涨跌幅（这是有意的：宁可少一天，也不要跨窗口错算）。
    """
    def build():
        import pyarrow.dataset as ds
        # 要多少天就取多少天：trade_dates() 默认只返回最近 400 个交易日，
        # 直接 dates[-700] 会 IndexError（实测：概念手臂做 24 个月回放时炸在这里）。
        dates = trade_dates(max(int(days) + 5, 400))
        if not dates:
            return pd.DataFrame()
        cutoff = dates[max(-int(days), -len(dates))]
        years = sorted({cutoff.year, dates[-1].year})
        dataset, files = _daily_dataset(years)
        if dataset is None:
            return pd.DataFrame()
        table = dataset.to_table(
            columns=["trade_date", "stock_code", "open", "high", "low", "close", "volume", "amount"],
            filter=ds.field("trade_date") >= cutoff,
        )
        frame = table.to_pandas()
        frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.normalize()
        # 去重（2026-10-10 实测）：日线分区里存在**同一 (trade_date, stock_code) 重复行**
        # （最近 120 天里有 3,870 行，value 完全相同 —— 应该是同步时重复写入）。
        # 下游大量 `set_index("stock_code")` 后再 `.map()/.reindex()`，pandas 3 遇到非唯一索引会**直接抛**
        # （`InvalidIndexError: Reindexing only valid with uniquely valued Index objects`），
        # 表现为行业热度 / 概念手臂的一批回归测试同时变红 —— 根因就在这里，不在各自的业务代码。
        frame = frame.drop_duplicates(subset=["trade_date", "stock_code"], keep="last")
        return frame.sort_values(["stock_code", "trade_date"], kind="stable").reset_index(drop=True)

    return _cached(("daily", int(days)), 300.0, build)


# ─────────────────────────────────────────────────────────────────────────────
# 基础档案
# ─────────────────────────────────────────────────────────────────────────────
def basic_frame() -> pd.DataFrame:
    """ts_code → name / industry / market（板块）/ list_date / list_status。"""
    def build():
        if not BASIC_PATH.exists():
            return pd.DataFrame(columns=["name", "industry", "market", "list_date", "list_status"])
        frame = pd.read_parquet(BASIC_PATH, columns=["ts_code", "name", "industry", "market",
                                                     "list_date", "list_status"])
        frame["ts_code"] = frame["ts_code"].astype(str)
        return frame.drop_duplicates("ts_code").set_index("ts_code")

    return _cached(("basic",), 600.0, build)


def _name_lookup() -> pd.DataFrame:
    basics = basic_frame()
    if basics.empty:
        return pd.DataFrame(columns=["name", "industry", "market", "list_date", "list_status"])
    return basics


# ─────────────────────────────────────────────────────────────────────────────
# 涨停 / 连板
# ─────────────────────────────────────────────────────────────────────────────
def is_st_name(name: str) -> bool:
    upper = str(name).upper()
    return "ST" in upper or "退" in str(name)


def limit_ratio(code: str, name: str = "", market: str = "") -> float:
    """涨停幅度上限（小数）：北交所 30%、创业板/科创板 20%、其余主板 10%。

    **这里故意不给 ST 单独设 5%**（真实市场规则里主板 ST 是 5%），原因是本仓库
    的数据实测对不上，硬套 5% 会让连板统计直接错：
      * 2026-10-08 主板 ST 有 142 只，涨幅落在 4.9%~5.05% 的只有 1 只，
        而**超过 5% 的有 12 只**（*ST网达 +10.03%、*ST华西 +9.79%、ST信通 +9.77%…）；
        若真有 5% 限制，分布应该是"一堆卡在 5%"而不是"一批冲到 10%"。
      * 上面这几只的涨跌幅与东财当日行情（`moneyflow_dc` 快照的 `pct_change`）
        完全一致，不是复权算错。
      * 用 10% 上限逐只对账 2026-10-08：本地判定与东财涨停池 43 只中的 42 只重合，
        多出来的 2 只是按 10% 涨停价封板的 ST 票（东财池不收录 ST）。
    结论：本数据集里用**板块上限**（10/20/30）判定才是与市场一致的算法。
    ST 仍然会作为 `st_pit` 标记返回给前端展示。
    北交所新代码段（920xxx / 43xxxx / 83xxxx / 87xxxx / 88xxxx）必须显式判，否则 30% 的票永远算不上涨停。
    """
    code = str(code)
    if market == "北交所" or code.endswith(".BJ"):
        return 0.30
    if code.startswith(("300", "301", "688", "689")) or market in ("创业板", "科创板"):
        return 0.20
    return 0.10


def _st_matrix(dates: list, codes: list) -> pd.DataFrame:
    """(交易日 × 代码) 的 PIT「当时是否 ST」布尔矩阵。

    用 `factor_engine.universe_pit.risk_asof`（仓库既有的 PIT 名称区间表）——
    不能用当前名称反推历史，否则会出现"今天戴帽、历史全按 5% 算"的前视错误
    （项目里 `scripts/st_pit_audit.py` 专门审计过这件事）。
    """
    stamp = (tuple(str(d) for d in dates), len(codes),
             codes[0] if codes else "", codes[-1] if codes else "")

    def build():
        try:
            from factor_engine.universe_pit import load_namechange, risk_asof
        except Exception:                                       # noqa: BLE001
            return pd.DataFrame(False, index=pd.Index(dates), columns=codes)
        namechange = load_namechange()
        frame = pd.DataFrame(False, index=pd.Index(dates, name="trade_date"), columns=codes)
        if namechange is None or namechange.empty:
            return frame
        for day in dates:
            result = risk_asof(namechange, codes, day, marker_mode="st_only")
            frame.loc[day] = result["is_risk"].to_numpy()
        return frame

    return _cached(("st_matrix",) + stamp, 900.0, build)


def with_limit_flags(daily: pd.DataFrame, basics: pd.DataFrame | None = None) -> pd.DataFrame:
    """补 prev_close / pct_chg / limit_ratio / is_limit_up / is_limit_down / boards / st_pit。"""
    if daily.empty:
        return daily
    basics = _name_lookup() if basics is None else basics
    frame = daily.copy()
    frame["prev_close"] = frame.groupby("stock_code", sort=False)["close"].shift(1)
    frame["pct_chg"] = frame["close"] / frame["prev_close"] - 1.0

    meta = frame["stock_code"].map(basics["name"]) if not basics.empty else pd.Series("", index=frame.index)
    market = frame["stock_code"].map(basics["market"]) if not basics.empty else pd.Series("", index=frame.index)
    frame["name"] = meta.fillna("")
    frame["market"] = market.fillna("")
    frame["limit_ratio"] = [
        limit_ratio(c, n, m) for c, n, m in zip(frame["stock_code"], frame["name"], frame["market"])
    ]

    # 按价格浮动的涨停容差：见文件头 LIMIT_TOL_MIN/MAX 的标定说明
    tol = (_TICK / frame["prev_close"]).clip(lower=LIMIT_TOL_MIN, upper=LIMIT_TOL_MAX)
    frame["limit_tol"] = tol
    frame["is_limit_up"] = frame["pct_chg"] >= (frame["limit_ratio"] - tol)
    frame["is_limit_down"] = frame["pct_chg"] <= -(frame["limit_ratio"] - tol)
    frame.loc[frame["prev_close"].isna(), ["is_limit_up", "is_limit_down"]] = False

    # PIT 的 ST 标记（只做展示/统计，不参与上限判定）
    days = sorted(frame["trade_date"].unique())
    codes = sorted(frame["stock_code"].unique())
    matrix = _st_matrix(days, codes)
    if isinstance(matrix, pd.DataFrame) and not matrix.empty:
        long = matrix.stack().rename("st_pit").reset_index()
        long.columns = ["trade_date", "stock_code", "st_pit"]
        long["trade_date"] = pd.to_datetime(long["trade_date"])
        long["stock_code"] = long["stock_code"].astype(str)
        frame = frame.merge(long, on=["trade_date", "stock_code"], how="left")
        frame["st_pit"] = frame["st_pit"].fillna(False).astype(bool)
    else:
        frame["st_pit"] = False

    # 连板数：同一只票上连续涨停的天数（当天不涨停则为 0）
    streak = []
    last_code, run = None, 0
    for code, hit in zip(frame["stock_code"], frame["is_limit_up"]):
        if code != last_code:
            last_code, run = code, 0
        run = run + 1 if bool(hit) else 0
        streak.append(run)
    frame["boards"] = streak
    return frame


def board_bucket(boards: int) -> str:
    """一板/两板/三板 → 'b1'/'b2'/'b3'，四板及以上统一 'b4p'（前端再显示 4板+）。"""
    if boards <= 0:
        return ""
    return f"b{boards}" if boards <= 3 else "b4p"


def enriched_daily(days: int = 120) -> pd.DataFrame:
    """带涨停/连板标记的日线窗口（TTL 缓存）。

    `with_limit_flags` 里有一次性 Python 循环（算连板数），960k 行约 0.6s；
    榜单页 + 连板页 + 明细页都会用到，所以必须缓存，不能每个请求重算。
    """
    return _cached(("enriched", int(days)), 300.0, lambda: with_limit_flags(load_daily(days)))


def window_days_for(day) -> int:
    """让窗口刚好覆盖到 `day` 所需的交易日数（+2 天冗余给 prev_close 的 shift）。"""
    dates = trade_dates()
    target = pd.Timestamp(day).normalize()
    if not dates:
        return 60
    index = next((i for i, d in enumerate(dates) if d >= target), len(dates) - 1)
    return int(len(dates) - index) + 2


# 连板数必须在窗口内用"连续涨停"累计出来，所以窗口不能只覆盖到目标日：
# 窗口太短会把长连板截断（实测：detail 一开始只取到目标日 +2 天，10-08 的
# 8 连板被算成 2 板、梯队家数全错）。这里统一多留 30 个交易日做"回看垫"，
# 再贵的票也够把真实连板数接上。
STREAK_LOOKBACK = 30
MIN_WINDOW = 45


def window_for(day=None, lookback: int = 0) -> int:
    """统一的日线窗口大小（交易日数）。"""
    days = MIN_WINDOW
    if day is not None:
        days = max(days, window_days_for(day) + STREAK_LOOKBACK)
    if lookback:
        days = max(days, int(lookback) + STREAK_LOOKBACK + 10)
    return days


def _date_boards(frame: pd.DataFrame, day: pd.Timestamp) -> pd.DataFrame:
    sub = frame[frame["trade_date"] == pd.Timestamp(day)]
    if sub.empty:
        return sub
    cols = ["stock_code", "name", "market", "close", "pct_chg", "amount", "boards",
            "is_limit_up", "is_limit_down", "prev_close", "limit_ratio", "st_pit"]
    return sub[[c for c in cols if c in sub.columns]].copy()


def ladder(lookback: int = 30) -> dict:
    """连板梯队：逐日各板数量 + 昨日→今日 晋级率。

    晋级率 = 昨日恰好 N 板、今日变成 N+1 板及以上的家数 ÷ 昨日恰好 N 板的家数。
    这是"连板记录统计"里唯一能看出**情绪强弱**的量：数量只是存量，晋级率才是流量。
    """
    daily = enriched_daily(window_for(lookback=int(lookback)))
    if daily.empty:
        return {"dates": [], "series": [], "buckets": ["b1", "b2", "b3", "b4p"]}
    flagged = daily
    days = sorted(flagged["trade_date"].unique())[-int(lookback):]
    flagged = flagged[flagged["trade_date"].isin(days)]
    flagged = flagged[flagged["boards"] > 0]

    dates: list[str] = []
    series: list[dict] = []
    per_date = {pd.Timestamp(d): g for d, g in flagged.groupby("trade_date")}
    for i, day in enumerate(days):
        group = per_date.get(pd.Timestamp(day), flagged.iloc[0:0])
        counts = {"b1": 0, "b2": 0, "b3": 0, "b4p": 0}
        for boards in group["boards"]:
            counts[board_bucket(int(boards))] += 1

        promotion: dict[str, dict] = {}
        prev_day = pd.Timestamp(days[i - 1]) if i > 0 else None
        if prev_day is not None and prev_day in per_date:
            prev_group = per_date[prev_day][["stock_code", "boards"]]
            today_map = dict(zip(group["stock_code"], group["boards"]))
            for level, key in ((1, "b1"), (2, "b2"), (3, "b3"), (4, "b4p")):
                pool = prev_group[prev_group["boards"] == level]["stock_code"]
                if key == "b4p":
                    pool = prev_group[prev_group["boards"] >= 4]["stock_code"]
                shifted = [c for c in pool if int(today_map.get(c, 0)) >= level + 1]
                promotion[key] = {
                    "prev": int(len(pool)),
                    "next": int(len(shifted)),
                    "rate": (round(len(shifted) / len(pool), 4) if len(pool) else None),
                }

        series.append({
            "date": pd.Timestamp(day).strftime("%Y-%m-%d"),
            "counts": counts,
            "total": int(len(group)),
            "max_boards": int(group["boards"].max()) if len(group) else 0,
            "promotion": promotion,
        })
        dates.append(pd.Timestamp(day).strftime("%Y-%m-%d"))
    return {"dates": dates, "series": series, "buckets": ["b1", "b2", "b3", "b4p"]}


def limit_up_detail(day, limit: int = 300) -> list[dict]:
    """某日涨停明细（按连板数、成交额降序）。"""
    flagged = enriched_daily(window_for(day=day))
    sub = _date_boards(flagged, pd.Timestamp(day))
    if sub.empty:
        return []
    sub = sub[sub["is_limit_up"]].sort_values(["boards", "amount"], ascending=[False, False])
    rows = []
    for _, r in sub.head(int(limit)).iterrows():
        rows.append({
            "stock_code": str(r["stock_code"]),
            "name": str(r["name"] or ""),
            "market": str(r["market"] or ""),
            "boards": int(r["boards"]),
            "bucket": board_bucket(int(r["boards"])),
            "close": _num(r["close"]),
            "pct_chg": _num(r["pct_chg"], 4),
            "amount": _num(r["amount"]),
            "st_pit": bool(r.get("st_pit", False)),
        })
    return rows


def change_rank(period: str = "daily", limit: int = 50,
                direction: str = "up", board: str = "", exclude_st: bool = False,
                min_amount: float = 0.0) -> tuple[list[dict], dict]:
    """涨跌幅排行。period=daily → 最近交易日单日；weekly → 近 5 个交易日累计。

    过滤（板块 / 排除 ST / 最小成交额）**在排序取前 N 之后不做**，而是先过滤再排序，
    否则会出现"筛掉 ST 后前 50 名只剩 12 只"这种看着像 bug 的结果。
    `stats` 里的市场宽度永远是**全市场**口径（不受过滤影响），否则"上涨家数"会骗人。
    """
    daily = enriched_daily(window_for())
    if daily.empty:
        return [], {}
    flagged = daily
    days = sorted(flagged["trade_date"].unique())
    if not days:
        return [], {}
    span = 1 if period == "daily" else 5
    if len(days) < span + 1:
        return [], {"error": f"本地日线只有 {len(days)} 个交易日，算不出{'周' if span == 5 else '日'}区间"}
    last, base_day = pd.Timestamp(days[-1]), pd.Timestamp(days[-1 - span])
    window = flagged[flagged["trade_date"].isin([last, base_day])]

    pivot_close = window.pivot_table(index="stock_code", columns="trade_date", values="close", aggfunc="last")
    if last not in pivot_close.columns or base_day not in pivot_close.columns:
        return [], {"error": "窗口内缺少基准日或最新日的收盘价"}
    ret = (pivot_close[last] / pivot_close[base_day] - 1.0).dropna()

    today = _date_boards(flagged, last).set_index("stock_code")
    basics = _name_lookup()
    rows = []
    for code, value in ret.items():
        info = today.loc[code] if code in today.index else None
        basics_row = basics.loc[code] if (not basics.empty and code in basics.index) else None
        rows.append({
            "stock_code": str(code),
            "name": str(basics_row["name"]) if basics_row is not None else "",
            "industry": str(basics_row["industry"]) if basics_row is not None else "",
            "market": str(basics_row["market"]) if basics_row is not None else "",
            "close": _num(info["close"]) if info is not None else None,
            "pct_chg": _num(info["pct_chg"], 4) if info is not None else None,
            "period_ret": round(float(value), 5),
            "amount": _num(info["amount"]) if info is not None else None,
            "boards": int(info["boards"]) if info is not None else 0,
            "is_limit_up": bool(info["is_limit_up"]) if info is not None else False,
            "is_limit_down": bool(info["is_limit_down"]) if info is not None else False,
            # 用 PIT 标记（当时是否 ST），拿不到才退回"当前名称"。
            "is_st": bool(info["st_pit"]) if info is not None and "st_pit" in info
                     else is_st_name(basics_row["name"] if basics_row is not None else ""),
        })

    stats = {
        "date": last.strftime("%Y-%m-%d"),
        "base_date": base_day.strftime("%Y-%m-%d"),
        "span": span,
        "n_total": int(len(rows)),
        "n_up": int(sum(1 for r in rows if r["period_ret"] > 0)),
        "n_down": int(sum(1 for r in rows if r["period_ret"] < 0)),
        "n_flat": int(sum(1 for r in rows if r["period_ret"] == 0)),
        "n_limit_up": int(sum(1 for r in rows if r["is_limit_up"])),
        "n_limit_down": int(sum(1 for r in rows if r["is_limit_down"])),
        "mean_ret": round(float(sum(r["period_ret"] for r in rows) / len(rows)), 5) if rows else None,
    }

    if board:
        rows = [r for r in rows if r["market"] == board]
    if exclude_st:
        rows = [r for r in rows if not r["is_st"]]
    if min_amount:
        rows = [r for r in rows if (r["amount"] or 0) >= float(min_amount)]
    rows.sort(key=lambda r: r["period_ret"], reverse=(direction != "down"))
    return rows[: int(limit)], stats


# ─────────────────────────────────────────────────────────────────────────────
# 资金流排行
# ─────────────────────────────────────────────────────────────────────────────
def moneyflow_dates(limit: int = 400) -> list[str]:
    def build():
        if not MONEYFLOW_PANEL.exists():
            return []
        import pyarrow.dataset as ds
        dataset = ds.dataset(str(MONEYFLOW_PANEL), format="parquet")
        table = dataset.to_table(columns=["trade_date"])
        stamps = pd.to_datetime(table.column("trade_date").to_pandas()).dt.normalize()
        return [pd.Timestamp(d).strftime("%Y-%m-%d") for d in sorted(pd.Index(stamps).unique())]

    return _cached(("mf_dates",), 300.0, build)[-int(limit):]


def moneyflow_rows(day: str, source: str = "moneyflow") -> pd.DataFrame:
    """某日某口径的资金流面板切片（已过滤 source，避免同一只票出现 3 行）。"""
    source = source if source in MONEYFLOW_SOURCES else "moneyflow"

    def build():
        import pyarrow.dataset as ds
        dataset = ds.dataset(str(MONEYFLOW_PANEL), format="parquet")
        columns = ["trade_date", "stock_code", "source",
                   f"{source}_net_amount", f"{source}_net_5d", f"{source}_net_10d",
                   f"{source}_net_20d", f"{source}_net_amount_pct"]
        columns = [c for c in columns]
        table = dataset.to_table(
            columns=columns,
            filter=(ds.field("trade_date") == pd.Timestamp(day)) & (ds.field("source") == source),
        )
        frame = table.to_pandas()
        if not frame.empty:
            frame = frame.rename(columns={
                f"{source}_net_amount": "net_amount",
                f"{source}_net_5d": "net_5d",
                f"{source}_net_10d": "net_10d",
                f"{source}_net_20d": "net_20d",
                f"{source}_net_amount_pct": "net_amount_pct",
            })
            frame = frame.drop_duplicates("stock_code", keep="first")
        return frame

    return _cached(("mf_rows", str(day), source), 300.0, build)


def moneyflow_rank(day: str, period: str = "daily", limit: int = 50, direction: str = "in",
                   source: str = "moneyflow", board: str = "",
                   exclude_st: bool = False) -> tuple[list[dict], dict]:
    """资金流排行。daily → 当日净额；weekly → 面板已算好的 5 日累计净额。"""
    frame = moneyflow_rows(day, source)
    if frame.empty:
        return [], {"error": f"{day} 没有 {source} 口径的资金流数据"}
    column = "net_amount" if period == "daily" else "net_5d"
    if column not in frame.columns:
        return [], {"error": f"面板缺少 {column} 列"}
    frame = frame[frame[column].notna()].copy()

    basics = _name_lookup()
    closes: dict[str, dict] = {}
    flagged = enriched_daily(window_for(day=day))
    if not flagged.empty:
        sub = _date_boards(flagged, pd.Timestamp(day))
        if not sub.empty:
            closes = {
                str(r["stock_code"]): {
                    "close": _num(r["close"]), "pct_chg": _num(r["pct_chg"], 4),
                    "amount": _num(r["amount"]), "boards": int(r["boards"]),
                    "is_limit_up": bool(r["is_limit_up"]), "name": str(r["name"] or ""),
                }
                for _, r in sub.iterrows()
            }

    rows = []
    for _, r in frame.iterrows():
        code = str(r["stock_code"])
        basics_row = basics.loc[code] if (not basics.empty and code in basics.index) else None
        quote = closes.get(code, {})
        rows.append({
            "stock_code": code,
            "name": quote.get("name") or (str(basics_row["name"]) if basics_row is not None else ""),
            "industry": str(basics_row["industry"]) if basics_row is not None else "",
            "market": str(basics_row["market"]) if basics_row is not None else "",
            "net_amount": _num(r["net_amount"]),
            "net_period": _num(r.get(column)),
            "net_5d": _num(r.get("net_5d")),
            "net_10d": _num(r.get("net_10d")),
            "net_20d": _num(r.get("net_20d")),
            "net_pct": _num(r.get("net_amount_pct")),
            "close": quote.get("close"),
            "pct_chg": quote.get("pct_chg"),
            "amount": quote.get("amount"),
            "boards": quote.get("boards", 0),
            "is_limit_up": quote.get("is_limit_up", False),
        })
    values = [r["net_period"] for r in rows if r["net_period"] is not None]
    stats = {
        "date": day,
        "period": period,
        "source": source,
        "source_label": MONEYFLOW_SOURCES.get(source, source),
        "column": column,
        "n_total": int(len(rows)),
        "n_in": int(sum(1 for v in values if v > 0)),
        "n_out": int(sum(1 for v in values if v < 0)),
        "sum_net": _num(sum(values)) if values else None,
        "sum_net_5d": _num(frame["net_5d"].sum()) if "net_5d" in frame.columns else None,
    }

    if board:
        rows = [r for r in rows if r["market"] == board]
    if exclude_st:
        rows = [r for r in rows if not is_st_name(r["name"])]
    rows.sort(key=lambda r: (r["net_period"] if r["net_period"] is not None else 0.0),
              reverse=(direction != "out"))
    return rows[: int(limit)], stats


# ─────────────────────────────────────────────────────────────────────────────
# 分时（本地 5 分钟 + 实时）
# ─────────────────────────────────────────────────────────────────────────────
def intraday_store_path(code: str) -> Path | None:
    """本地 5 分钟分时文件：`<6位代码>_<SH|SZ|BJ>.parquet`。"""
    raw = str(code).strip().upper()
    if "." in raw:
        symbol, exchange = raw.split(".", 1)
    else:
        symbol, exchange = raw, ""
    if not exchange:
        exchange = "SH" if symbol.startswith(("6", "9", "5")) else ("BJ" if symbol.startswith("4") else "SZ")
    path = INTRADAY_5MIN_DIR / f"{symbol}_{exchange}.parquet"
    return path if path.exists() else None


def _intraday_store_full(code: str) -> pd.DataFrame:
    """整只票的本地 5 分钟分时（baostock，约 1.1 万行 / 覆盖一年多）。"""
    path = intraday_store_path(code)
    if path is None:
        return pd.DataFrame()

    def build():
        frame = pd.read_parquet(path, columns=["date", "time", "open", "high", "low", "close",
                                               "volume", "amount"])
        frame["date"] = pd.to_datetime(frame["date"]).dt.strftime("%Y-%m-%d")
        stamp = frame["time"].astype(str).str.slice(0, 14)
        frame["clock"] = pd.to_datetime(stamp, format="%Y%m%d%H%M%S", errors="coerce")
        return frame.dropna(subset=["clock"]).sort_values("clock")

    return _cached(("intraday_store_full", str(path)), 600.0, build)


def load_intraday_store(code: str, day: str | None = None) -> pd.DataFrame:
    """读本地 5 分钟分时（baostock）。列：clock / open / high / low / close / volume / amount。

    `day` 留空时**只取文件里的最后一天** —— 分时文件是逐只全历史累积（约 1.1 万行、
    覆盖一年多），不截断就会把一整年的 5 分钟线当成"当日分时"画出来（实测踩过：
    live 挂掉退化到本地时，前端拿到 11568 个点、横跨 2025-10 到 2026-09）。
    """
    frame = _intraday_store_full(code)
    if frame.empty:
        return frame
    if day:
        frame = frame[frame["date"] == day]
    else:
        frame = frame[frame["date"] == frame["date"].max()]
    return frame


def intraday_store_coverage(code: str) -> dict:
    """本地 5 分钟归档的日期覆盖（K 线页点日期前用来判断"这天有没有分时"）。"""
    frame = _intraday_store_full(code)
    if frame.empty:
        return {"available": False, "path": None, "first_date": None, "last_date": None,
                "n_dates": 0, "dates": []}
    dates = sorted(frame["date"].unique().tolist())
    path = intraday_store_path(code)
    return {
        "available": True,
        "path": str(path) if path else None,
        "first_date": dates[0],
        "last_date": dates[-1],
        "n_dates": len(dates),
        "dates": dates,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 小工具
# ─────────────────────────────────────────────────────────────────────────────
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


def yi(wan: float | None) -> float | None:
    """万元 → 亿元（资金流三家口径的原始单位都是万元）。"""
    if wan is None:
        return None
    return round(float(wan) / 1e4, 4)
