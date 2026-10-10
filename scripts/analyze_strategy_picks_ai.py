#!/usr/bin/env python3
"""对当日**全部策略**（生产 + 备选/候选）的名单逐股做多空研究。

覆盖范围：`config/strategies.json` 里每一项带 `history_dir` + `selection_source` 的策略——
生产策略（cn_ensemble / arm_select / arm_concept）与候选策略（jq_multi_factor / jq_roe_roa）
一视同仁；另外把当日 `feishu_<date>.json` 的最终通知名单也并进来（PK 后的名单不一定与
preselection 快照逐票一致）。

产物：`output/results_cn/ai_pick_analysis/<date>.{json,csv}`，
`scripts/build_selection_ledger.py` 会把它合进选股台账，前端 K 线页与选股日历读同一份。

--skip-existing：同一天重复触发（每日自动化 19:00/20:30/22:00 的失败重试）时，
已完成（status=completed）的股票不再重复调用 API，只补还挂着"待分析"的那部分。
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "output/results_cn"
REGISTRY = ROOT / "config/strategies.json"
OUT = RESULTS / "ai_pick_analysis"
ACTIONS = {"买入", "持有", "卖出"}


def _value(value):
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        value = value.item()
    return value


def collect(date: str, registry: Path = REGISTRY, results: Path = RESULTS) -> list[dict]:
    """按决策日和代码去重，保留全部命中策略及可核实的当日字段。

    每只票同时带上 `strategies`（注册表 id，前端用它映射中文名）与 `strategies_zh`
    （中文名，喂给模型）—— 否则模型会在多空分析正文里冒出一串英文 id
    （实测："在 jq_roe_roa_diagnostic 策略中排名第 8"），用户读起来不知所云。
    """
    strategies = json.loads(registry.read_text(encoding="utf-8"))["strategies"]
    names = {str(s["id"]): str(s.get("name") or s["id"]) for s in strategies if s.get("id")}
    found: dict[str, dict] = {}
    for strategy in strategies:
        history = strategy.get("history_dir")
        if not history or not strategy.get("selection_source"):
            continue
        path = (registry.parent.parent / history / f"{date}.csv")
        if not path.exists():
            continue
        frame = pd.read_csv(path)
        for _, row in frame.iterrows():
            if "trade_date" in row and str(row["trade_date"])[:10] != date:
                continue
            code = str(row.get("stock_code") or "").strip()
            if not code or code == "nan":
                continue
            item = found.setdefault(code, {"stock_code": code, "name": "", "strategies": [],
                                          "strategies_zh": [], "signals": []})
            if pd.notna(row.get("name")) and not item["name"]:
                item["name"] = str(row["name"])
            item["strategies"].append(strategy["id"])
            if names.get(str(strategy["id"])) not in item["strategies_zh"]:
                item["strategies_zh"].append(names.get(str(strategy["id"]), str(strategy["id"])))
            # signals 只喂给模型（前端/台账都不读它），所以这里直接用中文策略名，
            # 免得模型在正文里回抄英文 id。
            signal = {"strategy": names.get(str(strategy["id"]), str(strategy["id"]))}
            for field in ("rank", "score", "model_score", "roe", "roa", "market_cap",
                          "target_weight", "trade_weight", "last_close", "industry"):
                if field in row and _value(row[field]) is not None:
                    signal[field] = _value(row[field])
            item["signals"].append(signal)
    # 最终通知名单可能和 selection_history 不完全一致，也要覆盖。
    notice = results / f"feishu_{date}.json"
    if notice.exists():
        payload = json.loads(notice.read_text(encoding="utf-8"))
        if str(payload.get("trade_date"))[:10] == date:
            for row in payload.get("rows", []):
                code = str(row.get("stock_code") or "").strip()
                if not code:
                    continue
                item = found.setdefault(code, {"stock_code": code, "name": "", "strategies": [],
                                               "strategies_zh": [], "signals": []})
                item["name"] = item["name"] or str(row.get("name") or "")
                if "cn_ensemble" not in item["strategies"]:
                    item["strategies"].append("cn_ensemble")
                    label = names.get("cn_ensemble", "cn_ensemble")
                    if label not in item["strategies_zh"]:
                        item["strategies_zh"].append(label)
                item["signals"].append({"strategy": names.get("cn_ensemble", "cn_ensemble"),
                                        "source": "notify",
                                        "rank": row.get("model_rank"), "weight": row.get("target_weight"),
                                        "reason": row.get("reason")})
    return [found[k] for k in sorted(found)]


def _ask(batch: list[dict], date: str, key: str) -> dict[str, dict]:
    system = ("你是A股收盘后研究分析员。只根据用户给出的决策日及之前已知数据做多空综合分析，"
              "不得编造新闻、财报或未来价格。逐股说明多头依据、空头依据和风险。"
              "提到入选策略时用 stocks 里 strategies_zh 的中文名，不要写英文 id。"
              "action 只能为买入、持有、卖出；资料不足选持有且降低 confidence。"
              "只返回 JSON 对象，结构为 {analyses:[{stock_code,action,confidence,summary,"
              "bull_case,bear_case,risk_flags,horizon}]}；confidence 为 0 到 1，risk_flags 是字符串数组。"
              "这些是研究意见，不生成订单。")
    response = requests.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": "deepseek-chat", "temperature": 0.1,
              "response_format": {"type": "json_object"},
              "messages": [{"role": "system", "content": system},
                           {"role": "user", "content": json.dumps({"decision_date": date, "stocks": batch}, ensure_ascii=False)}]},
        timeout=120,
    )
    response.raise_for_status()
    parsed = json.loads(response.json()["choices"][0]["message"]["content"])
    return {str(r.get("stock_code")): r for r in parsed.get("analyses", []) if isinstance(r, dict)}


def analyze(date: str, picks: list[dict], *, fallback_only: bool = False,
            api_key: str | None = None, batch_size: int = 8) -> list[dict]:
    key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    output = []
    for start in range(0, len(picks), batch_size):
        batch = picks[start:start + batch_size]
        answers: dict[str, dict] = {}
        error = ""
        if fallback_only:
            error = "fallback_only"
        elif not key:
            error = "missing_api_key"
        else:
            try:
                answers = _ask(batch, date, key)
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                error = f"{type(exc).__name__}: {str(exc)[:160]}"
        for pick in batch:
            raw = answers.get(pick["stock_code"], {})
            valid = raw.get("action") in ACTIONS
            flags = raw.get("risk_flags", []) if valid else []
            if not isinstance(flags, list):
                flags = [str(flags)]
            try:
                confidence = max(0.0, min(1.0, float(raw.get("confidence")))) if valid else None
            except (TypeError, ValueError):
                confidence = None
            output.append({
                "decision_date": date, "stock_code": pick["stock_code"], "name": pick["name"],
                "strategies": pick["strategies"], "action": raw["action"] if valid else "待分析",
                "confidence": confidence, "summary": str(raw.get("summary") or "") if valid else "",
                "bull_case": str(raw.get("bull_case") or "") if valid else "",
                "bear_case": str(raw.get("bear_case") or "") if valid else "",
                "risk_flags": [str(x) for x in flags],
                "horizon": str(raw.get("horizon") or "") if valid else "",
                "status": "completed" if valid else "pending", "error": "" if valid else (error or "missing_or_invalid_response"),
                "generated_at": now,
            })
    return output


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--fallback-only", action="store_true")
    ap.add_argument("--skip-existing", action="store_true",
                    help="保留已完成的逐股结论，只重新分析还处于待分析状态的股票")
    args = ap.parse_args()
    try:
        from data.store.scheduler_env import load_scheduler_env
        load_scheduler_env()
    except ImportError:
        pass

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{args.date}.json"
    existing: list[dict] = []
    if args.skip_existing and path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if str(payload.get("date")) == args.date:
                existing = [r for r in payload.get("rows", []) if isinstance(r, dict)]
        except (OSError, json.JSONDecodeError):
            existing = []
    done = {str(r.get("stock_code")) for r in existing if r.get("status") == "completed"}

    picks = [p for p in collect(args.date) if p["stock_code"] not in done]
    rows = existing + analyze(args.date, picks, fallback_only=args.fallback_only)
    rows.sort(key=lambda r: str(r.get("stock_code")))
    path.write_text(json.dumps({"date": args.date, "rows": rows}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    flat = [{**row, "strategies": ",".join(row["strategies"]), "risk_flags": "；".join(row["risk_flags"])} for row in rows]
    pd.DataFrame(flat).to_csv(OUT / f"{args.date}.csv", index=False)
    by_action: dict[str, int] = {}
    for row in rows:
        by_action[row["action"]] = by_action.get(row["action"], 0) + 1
    print(f"[ai-picks] date={args.date} picks={len(rows)} "
          f"completed={sum(r['status'] == 'completed' for r in rows)} "
          f"pending={sum(r['status'] == 'pending' for r in rows)} "
          f"actions={by_action} -> {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
