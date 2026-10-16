"""「选股结果」页的数据源注册表。

背景（为什么要有这个模块）
--------------------------
`/api/selection` 原来只读一个文件：`output/lightgbm_top10_selected.csv` —— 那是**港股**
LightGBM 名单（5 位代码、`factor_explanation` 里带 SHAP）。但整个系统的主工作流早已是 A 股：
`output/results_cn/` 下才是每天真正在跑的生产名单（preselect → PK）与研究手臂名单。

结果就是前端首页显示的名单和命令行跑出来的名单**不是一回事**，页面上还没有任何提示。
这里把三个源显式登记成一张表，接口按 `source` 取，页面把「数据源 / 文件 / 时间 / 交易日」摊开给用户看。

口径约定
--------
* `code_style="ts_code"`：CN 侧的代码本身就是 `301448.SZ`，**不要** zfill(5)。
* `code_style="hk"`：港股是 5 位纯数字，要 zfill(5) 才能查名称。
* 列是**白名单**：CN 的 CSV 有 61 列，整包丢给前端既没用又容易泄口径，只挑展示需要的。
"""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
RESULTS_CN = ROOT / "output/results_cn"
OUTPUT = ROOT / "output"
CN_BASIC = ROOT / "assets/data/meta/cn_stock_basic.parquet"

# 展示列白名单：(csv 列名, 中文表头, 是否文本列)
_COLS = {
    "roa": ("roa", "ROA(%)", False),
    "roa_rank": ("roa_rank", "ROA分位", False),
    "roe": ("roe", "ROE(%)", False),
    "market_cap": ("market_cap", "总市值(亿元)", False),
    "roe_rank": ("roe_rank", "ROE分位", False),
    "market_cap_rank": ("market_cap_rank", "市值分位", False),
    "ann_date": ("ann_date", "公告日(PIT)", True),
    "end_date": ("end_date", "财报期", True),
    "rank": ("rank", "#", False),
    # 概念/板块手臂（cn_arm_concept）新增列
    "concept_score": ("concept_score", "概念分", False),
    "rev_5d": ("rev_5d", "5日涨幅", False),
    "board": ("board", "概念/板块", True),
    "stock_code": ("stock_code", "代码", True),
    "name": ("name", "名称", True),
    "stage": ("stage", "阶段", True),
    "model_score": ("model_score", "model_score", False),
    "ensemble_score": ("ensemble_score", "ensemble", False),
    "lightgbm_score": ("lightgbm_score", "lightgbm", False),
    "transformer_score": ("transformer_score", "transformer", False),
    "target_weight": ("target_weight", "目标权重", False),
    "selection_channel": ("selection_channel", "渠道", True),
    "selection_sleeve": ("selection_sleeve", "腿", True),
    "constraint_status": ("constraint_status", "约束", True),
    "industry_l1": ("industry_l1", "行业", True),
    "last_close": ("last_close", "收盘", False),
    "lots_at_target": ("lots_at_target", "手数", False),
    "tradable_flag": ("tradable_flag", "可交易", True),
    "board": ("board", "板块", True),
    "industry": ("industry", "行业", True),
    "score": ("score", "分数", False),
    "in_portfolio": ("in_portfolio", "持仓", True),
    "lots_at_capital": ("lots_at_capital", "手数", False),
    "planned_amount": ("planned_amount", "金额", False),
    "pit_name": ("pit_name", "快照日名称(PIT)", True),
    "mktcap_rank": ("mktcap_rank", "市值分位", False),
    # 实盘镜像（2026-10-12）：真实券商持仓接进同一条展示链路
    "cost_price": ("cost_price", "成本价", False),
    "pnl_amount": ("pnl_amount", "浮盈(元)", False),
    "pnl_pct": ("pnl_pct", "浮盈(%)", False),
    "action": ("action", "建议", True),
    "rule": ("rule", "规则", True),
    "note": ("note", "说明", True),
    "price_source": ("price_source", "价源", True),
    # 港股侧（沿用原有中文表头）
    "ranking_score": ("ranking_score", "综合评分", False),
    "signal_tier": ("signal_tier", "信号等级", True),
    "setup_type": ("setup_type", "形态类型", True),
    "risk_adjusted_score": ("risk_adjusted_score", "风险调整得分", False),
    "win_rate": ("win_rate", "胜率(%)", False),
    "backtest_return": ("backtest_return", "回测收益(%)", False),
}

