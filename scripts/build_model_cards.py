#!/usr/bin/env python3
"""模型卡 `output/models/cn/<model>/card.json`（P2.42 §4.3 + P2.47 R2 + P2.46-1）。

为什么要有它（两条用户问题逼出来的）：
  * "lightgbm 是不是改成实时训练更好呢，现有的是保存成模型" → 先要能回答"**现在用的是哪一版、什么时候训的、赢在哪**"，
    否则重训节奏无从谈起；每次重跑本脚本会把上一张卡挪成 `card.prev.json`，卡里直接给**与上一版的逐项对照**。
  * "为什么模型看起来跑不过传统策略" → 卡里把**有效样本量**摊开（书 Ch 7.2：H 日前瞻标签重叠 ⇒
    `N_eff ≈ N/H`，SPY 实测 10 年名义 248,436 → 有效 11,830，约 4.6× 高估），
    我们 `days=365` + `label_horizon=20` ⇒ 每只票只有约 12 个独立窗口 —— 这个数字必须写在卡上，而不是藏在配置里。
用法：uv run python scripts/build_model_cards.py [--print]
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "output/models/cn"
ADMITTED = ROOT / "output/research/factor_ic_protocol/factor_ic_admitted.json"
RETIRE_RULE = ("滚动 3 个月 IC 连续为负、或特征覆盖率跌破 80%、"
               "或影子变体在 ≥20 个决策日的同窗口配对中显著超越（promotion_check 判 PROMOTE）")


def _rel(path: Path) -> str:
    """仓库内 → 相对路径；仓库外（测试里的 tmp_path）→ 原样返回（`relative_to` 会直接抛 ValueError）。"""
    text = str(path)
    prefix = str(ROOT) + "/"
    return text[len(prefix):] if text.startswith(prefix) else text


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _trading_days(start: str, end: str) -> int:
    """训窗内的 A 股交易日数（按自然日 ×5/7 粗算，只为给 N_eff 一个可核对的数量级）。"""
    try:
        span = (datetime.fromisoformat(str(end)[:10]) - datetime.fromisoformat(str(start)[:10])).days
    except (TypeError, ValueError):
        return 0
    return max(int(span * 5 / 7), 0)


def build_card(manifest_path: Path) -> dict:
    manifest = _read_json(manifest_path, {})
    if not manifest:
        return {}
    extra = manifest.get("extra") or {}
    params = extra.get("params") or {}
    split = extra.get("split") or {}
    horizon = 20                                  # `[lightgbm] label_horizon`（训练脚本写进 label_column）
    train_days = _trading_days(manifest.get("train_start"), manifest.get("train_end"))
    admitted = _read_json(ADMITTED, {})
    model_file = manifest_path.parent / ("model.txt" if "lightgbm" in str(manifest_path) else "model.pt")
    trained_at = (datetime.fromtimestamp(model_file.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                  if model_file.exists() else None)
    card = {
        "model_type": manifest.get("model_type") or manifest_path.parent.name,
        "path": _rel(manifest_path.parent),
        "trained_at": trained_at,
        "factor_set": manifest.get("factor_set"),
        "cleaning_version": manifest.get("cleaning_version"),
        "label": {"column": manifest.get("label_column"), "horizon_days": horizon,
                  "note": "前向收益标签（非超额）；P2.45-B 计划改成超额并做 A/B"},
        "window": {"train_start": manifest.get("train_start"), "train_end": manifest.get("train_end"),
                   "validation_start": manifest.get("validation_start"),
                   "validation_end": manifest.get("validation_end"),
                   "train_trading_days": train_days},
        "split": {"method": split.get("method"), "validation_days": split.get("validation_days"),
                  "embargo_days": split.get("embargo_days"),
                  "purged_date_count": split.get("purged_date_count")},
        "features": {"count": manifest.get("feature_count"),
                     "preprocessing": (manifest.get("preprocessing") or {}).get("mode"),
                     "schema_hash": manifest.get("feature_schema_hash"),
                     "admission_file": "output/research/factor_ic_protocol/factor_ic_admitted.json",
                     "admitted_count": admitted.get("admitted_count")},
        "params": params,
        "sample_size": {
            "label_windows_per_stock": round(train_days / horizon, 1) if train_days else None,
            "formula": "N_eff ≈ N/H（H 日前瞻标签重叠；López de Prado 2018 的 purging / Jansen 2e Ch 7.2 p.196）",
            "note": ("名义行数会把显著性高估约 H 倍：本卡的 `label_windows_per_stock` = 训窗交易日数 / 标签期，"
                     "才是**每只票的独立样本数**。要报置信区间就该用 N_eff，不是名义行数。"),
        },
        "performance": {
            "validation_ic": (extra.get("validation_ic") or {}).get("value"),
            "validation_ic_name": (extra.get("validation_ic") or {}).get("name"),
            "best_ic": (extra.get("validation_ic") or {}).get("best"),
            "trees_kept": extra.get("trees_kept"),
            "best_iteration": extra.get("best_iteration"),
            "early_stopping_rounds": extra.get("early_stopping_rounds"),
            "refit_without_early_stopping": extra.get("refit_without_early_stopping"),
            "note": ("valid IC 是**验证段**的日均 IC；`early_stopping_rounds = 0` 意味着没按验证集挑轮数"
                     "（P2.45-C 要改成 >0 并做 A/B）"),
        },
        "retire_when": RETIRE_RULE,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    # 与上一版对照（上一张卡若存在）
    prev_path = manifest_path.parent / "card.prev.json"
    prev = _read_json(prev_path, {})
    if prev:
        card["previous"] = {
            "trained_at": prev.get("trained_at"),
            "features": (prev.get("features") or {}).get("count"),
            "train_window": (prev.get("window") or {}).get("train_start",
                                                           "") + "~" + (prev.get("window") or {}).get("train_end", ""),
            "label_windows_per_stock": (prev.get("sample_size") or {}).get("label_windows_per_stock"),
            "delta_features": (manifest.get("feature_count") or 0) - ((prev.get("features") or {}).get("count") or 0),
        }
    return card


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成模型卡（P2.42 §4.3）")
    ap.add_argument("--print", dest="do_print", action="store_true")
    args = ap.parse_args(argv)
    cards = []
    for manifest_path in sorted(MODELS.glob("*/*/model_manifest.json")):
        card = build_card(manifest_path)
        if not card:
            continue
        target = manifest_path.parent / "card.json"
        if target.exists():
            target.replace(manifest_path.parent / "card.prev.json")   # 旧卡留档，新卡带对照
        target.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        cards.append(card)
    summary = ["# 模型卡汇总", "",
               "| 模型 | 路径 | 训练于 | 特征数 | 训窗 | 每票独立窗口 | 标签 |",
               "|---|---|---|---|---|---|---|"]
    for card in cards:
        summary.append(f"| {card['model_type']} | `{card['path']}` | {card['trained_at']} | "
                       f"{card['features']['count']} | {card['window']['train_start']}~{card['window']['train_end']} | "
                       f"{card['sample_size']['label_windows_per_stock']} | {card['label']['column']} |")
    (MODELS / "CARDS.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    if args.do_print:
        for card in cards:
            print(f"[model-card] {card['path']}: 训练于 {card['trained_at']} ｜ 特征 {card['features']['count']} "
                  f"｜ 训窗 {card['window']['train_start']}~{card['window']['train_end']} "
                  f"｜ 每票独立窗口 {card['sample_size']['label_windows_per_stock']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
