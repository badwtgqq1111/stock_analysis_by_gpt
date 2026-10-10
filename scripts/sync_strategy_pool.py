#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""策略池同步：把「声明式登记表 + 候选登记 + 研究证据」合成一份前端可读的快照。

用户要求（2026-10-10）："应该有个策略池，比如现有 模型选股 / arm_select / 行业轮动，
后面还应该有其他策略进入策略池，agent 挖掘好的新策略应该进入策略池。"

三层结构（**声明与运行时分开放**，避免"改了一行配置就把运行状态抹掉"）：

  1. 声明层 `config/strategies.json`
     —— 人写的：id / 名称 / 分组 / 类别 / 上线阶段 / 入口命令 / 产物路径。
  2. 运行时层 `output/strategy_pool/strategy_pool.json`（**本脚本生成**）
     —— 扫产物得到的：最新交易日 / 条数 / 文件是否存在 / 是否新鲜 / 相对基准交易日落后几天。
  3. 候选层 `output/strategy_pool/candidates/*.json`
     —— LLM 因子挖掘、agent 研究、人工试跑产出的**新策略**先落这里（stage=candidate），
        验证过再用 `--set-stage <id> research|production` 升格；升到 production 还要
        同步写进 `config/strategies.json`（脚本会提醒，不自动改配置文件）。

另外把 `output/research/*/REPORT.md` 扫成「研究证据」清单挂到页面上：
它们不是策略（不产生每日名单），但正是"策略从哪来"的可追溯出处。

用法：
  uv run python scripts/sync_strategy_pool.py                     # 同步快照（每日自动化最后几步调用）
  uv run python scripts/sync_strategy_pool.py --strict            # 生产策略不新鲜则退出码 1
  uv run python scripts/sync_strategy_pool.py --register cand.json
  uv run python scripts/sync_strategy_pool.py --register-json '{"id": ...}'
  uv run python scripts/sync_strategy_pool.py --set-stage <id> research
  uv run python scripts/sync_strategy_pool.py --print             # 打表格
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONFIG_PATH = ROOT / "config/strategies.json"
POOL_DIR = ROOT / "output/strategy_pool"
POOL_PATH = POOL_DIR / "strategy_pool.json"
POOL_MD = POOL_DIR / "strategy_pool.md"
CANDIDATE_DIR = POOL_DIR / "candidates"
RESEARCH_DIR = ROOT / "output/research"

# 阶段词汇（前端按这个顺序渲染）：candidate 待验证 → research 研究中 → production 生产中 → retired 已下线
STAGES = ("nowhere", "candidate", "research", "production", "retired")
STAGE_LABEL = {
    "candidate": "候选（待验证）",
    "research": "研究中（有名单但不进生产）",
    "production": "生产中（每日自动化在跑）",
    "retired": "已下线",
}


# ─────────────────────────────────────────────────────────────────────────────
# 基准交易日
# ─────────────────────────────────────────────────────────────────────────────
def latest_trade_date() -> str | None:
    """最新交易日：优先用行情服务（和榜单/K 线同一口径），失败则退回基准指数。"""
    try:
        from backend.services.market_board_service import latest_trade_date as _ltd
        day = _ltd()
        if day is not None:
            return str(pd.Timestamp(day).date())
    except Exception:                                              # noqa: BLE001
        pass
    benchmark = ROOT / "assets/data/meta/cn_benchmark_daily.parquet"
    if benchmark.exists():
        try:
            frame = pd.read_parquet(benchmark, columns=["trade_date"])
            values = pd.to_datetime(frame["trade_date"], errors="coerce").dropna()
            if not values.empty:
                return str(values.max().date())
        except Exception:                                          # noqa: BLE001
            pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 产物探测
