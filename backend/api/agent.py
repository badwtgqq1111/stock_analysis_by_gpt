"""投研执行台 API（页面：`frontend/src/pages/AgentConsole.vue`，导航「研究 → 投研执行台」）。

为什么要有这一页（用户 2026-10-11）："我想最终做成一个专业的量化投研 agent" —— 专业体现在三件事上，
都必须是可查的，而不是一个聊天框：
  1. **两族 / 四层现在什么状态**（哪条策略落后了、哪一档门槛没过）；
  2. **每次运行干了什么**（`scripts/agent_run.py` 写的 `output/agent_runs/<run_id>/run.json` 轨迹）；
  3. **这个人设是谁**——提示词可读可写，默认是"量化领域专家"，用户在页面上改，落 `config/agent_profile.json`。

数据全部来自产物（不重算）：
  * `config/strategies.json` + `output/strategy_pool/strategy_pool.json`     两族清单与新鲜度
  * `output/research/factor_ic_protocol/factor_ic_admitted.json`            因子层准入（BH-FDR/t≥3）
  * `output/results_cn/promotion_check/<ts>/verdicts.csv`                   优化层闸门判决
  * `output/paper_trading/shadow/summary.json`                              优化/决策层影子组合
  * `output/data_quality/web_freshness.json`                                页面新鲜度（决策层落地情况）
  * `output/agent_runs/<run_id>/run.json`                                   运行轨迹

写操作只有一个：提示词/人设（`PUT /api/agent/profile`）。写之前先把旧版本备份到
`output/agent_runs/profile_history/<ts>.json`，`POST /api/agent/profile/reset` 恢复默认。
"""
from __future__ import annotations

import csv
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = ROOT / "output/agent_runs"
HISTORY_DIR = RUNS_DIR / "profile_history"
PROFILE_PATH = ROOT / "config/agent_profile.json"
CONFIG_PATH = ROOT / "config/strategies.json"
POOL_PATH = ROOT / "output/strategy_pool/strategy_pool.json"
CONFIG_PATH = ROOT / "config/strategies.json"
PRE_ORDER_DIR = ROOT / "output/results_cn/pre_order_check"
PROMOTION_DIR = ROOT / "output/results_cn/promotion_check"
RESEARCH_SOURCES = ROOT / "config/research_sources.json"
INSPECTOR_DIR = ROOT / "output/factor_library/inspector"
FACTOR_ADMITTED = ROOT / "output/research/factor_ic_protocol/factor_ic_admitted.json"
PROMOTION_DIR = ROOT / "output/results_cn/promotion_check"
SHADOW_SUMMARY = ROOT / "output/paper_trading/shadow/summary.json"
FRESHNESS = ROOT / "output/data_quality/web_freshness.json"
# 实盘镜像（2026-10-12）：真实券商持仓的盯市摘要 —— 决策层要带它（那是用户自己的钱）。
LIVE_MIRROR_SUMMARY = ROOT / "output/paper_trading/live_mirror/summary.json"
FACTOR_LIBRARY = ROOT / "output/factor_library/factors.json"

router = APIRouter(prefix="/api/agent", tags=["agent"])

# ── 两族分类（与 docs/todo/P2_42_quant_research_agent_plan.md §0.4 一一对应）─────────
# 这份表是"页面左侧树"的数据源；`test/test_agent_api.py` 会断言它覆盖 `config/strategies.json`
# 里的每一条策略 —— 以后往策略池里加策略，忘了加族就会被测试挡住（避免页面上凭空少一条）。
FAMILIES: list[dict] = [
    {
        "key": "A",
        "label": "规则类策略",
        "hint": "证据单位是「一笔」：单笔净期望 + 事件样本数 + 按决策日聚类 NW t + 参数敏感性 + 容量",
        "branches": [
            {"key": "event", "label": "事件规则", "ids": ["first_ban_lowxi"]},
            # 威科夫吸筹突破（2026-10-14）：结构型规则策略 —— 区间/spring/SOS 都是可复现的规则，
            # 证据单位同样是「一笔」（269 笔、逐笔 +1.31%、按决策日聚类可检验），所以放族 A。
            {"key": "structure", "label": "结构形态（威科夫）", "ids": ["wyckoff_arm"]},
            # 突破（通道/箱体）：box_arm（横盘箱体突破回踩）与 donchian_breakout（通道突破腿，2026-10-15
            # 从生产 config 的 model leg `[strategies.donchian_breakout]`（status=off）搬进策略池）。
            # ⚠️ donchian_breakout 是 **zero-edge 观察候选**（A/B 8 个口径全为负、最好的打平），
            # 登记只为和中证500 逐日对照；box_arm 参数极敏感，同为观察候选。
            {"key": "breakout", "label": "突破（通道/箱体）",
             "ids": ["box_arm", "box_squeeze_arm", "donchian_breakout", "rbreaker_arm"]},
            # 主力追踪（2026-10-14）：资金流口径的规则策略（净流入 z 分数 + 吸筹形态），
            # 证据单位同样是「一笔」，所以同属族 A。
            {"key": "moneyflow", "label": "资金流（主力追踪）",
             "ids": ["moneyflow_arm", "mainforce_arm"]},
            {"key": "formula", "label": "公式打分（不训练）", "ids": ["jq_multi_factor_size_roe", "jq_roe_roa_diagnostic"]},
            {"key": "arm", "label": "手臂规则（行业/概念/宽度）",
             "ids": ["arm_concept", "rot_arm", "arm_select", "arm_select_volrisk"]},
        ],
    },
    {
        "key": "B",
        "label": "因子流水线",
        "hint": "证据单位是「一个截面/一个组合」：秩 IC / ICIR / NW t / BH-FDR + 组合级逐期配对",
        "branches": [
            {
                "key": "linear", "label": "B1 线性组合",
                "ids": ["moneyflow_factor_equivalent", "moneyflow_factor_institutional",
                        "moneyflow_ml_nb", "brain_quality_lowpe_momentum",
                        "brain_panic_reversal_quality"],
                "note": "等权 / IC 加权线性合成（base_v1、llm_new、combo_50_50），不训练模型",
                "evidence": ["output/research/llm_factor_mining_20261007_155107/sleeve_oos_v4d/arms_summary_daily.csv"],
            },
            {
                # cn_ensemble_pk_legacy（2026-10-12）：旧 PK 口径的对照臂，和 cn_ensemble 同属
                # 这条流水线（只是被生产替换掉了），所以放同一支里，不要另开一族。
                "key": "nonlinear", "label": "B2 非线性组合",
                "ids": ["cn_ensemble", "cn_ensemble_pk_legacy", "cn_ensemble_volcap"],
                "note": "LightGBM / Transformer / CNN；生产 ensemble_weights = {lightgbm 1.00, transformer 0.00}；"
                        "cn_ensemble_pk_legacy = 旧 PK 口径对照（stage=research，已不进生产）",
                "evidence": ["output/models/cn/lightgbm", "output/models/cn/transformer", "output/models/cn/cnn"],
            },
        ],
    },
]

