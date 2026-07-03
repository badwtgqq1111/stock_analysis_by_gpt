#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""百度网盘数据备份单元测试，不访问外网。"""

import json
import hashlib
import sys
import tarfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.backup.baidu_pan import (
    BaiduPanClient,
    BaiduPanConfig,
    BaiduPanError,
    BackupArchive,
    BackupPart,
    build_backup_archive,
    chunk_md5s,
    combine_archive_parts,
    safe_extract_tar,
    split_archive_parts,
)


class FakeResponse:
    def __init__(self, payload=None, content=b"", status_code=200):
        self._payload = payload or {}
        self.content = content
        self.status_code = status_code
        self.headers = {"content-length": str(len(content))}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=1024 * 1024):
        del chunk_size
        yield self.content


class FakeSession:
    def __init__(self):
        self.calls = []
        self.download_content = b""
        self.downloads = {}
        self.meta_by_id = {}
        self.remote_items = [
            {"server_filename": "old.tar.gz", "fs_id": 1, "server_mtime": 1},
            {"server_filename": "new.tar.gz", "fs_id": 2, "server_mtime": 2},
        ]

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if "method=listall" in url:
            return FakeResponse({"list": self.remote_items})
        if "method=filemetas" in url:
            fsids = json.loads(kwargs["params"]["fsids"])
            items = [
                self.meta_by_id.get(fs_id, {"fs_id": fs_id, "dlink": f"https://download.example/{fs_id}"})
                for fs_id in fsids
            ]
            return FakeResponse({"list": items})
        if "download.example" in url:
            content = self.downloads.get(url.split("?")[0], self.download_content)
            return FakeResponse(content=content)
        raise AssertionError(url)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if "method=create" in url and kwargs.get("data", {}).get("isdir") == 1:
            return FakeResponse({"errno": 0})
        if "method=precreate" in url:
            return FakeResponse({"errno": 0, "uploadid": "upload-1"})
        if "superfile2" in url:
            return FakeResponse({"md5": "part-md5"})
        if "method=create" in url:
            return FakeResponse({"errno": 0, "path": kwargs["data"]["path"], "fs_id": 99})
        raise AssertionError(url)


def test_config_reads_required_environment(monkeypatch):
    monkeypatch.setenv("BAIDU_PAN_APP_ID", "appid")
    monkeypatch.setenv("BAIDU_PAN_APP_KEY", "appkey")
    monkeypatch.setenv("BAIDU_PAN_SECRET_KEY", "secret")
    monkeypatch.setenv("BAIDU_PAN_SIGN_KEY", "sign")

    config = BaiduPanConfig.from_env()

    assert config.app_id == "appid"
    assert config.app_key == "appkey"
    assert config.secret_key == "secret"
    assert config.sign_key == "sign"


def test_config_reports_missing_environment(monkeypatch):
    for key in BaiduPanConfig.required_env_names():
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(BaiduPanError) as exc:
        BaiduPanConfig.from_env()

    assert "BAIDU_PAN_APP_ID" in str(exc.value)


def test_build_backup_archive_contains_manifest_and_data(tmp_path):
    base_dir = tmp_path / "assets" / "data"
    (base_dir / "clean" / "ohlcv").mkdir(parents=True)
    (base_dir / "clean" / "ohlcv" / "part.parquet").write_bytes(b"ohlcv")

    archive = build_backup_archive(base_dir, tmp_path / "out", name="unit")

    assert archive.path.exists()
    assert archive.manifest["name"] == "unit"
    assert archive.manifest["sha256"] == archive.sha256
    with tarfile.open(archive.path, "r:gz") as tar:
        names = set(tar.getnames())
        assert "stock_analysis_backup_manifest.json" in names
        assert "assets/data/clean/ohlcv/part.parquet" in names
        manifest_file = tar.extractfile("stock_analysis_backup_manifest.json")
        manifest = json.loads(manifest_file.read().decode("utf-8"))
        assert manifest["included_root"] == "assets/data"


def test_build_backup_archive_splits_large_archive_when_threshold_is_small(tmp_path):
    base_dir = tmp_path / "assets" / "data"
    (base_dir / "clean" / "ohlcv").mkdir(parents=True)
    (base_dir / "clean" / "ohlcv" / "part.parquet").write_bytes(b"x" * 4096)

    archive = build_backup_archive(base_dir, tmp_path / "out", name="unit", split_size_bytes=200)

    assert archive.is_split
    assert len(archive.parts) > 1
    assert archive.manifest_path.exists()
    assert archive.manifest["archive"]["sha256"] == archive.sha256
    assert archive.manifest["parts"][0]["filename"].endswith(".part001")
    assert sum(part.size for part in archive.parts) == archive.size


def test_combine_archive_parts_rebuilds_original_file(tmp_path):
    archive_path = tmp_path / "payload.tar.gz"
    archive_path.write_bytes(b"abcdef")
    parts = split_archive_parts(archive_path, split_size_bytes=2)

    combined = combine_archive_parts(
        parts,
        tmp_path / "combined.tar.gz",
        expected_sha256=hashlib.sha256(b"abcdef").hexdigest(),
    )

    assert combined.read_bytes() == b"abcdef"


def test_chunk_md5s_uses_fixed_size_chunks(tmp_path):
    path = tmp_path / "payload.bin"
    path.write_bytes(b"abcde")

    md5s = chunk_md5s(path, chunk_size=2)

    assert len(md5s) == 3
    assert md5s[0] != md5s[1]


