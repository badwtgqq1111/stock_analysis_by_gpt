#!/usr/bin/env python3
"""策略抽象层的 CLI：校验 / 接线矩阵 / 新增策略骨架。

    uv run python scripts/strategy_registry.py --check            # 声明层校验 + 覆盖自检（退出码 1 = 有问题）
    uv run python scripts/strategy_registry.py --wiring           # 打印接线矩阵（每条策略 × 每个消费端）
    uv run python scripts/strategy_registry.py --wiring --save    # 落 output/strategy_pool/wiring.{json,md}
    uv run python scripts/strategy_registry.py --consumers rot_arm
    uv run python scripts/strategy_registry.py --new my_strategy --kind rule_strategy \\
        --entry "uv run python scripts/select_my_strategy.py" --selection-source cn_my_strategy \\
        --file output/results_cn/my_strategy_selection.csv --refresh run     # 打印骨架（--write 才写入 config）

为什么要有这个 CLI：用户要求"后续新增策略的时候，web 端能自动同步好数据"。
新增一条策略的最小动作 = `--new ... --write` 一条命令；写完后
选股结果页 / K线自选 / 策略池 / 每日刷新 / 体检 **全部自动出现**（它们都从注册表派生）。
`--check` 就是这条承诺的守门人：漏接线会当场点名（例如"有名单却没有 selection 块"）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services import strategy_registry as reg   # noqa: E402

OUT_DIR = ROOT / "output/strategy_pool"


def cmd_check(save: bool = False) -> int:
    problems = reg.validate()
    cov = reg.coverage()
    print(f"[registry] 策略 {cov['n_strategies']} 条；消费端 {len(cov['consumers'])} 个")
    for key, label in cov["consumers"].items():
        wired = cov["matrix"].get(key, [])
        missing = sorted(set(reg.ids()) - set(wired))
        print(f"  - {key:<20} {label:<34} 已接 {len(wired):>2}/{cov['n_strategies']} 条"
              f"{'' if not missing else ' ← 未接：' + '、'.join(missing)}")
    if cov["by_design"]:
        print(f"[registry] 按设计未接 {len(cov['by_design'])} 处（不是漏接线）：")
        for row in cov["by_design"]:
            print(f"  · {row['strategy']:<24} {row['consumer']:<20} {row['reason']}")
    if cov["gaps"]:
        print(f"[registry] ⚠️ 意外的未接 {len(cov['gaps'])} 处（漏接线）：{[ (g['strategy'], g['consumer']) for g in cov['gaps'] ]}")
        return 1
    if problems:
        print(f"[registry] ❌ 声明层问题 {len(problems)} 条：")
        for item in problems:
            print(f"  - {item}")
        return 1
    if save:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        payload = reg.wiring_matrix()
        (OUT_DIR / "wiring.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        (OUT_DIR / "wiring.md").write_text(reg.to_markdown(payload), encoding="utf-8")
        (OUT_DIR / "registry_check.json").write_text(json.dumps(
            {"generated_at": cov["generated_at"], "problems": problems, "n_strategies": cov["n_strategies"],
             "gaps": cov["gaps"], "by_design": cov["by_design"],
             "matrix": {k: len(v) for k, v in cov["matrix"].items()}}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"[registry] 已写 {(OUT_DIR / 'wiring.json').relative_to(ROOT)} / wiring.md / registry_check.json")
    if problems:
        return 1
    print("[registry] ✅ 声明层校验通过（每条策略的选股源/刷新/台账/曲线/历史都接上了）")
    return 0 if not cov["gaps"] else 1


def cmd_wiring(save: bool) -> int:
    payload = reg.wiring_matrix()
    md = reg.to_markdown(payload)
    print(md)
    if save:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "wiring.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        (OUT_DIR / "wiring.md").write_text(md, encoding="utf-8")
        print(f"[registry] 已写 {(OUT_DIR / 'wiring.json').relative_to(ROOT)} 与 wiring.md")
    return 0


def cmd_consumers(ident: str) -> int:
    item = reg.spec(ident)
    print(json.dumps({"id": item["id"], "name": item["name"], "consumers": reg.consumers(item)},
                     ensure_ascii=False, indent=2))
    return 0


def skeleton(args: argparse.Namespace) -> dict:
    """一条新策略的最小声明：所有消费端需要的字段都在这里，缺的会用 TODO 明确标出来。"""
    ident = args.new
    file_rel = args.file or f"output/results_cn/{ident}_selection.csv"
    source = args.selection_source or f"cn_{ident}"
    entry = args.entry or f"uv run python scripts/select_{ident}.py"
    return {
        "id": ident,
        "name": args.name or ident,
        "group": args.group or "未分组",
        "kind": args.kind,
        "stage": args.stage,
        "owner": "TODO：谁负责（agent / user）",
        "summary": "TODO：一句话说清这条策略在赌什么、口径是什么（会显示在策略池与选股结果页）",
        "entry": entry,
        "schedule": "每日自动化（daily_refresh 声明）",
        "date_column": "trade_date",
        "tag_class": "dim",
        "artifacts": [file_rel],
        "history_dir": args.history_dir or f"output/results_cn/{ident}_history",
        "selection_source": source,
        "selection": {
            "label": args.name or ident,
            "note": "TODO：口径与免责（会贴在选股结果页的说明里）",
            "roles": [[("selected" if args.kind in ("model", "model_legacy") else "research"), file_rel]],
            "columns": ["rank", "stock_code", "score", "target_weight"],
            "code_style": "ts_code",
            "supports_shap": False,
        },
        "daily_refresh": ({"mode": "skip", "reason": "TODO：按需/无独立名单"}
                          if args.refresh == "skip" else {"mode": args.refresh, "max_minutes": 20}),
        # 台账来源默认留空：解析器（build_selection_ledger 的适配器 + registry.LEDGER_ADAPTERS）
        # 建好之后再打开，否则 `--check` 会（正确地）报"声明了却没人解析"。
        "ledger_sources": [args.ledger] if args.ledger else [],
        "curve": {"tracking": True},
    }


def cmd_new(args: argparse.Namespace) -> int:
    spec = skeleton(args)
    print(json.dumps(spec, ensure_ascii=False, indent=2))
    # 干跑校验：把候选条目临时塞进一份配置副本，跑注册表的校验（不碰真 config）
    import tempfile

    config = reg.read_config()
    config["strategies"] = list(config["strategies"]) + [spec]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False)
        tmp_path = Path(handle.name)
    original = reg.CONFIG
    reg.CONFIG = tmp_path
    try:
        problems = [p for p in reg.validate() if p.startswith(spec["id"])]
        wired = reg.consumers(reg.normalize(spec))
    finally:
        reg.CONFIG = original
        tmp_path.unlink(missing_ok=True)
    print(f"\n[new] 干跑校验：{'✅ 通过' if not problems else '❌ ' + '；'.join(problems)}")
    print("[new] 这条声明一写进 config，就会自动接上：")
    for key, info in wired.items():
        flag = "✅" if info.get("wired") else "⚠️ "
        print(f"  {flag} {key:<20} {info.get('how')}")
    if args.write:
        if problems:
            print("[new] ❌ 有问题，未写入（先补 TODO 再 --write）")
            return 1
        config = reg.read_config()
        config["strategies"].append(spec)
        config["updated_at"] = "2026-10-13"
        reg.CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[new] 已写入 {reg.CONFIG.relative_to(ROOT)}（共 {len(config['strategies'])} 条策略）")
    else:
        print("[new] 未写入（加 --write 才写 config）")
    return 0 if not problems else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="策略抽象层：校验 / 接线矩阵 / 新增策略")
    ap.add_argument("--check", action="store_true", help="声明层校验 + 覆盖自检")
    ap.add_argument("--wiring", action="store_true", help="打印接线矩阵")
    ap.add_argument("--save", action="store_true", help="把接线矩阵落到 output/strategy_pool/wiring.{json,md}")
    ap.add_argument("--consumers", default=None, help="只看某条策略的接线")
    ap.add_argument("--new", default=None, help="生成一条新策略的声明骨架")
    ap.add_argument("--name", default=None)
    ap.add_argument("--group", default=None)
    ap.add_argument("--kind", default="rule_strategy", choices=sorted(reg.KINDS))
    ap.add_argument("--stage", default="candidate", choices=list(reg.STAGES))
    ap.add_argument("--entry", default=None)
    ap.add_argument("--selection-source", dest="selection_source", default=None)
    ap.add_argument("--file", default=None, help="顶层名单产物（仓库相对路径）")
    ap.add_argument("--history-dir", dest="history_dir", default=None)
    ap.add_argument("--refresh", default="run", choices=list(reg.REFRESH_MODES))
    ap.add_argument("--ledger", default=None,
                    help="台账 source 名（决定日历里的来源标签）；建好解析器后再填，"
                         "否则 --check 会报「声明了却没人解析」")
    ap.add_argument("--write", action="store_true", help="--new 时真正写进 config/strategies.json")
    args = ap.parse_args()

    if args.new:
        return cmd_new(args)
    if args.consumers:
        return cmd_consumers(args.consumers)
    if args.wiring:
        return cmd_wiring(args.save)
    return cmd_check(args.save)


if __name__ == "__main__":
    raise SystemExit(main())
