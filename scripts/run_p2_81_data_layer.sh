#!/usr/bin/env bash
# P2.81 数据层一键重建：市场因子 + PIT 板块因子 + 策略族特征 + 元数据集。
# 行业成员历史默认不重抓（约 3 分钟、依赖中继）；需要时加 --with-industry。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${PY:-.venv/bin/python}"
START="${START:-2016-01-01}"
END="${END:-2026-12-31}"
WITH_INDUSTRY=0
WITH_IFIND=0
WITH_IFIND_DAILY=0

for arg in "$@"; do
  case "$arg" in
    --with-industry) WITH_INDUSTRY=1 ;;
    --with-ifind) WITH_IFIND=1 ;;
    --with-ifind-daily) WITH_IFIND_DAILY=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

if [ "$WITH_IFIND" = "1" ]; then
  echo "[p2.81] iFinD monthly moneyflow 2016-2024"
  "$PY" scripts/fetch_ifind_moneyflow_monthly.py --start 2016-01-01 --end 2024-12-31
  "$PY" scripts/build_ifind_moneyflow_monthly_features.py
fi

if [ "$WITH_IFIND_DAILY" = "1" ]; then
  echo "[p2.81] iFinD daily moneyflow 2024-01 ~ 2024-08"
  "$PY" scripts/fetch_ifind_moneyflow_history.py --start 2024-01-01 --end 2024-08-22 --sleep 0.15
  "$PY" scripts/build_ifind_moneyflow_daily_features.py
fi

if [ "$WITH_INDUSTRY" = "1" ]; then
  echo "[p2.81] sync SW2021 industry history"
  "$PY" scripts/sync_cn_sw2021_history.py
fi

echo "[p2.81] market factors $START ~ $END"
"$PY" scripts/build_market_factors.py --start "$START" --end "$END"

echo "[p2.81] sector factors $START ~ $END"
"$PY" scripts/build_sector_factors.py --start "$START" --end "$END"

echo "[p2.81] strategy family features"
"$PY" scripts/build_strategy_family_features.py

echo "[p2.81] meta dataset"
"$PY" scripts/build_meta_dataset.py --start "$START" --end "$END"

echo "[p2.81] done"
