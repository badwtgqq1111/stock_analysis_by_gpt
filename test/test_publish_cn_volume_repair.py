"""Publication of the historical CN daily volume repair is reversible."""

import json
import shutil
import subprocess

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from scripts.publish_cn_volume_repair import prepare_repair, rollback, sha256


def test_publish_repairs_only_lot_rows_and_rollback_copy(tmp_path):
    root = tmp_path / "ohlcv"
    relative = ("market=CN/exchange=SZSE/asset_type=equity/frequency=daily/"
                "adjust=qfq/year=2026/part.parquet")
    live = root / relative
    live.parent.mkdir(parents=True)
    frame = pd.DataFrame({
        "stock_code": ["000796.SZ", "000796.SZ", "000955.SZ"],
        "trade_date": pd.to_datetime(["2026-09-20", "2026-09-21", "2026-09-21"]),
        "source": ["tencent"] * 3,
        "close": [10.0] * 3, "volume": [1_000_000.0, 10_000.0, 10_000.0],
        "amount": [10_000_000.0] * 3, "vwap": [10.0, 1000.0, 1000.0],
    })
    pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), live)
    original_hash = sha256(live)
    out = tmp_path / "repair"
    result = prepare_repair(data_root=root, output_dir=out,
                            start_date="2026-09-21", end_date="2026-09-21", publish=True)
    assert result["published"] is True
    assert result["lots_repaired"] == 2
    assert result["files"][0]["original_sha256"] == original_hash
    fixed = pq.ParquetFile(live).read().to_pandas()
    assert fixed["volume"].tolist() == [1_000_000.0] * 3
    assert fixed["vwap"].tolist() == [10.0] * 3
    assert len(pd.read_csv(out / "DIFF_FILE.csv")) == 2
    assert json.loads((out / "MODIFIED_FILE.json").read_text())["published"] is True
    assert (out / "ROLLBACK.sh").stat().st_mode & 0o111

    copy_root = tmp_path / "ROLLBACK_TARGET"
    copy_live = copy_root / relative
    copy_live.parent.mkdir(parents=True)
    shutil.copy2(live, copy_live)
    command = [str(out / "ROLLBACK.sh"), str(copy_root)]
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    assert completed.stdout.strip() == '{"restored_files": 1, "restored_rows": 2}'
    assert sha256(copy_live) == original_hash
    assert sha256(live) == result["files"][0]["repaired_sha256"]
    with pytest.raises(ValueError, match="modified hash mismatch"):
        rollback(out / "MODIFIED_FILE.json", target_root=copy_root)


def test_prepare_requires_fresh_output_directory(tmp_path):
    out = tmp_path / "existing"
    out.mkdir()
    with pytest.raises(FileExistsError):
        prepare_repair(data_root=tmp_path, output_dir=out,
                       start_date="2026-09-21", end_date="2026-09-21")