def test_upload_archive_creates_dirs_and_uploads_chunks(tmp_path):
    payload = tmp_path / "payload.tar.gz"
    payload.write_bytes(b"abcde")
    part = BackupPart(
        path=payload,
        filename="payload.tar.gz",
        sha256="sha",
        size=5,
        index=1,
    )
    archive = BackupArchive(
        path=payload,
        manifest={"name": "payload", "sha256": "sha", "size": 5},
        manifest_path=tmp_path / "payload.manifest.json",
        parts=[part],
        sha256="sha",
        size=5,
    )
    archive.manifest_path.write_text("{}", encoding="utf-8")
    session = FakeSession()
    client = BaiduPanClient(access_token="token", session=session, chunk_size=2)

    remote_path = client.upload_archive(archive, remote_dir="/apps/stock_analysis_by_gpt/backups")

    assert remote_path == "/apps/stock_analysis_by_gpt/backups/payload.tar.gz"
    post_urls = [url for method, url, _ in session.calls if method == "POST"]
    assert any("method=precreate" in url for url in post_urls)
    assert sum("superfile2" in url for url in post_urls) == 3
    assert any("method=create" in url for url in post_urls)


def test_upload_split_archive_uploads_manifest_and_all_parts(tmp_path):
    part1 = tmp_path / "payload.tar.gz.part001"
    part2 = tmp_path / "payload.tar.gz.part002"
    part1.write_bytes(b"ab")
    part2.write_bytes(b"cd")
    manifest_path = tmp_path / "payload.manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    archive = BackupArchive(
        path=tmp_path / "payload.tar.gz",
        manifest={"name": "payload"},
        manifest_path=manifest_path,
        parts=[
            BackupPart(part1, part1.name, "sha1", 2, 1),
            BackupPart(part2, part2.name, "sha2", 2, 2),
        ],
        sha256="sha",
        size=4,
    )
    session = FakeSession()
    client = BaiduPanClient(access_token="token", session=session, chunk_size=2)

    remote_path = client.upload_archive(archive, remote_dir="/apps/stock_analysis_by_gpt/backups")

    assert remote_path == "/apps/stock_analysis_by_gpt/backups/payload.manifest.json"
    created_paths = [
        call[2]["data"]["path"]
        for call in session.calls
        if call[0] == "POST" and "method=create" in call[1] and call[2].get("data", {}).get("isdir") == 0
    ]
    assert "/apps/stock_analysis_by_gpt/backups/payload.manifest.json" in created_paths
    assert "/apps/stock_analysis_by_gpt/backups/payload.tar.gz.part001" in created_paths
    assert "/apps/stock_analysis_by_gpt/backups/payload.tar.gz.part002" in created_paths


def test_find_latest_archive_prefers_newest_mtime():
    session = FakeSession()
    client = BaiduPanClient(access_token="token", session=session)

    item = client.find_archive("/apps/stock_analysis_by_gpt/backups", latest=True)

    assert item["server_filename"] == "new.tar.gz"


def test_download_split_archive_uses_manifest_parts_and_combines(tmp_path):
    session = FakeSession()
    manifest = {
        "name": "split",
        "archive": {"filename": "split.tar.gz", "sha256": None, "size": 4},
        "parts": [
            {"filename": "split.tar.gz.part001", "sha256": None, "size": 2, "index": 1},
            {"filename": "split.tar.gz.part002", "sha256": None, "size": 2, "index": 2},
        ],
    }
    manifest["archive"]["sha256"] = hashlib.sha256(b"abcd").hexdigest()
    manifest["parts"][0]["sha256"] = hashlib.sha256(b"ab").hexdigest()
    manifest["parts"][1]["sha256"] = hashlib.sha256(b"cd").hexdigest()
    session.remote_items = [
        {"server_filename": "split.manifest.json", "fs_id": 10, "server_mtime": 3},
        {"server_filename": "split.tar.gz.part001", "fs_id": 11, "server_mtime": 3},
        {"server_filename": "split.tar.gz.part002", "fs_id": 12, "server_mtime": 3},
    ]
    session.meta_by_id = {
        10: {"fs_id": 10, "dlink": "https://download.example/manifest"},
        11: {"fs_id": 11, "dlink": "https://download.example/part1"},
        12: {"fs_id": 12, "dlink": "https://download.example/part2"},
    }
    session.downloads = {
        "https://download.example/manifest": json.dumps(manifest).encode("utf-8"),
        "https://download.example/part1": b"ab",
        "https://download.example/part2": b"cd",
    }
    client = BaiduPanClient(access_token="token", session=session)

    local_path = client.download_archive(
        remote_dir="/apps/stock_analysis_by_gpt/backups",
        local_dir=tmp_path,
        latest=True,
    )

    assert local_path.name == "split.tar.gz"
    assert local_path.read_bytes() == b"abcd"


def test_safe_extract_tar_rejects_path_traversal(tmp_path):
    archive_path = tmp_path / "bad.tar.gz"
    outside = tmp_path / "outside.txt"
    with tarfile.open(archive_path, "w:gz") as tar:
        info = tarfile.TarInfo("../outside.txt")
        payload = b"bad"
        info.size = len(payload)
        tar.addfile(info, fileobj=__import__("io").BytesIO(payload))

    with pytest.raises(BaiduPanError):
        safe_extract_tar(archive_path, tmp_path / "restore")

    assert not outside.exists()
