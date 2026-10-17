"""逐股研报多空调研 API（K 线页「研报多空调研」面板的数据源）。

用户要求（2026-10-17）："K线图表里的策略选出来的票，每只我都想做一下针对个股的研报
调研，用于对个股的多空分析，方便买卖股票时做为参考。并在前端展示。"

数据两层（Bynewlt 库在 `output/results_cn/bynewlt_research/`，公开来源在
`output/results_cn/cn_public_research/`）：
  * `state.json` + `evidence/<code>.json` —— 管线脚本（`bynewlt_stock_research.py` /
    `cn_public_stock_research.py`）取回的**原始证据**；
  * `dossier/<code>.json` —— agent 逐条核对后撰写的多空要点（每条挂 citation refs；
    核对级别、口径、时间窗写在 verification/caveats 里）。

覆盖顺序：Bynewlt dossier 优先；缺则回落到公开来源 dossier（response.source 标明
bynewlt / public_web，二者不混同）。本 API 只读盘、不发任何计费请求；面板上的
"刷新"= 重新读盘。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

from backend.api.ai_picks import _norm, _strategy_names

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "output/results_cn/bynewlt_research"
STATE_PATH = STORE / "state.json"
EVIDENCE_DIR = STORE / "evidence"
DOSSIER_DIR = STORE / "dossier"

PUBLIC_STORE = ROOT / "output/results_cn/cn_public_research"
PUBLIC_STATE_PATH = PUBLIC_STORE / "state.json"
PUBLIC_EVIDENCE_DIR = PUBLIC_STORE / "evidence"
PUBLIC_DOSSIER_DIR = PUBLIC_STORE / "dossier"

router = APIRouter(prefix="/api", tags=["research"])

_cache: dict[str, object] = {"signature": None, "state": None}
_public_cache: dict[str, object] = {"signature": None, "state": None}


def _load_state() -> dict:
    """读台账。用 mtime 当签名缓存：拉取进程每票都会原子重写一次 state.json。"""
    if not STATE_PATH.exists():
        return {"stocks": {}}
    try:
        signature = STATE_PATH.stat().st_mtime
    except OSError:
        signature = 0.0
    if _cache["signature"] == signature and _cache["state"] is not None:
        return _cache["state"]  # type: ignore[return-value]
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {"stocks": {}}
    _cache.update({"signature": signature, "state": payload})
    return payload


def _load_public_state() -> dict:
    """读公开来源台账（同样的 mtime 签名缓存）。"""
    if not PUBLIC_STATE_PATH.exists():
        return {"stocks": {}}
    try:
        signature = PUBLIC_STATE_PATH.stat().st_mtime
    except OSError:
        signature = 0.0
    if _public_cache["signature"] == signature and _public_cache["state"] is not None:
        return _public_cache["state"]  # type: ignore[return-value]
    try:
        payload = json.loads(PUBLIC_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {"stocks": {}}
    _public_cache.update({"signature": signature, "state": payload})
    return payload


def _load_json(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _evidence_digest(code: str) -> dict | None:
    """证据档摘要（只读 evidence/<code>.json，不读回全部正文）。"""
    archive = _load_json(EVIDENCE_DIR / f"{code}.json")
    if not archive:
        return None
    evidence = archive.get("evidence") or []
    context = archive.get("context") or []
    brokers = sorted({str(e.get("broker")) for e in evidence if e.get("broker")})
    reports = sorted({str(e.get("reportTitle")) for e in evidence if e.get("reportTitle")})
    dates = sorted(str(e.get("reportDate")) for e in evidence if e.get("reportDate"))
    stats = archive.get("stats") or {}
    return {
        "items": len(evidence),
        "context": len(context),
        "reports": len(reports),
        "brokers": brokers,
        "report_dates": [dates[0], dates[-1]] if dates else [],
        "window_since": (archive.get("args") or {}).get("since"),
        "fetched_at": archive.get("fetched_at"),
        "library_status": archive.get("status"),
        "returned_items": stats.get("returnedItems"),
    }


def _public_evidence_digest(code: str) -> dict | None:
    """公开来源证据档摘要（口径对齐 Bynewlt 版，附公开来源细分计数）。"""
    archive = _load_json(PUBLIC_EVIDENCE_DIR / f"{code}.json")
    if not archive:
        return None
    reports = archive.get("reports") or []
    annos = archive.get("announcements") or []
    news = archive.get("news") or []
    tushare = archive.get("tushare") or {}
    tushare_counts = {k: len(v) for k, v in tushare.items() if isinstance(v, list) and v}
    brokers = sorted({str(r.get("org")) for r in reports if r.get("org")})
    dates = sorted(str(r.get("date")) for r in reports if r.get("date"))
    return {
        "items": len(reports) + len(annos) + len(news),
        "context": 0,
        "reports": len(reports),
        "report_pdfs": sum(1 for r in reports if (r.get("pdf") or {}).get("ok")),
        "announcements": len(annos),
        "announcement_pdfs": sum(1 for a in annos if a.get("pdf_text")),
        "news": len(news),
        "news_text": sum(1 for n in news if n.get("text_ok")),
        "brokers": brokers,
        "report_dates": [dates[0], dates[-1]] if dates else [],
        "window_since": None,
        "tushare": tushare_counts,
        "fetched_at": archive.get("fetched_at"),
        "library_status": None,
        "returned_items": None,
        "source": "public_web",
    }


def _hint(state_label: str, evidence: dict | None, note: str, *, origin: str = "bynewlt") -> str:
    """面板在"还没有合成稿"时显示的下一步说明（按状态机给话）。"""
    items = (evidence or {}).get("items")
    antechamber = ("Bynewlt 研报库未收录，已转公开来源兜底"
                   "（东财研报/巨潮公告/searxng/结构化数据）。") if origin == "public" else ""
    if state_label == "queued":
        return antechamber + ("已入公开来源采集队列，稍后回来看。" if origin == "public"
                              else "已入研报调研队列（按名单顺序逐票拉取证据），稍后回来看。")
    if state_label == "fetching":
        return antechamber + ("正在采集公开来源证据（研报/公告/新闻/行情与结构化数据）…" if origin == "public"
                              else "正在从 Bynewlt 研报库拉取证据…")
    if state_label == "fetched":
        if origin == "public":
            return (antechamber + f"公开来源证据已取回（{items if items is not None else '—'} 条："
                    "券商研报/公告/新闻/结构化数据），多空稿按名单顺序逐票整理中；先看下方证据摘要。")
        return (f"证据已取回（{items if items is not None else '—'} 条原始引文），"
                "多空研判按名单顺序逐票整理中；先看下方覆盖信息。")
    if state_label == "no_data":
        return antechamber + "公开来源未取到有效证据（可能无对应研报/公告或代码有误），不编造内容。"
    if state_label == "not_found":
        return ("Bynewlt 研报库当前未收录该公司。未收录不等于没有研报——库在持续扩充，"
                "之后可重查；本条不计入「已调研」覆盖。")
    if state_label == "empty":
        return "库中暂无匹配本次提问的证据（公司已被收录但近一年半无命中报告）。"
    if state_label == "error":
        return (antechamber if origin == "public" else "") + f"拉取出错，且按计费纪律**不自动重试**：{note or '—'}"
    if state_label == "unknown":
        return f"调用结果待人工确认（为避免重复计费未自动重试）：{note or '—'}"
    if state_label == "rejected":
        return f"调用被拒（鉴权/额度/限流问题，已停止整批）：{note or '—'}"
    return ""


@router.get("/stocks/{code}/research")
def stock_research(code: str) -> dict:
    """这只票的研报多空调研：Bynewlt 优先，缺则回落公开来源；覆盖状态 + 证据摘要 +（若有）多空稿。"""
    target = _norm(code)
    if not target:
        raise HTTPException(status_code=400, detail="缺少股票代码")
    state = _load_state()
    record = (state.get("stocks") or {}).get(target)
    dossier = _load_json(DOSSIER_DIR / f"{target}.json")
    source = "bynewlt" if dossier is not None else None

    public_state = _load_public_state()
    public_record = (public_state.get("stocks") or {}).get(target)
    if dossier is None:
        public_dossier = _load_json(PUBLIC_DOSSIER_DIR / f"{target}.json")
        if public_dossier is not None:
            dossier = public_dossier
            source = "public_web"

    # 证据摘要跟随稿的来源；没有稿时优先有内容的 Bynewlt 摘要，缺/空则回落公开来源
    # （Bynewlt 对 not_found 票也会落一份空证据档，不能让它挡住公开来源的全文证据）
    bynewlt_evidence = _evidence_digest(target)
    public_evidence = _public_evidence_digest(target)
    if source == "public_web":
        evidence = public_evidence or bynewlt_evidence
    elif bynewlt_evidence is not None and (bynewlt_evidence.get("items") or 0) > 0:
        evidence = bynewlt_evidence
    else:
        evidence = public_evidence or bynewlt_evidence

    if record is None and public_record is None and dossier is None:
        return {
            "code": target, "in_scope": False, "state": "not_in_scope", "name": "",
            "source": None, "dossier": None, "evidence": None,
            "hint": ("该票不在本轮研报调研名单（各策略最新名单的并集）里；"
                     "等名单刷新或按需点名调研后再来看。"),
        }

    strategies = [str(s) for s in (record or {}).get("strategies") or []]
    names = _strategy_names()
    record_status = str((record or {}).get("status") or "")
    public_status = str((public_record or {}).get("status") or "")
    if dossier is not None:
        state_label, origin = "composed", (source or "bynewlt")
    elif record is not None and record_status != "not_found":
        state_label, origin = (record_status or "unknown"), "bynewlt"
    elif public_record is not None:
        state_label, origin = (public_status or "unknown"), "public"
    elif record is not None:
        state_label, origin = "not_found", "bynewlt"
    else:
        state_label, origin = "unknown", None  # 有稿但台账没记录（手工放的稿）：以稿为准展示
    active_record = record if origin == "bynewlt" else (public_record or record)
    return {
        "code": target,
        "in_scope": record is not None or public_record is not None or dossier is not None,
        "name": (record or public_record or {}).get("name") or (dossier or {}).get("name") or "",
        "state": state_label,
        "source": source,
        "note": (active_record or {}).get("note") or "",
        "fetched_at": ((active_record or {}).get("fetched_at")
                       or (evidence or {}).get("fetched_at")),
        "calls_used": (record or {}).get("calls_used"),
        "strategies": strategies,
        "strategy_labels": [names.get(s, s) for s in strategies],
        "dossier": dossier,
        "evidence": evidence,
        "hint": _hint(state_label, evidence, (active_record or {}).get("note") or "",
                      origin=origin or "bynewlt"),
    }
