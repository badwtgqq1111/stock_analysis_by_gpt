#!/usr/bin/env bash
# 估值特征修复：用修复后的（带 ClickHouse 环境变量的）进程重算最近 30 个交易日的特征，
# 让新鲜的估值观测以最新 ingest_time 覆盖 9/26 那批冻结镜像写出的陈旧行。
#
# 背景：launchd 任务的 EnvironmentVariables 缺 CLICKHOUSE_*，MarketDataWarehouse 静默
# 退化成只读 Parquet 镜像（8/10 冻结、市值单位"元"），2026-09-26 16:09 的对齐重算把
# 000001.SZ 等 5,205 只的 valuation_market_cap_age_td 从 1 覆盖成 33。
#
# 分块可续跑：每块 400 只，进度写进 --state-dir，被中断后重跑会从断点继续。
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
EV="output/verification/valuation_repair_20260927"
mkdir -p "$EV"
export PYTHONPATH=.
for i in $(seq 1 60); do
  .venv/bin/python scripts/recompute_features_chunk.py --chunk-size 400 --warmup-days 515 \
      --write-days 30 --workers 8 --state-dir "$EV" >> "$EV/chunks.log" 2>&1
  rc=$?
  if [ $rc -ne 0 ]; then
    echo "$(date '+%F %T') FAILED rc=$rc —— 见 chunks.log 尾部" >> "$EV/driver.log"
    exit $rc
  fi
  # 注意：必须取退出码，不能把 python 的 stdout 当结果比较（首版写成 done_ok=输出文本，
  # 结果跑完后仍继续空转直到循环上限）。
  .venv/bin/python - <<'PY' > /dev/null 2>&1
import json, pathlib
p = pathlib.Path("output/verification/valuation_repair_20260927/realign_progress.json")
try:
    s = json.loads(p.read_text())
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if int(s.get("next_index", 0)) >= int(s.get("codes", 10**9)) else 1)
PY
  if [ $? -eq 0 ]; then
    echo "$(date '+%F %T') ALL DONE" >> "$EV/driver.log"
    exit 0
  fi
done
echo "$(date '+%F %T') 达到循环上限（60 轮）" >> "$EV/driver.log"
