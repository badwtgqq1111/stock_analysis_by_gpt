#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Minimal iFinD HTTP client (trial friendly).

Token 不写进仓库、不打印、不放进命令行参数：
  * refresh_token: 环境变量 IFIND_REFRESH_TOKEN 或 ~/.config/quant/ifind_token
  * access_token: 缓存到 ~/.config/quant/ifind_access_token（600）
  * curl 通过 ~/.config/quant/ifind_curl.conf 读取 access_token header

iFinD 这台网关对 LibreSSL 的默认握手不稳定，客户端使用
`curl --tls-max 1.2` + 重试；失败只返回脱敏后的错误。
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import pandas as pd
import requests

BASE = "https://quantapi.51ifind.com/api/v1"
CONF_DIR = Path.home() / ".config/quant"
REFRESH_FILE = CONF_DIR / "ifind_token"
ACCESS_FILE = CONF_DIR / "ifind_access_token"
EXPIRY_FILE = CONF_DIR / "ifind_access_token_expiry"
CURL_CONF = CONF_DIR / "ifind_curl.conf"


def _read_refresh() -> str:
    token = os.environ.get("IFIND_REFRESH_TOKEN", "").strip()
    if token:
        return token
    if REFRESH_FILE.exists():
        return REFRESH_FILE.read_text(encoding="utf-8").strip()
    raise RuntimeError("missing iFinD refresh token (IFIND_REFRESH_TOKEN or ~/.config/quant/ifind_token)")


def _save_access(token: str, expiry: str = "") -> None:
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    ACCESS_FILE.write_text(token, encoding="utf-8")
    os.chmod(ACCESS_FILE, 0o600)
    if expiry:
        EXPIRY_FILE.write_text(expiry, encoding="utf-8")
    # curl config: header 不进入命令行参数
    CURL_CONF.write_text(f'header = "access_token: {token}"\n', encoding="utf-8")
    os.chmod(CURL_CONF, 0o600)


def refresh_access_token(force: bool = False) -> str:
    """Refresh and cache access_token. Returns the token (do not print it)."""
    if not force and ACCESS_FILE.exists() and CURL_CONF.exists():
        return ACCESS_FILE.read_text(encoding="utf-8").strip()
    refresh = _read_refresh()
    last_error = None
    for attempt in range(4):
        try:
            response = requests.post(
                f"{BASE}/get_access_token",
                headers={"Content-Type": "application/json", "refresh_token": refresh},
                timeout=30,
            )
            payload = response.json()
            if payload.get("errorcode") not in (None, 0):
                raise RuntimeError(str(payload.get("errmsg") or "get_access_token failed"))
            data = payload.get("data") or {}
            token = str(data.get("access_token") or "").strip()
            if not token:
                raise RuntimeError("get_access_token returned empty token")
            _save_access(token, str(data.get("expired_time") or ""))
            return token
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"iFinD refresh failed: {last_error}")


def _curl_post(endpoint: str, payload: dict, timeout: int = 40) -> dict:
    refresh_access_token()
    body = json.dumps(payload, ensure_ascii=False)
    last = ""
    for attempt in range(10):
        proc = subprocess.run(
            [
                "curl", "-sS", "--tls-max", "1.2", "--max-time", str(timeout),
                "-K", str(CURL_CONF),
                "-X", "POST", f"{BASE}/{endpoint}",
                "-H", "Content-Type: application/json",
                "--data", body,
            ],
            capture_output=True, text=True,
        )
        text = (proc.stdout or "").strip()
        if text:
            try:
                parsed = json.loads(text)
                if parsed.get("errorcode") in (-1302, 401):
                    refresh_access_token(force=True)
                    last = "access token expired"
                    time.sleep(1.0)
                    continue
                return parsed
            except json.JSONDecodeError:
                last = text[:200]
        else:
            last = (proc.stderr or "empty response").strip()[:200]
        time.sleep(min(0.5 * (attempt + 1), 3.0))
    raise RuntimeError(f"iFinD {endpoint} failed after retries: {last}")


def realtime(codes: str, indicators: str = "latest") -> dict:
    return _curl_post("real_time_quotation", {"codes": codes, "indicators": indicators})


def history(codes: str, indicators: str, start: str, end: str) -> dict:
    return _curl_post("cmd_history_quotation", {
        "codes": codes, "indicators": indicators,
        "startdate": start, "enddate": end,
        "functionpara": {"Fill": "Blank"},
    })


def smart_stock_picking(query: str) -> pd.DataFrame:
    payload = _curl_post("smart_stock_picking", {"searchstring": query, "searchtype": "stock"})
    if payload.get("errorcode") not in (None, 0):
        raise RuntimeError(str(payload.get("errmsg") or "smart_stock_picking failed"))
    tables = payload.get("tables") or []
    if not tables:
        return pd.DataFrame()
    table = tables[0].get("table") or {}
    return pd.DataFrame({key: value for key, value in table.items() if isinstance(value, list)})