# ─────────────────────────────────────────────────────────────────────────────
def _artifact_info(rel_path: str, date_column: str = "trade_date", mtime_only: bool = False) -> dict:
    """单个产物：存在性 / 行数 / 最新日期（CSV/JSON 都尽量猜）。"""
    path = ROOT / rel_path
    info = {"path": rel_path, "exists": path.exists(), "rows": None, "latest_date": None,
            "mtime": None, "bytes": None, "detail": ""}
    if not path.exists():
        return info
    stat = path.stat()
    info["mtime"] = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    info["bytes"] = stat.st_size
    if mtime_only or path.suffix.lower() == ".json":
        return info
    try:
        if path.suffix.lower() == ".csv":
            frame = pd.read_csv(path)
        elif path.suffix.lower() == ".parquet":
            frame = pd.read_parquet(path)
        else:
            return info
        info["rows"] = int(len(frame))
        column = date_column if date_column in frame.columns else None
        if column is None:
            candidates = [c for c in frame.columns if "date" in c.lower()]
            column = candidates[0] if candidates else None
        if column:
            values = pd.to_datetime(frame[column], errors="coerce").dropna()
            if not values.empty:
                info["latest_date"] = str(values.max().date())
    except Exception as exc:                                       # noqa: BLE001
        info["detail"] = f"读取失败：{exc}"
    return info


def _history_latest(rel_dir: str) -> str | None:
    """历史快照目录里最新的 `YYYY-MM-DD.csv` 日期（按文件名，不看 mtime）。"""
    if not rel_dir:
        return None
    folder = ROOT / rel_dir
    if not folder.exists():
        return None
    names = [p.stem for p in folder.glob("*.csv")
             if len(p.stem) == 10 and p.stem[4] == "-" and p.stem[:4].isdigit()]
    return max(names) if names else None


def _history_count(rel_dir: str) -> int:
    if not rel_dir:
        return 0
    folder = ROOT / rel_dir
    if not folder.exists():
        return 0
    return len([p for p in folder.glob("*.csv")
                if len(p.stem) == 10 and p.stem[4] == "-" and p.stem[:4].isdigit()])


def _metrics_from_files(rel_paths, keys: dict[str, str]) -> list[dict]:
    """把一串指标文件合并成一张表（后面的文件覆盖同名字段）。

    为什么要支持多个：一条策略的"它行不行"往往分散在两个产物里 ——
    例：`arm_concept_backtest.json` 有期均/基准/累计/回撤，`arm_concept_robustness.json`
    有前半/后半超额与判定。只看前者会把"半窗为负"的策略显示成 +0.52% 的漂亮数字。
    """
    if isinstance(rel_paths, str):
        rel_paths = [rel_paths]
    merged: dict[str, float] = {}
    for rel in rel_paths or []:
        for row in _metrics_from_file(rel, keys):
            merged[row["key"]] = row["value"]
    return [{"key": key, "label": keys[key], "value": value}
            for key, value in merged.items() if key in keys]


def _metrics_from_file(rel_path: str, keys: dict[str, str]) -> list[dict]:
    """策略自己的回放指标（策略卡上直接显示"它到底行不行"，不用点进别的页面）。

    约定：`metrics_file` 是一个 JSON（如 arm_concept_backtest.json），`metrics_map` 给出
    字段 → 中文名；`metrics_pct` 里的字段按百分数显示。缺文件/缺字段时返回空表，不编造。
    """
    if not rel_path:
        return []
    path = ROOT / rel_path
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = []
    for key, label in keys.items():
        value = payload.get(key)
        if value is None or not isinstance(value, (int, float)):
            continue
        rows.append({"key": key, "label": label, "value": float(value)})
    return rows


