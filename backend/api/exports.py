"""导出入口：把当日各策略的选股结果给成可直接用的文件（同花顺自选股 / CSV）。

同花顺不开放写接口（自选股在账号云端），所以这里做的是**生成格式正确的文件 + 提供一键复制**：
最快路径是「网页点一键复制 → 同花顺自选股里 Ctrl+V」，其次是「下载文件 → 导入自选股」。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

ROOT = Path(__file__).resolve().parents[2]
THS_DIR = ROOT / "output/results_cn/ths_watchlist"

router = APIRouter(prefix="/api/export", tags=["export"])

_ALLOWED = (".txt", ".csv", ".json")
# 源的展示名：让前端不用自己拼中文
SOURCE_LABELS = {
    "holdings": "生产名单（实际买入）",
    "preselected": "候选池（PK 输入）",
    "arm": "研究手臂",
    "all": "全部合并（一键导入）",
}
SOURCE_ORDER = ["all", "holdings", "preselected", "arm"]


def _safe(name: str) -> Path:
    if "/" in name or ".." in name or not name.endswith(_ALLOWED):
        raise HTTPException(status_code=400, detail="非法文件名")
    path = (THS_DIR / name).resolve()
    if not str(path).startswith(str(THS_DIR.resolve())) or not path.is_file():
        raise HTTPException(status_code=404, detail=f"没有 {name}")
    return path


def _latest_manifest() -> dict:
    if not THS_DIR.exists():
        return {}
    manifests = sorted(THS_DIR.glob("*_manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not manifests:
        return {}
    try:
        return json.loads(manifests[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


@router.get("/ths")
def ths_files() -> dict:
    manifest = _latest_manifest()
    if not manifest:
        return {"available": False, "hint": "先跑 scripts/export_ths_watchlist.py",
                "trade_date": None, "sources": [], "how_to_import": None}

    trade_date = manifest.get("trade_date")
    sources = []
    for key in SOURCE_ORDER:
        info = (manifest.get("sources") or {}).get(key) or {}
        if not info.get("codes"):
            continue
        sources.append({
            "key": key,
            "label": SOURCE_LABELS.get(key, key),
            "n": info.get("n", len(info["codes"])),
            "codes": info["codes"],
            "names": info.get("names", []),
            # 只留能点的几个：纯代码 txt / GBK 备份 / 带名称 / CSV / 固定路径副本
            "files": sorted(f for f in info.get("files", []) if f.endswith((".txt", ".csv"))),
        })
    return {
        "available": bool(sources),
        "trade_date": trade_date,
        "dir": str(THS_DIR.relative_to(ROOT)),
        "sources": sources,
        "how_to_import": manifest.get("how_to_import"),
    }


@router.get("/ths/{name}")
def ths_download(name: str):
    path = _safe(name)
    media = "text/plain" if path.suffix == ".txt" else ("application/json" if path.suffix == ".json" else "text/csv")
    return FileResponse(path, media_type=media, filename=path.name)