# 四层：把"这一层现在什么状态"落到一个可核对的产物上（聚宽那张图只有一句话，我们给的是数字）
LAYERS: list[dict] = [
    {"key": "factor", "label": "因子层", "families": ["A", "B"]},
    {"key": "model", "label": "模型层", "families": ["B"]},
    {"key": "optimize", "label": "优化层", "families": ["A", "B"]},
    {"key": "decide", "label": "决策层", "families": ["A", "B"]},
]

DEFAULT_PROFILE: dict = {
    "role": "量化领域专家",
    "language": "zh",
    "detail_level": "专家（默认给结论+证据+命令，省略科普）",
    "system_prompt": (
        "你是这套 A 股量化投研系统（stock_analysis_by_gpt）的投研智能体，服务对象是【量化领域专家】。\n"
        "角色与这段提示词都可以在「投研执行台 → 提示词」里随时改写，改完即刻生效"
        "（落盘 config/agent_profile.json，历史版本留在 output/agent_runs/profile_history/）。\n"
        "\n【说话方式】\n"
        "- 结论先行 → 证据 → 缺口 → 下一步可执行命令；不写免责声明、不写「仅供参考」。\n"
        "- 数字必须带口径与来源：月频 fwd20 / 日频逐日盯市 / 逐笔期望，三者不可混着比大小。\n"
        "\n【证据标准（按族分开，见 docs/todo/P2_42_quant_research_agent_plan.md §0.4）】\n"
        "- 族 A 规则类策略（事件规则 first_ban_lowxi/box_arm；公式打分 jq_multi_factor_size_roe/jq_roe_roa_diagnostic；"
        "手臂规则 arm_concept/rot_arm/arm_select）：证据单位是「一笔」——单笔毛期望、单笔净期望"
        "（佣金2.5bp+滑点3bp+印花10bp）、事件样本数（≥200 笔或 ≥30 个决策日）、按决策日聚类的 NW t、参数敏感性、容量。\n"
        "- 族 B 因子流水线（B1 线性组合 base_v1/llm_new/combo_50_50；B2 非线性组合 LightGBM/Transformer/CNN）："
        "证据单位是「一个截面/一个组合」——秩 IC / ICIR / NW t / BH-FDR(q=0.05) / Harvey-Liu-Zhu t≥3 / "
        "与存量字段最大相关性 ≤0.7；组合层必须过上线闸门（同窗口逐期配对 + 集中度体检）。\n"
        "- 唯一共用判据：跟「它自己的对照」做同窗口逐期配对。禁止拿 IC 给规则策略背书，"
        "也禁止拿「逐笔很赚」给因子流水线背书。\n"
        "\n【三条禁令】\n"
        "1. 多重检验优先：跑得越多越容易「看起来显著」，报告必须写清累计试验次数（DSR）与是否过 BH-FDR。\n"
        "2. 短样本不下结论：观测 <30 标「样本不足」，<252 不参与年化排名。\n"
        "3. 不给下单指令：决策层停在模拟盘 + 自选导出；实盘需要人工确认 + 下单前 checklist 全绿。\n"
        "\n【保命规则默认不采信】个股止损（A15/A16）、择时风控（A17）、ST 过滤（A18）在本仓库都是负贡献；"
        "任何「加个止损 / 滤掉 ST 会更稳」的说法，必须先过闸门 A/B（scripts/promotion_check.py）才能改写生产规则。\n"
        "\n【输出结构】结论（一句话 + 数字）→ 证据（产物路径 + 样本区间 + 口径）→ 缺口（缺什么数据/什么检验）"
        "→ 下一步（可直接复制的命令）。"
    ),
    "notes": "本文件是「投研执行台」提示词面板的唯一数据源（人写 + 可在页面改）。",
}
PROFILE_FIELDS = ("role", "language", "detail_level", "system_prompt", "notes")


def _rel_path(path: Path | None) -> str | None:
    """仓库内 → 相对路径；仓库外（测试里被 monkeypatch 到 tmp_path）→ 原样返回。

    踩过坑：`tests` 把 `PROFILE_PATH` 指向 tmp 之后，`path.relative_to(ROOT)` 直接抛 ValueError
    ⇒ 接口 500。路径展示是给页面看的，不该因为"文件不在仓库里"就把接口打挂。
    """
    if path is None:
        return None
    text = str(path)
    prefix = str(ROOT) + "/"
    return text[len(prefix):] if text.startswith(prefix) else text


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def load_profile() -> dict:
    data = _read_json(PROFILE_PATH, {})
    profile = {key: data.get(key, DEFAULT_PROFILE[key]) for key in PROFILE_FIELDS}
    profile["updated_at"] = data.get("updated_at")
    profile["is_default"] = all(profile[key] == DEFAULT_PROFILE[key] for key in ("role", "system_prompt"))
    return profile


def _history_count() -> int:
    return len(list(HISTORY_DIR.glob("*.json"))) if HISTORY_DIR.exists() else 0


def _pool_items() -> dict[str, dict]:
    """策略池快照 → {id: 运行时状态}（没有快照就退化成空表，页面显示"未同步"）。"""
    payload = _read_json(POOL_PATH, {})
    items = {}
    for item in payload.get("strategies", []):
        items[item["id"]] = item
    for item in payload.get("candidates", []):
        if item.get("id"):
            items.setdefault(item["id"], item)
    return items


def _registered() -> dict[str, dict]:
    payload = _read_json(CONFIG_PATH, {})
    return {item["id"]: item for item in payload.get("strategies", [])}


