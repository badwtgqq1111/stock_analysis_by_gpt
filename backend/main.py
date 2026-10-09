import hashlib
import re
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from backend.api.stocks import router as stocks_router
from backend.api.ohlcv import router as ohlcv_router
from backend.api.selection import router as selection_router
from backend.api.factor_ic import router as factor_ic_router
from backend.api.portfolio import router as portfolio_router
from backend.api.factor_mining import router as factor_mining_router
from backend.api.pipeline import router as pipeline_router
from backend.api.calendar import router as calendar_router
from backend.api.stock_search import router as stock_search_router
from backend.api.automation import router as automation_router
from backend.api.performance import router as performance_router
from backend.api.paper import router as paper_router
from backend.api.exports import router as exports_router
from backend.api.attribution import router as attribution_router
from backend.api.forward import router as forward_router
from backend.api.rankings import router as rankings_router
from backend.api.limitup import router as limitup_router
from backend.api.intraday import router as intraday_router

app = FastAPI(title="Quant Analysis API", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(stocks_router)
app.include_router(ohlcv_router)
app.include_router(selection_router)
app.include_router(factor_ic_router)
app.include_router(portfolio_router)
app.include_router(factor_mining_router)
app.include_router(pipeline_router)
app.include_router(calendar_router)
app.include_router(stock_search_router)
app.include_router(automation_router)
app.include_router(performance_router)
app.include_router(paper_router)
app.include_router(exports_router)
app.include_router(attribution_router)
app.include_router(forward_router)
app.include_router(rankings_router)
app.include_router(limitup_router)
app.include_router(intraday_router)


# ─────────────────────────────────────────────────────────────────────────────
# 前端构建产物（SPA）服务
# ─────────────────────────────────────────────────────────────────────────────
# 历史 bug 1：只找了 repo 根的 frontend/dist，实际在 stock_analysis_by_gpt/frontend/dist
#   → 静态挂载从未生效，`/` 一直是 404。
# 历史 bug 2：用 `app.mount("/", StaticFiles(...))`。挂载会**吞掉**之后注册的路径
#   （修好路径后 `/health` 一度变 404），所以现在改成显式路由，放在所有 router 之后。
# 历史 bug 3（2026-10-08 白屏事故）：静态响应**没有任何 Cache-Control**。
#   `index.html` 被浏览器启发式缓存 → 一直请求上一轮构建的 `/assets/index-<旧hash>.js`
#   → vite 的 emptyOutDir 已把那文件删了 → 404 → 白屏。
#   现在按文件类型分流：
#     /assets/<fingerprint>  → public, max-age=31536000, immutable（内容哈希，永不变）
#     index.html / SPA 回退  → no-cache, must-revalidate（每次回源校验 ETag，命中 304）
#
# 注意：Starlette 1.0.1 的 `FileResponse` **不实现条件请求**（源码里只处理 Range，
# 没有 If-None-Match / 304）。所以下面自己算 validator 并返回 304 —— 否则
# "每次回源校验"会退化成"每次都整包重传"。
_here = Path(__file__).resolve()

# 入口 html 里引用的构建产物：<script src="/assets/x.js"> / <link href="/assets/x.css">
_ASSET_REF_RE = re.compile(r'(?:src|href)="(/assets/[^"]+)"')


def _resolve_frontend_dist() -> Path | None:
    for candidate in (_here.parents[1] / "frontend" / "dist", _here.parents[2] / "frontend" / "dist"):
        if (candidate / "index.html").is_file():
            return candidate
    return None


def _audit_dist(dist: Path) -> dict:
    """启动自检：入口 html 引用的每个 /assets/... 是否真的存在。

    这是白屏事故的**守门人** —— 缺文件时应用照样起来，但会在启动日志和 `/health`
    里明确报出来，而不是等用户对着白屏猜。
    """
    html = (dist / "index.html").read_text(encoding="utf-8", errors="replace")
    refs = sorted(set(_ASSET_REF_RE.findall(html)))
    missing = [r for r in refs if not (dist / r.lstrip("/")).is_file()]
    return {"dist": str(dist), "refs": refs, "missing": missing, "ok": not missing}


def _validators(path: Path) -> tuple[str, str]:
    """(etag, last-modified)：与 Starlette 同款 mtime+size 口径，保证一致。"""
    st = path.stat()
    etag = '"' + hashlib.md5(f"{st.st_mtime}-{st.st_size}".encode(), usedforsecurity=False).hexdigest() + '"'
    return etag, formatdate(st.st_mtime, usegmt=True)


def _file_with_conditional(request: Request, path: Path, cache_control: str) -> Response:
    """按文件类型带缓存策略地返回文件，并正确处理 If-None-Match / If-Modified-Since。"""
    etag, last_modified = _validators(path)
    headers = {"Cache-Control": cache_control, "ETag": etag, "Last-Modified": last_modified}

    inm = request.headers.get("if-none-match")
    if inm is not None and (inm.strip() == "*" or etag in [t.strip() for t in inm.split(",")]):
        return Response(status_code=304, headers=headers)
    ims = request.headers.get("if-modified-since")
    if inm is None and ims:
        try:
            if parsedate_to_datetime(last_modified) <= parsedate_to_datetime(ims):
                return Response(status_code=304, headers=headers)
        except (TypeError, ValueError):
            pass  # 畸形日期 → 当作"不满足条件"，正常返回 200
    return FileResponse(path, headers=headers)


frontend_dist = _resolve_frontend_dist()
frontend_audit: dict = {"dist": None, "refs": [], "missing": [], "ok": False}

if frontend_dist is None:
    print(
        "[frontend] ⚠ 未找到 frontend/dist/index.html —— 前端不会挂载，浏览器打开 `/` 将得到 404。\n"
        "[frontend]   修复：cd frontend && bun install && bun run build",
        flush=True,
    )
else:
    frontend_audit = _audit_dist(frontend_dist)
    if frontend_audit["ok"]:
        print(f"[frontend] dist={frontend_dist}｜入口引用 {len(frontend_audit['refs'])} 个资源，全部存在 ✅",
              flush=True)
    else:
        print(
            f"[frontend] ⚠ 入口 index.html 引用的资源缺失：{frontend_audit['missing']}\n"
            f"[frontend]   这会让浏览器白屏（404 的资源名就是上一轮构建的 hash）。\n"
            f"[frontend]   修复：cd frontend && bun run build   然后**硬刷新**浏览器（Cmd+Shift+R）",
            flush=True,
        )


@app.get("/health")
def health():
    return {"status": "ok", "frontend": frontend_audit}


if frontend_dist is not None:
    _ASSETS_DIR = (frontend_dist / "assets").resolve()
    _DIST_DIR = frontend_dist.resolve()

    def _is_inside(path: Path, root: Path) -> bool:
        """防目录穿越：解析后的路径必须落在 root 之内。"""
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @app.get("/assets/{asset_path:path}", include_in_schema=False)
    def spa_asset(asset_path: str, request: Request):
        target = (_ASSETS_DIR / asset_path).resolve()
        if not _is_inside(target, _ASSETS_DIR) or not target.is_file():
            raise HTTPException(status_code=404, detail="asset not found")
        # 文件名自带内容哈希 ⇒ 内容变了就是新 URL，可以放心长期强缓存
        return _file_with_conditional(request, target, "public, max-age=31536000, immutable")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str, request: Request):
        """静态文件直出；其余（含 hash 路由的入口）一律回 index.html。

        显式挡掉 api/ 前缀：未匹配的 API 路径应返回 404，不能悄悄回一张 HTML。
        """
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="unknown API route")
        if full_path:
            target = (_DIST_DIR / full_path).resolve()
            if _is_inside(target, _DIST_DIR) and target.is_file() and target.name != "index.html":
                return _file_with_conditional(request, target, "public, max-age=3600")
        # 入口 html 绝不能强缓存：浏览器一旦缓存住旧的 index.html，
        # 就会去请求上一轮构建的 /assets/index-<旧hash>.js → 404 → 白屏。
        return _file_with_conditional(request, _DIST_DIR / "index.html", "no-cache, must-revalidate")