# ── 数据源注册表 ───────────────────────────────────────────────────────────────
# 9 条**策略**源由抽象层派生（`config/strategies.json` 的 `selection` 块 →
# `backend/services/strategy_registry.py`）。以前它们硬编码在这里，新增一条策略必须回来改 Python，
# 漏改的表现是"/api/selection?source=xxx 未知数据源，已回退默认源" —— 页面看不出是接线漏了。
# 现在：**新增策略只改 config，这里不用动**。
from backend.services import strategy_registry as registry


def _derived_sources() -> dict[str, dict]:
    return {**registry.selection_sources(), **LEGACY_SOURCES}


class _SourceMap:
    """`src.SOURCES` 的懒加载视图。

    为什么要懒：web 端"新增策略自动同步"要求**改完 config 不用重启**。
    普通 dict 是 import 时求值的，改 config 后进程内一直是旧表（实测过：加了策略页面却看不到）。
    这个类支持 `.get / [] / in / keys / items / len / 迭代`，与 dict 用法完全一致。
    """

    def _data(self) -> dict[str, dict]:
        return _derived_sources()

    def __getitem__(self, key: str) -> dict:
        return self._data()[key]

    def get(self, key: str, default=None):
        return self._data().get(key, default)

    def __contains__(self, key: object) -> bool:
        return key in self._data()

    def __iter__(self):
        return iter(self._data())

    def __len__(self) -> int:
        return len(self._data())

    def keys(self):
        return self._data().keys()

    def items(self):
        return self._data().items()

    def values(self):
        return self._data().values()


# 不是策略、只作历史遗留展示的源（港股链路），继续留在代码里
LEGACY_SOURCES: dict[str, dict] = {
    "hk_lightgbm": {
        "label": "港股 LightGBM Top10（历史遗留）",
        "note": "旧的港股链路产物；带 SHAP/factor_explanation，是本页「因子贡献」分析的唯一来源。",
        "files": [("selected", OUTPUT / "lightgbm_top10_selected.csv")],
        "code_style": "hk",
        "columns": [
            "stock_code", "ranking_score", "signal_tier", "setup_type",
            "risk_adjusted_score", "win_rate", "backtest_return",
        ],
        "supports_shap": True,
    },
}

SOURCES = _SourceMap()

DEFAULT_SOURCE = "cn_ensemble"


HK_ALIAS = ROOT / "docs/hk_entity_alias_registry.csv"


@lru_cache(maxsize=1)
def _hk_alias_table() -> dict[str, str]:
    """港股 5 位代码 → 公司名。

    来源优先级与 `stock_search._hk_table` 一致：① 别名注册表 CSV ② SQLite 注册表。
    踩过的坑：本机的 `stock_info_registry` 查不到港股名（实测 `get_stock_names_batch(['01358'])` → `{}`），
    所以只用它会让港股页面的"名称"列长期空白；CSV 才是这个环境里真正有数据的那份。
    另：CSV 里 `00001` 会被 pandas 读成 int（`700` 这种），必须 `str.zfill(5)` 补回来。
    """
    if not HK_ALIAS.exists():
        return {}
    try:
        alias = pd.read_csv(HK_ALIAS, usecols=["stock_code", "market", "alias", "alias_type"],
                            dtype={"stock_code": str})
        alias = alias[(alias["market"] == "HK") & (alias["alias_type"] == "company_name")]
        return dict(zip(alias["stock_code"].astype(str).str.zfill(5), alias["alias"].astype(str)))
    except Exception:
        return {}


def _hk_names(codes: list[str]) -> dict[str, str]:
    """港股名称：CSV 优先，缺失的再用 SQLite 注册表兜底。"""
    table = _hk_alias_table()
    missing = [c for c in codes if c.zfill(5) not in table]
    if missing:
        try:
            from backend.services.data_service import get_stock_names_batch

            table = {**table, **get_stock_names_batch([c.zfill(5) for c in missing])}
        except Exception:
            pass
    return table


@lru_cache(maxsize=1)
def _cn_names() -> dict[str, str]:
    if not CN_BASIC.exists():
        return {}
    frame = pd.read_parquet(CN_BASIC, columns=["ts_code", "name"]).drop_duplicates("ts_code")
    return dict(zip(frame["ts_code"].astype(str), frame["name"].fillna("").astype(str)))



