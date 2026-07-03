#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""百度网盘数据备份/恢复。

这里直接使用百度网盘 OpenAPI HTTP 端点，避免生成客户端内部包名
`openapi_client` 与本仓库目录名不一致带来的导入问题。
"""

from __future__ import annotations

import hashlib
import json
import os
import tarfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests


DEFAULT_REMOTE_DIR = "/apps/stock_analysis_by_gpt/backups"
DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024
DEFAULT_SPLIT_SIZE_BYTES = 2 * 1024 * 1024 * 1024
MANIFEST_NAME = "stock_analysis_backup_manifest.json"


class BaiduPanError(RuntimeError):
    """百度网盘备份异常。"""


@dataclass(frozen=True)
class BaiduPanConfig:
    app_id: str
    app_key: str
    secret_key: str
    sign_key: str
    scope: str = "basic,netdisk"

    @staticmethod
    def required_env_names() -> tuple[str, ...]:
        return (
            "BAIDU_PAN_APP_ID",
            "BAIDU_PAN_APP_KEY",
            "BAIDU_PAN_SECRET_KEY",
            "BAIDU_PAN_SIGN_KEY",
        )

    @classmethod
    def from_env(cls) -> "BaiduPanConfig":
        missing = [name for name in cls.required_env_names() if not os.getenv(name)]
        if missing:
            raise BaiduPanError(
                "缺少百度网盘环境变量: "
                + ", ".join(missing)
                + "。请先 export BAIDU_PAN_APP_ID/APP_KEY/SECRET_KEY/SIGN_KEY。"
            )
        return cls(
            app_id=os.environ["BAIDU_PAN_APP_ID"],
            app_key=os.environ["BAIDU_PAN_APP_KEY"],
            secret_key=os.environ["BAIDU_PAN_SECRET_KEY"],
            sign_key=os.environ["BAIDU_PAN_SIGN_KEY"],
            scope=os.getenv("BAIDU_PAN_SCOPE", "basic,netdisk"),
        )


@dataclass(frozen=True)
class BackupPart:
    path: Path
    filename: str
    sha256: str
    size: int
    index: int

    def to_manifest(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "sha256": self.sha256,
            "size": self.size,
            "index": self.index,
        }


@dataclass(frozen=True)
class BackupArchive:
    path: Path
    manifest: dict[str, Any]
    manifest_path: Path
    parts: list[BackupPart]
    sha256: str
    size: int

    @property
    def is_split(self) -> bool:
        return len(self.parts) > 1


class TokenStore:
    """本机 OAuth token 缓存。"""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path).expanduser() if path else Path.home() / ".config" / "stock_analysis_by_gpt" / "baidu_pan_token.json"

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise BaiduPanError(f"百度网盘 token 缓存损坏: {self.path}") from exc

    def save(self, token: dict[str, Any]) -> None:
        payload = dict(token)
        if "expires_in" in payload:
            payload["expires_at"] = int(time.time()) + int(payload["expires_in"]) - 300
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass


class BaiduPanAuth:
    """OAuth 设备码鉴权，首轮授权后自动刷新 token。"""

    def __init__(
        self,
        config: BaiduPanConfig,
        token_store: TokenStore | None = None,
        session: requests.Session | None = None,
        timeout: int = 30,
    ):
        self.config = config
        self.token_store = token_store or TokenStore()
        self.session = session or requests.Session()
        self.timeout = timeout

    def get_access_token(self) -> str:
        env_token = os.getenv("BAIDU_PAN_ACCESS_TOKEN")
        if env_token:
            return env_token

        cached = self.token_store.load()
        if cached and cached.get("access_token") and int(cached.get("expires_at", 0)) > int(time.time()):
            return str(cached["access_token"])
        if cached and cached.get("refresh_token"):
            refreshed = self.refresh_token(str(cached["refresh_token"]))
            self.token_store.save(refreshed)
            return str(refreshed["access_token"])

        token = self.device_authorize()
        self.token_store.save(token)
        return str(token["access_token"])

    def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        payload = self._get_json(
            "https://openapi.baidu.com/oauth/2.0/token",
            params={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.config.app_key,
                "client_secret": self.config.secret_key,
                "openapi": "xpansdk",
            },
        )
        if "access_token" not in payload:
            raise BaiduPanError(f"刷新百度网盘 token 失败: {payload}")
        return payload

    def device_authorize(self) -> dict[str, Any]:
        code_payload = self._get_json(
            "https://openapi.baidu.com/oauth/2.0/device/code",
            params={
                "response_type": "device_code",
                "client_id": self.config.app_key,
                "scope": self.config.scope,
                "openapi": "xpansdk",
            },
        )
        device_code = code_payload.get("device_code")
        user_code = code_payload.get("user_code")
        verification_url = code_payload.get("verification_url") or code_payload.get("verification_url_complete")
        if not device_code:
            raise BaiduPanError(f"获取百度网盘设备码失败: {code_payload}")

        print("首次使用需要完成一次百度网盘授权：")
        if verification_url:
            print(f"  打开: {verification_url}")
        if user_code:
            print(f"  输入用户码: {user_code}")
        print("授权成功后本机会缓存 refresh_token，后续上传/下载会自动刷新。")

        interval = int(code_payload.get("interval") or 5)
        expires_at = time.time() + int(code_payload.get("expires_in") or 600)
        while time.time() < expires_at:
            time.sleep(interval)
            token_payload = self._get_json(
                "https://openapi.baidu.com/oauth/2.0/token",
                params={
                    "grant_type": "device_token",
                    "code": device_code,
                    "client_id": self.config.app_key,
                    "client_secret": self.config.secret_key,
                    "openapi": "xpansdk",
                },
                allow_oauth_pending=True,
            )
            if "access_token" in token_payload:
                return token_payload
            error = token_payload.get("error")
            if error == "slow_down":
                interval += 5
            elif error not in {"authorization_pending", "authorization_pending ", None}:
                raise BaiduPanError(f"百度网盘授权失败: {token_payload}")
        raise BaiduPanError("百度网盘授权超时，请重新运行命令。")

    def _get_json(self, url: str, params: dict[str, Any], allow_oauth_pending: bool = False) -> dict[str, Any]:
        response = self.session.get(url, params=params, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        if allow_oauth_pending and payload.get("error") in {"authorization_pending", "slow_down"}:
            return payload
        if payload.get("error"):
            raise BaiduPanError(f"百度网盘 OAuth 请求失败: {payload}")
        return payload


class BaiduPanClient:
    """百度网盘 OpenAPI 文件客户端。"""

    def __init__(
        self,
        access_token: str,
        session: requests.Session | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        timeout: int = 60,
    ):
        self.access_token = access_token
        self.session = session or requests.Session()
        self.chunk_size = chunk_size
        self.timeout = timeout

    def upload_archive(
        self,
        archive: BackupArchive,
        remote_dir: str = DEFAULT_REMOTE_DIR,
        show_progress: bool = False,
    ) -> str:
        remote_dir = normalize_remote_path(remote_dir)
        self.ensure_remote_dir(remote_dir)
        if not archive.is_split:
            remote_path = f"{remote_dir}/{archive.path.name}"
            self.upload_file(archive.path, remote_path, show_progress=show_progress)
            return remote_path

        manifest_remote_path = f"{remote_dir}/{archive.manifest_path.name}"
        self.upload_file(archive.manifest_path, manifest_remote_path, show_progress=show_progress)
        for part in archive.parts:
            self.upload_file(part.path, f"{remote_dir}/{part.filename}", show_progress=show_progress)
        return manifest_remote_path

    def upload_file(self, local_path: str | Path, remote_path: str, show_progress: bool = False) -> str:
        local_path = Path(local_path)
        size = local_path.stat().st_size
        blocks = chunk_md5s(local_path, self.chunk_size)
        uploadid = self.precreate(remote_path, size, blocks)
        uploaded = 0
        with local_path.open("rb") as handle:
            for partseq, chunk in enumerate(iter(lambda: handle.read(self.chunk_size), b"")):
                self.upload_part(remote_path, uploadid, partseq, chunk)
                uploaded += len(chunk)
                if show_progress:
                    print(f"\rupload {local_path.name}: {uploaded}/{size} bytes", end="", flush=True)
        if show_progress:
            print()
        self.create_file(remote_path, size, uploadid, blocks)
        return remote_path

    def download_archive(
        self,
        remote_dir: str = DEFAULT_REMOTE_DIR,
        local_dir: str | Path = "output/backups",
        name: str | None = None,
        latest: bool = False,
        show_progress: bool = False,
    ) -> Path:
        item = self.find_archive(remote_dir, name=name, latest=latest or name is None)
        fs_id = item.get("fs_id")
        filename = item.get("server_filename") or Path(item.get("path", "")).name
        if not fs_id or not filename:
            raise BaiduPanError(f"远端文件信息不完整: {item}")
        if str(filename).endswith(".manifest.json"):
            return self._download_split_archive(item, remote_dir, local_dir, show_progress=show_progress)
        meta = self.file_meta(int(fs_id))
        dlink = meta.get("dlink")
        if not dlink:
            raise BaiduPanError(f"远端文件缺少下载链接: {meta}")
        local_dir = Path(local_dir)
        local_dir.mkdir(parents=True, exist_ok=True)
        local_path = local_dir / filename
        self._download_url(dlink, local_path, show_progress=show_progress)
        return local_path

    def ensure_remote_dir(self, remote_dir: str) -> None:
        parts = [part for part in normalize_remote_path(remote_dir).split("/") if part]
        current = ""
        for part in parts:
            current += "/" + part
            if current == "/apps":
                continue
            self.create_dir(current)

    def create_dir(self, path: str) -> None:
        payload = self._post_json(
            "https://pan.baidu.com/rest/2.0/xpan/file?method=create&openapi=xpansdk",
            params={"access_token": self.access_token},
            data={"path": path, "isdir": 1, "rtype": 0},
            allowed_errno={0, -8, 31061},
        )
        if payload.get("errno") not in (None, 0, -8, 31061):
            raise BaiduPanError(f"创建百度网盘目录失败: {payload}")

    def precreate(self, remote_path: str, size: int, blocks: list[str]) -> str:
        payload = self._post_json(
            "https://pan.baidu.com/rest/2.0/xpan/file?method=precreate&openapi=xpansdk",
            params={"access_token": self.access_token},
            data={
                "path": remote_path,
                "isdir": 0,
                "size": size,
                "autoinit": 1,
                "block_list": json.dumps(blocks),
                "rtype": 3,
            },
        )
        uploadid = payload.get("uploadid")
        if not uploadid:
            raise BaiduPanError(f"百度网盘 precreate 未返回 uploadid: {payload}")
        return str(uploadid)

    def upload_part(self, remote_path: str, uploadid: str, partseq: int, chunk: bytes) -> None:
        response = self.session.post(
            "https://d.pcs.baidu.com/rest/2.0/pcs/superfile2?method=upload&openapi=xpansdk",
            params={
                "access_token": self.access_token,
                "type": "tmpfile",
                "path": remote_path,
                "uploadid": uploadid,
                "partseq": str(partseq),
            },
            files={"file": ("blob", chunk)},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error_code") or payload.get("errno"):
            raise BaiduPanError(f"百度网盘分片上传失败: {payload}")

    def create_file(self, remote_path: str, size: int, uploadid: str, blocks: list[str]) -> dict[str, Any]:
        return self._post_json(
            "https://pan.baidu.com/rest/2.0/xpan/file?method=create&openapi=xpansdk",
            params={"access_token": self.access_token},
            data={
                "path": remote_path,
                "isdir": 0,
                "size": size,
                "uploadid": uploadid,
                "block_list": json.dumps(blocks),
                "rtype": 3,
            },
        )

    def list_archives(self, remote_dir: str = DEFAULT_REMOTE_DIR) -> list[dict[str, Any]]:
        items = self.list_remote_items(remote_dir)
        return [
            item for item in items
            if str(item.get("server_filename") or item.get("path") or "").endswith((".tar.gz", ".manifest.json"))
        ]

    def list_remote_items(self, remote_dir: str = DEFAULT_REMOTE_DIR) -> list[dict[str, Any]]:
        payload = self._get_json(
            "https://pan.baidu.com/rest/2.0/xpan/multimedia?method=listall&openapi=xpansdk",
            params={
                "access_token": self.access_token,
                "path": normalize_remote_path(remote_dir),
                "recursion": 0,
                "web": "1",
                "order": "time",
                "desc": 1,
            },
        )
        return payload.get("list") or []

    def find_archive(
        self,
        remote_dir: str = DEFAULT_REMOTE_DIR,
        name: str | None = None,
        latest: bool = False,
    ) -> dict[str, Any]:
        archives = self.list_archives(remote_dir)
        if name:
            candidates = {name, f"{name}.tar.gz", f"{name}.manifest.json"}
            matches = [
                item for item in archives
                if item.get("server_filename") in candidates or Path(str(item.get("path", ""))).name in candidates
            ]
            if not matches:
                raise BaiduPanError(f"远端未找到备份: {name}")
            return matches[0]
        if not latest:
            raise BaiduPanError("请指定 --name 或 --latest")
        if not archives:
            raise BaiduPanError(f"远端目录没有备份文件: {remote_dir}")
        return sorted(
            archives,
            key=lambda item: int(item.get("server_mtime") or item.get("local_mtime") or item.get("mtime") or 0),
            reverse=True,
        )[0]

    def _download_split_archive(
        self,
        manifest_item: dict[str, Any],
        remote_dir: str,
        local_dir: str | Path,
        show_progress: bool = False,
    ) -> Path:
        local_dir = Path(local_dir)
        local_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = self._download_remote_item(manifest_item, local_dir, show_progress=show_progress)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        archive_info = manifest.get("archive") or {}
        archive_filename = archive_info.get("filename")
        parts_info = sorted(manifest.get("parts") or [], key=lambda item: int(item.get("index") or 0))
        if not archive_filename or not parts_info:
            raise BaiduPanError(f"备份 manifest 不完整: {manifest_path}")

        remote_items = {
            item.get("server_filename") or Path(str(item.get("path", ""))).name: item
            for item in self.list_remote_items(remote_dir)
        }
        downloaded_parts: list[BackupPart] = []
        for part_info in parts_info:
            filename = part_info.get("filename")
            remote_item = remote_items.get(filename)
            if not remote_item:
                raise BaiduPanError(f"远端缺少备份分卷: {filename}")
            part_path = self._download_remote_item(remote_item, local_dir, show_progress=show_progress)
            part = BackupPart(
                path=part_path,
                filename=str(filename),
                sha256=str(part_info.get("sha256") or ""),
                size=int(part_info.get("size") or part_path.stat().st_size),
                index=int(part_info.get("index") or len(downloaded_parts) + 1),
            )
            if part.sha256 and file_sha256(part.path) != part.sha256:
                raise BaiduPanError(f"备份分卷 sha256 校验失败: {part.filename}")
            downloaded_parts.append(part)

        expected_sha256 = archive_info.get("sha256") or manifest.get("sha256")
        return combine_archive_parts(
            downloaded_parts,
            local_dir / str(archive_filename),
            expected_sha256=str(expected_sha256) if expected_sha256 else None,
        )

    def _download_remote_item(
        self,
        item: dict[str, Any],
        local_dir: Path,
        show_progress: bool = False,
    ) -> Path:
        fs_id = item.get("fs_id")
        filename = item.get("server_filename") or Path(str(item.get("path", ""))).name
        if not fs_id or not filename:
            raise BaiduPanError(f"远端文件信息不完整: {item}")
        meta = self.file_meta(int(fs_id))
        dlink = meta.get("dlink")
        if not dlink:
            raise BaiduPanError(f"远端文件缺少下载链接: {meta}")
        local_path = local_dir / str(filename)
        self._download_url(dlink, local_path, show_progress=show_progress)
        return local_path

    def file_meta(self, fs_id: int) -> dict[str, Any]:
        payload = self._get_json(
            "https://pan.baidu.com/rest/2.0/xpan/multimedia?method=filemetas&openapi=xpansdk",
            params={"access_token": self.access_token, "fsids": json.dumps([fs_id]), "dlink": "1"},
        )
        items = payload.get("list") or []
        if not items:
            raise BaiduPanError(f"无法获取远端文件元信息: fs_id={fs_id}")
        return items[0]

    def _download_url(self, dlink: str, local_path: Path, show_progress: bool = False) -> None:
        separator = "&" if "?" in dlink else "?"
        url = f"{dlink}{separator}{urlencode({'access_token': self.access_token})}"
        response = self.session.get(url, stream=True, timeout=self.timeout)
        response.raise_for_status()
        expected = int(response.headers.get("content-length") or 0)
        downloaded = 0
        with local_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=self.chunk_size):
                if not chunk:
                    continue
                handle.write(chunk)
                downloaded += len(chunk)
                if show_progress:
                    total = expected or "?"
                    print(f"\rdownload {local_path.name}: {downloaded}/{total} bytes", end="", flush=True)
        if show_progress:
            print()

    def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self.session.get(url, params=params, timeout=self.timeout)
        response.raise_for_status()
        return self._validate_payload(response.json())

    def _post_json(
        self,
        url: str,
        params: dict[str, Any],
        data: dict[str, Any],
        allowed_errno: set[int] | None = None,
    ) -> dict[str, Any]:
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        response.raise_for_status()
        return self._validate_payload(response.json(), allowed_errno=allowed_errno)

    @staticmethod
    def _validate_payload(payload: dict[str, Any], allowed_errno: set[int] | None = None) -> dict[str, Any]:
        allowed_errno = allowed_errno or {0}
        errno = payload.get("errno")
        error_code = payload.get("error_code")
        if error_code:
            raise BaiduPanError(f"百度网盘请求失败: {payload}")
        if errno not in (None, *allowed_errno):
            raise BaiduPanError(f"百度网盘请求失败: {payload}")
        return payload


def normalize_remote_path(path: str) -> str:
    path = "/" + path.strip("/")
    return path.rstrip("/") or "/"


def chunk_md5s(path: str | Path, chunk_size: int = DEFAULT_CHUNK_SIZE) -> list[str]:
    result: list[str] = []
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            result.append(hashlib.md5(chunk).hexdigest())
    return result


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def split_archive_parts(path: str | Path, split_size_bytes: int) -> list[BackupPart]:
    path = Path(path)
    if split_size_bytes <= 0:
        raise BaiduPanError("--split-size-gb 必须大于 0")
    parts: list[BackupPart] = []
    with path.open("rb") as source:
        index = 1
        while True:
            chunk = source.read(split_size_bytes)
            if not chunk:
                break
            part_path = path.with_name(f"{path.name}.part{index:03d}")
            part_path.write_bytes(chunk)
            parts.append(
                BackupPart(
                    path=part_path,
                    filename=part_path.name,
                    sha256=file_sha256(part_path),
                    size=part_path.stat().st_size,
                    index=index,
                )
            )
            index += 1
    return parts


def combine_archive_parts(
    parts: list[BackupPart],
    output_path: str | Path,
    expected_sha256: str | None = None,
) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as target:
        for part in sorted(parts, key=lambda item: item.index):
            if part.sha256 and file_sha256(part.path) != part.sha256:
                raise BaiduPanError(f"备份分卷 sha256 校验失败: {part.filename}")
            with part.path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    target.write(chunk)
    if expected_sha256 and file_sha256(output_path) != expected_sha256:
        raise BaiduPanError(f"合并后的备份包 sha256 校验失败: {output_path}")
    return output_path


def build_backup_archive(
    base_dir: str | Path,
    output_dir: str | Path,
    name: str | None = None,
    extra_paths: list[str | Path] | None = None,
    split_size_bytes: int = DEFAULT_SPLIT_SIZE_BYTES,
) -> BackupArchive:
    base_dir = Path(base_dir).resolve()
    if not base_dir.exists():
        raise BaiduPanError(f"数据目录不存在: {base_dir}")
    project_root = _infer_project_root(base_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if not name:
        name = "stock-analysis-data-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_path = output_dir / f"{name}.tar.gz"

    include_paths = [base_dir]
    for extra_path in extra_paths or []:
        path = Path(extra_path).resolve()
        if path.exists():
            include_paths.append(path)

    manifest = {
        "name": name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "included_root": _archive_name(base_dir, project_root),
        "included_roots": [_archive_name(path, project_root) for path in include_paths],
        "sha256": None,
        "size": None,
    }

    with tarfile.open(archive_path, "w:gz") as tar:
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(manifest_bytes)
        info.mtime = int(time.time())
        tar.addfile(info, fileobj=__import__("io").BytesIO(manifest_bytes))
        for path in include_paths:
            tar.add(path, arcname=_archive_name(path, project_root), recursive=True)

    sha256 = file_sha256(archive_path)
    size = archive_path.stat().st_size
    if size > split_size_bytes:
        parts = split_archive_parts(archive_path, split_size_bytes)
    else:
        parts = [
            BackupPart(
                path=archive_path,
                filename=archive_path.name,
                sha256=sha256,
                size=size,
                index=1,
            )
        ]
    manifest["sha256"] = sha256
    manifest["size"] = size
    manifest["archive"] = {
        "filename": archive_path.name,
        "sha256": sha256,
        "size": size,
    }
    manifest["split"] = {
        "enabled": len(parts) > 1,
        "split_size_bytes": split_size_bytes,
    }
    manifest["parts"] = [part.to_manifest() for part in parts]
    sidecar_path = output_dir / f"{name}.manifest.json"
    sidecar_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return BackupArchive(path=archive_path, manifest=manifest, manifest_path=sidecar_path, parts=parts, sha256=sha256, size=size)


def safe_extract_tar(archive_path: str | Path, target_dir: str | Path) -> None:
    archive_path = Path(archive_path)
    target_dir = Path(target_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as tar:
        for member in tar.getmembers():
            destination = (target_dir / member.name).resolve()
            try:
                destination.relative_to(target_dir)
            except ValueError as exc:
                raise BaiduPanError(f"备份包包含非法路径: {member.name}") from exc
        tar.extractall(target_dir)


def upload_project_backup(
    base_dir: str | Path = "./assets/data",
    output_dir: str | Path = "output/backups",
    remote_dir: str = DEFAULT_REMOTE_DIR,
    name: str | None = None,
    include_clickhouse: bool = True,
    split_size_bytes: int = DEFAULT_SPLIT_SIZE_BYTES,
    show_progress: bool = False,
) -> tuple[BackupArchive, str]:
    base_path = Path(base_dir).resolve()
    _best_effort_prepare_parquet(base_path)
    extra_paths: list[Path] = []
    clickhouse_path = base_path.parent / "clickhouse"
    if include_clickhouse and clickhouse_path.exists():
        extra_paths.append(clickhouse_path)
    archive = build_backup_archive(
        base_path,
        output_dir,
        name=name,
        extra_paths=extra_paths,
        split_size_bytes=split_size_bytes,
    )
    access_token = BaiduPanAuth(BaiduPanConfig.from_env()).get_access_token()
    client = BaiduPanClient(access_token=access_token)
    remote_path = client.upload_archive(archive, remote_dir=remote_dir, show_progress=show_progress)
    return archive, remote_path


def download_project_backup(
    remote_dir: str = DEFAULT_REMOTE_DIR,
    output_dir: str | Path = "output/backups",
    restore_dir: str | Path = ".",
    name: str | None = None,
    latest: bool = True,
    show_progress: bool = False,
) -> Path:
    access_token = BaiduPanAuth(BaiduPanConfig.from_env()).get_access_token()
    client = BaiduPanClient(access_token=access_token)
    archive_path = client.download_archive(
        remote_dir=remote_dir,
        local_dir=output_dir,
        name=name,
        latest=latest,
        show_progress=show_progress,
    )
    safe_extract_tar(archive_path, restore_dir)
    return archive_path


def _infer_project_root(path: Path) -> Path:
    if path.name == "data" and path.parent.name == "assets":
        return path.parent.parent
    if path.name == "clickhouse" and path.parent.name == "assets":
        return path.parent.parent
    return path.parent


def _archive_name(path: Path, project_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(project_root.resolve()))
    except ValueError:
        return path.name


def _best_effort_prepare_parquet(base_path: Path) -> None:
    try:
        from data.store import DataLayout, MarketDataWarehouse

        warehouse = MarketDataWarehouse(DataLayout(base_dir=str(base_path)))
        try:
            warehouse.sync_ohlcv_to_parquet()
        finally:
            warehouse.close()
    except Exception as exc:
        print(f"[WARNING] 备份前 Parquet 准备失败，继续打包现有数据: {exc}")
