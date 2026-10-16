"""策略抽象层（Strategy Registry）—— **一条策略 = 一条声明**，其余全部派生。

背景（用户 2026-10-13）："能把这些策略都做成抽象统一的么？方便后续新增策略的时候，
可以在 web 端自动同步好数据，把策略行为的共性提取出来做一层抽象。"

现状的问题不是"没有配置"，而是**共性散落在各处**：一条策略的"行为"被写在 6 个地方：

    行为（共性）                        | 以前写在哪                                   | 新增策略时会发生什么
    ------------------------------------|---------------------------------------------|-------------------------------------------
    每日刷新（跑什么/超时/当日无票）      | config/strategies.json + 各脚本（P2.56 已修） | 声明了才有契约
    选股结果页的数据源（文件/列/口径）    | backend/services/selection_sources.py 硬编码 | **必须手改 Python**，否则 404 / 回退默认源
    曲线口径（跟踪/影子/回放/原始逐笔）   | scripts/build_strategy_pool_performance.py   | **必须手改 Python**，否则没有中证500 对比
    台账来源（日历）                     | build_selection_ledger.py + ledger_sources   | 声明了但没人解析 ⇒ 日历缺这一天
    同花顺导出 / AI 点评 / K线自选分组    | 各自硬编码列表或 selection_source            | 有的自动、有的不会出现

本模块把上面这些收成**一层**：`config/strategies.json` 的每条策略是完整声明，
其余消费端（选股结果 / 策略池 / 日历 / K线自选 / 导出 / 每日流程 / 体检）**一律从注册表派生**。

声明块（每条策略，节选）：

    {
      "id": "rot_arm", "name": "行业轮动手臂", "kind": "rule_strategy", "stage": "candidate",
      "entry": "uv run python scripts/select_rotation_arm.py",
      "artifacts": ["output/results_cn/rot_arm_selection.csv"],
      "history_dir": "output/results_cn/rot_arm_history",
      "selection_source": "cn_arm_rot",
      "selection": {"roles": [["research", "output/results_cn/rot_arm_selection.csv"]],
                    "columns": ["rank", "stock_code", "..."], "code_style": "ts_code",
                    "supports_shap": false, "note": "……口径说明，直接显示在页面上"},
      "daily_refresh": {"mode": "run", "max_minutes": 20},
      "ledger_sources": ["rot_arm"],
      "curve": {"tracking": true, "shadow": {"nav_csv": "...", "history_csv": "..."}},
      "metrics_files": ["..."], "metrics_map": {"...": "..."}
    }

用法：

    from backend.services import strategy_registry as reg
    reg.specs()               # 全部策略（已归一化）
    reg.spec("rot_arm")       # 单条
    reg.selection_sources()   # -> selection_sources.SOURCES（派生）
    reg.curve_plan(spec)      # -> 曲线怎么算
    reg.consumers(spec)       # -> 这条策略接到哪些页面/流程
    reg.coverage()            # -> 覆盖矩阵 + 缺线清单
    reg.validate()            # -> 声明层问题清单（空 = 通过）

CLI：`uv run python scripts/strategy_registry.py --check|--wiring|--new ...`
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config/strategies.json"

# ── 词表（声明层允许的取值；校验与页面共用） ───────────────────────────────────
KINDS = {
    "rule_strategy": "规则类策略（事件/结构/公式打分）",
    "factor_pipeline": "因子流水线（线性/非线性组合）",
    "model": "模型策略（生产 ensemble）",
    "model_legacy": "模型旧口径（影子对照）",
    "research_arm": "研究手臂（板块/行业轮动）",
    "factor_candidate": "因子候选（独立研究名单）",
    "live_account": "实盘镜像（真实持仓，不是策略）",
}
STAGES = ("candidate", "research", "production", "live", "retired")
STAGE_LABEL = {
    "candidate": "候选（待验证）", "research": "研究中（有名单但不进生产）",
    "production": "生产中（每日自动化在跑）", "live": "实盘镜像（人工账户，不下单）",
    "retired": "已下线",
}
REFRESH_MODES = ("run", "archive", "skip")
DATE_COLUMNS = ("trade_date", "decision_date", "asof_date")
CODE_STYLES = ("ts_code", "hk")

# 台账里每条 source 由哪个解析器负责（build_selection_ledger.py 的适配器）。
# 新增策略声明了 ledger_sources，必须在这里给出解析器，否则 validate() 点名
# "声明了却没人解析" —— 那正是日历会缺日期的原因。
LEDGER_ADAPTERS = {
    "notify": "build_selection_ledger._from_notify",
    "preselected": "build_selection_ledger._from_preselected",
    "arm": "build_selection_ledger._from_arm_history",
    "arm_concept": "build_selection_ledger._from_arm_concept_history",
    "rot_arm": "build_selection_ledger._from_named_history(rot_arm)",
    "box_arm": "build_selection_ledger._from_named_history(box_arm)",
    "first_ban_lowxi": "build_selection_ledger._from_named_history(first_ban_lowxi)",
    "wyckoff_arm": "build_selection_ledger.read_wyckoff_arm_history",
    "moneyflow_arm": "build_selection_ledger.read_moneyflow_arm_history",
    "mainforce_arm": "build_selection_ledger.read_mainforce_arm_history",
    "donchian_breakout": "build_selection_ledger._from_named_history(donchian_breakout)",
    "jq_multi_factor": "build_selection_ledger._from_jq_multi_factor",
    "jq_roe_roa": "build_selection_ledger._from_jq_multi_factor",
    "survey_gate": "build_selection_ledger._from_survey",
    "survey_raw": "build_selection_ledger._from_survey",
}

CONSUMERS = {
    "strategy_pool": "策略池（登记 + 新鲜度 + 曲线 + 优劣）",
    "selection_page": "选股结果页（/api/selection?source=）",
    "selection_calendar": "选股日历（台账 selection_ledger.parquet）",
    "kline_watchlist": "K 线自选（侧栏策略分组）",
    "ths_export": "同花顺导出（export_ths_watchlist.py）",
    "ai_picks": "逐股 AI 多空点评（analyze_strategy_picks_ai.py）",
    "daily_refresh": "每日名单刷新契约（refresh_strategy_lists.py）",
    "web_freshness": "每日新鲜度体检（audit_web_freshness.py）",
}


class SpecError(ValueError):
    """声明层错误（缺字段 / 取值非法 / 接线缺失）。"""


# ── 读取与归一化 ───────────────────────────────────────────────────────────────
def read_config() -> dict:
    if not CONFIG.exists():
        raise SpecError(f"缺少策略登记表：{CONFIG}")
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def spec_path(spec: dict, rel: str) -> Path:
    return (ROOT / str(rel)).resolve()


def selection_roles(spec: dict) -> list[tuple[str, Path]]:
    """选股结果页要读的文件（role, path）。role 决定展示阶段（selected/preselected/arm/research）。"""
    block = spec.get("selection") or {}
    out: list[tuple[str, Path]] = []
    for item in block.get("roles") or []:
        if isinstance(item, dict):
            role, rel = item.get("role"), item.get("file")
        else:
            role, rel = item[0], item[1]
        out.append((str(role), spec_path(spec, str(rel))))
    return out


def curve_plan(spec: dict) -> dict:
    """这条策略的曲线该怎么画（消费端不许自己猜）。

    * tracking：名单跟踪（持有到下一个决策日、次日开盘建仓）——需要 history_dir 有连续归档，
      且持仓语义必须是"持有到下一个决策日"（no_tracking=True 的不算，如首板低吸当日卖）；
    * replay：独立研究回放（原始 nav.csv，口径写在 output/research/*/REPORT.md）；
    * shadow：影子模拟盘净值（统一执行假设、历史可回填 ⇒ 决策日稀疏的策略也有多期对照）；
    * original：策略自己产出的逐笔模拟曲线（curve_source.nav_csv，实盘镜像/事件型用）。
    """
    curve = spec.get("curve") if isinstance(spec.get("curve"), dict) else {}
    original = spec.get("curve_source") or {}
    has_tracking = bool(spec.get("history_dir")) and not bool(spec.get("no_tracking"))
    plan = {
        "tracking": has_tracking,
        "replay": curve.get("replay") or None,
        "shadow": curve.get("shadow") or None,
        "original": ({"nav_csv": original.get("nav_csv"), "kind": original.get("kind"),
                      "reason": original.get("reason")} if original.get("nav_csv") else None),
    }
    if spec.get("no_tracking"):
        plan["no_tracking_reason"] = ("该策略的持仓语义不是「持有到下一个决策日」，"
                                      "名单跟踪口径会把方向算反")
    return plan


def normalize(spec: dict) -> dict:
    """把声明补全成消费端直接可用的形状（不写回文件）。"""
    entry = dict(spec)
    entry["stage_label"] = STAGE_LABEL.get(entry.get("stage", "candidate"), entry.get("stage", ""))
    entry["kind_label"] = KINDS.get(entry.get("kind", ""), entry.get("kind", ""))
    entry.setdefault("date_column", "trade_date")
    refresh = entry.get("daily_refresh") or {}
    entry["refresh"] = {
        **refresh,
        "mode": refresh.get("mode") or ("skip" if not entry.get("selection_source") else "run"),
        "max_minutes": float(refresh.get("max_minutes") or 20),
        "empty_is_ok": bool(refresh.get("empty_is_ok")),
    }
    entry["roles"] = [{"role": role, "file": str(path)} for role, path in selection_roles(entry)]
    entry["curve"] = curve_plan(entry)
    return entry


def specs() -> list[dict]:
    return [normalize(item) for item in read_config().get("strategies", [])]


def spec(ident: str) -> dict:
    for item in specs():
        if item["id"] == ident:
            return item
    raise SpecError(f"注册表里没有这条策略：{ident}")


def ids() -> list[str]:
    return [item["id"] for item in specs()]


# ── 派生：选股结果页的数据源（selection_sources.SOURCES） ───────────────────────
def selection_source_entry(spec: dict) -> dict | None:
    key = spec.get("selection_source") or ""
    if not key:
        return None
    block = spec.get("selection") or {}
    return {
        "label": block.get("label") or spec.get("name") or key,
        "note": block.get("note") or spec.get("summary") or "",
        "files": selection_roles(spec),
        "code_style": block.get("code_style") or "ts_code",
        "columns": list(block.get("columns") or []),
        "supports_shap": bool(block.get("supports_shap")),
        "strategy_id": spec["id"],
    }


def selection_sources() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for item in specs():
        entry = selection_source_entry(item)
        if entry:
            out[item["selection_source"]] = entry
    return out


# ── 派生：接线表（这条策略接到了哪些页面/流程） ─────────────────────────────────
def consumers(spec: dict) -> dict:
    roles = selection_roles(spec)
    history_dir = spec.get("history_dir") or ""
    ledger = list(spec.get("ledger_sources") or [])
    plan = curve_plan(spec)
    source = spec.get("selection_source") or ""
    rel = lambda p: str(p.relative_to(ROOT)) if str(p).startswith(str(ROOT)) else str(p)
    return {
        "strategy_pool": {"wired": True, "how": "config/strategies.json（本声明即登记）"},
        "selection_page": {"wired": bool(source and roles),
                           "how": f"/api/selection?source={source}" if source else None,
                           "files": [rel(path) for _, path in roles]},
        "daily_refresh": {"wired": (spec.get("refresh") or {}).get("mode") != "skip",
                          "how": f"mode={(spec.get('refresh') or {}).get('mode')}",
                          "command": spec.get("entry") or ""},
        "selection_calendar": {"wired": bool(history_dir and ledger),
                               "how": f"ledger_sources={ledger}" if ledger else None,
                               "history_dir": history_dir or None,
                               "adapters": {src: LEDGER_ADAPTERS.get(src) for src in ledger}},
        "kline_watchlist": {"wired": bool(source), "how": "池内 selection_source → 侧栏分组"},
        "ths_export": {"wired": bool(source), "how": "--sources auto 会带上这条"},
        "ai_picks": {"wired": bool(history_dir and source), "how": "history_dir + selection_source 齐备即纳入"},
        "web_freshness": {"wired": True, "how": "「策略名单（每日刷新契约）」行点名"},
        "curve": {"wired": any(plan[k] for k in ("tracking", "replay", "shadow", "original")),
                  "how": {k: v for k, v in plan.items() if v and k != "no_tracking_reason"}},
    }


def _by_design_reason(item: dict, key: str) -> str | None:
    """没接上，但**是设计如此**（不是漏接线）——分开记账，否则告警会被"影子对照没名单"刷满。"""
    source = item.get("selection_source") or ""
    mode = (item.get("refresh") or {}).get("mode")
    if key in ("selection_page", "kline_watchlist", "ths_export", "ai_picks") and not source:
        return "该策略没有独立名单（影子对照/实盘镜像，按设计）"
    if key == "daily_refresh" and mode == "skip":
        return "声明 daily_refresh.mode=skip（按设计）"
    if key == "selection_calendar" and not (item.get("ledger_sources") or []):
        return "没有台账来源 ledger_sources（按设计）"
    if key == "ai_picks" and not item.get("history_dir"):
        return "没有 history_dir（按设计）"
    return None


def coverage() -> dict:
    """覆盖矩阵：每个消费端接上了几条、谁没接上。

    `gaps` 只装**意外的**没接上（漏接线）；设计如此的落在 `by_design`（带原因），
    两者之和 = 未接数 —— 这样"新增策略自动同步"的自检不会被噪声淹没。
    """
    all_specs = specs()
    matrix: dict[str, list[str]] = {key: [] for key in CONSUMERS}
    gaps: list[dict] = []
    by_design: list[dict] = []
    for item in all_specs:
        wired = consumers(item)
        for key, info in wired.items():
            if key == "curve":
                continue
            if info.get("wired"):
                matrix.setdefault(key, []).append(item["id"])
                continue
            reason = _by_design_reason(item, key)
            row = {"strategy": item["id"], "consumer": key, "reason": reason, "detail": info}
            (by_design if reason else gaps).append(row)
    return {"generated_at": datetime.now().isoformat(timespec="seconds"),
            "n_strategies": len(all_specs), "consumers": CONSUMERS, "matrix": matrix,
            "gaps": gaps, "by_design": by_design}


# ── 校验 ───────────────────────────────────────────────────────────────────────
def validate() -> list[str]:
    """声明层问题清单（空 = 通过）。校验的是"接线完整性"，不是"策略好不好"。"""
    problems: list[str] = []
    strategies = read_config().get("strategies") or []
    seen: set[str] = set()
    for item in strategies:
        ident = item.get("id") or "<无 id>"
        if ident in seen:
            problems.append(f"{ident}：id 重复")
        seen.add(ident)
        for field in ("id", "name", "stage", "group"):
            if not item.get(field):
                problems.append(f"{ident}：缺必填字段 {field}")
        if item.get("stage") not in STAGES:
            problems.append(f"{ident}：stage 非法（{item.get('stage')}；可选 {list(STAGES)}）")
        if item.get("kind") and item["kind"] not in KINDS:
            problems.append(f"{ident}：kind 非法（{item['kind']}；可选 {list(KINDS)}）")

        refresh = item.get("daily_refresh")
        if not isinstance(refresh, dict):
            problems.append(f"{ident}：缺 daily_refresh（缺失会静默，见 P2.56）")
        else:
            if refresh.get("mode") not in REFRESH_MODES:
                problems.append(f"{ident}：daily_refresh.mode 非法（{refresh.get('mode')}）")
            if refresh.get("mode") in ("run", "archive"):
                if not item.get("entry"):
                    problems.append(f"{ident}：声明要刷新但没有 entry 命令")
                if not item.get("selection_source"):
                    problems.append(f"{ident}：声明要刷新但没有 selection_source（无法判新鲜度）")
            if refresh.get("mode") == "skip" and not refresh.get("reason"):
                problems.append(f"{ident}：mode=skip 必须写 reason")

        if item.get("selection_source"):
            block = item.get("selection")
            if not isinstance(block, dict):
                problems.append(f"{ident}：有 selection_source 但没有 selection 块（选股结果页无从显示）")
            else:
                if not (block.get("roles") or []):
                    problems.append(f"{ident}：selection.roles 为空（没有可读名单文件）")
                if block.get("code_style", "ts_code") not in CODE_STYLES:
                    problems.append(f"{ident}：selection.code_style 非法（{block.get('code_style')}）")
                for role, rel in (block.get("roles") or []):
                    rel_str = rel.get("file") if isinstance(rel, dict) else rel[1]
                    if str(rel_str).startswith("/"):
                        problems.append(f"{ident}：selection.roles 必须写仓库相对路径（{rel_str}）")

        for src in item.get("ledger_sources") or []:
            if src not in LEDGER_ADAPTERS:
                problems.append(f"{ident}：ledger_sources 里的 {src!r} 没有解析器"
                                f"（声明了但没人解析 ⇒ 日历会缺这条来源）")

        if item.get("history_dir") and not str(item["history_dir"]).startswith("output/"):
            problems.append(f"{ident}：history_dir 必须写在 output/ 下（{item['history_dir']}）")
        if not item.get("history_dir") and item.get("stage") in ("production", "candidate", "live"):
            problems.append(f"{ident}：{item.get('stage')} 阶段却没有 history_dir（日历/自选会没数据）")

        plan = curve_plan(item)
        if item.get("no_tracking") and not (plan["replay"] or plan["shadow"] or plan["original"]):
            problems.append(f"{ident}：no_tracking 但没给 curve.replay/shadow 或 curve_source"
                            f"（策略池里会没有中证500 对比曲线）")
    return problems


def wiring_matrix() -> dict:
    rows = []
    for item in specs():
        rows.append({
            "id": item["id"], "name": item["name"], "stage": item["stage"],
            "stage_label": item["stage_label"], "kind": item.get("kind"),
            "group": item.get("group"), "selection_source": item.get("selection_source") or None,
            "refresh": (item.get("refresh") or {}).get("mode"),
            "ledger_sources": item.get("ledger_sources") or [],
            "curve": {k: bool(v) for k, v in curve_plan(item).items() if k != "no_tracking_reason"},
            "consumers": consumers(item),
        })
    return {"generated_at": datetime.now().isoformat(timespec="seconds"),
            "n_strategies": len(rows), "consumers": CONSUMERS, "rows": rows}


def to_markdown(payload: dict) -> str:
    icons = {"selection_page": "选股", "selection_calendar": "日历", "kline_watchlist": "自选",
             "ths_export": "导出", "ai_picks": "AI", "daily_refresh": "刷新", "web_freshness": "体检"}
    lines = [f"# 策略接线矩阵 · {payload['generated_at']}", "",
             f"共 **{payload['n_strategies']}** 条策略；消费端：" +
             "、".join(payload["consumers"].values()), "",
             "| 策略 | 阶段 | 选股来源 | 刷新 | 台账来源 | 曲线 | 消费端 |",
             "|---|---|---|---|---|---|---|"]
    for row in payload["rows"]:
        wires = [label for key, label in icons.items() if row["consumers"].get(key, {}).get("wired")]
        curves = [key for key, ok in row["curve"].items() if ok]
        lines.append(f"| `{row['id']}` | {row['stage']} | {row['selection_source'] or '—'} | "
                     f"{row['refresh']} | {'、'.join(row['ledger_sources']) or '—'} | "
                     f"{'、'.join(curves) or '—'} | {'、'.join(wires)} |")
    return "\n".join(lines) + "\n"