def build_families() -> list[dict]:
    registered, runtime = _registered(), _pool_items()
    families = []
    for family in FAMILIES:
        branches = []
        for branch in family["branches"]:
            items = []
            for ident in branch["ids"]:
                declaration = registered.get(ident, {})
                live = runtime.get(ident, {})
                items.append({
                    "id": ident,
                    "name": declaration.get("name", live.get("name", ident)),
                    "stage": declaration.get("stage", live.get("stage", "unknown")),
                    "stage_label": live.get("stage_label") or declaration.get("stage", ""),
                    "group": declaration.get("group", ""),
                    "entry": declaration.get("entry", ""),
                    "selection_source": declaration.get("selection_source", ""),
                    "latest_date": live.get("latest_date"),
                    "n_picks": live.get("n_picks"),
                    "fresh": live.get("fresh"),
                    "health": live.get("health"),
                    "summary": declaration.get("summary", ""),
                })
            branches.append({
                "key": branch["key"], "label": branch["label"], "note": branch.get("note", ""),
                "items": items,
                "evidence": [path for path in branch.get("evidence", []) if (ROOT / path).exists()],
            })
        families.append({"key": family["key"], "label": family["label"], "hint": family["hint"],
                         "branches": branches})
    return families


def _factor_layer() -> dict:
    payload = _read_json(FACTOR_ADMITTED, {})
    if not payload:
        return {"status": "missing", "detail": "没有因子准入清单（跑 scripts/factor_ic_protocol.py）"}
    criteria = payload.get("criteria", {})
    return {
        "status": "ok",
        "detail": (f"准入 {payload.get('admitted_count', 0)} 个因子"
                   f"（窗口 {payload.get('window_months', ['?', '?'])[0]}~{payload.get('window_months', ['?', '?'])[-1]}；"
                   f"|IC|≥{criteria.get('min_abs_ic')}、BH-FDR q={criteria.get('fdr_q')}、t≥{criteria.get('hlz_t')}）"),
        "metric": {"admitted": payload.get("admitted_count"), "generated_at": payload.get("generated_at")},
    }


PRODUCTION_CARD = ROOT / "output/models/cn/lightgbm/alpha_zoo_hk/card.json"


def _model_cards() -> list[dict]:
    base = ROOT / "output/models/cn"
    return [_read_json(path, {}) for path in sorted(base.glob("*/*/card.json")) if path.exists()] if base.exists() else []


