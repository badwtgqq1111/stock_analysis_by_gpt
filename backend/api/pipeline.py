"""CN 数据流水线 + preselect/PK 结果的只读 API，以及流水线触发入口。

设计取舍（2026-10-07）：
  * 展示面直接读**流水线自己写的产物**（`output/results_cn/*.csv`、`output/pipeline_reports/*.json`），
    不重新计算 —— 前端看到的必须和命令行跑出来的完全一致；
  * 触发面 = 子进程 `scripts/run_cn_pipeline.py --stage X`，stdout 流式回显、同一时刻只允许一个；
  * **破坏性保护**：preselection/pk/selection 等阶段会覆盖 `output/results_cn/*`，
    必须显式带 `confirm_overwrite=true` 才允许启动（避免误点把当日名单冲掉）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = ROOT / "output" / "results_cn"
REPORTS_DIR = ROOT / "output" / "pipeline_reports"
SCRIPT = ROOT / "scripts" / "run_cn_pipeline.py"
DEFAULT_CONFIG = ROOT / "config" / "cn_pipeline.toml"
RUN_ROOT = ROOT / "output" / "pipeline_console_runs"

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])
_RUN_LOCK = threading.Lock()

# 与 scripts/run_cn_pipeline.py --stage 的 choices 保持一致（该脚本 1049 行）
STAGE_CHOICES = [
    "all", "daily_bars", "moneyflow", "cyq_chips", "intraday_bars", "fundamental", "alternative",
    "strategy_labels", "features", "regime", "xiaomkt_rotation", "sleeve_features",
    "sleeve_diagnostics", "sleeve_train", "sleeve_combine", "sleeve_budgets", "sleeve_targets",
    "clean_panel", "lightgbm", "transformer", "cnn", "model_scores", "preselection", "selection",
    "pk", "paper_outcomes", "paper_account", "exits", "graph_temporal", "oos_predictions",
    "model_comparison", "arm_selection", "arm_concept",
]
# 会写 output/results_cn/* 的阶段 → 必须确认后才可以跑
OVERWRITE_STAGES = {"preselection", "pk", "selection", "sleeve_targets", "paper_account",
                    "paper_outcomes", "exits", "all"}

# 关键产物（stage → 相对路径），用于"新鲜度"卡片；不存在就显示 missing
STAGE_ARTIFACTS: dict[str, list[str]] = {
    "features": ["assets/data/feature/clean_feature_panel"],
    "clean_panel": ["assets/data/meta/cn_sleeve_features_long.parquet"],
    "lightgbm": ["output/results_cn/cn_ensemble_explanations.json"],
    "model_scores": ["output/results_cn/cn_ensemble_preselected.csv"],
    "preselection": ["output/results_cn/cn_ensemble_preselected.csv",
                     "output/results_cn/cn_ensemble_preselection_config_latest.json"],
    "pk": ["output/results_cn/cn_ensemble_selected.csv",
           "output/results_cn/cn_ensemble_pk_config_latest.json"],
    "sleeve_targets": ["output/results_cn/sleeve_preselected.csv"],
    "exits": ["output/results_cn/cn_exit_plan.csv"],
    "arm_selection": ["output/results_cn/arm_latest_selection.csv",
                      "output/results_cn/arm_latest_selection.json"],
    "arm_concept": ["output/results_cn/arm_concept_selection.csv",
                    "output/results_cn/arm_concept_selection.json"],
}
SELECTION_CANDIDATES = [
    RESULTS_DIR,
    RESULTS_DIR / "sleeve_pk",
    RESULTS_DIR / "replay_20260918",
]


@dataclass
class Run:
    id: str
    stage: str
    config: str
    extra: list[str] = field(default_factory=list)
    status: str = "pending"            # pending | running | done | failed
    log: list[str] = field(default_factory=list)
    returncode: int | None = None
    started_at: str = ""
    finished_at: str = ""


RUNS: dict[str, Run] = {}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _path_info(rel: str) -> dict:
    path = ROOT / rel
    info = {"path": rel, "exists": path.exists()}
    if not path.exists():
        return info
    if path.is_dir():
        files = sorted(path.rglob("*.parquet"))
        latest = max((f.stat().st_mtime for f in files), default=path.stat().st_mtime)
        info.update({"kind": "dir", "files": len(files), "bytes": sum(f.stat().st_size for f in files),
                     "mtime": datetime.fromtimestamp(latest).strftime("%Y-%m-%d %H:%M")})
        return info
    stat = path.stat()
    info.update({"kind": "file", "bytes": stat.st_size,
                 "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")})
    if path.suffix == ".csv":
        try:
            info["rows"] = int(len(pd.read_csv(path, usecols=[0])))
        except Exception:
            pass
    return info


def _latest_report() -> dict | None:
    if not REPORTS_DIR.exists():
        return None
    files = sorted(REPORTS_DIR.glob("cn_pipeline_*.json"))
    if not files:
        return None
    path = files[-1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    data["_file"] = path.name
    return data


def _recent_reports(limit: int = 8) -> list[dict]:
    out = []
    if not REPORTS_DIR.exists():
        return out
    for path in sorted(REPORTS_DIR.glob("cn_pipeline_*.json"), reverse=True)[:limit]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        stages = data.get("stages", [])
        seconds = data.get("seconds")
        if seconds is None and data.get("started_at"):
            # 报告 JSON 里没有 seconds 时，用"报告落盘时间 − 开始时间"近似运行时长
            try:
                started = datetime.strptime(str(data["started_at"]), "%Y-%m-%dT%H:%M:%S")
                seconds = max(0.0, (path.stat().st_mtime - started.timestamp()))
            except (ValueError, OSError):
                seconds = None
        out.append({"file": path.name,
                    "started_at": data.get("started_at"),
                    "seconds": seconds,
                    "finished_at": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "result": data.get("result"),
                    "config": data.get("config"),
                    "stage_status": {s.get("name"): s.get("status") for s in stages if isinstance(s, dict)},
                    "failed": [s.get("name") for s in stages
                               if isinstance(s, dict) and s.get("status") == "failed"]})
    return out


def _pick_selection_dir() -> tuple[Path, list[dict]]:
    """在多个产物目录里选"最新的那一套"（同日历下以 preselected 的 mtime 为准）。"""
    seen = []
    for base in SELECTION_CANDIDATES:
        pre = base / "cn_ensemble_preselected.csv"
        sel = base / "cn_ensemble_selected.csv"
        if pre.exists():
            seen.append({"dir": str(base.relative_to(ROOT)), "mtime": pre.stat().st_mtime,
                         "mtime_text": datetime.fromtimestamp(pre.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                         "rows": int(len(pd.read_csv(pre, usecols=[0])))})
    if not seen:
        return RESULTS_DIR, []
    newest = max(seen, key=lambda x: x["mtime"])
    base = ROOT / newest["dir"]
    return base, sorted(seen, key=lambda x: -x["mtime"])


def _frame_payload(path: Path, columns: list[str] | None = None, limit: int = 60,
                   frame: pd.DataFrame | None = None) -> dict:
    if not path.exists():
        return {"exists": False, "path": str(path.relative_to(ROOT)), "rows": [], "columns": []}
    if frame is None:
        frame = pd.read_csv(path)
    if columns:
        keep = [c for c in columns if c in frame.columns]
        frame = frame[keep]
    rows = []
    for _, row in frame.head(limit).iterrows():
        item = {}
        for col, val in row.items():
            if pd.isna(val):
                item[col] = None
            elif isinstance(val, (int, float)):
                item[col] = round(float(val), 4)
            else:
                text = str(val)
                item[col] = text if len(text) <= 120 else text[:117] + "…"
        rows.append(item)
    return {"exists": True, "path": str(path.relative_to(ROOT)), "rows": rows,
            "columns": list(frame.columns), "count": int(len(frame)),
            "mtime": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")}


PRE_COLS = ["trade_date", "stock_code", "stock_name", "rank", "model_score", "ensemble_score",
            "lightgbm_score", "transformer_score", "gate_tilt", "selection_channel",
            "selection_sleeve", "strategy_id", "constraint_status", "target_weight", "regime"]
SEL_COLS = PRE_COLS + ["industry_l1", "market_cap", "daily_turnover", "portfolio_mode"]


def _names(codes: list[str]) -> dict[str, str]:
    """A 股名称：优先用 CN 基础表（ts_code → name），拿不到再退回通用接口。"""
    out: dict[str, str] = {}
    basic = ROOT / "assets/data/meta/cn_stock_basic.parquet"
    if basic.exists() and codes:
        try:
            table = pd.read_parquet(basic, columns=["ts_code", "name"])
            mapping = dict(zip(table["ts_code"].astype(str), table["name"].astype(str)))
            out = {c: mapping[c] for c in codes if c in mapping}
        except Exception:
            out = {}
    missing = [c for c in codes if c not in out]
    if missing:
        try:
            from backend.services.data_service import get_stock_names_batch
            for code, name in get_stock_names_batch(missing).items():
                if name:
                    out[code] = name
        except Exception:
            pass
    return out


@router.get("/overview")
def overview() -> dict:
    """阶段目录 + 产物新鲜度 + 最近运行报告（前端"流水线"页的主数据）。"""
    stages_cfg: list[str] = []
    if DEFAULT_CONFIG.exists():
        for line in DEFAULT_CONFIG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("stages") and "=" in line:
                inside = line.split("=", 1)[1].strip().strip("[]")
                stages_cfg = [s.strip().strip('"\'') for s in inside.split(",") if s.strip()]
                break
    latest = _latest_report() or {}
    latest_status = {s.get("name"): s for s in latest.get("stages", []) if isinstance(s, dict)}
    catalog = []
    for stage in STAGE_CHOICES:
        if stage == "all":
            continue
        artifacts = [_path_info(rel) for rel in STAGE_ARTIFACTS.get(stage, [])]
        report_stage = latest_status.get(stage, {})
        catalog.append({
            "stage": stage,
            "configured": stage in stages_cfg,
            "overwrites_results": stage in OVERWRITE_STAGES,
            "last_status": report_stage.get("status"),
            "last_detail": (report_stage.get("detail") or "")[:200] or None,
            "artifacts": artifacts,
        })
    return {
        "config": str(DEFAULT_CONFIG.relative_to(ROOT)),
        "stages_configured": stages_cfg,
        "stage_catalog": catalog,
        "latest_report": {"file": latest.get("_file"), "started_at": latest.get("started_at"),
                          "result": latest.get("result"), "seconds": latest.get("seconds"),
                          "config": latest.get("config"),
                          "stages": [{"name": s.get("name"), "status": s.get("status"),
                                      "detail": (s.get("detail") or "")[:300] or None}
                                     for s in latest.get("stages", []) if isinstance(s, dict)]},
        "recent_reports": _recent_reports(),
        "runs": [_run_summary(r) for r in sorted(RUNS.values(), key=lambda x: x.started_at, reverse=True)],
    }


@router.get("/selection")
def selection() -> dict:
    """preselect（候选池）与 pk（最终名单）两份结果 + 进出差异 + 分布统计。"""
    base, dirs = _pick_selection_dir()
    pre_path = base / "cn_ensemble_preselected.csv"
    sel_path = base / "cn_ensemble_selected.csv"
    pre = pd.read_csv(pre_path) if pre_path.exists() else pd.DataFrame()
    sel = pd.read_csv(sel_path) if sel_path.exists() else pd.DataFrame()
    # 候选池常不含名称，补上
    if not pre.empty and "stock_name" not in pre.columns:
        pre["stock_name"] = pre["stock_code"].astype(str).map(_names(list(pre["stock_code"].astype(str))))
    if not sel.empty and "stock_name" not in sel.columns:
        sel["stock_name"] = sel["stock_code"].astype(str).map(_names(list(sel["stock_code"].astype(str))))
    pre_codes = set(pre["stock_code"].astype(str)) if not pre.empty else set()
    sel_codes = set(sel["stock_code"].astype(str)) if not sel.empty else set()

    def dist(frame: pd.DataFrame, col: str) -> dict:
        if frame.empty or col not in frame.columns:
            return {}
        return {str(k): int(v) for k, v in frame[col].astype(str).value_counts().head(12).items()}

    stats = {
        "preselected_count": int(len(pre)), "selected_count": int(len(sel)),
        "by_channel": dist(pre, "selection_channel"),
        "by_sleeve": dist(pre, "selection_sleeve"),
        "by_strategy": dist(pre, "strategy_id"),
        "by_regime": dist(pre, "regime"),
        "selected_by_constraint": dist(sel, "constraint_status"),
        "selected_by_portfolio_mode": dist(sel, "portfolio_mode"),
        "model_score": {}, "trade_date": None,
    }
    if not pre.empty and "model_score" in pre.columns:
        s = pd.to_numeric(pre["model_score"], errors="coerce").dropna()
        if len(s):
            stats["model_score"] = {"min": round(float(s.min()), 3), "p50": round(float(s.median()), 3),
                                    "max": round(float(s.max()), 3), "mean": round(float(s.mean()), 3)}
    if not pre.empty and "trade_date" in pre.columns:
        stats["trade_date"] = str(pre["trade_date"].iloc[0])
    # 研究手臂（arm_selection 阶段）：单独一块返回，前端与生产名单并列展示
    arm_path = RESULTS_DIR / "arm_latest_selection.csv"
    arm_cfg = RESULTS_DIR / "arm_latest_selection.json"
    arm_meta = {}
    if arm_cfg.exists():
        try:
            arm_meta = json.loads(arm_cfg.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            arm_meta = {}
    arm_cols = ["rank", "stock_code", "name", "board", "industry", "score", "last_close",
                "tradable", "limit_up", "lots_at_capital", "planned_amount", "in_portfolio",
                "target_weight", "pit_name"]

    # 概念/板块手臂（arm_concept 阶段）：与 base_v1 手臂**并列**返回，前端各占一个 tab
    concept_path = RESULTS_DIR / "arm_concept_selection.csv"
    concept_cfg = RESULTS_DIR / "arm_concept_selection.json"
    concept_meta = {}
    if concept_cfg.exists():
        try:
            concept_meta = json.loads(concept_cfg.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            concept_meta = {}
    concept_backtest = {}
    concept_bt_path = RESULTS_DIR / "arm_concept_backtest.json"
    if concept_bt_path.exists():
        try:
            concept_backtest = json.loads(concept_bt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            concept_backtest = {}
    concept_cols = ["rank", "stock_code", "name", "board", "industry", "score", "concept_score",
                    "rev_5d", "last_close", "tradable", "limit_up", "lots_at_capital",
                    "planned_amount", "in_portfolio", "target_weight", "pit_name"]

    return {
        "source_dir": str(base.relative_to(ROOT)),
        "candidate_dirs": dirs,
        "arm": {"payload": _frame_payload(arm_path, arm_cols), "meta": arm_meta},
        "arm_concept": {"payload": _frame_payload(concept_path, concept_cols),
                        "meta": concept_meta, "backtest": concept_backtest},
        "stats": stats,
        "preselected": _frame_payload(pre_path, PRE_COLS, frame=pre),
        "selected": _frame_payload(sel_path, SEL_COLS, frame=sel),
        "diff": {
            "entered": sorted(sel_codes - pre_codes),
            "exited": sorted(pre_codes - sel_codes),
            "kept": sorted(sel_codes & pre_codes),
            "entered_names": _names(sorted(sel_codes - pre_codes)),
            "exited_names": _names(sorted(pre_codes - sel_codes)),
        },
    }


class RunRequest(BaseModel):
    stage: str
    config: str = ""
    confirm_overwrite: bool = False
    extra_args: list[str] = []


def _run_summary(run: Run) -> dict:
    return {"run_id": run.id, "stage": run.stage, "status": run.status, "config": run.config,
            "returncode": run.returncode, "started_at": run.started_at, "finished_at": run.finished_at,
            "log_lines": len(run.log)}


def _execute(run: Run) -> None:
    config = Path(run.config)
    cmd = [sys.executable, str(SCRIPT), "--stage", run.stage]
    if config.exists():
        cmd += ["--config", str(config)]
    cmd += list(run.extra)
    with _RUN_LOCK:
        run.status = "running"
        run.started_at = _now()
        try:
            proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1)
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    run.log.append(line)
            run.returncode = proc.wait(timeout=60)
            run.status = "done" if run.returncode == 0 else "failed"
        except Exception as exc:                      # 不让异常吞掉运行记录
            run.status = "failed"
            run.log.append(f"[console] {type(exc).__name__}: {exc}")
        finally:
            run.finished_at = _now()
            (RUN_ROOT / f"{run.id}.log").write_text("\n".join(run.log), encoding="utf-8")


@router.post("/runs")
def create_run(req: RunRequest) -> dict:
    if req.stage not in STAGE_CHOICES:
        raise HTTPException(status_code=400, detail=f"未知阶段：{req.stage}")
    if any(r.status in ("pending", "running") for r in RUNS.values()):
        raise HTTPException(status_code=409, detail="已有流水线任务在跑，等它结束或先看日志")
    if req.stage in OVERWRITE_STAGES and not req.confirm_overwrite:
        raise HTTPException(status_code=400,
                            detail=f"阶段 {req.stage} 会覆盖 output/results_cn/*，需要 confirm_overwrite=true")
    run = Run(id=uuid.uuid4().hex[:12], stage=req.stage,
              config=req.config or str(DEFAULT_CONFIG), extra=list(req.extra_args))
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    RUNS[run.id] = run
    threading.Thread(target=_execute, args=(run,), daemon=True).start()
    return {"run_id": run.id, "status": run.status, "overwrites_results": req.stage in OVERWRITE_STAGES}


@router.get("/runs")
def list_runs() -> list[dict]:
    return [_run_summary(r) for r in sorted(RUNS.values(), key=lambda x: x.started_at, reverse=True)]


@router.get("/runs/{run_id}")
def get_run(run_id: str, tail: int = 400) -> dict:
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return {**_run_summary(run), "cmd_stage": run.stage, "log": run.log[-max(1, tail):]}
