#!/usr/bin/env python3
"""概念成分校准：宽概念降噪 + LLM 置信度过滤 + 来源分层。

问题（2026-10-24）：iFinD 的「具身智能」1,921 只、「机器人概念」1,229 只把中国平安/顺丰
这类完全无关的票也包进来，导致概念热度/选股被污染。

规则：
  1. 供应商概念按规模分级：≤600 只 = 窄概念，全部保留；
      >600 只 = 宽概念，只保留「同时属于本 family 任一窄概念」的成员（核心圈）。
  2. LLM 标签：confidence ≥ 0.6 才保留；若概念是宽概念，还要求同时是窄概念成员。
  3. 非主题标签（基金重仓/融资融券/央企50 等）直接剔除。
  4. 输出带 tier（core/expanded）与 reason，便于审计。

产物：
  assets/data/derived/cn_concept_members_calibrated.parquet
  output/research/p2_77/concept_calibration.json
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets/data/derived/cn_concept_members_calibrated.parquet"
REPORT = ROOT / "output/research/p2_77/concept_calibration.json"
BANNED = {"ST板块", "摘帽概念", "准ST股", "外资背景", "本月解禁", "保险重仓", "信托重仓",
          "券商重仓", "2026三季报预增", "2026中报预增", "共同富裕示范区", "同花顺果指数",
          "同花顺漂亮100", "净水概念", "摘帽", "国企改革", "专精特新", "恒大概念",
          "股权转让(并购重组)", "举牌", "定增", "回购", "增持", "员工持股"}
BROAD = 600
INVALID = 1500   # 成员数超过这个值，视为 iFinD 查询退化为“全市场”，不可信
NON_THEME = ("基金重仓", "社保重仓", "QFII", "融资融券", "深股通", "沪股通", "股权激励", "MSCI",
             "标普", "富时", "转债标的", "预盈预增", "预降", "参股金融", "参股券商", "破净股",
             "次新股", "超大盘", "大盘", "中盘", "小盘", "央企50", "国企50", "上证50", "沪深300",
             "中证100", "行业龙头", "专精特新", "业绩")
TECH_KEYWORDS = ("AI", "人工智能", "华为", "芯片", "半导体", "机器人", "CPO", "光模块", "算力",
                 "存储", "软件", "云", "数据", "通信", "5G", "6G", "消费电子", "智能", "元宇宙",
                 "虚拟现实", "MR", "AR", "VR", "小米", "特斯拉", "苹果", "比亚迪", "宁德",
                 "英伟达", "微软", "谷歌", "Meta", "鸿蒙", "欧拉", "昇腾")
NON_TECH_INDUSTRIES = {"银行", "保险", "证券", "食品", "白酒", "饮料", "纺织", "服装", "房地产",
                       "建筑", "煤炭", "钢铁", "公用事业", "公路", "铁路", "港口", "机场", "商贸",
                       "零售", "旅游", "酒店", "餐饮", "农业", "养殖", "种植", "家居用品"}
CORE_TECH = {"专用机械", "电气设备", "电子", "半导体", "元器件", "汽车配件", "机械基件",
             "仪器仪表", "通信设备", "软件服务", "电器仪表", "IT设备", "化工原料", "小金属"}
FAMILY = {"人形机器人": "机器人", "机器人概念": "机器人", "具身智能": "机器人", "智能机器": "机器人",
          "CPO": "光通信", "光模块": "光通信"}


def main() -> int:
    frames = []
    si = pd.read_parquet(ROOT / "assets/data/meta/cn_board_membership.parquet")
    si = si[si.taxonomy == "sina_concept"][["board", "code"]].rename(columns={"board": "concept"})
    si["stock_code"] = si["code"].map(lambda x: x + (".SH" if str(x).startswith(("6", "9")) else ".SZ"))
    si["source"], si["confidence"] = "sina", 1.0
    frames.append(si[["concept", "stock_code", "source", "confidence"]])

    ifd = pd.read_parquet(ROOT / "assets/data/derived/cn_concept_members_ifind.parquet")
    ifd = ifd[ifd.snapshot_date == ifd.snapshot_date.max()][["concept", "stock_code"]]
    ifd["source"], ifd["confidence"] = "ifind", 0.9
    frames.append(ifd)

    llm_path = ROOT / "assets/data/derived/cn_concept_members_llm.parquet"
    if llm_path.exists():
        llm = pd.read_parquet(llm_path)[["concept", "stock_code", "confidence"]]
        llm["source"] = "llm"
        frames.append(llm)

    allm = pd.concat(frames, ignore_index=True)
    allm = allm[~allm["concept"].isin(BANNED)]
    # 指数型/榜单型标签（如 中国AI 50、同花顺漂亮100）不是题材
    allm = allm[~allm["concept"].astype(str).str.contains(r"(?:\d{2,3}$|指数|成份|成分|榜单|精选$)", regex=True, na=False)]
    allm = allm[~allm["concept"].astype(str).str.contains("|".join(NON_THEME), na=False)].drop_duplicates(
        ["concept", "stock_code"], keep="first")

    sizes = allm.groupby("concept")["stock_code"].nunique()
    # 泛 AI 大词（人工智能/AI应用…）成员数 >800 时等同于“所有科技股”，直接剔除
    generic = {"人工智能", "AI应用", "AI视频", "AI语料", "AI智能体", "DeepSeek概念"}
    allm = allm[~((allm["concept"].isin(generic)) & (allm["concept"].map(sizes) > 800))]
    sizes = allm.groupby("concept")["stock_code"].nunique()
    broad = set(sizes[sizes > BROAD].index)
    family = allm["concept"].map(lambda c: FAMILY.get(c, c))
    narrow = allm[~allm["concept"].isin(broad)]
    narrow_pairs = set(zip(narrow["concept"], narrow["stock_code"]))
    narrow_stocks_by_family = {}
    for c, s in zip(narrow["concept"], narrow["stock_code"]):
        narrow_stocks_by_family.setdefault(FAMILY.get(c, c), set()).add(s)

    basic = pd.read_parquet(ROOT / "assets/data/meta/cn_stock_basic.parquet", columns=["ts_code", "industry"])
    ind_map = dict(zip(basic["ts_code"], basic["industry"].fillna("")))

    rows, dropped = [], []
    for r in allm.itertuples(index=False):
        con, code = r.concept, r.stock_code
        fam = FAMILY.get(con, con)
        if con in broad:
            fam_stocks = narrow_stocks_by_family.get(fam, set())
            if not fam_stocks:
                if sizes.get(con, 0) > INVALID:
                    ok, reason = False, "invalid_too_broad"
                else:
                    # 宽概念但没有同族窄概念可参照 → 保留原样（否则整个主题会被清空）
                    ok, reason = True, "broad_no_narrow"
            else:
                ok = code in fam_stocks
                reason = "broad∩narrow" if ok else "broad_dropped"
            # 宽概念的第二条通道：LLM 高置信 + 核心行业（挡住银行/保险/食品等蹭概念）
            if not ok and r.source == "llm" and float(r.confidence or 0) >= 0.8 and ind_map.get(code) in CORE_TECH:
                ok, reason = True, "broad_llm_core_industry"
        else:
            ok = True
            reason = "narrow"
        if r.source == "llm" and float(r.confidence or 0) < 0.6:
            ok, reason = False, "llm_low_conf"
        _ind = str(ind_map.get(code, ""))
        if ok and any(k in str(con) for k in TECH_KEYWORDS) and (
                _ind in NON_TECH_INDUSTRIES or any(b in _ind for b in
                ("银行", "保险", "证券", "食品", "饮料", "乳", "酒", "纺织", "服装", "地产", "建筑",
                 "煤炭", "钢铁", "公路", "铁路", "港口", "机场", "旅游", "酒店", "餐饮", "商贸",
                 "零售", "农业", "种植", "养殖", "饲料", "林业", "渔业", "物流", "仓储", "运输",
                 "航运", "航空", "快递"))):
            ok, reason = False, "tech_theme_non_tech_industry"
        if ok:
            rows.append({"concept": con, "stock_code": code, "source": r.source,
                         "confidence": float(r.confidence or 0), "tier": "core" if con not in broad else "expanded",
                         "reason": reason})
        else:
            dropped.append({"concept": con, "stock_code": code, "source": r.source, "reason": reason})

    out = pd.DataFrame(rows).drop_duplicates(["concept", "stock_code"], keep="first")
    out.to_parquet(OUT, index=False)

    before = sizes.to_dict()
    after = out.groupby("concept")["stock_code"].nunique().to_dict()
    report = {
        "broad_threshold": BROAD,
        "broad_concepts": {c: {"before": int(before[c]), "after": int(after.get(c, 0))} for c in sorted(broad)},
        "total": {"before": int(len(allm)), "after": int(len(out)), "dropped": int(len(dropped))},
        "dropped_examples": dropped[:20],
        "coverage_stocks": int(out["stock_code"].nunique()),
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("broad_concepts", "total", "coverage_stocks")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
