#!/usr/bin/env python3
"""模型上线：**候选目录 → 生产目录**，带备份/校验/回滚（P2.47-R3）。

为什么必须有它（2026-10-11 事故）：一次实验参数没生效，训练**直接写进了生产目录**
`output/models/cn/lightgbm/alpha_zoo_hk/model.txt`，而旧模型**没有备份、无法原样回退**。
从那以后约定：**训练永远写候选目录，上线只走这一个脚本**（备份 → 校验 → 替换 → 出卡）。

用法：
    # 看一眼会做什么（不落盘）
    uv run python scripts/promote_model.py --candidate output/models/cn/lightgbm/alpha_zoo_hk_excess --dry-run
    # 真上线（先把生产文件备份成 model.prev.txt / model_manifest.prev.json）
    uv run python scripts/promote_model.py --candidate output/models/cn/lightgbm/alpha_zoo_hk_excess
    # 回滚上一次上线（把 prev 换回来）
    uv run python scripts/promote_model.py --rollback
规则：
  * 候选目录必须有 `model.txt` + `model_manifest.json`；
  * 候选的训练时间必须**比生产新**（否则拒绝，`--force` 可越过）；
  * 特征数/特征 schema 与生产不一致时**打印警告**（不静默）；
  * 上线后自动刷新模型卡（`scripts/build_model_cards.py`）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / "output/models/cn/lightgbm/alpha_zoo_hk"
MODEL_FILES = ("model.txt", "model_manifest.json")
BACKUP_SUFFIX = {"model.txt": "model.prev.txt", "model_manifest.json": "model_manifest.prev.json"}


def _rel(path: Path) -> str:
    """仓库内 → 相对路径；仓库外（测试 tmp_path）→ 原样返回（`relative_to` 会抛 ValueError）。"""
    text = str(path)
    prefix = str(ROOT) + "/"
    return text[len(prefix):] if text.startswith(prefix) else text


def _stamp(directory: Path) -> "str | None":
    path = directory / "model.txt"
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S") if path.exists() else None


def _manifest(directory: Path) -> dict:
    try:
        return json.loads((directory / "model_manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def promote(candidate: Path, production: Path, *, dry_run: bool = False, force: bool = False) -> dict:
    """把候选目录的模型文件换到生产目录（先备份）。返回一份审计结果（字典）。"""
    candidate, production = Path(candidate), Path(production)
    missing = [name for name in MODEL_FILES if not (candidate / name).exists()]
    if missing:
        raise SystemExit(f"候选目录缺文件：{missing}（{candidate}）")
    if not production.exists():
        raise SystemExit(f"生产目录不存在：{production}")
    cand_stamp, prod_stamp = _stamp(candidate), _stamp(production)
    cand_manifest, prod_manifest = _manifest(candidate), _manifest(production)
    report = {"candidate": str(candidate), "production": str(production), "candidate_trained_at": cand_stamp,
              "production_trained_at": prod_stamp, "dry_run": bool(dry_run),
              "candidate_features": cand_manifest.get("feature_count"),
              "production_features": prod_manifest.get("feature_count"),
              "candidate_label": cand_manifest.get("label_column"), "warnings": []}
    if cand_stamp and prod_stamp and cand_stamp <= prod_stamp and not force:
        raise SystemExit(f"候选不比生产新（候选 {cand_stamp} ≤ 生产 {prod_stamp}）；确认要覆盖请加 --force")
    if report["candidate_features"] != report["production_features"]:
        report["warnings"].append(f"特征数不同：{report['production_features']} → {report['candidate_features']}")
    if cand_manifest.get("feature_schema_hash") != prod_manifest.get("feature_schema_hash"):
        report["warnings"].append("feature_schema_hash 不同（特征口径可能已变）")
    if dry_run:
        report["action"] = "dry-run（未落盘）"
        return report
    for name in MODEL_FILES:
        target = production / name
        if target.exists():
            shutil.copy2(target, production / BACKUP_SUFFIX[name])       # 单级备份：每次上线覆盖上一次的 prev
    for name in MODEL_FILES:
        shutil.copy2(candidate / name, production / name)
    report["action"] = "promoted"
    report["backups"] = [_rel(production / BACKUP_SUFFIX[name]) for name in MODEL_FILES]
    return report


def rollback(production: Path) -> dict:
    """用 `prev` 备份把生产模型换回去（上一次上线的反向操作）。"""
    production = Path(production)
    restored = []
    for name, backup in BACKUP_SUFFIX.items():
        src = production / backup
        if not src.exists():
            raise SystemExit(f"没有备份文件：{src}")
        shutil.copy2(src, production / name)
        restored.append(name)
    return {"action": "rolled-back", "restored": restored, "trained_at": _stamp(production)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="模型上线（备份 → 校验 → 替换 → 出卡）")
    ap.add_argument("--candidate", default=None, help="候选模型目录（训练一律写这里）")
    ap.add_argument("--production", default=str(PRODUCTION))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="候选不比生产新时也允许覆盖")
    ap.add_argument("--rollback", action="store_true", help="用 prev 备份换回上一次的模型")
    ap.add_argument("--skip-cards", action="store_true")
    args = ap.parse_args(argv)

    if args.rollback:
        report = rollback(Path(args.production))
    else:
        if not args.candidate:
            ap.error("要么给 --candidate，要么用 --rollback")
        report = promote(Path(args.candidate), Path(args.production),
                         dry_run=args.dry_run, force=args.force)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report.get("warnings"):
        print("[promote] ⚠️ " + "；".join(report["warnings"]))
    if not args.dry_run and not args.skip_cards and report.get("action") in {"promoted", "rolled-back"}:
        subprocess.run([sys.executable, str(ROOT / "scripts/build_model_cards.py"), "--print"],
                       cwd=ROOT, check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
