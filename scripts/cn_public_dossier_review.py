#!/usr/bin/env python3
"""P2.73 草稿复核摘要 / 批量晋级（agent 复核的辅助工具）。

只读 `dossier_draft/`，输出每票的自动核对与结构指标：
  verify = 自动核对通过数/总数（未通过引用已在 draft 阶段自动剔除、refs 连坐过滤）
  b/b/r  = 多头/空头/风险条目数
  rep/ann/news = 证据档里的研报/公告/新闻条数

闸门（blocking）：
  verify   —— 有引用未通过逐字核对（说明草稿里有引用失败；内容仍只保留已核对部分）
  thin_bull / thin_bear / few_risks —— 多空结构过薄
  no_cites / no_stance —— 结构缺失

用法：
  python3 scripts/cn_public_dossier_review.py                 # 打印清单
  python3 scripts/cn_public_dossier_review.py --promote       # 晋级所有 PASS 草稿
  python3 scripts/cn_public_dossier_review.py --codes A,B     # 只看指定票
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "output/results_cn/cn_public_research"
DRAFT_DIR = STORE / "dossier_draft"
EVIDENCE_DIR = STORE / "evidence"


def load_pipeline():
    """按路径加载管线脚本（scripts/ 不是包），复用其 promote 实现。"""
    spec = importlib.util.spec_from_file_location("cn_public_stock_research",
                                                  ROOT / "scripts/cn_public_stock_research.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def evidence_counts(code: str) -> tuple[int, int, int]:
    path = EVIDENCE_DIR / f"{code}.json"
    if not path.exists():
        return 0, 0, 0
    try:
        pack = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0, 0, 0
    return (len(pack.get("reports") or []), len(pack.get("announcements") or []),
            len(pack.get("news") or []))


def review_row(code: str, draft: dict) -> dict:
    verification = draft.get("verification") or {}
    checked = int(verification.get("checked") or 0)
    verified = int(verification.get("verified") or 0)
    failed = verification.get("failed") or []
    bull = len(draft.get("bull") or [])
    bear = len(draft.get("bear") or [])
    risks = len(draft.get("risks") or [])
    citations = draft.get("citations") or []
    reports, annos, news = evidence_counts(code)

    blocking = []
    if verified < checked:
        blocking.append("verify")
    if bull < 3:
        blocking.append("thin_bull")
    if bear < 2:
        blocking.append("thin_bear")
    if risks < 2:
        blocking.append("few_risks")
    if not citations:
        blocking.append("no_cites")
    if not (draft.get("stance") or "").strip():
        blocking.append("no_stance")
    info = []
    if checked < 6:
        info.append("low_checked")
    if reports == 0:
        info.append("no_reports")
    if news == 0:
        info.append("no_news")
    return {
        "code": code, "name": draft.get("name") or "", "stance": draft.get("stance") or "",
        "verified": verified, "checked": checked, "failed": len(failed),
        "bull": bull, "bear": bear, "risks": risks, "cites": len(citations),
        "kinds": sorted({str(c.get("kind") or "") for c in citations if c.get("kind")}),
        "reports": reports, "annos": annos, "news": news,
        "blocking": blocking, "info": info,
        "status": "PASS" if not blocking else "FLAG",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--promote", action="store_true", help="把 PASS 草稿晋级为 dossier")
    parser.add_argument("--codes", default="", help="只看这些代码（逗号分隔）")
    args = parser.parse_args()
    wanted = {part.strip() for part in args.codes.split(",") if part.strip()}

    rows = []
    for path in sorted(DRAFT_DIR.glob("*.json")):
        code = path.stem
        if wanted and code not in wanted:
            continue
        try:
            draft = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows.append(review_row(code, draft))

    header = (f"{'code':10} {'name':10} {'stance':5} {'verify':8} {'b/b/r':8} "
              f"{'rep':>3} {'ann':>3} {'news':>4} kinds")
    print(header)
    for row in rows:
        print(f"{row['code']:10} {row['name'][:10]:10} {row['stance'][:5]:5} "
              f"{row['verified']:>3}/{row['checked']:<4} {row['bull']:>2}/{row['bear']:>2}/{row['risks']:<2} "
              f"{row['reports']:>3} {row['annos']:>3} {row['news']:>4} "
              f"{'/'.join(row['kinds'])[:24]:24} "
              f"{','.join(row['blocking']) or '-'}"
              + (f"  ({','.join(row['info'])})" if row["info"] else ""))
    passed = [row["code"] for row in rows if row["status"] == "PASS"]
    flagged = [row["code"] for row in rows if row["status"] != "PASS"]
    print(f"\n共 {len(rows)} 份草稿：PASS {len(passed)} / FLAG {len(flagged)}")
    print("PASS_CODES=" + ",".join(passed))
    if flagged:
        print("FLAG_CODES=" + ",".join(flagged))

    if args.promote and passed:
        module = load_pipeline()
        for code in passed:
            module.promote(argparse.Namespace(code=code))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
