#!/usr/bin/env python3
"""给「策略池候选」里的因子存值快照（幂等；不是面板列的候选会被**如实记下来**）。

为什么需要（P2.53 §5 第 2 条）：候选注册时会尝试快照一次（`POST /api/strategies/candidates`），
但候选的因子值可能**当时还不可复算**（LLM 表达式尚未落成面板列），也可能当时面板还没更新。
所以再加一个每日扫描：能存就存、已有就跳过、不能存就记原因并给出命令（不假装成功）。

用法：
    uv run python scripts/snapshot_candidates.py --print
    uv run python scripts/snapshot_candidates.py --force        # 已有快照也重存
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = ROOT / "output/strategy_pool/candidates"
SNAPSHOTS = ROOT / "output/factor_library/snapshots"
REPORT = ROOT / "output/factor_library/snapshot_candidates.json"
PANEL_PREFIX = "llm-factor-"


def panel_column_candidates(ident: str) -> list[str]:
    """候选 id → 可能的面板列名（候选 id 常带前缀，例如 `llm-factor-rev_vol`）。"""
    names = [ident]
    if ident.startswith(PANEL_PREFIX):
        names.append(ident[len(PANEL_PREFIX):])
    if ident.endswith("_clean"):
        names.append(ident[:-len("_clean")])
    else:
        names.append(f"{ident}_clean")
    seen, out = set(), []
    for name in names:
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="候选因子值快照（幂等）")
    ap.add_argument("--force", action="store_true", help="已有快照也重存")
    ap.add_argument("--months", type=int, default=24)
    ap.add_argument("--print", action="store_true")
    args = ap.parse_args()
    rows = []
    if CANDIDATES.exists():
        for path in sorted(CANDIDATES.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            ident = str(payload.get("id") or path.stem)
            existing = next((SNAPSHOTS / f"{name}.json" for name in panel_column_candidates(ident)
                             if (SNAPSHOTS / f"{name}.json").exists()), None)
            if existing and not args.force:
                rows.append({"id": ident, "status": "skipped_existing",
                             "snapshot": str(existing.relative_to(ROOT))})
                continue
            done = None
            for name in panel_column_candidates(ident):
                proc = subprocess.run([sys.executable, str(ROOT / "scripts/snapshot_factor_values.py"),
                                       "--factor", name, "--months", str(args.months)],
                                      cwd=ROOT, capture_output=True, text=True)
                if proc.returncode == 0:
                    done = name
                    break
            rows.append({"id": ident,
                         "status": "ok" if done else "not_a_panel_column",
                         "panel_column": done,
                         "snapshot": f"output/factor_library/snapshots/{done}.parquet" if done else None,
                         "command": None if done else
                         f"uv run python scripts/snapshot_factor_values.py --factor <面板列名> --months {args.months}",
                         "hint": None if done else "该候选是表达式（还没有落成面板列）；等它进流水线后再存快照"})
    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "counts": {"candidates": len(rows),
                          "ok": sum(1 for r in rows if r["status"] == "ok"),
                          "skipped": sum(1 for r in rows if r["status"] == "skipped_existing"),
                          "not_panel_column": sum(1 for r in rows if r["status"] == "not_a_panel_column")},
               "rows": rows}
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.print:
        print(f"[candidates] {payload['counts']['candidates']} 个候选：存了 {payload['counts']['ok']}"
              f" / 已存在 {payload['counts']['skipped']} / 非面板列 {payload['counts']['not_panel_column']}")
        for row in rows:
            print(f"  {row['status']:<20} {row['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
