#!/usr/bin/env python3
"""P2.72 Bynewlt 个股研报调研管线（证据拉取 + 台账）。

用户需求（2026-10-17）："K线图表里的策略选出来的票，每只我都想做一下针对个股的
研报调研，用于对个股的多空分析，方便买卖股票时做为参考。并在前端展示。"

本脚本只负责**取证据**这一段（合成是下一步、由 agent 按 Skill 规则逐条核对后写
`dossier/<code>.json`）：

  * 名单 = 各策略最新名单的并集（与 K 线页左侧自选股分组同一口径：
    config/strategies.json 里带 selection_source + history_dir、且未退休的策略）；
  * 逐票调 Bynewlt 研报库（bynewlt-research 技能的 research.py），
    一次 analysis 模式的公司证据检索（含上下文补全）；原始返回**原样**存档
    （ref/quote/报告标题/机构/日期/PDF页码/来源 SHA 全保留）；
  * 状态机 + 断点续跑（已有状态默认跳过）、批次调用上限（--max-calls）、
    **绝不自动重试**（计次调用纪律：相同参数 10 分钟内本地复用，超时后不自动重发）；
  * 401/402/403/429 一律停止整批（鉴权/额度/限流问题不该继续烧额度）。

用法：
  python3 scripts/bynewlt_stock_research.py dry-run                 # 只列队列（不花钱）
  python3 scripts/bynewlt_stock_research.py fetch                   # 全量拉取（跳过已完成）
  python3 scripts/bynewlt_stock_research.py fetch --codes 600028.SH,300628.SZ
  python3 scripts/bynewlt_stock_research.py fetch --max-calls 50    # 批次上限（默认不限）
  python3 scripts/bynewlt_stock_research.py status                  # 进度汇总
  python3 scripts/bynewlt_stock_research.py show --code 603369.SH   # 单票证据摘要（本地，免费）

产物（output/results_cn/bynewlt_research/）：
  state.json            状态台账（每票 status / 调用次数 / request_id / 时间）
  evidence/<code>.json  原始证据存档（含当日 args 与 structuredContent）
  dossier/<code>.json   合成后的多空调研（由 agent 写，本脚本只读不写）
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "output/results_cn/bynewlt_research"
EVIDENCE_DIR = STORE / "evidence"
DOSSIER_DIR = STORE / "dossier"
DRAFT_DIR = STORE / "dossier_draft"
STATE_PATH = STORE / "state.json"

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

DRAFT_RULES = (
    "你是卖方研报证据整理员：只把资料包里的原文按多空归类，不给投资建议、不补充资料包以外的信息。\n"
    "铁律：\n"
    "1) citations[].quote 必须从对应证据 E# 的原文中**逐字**摘录（英文原文照抄；可用 … 省略中间片段），"
    "不得改写、翻译、拼接不同证据；quote_zh 可以给中文翻译（没有就留空）。\n"
    "2) bull / bear / risks 的每个要点只能引用资料包里存在的编号（如 E3），每条都要有 refs；"
    "没有证据支持的观点不要写。bear 优先收录反向证据（业绩低于预期、预测/评级下修、估值偏高、"
    "减持/解禁、价格或需求走弱、竞争加剧等）；risks 至少 2 条（引用资料包中的风险提示或中性事实），"
    "即使证据整体偏多也不得省略。\n"
    "3) ratings 只写资料包里明确出现的机构/评级/目标价/日期；目标价缺失就留空字符串，不要猜。\n"
    "4) summary 用 1-2 句中文概括证据倾向，出现的数字必须在引用原文里能查到。\n"
    "输出严格 JSON：{\"stance\":\"偏多|偏空|中性|证据不足\",\"summary\":\"\",\"bull\":[{\"point\":\"\",\"refs\":[\"E1\"]}],"
    "\"bear\":[],\"risks\":[],\"ratings\":[{\"broker\":\"\",\"rating\":\"\",\"target_price\":\"\",\"date\":\"\",\"change\":\"\"}],"
    "\"citations\":[{\"id\":\"E1\",\"quote\":\"\",\"quote_zh\":\"\"}]}。"
)

DEFAULT_SKILL_PY = Path.home() / ".claude/skills/bynewlt-research/scripts/research.py"
SKILL_PY = Path(os.environ.get("BYNEWLT_SKILL_PY", str(DEFAULT_SKILL_PY)))

# 生产/实盘优先跑（顺序 = 关注度），其余按代码序。
STRATEGY_PRIORITY = {
    "live_holdings": 0, "cn_ensemble": 1, "arm_select": 1, "arm_concept": 1,
    "rot_arm": 2, "moneyflow_arm": 2, "mainforce_arm": 2, "donchian_breakout": 2,
    "box_arm": 2, "wyckoff_arm": 2, "jq_multi_factor_size_roe": 3, "jq_roe_roa_diagnostic": 3,
    "first_ban_lowxi": 4, "cn_ensemble_volcap": 4, "arm_select_volrisk": 4, "moneyflow_ml_nb": 4,
}

ST_QUEUED, ST_FETCHING, ST_FETCHED = "queued", "fetching", "fetched"
ST_EMPTY, ST_NOT_FOUND = "empty", "not_found"
ST_ERROR, ST_UNKNOWN, ST_REJECTED = "error", "unknown", "rejected"
ST_COMPOSED = "composed"

CST = timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(CST).strftime("%Y-%m-%dT%H:%M:%S%z")


def norm_code(raw: str) -> str:
    """`600519` / `sh600519` / `600519.SH` → `600519.SH`（与后端 _norm 同一口径）。"""
    text = str(raw or "").strip().upper()
    if not text:
        return ""
    if "." in text:
        number, _, market = text.partition(".")
        number, market = number.strip(), market.strip()
        if market in ("SH", "SZ", "BJ"):
            return f"{number}.{market}"
    for prefix in ("SH", "SZ", "BJ"):
        if text.startswith(prefix) and text[len(prefix):].isdigit():
            return f"{text[len(prefix):]}.{prefix}"
    if text.isdigit():
        number = text
        market = "SH" if number[0] in "69" else ("BJ" if number[0] in "48" else "SZ")
        return f"{number}.{market}"
    return text


# ─── 名单采集（与 K 线页自选股分组同一口径）────────────────────────────────

def collect_picks() -> dict[str, dict]:
    registry = json.loads((ROOT / "config/strategies.json").read_text(encoding="utf-8"))
    picks: dict[str, dict] = {}
    used_files: dict[str, str] = {}
    for item in registry.get("strategies", []):
        history = item.get("history_dir")
        if not history or not item.get("selection_source"):
            continue
        if str(item.get("stage") or "") == "retired":
            continue
        files = sorted((ROOT / history).glob("*.csv"))
        if not files:
            continue
        latest = files[-1]
        used_files[str(item["id"])] = str(latest.relative_to(ROOT))
        with open(latest, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            columns = reader.fieldnames or []
            code_col = "stock_code" if "stock_code" in columns else ("code" if "code" in columns else columns[0])
            name_col = "name" if "name" in columns else None
            for row in reader:
                code = norm_code(row.get(code_col))
                if not re.match(r"^\d{6}\.(SH|SZ|BJ)$", code):
                    continue
                entry = picks.setdefault(code, {"name": "", "strategies": []})
                if name_col and not entry["name"]:
                    entry["name"] = str(row.get(name_col) or "").strip()
                if item["id"] not in entry["strategies"]:
                    entry["strategies"].append(str(item["id"]))
    for code, entry in picks.items():
        entry["priority"] = min(STRATEGY_PRIORITY.get(s, 9) for s in entry["strategies"])
    return {"picks": picks, "files": used_files}


# ─── 状态台账 ────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"updated_at": None, "stocks": {}}


def save_state(state: dict) -> None:
    state["updated_at"] = now_iso()
    STORE.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def refresh_state_from_picks(state: dict) -> dict:
    """把当前名单合进台账：新票入队，老票保留状态（名单里删掉的票不删记录、标记 out_of_list）。"""
    collected = collect_picks()
    picks = collected["picks"]
    stocks = state.setdefault("stocks", {})
    current = set(picks)
    for code, entry in picks.items():
        record = stocks.setdefault(code, {"status": ST_QUEUED, "calls_used": 0, "requests": []})
        record["name"] = entry["name"] or record.get("name", "")
        record["strategies"] = entry["strategies"]
        record["priority"] = entry["priority"]
        record.pop("out_of_list", None)
    for code, record in stocks.items():
        if code not in current:
            record["out_of_list"] = True
    state["picks_collected_at"] = now_iso()
    state["picks_source"] = collected["files"]
    return state


def totals(state: dict) -> dict:
    counts: dict[str, int] = {}
    calls = 0
    for record in state["stocks"].values():
        counts[record.get("status", "?")] = counts.get(record.get("status", "?"), 0) + 1
        calls += int(record.get("calls_used") or 0)
    counts["calls_used"] = calls
    return counts


# ─── 单票拉取 ────────────────────────────────────────────────────────────────

def build_args(record: dict, code: str, since: str) -> dict:
    name = record.get("name") or code
    return {
        "company": name,
        "companyCode": code,
        "companyMarket": "A股",
        "question": (f"{name}：2025 年以来的营业收入与净利润变化趋势、最近一期业绩表现"
                     f"（超预期/低于预期及原因）、核心业务/产品与区域结构表现、"
                     f"机构对盈利预测/目标价/评级的调整方向、主要风险因素有哪些？"),
        "mode": "analysis",
        "since": since,
        "limit": 20,
        "maxPerReport": 5,
    }


def call_bynewlt(args: dict, timeout: int) -> tuple[int, dict | None, str, str]:
    """调 research.py call。返回 (rc, stdout_json, stderr_text, error)。不自动重试。"""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    args_file = EVIDENCE_DIR / f".args.{os.getpid()}.json"
    args_file.write_text(json.dumps(args, ensure_ascii=False), encoding="utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, str(SKILL_PY), "call", "search_company_research_evidence",
             "--args-file", str(args_file)],
            capture_output=True, text=True, timeout=timeout,
        )
        payload = None
        if proc.stdout.strip():
            try:
                payload = json.loads(proc.stdout)
            except json.JSONDecodeError as exc:
                return proc.returncode, None, proc.stderr, f"stdout 不是 JSON: {exc}"
        return proc.returncode, payload, proc.stderr, ""
    except subprocess.TimeoutExpired:
        return 2, None, "", f"子进程超时（{timeout}s）"
    except OSError as exc:
        return 1, None, "", f"无法执行 {SKILL_PY}: {exc}"
    finally:
        args_file.unlink(missing_ok=True)


def classify(payload: dict | None, rc: int, record: dict, code: str, args: dict) -> tuple[str, str]:
    """把一次调用结果分类到状态机。返回 (status, note)。"""
    if rc == 2 or payload is None:
        request_id = ""
        return ST_UNKNOWN, f"结果待确认（rc={rc}；用 status <request_id> 核对，不要重复提交）"
    state = str(payload.get("state") or "")
    request_id = str(payload.get("request_id") or "")
    if state == "rejected":
        result = payload.get("result") or {}
        return ST_REJECTED, f"rejected: {json.dumps(result.get('error') or result, ensure_ascii=False)[:200]}"
    if state != "completed":
        return ST_UNKNOWN, f"state={state}（request_id={request_id}）"
    sc = ((payload.get("result") or {}).get("structuredContent") or {})
    status = str(sc.get("status") or "")
    stats = sc.get("stats") or {}
    if status == "company_not_found":
        return ST_NOT_FOUND, "研报库未收录该公司（aliases 契约未命中）"
    if status in ("ambiguous_company",):
        return ST_ERROR, f"公司身份歧义：{sc.get('warnings')}"
    returned = int(stats.get("returnedItems") or 0)
    if returned == 0:
        return ST_EMPTY, f"库中无匹配证据（status={status}）"
    note = f"status={status} returned={returned} reports={stats.get('reportCount')}"
    return ST_FETCHED, note


def fetch(args) -> int:
    state = refresh_state_from_picks(load_state())
    save_state(state)
    stocks = state["stocks"]

    if args.codes:
        wanted = {norm_code(c) for c in args.codes.split(",") if c.strip()}
        queue = sorted(c for c in wanted if c in stocks)
        missing = wanted - set(queue)
        if missing:
            print(f"[WARN] 不在名单里的代码：{sorted(missing)}")
        if args.refetch:
            pass  # 显式点名即视为重拉意图
    elif args.refetch:
        queue = sorted(c for c, r in stocks.items()
                       if not r.get("out_of_list") and r.get("status") != ST_FETCHING)
    else:
        queue = sorted(c for c, r in stocks.items()
                       if not r.get("out_of_list") and r.get("status") in (ST_QUEUED, ST_ERROR, ST_EMPTY, ST_UNKNOWN)
                       and not (args.skip_error and r.get("status") in (ST_ERROR, ST_UNKNOWN)))

    if not args.refetch and not args.codes:
        queue = sorted(queue, key=lambda c: (stocks[c].get("priority", 9), c))

    if args.max_calls:
        queue = queue[: args.max_calls]

    if not queue:
        print("没有需要拉取的票（全部已有终态）。status:")
        print(json.dumps(totals(state), ensure_ascii=False))
        return 0

    print(f"待拉取 {len(queue)} 只（sleep {args.sleep}s/票，上限 {args.max_calls or '不限'}）")
    calls_used = 0
    stopped = ""
    for index, code in enumerate(queue, 1):
        record = stocks[code]
        record["status"] = ST_FETCHING
        save_state(state)
        call_args = build_args(record, code, args.since)
        started = time.time()
        rc, payload, stderr, error = call_bynewlt(call_args, args.timeout)
        calls_used += 1  # 保守计数：凡提交就算一次（空结果也可能计次）
        record["calls_used"] = int(record.get("calls_used") or 0) + 1
        request_id = ""
        stderr_id = re.search(r'"request_id":\s*"([^"]+)"', stderr or "")
        if stderr_id:
            request_id = stderr_id.group(1)
        if payload and payload.get("request_id"):
            request_id = str(payload["request_id"])

        if error and payload is None:
            status, note = ST_ERROR, error
        else:
            status, note = classify(payload, rc, record, code, call_args)

        record["status"] = status
        record["note"] = note
        record["fetched_at"] = now_iso()
        record.setdefault("requests", []).append(
            {"tool": "search_company_research_evidence", "request_id": request_id,
             "state": (payload or {}).get("state"), "rc": rc, "at": record["fetched_at"]})
        save_state(state)

        if status in (ST_FETCHED, ST_EMPTY, ST_NOT_FOUND) and payload is not None:
            evidence_file = EVIDENCE_DIR / f"{code}.json"
            sc = ((payload.get("result") or {}).get("structuredContent") or {})
            archive = {
                "code": code, "name": record.get("name", ""),
                "strategies": record.get("strategies", []),
                "fetched_at": record["fetched_at"], "request_id": request_id,
                "args": call_args, "status": sc.get("status"), "stats": sc.get("stats"),
                "warnings": sc.get("warnings"), "evidence": sc.get("evidence", []),
                "context": sc.get("context", []),
                "contextDiagnostics": sc.get("contextDiagnostics"),
                "rerankDiagnostics": sc.get("rerankDiagnostics"),
                "sourceContract": sc.get("sourceContract"), "contractVersion": sc.get("contractVersion"),
            }
            tmp = evidence_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(archive, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, evidence_file)
            record["evidence_file"] = f"evidence/{code}.json"

        elapsed = time.time() - started
        flag = {"fetched": "✓", "empty": "○", "not_found": "·", "error": "!", "unknown": "?", "rejected": "✗"}.get(status, "?")
        print(f"[{index}/{len(queue)}] {flag} {code} {record.get('name','')} -> {status} "
              f"({elapsed:.1f}s) {note[:120]}")

        if status == ST_REJECTED:
            stopped = f"批量中止：计费/鉴权问题（{note}）"
            break
        if args.max_calls and calls_used >= args.max_calls:
            stopped = "达到 --max-calls 上限"
            break
        if index < len(queue):
            time.sleep(args.sleep)

    print("\n本批结束。" + (f" {stopped}。" if stopped else ""))
    print("累计调用（本批）:", calls_used)
    print("状态汇总:", json.dumps(totals(state), ensure_ascii=False))
    return 0


# ─── 查看 ────────────────────────────────────────────────────────────────────

def show(args) -> int:
    code = norm_code(args.code)
    path = EVIDENCE_DIR / f"{code}.json"
    if not path.exists():
        print(f"没有证据档：{path}")
        return 1
    archive = json.loads(path.read_text(encoding="utf-8"))
    if args.json:
        print(json.dumps(archive, ensure_ascii=False, indent=1))
        return 0
    print(f"== {code} {archive.get('name')} | {archive.get('fetched_at')} | status={archive.get('status')} | stats={archive.get('stats')}")
    if archive.get("warnings"):
        print("warnings:", archive["warnings"])
    for i, ev in enumerate(archive.get("evidence", []), 1):
        print(f"\n[{i}] ref={ev.get('ref')} | {ev.get('broker')} | {ev.get('reportDate')} | p{ev.get('page')} | {ev.get('metric') or ev.get('dataNature') or ''}")
        print(f"    title: {(ev.get('reportTitle') or '')[:100]}")
        claim = ev.get("claim") or ev.get("quote") or ""
        print(f"    claim: {claim}")
    for i, cx in enumerate(archive.get("context", []), 1):
        print(f"\n(c{i}) ref={cx.get('ref')} p{cx.get('page')} [{cx.get('relation')}] {(cx.get('quote') or '')[:400]}")
    return 0


def status(args) -> int:
    sys.path_missing = False
    state = load_state()
    if not state.get("stocks"):
        print("台账为空：先跑 `fetch`（或 dry-run 看名单）")
        return 0
    counts = totals(state)
    print(f"更新于 {state.get('updated_at')} | 名单采集于 {state.get('picks_collected_at')}")
    print("状态汇总:", json.dumps(counts, ensure_ascii=False, indent=1))
    composed = [c for c, r in state["stocks"].items() if r.get("status") == ST_COMPOSED]
    fetched = [c for c, r in state["stocks"].items() if r.get("status") == ST_FETCHED]
    print(f"已合成 {len(composed)} 只；已取证据待合成 {len(fetched)} 只: {sorted(fetched)[:20]}")
    if args.verbose:
        for code in sorted(state["stocks"]):
            r = state["stocks"][code]
            print(f"  {code} {r.get('name',''):8s} {r.get('status'):10s} calls={r.get('calls_used',0)} {r.get('note','')[:60]}")
    return 0


def mark(args) -> int:
    """人工更新单票状态（本地、免费）：合成完把票标 composed，或标注 error 结论等。"""
    state = load_state()
    code = norm_code(args.code)
    if not code:
        print("缺少/无法识别 --code")
        return 1
    record = state.setdefault("stocks", {}).setdefault(code, {"calls_used": 0, "requests": []})
    record["status"] = args.status
    record["marked_at"] = now_iso()
    if args.note:
        record["note"] = args.note
    save_state(state)
    print(json.dumps({"code": code, "status": record["status"], "note": record.get("note", "")},
                     ensure_ascii=False))
    return 0


def dry_run(args) -> int:
    collected = collect_picks()
    picks = collected["picks"]
    state = load_state()
    stocks = state.get("stocks", {})
    print(f"名单：{len(picks)} 只（{len(collected['files'])} 个策略名单文件）")
    by_status: dict[str, list[str]] = {}
    for code, entry in sorted(picks.items(), key=lambda x: (x[1]["priority"], x[0])):
        status = stocks.get(code, {}).get("status", "new")
        by_status.setdefault(status, []).append(f"{code} {entry['name']} [{'/'.join(entry['strategies'])}]")
    for status, rows in by_status.items():
        print(f"\n-- {status} ({len(rows)}) --")
        for row in rows[:200]:
            print("  " + row)
    planned = len([c for c in picks if stocks.get(c, {}).get("status", "new") == "new"
                   or stocks[c].get("status") in (ST_QUEUED, ST_ERROR, ST_EMPTY, ST_UNKNOWN)])
    print(f"\n预计需要调用：{planned} 次（每票 1 次；未收录的也计 1 次）；不带 --max-calls 即全量。")
    return 0


# ─── 多空稿：DeepSeek 起草 + 引文逐条核对 + agent 复核 ─────────────────────

def _norm_text(text: str) -> str:
    """NFKC + 空白折叠 + 小写：中英文混排、PDF 断行都能对齐。"""
    value = unicodedata.normalize("NFKC", str(text or "")).lower()
    return re.sub(r"\s+", " ", value).strip()


def evidence_items(archive: dict) -> list[dict]:
    """资料包条目：优先 quote（原文），没有 quote 才退回 claim。"""
    items = []
    for ev in archive.get("evidence") or []:
        quote = str(ev.get("quote") or ev.get("claim") or "").strip()
        if not quote:
            continue
        items.append({**ev, "quote": quote})
    return items


def build_prompt(archive: dict, max_chars: int = 1800) -> str:
    """把证据档摊平成编号资料包（E1..En）；编号即引用 id。"""
    items = evidence_items(archive)
    lines = [
        f"公司：{archive.get('name', '')}（{archive.get('code', '')}）",
        f"报告窗口：{(archive.get('args') or {}).get('since', '')} 起；证据 {len(items)} 条",
        "",
        "资料包：",
    ]
    for index, ev in enumerate(items, 1):
        title = str(ev.get("reportTitle") or ev.get("report_title") or "")
        quote = str(ev.get("quote") or "")
        if len(quote) > max_chars:
            quote = quote[:max_chars] + "…"
        lines.extend([
            f"[E{index}] 机构={ev.get('broker') or ''} | 日期={ev.get('reportDate') or ev.get('report_date') or ''} "
            f"| 页码={ev.get('page') or ''} | 标题={title}",
            f"原文：{quote}",
            "",
        ])
    return "\n".join(lines)


def verify_citations(archive: dict, draft: dict) -> dict:
    """逐条核对 citation.quote 是否能在这条证据原文里找到（允许 … 分段）。"""
    items = evidence_items(archive)
    corpus = {}
    for index, ev in enumerate(items, 1):
        corpus[f"E{index}"] = _norm_text(ev.get("quote") or "")

    checked = 0
    failed = []
    citations = []
    used_ids: dict[str, int] = {}
    for citation in draft.get("citations") or []:
        base_id = str(citation.get("id") or "").strip().upper()
        quote = str(citation.get("quote") or "").strip()
        checked += 1
        target = corpus.get(base_id)
        if target is None or not quote:
            failed.append({"id": base_id, "reason": "missing_corpus_or_quote"})
            continue
        matched = re.match(r"^E(\d+)$", base_id)
        if not matched or not (1 <= int(matched.group(1)) <= len(items)):
            failed.append({"id": base_id, "reason": "invalid_id"})
            continue
        segments = [seg for seg in re.split(r"(?:\.{3,}|…+)", quote) if seg.strip()]
        if not segments or not all(_norm_text(seg) in target for seg in segments):
            failed.append({"id": base_id, "reason": "quote_not_found", "quote": quote[:80]})
            continue
        ev = items[int(matched.group(1)) - 1]
        # 同一条证据可能被引用多段：生成唯一 id（E1 / E1b / E1c…），否则前端 key 重复。
        used_ids[base_id] = used_ids.get(base_id, 0) + 1
        cid = base_id if used_ids[base_id] == 1 else f"{base_id}{chr(97 + used_ids[base_id] - 1)}"
        citations.append({
            "id": cid,
            "evidence_id": base_id,
            "quote": quote,
            "quote_zh": str(citation.get("quote_zh") or ""),
            "broker": ev.get("broker") or "",
            "reportTitle": ev.get("reportTitle") or ev.get("report_title") or "",
            "reportDate": ev.get("reportDate") or ev.get("report_date") or "",
            "page": ev.get("page"),
            "verified": "cite",
        })

    valid_ids = {c["id"] for c in citations}
    fallback = {}
    for citation in citations:
        fallback.setdefault(citation.get("evidence_id") or citation["id"], citation["id"])
    for section in ("bull", "bear", "risks"):
        kept = []
        for point in draft.get(section) or []:
            refs = [str(r).strip().upper() for r in (point.get("refs") or [])]
            refs = [r if r in valid_ids else fallback.get(r) for r in refs]
            refs = [r for r in refs if r in valid_ids]
            refs = list(dict.fromkeys(refs))
            text = str(point.get("point") or "").strip()
            if text and refs:
                kept.append({"point": text, "refs": refs})
        draft[section] = kept
    draft["citations"] = citations
    draft["verification"] = {
        "level": "auto_checked",
        "method": "quote-in-evidence 子串逐条比对（含 … 分段）+ refs 合法性过滤",
        "checked": checked,
        "verified": len(citations),
        "failed": failed[:20],
        "checked_at": now_iso(),
    }
    return draft


def call_deepseek(prompt: str, timeout: int = 240) -> tuple[dict | None, str]:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        return None, "missing DEEPSEEK_API_KEY"
    response = requests.post(
        DEEPSEEK_URL, timeout=timeout,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": DEEPSEEK_MODEL, "temperature": 0.2,
              "response_format": {"type": "json_object"},
              "messages": [{"role": "system", "content": DRAFT_RULES},
                           {"role": "user", "content": prompt}]},
    )
    if response.status_code != 200:
        return None, f"http_{response.status_code}: {response.text[:200]}"
    content = ((response.json().get("choices") or [{}])[0].get("message") or {}).get("content", "")
    try:
        return json.loads(content), ""
    except json.JSONDecodeError:
        return None, f"bad_json: {content[:200]}"


def draft(args) -> int:
    """把 fetched 证据整理成多空草稿（DeepSeek；引文自动核对）。"""
    state = load_state()
    stocks = state.get("stocks") or {}
    codes = [norm_code(part) for part in (getattr(args, "codes", "") or "").split(",") if part.strip()]
    if not codes:
        codes = sorted(
            (c for c, r in stocks.items()
             if r.get("status") == ST_FETCHED and (EVIDENCE_DIR / f"{c}.json").exists()
             and (getattr(args, "refetch", False) or not (DRAFT_DIR / f"{c}.json").exists())),
            key=lambda c: (stocks[c].get("priority", 9), c),
        )
    print(f"起草 {len(codes)} 只：{codes}", flush=True)
    DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    done = 0
    for code in codes:
        path = EVIDENCE_DIR / f"{code}.json"
        if not path.exists():
            print(f"  {code}: 无证据档，跳过", flush=True)
            continue
        archive = json.loads(path.read_text(encoding="utf-8"))
        print(f"  {code} {archive.get('name', '')}: 生成草稿…", flush=True)
        payload, error = call_deepseek(build_prompt(archive))
        if payload is None:
            print(f"    -> 失败：{error}", flush=True)
            continue
        payload = verify_citations(archive, payload)
        payload.update({
            "code": code,
            "name": archive.get("name", ""),
            "source": "bynewlt",
            "updated_at": now_iso(),
            "window": f"报告窗口 {(archive.get('args') or {}).get('since', '')} 起",
            "evidence_file": f"evidence/{code}.json",
        })
        tmp = (DRAFT_DIR / f"{code}.json").with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, DRAFT_DIR / f"{code}.json")
        v = payload["verification"]
        print(f"    -> 草稿完成：verified {v['verified']}/{v['checked']}，"
              f"bull={len(payload.get('bull') or [])} bear={len(payload.get('bear') or [])}", flush=True)
        done += 1
        time.sleep(max(0.0, float(getattr(args, "sleep", 1.0) or 0)))
    print(f"完成 {done} 只草稿 -> {DRAFT_DIR.relative_to(ROOT)}")
    return 0


def promote(args) -> int:
    """agent 复核后：草稿 → dossier，并把台账置 composed。"""
    code = norm_code(args.code)
    path = DRAFT_DIR / f"{code}.json"
    if not path.exists():
        print(f"没有草稿：{path}")
        return 1
    dossier = json.loads(path.read_text(encoding="utf-8"))
    verification = dossier.get("verification") or {}
    dossier["verification"] = {
        "level": str(getattr(args, "level", "agent_checked") or "agent_checked"),
        "method": "自动引文比对 + agent 复核（数值/口径/结论）",
        "verified": verification.get("verified"),
        "checked": verification.get("checked"),
        "checked_at": verification.get("checked_at"),
    }
    dossier["promoted_at"] = now_iso()
    DOSSIER_DIR.mkdir(parents=True, exist_ok=True)
    tmp = (DOSSIER_DIR / f"{code}.json").with_suffix(".json.tmp")
    tmp.write_text(json.dumps(dossier, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, DOSSIER_DIR / f"{code}.json")
    state = load_state()
    record = state.setdefault("stocks", {}).setdefault(code, {"calls_used": 0, "requests": []})
    record["status"] = ST_COMPOSED
    record["composed_at"] = now_iso()
    save_state(state)
    print(json.dumps({"code": code, "status": ST_COMPOSED,
                      "verified": verification.get("verified"),
                      "checked": verification.get("checked")}, ensure_ascii=False))
    return 0


def review(args) -> int:
    """批量体检草稿（本地免费）：引用核验率、要点数量、评级空缺、可疑数字。"""
    codes = [norm_code(part) for part in (getattr(args, "codes", "") or "").split(",") if part.strip()]
    if not codes:
        codes = sorted(p.stem for p in DRAFT_DIR.glob("*.json")) if DRAFT_DIR.exists() else []
    flagged = 0
    for code in codes:
        path = DRAFT_DIR / f"{code}.json"
        if not path.exists():
            print(f"{code}: 无草稿")
            continue
        d = json.loads(path.read_text(encoding="utf-8"))
        v = d.get("verification") or {}
        checked, verified = int(v.get("checked") or 0), int(v.get("verified") or 0)
        ratio = (verified / checked) if checked else 0.0
        issues = []
        if ratio < 0.8:
            issues.append(f"引文核验率低 {verified}/{checked}")
        if not (d.get("bull") or []):
            issues.append("无看多要点")
        if not (d.get("bear") or []):
            issues.append("无看空要点")
        if not (d.get("citations") or []):
            issues.append("无有效引用")
        if not (d.get("ratings") or []):
            issues.append("无评级行")
        if len(str(d.get("summary") or "")) < 20:
            issues.append("摘要过短")
        if issues:
            flagged += 1
        flag = "!" if issues else "✓"
        print(f"{flag} {code} {d.get('name','')}: stance={d.get('stance')} "
              f"bull={len(d.get('bull') or [])} bear={len(d.get('bear') or [])} "
              f"cite={verified}/{checked} {'; '.join(issues)}")
    print(f"\n体检 {len(codes)} 只，标记 {flagged} 只")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="P2.72 Bynewlt 个股研报调研管线（取证据）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="逐票拉取证据（计费；默认跳过已完成）")
    p_fetch.add_argument("--codes", default="", help="逗号分隔，只拉这些票")
    p_fetch.add_argument("--since", default="2025-01-01", help="报告发布日期下限（默认 2025-01-01）")
    p_fetch.add_argument("--max-calls", type=int, default=None, help="本批调用上限（保守计数：提交即算）")
    p_fetch.add_argument("--sleep", type=float, default=1.5, help="每票间隔秒数（默认 1.5）")
    p_fetch.add_argument("--timeout", type=int, default=180, help="单次调用超时秒数")
    p_fetch.add_argument("--refetch", action="store_true", help="重拉已有终态的票（10 分钟内相同参数会本地复用不重复计费）")
    p_fetch.add_argument("--skip-error", action="store_true", help="跳过 error/unknown 状态的票（只拉 queued/empty）")
    p_fetch.set_defaults(func=fetch)

    p_show = sub.add_parser("show", help="单票证据摘要（本地，免费）")
    p_show.add_argument("--code", required=True)
    p_show.add_argument("--json", action="store_true")
    p_show.set_defaults(func=show)

    p_status = sub.add_parser("status", help="进度汇总（本地，免费）")
    p_status.add_argument("--verbose", action="store_true")
    p_status.set_defaults(func=status)

    p_dry = sub.add_parser("dry-run", help="列出队列（不调用）")
    p_dry.set_defaults(func=dry_run)

    p_mark = sub.add_parser("mark", help="人工改单票状态（本地，免费）：合成完标 composed 等")
    p_mark.add_argument("--code", required=True)
    p_mark.add_argument("--status", required=True,
                        choices=[ST_QUEUED, ST_FETCHING, ST_FETCHED, ST_EMPTY, ST_NOT_FOUND,
                                 ST_ERROR, ST_UNKNOWN, ST_REJECTED, ST_COMPOSED])
    p_mark.add_argument("--note", default="")
    p_mark.set_defaults(func=mark)

    p_draft = sub.add_parser("draft", help="证据 → 多空草稿（DeepSeek；引文自动核对）")
    p_draft.add_argument("--codes", default="", help="逗号分隔；默认全部 fetched 票")
    p_draft.add_argument("--sleep", type=float, default=1.0, help="每票间隔秒数")
    p_draft.add_argument("--refetch", action="store_true", help="已有草稿也重写")
    p_draft.set_defaults(func=draft)

    p_promote = sub.add_parser("promote", help="草稿 → dossier（agent 复核后）")
    p_promote.add_argument("--code", required=True)
    p_promote.add_argument("--level", default="agent_checked",
                           choices=["auto_checked", "agent_checked"],
                           help="未人工复核的批次请用 auto_checked")
    p_promote.set_defaults(func=promote)

    p_review = sub.add_parser("review", help="批量体检草稿（本地，免费）")
    p_review.add_argument("--codes", default="")
    p_review.set_defaults(func=review)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
