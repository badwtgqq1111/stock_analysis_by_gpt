#!/usr/bin/env python3
"""回补退市股日线 + 历史市值，供小市值回测消除幸存者偏差。

背景：clean 层日线只覆盖当前仍在交易的标的（5352 只全部有最新 bar），
退市股在选股池里完全缺席，而小市值策略恰恰是退市高发区 —— 这会让任何
小市值回测系统性高估。本脚本用 Tushare 中继把退市股补回来：

  * `daily`       -> OHLCV（原始价，vol 单位手、amount 单位千元）
  * `adj-factor`  -> 前复权因子，转为与 clean 层一致的 qfq 口径
  * `daily-basic` -> total_mv / circ_mv（万元 -> 亿元）等历史市值与换手

输出：
  assets/data/raw/delisted_daily/<code>.parquet   逐股检查点（可断点续跑）
  assets/data/meta/cn_delisted_daily.parquet      合并结果（策略阶段消费）
  assets/data/meta/cn_delisted_daily.manifest.json 覆盖统计

用法：
  uv run python scripts/refresh_cn_delisted_daily.py                # 增量
  uv run python scripts/refresh_cn_delisted_daily.py --force        # 全量重抓
  uv run python scripts/refresh_cn_delisted_daily.py --limit-codes 20
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAIN_BOARD = re.compile(r"^(600|601|603|605|000|001|002|003)\d{3}\.(SH|SZ)$")
DAILY_COLUMNS = ["trade_date", "open", "high", "low", "close", "volume", "amount", "vwap"]


def _client():
    from data.ingest.providers.cn_tushare_relay import CNTushareRelayClient

    return CNTushareRelayClient()


def delisted_main_board_codes(status_path: Path, client=None) -> list[str]:
    if status_path.exists():
        frame = pd.read_parquet(status_path)
        status_col = next((c for c in ("status", "list_status") if c in frame.columns), None)
        if status_col is None:
            raise KeyError(f"{status_path} 缺少 status/list_status 列")
        frame = frame[frame[status_col] == "D"].copy()
        frame["code"] = frame["ts_code"].str.replace("^T", "", regex=True)
        return sorted(set(code for code in frame["code"] if MAIN_BOARD.match(code)))

    client = client or _client()
    codes: set[str] = set()
    page, _gateway = client.get("stock-basic", limit=2000, offset=0, list_status="D")
    offset = len(page)
    while not page.empty:
        codes.update(
            row.replace("T", "", 1) for row in page["ts_code"] if MAIN_BOARD.match(row.replace("T", "", 1))
        )
        page, _gateway = client.get("stock-basic", limit=2000, offset=offset, list_status="D")
        offset += len(page)
        if offset > 20000:
            break
    return sorted(codes)


def fetch_one(code: str, client, start: str, end: str) -> pd.DataFrame:
    # 中继对 limit 的上限介于 5000 与 8000 之间（8000 返回 HTTP 400）；
    # 2014 年以来全历史最多约 2900 根日线，单页 5000 足够。
    daily, _ = client.get("daily", ts_code=code, start_date=start, end_date=end, limit=5000)
    if daily is None or daily.empty:
        return pd.DataFrame()
    factors, _ = client.get("adj-factor", ts_code=code, start_date=start, end_date=end, limit=5000)
    basics, _ = client.get("daily-basic", ts_code=code, start_date=start, end_date=end, limit=5000)

    frame = daily.rename(columns={"trade_date": "d"}).copy()
    frame["d"] = pd.to_datetime(frame["d"], format="%Y%m%d")
    for col in ("open", "high", "low", "close"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame["volume"] = pd.to_numeric(frame["vol"], errors="coerce") * 100          # 手 -> 股
    frame["amount"] = pd.to_numeric(frame["amount"], errors="coerce") * 1000      # 千元 -> 元

    if factors is not None and not factors.empty:
        factors = factors.rename(columns={"trade_date": "d"}).copy()
        factors["d"] = pd.to_datetime(factors["d"], format="%Y%m%d")
        factors["adj_factor"] = pd.to_numeric(factors["adj_factor"], errors="coerce")
        frame = frame.merge(factors[["d", "adj_factor"]], on="d", how="left")
        frame["adj_factor"] = frame["adj_factor"].ffill().bfill()
        anchor = frame["adj_factor"].dropna()
        anchor = float(anchor.iloc[anchor.index.argmax()]) if not anchor.empty else float("nan")
        if anchor and anchor > 0:
            ratio = frame["adj_factor"] / anchor
            for col in ("open", "high", "low", "close"):
                frame[col] = frame[col] * ratio

    if basics is not None and not basics.empty:
        basics = basics.rename(columns={"trade_date": "d"}).copy()
        basics["d"] = pd.to_datetime(basics["d"], format="%Y%m%d")
        keep = [c for c in ("d", "total_mv", "circ_mv", "turnover_rate", "total_share") if c in basics.columns]
        basics = basics[keep]
        for col in keep[1:]:
            basics[col] = pd.to_numeric(basics[col], errors="coerce")
        if "total_mv" in basics.columns:
            basics["market_cap"] = basics["total_mv"] / 1e4        # 万元 -> 亿元
        frame = frame.merge(basics, on="d", how="left")

    frame = frame[frame["close"].notna() & (frame["close"] > 0)]
    frame["stock_code"] = code
    frame["vwap"] = frame["amount"] / frame["volume"].where(frame["volume"] > 0)
    frame["adjust"] = "qfq"
    frame["source"] = "tushare_relay_delisted"
    if "market_cap" not in frame.columns:
        frame["market_cap"] = pd.NA
    return frame.sort_values("d")


def main() -> int:
    parser = argparse.ArgumentParser(description="回补退市股日线与历史市值")
    parser.add_argument("--status-file", type=Path, default=ROOT / "assets/data/meta/cn_stock_basic.parquet")
    parser.add_argument("--out", type=Path, default=ROOT / "assets/data/meta/cn_delisted_daily.parquet")
    parser.add_argument("--checkpoint-dir", type=Path, default=ROOT / "assets/data/raw/delisted_daily")
    parser.add_argument("--clean-dir", type=Path, default=ROOT / "assets/data/clean/ohlcv/market=CN",
                        help="clean 层日线目录：其中已存在的代码不算退市股（如 T600018 这类退市后代码被上市公司复用的情形）")
    parser.add_argument("--start-date", default="20140101")
    parser.add_argument("--end-date", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit-codes", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    client = _client()
    codes = delisted_main_board_codes(args.status_file, client)
    if args.limit_codes:
        codes = codes[: args.limit_codes]
    args.checkpoint_dir.mkdir(parents=True, exist_ok=True)
    print(f"[delisted] 主板退市代码 {len(codes)} 只；检查点 {args.checkpoint_dir}", flush=True)

    def work(code: str) -> tuple[str, object]:
        path = args.checkpoint_dir / f"{code.replace('.', '_')}.parquet"
        if path.exists() and not args.force:
            cached = pd.read_parquet(path)
            return code, len(cached)
        try:
            frame = fetch_one(code, client, args.start_date, args.end_date)
        except Exception as exc:
            return code, f"error: {type(exc).__name__}: {exc}"[:160]
        if frame.empty:
            return code, 0
        frame.to_parquet(path, index=False)
        return code, len(frame)

    results: dict[str, object] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(work, code): code for code in codes}
        for done, future in enumerate(as_completed(futures), start=1):
            code, rows = future.result()
            results[code] = rows
            if done % 25 == 0 or done == len(codes):
                print(f"[delisted] {done}/{len(codes)} 完成", flush=True)

    frames = []
    for path in sorted(args.checkpoint_dir.glob("*.parquet")):
        try:
            frames.append(pd.read_parquet(path))
        except OSError:
            continue
    if not frames:
        print("[delisted] 没有抓到任何数据", flush=True)
        return 1
    merged = pd.concat(frames, ignore_index=True)
    merged = merged.rename(columns={"d": "trade_date"})
    merged["trade_date"] = pd.to_datetime(merged["trade_date"])
    merged = merged.drop_duplicates(subset=["trade_date", "stock_code"]).sort_values(["stock_code", "trade_date"])

    # 去掉 clean 层仍在交易的代码：中继退市清单里的 T 前缀（如 T600018.SH 上港集箱）剥掉 T 后会
    # 撞上今天仍在上市的 600018.SH 上港集团，若不过滤会把在市股票的行情当成退市股并进来。
    live: set[str] = set()
    if args.clean_dir.exists():
        import pyarrow.dataset as ds

        live_files = sorted(str(p) for p in args.clean_dir.glob("exchange=*/asset_type=equity/frequency=daily/adjust=qfq/**/*.parquet"))
        if live_files:
            live = set(
                ds.dataset(live_files, format="parquet").to_table(columns=["stock_code"])["stock_code"].to_pylist()
            )
    dropped = sorted(set(merged["stock_code"]) & live)
    if dropped:
        merged = merged[~merged["stock_code"].isin(live)]
        print(f"[delisted] 剔除与在市代码重名的 {len(dropped)} 只：{dropped[:10]}", flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(args.out, index=False)

    with_cap = merged["market_cap"].notna().mean() if "market_cap" in merged.columns else 0.0
    errors = {code: value for code, value in results.items() if isinstance(value, str)}
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "codes_requested": len(codes),
        "codes_with_data": int(sum(1 for v in results.values() if isinstance(v, int) and v > 0)),
        "codes_empty": int(sum(1 for v in results.values() if v == 0)),
        "codes_error": len(errors),
        "error_sample": dict(list(errors.items())[:5]),
        "codes_dropped_as_live": dropped,
        "rows": int(len(merged)),
        "market_cap_coverage": round(float(with_cap), 4),
        "window": [str(merged["trade_date"].min().date()), str(merged["trade_date"].max().date())],
        "out": str(args.out),
    }
    (args.out.parent / "cn_delisted_daily.manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
