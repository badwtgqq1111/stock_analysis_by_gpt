#!/usr/bin/env python3
"""因子适应度（Fitness Hook）：把"因子好坏"从写死的 |IC| 变成**可配置**打分。

参考（2026-10-12）：FactorMiner 的 `user_workspace/custom_fitness/`（可插拔适应度）。
本仓库的纪律比它严：**fitness 只用于排序候选**，不替代
  ① 因子 IC 协议（`scripts/factor_ic_protocol.py`：|IC|/BH-FDR/HLZ t≥3/簇冗余 —— 准入裁判）；
  ② 上线闸门（`scripts/promotion_check.py`：同窗口逐期配对 + 集中度体检）。

定义（全部写在 `config/factor_fitness.json`，脚本不写死）：
  score = Σ w_i × min(|x_i| / target_i, cap)  −  Σ w_j × 越界惩罚(x_j)
  越界惩罚 = w_j × max(0, (x_j − target_j) / target_j)（`mode=above`）
  缺证据（没有 inspector 档案）⇒ 惩罚项按**未知**计，不假装为 0，并在 coverage 里标出来。

用法：
    uv run python scripts/factor_fitness.py --profile default --print
    uv run python scripts/factor_fitness.py --profile low_turnover --top 20
输出：output/factor_library/fitness.json / fitness.csv / FITNESS.md
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/factor_fitness.json"
LIBRARY = ROOT / "output/factor_library/factors.json"
INSPECTOR = ROOT / "output/factor_library/inspector"
OUT = ROOT / "output/factor_library"
STATUSES = ("admitted", "in_use", "candidate")


def dig(payload: dict, path: str):
    """取 `a.b.20.c` 这样的路径（dict/list 混着走）；取不到返回 None。"""
    node = payload
    for part in path.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if node is None:
            return None
    return node


def load_fitness_config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def load_library() -> list[dict]:
    payload = json.loads(LIBRARY.read_text(encoding="utf-8")) if LIBRARY.exists() else {"factors": []}
    return payload.get("factors", [])


def load_inspector(factor_id: str) -> dict | None:
    path = INSPECTOR / f"{factor_id}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if payload.get("status") == "ok" else None


def score_one(entry: dict, inspector: dict | None, profile: dict, config: dict) -> dict:
    targets, cap = config["targets"], float(config.get("cap", 1.5))
    context = {"metrics": entry.get("metrics") or {}, "inspector": inspector or {}}
    score = 0.0
    components: dict[str, dict] = {}
    for item in profile["components"]:
        value = dig(context, item["source"])
        if value is None and item.get("fallback"):
            value = dig(context, item["fallback"])
        target = float(targets[item["key"].removeprefix("abs_")] if item["key"].removeprefix("abs_") in targets
                       else targets.get(item["key"], 0) or 0)
        target = target or float(item.get("target") or 0) or 1e-9
        if value is None:
            components[item["key"]] = {"value": None, "norm": 0.0, "note": "缺数据（按 0 计）"}
            continue
        norm = min(abs(float(value)) / target, cap)
        score += float(item["weight"]) * norm
        components[item["key"]] = {"value": float(value), "norm": round(norm, 4),
                                  "weight": item["weight"], "target": target}
    penalties: dict[str, dict] = {}
    unknown = []
    for item in profile["penalties"]:
        value = dig(context, item["source"])
        target = float(targets.get(item["target"], 0) or item.get("target") or 0)
        if value is None or target <= 0:
            unknown.append(item["key"])
            penalties[item["key"]] = {"value": None, "penalty": None, "note": "缺证据（未知，不按 0 计）"}
            continue
        excess = max(0.0, (abs(float(value)) - target) / target)
        penalty = float(item["weight"]) * excess
        score -= penalty
        penalties[item["key"]] = {"value": float(value), "target": target,
                                  "excess": round(excess, 4), "penalty": round(penalty, 4)}
    return {
        "id": entry.get("id"),
        "name": entry.get("name") or entry.get("id"),
        "status": entry.get("status"),
        "category": (entry.get("category") or {}).get("label") if isinstance(entry.get("category"), dict)
        else entry.get("category"),
        "score": round(score, 4),
        "components": components,
        "penalties": penalties,
        "unknown_penalties": unknown,
        "has_inspector": bool(inspector),
        "fdr_pass": (entry.get("metrics") or {}).get("fdr_pass"),
        "hlz_pass": (entry.get("metrics") or {}).get("hlz_pass"),
        "max_abs_rho": ((inspector or {}).get("redundancy") or {}).get("max_abs_rho"),
    }


def build(profile_key: str) -> dict:
    config = load_fitness_config()
    profiles = config["profiles"]
    if profile_key not in profiles:
        raise SystemExit(f"未知 profile {profile_key!r}；可选：{sorted(profiles)}")
    profile = profiles[profile_key]
    rows = [score_one(entry, load_inspector(str(entry.get("id"))), profile, config)
            for entry in load_library() if entry.get("status") in STATUSES]
    rows.sort(key=lambda row: row["score"], reverse=True)
    reviewed = sum(1 for row in rows if row["has_inspector"])
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
        # 「推荐入池候选」= 过 FDR **且已审过冗余证据**且不冗余。
        # 没跑过 inspector 的因子**不给"推荐"**，只标 `pending_review` —— 缺证据不假装（否则 96 条"推荐"是假象）。
        reviewed_ok = row["has_inspector"] and row["max_abs_rho"] is not None
        row["recommended"] = bool(row["fdr_pass"]) and reviewed_ok and row["max_abs_rho"] <= 0.7
        row["pending_review"] = bool(row["fdr_pass"]) and not reviewed_ok
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "profile": profile_key,
        "profile_label": profile.get("label"),
        "config_path": str(CONFIG.relative_to(ROOT)),
        "targets": config["targets"],
        "cap": config.get("cap"),
        "counts": {"factors": len(rows), "with_inspector": reviewed,
                   "recommended": sum(1 for row in rows if row["recommended"]),
                   "pending_review": sum(1 for row in rows if row.get("pending_review"))},
        "coverage_note": ("有 inspector 档案的因子才会被算冗余/换手/衰减惩罚；"
                          f"当前 {reviewed}/{len(rows)} 条有档案（其余惩罚项标为未知，不按 0 计）"),
        "discipline": ("fitness 只用于**排序候选**；准入仍由 factor_ic_protocol 判，上线仍由 promotion_check 判。"),
        "factors": rows,
    }


def _fmt(value, spec: str = ".3f") -> str:
    return "—" if value is None else format(float(value), spec)


def to_md(payload: dict, top: int = 30) -> str:
    lines = [f"# 因子适应度（{payload['profile']}：{payload['profile_label']}）", "",
             f"- 生成于 {payload['generated_at']}｜{payload['counts']['factors']} 条候选"
             f"（{payload['counts']['with_inspector']} 条有审查档案）｜"
             f"**推荐 {payload['counts']['recommended']} 条 / 待审查 {payload['counts']['pending_review']} 条**", "",
             f"> {payload['discipline']}", f"> {payload['coverage_note']}", "",
             "| # | 因子 | 状态 | 分类 | score | IC | ICIR | NW t | max\u03c1 | 惩罚（冗余/换手/衰减） | 推荐 |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for row in payload["factors"][:top]:
        components = row["components"]
        penalties = row["penalties"]

        def pen(key: str) -> str:
            item = penalties.get(key) or {}
            return "—" if item.get("penalty") is None else format(float(item["penalty"]), ".2f")

        lines.append(
            f"| {row['rank']} | `{row['id']}` | {row['status']} | {row.get('category') or '—'} | "
            f"**{format(row['score'], '.3f')}** | "
            f"{_fmt((components.get('abs_ic') or {}).get('value'), '.4f')} | "
            f"{_fmt((components.get('abs_icir') or {}).get('value'), '.2f')} | "
            f"{_fmt((components.get('abs_nw_t') or {}).get('value'), '.2f')} | "
            f"{_fmt(row.get('max_abs_rho'))} | "
            f"{pen('redundancy')} / {pen('turnover')} / {pen('decay')} | "
            f"{'✅' if row['recommended'] else ('待审' if row.get('pending_review') else '')} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="因子适应度（可配置 Fitness Hook）")
    ap.add_argument("--profile", default="default")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--print", action="store_true")
    ap.add_argument("--review-top", type=int, default=0,
                    help="先把 |ICIR| 最强且还没有审查档案的 N 条送进 factor_inspector，再评分")
    args = ap.parse_args()
    if args.review_top:
        import subprocess
        library = [e for e in load_library() if e.get("status") in STATUSES
                   and (e.get("metrics") or {}).get("icir") is not None]
        library.sort(key=lambda e: abs(float(e["metrics"]["icir"])), reverse=True)
        todo = [e["id"] for e in library if load_inspector(str(e["id"])) is None][: args.review_top]
        for factor_id in todo:
            print(f"[fitness] 先审查 {factor_id} …")
            subprocess.run([sys.executable, str(ROOT / "scripts/factor_inspector.py"),
                            "--factor", str(factor_id), "--months", "24"], cwd=ROOT, check=False)
    payload = build(args.profile)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "fitness.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "FITNESS.md").write_text(to_md(payload, top=max(args.top, 30)), encoding="utf-8")
    with (OUT / "fitness.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["rank", "id", "status", "category", "score", "has_inspector", "fdr_pass",
                         "max_abs_rho", "recommended", "penalty_redundancy", "penalty_turnover", "penalty_decay"])
        for row in payload["factors"]:
            writer.writerow([row["rank"], row["id"], row["status"], row.get("category") or "", row["score"],
                             row["has_inspector"], row["fdr_pass"], row["max_abs_rho"], row["recommended"],
                             (row["penalties"].get("redundancy") or {}).get("penalty"),
                             (row["penalties"].get("turnover") or {}).get("penalty"),
                             (row["penalties"].get("decay") or {}).get("penalty")])
    if args.print:
        print(f"[fitness] profile={payload['profile']} 候选 {payload['counts']['factors']} 条"
              f"（{payload['counts']['with_inspector']} 条有审查档案）→ 推荐 {payload['counts']['recommended']} 条")
        for row in payload["factors"][: args.top]:
            print(f"  {row['rank']:>3}. {row['score']:+.3f}  {row['id']:<34} {row['status']:<9}"
                  f" maxρ={_fmt(row['max_abs_rho'])}"
                  f"{'  ✅推荐' if row['recommended'] else ('  待审' if row.get('pending_review') else '')}")
        print(f"  -> {(OUT / 'FITNESS.md').relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