def _train_age_days(card: dict) -> "int | None":
    stamp = card.get("trained_at")
    if not stamp:
        return None
    try:
        trained = datetime.strptime(str(stamp)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return (datetime.now() - trained).days


def _model_layer() -> dict:
    """模型层 tile：读**模型卡**（§4.3）而不是只数目录 —— 直接回答"现在用的是哪一版、什么时候训的、
    每只票有多少独立样本、标的是什么"。超过 30 天没重训就转 warn（P2.47 的重训节奏问题在页面上可⻅）。"""
    cards = _model_cards()
    card = _read_json(PRODUCTION_CARD, {}) or (cards[0] if cards else {})
    if not card:
        base = ROOT / "output/models/cn"
        dirs = sorted(p.name for p in base.iterdir() if p.is_dir()) if base.exists() else []
        return {"status": "warn" if dirs else "missing",
                "detail": ("有模型目录但**没有模型卡**（跑 scripts/build_model_cards.py）" if dirs else "没有 output/models/cn"),
                "metric": {"models": dirs, "cards": 0}}
    age = _train_age_days(card)
    perf = card.get("performance") or {}
    sample = card.get("sample_size") or {}
    window = card.get("window") or {}
    features = card.get("features") or {}
    detail = (f"生产模型 {card.get('model_type')}｜训练于 {card.get('trained_at')}"
              + (f"（{age} 天前）" if age is not None else "")
              + f"｜训窗 {window.get('train_start')}~{window.get('train_end')}"
              + f"｜特征 {features.get('count')}（准入后）"
              + f"｜每票独立窗口 {sample.get('label_windows_per_stock')}"
              + (f"｜valid IC {perf.get('validation_ic'):.4f}" if perf.get("validation_ic") is not None else ""))
    if age is not None and age > 30:
        detail += f"｜⚠️ 已 {age} 天未重训"
    return {"status": "warn" if (age is not None and age > 30) else "ok",
            "detail": detail,
            "metric": {"cards": len(cards), "train_age_days": age, "features": features.get("count"),
                       "label": (card.get("label") or {}).get("column"),
                       "label_windows_per_stock": sample.get("label_windows_per_stock"),
                       "validation_ic": perf.get("validation_ic"),
                       "retire_when": card.get("retire_when")}}


@router.get("/models")
def agent_models() -> dict:
    """模型卡清单（P2.42 §4.3）：每张卡带训窗/特征/标签/有效样本/valid IC/何时退役。"""
    cards = _model_cards()
    return {"count": len(cards), "cards": cards,
            "summary": _rel_path(ROOT / "output/models/cn/CARDS.md")}


def _optimize_layer() -> dict:
    shadow = _read_json(SHADOW_SUMMARY, {})
    runs = sorted(PROMOTION_DIR.glob("*/verdicts.csv")) if PROMOTION_DIR.exists() else []
    verdicts: dict[str, int] = {}
    if runs:
        try:
            with runs[-1].open(encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    key = (row.get("verdict") or "?").strip().upper()
                    verdicts[key] = verdicts.get(key, 0) + 1
        except OSError:
            verdicts = {}
    raw_variants = shadow.get("variants") or []
    # `variants` 实测是 list（每条变体一个 dict），但历史上有过 dict 口径 —— 两种都认
    variants = len(raw_variants) if isinstance(raw_variants, (list, dict)) else 0
    detail = (f"影子组合 {variants} 条变体（参考口径 {shadow.get('reference_variant', '—')}）；"
              f"最近一次闸门：{('、'.join(f'{k} {v}' for k, v in verdicts.items()) or '没有判决')}")
    return {"status": "ok" if (variants or verdicts) else "missing", "detail": detail,
            "metric": {"shadow_variants": variants, "verdicts": verdicts,
                       "promotion_at": runs[-1].parent.name if runs else None}}


def _decision_layer() -> dict:
    payload = _read_json(FRESHNESS, {})
    rows = payload.get("rows", [])
    stale = payload.get("stale", [])
    if not rows:
        return {"status": "missing", "detail": "没有页面新鲜度体检（跑 scripts/audit_web_freshness.py）"}
    live = _live_account()
    return {
        "status": "ok" if not stale else "warn",
        "detail": (f"页面 {len(rows) - len(stale)}/{len(rows)} 已是最新交易日"
                   f"（基准 {payload.get('latest_trade_date')}）" + (f"；未更新：{'; '.join(stale)}" if stale else "")
                   + (f"；实盘镜像 {live['asof']}：{live['n_positions']} 只 / 建议 {live['counts']}" if live else "")),
        "metric": {"pages": len(rows), "stale": stale, "latest_trade_date": payload.get("latest_trade_date")},
        "live_account": live or {"available": False},
    }


def _live_account() -> dict | None:
    """实盘镜像的一句话状态（决策层用）：真实持仓 + 逐票建议分布 + 相对中证500 的同窗超额。

    只读 `output/paper_trading/live_mirror/summary.json`（由 scripts/build_live_mirror.py 写）。
    它不是策略，所以不进族树；但「决策层现在什么状态」必须包含它 —— 那是用户自己的钱。
    """
    payload = _read_json(LIVE_MIRROR_SUMMARY, None)
    if not payload:
        return None
    counts = payload.get("advice_counts") or {}
    rel = payload.get("relative_20d") or {}
    return {
        "available": True,
        "asof": payload.get("asof"),
        "captured_at": payload.get("captured_at"),
        "n_positions": payload.get("n_positions"),
        "total_assets": payload.get("total_assets"),
        "cumulative_return": payload.get("cumulative_return"),
        "counts": f"买入 {counts.get('买入', 0)} / 持有 {counts.get('持有', 0)} / 卖出 {counts.get('卖出', 0)}",
        "excess_20d": rel.get("excess_20d"),
        "path": _rel_path(LIVE_MIRROR_SUMMARY),
    }


LAYER_BUILDERS = {"factor": _factor_layer, "model": _model_layer,
                  "optimize": _optimize_layer, "decide": _decision_layer}


def _run_summaries(limit: int | None = None) -> list[dict]:
    runs = []
    if RUNS_DIR.exists():
        for path in sorted(RUNS_DIR.glob("*/run.json")):
            payload = _read_json(path, None)
            if not payload:
                continue
            runs.append({
                "run_id": payload.get("run_id"),
                "kind": payload.get("kind"),
                "title": payload.get("title"),
                "status": payload.get("status"),
                "started_at": payload.get("started_at"),
                "duration_seconds": payload.get("duration_seconds"),
                "totals": payload.get("totals", {}),
                "gates": payload.get("gates", []),
                # `session_id` 是"执行台 ↔ 因子挖掘"的跳转锚点：挖掘轨迹带上它，页面就能一键跳回会话
                "session_id": payload.get("session_id") or (payload.get("source") or {}).get("session_id"),
                "path": _rel_path(path),
            })
    runs.sort(key=lambda item: str(item.get("started_at") or ""), reverse=True)
    return runs[:limit] if limit else runs


_FACTOR_CACHE: dict = {"mtime": None, "payload": None}


def _factor_library() -> dict:
    """因子库注册表（`scripts/sync_factor_library.py` 写，1.4k 条 / ~1.3MB）。

    按 mtime 缓存：这个文件只有每日同步时才变，而 `/factors` 是要被页面反复筛的接口 ——
    每次请求都重新解析 1.3MB JSON 会白白吃掉几十毫秒。
    """
    if not FACTOR_LIBRARY.exists():
        return {}
    mtime = FACTOR_LIBRARY.stat().st_mtime
    if _FACTOR_CACHE["mtime"] != mtime:
        _FACTOR_CACHE.update(mtime=mtime, payload=_read_json(FACTOR_LIBRARY, {}))
    return _FACTOR_CACHE["payload"] or {}


def _factor_item(item: dict) -> dict:
    """给页面用的轻投影：完整 JSON 里 1,366 条 × 20 字段，页面只需要这些。"""
    metrics = item.get("metrics") or {}
    cluster = item.get("cluster") or {}
    category = item.get("category") or {}
    description = item.get("description") or {}
    performance = item.get("performance") or {}
    return {
        "id": item.get("id"), "kind": item.get("kind"), "source": item.get("source"),
        "family": item.get("family"), "status": item.get("status"), "grade": item.get("grade"),
        "category": category.get("key"), "category_label": category.get("label"),
        "category_core": category.get("core"),
        # 功能描述 + 性能评价：因子库现在每一条都带（来源如实标：curated / llm_reason / name_decoder）
        "description": description.get("text"), "description_source": description.get("source"),
        "performance_summary": performance.get("summary"),
        "expression": item.get("expression"), "note": item.get("note"),
        "ic_mean": metrics.get("ic_mean"), "icir": metrics.get("icir"), "nw_t": metrics.get("nw_t"),
        "fdr_pass": metrics.get("fdr_pass"), "hlz_pass": metrics.get("hlz_pass"),
        "max_corr": metrics.get("max_corr"), "months": metrics.get("months"),
        "cluster_size": cluster.get("size"), "representative": cluster.get("representative"),
        "evidence": (item.get("evidence") or [])[:3],
    }


@router.get("/factors")
def agent_factors(kind: str = "", status: str = "", category: str = "", q: str = "",
                  limit: int = 60, offset: int = 0) -> dict:
    """因子库（族 B 的因子 + 族 A 的条件，同一张表）。

    这个接口是"投研执行台"和"因子挖掘"合并后的公共数据源：挖掘页看会话与提议，
    执行台看"到底有多少条过了门槛、哪些被同簇更强因子替代了"。
    """
    payload = _factor_library()
    items = payload.get("factors", [])
    if kind:
        items = [item for item in items if item.get("kind") == kind]
    if status:
        items = [item for item in items if item.get("status") == status]
    if category:
        items = [item for item in items
                 if (item.get("category") or {}).get("key") == category
                 or (item.get("category") or {}).get("label") == category]
    if q:
        needle = q.strip().lower()
        items = [item for item in items
                 if needle in str(item.get("id", "")).lower()
                 or needle in str(item.get("expression", "")).lower()
                 or needle in str(item.get("family", "")).lower()]
    total = len(items)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    return {
        "available": bool(payload),
        "generated_at": payload.get("generated_at"),
        "criteria": payload.get("criteria", {}),
        "window_months": payload.get("window_months", []),
        "delta": payload.get("delta", {}),
        "totals": payload.get("totals", {"n": total}),
        # 分类清单（页面用它出"分类 chips"：量价 / 价值 / 财务 / 宏观代理 …）
        "categories": [
            {"key": key, "label": meta.get("label", key), "core": meta.get("core", "")}
            for key, meta in (payload.get("glossary_categories") or {}).items()
        ],
        "filtered": total,
        "offset": offset,
        "limit": limit,
        "items": [_factor_item(item) for item in items[offset:offset + limit]],
    }


@router.get("/factors/context")
def factors_context() -> dict:
    """挖掘工作台的「本次上下文」chips（P2.43 §2.3）：面板规模 / 准入口径 / 上次最佳因子 / 研究工具清单。

    口径只从产物里取（面板 manifest、因子库 criteria、最近一条挖掘会话的环境行），
    不在这里另写一份 —— 页面显示的准入阈值必须与因子库、与挖掘脚本用的一致。
    """
    panel_manifest = sorted((ROOT / "output/data_quality").glob("clean_feature_panel_cn_*_manifest.json"))
    panel: dict = {"available": False}
    if panel_manifest:
        path = panel_manifest[-1]
        payload = _read_json(path, {})
        panel = {
            "available": True,
            "version": str(payload.get("cleaning_version") or ""),
            "stocks": payload.get("stocks"),
            "features": payload.get("feature_count"),
            "asof": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            "source": _rel_path(path),
        }
    # 面板规模（行数/交易日）与"上次最佳因子"都从最近的挖掘会话里取（脚本自己打的 env/OK 行）
    sessions = sorted((ROOT / "output/factor_mining_sessions").glob("*.json"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    last: dict = {"available": False}
    if sessions:
        payload = _read_json(sessions[0], {})
        env_line = next((m.get("text", "") for m in payload.get("messages", [])
                         if "面板" in str(m.get("text", ""))), "")
        factors = payload.get("factors") or []
        best = max(factors, key=lambda f: abs(float(f.get("icir") or 0)), default=None)
        last = {
            "available": bool(factors),
            "session_id": payload.get("id"),
            "env_line": env_line,
            "panel_rows": (re.search(r"面板 ([\d,]+) 行", env_line).group(1) if re.search(r"面板 ([\d,]+) 行", env_line) else None),
            "trading_days": (re.search(r"/(\s*)?(\d+) 个交易日", env_line).group(2)
                             if re.search(r"/(\s*)?(\d+) 个交易日", env_line) else None),
            "n_factors": len(factors),
            "best": None if best is None else {k: best.get(k) for k in
                                               ("name", "ic_mean", "icir", "nw_t", "fdr_pass", "expression", "reason")},
        }
    library = _factor_library()
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "panel": panel,
        "criteria": library.get("criteria") or {},
        "last_session": last,
        "tools": [
            {"name": "因子 IC 协议", "command": "uv run python scripts/factor_ic_protocol.py",
             "artifact": "output/factor_ic_registry.csv", "page": "#/factor-ic"},
            {"name": "因子库", "command": "uv run python scripts/sync_factor_library.py --print",
             "artifact": "output/factor_library/factors.json", "page": "#/agent?tab=mining"},
            {"name": "上线闸门", "command": "uv run python scripts/promotion_check.py",
             "artifact": "output/results_cn/promotion_check/<ts>/verdicts.csv", "page": "#/paper"},
            {"name": "影子组合", "command": "uv run python scripts/run_shadow_variants.py",
             "artifact": "output/paper_trading/shadow/summary.json", "page": "#/paper"},
            {"name": "策略池", "command": "uv run python scripts/sync_strategy_pool.py --print",
             "artifact": "output/strategy_pool/strategy_pool.json", "page": "#/strategies"},
            {"name": "下单前 checklist", "command": "uv run python scripts/pre_order_check.py --print",
             "artifact": "output/results_cn/pre_order_check/<date>_<account>.md", "page": "#/agent?tab=console"},
            {"name": "上线评估", "command": "uv run python scripts/promote_candidate.py <strategy_id>",
             "artifact": "output/results_cn/promotion_check/<ts>/promote_<id>.md", "page": "#/agent?tab=console"},
        ],
        # 方向模板与灵感来源都从 config/research_sources.json 来（声明式登记，加一条不用改代码）
        "templates": research_source_templates(),
        "research_sources": research_sources(),
    }


def _research_registry() -> dict:
    return _read_json(RESEARCH_SOURCES, {"sources": [], "templates": []})


def research_source_templates() -> list[dict]:
    """方向模板（label / direction / source）。前两条保持与历史口径一致，方便老书签/测试对得上。"""
    templates = _research_registry().get("templates") or []
    return [{"label": t.get("label"), "direction": t.get("direction"), "source": t.get("source")}
            for t in templates if t.get("label") and t.get("direction")]


def research_sources() -> list[dict]:
    return list(_research_registry().get("sources") or [])


@router.get("/research-sources")
def agent_research_sources() -> dict:
    """灵感来源 / 知识库注册表（P2.51）：挖因子时该去哪儿找方向、每个来源怎么用、坑在哪。

    **纪律**（注册表里逐条写了 caution）：来源只提供**方向**，不提供**证据** ——
    证据一律回到本地 IC 协议（|IC|≥0.02 / BH-FDR q=0.05 / |t|≥3 / maxρ≤0.7）与上线闸门。
    """
    registry = _research_registry()
    sources = research_sources()
    by_region: dict[str, list[dict]] = {}
    for item in sources:
        by_region.setdefault(str(item.get("region") or "其他"), []).append(item)
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "note": registry.get("note") or [],
        "sources": sources,
        "by_region": by_region,
        "templates": research_source_templates(),
        "counts": {"sources": len(sources), "templates": len(registry.get("templates") or [])},
        "path": _rel_path(RESEARCH_SOURCES),
    }


LEDGER = ROOT / "output/results_cn/selection_ledger.parquet"
_LEDGER_CACHE: dict = {"mtime": None, "payload": None}


def _model_score_ic(horizon: str = "r20") -> dict | None:
    """模型分的 IC 曲线（**台账口径**）：决策日截面秩 IC(model_score, 已实现 r20/excess_r20)。

    为什么用台账而不是别的：模型分**没有逐日落盘的历史**（`output/model_scores/cn_lightgbm_scores.csv`
    只有当天一版），所以唯一能算历史 IC 的地方是选股台账（它记录了每个决策日的 model_score 与已实现收益）。
    口径差异必须写明：**稀疏决策日 + 已实现收益**，与面板口径的「逐日 + 自建前向收益」**不可直接比**。
    """
    if not LEDGER.exists():
        return None
    try:
        import pandas as pd

        mtime = LEDGER.stat().st_mtime
        if _LEDGER_CACHE["mtime"] != mtime:
            frame = pd.read_parquet(LEDGER, columns=["decision_date", "stock_code", "model_score", horizon])
            frame = frame.dropna(subset=["model_score", horizon])
            rows = []
            for day, chunk in frame.groupby("decision_date"):
                if len(chunk) < 10:
                    continue
                ic = float(chunk["model_score"].corr(chunk[horizon], method="spearman"))
                rows.append({"date": str(day)[:10], "ic": ic, "names": int(len(chunk))})
            rows.sort(key=lambda item: item["date"])
            values = pd.Series([item["ic"] for item in rows])
            _LEDGER_CACHE.update(mtime=mtime, payload={
                "points": rows,
                "periods": len(rows),
                "mean": float(values.mean()) if len(values) else None,
                "icir": (float(values.mean() / values.std(ddof=1)) if len(values) > 1 and values.std(ddof=1) else None),
                "positive_share": float((values > 0).mean()) if len(values) else None,
                "window": [rows[0]["date"], rows[-1]["date"]] if rows else None,
            })
        return _LEDGER_CACHE["payload"]
    except Exception:
        return None


@router.get("/factors/fitness")
def factor_fitness(profile: str = "default") -> dict:
    """因子适应度打分（`scripts/factor_fitness.py` 产出；可切换 profile）。

    纪律写在产物里：fitness **只用于排序候选**，准入仍由 IC 协议判、上线仍由闸门判。
    """
    path = ROOT / "output/factor_library/fitness.json"
    if not path.exists():
        return {"available": False, "reason": "还没有 fitness 产物",
                "command": "uv run python scripts/factor_fitness.py --profile default --print"}
    payload = _read_json(path, {})
    if profile != payload.get("profile"):
        # 请求了别的 profile → 现算一份（脚本很快：只读 factors.json + inspector 档案）
        proc = subprocess.run([str(ROOT / ".venv/bin/python"), "scripts/factor_fitness.py",
                               "--profile", profile], cwd=ROOT, capture_output=True, text=True)
        if proc.returncode != 0:
            raise HTTPException(status_code=400, detail=(proc.stderr or proc.stdout or "")[-300:])
        payload = _read_json(path, {})
    payload["available"] = True
    payload["path"] = _rel_path(path)
    return payload


@router.get("/factors/inspector/compare")
def factor_inspector_compare(ids: str = "", include_model: int = 0) -> dict:
    """多条（2–5 条）因子的审查档案对比：滚动 IC 对齐叠加 + 指标表。

    口径：只对齐**两边都有**的日期（不做插值、不补造）；某条没有档案就列进 `missing` 并给出命令。
    """
    wanted = [item.strip() for item in str(ids).split(",") if item.strip()][:5]
    if len(wanted) < 2:
        raise HTTPException(status_code=400, detail="至少给两条因子 id：/api/agent/factors/inspector/compare?ids=a,b")
    series, table, missing = [], [], []
    for factor_id in wanted:
        path = INSPECTOR_DIR / f"{factor_id}.json"
        payload = _read_json(path, None)
        if not payload or payload.get("status") != "ok":
            missing.append({"id": factor_id,
                            "command": f"uv run python scripts/factor_inspector.py --factor {factor_id} --print"})
            continue
        rolling = ((payload.get("rolling_ic") or {}).get("20") or [])
        ic = (payload.get("ic_by_horizon") or {}).get("20") or {}
        quant = payload.get("quantiles") or {}
        red = payload.get("redundancy") or {}
        series.append({"id": factor_id, "points": [{"date": row.get("date"), "ic": row.get("ic")} for row in rolling]})
        table.append({
            "id": factor_id, "periods": ic.get("periods"), "ic_h20": ic.get("mean"), "icir_h20": ic.get("icir"),
            "nw_t_h20": ic.get("nw_t"), "positive_share": ic.get("positive_share"),
            "long_short_mean": quant.get("long_short_mean"), "long_short_win_rate": quant.get("long_short_win_rate"),
            "top_turnover_mean": quant.get("top_turnover_mean"),
            "max_abs_rho": red.get("max_abs_rho"), "max_abs_rho_peer": red.get("max_abs_rho_peer"),
            "redundant": red.get("redundant"),
        })
    if include_model:
        ledger = _model_score_ic()
        if ledger and ledger["points"]:
            series.append({"id": "model_score@ledger",
                           "points": [{"date": item["date"], "ic": item["ic"]} for item in ledger["points"]]})
            table.append({"id": "model_score@ledger", "periods": ledger["periods"], "ic_h20": ledger["mean"],
                          "icir_h20": ledger["icir"], "nw_t_h20": None,
                          "positive_share": ledger["positive_share"], "long_short_mean": None,
                          "long_short_win_rate": None, "top_turnover_mean": None,
                          "max_abs_rho": None, "max_abs_rho_peer": None, "redundant": None})
            note_model = (f"模型分曲线来自**选股台账**（{ledger['periods']} 个决策日，"
                          f"{ledger['window'][0]} ~ {ledger['window'][1]}）：决策日截面秩 IC(model_score, 已实现 r20)，"
                          "稀疏且是已实现收益 —— 与面板口径的逐日 IC **不可直接比**。")
        else:
            missing.append({"id": "model_score@ledger", "command": "先跑 scripts/build_selection_ledger.py"})
            note_model = ""
    else:
        note_model = ""

    common_dates: list[str] = []
    if len(series) >= 2:
        sets = [{point["date"] for point in item["points"]} for item in series]
        common_dates = sorted(set.intersection(*sets))
    return {"available": bool(table), "ids": wanted, "common_dates": len(common_dates),
            "window": [common_dates[0], common_dates[-1]] if common_dates else None,
            "series": series, "table": table, "missing": missing,
            "note": "滚动 IC 只对齐两边都有的日期（不插值）；h20 口径。" + note_model}


@router.get("/factors/{factor_id}/inspector")
def factor_inspector(factor_id: str) -> dict:
    """单条因子的**审查档案**（滚动 IC / 衰减 / 分位组合 / 换手 / 冗余 / 血缘）。

    参考 FactorMiner 的 Inspector 维度，但口径按本仓库（脚本 `scripts/factor_inspector.py` 产出）。
    没有档案时**明确返回 available=false 并给出命令**，不生成占位曲线（宁可空，不造假）。
    """
    path = INSPECTOR_DIR / f"{factor_id}.json"
    if not path.exists():
        index_path = INSPECTOR_DIR / "index.json"
        index = _read_json(index_path, {"factors": {}})
        return {"available": False, "factor": factor_id,
                "reason": "还没有审查档案（跑一次即可）",
                "command": f"uv run python scripts/factor_inspector.py --factor {factor_id} --print",
                "reviewed": sorted((index.get("factors") or {}).keys())[:40],
                "criteria": index.get("criteria") or {}}
    payload = _read_json(path, {})
    payload["available"] = True
    payload["path"] = _rel_path(path)
    payload["markdown"] = str(_rel_path(INSPECTOR_DIR / f"{factor_id}.md"))
    return payload


@router.get("/overview")
def agent_overview() -> dict:
    runs = _run_summaries()
    families = build_families()
    layers = []
    for layer in LAYERS:
        built = LAYER_BUILDERS[layer["key"]]()
        layers.append({**layer, **built})
    latest = runs[0] if runs else None
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "families": families,
        "layers": layers,
        "runs": {"count": len(runs), "latest": latest,
                 "n_ok": (latest or {}).get("totals", {}).get("n_ok"),
                 "n_failed": (latest or {}).get("totals", {}).get("n_failed")},
        "gates": (latest or {}).get("gates", []),
        "factor_library": {
            "available": bool(_factor_library()),
            "generated_at": _factor_library().get("generated_at"),
            "totals": _factor_library().get("totals", {}),
            "criteria": _factor_library().get("criteria", {}),
        },
        "profile": {key: load_profile()[key] for key in ("role", "updated_at", "is_default")},
    }


@router.get("/runs")
def agent_runs(limit: int = 20) -> dict:
    runs = _run_summaries(max(1, min(limit, 200)))
    return {"count": len(runs), "runs": runs}


@router.get("/runs/{run_id}")
def agent_run_detail(run_id: str) -> dict:
    path = RUNS_DIR / run_id / "run.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"没有这个运行轨迹：{run_id}")
    return _read_json(path, {})


@router.get("/profile")
def get_profile() -> dict:
    profile = load_profile()
    return {
        "profile": profile,
        "default": {"role": DEFAULT_PROFILE["role"], "system_prompt": DEFAULT_PROFILE["system_prompt"],
                    "language": DEFAULT_PROFILE["language"], "detail_level": DEFAULT_PROFILE["detail_level"]},
        "history_count": _history_count(),
        "path": _rel_path(PROFILE_PATH),
    }


class ProfileUpdate(BaseModel):
    role: str | None = Field(default=None, max_length=80)
    language: str | None = Field(default=None, max_length=20)
    detail_level: str | None = Field(default=None, max_length=200)
    system_prompt: str | None = Field(default=None, max_length=20000)
    notes: str | None = Field(default=None, max_length=2000)


def _snapshot(profile: dict) -> None:
    """把旧版本存进历史（最多留 50 份），保证"改坏了能找回上一版"。"""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    path = HISTORY_DIR / f"{stamp}.json"
    payload = {key: profile.get(key) for key in PROFILE_FIELDS}
    payload["updated_at"] = profile.get("updated_at")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    backups = sorted(HISTORY_DIR.glob("*.json"))
    for stale in backups[:-50]:
        try:
            stale.unlink()
        except OSError:
            pass


def _write_profile(profile: dict) -> dict:
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: profile[key] for key in PROFILE_FIELDS}
    payload["version"] = 1
    payload["updated_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S%z")
    tmp = PROFILE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(PROFILE_PATH)
    return payload


