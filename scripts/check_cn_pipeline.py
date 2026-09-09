#!/usr/bin/env python3
"""Read-only preflight checks for the CN data and selection pipeline."""

from __future__ import annotations

import argparse
import importlib
import os
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
COMMANDS = (
    "sync-cn",
    "refresh-cn-stock-info",
    "refresh-cn-valuation-history",
    "refresh-cn-financial-metrics",
    "backfill-cn-industry",
    "generate-factors",
    "select",
    "cn-coverage-check",
)
REQUIRED_MODULES = (
    "akshare",
    "baostock",
    "clickhouse_connect",
    "lightgbm",
    "numpy",
    "pandas",
    "pyarrow",
    "sklearn",
    "talib",
    "tqdm",
)


@dataclass
class Check:
    name: str
    status: str
    detail: str


class Reporter:
    def __init__(self) -> None:
        self.results: list[Check] = []

    def add(self, name: str, status: str, detail: str) -> None:
        self.results.append(Check(name, status, detail))
        print(f"[{status.upper():4}] {name}: {detail}")

    @property
    def failed(self) -> bool:
        return any(result.status == "fail" for result in self.results)


def check(condition: bool, name: str, ok: str, failed: str, reporter: Reporter) -> None:
    reporter.add(name, "ok" if condition else "fail", ok if condition else failed)


def check_imports(reporter: Reporter) -> None:
    missing = []
    versions = []
    for module_name in REQUIRED_MODULES:
        try:
            module = importlib.import_module(module_name)
            versions.append(f"{module_name}={getattr(module, '__version__', 'installed')}")
        except Exception as exc:  # Import errors often carry useful binary-loader details.
            missing.append(f"{module_name} ({exc})")
    check(
        not missing,
        "Python dependencies",
        ", ".join(versions),
        "Missing or broken modules: " + "; ".join(missing),
        reporter,
    )


