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
    # 港股侧（沿用原有中文表头）
    "ranking_score": ("ranking_score", "综合评分", False),
    "signal_tier": ("signal_tier", "信号等级", True),
    "setup_type": ("setup_type", "形态类型", True),
    "risk_adjusted_score": ("risk_adjusted_score", "风险调整得分", False),
    "win_rate": ("win_rate", "胜率(%)", False),
    "backtest_return": ("backtest_return", "回测收益(%)", False),
}

SOURCES: dict[str, dict] = {
    "cn_ensemble": {
        "label": "CN 生产名单（preselect → PK）",
        "note": "流水线 [selection] 阶段产物；preselected 与 selected 合并展示，用「阶段」列区分。",
        "files": [
            ("preselected", RESULTS_CN / "cn_ensemble_preselected.csv"),
            ("selected", RESULTS_CN / "cn_ensemble_selected.csv"),
        ],
        "code_style": "ts_code",
        "columns": [
            "rank", "stock_code", "name", "stage", "model_score", "ensemble_score",
            "lightgbm_score", "transformer_score", "target_weight", "selection_channel",
            "selection_sleeve", "constraint_status", "industry_l1", "last_close",
            "lots_at_target", "tradable_flag",
        ],
        "supports_shap": False,
    },
    "cn_arm": {
        "label": "CN 研究手臂（base_v1）",
        "note": "研究手臂名单，**不替代** preselected→pk 的生产名单；见 P1_25 §20/§21。",
        "files": [("arm", RESULTS_CN / "arm_latest_selection.csv")],
        "code_style": "ts_code",
        "columns": [
            "rank", "stock_code", "board", "industry", "score", "in_portfolio",
            "target_weight", "last_close", "lots_at_capital", "planned_amount",
            "pit_name", "mktcap_rank",
        ],
        "supports_shap": False,
    },
    "cn_arm_concept": {
        "label": "CN 概念/板块手臂（concept_rotation）",
        "note": "板块层（概念动量×资金流−涨停密度）→ 个股层（板块内反转×资金流×流动性）；"
                "独立策略项，**不替代** preselection→pk 的生产名单。",
        "files": [("arm", RESULTS_CN / "arm_concept_selection.csv")],
        "code_style": "ts_code",
        "columns": [
            "rank", "stock_code", "board", "industry", "score", "concept_score", "rev_5d",
            "in_portfolio", "target_weight", "last_close", "lots_at_capital", "planned_amount",
            "pit_name",
        ],
        "supports_shap": False,
    },
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
        out["stage"] = "最终入选（PK 后）" if role == "selected" else "候选池（preselect）"
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
            name = str(row.get("name") or "") or names.get(code, "")
        else:
            code_display = code.zfill(5)
            name = str(row.get("stock_name") or "") or hk_names.get(code_display, "")
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
                item[col] = str(val)
        rows.append(item)

    columns = [{"key": "stock_code", "title": "代码"}]
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
