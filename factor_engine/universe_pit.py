# -*- coding: utf-8 -*-
"""PIT（point-in-time）股票池状态：按**当时**的证券名称判定 ST / 退市风险。

为什么需要
----------
原策略六层过滤第 2 条是「ST 与退市风险股：保命第一条件，不解释」。仓库此前只能用
`cn_stock_basic.name`（**当前**名称）来判定，等于用未来的信息过滤历史 —— 这正是结论台账
B13 那条边界：「ST 过滤是当前名称（非 PIT）→ 该名单只适合今天选票，不能当历史结论用」。

数据来源
--------
`assets/data/meta/cn_stock_namechange.parquet`（由 `scripts/backfill_cn_namechange.py` 逐只采集）：

    ts_code | name | start_date | end_date | ann_date | change_reason | is_st | is_delist_risk | is_risk

区间语义：`start_date` 是该名称**生效**日，`end_date` 是失效日；`end_date` 为空 ⇒ 至今有效。
这是 Tushare `namechange` 的原生契约（本机实测 `000004.SZ` 返回 17 条，覆盖 1998 至今，
含 `ST国华 → *ST国华 → 国华退` 的完整链路）。

口径选择
--------
* 默认用 **`start_date`（生效日）** 判定，而不是 `ann_date`（公告日）。
  理由：生效日更保守 —— 只在"市场上真的挂牌成 ST"之后才剔除，不提前使用公告信息，
  因此对回测不构成前视；`anchor="ann"` 是更激进的风控口径（公告即剔除）。
* 无任何名称记录、或命中记录的 `end_date` 已过期（区间表有缺口）→ `covered=False`
  （默认同时 `is_risk=False`，即"不剔除"），并单独暴露覆盖率供核对，绝不静默当作非 ST。

已知局限
--------
1. 中继 `namechange` **不支持按日期过滤**（带 `start_date`/`end_date` 参数实测 HTTP 400），
   只能逐 code 全量拉；频控下约 88% 一次跑完，其余需增量补拉（脚本可续跑）。
2. 它回答的是"当时叫什么名字"，**不覆盖**停牌 / 涨跌停 / 流动性等交易状态。
3. `退` 用于识别"退市整理期/退市"，与仓库既有 `select_latest_via_arm.py` 的口径一致。
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
NAME_CHANGE = ROOT / "assets/data/meta/cn_stock_namechange.parquet"

REQUIRED_COLS = ["ts_code", "name", "start_date", "end_date", "ann_date", "change_reason"]
FLAG_COLS = ["is_st", "is_delist_risk", "is_risk"]


def load_namechange(path: str | Path = NAME_CHANGE) -> pd.DataFrame:
    """读取区间表；缺文件时返回空表（调用方按"无覆盖"处理）。"""
    path = Path(path)
    if not path.exists():
        return pd.DataFrame(columns=REQUIRED_COLS + FLAG_COLS)
    df = pd.read_parquet(path)
    for col in ("start_date", "end_date", "ann_date"):
        df[col] = pd.to_datetime(df[col], errors="coerce")
    df["ts_code"] = df["ts_code"].astype(str)
    df["name"] = df["name"].astype(str)
    return df.sort_values(["ts_code", "start_date"]).reset_index(drop=True)


def _markers(marker_mode: str) -> tuple[str, ...]:
    if marker_mode == "st_only":
        return ("ST",)
    if marker_mode == "delist_only":
        return ("退",)
    return ("ST", "退")


def is_risk_name(name, marker_mode: str = "st_and_delist") -> bool:
    """名称 → 是否属「ST / 退市风险」（与既有 `--st-mode current` 的口径一致）。"""
    text = "" if name is None else str(name)
    if text == "" or text.lower() == "nan":
        return False
    upper = text.upper()
    for marker in _markers(marker_mode):
        if marker == "ST":
            if "ST" in upper:
                return True
        elif marker in text:
            return True
    return False


def _empty_result(codes: Sequence[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "pit_name": pd.Series([None] * len(codes), dtype=object),
            "is_st": np.zeros(len(codes), dtype=bool),
            "is_delist": np.zeros(len(codes), dtype=bool),
            "is_risk": np.zeros(len(codes), dtype=bool),
            "covered": np.zeros(len(codes), dtype=bool),
            "stale": np.zeros(len(codes), dtype=bool),
            "reason": [""] * len(codes),
        },
        index=pd.Index([str(c) for c in codes], name="ts_code"),
    )


def risk_asof(
    nc: pd.DataFrame,
    codes: Sequence[str],
    as_of,
    *,
    marker_mode: str = "st_and_delist",
    anchor: str = "start",
    stale_ok: bool = False,
) -> pd.DataFrame:
    """单一 as-of 日期的 PIT 判定（区间表 → 当时名称 → 是否剔除）。

    返回 index=`codes` 的 DataFrame：
      pit_name  当时该股使用的名称（无记录 → None）
      is_risk   是否属「ST / 退市风险」（= 应当剔除）
      covered   是否有可用记录（False ⇒ 判定不可信，调用方须标注覆盖率）
      stale     命中的记录 `end_date` 已过期（区间表在这一段有缺口）
      reason    命中的 `change_reason`（供前端讲清原因）
    """
    codes = [str(c) for c in codes]
    out = _empty_result(codes)
    if nc is None or len(nc) == 0 or len(codes) == 0:
        return out

    day = pd.Timestamp(as_of)
    anchor_col = "start_date" if anchor == "start" else "ann_date"
    sub = nc[nc[anchor_col].notna() & nc["ts_code"].isin(out.index)]
    eligible = sub[sub[anchor_col] <= day]
    if len(eligible) == 0:
        return out
    # 必须用 drop_duplicates(keep="last") 取"最后一行"：groupby.last() 会跳过 NaN，
    # 会把 end_date 取成"最后一个非空值"（而 name 取的是最后一行）→ 两者错位，
    # 例：000001.SZ 会得到 name=平安银行 + end_date=2012-08-01，被误判成 stale。
    picked = (
        eligible.sort_values(anchor_col)
        .drop_duplicates("ts_code", keep="last")
        .set_index("ts_code")
    )

    hit = out.index.isin(picked.index)
    hit_codes = out.index[hit]
    names = picked.loc[hit_codes, "name"].astype(str)
    out.loc[hit_codes, "pit_name"] = names.to_numpy()
    out.loc[hit_codes, "reason"] = picked.loc[hit_codes, "change_reason"].astype(str).to_numpy()
    out.loc[hit_codes, "is_st"] = [("ST" in str(n).upper()) for n in names]
    out.loc[hit_codes, "is_delist"] = [("退" in str(n)) for n in names]
    out.loc[hit_codes, "is_risk"] = [
        is_risk_name(n, marker_mode) for n in names
    ]

    stale = picked.loc[hit_codes, "end_date"].notna() & (picked.loc[hit_codes, "end_date"] < day)
    out.loc[hit_codes, "stale"] = stale.to_numpy()
    out.loc[hit_codes, "covered"] = (~stale).to_numpy() if not stale_ok else True
    return out


def name_asof(nc: pd.DataFrame, codes: Iterable[str], as_of) -> pd.Series:
    """只要「当时叫什么名字」。"""
    return risk_asof(nc, list(codes), as_of)["pit_name"]


def risk_for_frame(
    nc: pd.DataFrame,
    frame: pd.DataFrame,
    *,
    code_col: str = "stock_code",
    date_col: str = "trade_date",
    marker_mode: str = "st_and_delist",
    anchor: str = "start",
    stale_ok: bool = False,
) -> pd.DataFrame:
    """批量 PIT 判定：`frame` 每行是 (code, date)，逐个还原当时名称。

    用于把 PIT ST 过滤接进回测域（每月/每日决策点），避免逐点 Python 循环。
    返回与 `frame` **同索引**的三列：`pit_name` / `is_risk` / `covered`。
    """
    if nc is None or len(nc) == 0 or len(frame) == 0:
        return pd.DataFrame(
            {
                "pit_name": pd.Series([None] * len(frame), dtype=object),
                "is_st": np.zeros(len(frame), dtype=bool),
                "is_delist": np.zeros(len(frame), dtype=bool),
                "is_risk": np.zeros(len(frame), dtype=bool),
                "covered": np.zeros(len(frame), dtype=bool),
            },
            index=frame.index,
        )
    anchor_col = "start_date" if anchor == "start" else "ann_date"
    right = nc.loc[nc[anchor_col].notna(), ["ts_code", anchor_col, "end_date", "name", "change_reason"]].copy()
    right = right.sort_values(anchor_col)

    left = pd.DataFrame(
        {
            "_code": frame[code_col].astype(str).to_numpy(),
            "_day": pd.to_datetime(frame[date_col]).to_numpy(),
            "_pos": np.arange(len(frame)),
        }
    ).sort_values("_day")

    merged = pd.merge_asof(
        left,
        right,
        left_on="_day",
        right_on=anchor_col,
        left_by="_code",
        right_by="ts_code",
        direction="backward",
        allow_exact_matches=True,
    ).sort_values("_pos")

    valid = merged["name"].notna().to_numpy()
    stale = (
        merged["end_date"].notna().to_numpy()
        & (merged["end_date"] < merged["_day"]).to_numpy()
    )
    covered = valid & (~stale | stale_ok)
    risk = np.array(
        [is_risk_name(n, marker_mode) if v else False for n, v in zip(merged["name"], valid)]
    )
    names = np.where(valid, merged["name"].astype(str).to_numpy(), None)

    is_st = np.array([("ST" in str(n).upper()) if v else False for n, v in zip(merged["name"], valid)])
    is_delist = np.array([("退" in str(n)) if v else False for n, v in zip(merged["name"], valid)])
    result = pd.DataFrame(
        {"pit_name": names, "is_st": is_st, "is_delist": is_delist,
         "is_risk": risk, "covered": covered}
    )
    result.index = frame.index
    return result