def check_cli(reporter: Reporter) -> None:
    failures = []
    for command in COMMANDS:
        completed = subprocess.run(
            [sys.executable, "run.py", command, "--help"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            failures.append(f"{command}: {completed.stderr.strip() or completed.stdout.strip()}")
    check(
        not failures,
        "CN command parsing",
        f"{len(COMMANDS)} commands accept --help",
        "; ".join(failures),
        reporter,
    )


def check_clickhouse(reporter: Reporter) -> None:
    host = os.environ.get("CLICKHOUSE_HOST")
    if not host:
        reporter.add("ClickHouse", "warn", "not configured; the pipeline will use local Parquet storage")
        return

    port = int(os.environ.get("CLICKHOUSE_PORT") or os.environ.get("CLICKHOUSE_HTTP_PORT", "8123"))
    try:
        with socket.create_connection((host, port), timeout=3):
            pass
        from clickhouse_connect import get_client

        client = get_client(
            host=host,
            port=port,
            username=os.environ.get("CLICKHOUSE_USER", "default"),
            password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
            database=os.environ.get("CLICKHOUSE_DATABASE", "quant"),
            connect_timeout=3,
            send_receive_timeout=10,
        )
        client.command("SELECT 1")
        reporter.add("ClickHouse", "ok", f"{host}:{port} query succeeded")
    except Exception as exc:
        reporter.add("ClickHouse", "fail", f"configured but unusable: {exc}")


def check_online_sources(reporter: Reporter) -> None:
    def probe_baostock() -> str:
        from data.ingest.providers.cn_baostock import BaoStockSession, baostock_result_to_frame, bs

        with BaoStockSession(verbose=False):
            frame = baostock_result_to_frame(bs.query_history_k_data_plus(
                "sh.600000",
                "date,code,close",
                start_date="2026-01-01",
                end_date="2026-01-31",
                frequency="d",
                adjustflag="2",
            ))
        if frame.empty:
            raise RuntimeError("returned no sample rows")
        return f"BaoStock sample rows={len(frame)}"

    def probe_tencent() -> str:
        from data.ingest.providers.cn_history import CNHistoryDataFetcher

        frame = CNHistoryDataFetcher("600000.SH", data_source="tencent", verbose=False).fetch(
            period="daily", start_date="2026-01-01", end_date="2026-01-31", adjust="qfq"
        )
        if frame is None or frame.empty:
            raise RuntimeError("returned no sample rows")
        return f"Tencent sample rows={len(frame)}"

    def probe_eastmoney() -> str:
        from data.ingest.providers.cn_valuation_history import CNEastmoneyValuationHistoryFetcher

        frame = CNEastmoneyValuationHistoryFetcher("600000.SH", adjust="qfq", verbose=False).fetch(
            start_date="2026-01-01", end_date="2026-01-31"
        )
        if frame is None or frame.empty:
            raise RuntimeError("returned no sample rows")
        return f"Eastmoney sample rows={len(frame)}"

    for name, probe in (("BaoStock online source", probe_baostock), ("Tencent online source", probe_tencent), ("Eastmoney online source", probe_eastmoney)):
        try:
            reporter.add(name, "ok", probe())
        except Exception as exc:
            # Public market endpoints can be temporarily rate-limited or slow;
            # the result remains visible without blocking local deployment.
            reporter.add(name, "warn", str(exc))


def check_coverage(min_rows: int, reporter: Reporter) -> None:
    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=str(PROJECT_ROOT / "assets" / "data"), data_source="baostock")
    try:
        report = service.cn_backtest_coverage_report(min_ohlcv_rows=min_rows)
    finally:
        service.close()

    reasons = report["blocking_reasons"]
    covered = report["ohlcv"]["covered_stock_count"]
    total = report["stock_count"]
    detail = (
        f"stocks={total}, ohlcv={covered}/{total}, "
        f"stock_info={report['stock_info']['row_count']}, "
        f"valuation={report['financial']['valuation_stock_count']}, "
        f"financial={report['financial']['financial_stock_count']}, "
        f"features={report['features']['stock_count']}"
    )
    if report["backtest_ready"]:
        reporter.add("CN data coverage", "ok", detail)
    else:
        reporter.add("CN data coverage", "warn", detail + "; blockers=" + ",".join(reasons))


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only preflight checks for the CN data pipeline.")
    parser.add_argument("--skip-online", action="store_true", help="Skip live BaoStock/Tencent/Eastmoney probes.")
    parser.add_argument("--min-free-gb", type=float, default=20.0, help="Warn below this free-space threshold.")
    parser.add_argument("--min-ohlcv-rows", type=int, default=120, help="Coverage threshold passed to cn-coverage-check.")
    parser.add_argument("--strict", action="store_true", help="Treat warnings, including incomplete coverage, as failures.")
    args = parser.parse_args()

    os.chdir(PROJECT_ROOT)
    reporter = Reporter()
    print(f"Project: {PROJECT_ROOT}")
    check(sys.version_info >= (3, 10), "Python version", sys.version.split()[0], "Python 3.10+ is required", reporter)
    check((PROJECT_ROOT / "run.py").is_file(), "Project entry point", "run.py found", "run.py is missing", reporter)
    check(
        (PROJECT_ROOT.parent / "akshare" / "akshare").is_dir(),
        "Local AkShare source",
        "../akshare is available",
        "../akshare is missing; pyproject.toml declares it as an editable uv source",
        reporter,
    )
    check_imports(reporter)
    check_cli(reporter)
    check_clickhouse(reporter)

    free_gb = shutil.disk_usage(PROJECT_ROOT).free / 1024 ** 3
    reporter.add(
        "Free disk space",
        "ok" if free_gb >= args.min_free_gb else "warn",
        f"{free_gb:.1f} GiB available (recommended >= {args.min_free_gb:.1f} GiB)",
    )
    workers = os.cpu_count() or 1
    reporter.add(
        "Worker capacity",
        "ok" if workers >= 12 else "warn",
        f"logical CPUs={workers}; requested sync worker count=12",
    )

    if args.skip_online:
        reporter.add("Online sources", "warn", "skipped by --skip-online")
    else:
        check_online_sources(reporter)
    check_coverage(args.min_ohlcv_rows, reporter)

    warnings = sum(result.status == "warn" for result in reporter.results)
    failures = sum(result.status == "fail" for result in reporter.results)
    print(f"\nSummary: failures={failures}, warnings={warnings}")
    if failures or (args.strict and warnings):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
