#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把当日各策略的选股结果导出成**同花顺可直接导入**的自选股文件（多源 + 一键）。

同花顺这边能做到什么、做不到什么
--------------------------------
* ✅ **能**：同花顺 PC「自选股 → 导入自选股」支持 txt/csv/xls，导入时可**新建分组**
  （把分组名写成日期，就等价于"某天某个策略选出来的票"）。
* ❌ **不能**：同花顺**没有公开的写接口** —— 自选股存在账号云端，第三方无法写入。
  所以"导入"这一步必须由人点（或用客户端自动化，那是灰色手段，不做）。
* ⚡ **最接近"一键"的做法**：**复制到剪贴板 → 在同花顺自选股里 Ctrl+V**。
  本脚本同时输出一份纯代码文本，前端有「一键复制」按钮，配合粘贴就是两步。

五个源（2026-10-09 实测规模）
----------------------------
  holdings     3 只   生产名单里**真的给了目标权重**的（PK 之后要买的）
  preselected  8 只   生产链路的**候选池**（PK 的输入）
  arm         10 只   研究手臂（5 只持仓 + 5 只候选）
  reference   10 只   **参考账户**（模拟盘绑定的那条规则）的当前持仓 —— 第三十七轮新增；
                      "拿模拟盘做参考"时真正要导入同花顺的就是这一份
  all         24 只   上面四者的**去重并集**（一键导入用）

用法:
  uv run python scripts/export_ths_watchlist.py                     # 全部源
  uv run python scripts/export_ths_watchlist.py --sources holdings,arm
  uv run python scripts/export_ths_watchlist.py --date 2026-10-08
输出（以 holdings 为例，其余源同名前缀）:
  output/results_cn/ths_watchlist/<date>_holdings_code.txt        纯 6 位代码
  output/results_cn/ths_watchlist/<date>_holdings_code_gbk.txt    同上 GBK（老版本只认 ANSI）
  output/results_cn/ths_watchlist/<date>_holdings_code_name.txt   代码 + 名称（Tab 分隔）
  output/results_cn/ths_watchlist/<date>_holdings.csv             给人看（含权重/行业）
  output/results_cn/ths_watchlist/latest_holdings_code.txt        最新一天副本（路径固定）
  output/results_cn/ths_watchlist/<date>_sources.csv              合并视图：代码,名称,出现在哪些源
  output/results_cn/ths_watchlist/<date>_manifest.json            各源代码 + 导入步骤
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "output/results_cn"
HISTORY = RESULTS / "paper_selection_history.csv"
BASIC = ROOT / "assets/data/meta/cn_stock_basic.parquet"
OUT_DIR = RESULTS / "ths_watchlist"
SUFFIXES = (".SH", ".SZ", ".BJ")   # 同花顺只认 6 位数字，必须剥掉


def plain_code(code: str) -> str:
    text = str(code).strip().upper()
    for suffix in SUFFIXES:
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def _names_industries() -> tuple[dict, dict]:
    names, industries = {}, {}
    if BASIC.exists():
        basic = pd.read_parquet(BASIC, columns=["ts_code", "name", "industry"]).drop_duplicates("ts_code")
        for code, name, industry in zip(basic["ts_code"].astype(str), basic["name"], basic["industry"]):
            names[code] = "" if pd.isna(name) else str(name)
            industries[code] = "" if pd.isna(industry) else str(industry)
    return names, industries


def _frame(rows: list[dict], date: str, source: str, names: dict, industries: dict) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["trade_date", "stock_code", "name", "weight", "industry", "source"])
    out = pd.DataFrame(rows)
    out["trade_date"] = date
    out["stock_code"] = out["stock_code"].astype(str)
    out["name"] = out["stock_code"].map(lambda c: names.get(c, ""))
    out["industry"] = out["stock_code"].map(lambda c: industries.get(c, ""))
    out["source"] = source
    return out[["trade_date", "stock_code", "name", "weight", "industry", "source"]]


def load_sources(date: str) -> dict[str, pd.DataFrame]:
    """五个源（holdings/preselected/arm/reference/all）→ DataFrame（列：trade_date/stock_code/name/weight/industry/source）。"""
    names, industries = _names_industries()
    frames: dict[str, pd.DataFrame] = {}

    # ① holdings：生产名单里**有权重**的（读 paper_selection_history，它已过滤 weight>0）
    if HISTORY.exists():
        hist = pd.read_csv(HISTORY)
        day = hist[hist["trade_date"].astype(str) == date]
        frames["holdings"] = _frame(
            [{"stock_code": c, "weight": w} for c, w in zip(day["stock_code"], day["target_weight"])],
            date, "holdings", names, industries)

    # ② preselected：生产候选池（当日文件；历史回看用 selection_snapshots）
    pre = RESULTS / "cn_ensemble_preselected.csv"
    if pre.exists():
        d = pd.read_csv(pre)
        d = d[d["trade_date"].astype(str) == date]
        weight = d["trade_weight"] if "trade_weight" in d.columns else d.get("target_weight")
        frames["preselected"] = _frame(
            [{"stock_code": c, "weight": (None if weight is None else w)}
             for c, w in zip(d["stock_code"], weight if weight is not None else [None] * len(d))],
            date, "preselected", names, industries)

    # ③ arm：研究手臂（含"是否在持仓里"的标记 → 这里只用权重区分）
    arm = RESULTS / "arm_latest_selection.csv"
    if arm.exists():
        d = pd.read_csv(arm)
        d = d[d["trade_date"].astype(str) == date]
        frames["arm"] = _frame(
            [{"stock_code": c, "weight": w} for c, w in zip(d["stock_code"], d["target_weight"])],
            date, "arm", names, industries)

    # ④ reference：参考账户（模拟盘绑定的那条规则）的**当前持仓** —— 第三十七轮新增。
    #    这就是"拿模拟盘做参考"时真正要导入同花顺的那份清单。
    ref_pos = ROOT / "output/paper_trading/account_reference/positions.csv"
    ref_var = ROOT / "output/paper_trading/account_reference/variant.json"
    if ref_pos.exists():
        pos = pd.read_csv(ref_pos)
        if not pos.empty and "asof_date" in pos.columns:
            latest = pos["asof_date"].astype(str).max()
            day = pos[pos["asof_date"].astype(str) == latest]
            label = ""
            if ref_var.exists():
                try:
                    label = json.loads(ref_var.read_text(encoding="utf-8")).get("label", "")
                except Exception:
                    label = ""
            frames["reference"] = _frame(
                [{"stock_code": c, "weight": None} for c in day["stock_code"]],
                date, "reference", names, industries)
            if not frames["reference"].empty:
                frames["reference"].attrs["note"] = f"参考账户（{label}）as-of {latest}"

    # ⑤ all：并集（一键导入）
    parts = [f for f in frames.values() if not f.empty]
    if parts:
        merged = pd.concat(parts, ignore_index=True)
        dedup = (merged.sort_values("weight", ascending=False, na_position="last")
                 .drop_duplicates("stock_code", keep="first").copy())
        dedup["source"] = "all"
        frames["all"] = dedup
    return frames


