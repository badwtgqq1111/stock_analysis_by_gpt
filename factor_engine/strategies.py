"""策略注册表：把"模型腿"与各个信号腿放在同一个抽象下。

为什么需要它（2026-09-29 结论）：
    现状把"入场状态"塞在一个全局启动门禁里（`[selection.startup_gate]`），而模型腿与
    donchian 突破腿对状态的偏好是**相反**的——模型腿要"贴近 120 日低点 + 缩量"，
    突破腿要"刚突破 + 放量"。用一个门禁的两套参数去描述两种策略，就是在拿一个
    标量去拟合两个不同的条件分布；策略数量一多必然互相打架。

    正确的抽象：**策略 = (信号, 状态/资格, 仓位, 退出, 预算, 证据)**。
    门禁从"全局标量"降级为"每个策略自己的一段状态定义（state block）"，
    组合层只负责在策略之间分配预算（risk budgeting）与执行约束。

本模块只做三件事：读配置 → 校验 → 提供可复用接口（`state_mask` / `describe`）。
它**不改变现有选股行为**：接线到选股/组合层是后续步骤（见 docs/todo/P1_22 §23）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np
import pandas as pd

KINDS = ("model", "signal")
STATUSES = ("active", "research", "off")
OPS = ("gte", "lte", "gt", "lt", "between")


@dataclass(frozen=True)
class StateRule:
    column: str
    op: str
    value: float | None = None
    lo: float | None = None
    hi: float | None = None
    note: str = ""

    def mask(self, frame: pd.DataFrame) -> pd.Series:
        if self.column not in frame.columns:
            raise KeyError(f"strategy state column missing from frame: {self.column}")
        values = pd.to_numeric(frame[self.column], errors="coerce")
        if self.op == "gte":
            return values.ge(self.value)
        if self.op == "lte":
            return values.le(self.value)
        if self.op == "gt":
            return values.gt(self.value)
        if self.op == "lt":
            return values.lt(self.value)
        if self.op == "between":
            return values.between(self.lo, self.hi)
        raise ValueError(f"unsupported op {self.op!r}")


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    kind: str
    status: str = "active"
    label: str = ""
    signal: dict[str, Any] = field(default_factory=dict)
    state: tuple[StateRule, ...] = ()
    sizing: dict[str, Any] = field(default_factory=dict)
    exit: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id, "kind": self.kind, "status": self.status, "label": self.label,
            "signal": dict(self.signal),
            "state": [{"column": r.column, "op": r.op, "value": r.value, "lo": r.lo, "hi": r.hi, "note": r.note}
                      for r in self.state],
            "sizing": dict(self.sizing), "exit": dict(self.exit),
            "budget": dict(self.budget), "evidence": dict(self.evidence),
        }


def _parse_state(block: Any) -> tuple[StateRule, ...]:
    rules: list[StateRule] = []
    if not block:
        return ()
    if not isinstance(block, dict):
        raise TypeError("strategy.state must be a table of column -> {op, ...}")
    for column, spec in block.items():
        if not isinstance(spec, dict):
            raise TypeError(f"state rule for {column} must be a table")
        op = str(spec.get("op", "")).strip().lower()
        if op not in OPS:
            raise ValueError(f"state rule for {column}: op must be one of {OPS}, got {op!r}")
        value = spec.get("value")
        lo, hi = spec.get("lo"), spec.get("hi")
        if op == "between" and (lo is None or hi is None):
            raise ValueError(f"state rule for {column}: between needs lo and hi")
        if op != "between" and value is None:
            raise ValueError(f"state rule for {column}: {op} needs value")
        rules.append(StateRule(column=str(column), op=op,
                               value=None if value is None else float(value),
                               lo=None if lo is None else float(lo),
                               hi=None if hi is None else float(hi),
                               note=str(spec.get("note", "") or "")))
    return tuple(rules)


def load_strategies(config: dict | None) -> dict[str, StrategySpec]:
    """从 `[strategies.*]` 读取策略定义并校验。缺省（无该段）返回空字典，保持向后兼容。"""
    raw = (config or {}).get("strategies") or {}
    specs: dict[str, StrategySpec] = {}
    for name, block in raw.items():
        if not isinstance(block, dict):
            raise TypeError(f"[strategies.{name}] must be a table")
        kind = str(block.get("kind", "")).strip().lower()
        if kind not in KINDS:
            raise ValueError(f"[strategies.{name}].kind must be one of {KINDS}, got {kind!r}")
        status = str(block.get("status", "active")).strip().lower()
        if status not in STATUSES:
            raise ValueError(f"[strategies.{name}].status must be one of {STATUSES}, got {status!r}")
        specs[str(name)] = StrategySpec(
            strategy_id=str(name), kind=kind, status=status,
            label=str(block.get("label", "") or ""),
            signal=dict(block.get("signal") or {}),
            state=_parse_state(block.get("state")),
            sizing=dict(block.get("sizing") or {}),
            exit=dict(block.get("exit") or {}),
            budget=dict(block.get("budget") or {}),
            evidence=dict(block.get("evidence") or {}),
        )
    return specs


def active_strategies(specs: dict[str, StrategySpec], kind: str | None = None) -> list[StrategySpec]:
    return [spec for spec in specs.values()
            if spec.status == "active" and (kind is None or spec.kind == kind)]


def total_budget(specs: dict[str, StrategySpec]) -> float:
    """所有 active 策略声明的预算份额之和（组合层应要求 <= 1）。"""
    total = 0.0
    for spec in active_strategies(specs):
        share = spec.budget.get("share")
        if share is not None:
            total += float(share)
    return round(total, 6)


def state_mask(frame: pd.DataFrame, spec: StrategySpec, *, allow_missing: bool = False) -> pd.Series:
    """该策略的状态/资格掩码（全部规则 AND）。没有 state 时返回全 True。

    `allow_missing=True` 时跳过帧里没有的列（用于"体检类"调用：某些规则列只在候选阶段才有，
    例如 recipe 输出的 signal_extension）。选股路径应保持默认（缺列即报错）。
    """
    mask = pd.Series(True, index=frame.index)
    for rule in spec.state:
        if rule.column not in frame.columns:
            if allow_missing:
                continue
            raise KeyError(f"strategy state column missing from frame: {rule.column}")
        mask &= rule.mask(frame).fillna(False).astype(bool)
    return mask


def strategy_for_pick(selection_channel: str | None, recipe: str | None, sleeve: str | None,
                      specs: dict[str, StrategySpec]) -> str:
    """把选股结果里的通道/配方映射到策略 id（用于归因与报告，不改变选股逻辑）。"""
    channel = str(selection_channel or "").strip().lower()
    recipe_name = str(recipe or "").strip().lower()
    sleeve_name = str(sleeve or "").strip().lower()
    for candidate in (recipe_name, sleeve_name):
        if candidate and candidate in specs:
            return candidate
    if channel in {"model", ""}:
        for name, spec in specs.items():
            if spec.kind == "model":
                return name
    if recipe_name:
        return recipe_name
    return channel or "unknown"


def validate_budgets(specs: dict[str, StrategySpec], *, raise_on_overflow: bool = False) -> dict:
    """校验 active 策略的预算份额之和 <= 1，并给出逐策略明细。

    策略化之后最容易犯的错就是"每个策略都给一点预算、加起来超 1"——
    这在组合层会表现为隐性的杠杆或静默截断。
    """
    shares = {spec.strategy_id: float(spec.budget.get("share") or 0.0)
              for spec in active_strategies(specs)}
    total = round(sum(shares.values()), 6)
    report = {"shares": shares, "total": total, "overflow": round(max(0.0, total - 1.0), 6),
              "ok": total <= 1.0 + 1e-9}
    if raise_on_overflow and not report["ok"]:
        raise ValueError(f"active strategy budgets sum to {total:.4f} > 1.0: {shares}")
    return report


def model_state_exposures(specs: dict[str, StrategySpec], *, kind: str = "model") -> list[str]:
    """取某类策略 state 里声明的列，作为门禁正交化的暴露集（§23.4 第②步）。

    这样"模型腿用贴近低点+缩量"就不再是门禁代码里的硬编码列表，而是**从注册表派生**：
    改策略状态 = 改配置，门禁跟着变。
    """
    for spec in active_strategies(specs, kind=kind):
        explicit = (spec.signal or {}).get("orthogonalize_exposures")
        if explicit:
            return [str(column) for column in explicit]
    exposures: list[str] = []
    for spec in active_strategies(specs, kind=kind):
        for rule in spec.state:
            if rule.column not in exposures:
                exposures.append(rule.column)
    return exposures


def state_compliance(frame: pd.DataFrame, spec: StrategySpec) -> tuple[pd.Series, pd.DataFrame]:
    """逐行判断是否满足该策略的 state，并给出每条规则的逐行结果（用于归因）。"""
    detail = pd.DataFrame(index=frame.index)
    ok = pd.Series(True, index=frame.index)
    for rule in spec.state:
        values = rule.mask(frame)
        detail[f"{rule.column}:{rule.op}"] = values
        ok &= values.fillna(False).astype(bool)
    return ok, detail


def derive_sleeve_budgets(specs: dict[str, StrategySpec], *, key: str = "recipe") -> dict:
    """把 [strategies.*] 的 sizing 派生成 sleeve 口径（§23.4 第③步）。

    返回 {strategy_id: {slots, min_weight, max_weight}}，用于替换散落在
    `[selection.signals.*_sleeve]` 里的硬编码；`key` 决定用哪个字段匹配 sleeve
    （recipe = 信号函数名）。
    """
    derived: dict[str, dict] = {}
    for spec in active_strategies(specs, kind="signal"):
        recipe = str(spec.signal.get(key) or spec.strategy_id)
        sizing = dict(spec.sizing or {})
        derived[recipe] = {
            "strategy_id": spec.strategy_id,
            "slots": int(sizing.get("slots", 1) or 1),
            "min_weight": float(sizing.get("min_weight", 0.0) or 0.0),
            "max_weight": float(sizing.get("max_weight", 0.0) or 0.0),
            "budget_share": float((spec.budget or {}).get("share", 0.0) or 0.0),
        }
    return derived


def compare_sleeve_budgets(specs: dict[str, StrategySpec], sleeve_config: dict, *, key: str = "recipe") -> dict:
    """把注册表派生值与现有 sleeve 硬编码逐字段对比，返回差异（用于迁移期审计）。"""
    derived = derive_sleeve_budgets(specs)
    diffs: dict[str, dict] = {}
    for recipe, want in derived.items():
        spec = next((item for item in specs.values() if str(item.signal.get(key) or item.strategy_id) == recipe), None)
        section = str(((spec.budget if spec else {}) or {}).get("sleeve_section") or "")
        if section:
            block = (sleeve_config or {}).get(section)
            field_map = {"slots": "slots", "min_weight": "min_weight", "max_weight": "max_weight"}
        else:
            # setup 腿没有独立 sleeve 段：它对应 [selection.signals] 顶层的 forced_* 字段
            block = sleeve_config or {}
            field_map = {"slots": "max_overrides", "min_weight": "forced_min_weight",
                         "max_weight": "forced_max_weight"}
        if block is None:
            block = (sleeve_config or {}).get(recipe) or (sleeve_config or {}).get(f"{recipe}_sleeve")
        if not isinstance(block, dict):
            diffs[recipe] = {"missing_sleeve_block": True, "derived": want}
            continue
        row = {}
        for field in ("slots", "min_weight", "max_weight"):
            have = block.get(field_map[field])
            if have is None or float(have) != float(want[field]):
                row[field] = {"config_field": field_map[field], "config": have, "registry": want[field]}
        if row:
            diffs[recipe] = row
    return {"derived": derived, "diffs": diffs, "consistent": not diffs}


def state_conformance(frame: pd.DataFrame, spec: StrategySpec, *, score_column: str, top_k: int = 20) -> dict:
    """核对"策略声明的入场状态"与"它的头部候选实际长什么样"是否一致。

    动机（2026-09-30）：模型腿曾声明"贴近 120 日低点 + 缩量"，但实测模型分 Top-20 里只有 10%
    落在该带内（全市场 39%），即**声明与行为相反**。这类错误在策略化之后必须能被自动发现，
    否则每个策略的 state 都会慢慢变成"文档摆件"。

    判据：head_share / market_share = lift。
      lift >= 1  → 头部比全市场更符合该状态（声明与行为同向）
      lift < 0.5 → **mismatch**：声明的状态与头部行为相反
    """
    if spec is None or not spec.state:
        return {"strategy_id": getattr(spec, "strategy_id", None), "verdict": "n/a",
                "reason": "未声明 state（该策略不做入场状态约束）"}
    if score_column not in frame.columns:
        return {"strategy_id": spec.strategy_id, "verdict": "n/a", "reason": f"缺少打分列 {score_column}"}
    work = frame.dropna(subset=[score_column]).copy()
    if work.empty:
        return {"strategy_id": spec.strategy_id, "verdict": "n/a", "reason": "空截面"}
    skipped = [rule.column for rule in spec.state if rule.column not in work.columns]
    mask = state_mask(work, spec, allow_missing=True)
    if skipped and not [rule for rule in spec.state if rule.column in work.columns]:
        return {"strategy_id": spec.strategy_id, "verdict": "n/a",
                "reason": f"state 列都不在帧里：{skipped}"}
    market_share = float(mask.mean())
    top = work.nlargest(min(int(top_k), len(work)), score_column)
    head_mask = mask.reindex(top.index)
    head_share = float(head_mask.mean()) if len(head_mask) else float("nan")
    lift = (head_share / market_share) if market_share > 0 else float("nan")
    if not np.isfinite(lift):
        verdict = "n/a"
    elif lift < 0.5:
        verdict = "mismatch"
    elif lift < 1.0:
        verdict = "weak"
    else:
        verdict = "consistent"
    return {"strategy_id": spec.strategy_id, "verdict": verdict, "top_k": int(top_k),
            "head_in_state_share": round(head_share, 4), "market_in_state_share": round(market_share, 4),
            "lift": round(float(lift), 3) if np.isfinite(lift) else None,
            "rules": [f"{rule.column}:{rule.op}" for rule in spec.state if rule.column in work.columns],
            "skipped_rules": skipped,
            "sample_head": top["stock_code"].astype(str).head(5).tolist() if "stock_code" in top.columns else []}


def apply_strategy_status(signals_config: dict, specs: dict[str, StrategySpec]) -> tuple[dict, dict]:
    """让 `status` 真正生效：非 active 的策略从信号层摘掉（recipe / setup_types / sleeve）。

    为什么需要：`budget_from_registry` 只管预算，"off" 的腿如果不从 `recipes` 与 `allowed_setup_types`
    里摘掉，它仍会被扫描并作为 sleeve 候选进入组合（2026-10-01 体检发现三条腿在可交易口径下是负 edge）。
    返回 (新配置, 审计)。
    """
    signals = dict(signals_config or {})
    audit: dict = {"disabled": [], "enabled": [], "dropped_recipes": [], "dropped_setup_types": []}
    active_ids = {spec.strategy_id for spec in active_strategies(specs)}
    off_ids = {spec.strategy_id for spec in specs.values() if spec.status != "active"}
    if not off_ids:
        return signals, audit

    recipes = [str(name) for name in (signals.get("recipes") or [])]
    setup_types = [str(name) for name in (signals.get("allowed_setup_types") or [])]
    # 不变量：一个 recipe 只能属于**一条腿**。有自己 sleeve 段的策略，其 setup_type 必须从
    # 顶层 setup 腿的 allowed_setup_types 里摘掉，否则它会用 setup 腿的 forced_max_weight（0.20）
    # 绕过本腿预算（2026-10-01 实测：声明 0.05 的 value_reversal 靠这条路吃掉了 11% 权重）。
    owned_elsewhere: list[str] = []
    for strategy_id in sorted(off_ids):
        spec = specs[strategy_id]
        recipe = str(spec.signal.get("recipe") or "")
        types = [str(value) for value in (spec.signal.get("setup_types") or [])]
        section = str((spec.budget or {}).get("sleeve_section") or "")
        if recipe and recipe in recipes:
            recipes = [name for name in recipes if name != recipe]
            audit["dropped_recipes"].append(recipe)
        # 有些策略的 signal_type 就等于 recipe 名（value_reversal / limit_momentum / bottom_momentum），
        # 它们在配置里不一定声明 setup_types → 必须连 recipe 名一起摘掉。
        for value in [*types, *([recipe] if recipe else [])]:
            if value and value in setup_types:
                setup_types = [name for name in setup_types if name != value]
                audit["dropped_setup_types"].append(value)
        if section:
            block = dict(signals.get(section) or {})
            if block.get("enabled"):
                block["enabled"] = False
                signals[section] = block
                audit["disabled"].append(f"{strategy_id}:{section}")
        audit["disabled"].append(strategy_id)
    for strategy_id in sorted(active_ids):
        spec = specs[strategy_id]
        section = str((spec.budget or {}).get("sleeve_section") or "")
        if not section:
            continue          # setup 腿自己拥有它的 setup_types
        recipe_name = str(spec.signal.get("recipe") or "")
        for value in [*(spec.signal.get("setup_types") or []), *([recipe_name] if recipe_name else [])]:
            if value and value in setup_types:
                setup_types = [name for name in setup_types if name != value]
                owned_elsewhere.append(str(value))
    audit["exclusive_ownership_removed"] = owned_elsewhere
    signals["recipes"] = recipes
    signals["allowed_setup_types"] = setup_types
    return signals, audit


def active_sleeve_total(specs: dict[str, StrategySpec]) -> float:
    """active 信号腿的预算合计（与模型腿相加应 <= 1）。"""
    return round(sum(float((spec.budget or {}).get("share") or 0.0)
                     for spec in active_strategies(specs, kind="signal")), 6)