def _research_evidence(limit: int = 200) -> list[dict]:
    """`output/research/*/REPORT.md` → 研究证据（标题取第一行 markdown 标题）。"""
    rows: list[dict] = []
    if not RESEARCH_DIR.exists():
        return rows
    for path in sorted(RESEARCH_DIR.glob("*/REPORT.md")):
        title = ""
        try:
            with path.open(encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    line = line.strip()
                    if line.startswith("#"):
                        title = line.lstrip("#").strip()
                        break
        except OSError:
            pass
        rows.append({
            "key": path.parent.name,
            "title": title or path.parent.name,
            "path": str(path.relative_to(ROOT)),
            "mtime": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows[:limit]


# ─────────────────────────────────────────────────────────────────────────────
# 候选登记
# ─────────────────────────────────────────────────────────────────────────────
def load_candidates() -> list[dict]:
    rows: list[dict] = []
    if not CANDIDATE_DIR.exists():
        return rows
    for path in sorted(CANDIDATE_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        payload.setdefault("id", path.stem)
        # 用 _relativize 而不是 relative_to：候选目录被指到仓库外（测试/临时登记）时
        # relative_to 会直接抛 ValueError，整份策略池快照就没了。
        payload["_file"] = _relativize(str(path))
        rows.append(payload)
    return rows


def _slug(text: str) -> str:
    keep = [ch if (ch.isalnum() or ch in "-_") else "-" for ch in text.strip().lower()]
    slug = "".join(keep).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:60] or f"candidate-{datetime.now():%Y%m%d-%H%M%S}"


def _relativize(path: str) -> str:
    """绝对路径 → 仓库相对路径；已经在仓库外的原样返回（不假装它是本仓库的产物）。"""
    if not path:
        return path
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except (ValueError, OSError):
        return path


def register_candidate(payload: dict, overwrite: bool = False) -> Path:
    """登记一个候选策略；同 id 时默认返回已有文件（幂等），`overwrite=True` 覆盖。"""
    if not payload.get("name"):
        raise SystemExit("候选策略必须有 name")
    # 产物/证据路径统一存**仓库相对路径**：页面上的链接与 `git status` 才对得上
    # （挖掘脚本打印的是绝对路径，直接存进去会在策略池里显示一长串 /Users/... ）
    for key in ("artifacts", "evidence"):
        payload[key] = [_relativize(str(item)) for item in payload.get(key) or []]
    ident = _slug(str(payload.get("id") or payload["name"]))
    CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)
    path = CANDIDATE_DIR / f"{ident}.json"
    if path.exists() and not overwrite:
        existing = json.loads(path.read_text(encoding="utf-8"))
        existing["_path"] = str(path)
        existing["_existed"] = True
        return path
    now = datetime.now().isoformat(timespec="seconds")
    payload = {
        "id": ident,
        "name": payload["name"],
        "kind": payload.get("kind", "candidate"),
        "stage": payload.get("stage", "candidate"),
        "owner": payload.get("owner", "agent"),
        "origin": payload.get("origin", "manual"),
        "summary": payload.get("summary", ""),
        "entry": payload.get("entry", ""),
        "metrics": payload.get("metrics", {}),
        "artifacts": payload.get("artifacts", []),
        "evidence": payload.get("evidence", []),
        "created_at": payload.get("created_at", now),
        "updated_at": now,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def set_stage(ident: str, stage: str) -> tuple[Path, dict]:
    if stage not in STAGES:
        raise SystemExit(f"未知阶段 {stage}；可选 {STAGES}")
    CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)
    matches = [p for p in CANDIDATE_DIR.glob("*.json") if p.stem == ident or p.stem == _slug(ident)]
    if not matches:
        raise SystemExit(f"候选池里没有 {ident}（先 --register 登记）")
    path = matches[0]
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["stage"] = stage
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, payload


# ─────────────────────────────────────────────────────────────────────────────
# 组装
# ─────────────────────────────────────────────────────────────────────────────
def build_pool() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(f"缺少策略登记表：{CONFIG_PATH}")
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    reference = latest_trade_date()
    performance_path = ROOT / "output/strategy_pool/performance.json"
    try:
        performance = json.loads(performance_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        performance = {}

    def performance_for(ident: str, latest: str | None = None) -> dict:
        value = dict(performance.get("strategies", {}).get(ident, {}))
        value["stale"] = bool(value) and (
            performance.get("reference_trade_date") != reference or
            bool(latest and value.get("tracking", {}).get("latest_signal_date") != latest))
        return value

    strategies: list[dict] = []
    for item in config.get("strategies", []):
        date_column = item.get("date_column", "trade_date")
        artifacts = [_artifact_info(rel, date_column) for rel in item.get("artifacts", [])]
        history_dir = item.get("history_dir", "")
        history_date = _history_latest(history_dir)
        history_count = _history_count(history_dir)
        dates = [a["latest_date"] for a in artifacts if a["latest_date"]]
        if history_date:
            dates.append(history_date)
        latest = max(dates) if dates else None
        primary = next((a for a in artifacts if a["exists"] and a["rows"]), None) or (artifacts[0] if artifacts else None)
        lag = None
        if latest and reference:
            try:
                lag = int((pd.Timestamp(reference) - pd.Timestamp(latest)).days)
            except Exception:                                      # noqa: BLE001
                lag = None
        stage = item.get("stage", "research")
        fresh = bool(latest) and latest == reference
        strategies.append({
            "id": item["id"],
            "name": item.get("name", item["id"]),
            "group": item.get("group", ""),
            "kind": item.get("kind", "strategy"),
            "stage": stage,
            "stage_label": STAGE_LABEL.get(stage, stage),
            "owner": item.get("owner", ""),
            "tag_class": item.get("tag_class", "dim"),
            "summary": item.get("summary", ""),
            "entry": item.get("entry", ""),
            "schedule": item.get("schedule", ""),
            "ledger_sources": item.get("ledger_sources", []),
            "selection_source": item.get("selection_source", ""),
            "performance": performance_for(item["id"], latest),
            "latest_date": latest,
            "lag_days": lag,
            "fresh": fresh,
            "n_picks": primary["rows"] if primary else None,
            "n_snapshots": history_count,
            "history_dir": history_dir,
            "artifacts": artifacts,
            "metrics": _metrics_from_files(
                item.get("metrics_files") or item.get("metrics_file", ""), item.get("metrics_map", {})),
            "metrics_pct": item.get("metrics_pct", []),
            "health": "ok" if fresh else ("stale" if latest else "missing"),
        })

    candidates = []
    for payload in load_candidates():
        stage = payload.get("stage", "candidate")
        candidates.append({
            "id": payload.get("id"),
            "name": payload.get("name", payload.get("id")),
            "kind": payload.get("kind", "candidate"),
            "stage": stage,
            "stage_label": STAGE_LABEL.get(stage, stage),
            "owner": payload.get("owner", ""),
            "origin": payload.get("origin", ""),
            "summary": payload.get("summary", ""),
            "entry": payload.get("entry", ""),
            "metrics": payload.get("metrics", {}),
            "artifacts": payload.get("artifacts", []),
            "evidence": payload.get("evidence", []),
            "created_at": payload.get("created_at", ""),
            "updated_at": payload.get("updated_at", ""),
            "file": payload.get("_file", ""),
            "selection_source": next((s["selection_source"] for s in strategies
                                      if s["id"] == payload.get("id")), ""),
            "performance": performance_for(payload.get("id", "")),
        })
    candidates.sort(key=lambda c: (STAGES.index(c["stage"]) if c["stage"] in STAGES else 9,
                                   c.get("updated_at") or "", c["id"] or ""))

    evidence = _research_evidence()
    stale = [s["id"] for s in strategies if s["stage"] == "production" and not s["fresh"]]

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "reference_trade_date": reference,
        "config_path": "config/strategies.json",
        "config_updated_at": config.get("updated_at", ""),
        "stages": [{"key": k, "label": STAGE_LABEL[k]} for k in STAGES if k != "nowhere"],
        "strategies": strategies,
        "candidates": candidates,
        "performance_generated_at": performance.get("generated_at"),
        "common_window": performance.get("common_window", {"status": "insufficient_history"}),
        "evidence": evidence,
        "totals": {
            "n_strategies": len(strategies),
            "n_production": sum(1 for s in strategies if s["stage"] == "production"),
            "n_candidates": len(candidates),
            "n_evidence": len(evidence),
            "stale_production": stale,
            "fresh": not stale,
        },
        "notes": [
            "stage=production 的策略由每日自动化跑，必须更新到最新交易日（不达标会在这里点名）。",
            "候选（candidate）来自 LLM 因子挖掘 / agent 研究，需人工用 --set-stage 升格，不会自动进生产。",
            "研究证据（output/research/*/REPORT.md）是策略的来源出处，不是策略本身（不产生每日名单）。",
        ],
    }


def write_pool(payload: dict) -> None:
    POOL_DIR.mkdir(parents=True, exist_ok=True)
    POOL_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# 策略池（基准交易日 {payload['reference_trade_date']}）", "",
             f"生成 {payload['generated_at']}｜生产 {payload['totals']['n_production']} 个"
             f"｜候选 {payload['totals']['n_candidates']} 个"
             f"｜研究证据 {payload['totals']['n_evidence']} 份", "",
             "| 策略 | 分组 | 阶段 | 最新交易日 | 落后 | 名单条数 | 快照 | 回放指标 | 产物 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for item in payload["strategies"]:
        mark = "✅" if item["fresh"] else ("❌" if item["health"] == "stale" else "—")
        artifact_text = "、".join(f"`{a['path']}`" for a in item["artifacts"])
        metric_text = "；".join(
            f"{m['label']} " + (f"{m['value'] * 100:+.2f}%" if m["key"] in (item.get("metrics_pct") or [])
                                else f"{m['value']:.3f}")
            for m in item.get("metrics", [])) or "—"
        lines.append(
            f"| {item['name']} | {item['group']} | {item['stage_label']} | {item['latest_date'] or '—'} {mark} "
            f"| {item['lag_days'] if item['lag_days'] is not None else '—'} "
            f"| {item['n_picks'] if item['n_picks'] is not None else '—'} | {item['n_snapshots']} "
            f"| {metric_text} | {artifact_text} |"
        )
    if payload["candidates"]:
        lines += ["", "## 候选策略", "",
                  "| 候选 | 来源 | 阶段 | 说明 | 证据 |", "|---|---|---|---|---|"]
        for item in payload["candidates"]:
            lines.append(f"| {item['name']} | {item['origin']} | {item['stage_label']} | "
                         f"{(item['summary'] or '').replace('|', '/')} | {', '.join(item['evidence'])} |")
    POOL_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def print_table(payload: dict) -> None:
    print(f"基准交易日 {payload['reference_trade_date']}")
    for item in payload["strategies"]:
        flag = "OK " if item["fresh"] else ("STALE" if item["health"] == "stale" else "MISS")
        print(f"  [{flag:5}] {item['id']:14} {item['stage']:10} latest={item['latest_date'] or '—':10} "
              f"lag={item['lag_days']} picks={item['n_picks']}")
    for item in payload["candidates"]:
        print(f"  [cand ] {item['id']:14} {item['stage']:10} {item['name']}")
    print(f"证据 {payload['totals']['n_evidence']} 份；"
          f"生产不达标 {payload['totals']['stale_production'] or '无'}")


def main() -> int:
    ap = argparse.ArgumentParser(description="策略池同步（声明式登记 + 运行时状态 + 候选）")
    ap.add_argument("--register", metavar="FILE", help="登记候选策略（JSON 文件）")
    ap.add_argument("--register-json", metavar="JSON", help="登记候选策略（内联 JSON）")
    ap.add_argument("--overwrite", action="store_true", help="登记时覆盖同 id 候选")
    ap.add_argument("--set-stage", nargs=2, metavar=("ID", "STAGE"), help="改候选阶段")
    ap.add_argument("--print", action="store_true", help="打印表格")
    ap.add_argument("--strict", action="store_true", help="生产策略不新鲜时退出码 1")
    args = ap.parse_args()

    if args.register or args.register_json:
        if args.register:
            payload = json.loads(Path(args.register).read_text(encoding="utf-8"))
        else:
            payload = json.loads(args.register_json)
        path = register_candidate(payload, overwrite=args.overwrite)
        print(f"候选已登记：{path.relative_to(ROOT)}")
        if str(payload.get("stage")) == "production":
            print("提醒：升到 production 需要把该策略补进 config/strategies.json（本脚本不自动改配置）。")
    if args.set_stage:
        path, payload = set_stage(args.set_stage[0], args.set_stage[1])
        print(f"{payload['id']} → {payload['stage']}（{path.relative_to(ROOT)}）")
        if payload["stage"] == "production":
            print("提醒：请把该策略补进 config/strategies.json 的 strategies[]，并加进每日自动化与新鲜度体检。")

    pool = build_pool()
    write_pool(pool)
    if args.print:
        print_table(pool)
    else:
        print(f"策略池已同步：{POOL_PATH.relative_to(ROOT)}（生产 {pool['totals']['n_production']} / "
              f"候选 {pool['totals']['n_candidates']} / 证据 {pool['totals']['n_evidence']}）")
    if args.strict and pool["totals"]["stale_production"]:
        print(f"不达标的策略：{pool['totals']['stale_production']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