def write_outputs(frames: dict[str, pd.DataFrame], date: str, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                      "trade_date": date, "sources": {}}

    for source, frame in frames.items():
        if frame.empty:
            manifest["sources"][source] = {"n": 0}
            continue
        frame = frame.copy()
        frame["code6"] = frame["stock_code"].map(plain_code)
        codes = "\n".join(frame["code6"].tolist()) + "\n"
        code_name = "\n".join(f"{c}\t{n}" for c, n in zip(frame["code6"], frame["name"])) + "\n"

        (out_dir / f"{date}_{source}_code.txt").write_text(codes, encoding="utf-8")
        # 同花顺 PC 老版本只认 ANSI/GBK，同时给一份避免"文件格式不支持"
        (out_dir / f"{date}_{source}_code_gbk.txt").write_bytes(codes.encode("gbk", errors="replace"))
        (out_dir / f"{date}_{source}_code_name.txt").write_text(code_name, encoding="utf-8")
        (out_dir / f"latest_{source}_code.txt").write_text(codes, encoding="utf-8")
        (out_dir / f"latest_{source}_code_gbk.txt").write_bytes(codes.encode("gbk", errors="replace"))
        frame[["code6", "name", "weight", "industry"]].to_csv(out_dir / f"{date}_{source}.csv", index=False)

        manifest["sources"][source] = {
            "n": int(len(frame)), "codes": frame["code6"].tolist(), "names": frame["name"].tolist(),
            "files": sorted(p.name for p in out_dir.glob(f"*{source}*")),
        }

    # 合并视图：一只票一行，标出它出现在哪些源（给人核对，不给同花顺）
    rows = []
    for source, frame in frames.items():
        if source == "all" or frame.empty:
            continue
        for code, name in zip(frame["stock_code"], frame["name"]):
            rows.append({"stock_code": code, "name": name, "in_source": source})
    if rows:
        wide = (pd.DataFrame(rows).assign(flag=1)
                .pivot_table(index=["stock_code", "name"], columns="in_source", values="flag", aggfunc="max")
                .fillna(0).astype(int).reset_index())
        wide["code6"] = wide["stock_code"].map(plain_code)
        wide["n_sources"] = wide[[c for c in wide.columns if c not in ("stock_code", "name", "code6")]].sum(axis=1)
        wide.sort_values("n_sources", ascending=False).to_csv(out_dir / f"{date}_sources.csv", index=False)

    manifest["how_to_import"] = [
        "同花顺 PC → 自选股 → 右键 → 导入自选股 → 选文件（乱码就换 _code_gbk.txt）",
        "导入时新建分组，分组名建议 <日期>_<策略>，如 20261008_arm",
        "最快的方式：网页「选股结果 → 导出到同花顺」点「一键复制」，然后在同花顺自选股里 Ctrl+V 粘贴",
        "同花顺不开放写接口，这一步无法全自动；要全自动请用飞书/企微/邮件推送",
    ]
    (out_dir / f"{date}_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description="导出同花顺可导入的自选股文件（多源）")
    ap.add_argument("--date", default="", help="留空=最新交易日")
    ap.add_argument("--sources", default="holdings,preselected,arm,reference,all",
                    help="逗号分隔：holdings,preselected,arm,reference,all（reference=参考账户持仓）")
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    args = ap.parse_args()

    if HISTORY.exists():
        hist = pd.read_csv(HISTORY, usecols=["trade_date"])
        date = args.date or str(hist["trade_date"].max())
    else:
        date = args.date or datetime.now().strftime("%Y-%m-%d")

    want = [s.strip() for s in args.sources.split(",") if s.strip()]
    frames_all = load_sources(date)
    frames = {k: v for k, v in frames_all.items() if k in want}
    if not any(not v.empty for v in frames.values()):
        raise SystemExit(f"{date} 没有任何源的选股产物（可用的最近日期见 paper_selection_history.csv）")

    manifest = write_outputs(frames, date, Path(args.out_dir))
    print(f"[ths] {date}｜" + "｜".join(
        f"{s} {manifest['sources'].get(s, {}).get('n', 0)} 只" for s in want), flush=True)
    for source in want:
        info = manifest["sources"].get(source) or {}
        if info.get("codes"):
            print(f"[ths]   {source:12} {' '.join(info['codes'])}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