# 伪名字集合（2026-10-15 事故）：`str(np.nan or "")` 得到的是**字符串 "nan"** —— 因为 np.nan 是
# **真值**，`nan or ""` 取的是 nan，再 str() 就成了 "nan" ⇒ 前端自选里直接显示 "nan"
# （实测：/api/selection?source=cn_box_arm 的 stock_name 就是 "nan"，localStorage 里也被存成了 "nan"）。
_JUNK_NAMES = {"", "nan", "none", "null", "na", "nat", "undefined"}


def _clean_name(value) -> str:
    """把 NaN / None / 空串 / "nan" 这类"伪名字"统一清成空串；真名字去掉首尾空白。"""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return "" if text.lower() in _JUNK_NAMES else text


def _file_meta(path: Path) -> dict:
    if not path.exists():
        return {"path": str(path.relative_to(ROOT)), "exists": False, "mtime": None, "rows": None}
    try:
        rows = int(len(pd.read_csv(path, encoding="utf-8-sig", usecols=[0])))
    except Exception:
        rows = None
    return {
        "path": str(path.relative_to(ROOT)),
        "exists": True,
        "mtime": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        "bytes": path.stat().st_size,
        "rows": rows,
    }


def list_sources() -> list[dict]:
    """给前端的数据源清单（含可用性与文件指纹，页面直接展示，不用再猜）。"""
    out = []
    for key, spec in SOURCES.items():
        metas = [dict(_file_meta(p), role=role) for role, p in spec["files"]]
        out.append({
            "key": key,
            "label": spec["label"],
            "note": spec["note"],
            "available": any(m["exists"] for m in metas),
            "supports_shap": spec["supports_shap"],
            "files": metas,
        })
    return out


def _read_merged(spec: dict) -> pd.DataFrame:
    """把同一源的多个文件合成**一只票一行**。

    两个坑：
      1. `preselected` 与 `selected` 天然重叠（入选的票必然先被预选）。
         直接 concat 会让 `301230.SZ` 出现两次、rank 都是 1，页面看起来像重复数据。
      2. 两个文件的列集不同：`selected` 61 列（含行业/收盘/手数/可交易），
         `preselected` 只有 22 列。所以不能简单地"以宽表为基底左连接" —— 那样
         只在 `preselected` 里出现的 6 只票会丢掉 rank / model_score。
         正确做法是**外连接后逐列 coalesce**（同名时以 `selected` 为准）。

    最终 `stage` 标注每只票走到哪一步：最终入选（PK 后） / 候选池（preselect）。
    """
    frames = {role: pd.read_csv(path, encoding="utf-8-sig")
              for role, path in spec["files"] if path.exists()}
    frames = {k: v for k, v in frames.items() if not v.empty}
    if not frames:
        return pd.DataFrame()
    if len(frames) == 1:
        (role, frame), = frames.items()
        out = frame.copy()
        # role → 阶段文案。2026-10-15 补 "arm"：`arm_select` 的 roles 是 [["arm", …]]（冻结的迁移前口径，
        # 不许改），原来落到 else 分支 ⇒ 名单没有 stage 标签，前端的「阶段」列会是空的
        # （被 test_positive_expectancy_strategies_are_selectable_everywhere 抓到）。
        out["stage"] = ("候选研究名单" if role == "research" else
                        "研究手臂名单" if role == "arm" else
                        "最终入选（PK 后）" if role == "selected" else "候选池（preselect）")
        return _sort(out)

    primary = "selected" if "selected" in frames else next(iter(frames))
    secondary = next(r for r in frames if r != primary)
    left, right = frames[primary].copy(), frames[secondary].copy()
    # 关键：标记要打在 primary 侧。打在 secondary 侧的话，所有票都在 preselect 里出现，
    # stage 会全变成"候选池"，入选那两只认不出来（第一版就是这个错）。
    left["_in_primary"] = True
    out = left.merge(right, on="stock_code", how="outer", suffixes=("", f"_{secondary}"))

    # 逐列 coalesce：主表有值用主表，否则用副表
    suffix = f"_{secondary}"
    for col in [c for c in list(out.columns) if c.endswith(suffix) and not c.startswith("_")]:
        stem = col[: -(len(secondary) + 1)]
        if stem not in out.columns:
            out[stem] = out[col]
        else:
            out[stem] = out[stem].combine_first(out[col])
        out = out.drop(columns=[col])

    in_primary = out["_in_primary"].fillna(False).astype(bool)
    if primary == "selected":
        out["stage"] = in_primary.map({True: "最终入选（PK 后）", False: "候选池（preselect）"})
    else:
        out["stage"] = in_primary.map({True: "候选池（preselect）", False: "最终入选（PK 后）"})
    out = out.drop(columns=["_in_primary"])
    return _sort(out)


