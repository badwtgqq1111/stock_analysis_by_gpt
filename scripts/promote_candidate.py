#!/usr/bin/env python3
"""上线流水线一条命令（P2.42 §4.4）：登记候选 → 影子/闸门 → 出判决 → 给人工动作。

设计原则（与仓库既有约定一致）：
  * **一条命令只做"评估 + 给人工动作"**，默认**不改** `config/strategies.json`；
    升 stage 必须显式 `--apply --confirm`（人工确认那一步在页面上也可点，见 /api/agent/actions）；
  * 拿不到影子/闸门证据时给的是 **KEEP（继续观察）**，不是"通过"——宁可不动生产；
  * 判决三档与 `scripts/promotion_check.py` 完全同源：PROMOTE / KEEP / REJECT。

用法：
    uv run python scripts/promote_candidate.py box_arm                 # 只读评估（默认）
    uv run python scripts/promote_candidate.py box_arm --run            # 先跑影子 + 闸门再评估
    uv run python scripts/promote_candidate.py box_arm --apply --confirm  # 人工确认后改 stage
    uv run python scripts/promote_candidate.py --list                   # 列出在册策略与候选变体

产物：`output/results_cn/promotion_check/<ts>/promote_<id>.{json,md}`
退出码：PROMOTE 0 / KEEP 0 / REJECT 2 / 证据缺失 3 / 参数错误 4
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/strategies.json"
PROMOTION = ROOT / "output/results_cn/promotion_check"
SHADOW_SUMMARY = ROOT / "output/paper_trading/shadow/summary.json"
CANDIDATS_DIR = ROOT / "output/strategy_pool/candidates"
OUT_ROOT = ROOT / "output/results_cn/promotion_check"

EXIT = {"PROMOTE": 0, "KEEP": 0, "REJECT": 2, "MISSING": 3}


def load_registry() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def latest_verdicts() -> tuple[pd.DataFrame | None, str | None]:
    if not PROMOTION.exists():
        return None, None
    dirs = sorted((p for p in PROMOTION.iterdir() if p.is_dir()), reverse=True)
    for folder in dirs:
        path = folder / "verdicts.csv"
        if path.exists():
            return pd.read_csv(path), folder.name
    return None, None


def candidate_entry(ident: str) -> dict | None:
    if not CANDIDATS_DIR.exists():
        return None
    for path in CANDIDATS_DIR.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("id") == ident:
            return {**payload, "_file": str(path.relative_to(ROOT))}
    return None


def shadow_variant(ident: str) -> dict | None:
    if not SHADOW_SUMMARY.exists():
        return None
    payload = json.loads(SHADOW_SUMMARY.read_text(encoding="utf-8"))
    for variant in payload.get("variants", []):
        if variant.get("variant") == ident:
            return variant
    return None


def run_evidence(ident: str, *, run: bool) -> list[str]:
    """需要时**真跑**影子与闸门（默认不跑：这两个是重活，几十分钟量级）。"""
    logs: list[str] = []
    if not run:
        return ["未跑影子/闸门（默认复用既有产物；加 --run 才真跑）"]
    for cmd in (["uv", "run", "python", "scripts/run_shadow_variants.py", "--variants", ident],
                ["uv", "run", "python", "scripts/promotion_check.py"]):
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        logs.append(f"{' '.join(cmd[3:])} -> exit {proc.returncode}")
        if proc.returncode != 0:
            logs.append((proc.stderr or proc.stdout or "")[-400:])
    return logs


def evaluate(ident: str, *, run_evidence_flag: bool) -> dict:
    registry = load_registry()
    strategies = registry["strategies"]
    entry = next((s for s in strategies if s["id"] == ident), None)
    candidate = candidate_entry(ident)
    verdicts, run_id = latest_verdicts()
    row = None
    if verdicts is not None and "variant" in verdicts.columns:
        hits = verdicts[verdicts["variant"] == ident]
        if not hits.empty:
            row = hits.iloc[0].to_dict()

    evidence_logs = run_evidence(ident, run=run_evidence_flag)
    if run_evidence_flag:                       # 跑完重新读一次
        verdicts, run_id = latest_verdicts()
        if verdicts is not None and "variant" in verdicts.columns:
            hits = verdicts[verdicts["variant"] == ident]
            row = None if hits.empty else hits.iloc[0].to_dict()

    if entry is None:
        verdict, reason = "MISSING", f"{ident} 不在 config/strategies.json 里（先登记；候选在 {CANDIDAT_DIR_HINT}）"
        stage = None
    elif row is None:
        verdict, reason = "KEEP", (f"闸门里没有 {ident} 的同名变体（闸门只判影子变体）"
                                   "⇒ 继续观察，不动生产")
        stage = entry.get("stage")
    else:
        verdict = str(row.get("verdict") or "KEEP").upper()
        reason = str(row.get("reason") or "")
        stage = entry.get("stage")

    shadow = shadow_variant(ident)
    next_actions: list[str] = []
    if verdict == "PROMOTE":
        next_actions = [
            f"人工确认后升 stage：uv run python scripts/promote_candidate.py {ident} --apply --confirm",
            f"（等价命令）uv run python scripts/sync_strategy_pool.py --set-stage {ident} production",
        ]
    elif verdict == "KEEP":
        next_actions = [
            "继续用影子累积可比期（闸门要求 ≥20 个可比期、单票 ≤35%、每日 ≥3 只）",
            f"若还没登记候选：python scripts/sync_strategy_pool.py --register {ident} --stage research",
        ]
    else:
        next_actions = ["不要上线；把结论写进候选的 evidence 里，或换口径重做 A/B"]

    payload = {
        "id": ident,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "stage_now": stage,
        "verdict": verdict,
        "verdict_label": {"PROMOTE": "可以上线（人工确认）", "KEEP": "继续观察（不动生产）",
                          "REJECT": "否决", "MISSING": "缺乏证据/未登记"}.get(verdict, verdict),
        "reason": reason,
        "gate_run": run_id,
        "gate_row": None if row is None else {k: (None if pd.isna(v) else v) for k, v in row.items()},
        "shadow": None if shadow is None else {k: shadow.get(k) for k in
                                               ("label", "decision_dates", "final_nav", "total_return",
                                                "max_drawdown", "ann_vol")},
        "candidate": None if candidate is None else {k: candidate.get(k) for k in
                                                     ("id", "name", "stage", "summary", "entry", "updated_at")},
        "evidence_logs": evidence_logs,
        "next_actions": next_actions,
        "apply": {"applied": False},
    }
    return payload


CANDIDAT_DIR_HINT = "output/strategy_pool/candidates/"


def to_md(payload: dict) -> str:
    lines = [f"# 上线评估 · {payload['id']}", "",
             f"- 结论：**{payload['verdict_label']}**（`{payload['verdict']}`）",
             f"- 当前阶段：`{payload['stage_now']}`｜闸门批次：`{payload['gate_run']}`",
             f"- 理由：{payload['reason'] or '—'}", ""]
    if payload.get("shadow"):
        s = payload["shadow"]
        lines += [f"- 影子：{s['label']}｜{s['decision_dates']} 个决策日｜累计 {s['total_return']:+.2%}"
                  f"｜回撤 {s['max_drawdown']:+.2%}", ""]
    if payload.get("gate_row"):
        row = payload["gate_row"]
        lines += ["| 对照 | 可比期 | 更好/更差 | 中位差 | p | 单票上限 | 每日只数 | 判定 |",
                  "|---|---|---|---|---|---|---|---|",
                  f"| `{row.get('vs_baseline')}` | {row.get('common_periods')} | "
                  f"{row.get('better_periods')}/{row.get('worse_periods')} | "
                  f"{row.get('median_diff_vs_baseline')} | {row.get('sign_p')} | "
                  f"{row.get('max_single_weight')} | {row.get('min_names_per_date')} | {row.get('verdict')} |", ""]
    lines += ["## 下一步（人工动作）", ""] + [f"- {a}" for a in payload["next_actions"]]
    lines += ["", "## 证据", ""] + [f"- {line}" for line in payload["evidence_logs"]]
    lines += ["", "> 本命令默认**不改** `config/strategies.json`；`--apply --confirm` 才会动。"]
    return "\n".join(lines) + "\n"


def apply_stage(ident: str, stage: str) -> dict:
    cmd = ["uv", "run", "python", "scripts/sync_strategy_pool.py", "--set-stage", ident, stage]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return {"applied": proc.returncode == 0, "command": " ".join(cmd), "exit": proc.returncode,
            "stdout": (proc.stdout or "")[-500:], "stderr": (proc.stderr or "")[-500:]}


def main() -> int:
    ap = argparse.ArgumentParser(description="上线流水线一条命令（P2.42 §4.4）")
    ap.add_argument("strategy_id", nargs="?", help="策略 id（config/strategies.json 里的 id）")
    ap.add_argument("--run", action="store_true", help="先真跑影子 + 闸门（默认复用既有产物）")
    ap.add_argument("--apply", action="store_true", help="按判决改 stage（需要 --confirm）")
    ap.add_argument("--confirm", action="store_true", help="人工确认（与 --apply 一起用）")
    ap.add_argument("--list", action="store_true", help="列出在册策略与影子变体")
    ap.add_argument("--print", action="store_true")
    args = ap.parse_args()

    if args.list or not args.strategy_id:
        registry = load_registry()
        print("== 在册策略（config/strategies.json）==")
        for s in registry["strategies"]:
            print(f"  {s['id']:28} stage={s.get('stage'):11} {s.get('name', '')}")
        verdicts, run_id = latest_verdicts()
        if verdicts is not None:
            print(f"== 闸门最新批次 {run_id}（{len(verdicts)} 行）==")
            for row in verdicts.to_dict("records"):
                print(f"  {row.get('variant'):26} vs {str(row.get('vs_baseline')):16} "
                      f"可比 {row.get('common_periods')} 期 -> {row.get('verdict')}")
        return 0 if args.list else 4

    payload = evaluate(args.strategy_id, run_evidence_flag=args.run)
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    out_dir = OUT_ROOT / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.apply:
        if not args.confirm:
            print("[promote] --apply 必须配 --confirm（人工确认那一步不能自动化）")
            return 4
        payload["apply"] = apply_stage(args.strategy_id, "production")
    (out_dir / f"promote_{args.strategy_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / f"promote_{args.strategy_id}.md").write_text(to_md(payload), encoding="utf-8")
    if args.print or True:
        print(f"[promote] {args.strategy_id}: {payload['verdict_label']}（{payload['verdict']}）"
              f"｜stage={payload['stage_now']}｜理由：{payload['reason'][:80]}")
        for action in payload["next_actions"]:
            print(f"  → {action}")
        print(f"  -> {out_dir.relative_to(ROOT)}/promote_{args.strategy_id}.md")
    return EXIT.get(payload["verdict"], 3)


if __name__ == "__main__":
    raise SystemExit(main())