@router.put("/profile")
def update_profile(payload: ProfileUpdate) -> dict:
    current = load_profile()
    changed = {key: value for key, value in payload.model_dump().items() if value is not None}
    if not changed:
        raise HTTPException(status_code=400, detail="没有要更新的字段")
    for key, value in changed.items():
        if key in ("role", "system_prompt") and not value.strip():
            raise HTTPException(status_code=400, detail=f"{key} 不能为空（要恢复默认用 /profile/reset）")
    _snapshot(current)
    updated = {**{key: current[key] for key in PROFILE_FIELDS}, **changed}
    saved = _write_profile(updated)
    return {"saved": True, "profile": {**{k: saved[k] for k in PROFILE_FIELDS},
                                       "updated_at": saved["updated_at"],
                                       "is_default": False},
            "history_count": _history_count()}


@router.post("/profile/reset")
def reset_profile() -> dict:
    current = load_profile()
    _snapshot(current)
    saved = _write_profile(dict(DEFAULT_PROFILE))
    return {"saved": True, "profile": {**{k: saved[k] for k in PROFILE_FIELDS},
                                       "updated_at": saved["updated_at"], "is_default": True},
            "history_count": _history_count()}


@router.get("/profile/history")
def profile_history(limit: int = 10) -> dict:
    if not HISTORY_DIR.exists():
        return {"count": 0, "items": []}
    files = sorted(HISTORY_DIR.glob("*.json"), reverse=True)[:max(1, min(limit, 50))]
    items = []
    for path in files:
        payload = _read_json(path, {})
        items.append({"at": path.stem, "role": payload.get("role"),
                      "prompt_chars": len(payload.get("system_prompt") or ""),
                      "path": _rel_path(path)})
    return {"count": len(items), "items": items}


