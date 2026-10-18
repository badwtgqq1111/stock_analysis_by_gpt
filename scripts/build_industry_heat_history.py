#!/usr/bin/env python3
"""行业热度 3D 动态排名数据（时间 × 排名 × 行业）。

逐交易日重算行业热度（与 `/api/industry/heat` 同一口径），取窗口内平均热度最高的
`top` 个行业，输出 ranks/heats 矩阵给前端做 3D bump chart。

用法：
  python3 scripts/build_industry_heat_history.py --days 40 --top 20
产物：
  output/research/industry_heat/history.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services import industry_heat_service as ihs  # noqa: E402
from backend.services import market_board_service as mbs  # noqa: E402

OUT = ROOT / "output/research/industry_heat/history.json"


def build(days: int = 40, top: int = 20) -> dict:
    dates = [d.strftime("%Y-%m-%d") for d in mbs.trade_dates(max(days + 10, 400))][-days:]
    by_date: dict[str, dict] = {}
    for i, day in enumerate(dates, 1):
        try:
            payload = ihs.industry_heat(period="daily", limit=400, day=day)
        except Exception as exc:  # 单日失败不拖垮整段
            print(f"[{i}/{len(dates)}] {day} FAILED {exc}", flush=True)
            continue
        if not payload.get("available"):
            print(f"[{i}/{len(dates)}] {day} unavailable", flush=True)
            continue
        by_date[day] = {str(r["industry"]): (int(r["rank"]), float(r["heat"])) for r in payload["rows"]}
        print(f"[{i}/{len(dates)}] {day} industries={len(payload['rows'])}", flush=True)

    if not by_date:
        raise SystemExit("没有可用的行业热度历史")
    # 选**最新交易日**热度排名最靠前的 top 个行业（与页面"热度排名"表同一批），
    # 再回看它们过去 N 天的变化 —— 这比"窗口平均热度"更符合用户看榜的习惯。
    latest = dates[-1]
    latest_table = by_date.get(latest) or {}
    selected = [ind for ind, (_r, _h) in sorted(latest_table.items(), key=lambda x: x[1][0])[:top]]
    # 行业为主序：ranks[industry][date]，前端按行业画轨迹
    ranks = [[by_date[d].get(ind, (None, None))[0] for d in dates] for ind in selected]
    heats = [[by_date[d].get(ind, (None, None))[1] for d in dates] for ind in selected]
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "days": len(dates),
        "top": len(selected),
        "dates": dates,
        "industries": selected,
        "ranks": ranks,
        "heats": heats,
        "source": "backend/services/industry_heat_service.py（日频，PIT）",
        "note": "rank=1 为热度最高；null=当日该行业无数据；行业按窗口平均热度选取。",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"[write] {OUT} industries={len(selected)} dates={len(dates)}")
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description="行业热度 3D 历史")
    ap.add_argument("--days", type=int, default=40)
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()
    build(args.days, args.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
