"""R10 特征层压实的回归测试（同键只保留 ingest_time 最新的一行，可回滚）。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


def _load_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "compact_feature_layer.py"
    spec = importlib.util.spec_from_file_location("compact_feature_layer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_part(directory: Path, name: str, rows: list[dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(pd.DataFrame(rows), preserve_index=False), directory / name)


def _row(stock, feature, value, ingest, day="2026-09-24"):
    return {"market": "CN", "stock_code": stock, "trade_date": day, "frequency": "daily", "adjust": "qfq",
            "feature_set": "demo", "feature_version": "0.1.0", "feature_config_hash": "abc",
            "feature_name": feature, "feature_value": value, "source": "factor_engine", "ingest_time": ingest}


def test_compact_keeps_newest_row_and_is_reversible(tmp_path):
    module = _load_module()
    year_dir = tmp_path / "market=CN" / "exchange=SSE" / "asset_type=equity" / "frequency=daily" / "adjust=qfq" / "feature_set=demo" / "year=2026"
    _write_part(year_dir, "part-a.parquet", [_row("000001.SZ", "f1", 1.0, "2026-09-23T10:00:00"),
                                             _row("000001.SZ", "f2", 2.0, "2026-09-23T10:00:00")])
    _write_part(year_dir, "part-b.parquet", [_row("000001.SZ", "f1", 9.9, "2026-09-26T10:00:00"),
                                             _row("000002.SZ", "f1", 3.0, "2026-09-24T10:00:00")])

    result = module.compact_year(year_dir, work_root=tmp_path / "work", batch_files=10, publish=True, log=lambda m: None)

    assert result["rows_before"] == 4
    assert result["rows_after"] == 3
    assert result["dropped"] == 1
    assert result["duplicate_keys"] == 0
    compacted = pq.ParquetFile(result["compacted_file"]).read().to_pandas()
    assert len(compacted) == 3
    kept = compacted[(compacted.stock_code == "000001.SZ") & (compacted.feature_name == "f1")].iloc[0]
    assert kept.feature_value == 9.9  # 保留 ingest_time 最新的一行
    assert Path(result["previous"]).is_dir()  # 旧目录作为回滚点保留

    # 回滚：删掉重建出来的 year 目录，把 .previous 备份改名回来，行数与内容应与原始一致
    import shutil
    shutil.rmtree(year_dir)
    shutil.move(result["previous"], str(year_dir))
    restored = pd.concat([pq.ParquetFile(p).read().to_pandas() for p in sorted(year_dir.glob("*.parquet"))], ignore_index=True)
    assert len(restored) == 4


def test_compact_dry_run_does_not_touch_files(tmp_path):
    module = _load_module()
    year_dir = tmp_path / "market=CN" / "exchange=SSE" / "asset_type=equity" / "frequency=daily" / "adjust=qfq" / "feature_set=demo" / "year=2026"
    _write_part(year_dir, "part-a.parquet", [_row("000001.SZ", "f1", 1.0, "2026-09-23T10:00:00")])
    _write_part(year_dir, "part-b.parquet", [_row("000001.SZ", "f1", 2.0, "2026-09-24T10:00:00")])

    result = module.compact_year(year_dir, work_root=tmp_path / "work", batch_files=10, publish=False, log=lambda m: None)

    assert result["rows_before"] == 2 and result["rows_after"] == 1
    assert sorted(p.name for p in year_dir.glob("*.parquet")) == ["part-a.parquet", "part-b.parquet"]
    assert not list(tmp_path.glob(".year=2026.previous-*"))
