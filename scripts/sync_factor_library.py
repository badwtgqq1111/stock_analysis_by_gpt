#!/usr/bin/env python3
"""因子库注册表 `output/factor_library/factors.json`（P2_42 §4.2）。

为什么要有它（用户 2026-10-11："落一个 stock_analysis_by_gpt/docs/todo" + "继续"）：
现在"因子"散在四个地方，谁都没法一眼回答"我手里到底有多少因子、哪些过了门槛、哪些被谁替代了"：

  * `output/research/factor_ic_protocol/factor_ic_registry.csv`  —— 1,366 个候选因子（含准入/剔除判决）
  * `output/research/factor_ic_protocol/factor_ic_admitted.json` —— 本次准入清单（A/B 两档 + 判据 + 与上次的差额）
  * `output/factor_mining_sessions/*.json`                        —— LLM 会话里提议的因子（表达式 + 经济解释）
  * `output/research/llm_factor_mining_*/round_*/proposals.json`  —— LLM 逐轮提议（同上，带轮次）
  * `config/strategies.json`                                      —— 族 A 的**条件**（事件规则 / 公式打分 / 手臂规则）

这个脚本把上面五处合成**一张表**，两种 `kind`：
  * `kind="factor"`    —— 族 B 因子流水线的因子（有 IC/ICIR/NW t/BH-FDR/簇冗余）
  * `kind="condition"` —— 族 A 规则类策略的触发条件（有 stage，与策略池同一份登记）
这样"因子挖掘"页与"投研执行台"页就有同一个数据源了（本轮把两个页面合并，见 README）。

用法：
    uv run python scripts/sync_factor_library.py                # 写 output/factor_library/{factors.json,factors.md}
    uv run python scripts/sync_factor_library.py --print        # 顺带打印摘要（每日自动化用）

判据（都从产物里读，不重算、不猜）：
    准入A/准入B          → status=admitted（grade=A/B）
    剔除:*              → status=rejected（reason=原 verdict 文案）
    质量列(不参与准入)    → status=excluded（它们不是因子，是"某字段缺失"标记）
    LLM 会话/轮次里的提议 → status=proposed（若同时在准入清单里，升级为 admitted）
    族 A 的条件          → status=in_use/candidate/retired（跟随策略池 stage）
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_CSV = ROOT / "output/research/factor_ic_protocol/factor_ic_registry.csv"
ADMITTED_JSON = ROOT / "output/research/factor_ic_protocol/factor_ic_admitted.json"
SESSIONS_DIR = ROOT / "output/factor_mining_sessions"
MINING_GLOB = "output/research/llm_factor_mining_*/round_*/proposals.json"
CONFIG = ROOT / "config/strategies.json"
GLOSSARY = ROOT / "config/factor_glossary.json"
OUT_DIR = ROOT / "output/factor_library"

STATUS_ORDER = ["admitted", "in_use", "proposed", "candidate", "excluded", "rejected", "retired"]


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _num(value, cast=float):
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


def _rel(path: Path) -> str:
    text = str(path)
    return text[len(str(ROOT)) + 1:] if text.startswith(str(ROOT) + "/") else text


GLOSSARY_CACHE: dict | None = None


def glossary() -> dict:
    global GLOSSARY_CACHE
    if GLOSSARY_CACHE is None:
        GLOSSARY_CACHE = _read_json(GLOSSARY, {}) or {}
    return GLOSSARY_CACHE


def describe(name: str, family: str, reason: "str | None" = None, llm_reason: bool = False) -> dict:
    """给一个因子拼『功能描述 + 分类』。

    三条来源，按可信度排序（source 字段如实记录，不混）：
      1. `overrides`（人工写死，最准）
      2. LLM 会话里的 `reason`（经济解释，只有 LLM 因子有）
      3. **规则解码**：家族口径 + 名字 token + 后缀（掩码/清洗/行业内分位）
    解码不出来就显示"未识别"，绝不编公式 —— 家族级只给口径与出处，具体公式指向公开文献。
    """
    data = glossary()
    families = data.get("families", {})
    info = families.get(family, {})
    parts = []
    source = "name_decoder"

    override = (data.get("overrides", {}) or {}).get(name)
    if isinstance(override, dict) and override.get("text"):
        parts.append(override["text"])
        source = override.get("source", "curated")
    elif reason and llm_reason:
        # 注意：只有 LLM 会话里的 `reason` 才是"经济解释"。
        # 协议层因子的 `note` 是**剔除原因**（如「质量列(不参与准入)」「强度不足」），
        # 把它当解释会得到"LLM 提议：质量列(...)"这种鬼话（实测踩过）。
        parts.append(f"LLM 提议：{reason}")
        source = "llm_reason"
    elif reason:
        parts.append(f"判决：{reason}")

    if info:
        parts.append(f"口径：{info.get('label', family)}（{info.get('meaning', '')}；出处 {info.get('source', '—')}）")

    tokens = [rule["label"] for rule in data.get("tokens", []) if re.search(rule["match"], name)]
    if tokens:
        parts.append("读作：" + " + ".join(dict.fromkeys(tokens)))

    suffix = [text for token, text in (data.get("suffixes", {}) or {}).items() if name.endswith(token)]
    if suffix:
        parts.append("".join(suffix))

    if not parts:
        parts.append("未识别（请在 config/factor_glossary.json 里补规则）")
        source = "missing"
    return {"text": "；".join(parts), "source": source, "family": family}


def categorize(name: str, family: str) -> dict:
    """分类：先按家族映射，家族映射为 other/llm 时再用名字 token 兜底判一次。"""
    data = glossary()
    family_map = data.get("family_category", {})
    categories = data.get("categories", {})
    key = family_map.get(family, "other")
    if key in ("other", None):
        for rule in data.get("token_category", []):
            if re.search(rule["match"], name):
                key = rule["category"]
                break
    meta = categories.get(key, {})
    return {"key": key, "label": meta.get("label", key or "其它"), "core": meta.get("core", "")}


def load_admitted() -> dict:
    payload = _read_json(ADMITTED_JSON, {})
    admitted = set(payload.get("admitted_A", [])) | set(payload.get("admitted_B", []))
    grades = {**{name: "A" for name in payload.get("admitted_A", [])},
              **{name: "B" for name in payload.get("admitted_B", [])}}
    return {"admitted": admitted, "grades": grades, "payload": payload}


# ─────────────────────────── 1) 协议层：1,366 个候选因子 ───────────────────────────
def load_protocol_factors(admitted: dict) -> list[dict]:
    if not REGISTRY_CSV.exists():
        return []
    factors: list[dict] = []
    with REGISTRY_CSV.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            name = (row.get("") or "").strip()
            if not name:
                continue
            verdict = (row.get("verdict") or "").strip()
            if verdict.startswith("准入"):
                status, reason = "admitted", None
            elif verdict.startswith("剔除"):
                status, reason = "rejected", verdict.replace("剔除:", "")
            else:
                status, reason = "excluded", verdict
            # 与准入清单交叉核对：CSV 说准入但 JSON 清单没有 ⇒ 以 JSON 清单为准（清单是准入口径的唯一来源）
            if status == "admitted" and admitted["admitted"] and name not in admitted["admitted"]:
                status, reason = "rejected", "不在本次准入清单里（清单是准入口径的唯一来源）"
            grade = admitted["grades"].get(name)
            factors.append({
                "id": name,
                "kind": "factor",
                "source": "factor_ic_protocol",
                "family": row.get("family") or "",
                "status": status,
                "grade": grade,
                "expression": None,
                "note": reason,
                "metrics": {
                    "ic_mean": _num(row.get("ic_mean")),
                    "icir": _num(row.get("icir")),
                    "nw_t": _num(row.get("t_nw")),
                    "p_nw": _num(row.get("p_nw")),
                    "months": _num(row.get("months"), int),
                    "positive_share": _num(row.get("positive_share")),
                    "fdr_pass": (row.get("fdr_significant") == "True"),
                    "hlz_pass": (row.get("hlz_significant") == "True"),
                    "sign_stable": (row.get("sign_stable") == "True"),
                    "coverage": _num(row.get("coverage")),
                    "quality_column": (row.get("quality_column") == "True"),
                },
                "cluster": {
                    "id": _num(row.get("cluster"), int),
                    "size": _num(row.get("cluster_size"), int),
                    "representative": (row.get("is_representative") == "True"),
                },
                "first_seen": None,
                "last_seen": None,
                "evidence": [_rel(REGISTRY_CSV), _rel(ADMITTED_JSON)],
            })
    return factors


# ─────────────────────────── 2) LLM 提议的因子（会话 + 轮次） ───────────────────────────
def load_llm_factors(admitted: dict) -> list[dict]:
    found: dict[str, dict] = {}

    def put(name: str, source: str, record: dict, evidence: str) -> None:
        name = (name or "").strip()
        if not name:
            return
        item = found.setdefault(name, {
            "id": name, "kind": "factor", "source": source, "family": "llm",
            "status": "admitted" if name in admitted["admitted"] else "proposed",
            "grade": admitted["grades"].get(name), "expression": None, "note": None,
            "metrics": {}, "cluster": {}, "first_seen": None, "last_seen": None,
            "evidence": [], "sources": [],
        })
        if source not in item["sources"]:
            item["sources"].append(source)
        item["expression"] = item["expression"] or record.get("expression")
        item["note"] = item["note"] or record.get("reason")
        item["metrics"].update({key: value for key, value in {
            "ic_mean": record.get("ic_mean"), "icir": record.get("icir"),
            "nw_t": record.get("nw_t"), "max_corr": record.get("max_corr_with_fields"),
            "fdr_pass": record.get("fdr_pass"), "positive_share": record.get("pos_ratio"),
        }.items() if value is not None})
        if evidence not in item["evidence"]:
            item["evidence"].append(evidence)

    if SESSIONS_DIR.exists():
        for path in sorted(SESSIONS_DIR.glob("*.json")):
            payload = _read_json(path, {})
            session_id = payload.get("id") or path.stem
            label = f"llm_session:{session_id}"
            for factor in payload.get("factors") or []:
                put(factor.get("name"), label, factor, _rel(path))
                found.setdefault(factor.get("name", ""), {}).setdefault("first_seen", payload.get("created_at"))
    for path in sorted(ROOT.glob(MINING_GLOB)):
        payload = _read_json(path, [])
        if not isinstance(payload, list):
            continue
        run = path.parents[1].name
        label = f"llm_mining:{run}#{path.parent.name}"
        for factor in payload:
            if isinstance(factor, dict):
                put(factor.get("name"), label, factor, _rel(path))
    return list(found.values())


# ─────────────────────────── 3) 族 A 的条件（策略池登记） ───────────────────────────
def load_conditions() -> list[dict]:
    payload = _read_json(CONFIG, {})
    items = []
    for strategy in payload.get("strategies", []):
        if strategy.get("kind") not in ("rule_strategy", "research_arm", "factor_candidate"):
            continue
        stage = strategy.get("stage", "candidate")
        items.append({
            "id": strategy["id"],
            "kind": "condition",
            "source": f"strategy:{strategy['id']}",
            "family": strategy.get("group", ""),
            "status": {"production": "in_use"}.get(stage, stage),
            "grade": None,
            "expression": strategy.get("summary") or strategy.get("name"),
            "note": strategy.get("entry"),
            "metrics": {},
            "cluster": {},
            "first_seen": None,
            "last_seen": None,
            "evidence": list(strategy.get("artifacts", [])),
        })
    return items


# ─────────────────────────── 组装 ───────────────────────────
def build_library() -> dict:
    admitted = load_admitted()
    factors = load_protocol_factors(admitted) + load_llm_factors(admitted) + load_conditions()
    # 同名去重：协议层优先（它有完整指标与判决），LLM 的来源并入 evidence
    merged: dict[str, dict] = {}
    for item in factors:
        key = f"{item['kind']}:{item['id']}"
        if key in merged:
            existing = merged[key]
            existing["evidence"] = sorted(set(existing["evidence"]) | set(item["evidence"]))
            existing["sources"] = sorted(set(existing.get("sources", [])) | set(item.get("sources", [])))
            for name, value in item["metrics"].items():
                existing["metrics"].setdefault(name, value)
            existing["expression"] = existing["expression"] or item.get("expression")
            continue
        merged[key] = item
    items = sorted(merged.values(), key=lambda it: (STATUS_ORDER.index(it["status"]) if it["status"] in STATUS_ORDER else 99,
                                                    -(abs(it["metrics"].get("ic_mean") or 0))))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for item in items:
        item["last_seen"] = now
        item.setdefault("sources", [item["source"]])
        # 功能描述 + 分类（每个因子都要有，哪怕只是"未识别"）
        item["description"] = describe(item["id"], item.get("family") or "", item.get("note"),
                                       llm_reason=str(item.get("source", "")).startswith("llm_"))
        item["category"] = categorize(item["id"], item.get("family") or "")
        # 性能评价：把已有指标压成一句人话（避免页面上只有一堆数字）
        metrics, cluster = item.get("metrics") or {}, item.get("cluster") or {}
        bits = []
        if metrics.get("ic_mean") is not None:
            bits.append(f"IC 均值 {metrics['ic_mean']:+.4f}")
        if metrics.get("icir") is not None:
            bits.append(f"ICIR {metrics['icir']:+.2f}")
        if metrics.get("nw_t") is not None:
            bits.append(f"NW t {metrics['nw_t']:+.2f}")
        if metrics.get("fdr_pass") is not None:
            bits.append("过 BH-FDR" if metrics["fdr_pass"] else "未过 BH-FDR")
        if metrics.get("hlz_pass") is not None:
            bits.append("|t|≥3" if metrics["hlz_pass"] else "|t|<3")
        if metrics.get("months"):
            bits.append(f"{metrics['months']} 个月")
        if metrics.get("coverage") is not None:
            bits.append(f"覆盖 {metrics['coverage'] * 100:.0f}%")
        if cluster.get("size"):
            bits.append(f"簇内 {cluster['size']} 个" + ("（代表）" if cluster.get("representative") else "（冗余）"))
        item["performance"] = {
            "summary": "；".join(bits) or "没有可比指标（LLM 提议或质量列）",
            "ic_mean": metrics.get("ic_mean"), "icir": metrics.get("icir"), "nw_t": metrics.get("nw_t"),
            "ic_months": metrics.get("months"), "coverage": metrics.get("coverage"),
            "fdr_pass": metrics.get("fdr_pass"), "hlz_pass": metrics.get("hlz_pass"),
            "positive_share": metrics.get("positive_share"), "max_corr": metrics.get("max_corr"),
            "cluster_size": cluster.get("size"), "representative": cluster.get("representative"),
        }
    by_status: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    by_family: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for item in items:
        by_status[item["status"]] = by_status.get(item["status"], 0) + 1
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
        label = (item.get("category") or {}).get("label", "其它")
        by_category[label] = by_category.get(label, 0) + 1
        by_family[item["family"] or "未分类"] = by_family.get(item["family"] or "未分类", 0) + 1
    protocol = admitted["payload"]
    return {
        "generated_at": now,
        "criteria": protocol.get("criteria", {}),
        "window_months": protocol.get("window_months", []),
        "delta": protocol.get("delta_vs_previous", {}),
        "totals": {"n": len(items), "by_status": by_status, "by_kind": by_kind, "by_family": by_family,
                   "by_category": by_category, "n_admitted": by_status.get("admitted", 0)},
        "glossary_categories": glossary().get("categories", {}),
        "factors": items,
    }


def render_md(payload: dict) -> str:
    totals = payload["totals"]
    criteria = payload.get("criteria", {})
    lines = [
        f"# 因子库（生成于 {payload['generated_at']}）", "",
        f"* 总数 **{totals['n']}**：因子 {totals['by_kind'].get('factor', 0)} 条 + 条件 {totals['by_kind'].get('condition', 0)} 条"
        f"（族 A 的条件与策略池同一份登记）",
        f"* 状态分布：" + "、".join(f"{k} {v}" for k, v in sorted(totals["by_status"].items(), key=lambda kv: -kv[1])),
        f"* 准入口径：|IC|≥{criteria.get('min_abs_ic')}、ICIR≥{criteria.get('min_abs_icir')}、"
        f"BH-FDR q={criteria.get('fdr_q')}、HLZ t≥{criteria.get('hlz_t')}、簇内相关性阈值 {criteria.get('cluster_threshold')}"
        f"（窗口 {payload.get('window_months')}）",
        "",
        "## 本次新准入", "",
    ]
    newly = payload.get("delta", {}).get("newly_admitted", [])
    lines += [f"* `{name}`" for name in newly] or ["* （无）"]
    lines += ["", "## 准入因子（按 |IC| 排序，前 20）", "",
              "| 因子 | 档 | 族 | IC | ICIR | NW t | 簇 |", "|---|---|---|---|---|---|---|"]
    admitted_items = [item for item in payload["factors"] if item["status"] == "admitted"][:20]
    for item in admitted_items:
        metric = item["metrics"]
        cluster = item.get("cluster") or {}
        lines.append(f"| `{item['id']}` | {item['grade'] or '—'} | {item['family']} | "
                     f"{_fmt(metric.get('ic_mean'))} | {_fmt(metric.get('icir'))} | {_fmt(metric.get('nw_t'))} | "
                     f"#{cluster.get('id')}（{cluster.get('size')} 个同簇） |")
    lines += ["", "## 分类（每类一句话核心）", "", "| 分类 | 条数 | 一句话核心 |", "|---|---|---|"]
    categories = glossary().get("categories", {})
    for label, count in sorted(totals.get("by_category", {}).items(), key=lambda kv: -kv[1]):
        core = next((meta.get("core", "") for meta in categories.values() if meta.get("label") == label), "")
        lines.append(f"| {label} | {count} | {core} |")
    lines += ["", "## 族 A 的条件（来自策略池）", "", "| 条件 | 状态 | 说明 |", "|---|---|---|"]
    for item in [it for it in payload["factors"] if it["kind"] == "condition"]:
        lines.append(f"| `{item['id']}` | {item['status']} | {(item['expression'] or '')[:70]} |")
    lines.append("")
    return "\n".join(lines)


def _fmt(value) -> str:
    return "—" if value is None else f"{value:.4f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="合成因子库注册表（P2_42 §4.2）")
    parser.add_argument("--out", default=str(OUT_DIR))
    parser.add_argument("--print", dest="do_print", action="store_true")
    args = parser.parse_args(argv)

    payload = build_library()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "factors.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (out_dir / "factors.md").write_text(render_md(payload), encoding="utf-8")
    if args.do_print:
        totals = payload["totals"]
        print(f"[factor-library] {totals['n']} 条：factor {totals['by_kind'].get('factor', 0)} / "
              f"condition {totals['by_kind'].get('condition', 0)}；准入 {totals['n_admitted']}；"
              f"状态 " + "、".join(f"{k} {v}" for k, v in sorted(totals["by_status"].items(), key=lambda kv: -kv[1]))
              + f" -> {_rel(out_dir / 'factors.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
