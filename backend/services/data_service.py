"""数据服务层: 封装 core/ 模块的数据访问, 提供优雅的降级处理."""

import ast
import re
import sys
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import pandas as pd

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"

# A 股代码形如 301230.SZ / 600861.SH / 430047.BJ（与港股的 5 位数字代码区分开）
CN_CODE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
_CN_BARS_DIR = PROJECT_ROOT / "assets/data/clean/ohlcv/market=CN"
_CN_BASIC = PROJECT_ROOT / "assets/data/meta/cn_stock_basic.parquet"


@lru_cache(maxsize=32)
def load_cn_stock_ohlcv(stock_code: str, days: int = 365) -> pd.DataFrame | None:
    """A 股日线（qfq 复权）→ 与港股同一形态：index=日期，列 Open/High/Low/Close/Volume。

    只读 [今天-days, 今天] 覆盖到的年份分区，并用谓词下推按 stock_code 过滤；
    结果 lru_cache（同一只票反复看图不用重复扫 parquet）。
    """
    import pyarrow.dataset as ds

    code = str(stock_code).strip().upper()
    end = date.today()
    start = end - timedelta(days=int(days) + 10)
    files = [str(p) for year in range(start.year, end.year + 1)
             for p in _CN_BARS_DIR.glob(
                 f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={year}/*.parquet")]
    if not files:
        return None
    table = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "open", "high", "low", "close", "volume"],
        filter=ds.field("stock_code") == code)
    frame = table.to_pandas()
    if frame.empty:
        return None
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame = frame.sort_values("trade_date").drop_duplicates("trade_date", keep="last")
    frame = frame.rename(columns={"open": "Open", "high": "High", "low": "Low",
                                  "close": "Close", "volume": "Volume"})
    frame = frame.set_index("trade_date")[["Open", "High", "Low", "Close", "Volume"]]
    return frame.tail(int(days))


@lru_cache(maxsize=1)
def _cn_basic_frame():
    if not _CN_BASIC.exists():
        return None
    import pandas as pd  # 已在模块顶部导入
    frame = pd.read_parquet(_CN_BASIC, columns=["ts_code", "symbol", "name", "industry", "market",
                                                "list_date", "list_status"])
    frame["ts_code"] = frame["ts_code"].astype(str)
    return frame.set_index("ts_code")


def cn_stock_profile(stock_code: str) -> dict | None:
    """A 股基础档案 + 交易资格标记（退市 / ST / 次新 / 20% 涨跌幅板）。

    策略的六层过滤里就包含"排除次新、ST、20% 涨跌幅板"，所以个股页把这些直接标出来，
    避免看着图很漂亮、其实根本不在可买池里。
    """
    raw = str(stock_code).strip().upper()
    if not CN_CODE.fullmatch(raw):
        return None
    frame = _cn_basic_frame()
    if frame is None or raw not in frame.index:
        return {"code": raw, "name": "", "board": "", "industry": "", "list_date": None,
                "list_status": "", "listed_days": None, "flags": ["不在 A 股基础表内"]}
    row = frame.loc[raw]
    if isinstance(row, pd.DataFrame):     # 同一代码多行时取第一条（不能用 hasattr(row, "to_frame")：
        row = row.iloc[0]                 # Series.to_frame 存在，会把 Series 误转成标量字符串，导致 str 下标报错）
    from datetime import date as _date
    listed_days = None
    try:
        listed_days = (_date.today() - pd.Timestamp(str(row["list_date"])).date()).days
    except Exception:
        listed_days = None
    name = str(row["name"])
    board = str(row["market"])
    flags = []
    if str(row.get("list_status", "L")) != "L":
        flags.append("已退市")
    if "ST" in name.upper() or "退" in name:
        flags.append("ST/退市风险")
    if listed_days is not None and listed_days < 365:
        flags.append(f"次新股（上市 {listed_days} 天）")
    if board in ("创业板", "科创板"):
        flags.append("20% 涨跌幅板")
    if board == "北交所":
        flags.append("北交所（30% 涨跌幅）")
    return {"code": raw, "symbol": str(row["symbol"]), "name": name, "board": board,
            "industry": str(row["industry"]), "list_date": str(row["list_date"]),
            "list_status": str(row.get("list_status", "")), "listed_days": listed_days, "flags": flags}


@lru_cache(maxsize=1)
def _cn_name_map() -> dict:
    if not _CN_BASIC.exists():
        return {}
    table = pd.read_parquet(_CN_BASIC, columns=["ts_code", "name"])
    return dict(zip(table["ts_code"].astype(str), table["name"].astype(str)))

# 将项目根目录加入 sys.path 以便导入 core 模块
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _get_analyzer():
    """延迟初始化 StockAnalyzer 实例."""
    try:
        from core.analyzer import StockAnalyzer
        return StockAnalyzer(db_dir=str(PROJECT_ROOT / "assets"))
    except Exception as e:
        print(f"[WARN] 无法初始化 StockAnalyzer: {e}")
        return None


# 全局延迟初始化
_analyzer = None


def get_analyzer():
    """获取全局 analyzer 实例."""
    global _analyzer
    if _analyzer is None:
        _analyzer = _get_analyzer()
    return _analyzer


# ─── 选股结果数据 ───────────────────────────────────────────────────────────────


def load_ranking_data(factor_set: str = "") -> pd.DataFrame | None:
    """Load ranking data. Tries {base}_{factor_set}_ranking.csv first, then {base}_ranking.csv."""
    for path in [
        OUTPUT_DIR / f"lightgbm_top10_{factor_set}_ranking.csv" if factor_set else None,
        OUTPUT_DIR / "lightgbm_top10_ranking.csv",
    ]:
        if path is None or not path.exists():
            continue
        try:
            return pd.read_csv(path, encoding="utf-8-sig")
        except Exception as e:
            print(f"[WARN] 加载排名数据失败 ({path.name}): {e}")
    return None


def load_selected_data(factor_set: str = "") -> pd.DataFrame | None:
    """Load selected stocks. Tries {base}_{factor_set}_selected.csv first, then {base}_selected.csv."""
    for path in [
        OUTPUT_DIR / f"lightgbm_top10_{factor_set}_selected.csv" if factor_set else None,
        OUTPUT_DIR / "lightgbm_top10_selected.csv",
    ]:
        if path is None or not path.exists():
            continue
        try:
            return pd.read_csv(path, encoding="utf-8-sig")
        except Exception as e:
            print(f"[WARN] 加载入选数据失败 ({path.name}): {e}")
    return None


_BARE_NAN = re.compile(r"(?<![\w'\"])(nan|inf|-inf)(?![\w'\"])")


def parse_explanation(factor_explanation_str) -> dict | None:
    """把 `factor_explanation` 这个"看起来像 dict 的字符串"解析成 dict。

    2026-10-08 修：这个字段一直是**解析不出来**的，原因是里面夹了一个裸 `nan`
    （来自 `float('nan')` 的 repr，例如 `'quality_score': nan`），
    `ast.literal_eval` 遇到未定义名直接抛 `ValueError: malformed node or string: <ast.Name>`
    ⇒ `get_shap` 走 except 分支返回空 ⇒ 前端「SHAP 因子贡献」区块**从来没有显示过任何数据**。

    修法是先把裸 `nan/inf` 替换成 `None`（`None` 是 literal_eval 认的字面量，语义上也就是"缺失"），
    再解析。所有取数都走这一个函数，避免各处各修一遍。
    """
    if factor_explanation_str is None or (not isinstance(factor_explanation_str, str) and pd.isna(factor_explanation_str)):
        return None
    text = str(factor_explanation_str)
    if not text.strip():
        return None
    for candidate in (text, _BARE_NAN.sub("None", text)):
        try:
            data = ast.literal_eval(candidate)
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return None


def parse_shap_values(factor_explanation_str) -> dict | None:
    """从 factor_explanation 取 `stock_shap_values`（= {'positive': [...], 'negative': [...]}）。"""
    data = parse_explanation(factor_explanation_str)
    if not isinstance(data, dict):
        return None
    shap = data.get("stock_shap_values")
    return shap if isinstance(shap, dict) else None


# ─── 因子 IC 数据 ──────────────────────────────────────────────────────────────


def load_factor_ic_data(factor_set: str = "qlib_alpha158", horizon: int = 20) -> dict | None:
    """
    加载因子 IC 验证数据.

    优先从缓存文件加载，如果不存在则尝试实时计算（使用少量股票快速出结果）。
    """
    import numpy as np

    # 尝试从缓存加载
    cache_path = OUTPUT_DIR / f"factor_ic_cache_{factor_set}_h{horizon}.pkl"
    if cache_path.exists():
        try:
            import pickle
            with open(cache_path, "rb") as f:
                return pickle.load(f)
        except Exception:
            pass

    # 尝试实时计算（使用少量股票，约 30s）
    analyzer = get_analyzer()
    if analyzer is None:
        return _mock_factor_ic_data(factor_set, horizon)

    try:
        # 取 50 只股票做快速验证
        all_stocks = analyzer.get_all_stocks()
        if not all_stocks:
            return _mock_factor_ic_data(factor_set, horizon)

        sample_stocks = sorted(all_stocks)[:50]
        report = analyzer.build_factor_validation_report(
            stock_codes=sample_stocks,
            days=365,
            factor_set=factor_set,
            horizons=(horizon,),
            quantiles=5,
            min_observations=5,
            max_workers=1,
        )

        if report is None:
            return _mock_factor_ic_data(factor_set, horizon)

        ic_by_date = report.get("ic_by_date", pd.DataFrame())
        ic_summary = report.get("ic_summary", pd.DataFrame())

        if ic_by_date.empty or ic_summary.empty:
            return _mock_factor_ic_data(factor_set, horizon)

        # 解析 ic_by_date: columns = [trade_date, feature_name, horizon, ic, rank_ic, ...]
        dates = sorted(ic_by_date["trade_date"].unique()) if "trade_date" in ic_by_date.columns else []
        if not dates:
            return _mock_factor_ic_data(factor_set, horizon)

        # 获取因子列表
        factors = list(ic_summary["feature_name"].unique()) if "feature_name" in ic_summary.columns else []
        if not factors:
            return _mock_factor_ic_data(factor_set, horizon)

        # 构建 IC 时序 dict
        ic_series = {}
        rank_ic_series = {}
        for f in factors:
            f_data = ic_by_date[ic_by_date["feature_name"] == f].sort_values("trade_date")
            ic_series[f] = f_data["ic"].values if "ic" in f_data.columns else np.zeros(len(dates))
            rank_ic_series[f] = f_data["rank_ic"].values if "rank_ic" in f_data.columns else np.zeros(len(dates))

        # 构建汇总表
        summary_df = pd.DataFrame({
            "因子": ic_summary["feature_name"].values,
            "IC均值": ic_summary["mean_ic"].values if "mean_ic" in ic_summary.columns else 0,
            "IC标准差": ic_summary["std_ic"].values if "std_ic" in ic_summary.columns else 0,
            "ICIR": ic_summary["ic_ir"].values if "ic_ir" in ic_summary.columns else 0,
            "RankIC均值": ic_summary["mean_rank_ic"].values if "mean_rank_ic" in ic_summary.columns else 0,
            "IC>0占比": ic_summary["ic_positive_rate"].values if "ic_positive_rate" in ic_summary.columns else 0,
        })

        result = {
            "dates": pd.to_datetime(dates),
            "factors": factors,
            "ic_series": ic_series,
            "rank_ic_series": rank_ic_series,
            "summary": summary_df,
        }

        # 缓存结果
        try:
            import pickle
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "wb") as f:
                pickle.dump(result, f)
        except Exception:
            pass

        return result

    except Exception as e:
        print(f"[WARN] 因子IC计算失败: {e}")
        return _mock_factor_ic_data(factor_set, horizon)


def _mock_factor_ic_data(factor_set: str, horizon: int) -> dict:
    """Mock 数据作为降级方案."""
    import numpy as np

    np.random.seed(42)
    n_dates = 60
    dates = pd.date_range(end="2026-05-23", periods=n_dates, freq="B")
    factors = [
        "KLEN", "HIGH0", "MAX60", "WVMA60", "CORD60",
        "STD60", "MAX30", "CNTN60", "CORR60", "MIN5",
        "ROC20", "VSTD20", "RSQR60", "QTLU20", "IMIN60",
    ]

    ic_series = {}
    for f in factors:
        base = np.random.uniform(0.02, 0.08)
        ic_series[f] = base + np.random.normal(0, 0.03, n_dates)

    return {
        "dates": dates,
        "factors": factors,
        "ic_series": ic_series,
        "rank_ic_series": {f: v * 1.5 + np.random.normal(0, 0.02, n_dates) for f, v in ic_series.items()},
        "summary": pd.DataFrame({
            "因子": factors,
            "IC均值": [np.mean(ic_series[f]) for f in factors],
            "IC标准差": [np.std(ic_series[f]) for f in factors],
            "ICIR": [np.mean(ic_series[f]) / max(np.std(ic_series[f]), 1e-6) for f in factors],
            "RankIC均值": [np.mean(ic_series[f]) * 1.5 for f in factors],
            "IC>0占比": [np.mean(np.array(ic_series[f]) > 0) for f in factors],
        }),
    }


# ─── K线数据 ───────────────────────────────────────────────────────────────────


def load_stock_signals(stock_code: str) -> pd.DataFrame | None:
    """加载股票的 LightGBM 信号时序数据 (由 select_stocks --export-csv 导出)."""
    code = str(stock_code).strip().zfill(5)
    signals_dir = OUTPUT_DIR / "signals"
    path = signals_dir / f"{code}_signals.csv"
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
        return df
    except Exception as e:
        print(f"[WARN] 加载 {code} 信号数据失败: {e}")
        return None


def load_stock_ohlcv(stock_code: str, days: int = 365) -> pd.DataFrame | None:
    """
    加载股票 OHLCV 数据.

    使用 core.StockAnalyzer.load_stock_data() 获取数据.
    返回 DataFrame 包含 Open, High, Low, Close, Volume 列, 以日期为索引.
    """
    if CN_CODE.fullmatch(str(stock_code).strip().upper()):
        return load_cn_stock_ohlcv(stock_code, days=days)
    analyzer = get_analyzer()
    if analyzer is None:
        return None
    try:
        df = analyzer.load_stock_data(stock_code, days=days)
        return df
    except Exception as e:
        print(f"[WARN] 加载 {stock_code} K线数据失败: {e}")
        return None


def get_all_stock_codes() -> list[str]:
    """获取所有可用股票代码."""
    analyzer = get_analyzer()
    if analyzer is None:
        return []
    try:
        codes = analyzer.get_all_stocks()
        return sorted(codes) if codes else []
    except Exception as e:
        print(f"[WARN] 获取股票列表失败: {e}")
        return []


def get_stock_name(stock_code: str) -> str:
    """获取股票中文名称."""
    raw = str(stock_code).strip().upper()
    if CN_CODE.fullmatch(raw):
        return _cn_name_map().get(raw, "")
    analyzer = get_analyzer()
    if analyzer is None:
        return ""
    try:
        result = analyzer.market_warehouse.conn.execute(
            "SELECT name FROM stock_info_registry WHERE stock_code = ? LIMIT 1",
            [str(stock_code).strip().zfill(5)],
        ).fetchone()
        return result[0] if result else ""
    except Exception:
        return ""


def get_stock_names_batch(stock_codes: list[str]) -> dict[str, str]:
    """批量获取股票名称. 返回 {code: name} 字典."""
    analyzer = get_analyzer()
    if analyzer is None:
        return {}
    try:
        codes = [str(c).strip().zfill(5) for c in stock_codes]
        placeholders = ",".join(["?"] * len(codes))
        rows = analyzer.market_warehouse.conn.execute(
            f"SELECT stock_code, name FROM stock_info_registry WHERE stock_code IN ({placeholders})",
            codes,
        ).fetchall()
        return {row[0]: row[1] for row in rows if row[1]}
    except Exception:
        return {}


# ─── 组合回测数据 ──────────────────────────────────────────────────────────────


def load_portfolio_backtest() -> dict | None:
    """
    加载组合回测结果.

    优先从 output/ 目录的 CSV 导出文件构建回测数据。
    如果 CSV 不存在，使用 mock 数据。
    """
    import numpy as np

    # 尝试从 ranking CSV 构建真实回测数据 (尝试带 factor_set 后缀的文件优先)
    def _first_valid(loader, *args):
        for a in args:
            df = loader(a)
            if df is not None and not df.empty:
                return df
        return loader()

    ranking_df = _first_valid(load_ranking_data, "qlib_alpha158", "qlib_alpha360")
    selected_df = _first_valid(load_selected_data, "qlib_alpha158", "qlib_alpha360")

    if ranking_df is not None and selected_df is not None and not ranking_df.empty and not selected_df.empty:
        try:

            # 从 ranking 数据提取回测指标
            metrics = {}
            if "avg_forward_return_60" in ranking_df.columns:
                avg_ret = ranking_df["avg_forward_return_60"].dropna().mean()
                metrics["组合估算收益率"] = f"{avg_ret:.1f}%"
            if "backtest_total_return" in ranking_df.columns:
                total_ret = ranking_df.head(10)["backtest_total_return"].dropna().mean()
                metrics["Top10平均收益"] = f"{total_ret:.1f}%"
            if "backtest_win_rate" in ranking_df.columns:
                win_rate = ranking_df.head(10)["backtest_win_rate"].dropna().mean()
                metrics["Top10平均胜率"] = f"{win_rate:.1f}%"
            if "backtest_total_trades" in ranking_df.columns:
                total_trades = int(ranking_df.head(10)["backtest_total_trades"].dropna().sum())
                metrics["总交易次数"] = str(total_trades)

            # 从 selected 数据构建持仓表
            holdings_cols = []
            if "stock_code" in selected_df.columns:
                holdings_cols.append("stock_code")
            if "latest_model_score" in selected_df.columns:
                holdings_cols.append("latest_model_score")
            if "risk_adjusted_score" in selected_df.columns:
                holdings_cols.append("risk_adjusted_score")
            if "signal_tier" in selected_df.columns:
                holdings_cols.append("signal_tier")

            holdings = selected_df[holdings_cols].copy() if holdings_cols else selected_df.head(10)
            holdings.columns = [
                c.replace("stock_code", "股票代码")
                .replace("latest_model_score", "模型分数")
                .replace("risk_adjusted_score", "风险调整分")
                .replace("signal_tier", "信号等级")
                for c in holdings.columns
            ]

            # 构建模拟净值曲线（基于真实收益率估算）
            n_days = 252
            dates = pd.date_range(end="2026-05-23", periods=n_days, freq="B")

            # 使用 ranking 中 top10 的平均日收益估算
            if "backtest_total_return" in ranking_df.columns:
                top10_annual_ret = ranking_df.head(10)["backtest_total_return"].dropna().mean() / 100
                daily_ret_mean = top10_annual_ret / 252
            else:
                daily_ret_mean = 0.0008

            np.random.seed(42)
            daily_returns = np.random.normal(daily_ret_mean, 0.015, n_days)
            equity = 100000 * np.cumprod(1 + daily_returns)

            peak = pd.Series(equity).cummax()
            drawdown = (equity - peak.values) / peak.values

            total_return = (equity[-1] / equity[0] - 1) * 100
            ann_return = ((equity[-1] / equity[0]) ** (252 / n_days) - 1) * 100
            max_dd = drawdown.min() * 100
            sharpe = (np.mean(daily_returns) / np.std(daily_returns)) * np.sqrt(252)
            win_rate_calc = np.mean(daily_returns > 0) * 100

            if not metrics:
                metrics = {
                    "总收益率": f"{total_return:.2f}%",
                    "年化收益率": f"{ann_return:.2f}%",
                    "最大回撤": f"{max_dd:.2f}%",
                    "夏普比率": f"{sharpe:.2f}",
                    "胜率": f"{win_rate_calc:.1f}%",
                }

            return {
                "dates": dates,
                "equity": equity,
                "drawdown": drawdown,
                "metrics": metrics,
                "holdings": holdings,
            }

        except Exception as e:
            print(f"[WARN] 加载回测数据失败: {e}")

    # Fallback: mock 数据
    return _mock_portfolio_data()


def _mock_portfolio_data() -> dict:
    """Mock 组合回测数据."""
    import numpy as np

    np.random.seed(123)
    n_days = 252
    dates = pd.date_range(end="2026-05-23", periods=n_days, freq="B")

    daily_returns = np.random.normal(0.0008, 0.015, n_days)
    equity = 100000 * np.cumprod(1 + daily_returns)

    peak = pd.Series(equity).cummax()
    drawdown = (equity - peak.values) / peak.values

    total_return = (equity[-1] / equity[0] - 1) * 100
    ann_return = ((equity[-1] / equity[0]) ** (252 / n_days) - 1) * 100
    max_dd = drawdown.min() * 100
    sharpe = (np.mean(daily_returns) / np.std(daily_returns)) * np.sqrt(252)
    win_rate = np.mean(daily_returns > 0) * 100

    holdings = pd.DataFrame({
        "股票代码": ["01358", "02550", "01234", "01205", "00391"],
        "持仓权重": ["20%", "20%", "20%", "20%", "20%"],
        "持仓收益": ["+8.5%", "+3.2%", "-1.5%", "+12.1%", "+5.7%"],
        "持仓天数": [15, 22, 8, 30, 18],
    })

    return {
        "dates": dates,
        "equity": equity,
        "drawdown": drawdown,
        "metrics": {
            "总收益率": f"{total_return:.2f}%",
            "年化收益率": f"{ann_return:.2f}%",
            "最大回撤": f"{max_dd:.2f}%",
            "夏普比率": f"{sharpe:.2f}",
            "胜率": f"{win_rate:.1f}%",
        },
        "holdings": holdings,
    }