def _latest_json(folder: Path, pattern: str) -> dict | None:
    """取目录里最新的匹配文件内容（找不到返回 None）。"""
    if not folder.exists():
        return None
    files = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files:
        payload = _read_json(path, None)
        if payload:
            return {**payload, "_path": _rel_path(path)}
    return None


@router.get("/actions")
def agent_actions() -> dict:
    """执行台的**可点动作**清单 + 上次结果（P2.42 §4.4 / §4.5 的人工那一步）。

    动作只有两个，都跑本地脚本、都不改生产：
      * `pre_order_check`：下单前 checklist（账户可选 paper / live）；
      * `promote_candidate`：上线评估（参数限定为 `config/strategies.json` 里**在册**的 id，
        否则就是任意命令注入口 —— 白名单是硬要求）。
    """
    registry = _read_json(CONFIG_PATH, {"strategies": []})
    ids = [{"id": s.get("id"), "name": s.get("name"), "stage": s.get("stage")}
           for s in registry.get("strategies", []) if s.get("id")]
    return {
        "actions": [
            {"key": "pre_order_check", "label": "下单前 checklist",
             "desc": "数据新鲜度 / 执行假设一致性 / 涨跌停停牌 / 资金整手 / 单票上限 / PK 护栏 / 参考账户一致性",
             "command": "uv run python scripts/pre_order_check.py --account <paper|live> --print",
             "accounts": ["paper", "live"]},
            {"key": "factor_inspect", "label": "因子审查（Inspector）",
             "desc": "滚动 IC / 多 horizon 衰减 / 5 档分位组合 / 换手 / 与准入因子冗余 / 数据血缘",
             "command": "uv run python scripts/factor_inspector.py --factor <factor_id> --print",
             "factors": [item.get("id") for item in (_factor_library().get("factors") or [])
                         if item.get("status") in ("admitted", "in_use", "candidate")
                         and not str(item.get("id", "")).endswith("_is_missing")][:60]},
            {"key": "promote_candidate", "label": "上线评估",
             "desc": "登记候选 → 影子/闸门判决 → 给人工动作（默认不改 config）",
             "command": "uv run python scripts/promote_candidate.py <strategy_id>",
             "strategies": ids},
        ],
        "pre_order": {"paper": _latest_json(PRE_ORDER_DIR, "paper_latest.json"),
                      "live": _latest_json(PRE_ORDER_DIR, "live_latest.json")},
        "promote": _latest_json(PROMOTION_DIR, "*/promote_*.json"),
    }


