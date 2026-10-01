#!/usr/bin/env python
"""分块、可续跑的特征重算驱动（R12 对齐用）。

为什么分块：本机长任务（>20~30 分钟）会被外部清理掉，一次性跑 5,221 只会中断在中间。
这个驱动每次只算一个 chunk（默认 250 只）、把进度写进 JSON，然后退出；
由 launchd（`StartInterval`）每隔几分钟再触发一次，直到全部完成后执行收尾
（clean_panel → model_scores → 写 .done），之后每次触发都快速空转。

用法：
  PYTHONPATH=. .venv/bin/python scripts/recompute_features_chunk.py --chunk-size 250
  PYTHONPATH=. .venv/bin/python scripts/recompute_features_chunk.py --chunk-size 250 --finalize
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "output/verification/cn_base_data_repair_20260926"
PROGRESS = OUT / "realign_progress.json"
DONE = OUT / "realign.done"


def load_progress(config: dict, codes: list[str]) -> dict:
    if PROGRESS.is_file():
        try:
            state = json.loads(PROGRESS.read_text())
            if state.get("config") == config and state.get("codes") == len(codes):
                return state
        except Exception:
            pass
    return {"config": config, "codes": len(codes), "next_index": 0, "computed": 0, "started": time.strftime("%F %T")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk-size", type=int, default=250)
    ap.add_argument("--write-days", type=int, default=30)
    ap.add_argument("--warmup-days", type=int, default=515)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--finalize", action="store_true", help="全部算完后执行 clean_panel + model_scores")
    ap.add_argument("--state-dir", default=str(OUT),
                    help="进度/完成标记目录（默认沿用 20260926 那轮；修复重跑请给独立目录）")
    args = ap.parse_args()

    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    progress_path = state_dir / "realign_progress.json"
    done_path = state_dir / "realign.done"
    global PROGRESS, DONE
    PROGRESS, DONE = progress_path, done_path

    OUT.mkdir(parents=True, exist_ok=True)
    if DONE.is_file():
        print("realign already done:", DONE.read_text().strip()[:120])
        return 0

    from data.ingest.service import MarketDataService, filter_cn_equity_codes

    cfg = tomllib.load(open(ROOT / "config/cn_pipeline.toml", "rb"))
    p = cfg["pipeline"]
    svc = MarketDataService(base_dir=str(ROOT / "assets/data"))
    all_codes = [str(c) for c in svc.warehouse.get_all_stock_codes(market="CN")]
    codes, dropped = filter_cn_equity_codes(all_codes)  # 只算真股票，指数/基金不参与特征重算
    config = {"write_days": args.write_days, "warmup_days": args.warmup_days, "factor_set": p["factor_set"]}
    state = load_progress(config, codes)

    start = int(state["next_index"])
    chunk = codes[start:start + args.chunk_size]
    if chunk:
        t0 = time.time()
        summary = svc.generate_factor_set(
            stock_codes=chunk, factor_set=p["factor_set"], market="CN", frequency="daily",
            adjust=p.get("adjust", "qfq"), days=args.write_days, warmup_days=args.warmup_days,
            max_workers=args.workers, show_progress=False,
            config=cfg.get("factor_config") or None, force_recompute=True,
        )
        state["next_index"] = start + len(chunk)
        state["computed"] = int(state.get("computed", 0)) + len(chunk)
        state["last_chunk"] = {"start_index": start, "size": len(chunk), "elapsed_s": round(time.time() - t0, 1),
                               "rows_written": summary.get("rows_written"), "at": time.strftime("%F %T")}
        PROGRESS.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"chunk {start}..{state['next_index']}/{len(codes)} done in {state['last_chunk']['elapsed_s']}s "
              f"rows={summary.get('rows_written'):,} (non-equity skipped={len(dropped)})", flush=True)

    if state["next_index"] >= len(codes) and args.finalize:
        for stage in ("clean_panel", "model_scores"):
            log = state_dir / f"realign_{stage}.log"
            with open(log, "w", encoding="utf-8") as fh:
                completed = subprocess.run(
                    [sys.executable, "scripts/run_cn_pipeline.py", "--stage", stage],
                    cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT,
                    # 之前这里把 env 白名单化成只有 PATH/PYTHONPATH，CLICKHOUSE_* 全被剥掉，
                    # 收尾的 clean_panel/model_scores 因此也只能读 Parquet 镜像。继承父进程环境。
                    env={**os.environ, "PYTHONPATH": "."},
                )
            print(f"finalize {stage} exit={completed.returncode} -> {log}", flush=True)
            if completed.returncode:
                state["finalize_failed"] = stage
                PROGRESS.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
                return 1
        DONE.write_text(f"completed {time.strftime('%F %T')} | {json.dumps(state, ensure_ascii=False)}", encoding="utf-8")
        print("realign complete:", DONE.read_text()[:150], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