def _sort(df: pd.DataFrame) -> pd.DataFrame:
    """最终入选排前面，其次按 rank 升序。"""
    if "rank" not in df.columns:
        return df.reset_index(drop=True)
    order = df["stage"].map({"最终入选（PK 后）": 0}).fillna(1) if "stage" in df.columns else 0
    rank = pd.to_numeric(df["rank"], errors="coerce").fillna(10**6)
    return df.assign(_o=order, _r=rank).sort_values(["_o", "_r"], kind="stable") \
             .drop(columns=["_o", "_r"]).reset_index(drop=True)


def load_source(key: str) -> dict:
    """读取一个源并整理成前端直接可用的结构。"""
    spec = SOURCES.get(key)
    if spec is None:
        raise KeyError(key)

    df = _read_merged(spec)
    files = [dict(_file_meta(p), role=role) for role, p in spec["files"]]
    trade_date = None
    wanted = [c for c in spec["columns"] if c in df.columns]
    # `stage` 是**算出来的**（按 role 标注每只票走到哪一步），不在声明层的 columns 里；
    # 不补进去的话它会被"只输出声明列"的过滤丢掉 ⇒ 前端「阶段」列空着
    # （2026-10-15 被 test_positive_expectancy_strategies_are_selectable_everywhere 抓到：cn_arm 没 stage）。
    if "stage" in df.columns and "stage" not in wanted:
        wanted.append("stage")
    if not df.empty and "trade_date" in df.columns:
        try:
            trade_date = str(pd.to_datetime(df["trade_date"]).max().date())
        except Exception:
            trade_date = None

    if spec["code_style"] == "ts_code":
        names = _cn_names()
        hk_names: dict[str, str] = {}
    else:
        names = {}
        hk_names = _hk_names([str(c) for c in df.get("stock_code", [])])

    rows: list[dict] = []
    for _, row in df.iterrows():
        code = str(row.get("stock_code", ""))
        if spec["code_style"] == "ts_code":
            code_display = code
            name = _clean_name(row.get("name")) or names.get(code, "")
        else:
            code_display = code.zfill(5)
            name = _clean_name(row.get("stock_name")) or hk_names.get(code_display, "")
        item = {"stock_code": code_display, "stock_name": name}
        for col in wanted:
            if col == "stock_code":
                continue
            val = row.get(col)
            if val is None or (isinstance(val, float) and pd.isna(val)) or pd.isna(val):
                item[col] = None
            elif isinstance(val, bool) or str(val) in ("True", "False"):
                item[col] = str(val) == "True"
            elif isinstance(val, (int, float)):
                item[col] = round(float(val), 4)
            else:
                text = str(val).strip()
                item[col] = None if text.lower() in _JUNK_NAMES else text
        rows.append(item)

    columns = [{"key": "stock_code", "title": "代码"}]
    if "stage" in wanted:
        columns.append({"key": "stage", "title": "阶段"})
    if names or spec["code_style"] == "ts_code":
        columns.append({"key": "stock_name", "title": "名称"})
    for col in wanted:
        if col in ("stock_code", "name"):
            continue
        # 兜底：没注册标题的列直接用列名当标题。**不要**直接下标 `_COLS[col]` ——
        # 那样会在 `selection_service` 的 `except KeyError` 里被吞掉，表现为
        # 「未知数据源 xxx，已回退到默认源」，看起来像数据源没注册（实测踩过：
        # 新增概念手臂时 concept_score 未注册，排查了半天才发现是列标题缺失）。
        columns.append({"key": col, "title": _COLS.get(col, (col, col, False))[1]})

    return {
        "key": key,
        "label": spec["label"],
        "note": spec["note"],
        "files": files,
        "trade_date": trade_date,
        "rows": rows,
        "columns": columns,
        "row_count": len(rows),
        "empty": not rows,
        "supports_shap": spec["supports_shap"],
    }