class ActionRequest(BaseModel):
    action: str = Field(max_length=40)
    account: str | None = Field(default="paper", max_length=10)
    strategy_id: str | None = Field(default=None, max_length=60)
    factor_id: str | None = Field(default=None, max_length=80)


@router.post("/actions/run")
def agent_run_action(req: ActionRequest) -> dict:
    """跑一个动作并把结果回给页面（同步、非阻塞无关：两个脚本都是秒级）。"""
    resolved_account = None
    if req.action == "pre_order_check":
        # 账户也要走白名单：非法值**回落**成 paper（不把用户输入透传给子进程），
        # 读回结果时用的也是这个回落后的值 —— 实测踩过：用原始 req.account 去拼文件名 ⇒ 找不到产物。
        resolved_account = req.account if req.account in ("paper", "live") else "paper"
        cmd = [str(ROOT / ".venv/bin/python"), "scripts/pre_order_check.py",
               "--account", resolved_account, "--print"]
    elif req.action == "promote_candidate":
        registry = _read_json(CONFIG_PATH, {"strategies": []})
        allowed = {s.get("id") for s in registry.get("strategies", [])}
        if req.strategy_id not in allowed:
            raise HTTPException(status_code=400,
                                detail=f"{req.strategy_id!r} 不在 config/strategies.json 里（可用：{sorted(allowed)}）")
        cmd = [str(ROOT / ".venv/bin/python"), "scripts/promote_candidate.py", str(req.strategy_id)]
    elif req.action == "factor_inspect":
        library = _factor_library()
        known = {str(item.get("id")) for item in (library.get("factors") or [])}
        if req.factor_id not in known:
            raise HTTPException(status_code=400,
                                detail=f"{req.factor_id!r} 不在因子库里（可用：{sorted(known)[:8]} …共 {len(known)} 条）")
        cmd = [str(ROOT / ".venv/bin/python"), "scripts/factor_inspector.py",
               "--factor", str(req.factor_id), "--months", "24", "--print"]
    else:
        raise HTTPException(status_code=400,
                            detail=f"未知动作 {req.action!r}；可用：pre_order_check / promote_candidate / factor_inspect")
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    payload = {"action": req.action, "command": " ".join(cmd[1:]), "exit": proc.returncode,
               "stdout": (proc.stdout or "")[-4000:], "stderr": (proc.stderr or "")[-1000:]}
    if req.action == "pre_order_check":
        payload["result"] = _latest_json(PRE_ORDER_DIR, f"{resolved_account}_latest.json")
    elif req.action == "factor_inspect":
        payload["result"] = _read_json(INSPECTOR_DIR / f"{req.factor_id}.json", None)
    else:
        payload["result"] = _latest_json(PROMOTION_DIR, f"*/promote_{req.strategy_id}.json")
    return payload


@router.get("/health")
def agent_health() -> dict:
    """给"投研执行台"自检用：三块数据源在不在、提示词文件是不是可写。"""
    return {
        "runs_dir": {"path": _rel_path(RUNS_DIR), "exists": RUNS_DIR.exists(),
                     "count": len(_run_summaries())},
        "profile": {"path": _rel_path(PROFILE_PATH), "exists": PROFILE_PATH.exists(),
                    "writable": PROFILE_PATH.exists() and PROFILE_PATH.stat().st_mode & 0o200 != 0},
        "pool": {"path": _rel_path(POOL_PATH), "exists": POOL_PATH.exists()},
        "factor_library": {"path": _rel_path(FACTOR_LIBRARY), "exists": FACTOR_LIBRARY.exists(),
                           "n": (_factor_library().get("totals") or {}).get("n")},
        "cli": {"script": "scripts/agent_run.py", "exists": (ROOT / "scripts/agent_run.py").exists()},
        "disk_free_gb": round(shutil.disk_usage(ROOT).free / 1e9, 1),
    }
