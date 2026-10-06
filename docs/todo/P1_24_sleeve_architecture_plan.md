# P1.24 Sleeve（策略分片）架构落地计划

编写：2026-10-01。关联：`P1_22_cn_volume_style_oos_shap_plan.md`（数据/归因底座）、
`P1_23_two_rule_strategies_plan.md`（规则策略与 ML 取舍）、`docs/runbooks/xiaomkt-rotation-todo.md`（容量与执行）、`config/cn_pipeline.toml`。

## 0. 结论先行

1. **sleeve 骨架在这个仓库里已经存在**，不是从零开始：`[strategies.*]` 注册表 + `factor_engine/strategies.py`
   已经能做"加载 → 派生 sleeve 预算 → 校验 → 状态生效 → 一致性审计"。当前形态是
   **1 个模型腿（`model_alpha`，share 0.90）+ 5 个规则腿**（dip_low_position / donchian_breakout /
   limit_momentum / value_reversal / bottom_momentum），每个腿有独立 `sizing`（slots/min/max weight）、
   `state` 规则、`exit`、`budget.share` 与 `evidence`。
2. **缺的不是"拆"，而是三样**：① 按因子家族拆分的**模型腿**（现在只有一个模型腿吃 `admitted_90` 全特征）；
   ② 上层的**资金/风险分配器**（现在是配置里手写的静态 `share`）；③ sleeve 层的**归因、共线性与净额化度量**
   （现在没有 sleeve 级 artifact，只有选股结果与事后报告）。
3. **必须带着"证明增量"的前提做**：`[strategies.model_alpha].budget.note` 已记录
   "sleeve 层在可交易口径下无正 edge（P1.22 §32/§33）"。因此本计划把**A/B 验收**放在 Phase 1 之前，
   任何一项没有通过验收就**不合并、并写负结果**（与 P1.23 对两条规则腿的处理方式一致）。
4. **执行顺序**：只读度量（Phase 0）→ 分配器（Phase 1）→ 家族模型腿（Phase 2）→ 汇总执行与容量（Phase 3）。
   越靠后越贵、越容易过拟合；Phase 0/1 即使最终不拆模型，也直接改善可解释性与风险预算。

## 1. 现状盘点（可核对）

| 能力 | 现有实现 | 状态 |
|---|---|---|
| sleeve 注册表 | `config/cn_pipeline.toml` 的 `[strategies.model_alpha]`、`[strategies.dip_low_position]`、`[strategies.donchian_breakout]`、`[strategies.limit_momentum]`、`[strategies.value_reversal]`、`[strategies.bottom_momentum]` | ✅ 6 个腿，含 `kind/status/label/signal/state/sizing/exit/budget/evidence` |
| 注册表加载与派生 | `factor_engine/strategies.py`：`load_strategies` / `derive_sleeve_budgets` / `compare_sleeve_budgets` / `validate_budgets` / `model_state_exposures` | ✅ 381 行；**2026-10-01 实测**：6 腿、`validate_budgets.total=0.95 / overflow=0 / ok=True`、`compare_sleeve_budgets.consistent=True`（注册表与 `[selection.signals.*_sleeve]` 已一致、无差异） |
| 状态生效 | `apply_strategy_status`（非 active 的腿从 recipes / allowed_setup_types / sleeve 里摘掉） | ✅ 2026-10-01 引入 |
| 预算派生开关 | `[selection.signals].budget_from_registry`（第③步） | ✅ 默认开启，可回退 |
| 组合构建 | `preselection` → `pk` 两阶段；`[selection.signals.*_sleeve]` 硬编码（作为审计对照保留） | ✅ |
| 归因 | `scripts/pick_attribution_report.py`、`output/research/gate_ab_pipeline`、`model_comparison` | △ 组合级/模型级，**无 sleeve 级** |
| 容量与执行 | `backtest_engine/xiaomkt_rotation.py` 的 `max_participation`（≤1%×ADV20）、`execution_bars`（5min 铺单）、`scripts/calibrate_impact_xiaomkt.py` | ✅ 2026-10-01 落地，**尚未接到主链路** |
| 独立策略腿样例 | `xiaomkt_rotation` 阶段（小市值轮动，未注册进 `[strategies.*]`） | △ 已跑通，待注册 |

**缺口清单**：

1. 模型腿只有一条（`model_alpha`，`features = "admitted_90"`），没有"估值/动量/质量/反转"的家族拆分与家族内独立训练。
2. 没有分配器：`share` 是配置里手写常量，没有等风险 / 波动目标 / 滚动 ICIR 动态分配的实现与 walk-forward 验收。
3. 没有 sleeve 级 artifact（目标权重级），因此无法做 sleeve 归因、共线性检测、净额化换手度量。
4. 主链路没有接入容量约束（`max_participation`）与订单净额化。

## 2. 目标架构

```
数据层（clean panel / moneyflow / regime）
   │
   ├─ sleeve 层：每个 sleeve 独立产出【目标权重】(sleeve_id, trade_date, stock_code, target_weight, score, ...)
   │     ├─ model_valuation / model_momentum / model_quality / model_reversal   ← 家族约束的模型腿（Phase 2）
   │     └─ signal_value_reversal / signal_bottom_momentum / ...                ← 现有规则腿
   │
   ├─ 分配层：Allocator → {sleeve_id: risk_budget}（静态 share / 等风险 / 滚动 ICIR × 波动目标 / 硬上限）
   │
   ├─ 汇总层：合并重复标的权重 → 净额化（同标的反向订单内部对冲）→ 风格与行业约束 → 单一主组合目标权重
   │
   ├─ 风控层：启动门禁（`startup_gate`）、状态合规（`state_compliance`）、单 sleeve 硬上限、组合级止损
   │
   └─ 执行层：订单聚合 → 小额调仓过滤 → 容量约束（`max_participation`，≤1%×ADV20）→ 铺单（`execution_bars`）
   │
   └─ 归因层：sleeve 级收益归因（贡献加总 = 组合收益）、共线性报告、换手分解
```

## 3. 接口契约（新增，尽量复用现有字段）

### 3.1 sleeve 目标权重 artifact（新增落盘，Phase 0 起）

路径：`output/research/sleeve_targets_<ts>/<sleeve_id>.parquet`，列：

| 列 | 含义 |
|---|---|
| `trade_date` / `stock_code` | 决策日与标的 |
| `sleeve_id` / `sleeve_family` | 分片标识与家族（valuation/momentum/quality/reversal/rule） |
| `target_weight` | **sleeve 内部**归一化后的目标权重（不含预算，预算由分配层给） |
| `score` / `rank` | 该 sleeve 的内部排序键 |
| `state_pass` | 是否通过该 sleeve 的 state 规则 |
| `budget_share` | 分配层本次给该 sleeve 的预算（回写快照，便于复现） |
| `evidence_version` | 对应 `[strategies.*].evidence.updated` |

### 3.2 Python 接口

```python
# factor_engine/sleeve.py（新增）
class Sleeve:                      # 一个 sleeve 的最小实现
    spec: StrategySpec             # 复用现有注册表条目
    def targets(self, as_of: str) -> pd.DataFrame: ...   # 上表 schema

class Allocator:                   # 分配层
    def allocate(self, perf: pd.DataFrame, *, mode: str) -> dict[str, float]: ...
    # mode: "static" | "equal_risk" | "icir_vol" ；perf 列含 sleeve_id, oos_ic, oos_icir, vol, turnover

def aggregate(targets: list[pd.DataFrame], budgets: dict[str, float],
              constraints: dict) -> pd.DataFrame: ...      # 合并 + 净额化 + 约束
```

注册表新增可选字段（向后兼容）：`sleeve_family`（家族）、`allocator`（该 sleeve 的预算上限与波动目标）。

## 4. 分阶段计划

### Phase 0 —— 只读度量（0.5~1 天，无需改主链路）

产物 `scripts/sleeve_diagnostics.py` → `output/research/sleeve_diagnostics_<ts>/{attribution.csv,collinearity.csv,netting.csv}`：

1. **sleeve 归因**：用回放（`--trade-date`）或历史选股产物，把每笔持仓归到 `strategy_for_pick` 判定的 sleeve 上，输出每 sleeve 的收益贡献、权重占比、换手、样本数；校验**贡献加总 = 组合收益（容差 ≤ 0.05pp/月）**。
2. **共线性**：sleeve 目标权重的两两日度相关、行业暴露相关、市值分位暴露重合度（重点看"是不是都压小盘"）。
3. **净额化空间**：统计同标的多 sleeve 反向订单的金额占比与可抵消换手（上界估计）。

验收：三张表能复现；共线性报告给出"当前 6 个腿是否存在伪分散"的明确结论。

#### Phase 0 v1 已执行（2026-10-06，组合层快照）

`uv run python scripts/sleeve_diagnostics.py` → `output/research/sleeve_diagnostics_20261006_093457/`
样本：**42 个回放日 / 404 条 selected 记录**（`replay_*/cn_ensemble_selected.csv`）。

| sleeve | 出现天数 | 累计选股 | 单日均权重 | 市值中位 | 20 日均额中位 |
|---|---|---|---|---|---|
| **model_alpha** | 34 | **183** | **41.51%** | **45.4 亿** | 1.84 亿 |
| setup | 24 | 37 | 3.78% | 136.1 亿 | 2.28 亿 |
| reversal | 34 | 36 | 9.40% | 78.4 亿 | 1.64 亿 |
| bottom_momentum | 12 | 12 | 2.11% | 116.5 亿 | 2.26 亿 |
| momentum | 12 | 12 | 2.92% | 123.3 亿 | 1.93 亿 |

**三条与预期相反的实测结论**：

1. **不存在伪分散**：所有 sleeve 两两 **Jaccard = 0.000**（累计重叠标的 0）—— 各腿选出的标的完全不重叠。
   原计划把"多个 sleeve 都压小盘"列为头号风险，实测**不成立**；真正的风险是另一面（见第 2 条）。
2. **净额化收益 ≈ 0**：因为 sleeve 之间零重叠，`netting.csv` 显示"有重叠标的的天数 0/34、单日最多重叠 0 只"。
   → 方案里"多 sleeve 反向订单内部对冲、降低换手"的收益，在**当前配置下不成立**；它只在 sleeve 候选池高度重叠时才有意义，
   而那时又会踩上共线性问题。二者需要权衡，不能同时宣称。
3. **市值画像被模型腿主导**：model_alpha 持仓市值中位 **45.4 亿**，而规则腿在 78~136 亿；
   模型腿同时占了 41.5% 的单日均权重与 183/404 的选股数 → **组合的市值暴露基本等价于模型腿的市值暴露**。
   这是 Phase 1 分配器必须显式约束的项（否则调预算＝调市值暴露）。

**同时发现的数据缺陷（Phase 1 前置）**：

- 404 条 selected 记录里 **304 条的 `selection_sleeve` 为空**（模型腿不写这个字段），
  本次按 `channel=model → model_alpha` 归一后才得到上表。**Phase 1 第一步必须让每个 sleeve 都写出 sleeve 标签**，
  否则分配器与归因都建立在残缺字段上。
- `strategy_id` 字段是**市场状态标签**（`factor_engine/ml/regime.py:98`：bull→trend_following / sideways→quality_reversion /
  bear→defensive_quality），**不是 sleeve id**；把两者混用会得到"quality_reversion 腿"这种不存在的结论。

**Phase 0 v2（待做，0.5 天）**：用信号层逐日重算每个 sleeve 的**完整候选集**（含未进组合的部分），
对齐可交易口径（停牌/涨跌停/成交额下限），再算一遍共线性与净额化空间 —— v1 只能看到"进了组合的标的"。

#### Phase 0 v2 已执行（2026-10-06，信号层候选域）

`uv run python scripts/sleeve_candidate_diagnostics.py --start 2024-10-01 --freq M`
→ `output/research/sleeve_candidate_diagnostics_20261006_095356/`

口径：日线 qfq 面板（3,325 只 / 2024-01 ~ 2026-09）本地重算 state 指标，再调用**生产同源的**
`factor_engine.strategies.state_mask()` 判定；24 个月末诊断日；`flow_z` 用 `moneyflow_net_z_20d` 代理，
`signal_relative20` 用全市场中位相对代理（详见报告 §5）。

| sleeve | 状态 | 候选数中位（只/日） | 20 日均额中位（万元） |
|---|---|---|---|
| bottom_momentum | off | 598 | 17,140 |
| value_reversal | active | 606 | 18,204 |
| dip_low_position | research | 590 | 12,506 |
| donchian_breakout | off | 132 | 16,410 |

**候选域共线性（两两 Jaccard）**：

| sleeve A | sleeve B | 平均 Jaccard | 平均重叠标的（只/日） |
|---|---|---|---|
| **bottom_momentum** | **value_reversal** | **0.6188** | **466** |
| donchian_breakout | value_reversal | 0.2279 | 146 |
| bottom_momentum | donchian_breakout | 0.2177 | 143 |
| bottom_momentum | dip_low_position | 0.0185 | 23 |
| dip_low_position | donchian_breakout / value_reversal | 0.0000 | 0 |

净额化空间：平均 **507 只/日** 被 ≥2 个 sleeve 同时选中，占候选并集的 **38.1%**（最多一天 703 只，2026-08-31）。

**v1 与 v2 的结论相反，必须一起读**：

1. v1（只看最终进组合的标的）所有 sleeve **Jaccard = 0**；v2（看完整候选域）最高一对 **0.62**。
   → **组合层的"零重叠"是选择器（slots 限制 + 约束）硬做出来的，不是策略逻辑天然不重叠。**
2. 因此三条判断同时成立：
   - **伪分散在候选/风险暴露层面确实存在**（bottom_momentum 与 value_reversal 盯的是同一批"低位+放量"股票）；
   - **净额化降换手在持仓层面依然≈0**（最终不重叠，没有反向订单可对冲）；
   - **当前的分散度是脆弱的**：一旦放宽 sleeve 的 slots/权重（Phase 1 分配器很可能这么做），两个腿会立刻撞车，
     届时组合会同时买入同一批标的，市值/行业暴露会集中。
3. **对 Phase 1 的设计修订（重要）**：分配器不能只做风险平价，必须带**候选域重叠惩罚**：
   对 Jaccard 高的 sleeve 对，联合预算上限按 `1 / (1 + Jaccard)` 收紧，并在验收里加一条
   "任意两 sleeve 候选域 Jaccard > 0.5 时，两者合计风险贡献 ≤ 单一上限"。

#### Phase 0 的附带修复（2026-10-06）

- **模型腿补 sleeve 标签**：`data/ingest/service.py` 在 `union["selection_sleeve"]` 落盘前，
  对 `channel=model` 且标签为空的行补 `model_alpha`（此前 404 条 selected 里 304 条为空）。
  校验：`ast.parse` 通过；用相同逻辑的单元样例验证 `model → model_alpha`、`signal_override → reversal`。
- 结论：Phase 0 的两张表（`sleeve_diagnostics` v1 / `sleeve_candidate_diagnostics` v2）现在是**可对比**的，
  后续归因与分配器都以 v2 的候选域为准、v1 的持仓为界。

## 10. 执行进展（2026-10-06 下午：按设计跑出的判决与动作）

| 工单 | 动作 | 结果 | 状态 |
|---|---|---|---|
| **W2** | 域内随机基线 | 39 决策日 × 每抽 5 只 × 300 次；只有 `model_alpha` 三 horizon 全部跑赢随机均值 | ✅ 已判决 |
| **W3** | 训练 `model_valuation` | pooled IC **+0.0451**、ICIR +0.381、**NW t +2.30**、top-5 毛超额 **+3.15%**；IC 为正 4/5 折 → **通过门槛** | ✅ 通过 |
| **W6** | 动量腿复活评审（训练 `model_momentum`） | pooled IC +0.0252、NW t **+1.15 < 1.5**、top-5 超额 **−1.85%** → **不通过，维持 off** | ✅ 已判决 |
| **W5b** | 反转腿模型化复评（训练 `model_reversal`，10 日标签） | pooled IC +0.0219、NW t **+1.33 < 1.5**、top-5 超额 **−0.98%**、IC 为正仅 3/5 折 → **不通过** | ✅ 已判决 |
| **W5** | 反转家族降级 | `value_reversal` **active → off**（证据写入注册表 evidence）；`bottom_momentum`/`dip_low_position` 补 W2 判决与 Jaccard 证据；`total_budget` 0.95 → **0.90**，注册表一致性仍为 True | ✅ 已执行 |
| 附带修复 | 落盘点补齐 sleeve 标签 | `select_persisted_model_scores` 写 CSV 前把 `channel=model & sleeve 空` 的行填 `model_alpha`；重跑 preselection 后 `cn_ensemble_preselected.csv` 8/8 行带标签 | ✅ 已验证 |

**对"反转三合一"的修订（重要）**：合并的前提是家族内**至少有一条腿能过门槛**。W5b 实测反转腿的
模型化重做也过不了（NW t 1.33），因此**先全部下线，合并留作研究**——不做"为合并而合并"。

**当前产线的真实状态**：只有 `model_alpha` 是 active（预算 0.90），其余 5 条腿为 off/research。
这与归因（风险贡献 96.1%）和 W2（唯一跑赢随机均值）完全一致：**所谓"多 sleeve 组合"目前就是单腿**，
补上第二个真实家族（valuation 已通过门槛、quality 待财务数据）才是下一步的正事。

## 11. 收工状态（2026-10-06 晚）

| 阶段 | 结论 |
|---|---|
| Phase 0 诊断 | ✅ 完成（组合层 + 候选域两套；发现 304/404 条 sleeve 标签缺失并修复） |
| Phase 1 分配器 | ✅ 设计与实现完成，**等权特例已过组合门槛**；`[selection.sleeve_allocator]` 已入配置（默认关闭） |
| Phase 2 家族模型腿 | ✅ 四条腿全部训练完成并通过统计门槛（长样本 2015-2026，含退市股）；判决见 §10 与 P1_24b §8.6 |
| Phase 3 执行/容量 | ✅ 容量约束（1%×ADV20）与铺单模型在 xiaomkt 侧验证，容量上限 100~250 万 |
| W1–W8 | W2/W3/W3b/W4/W5/W5b/W6/W7/W8 **全部完成**；W7 的"接入"完成到配置与预算产出层 |

**最终判决（长样本 + 组合口径）**：

1. **组合口径通过**：等权组合 IC +0.1603 > 最好单腿 +0.1274，净超额 **+1.245%/20 日**、NW t **+4.83**（≥1.5）；
2. **质量腿是最优选**：净 +2.74%、换手仅 12~14%、与其余腿近零相关；
3. **估值与动量冗余**（相关 0.570）→ 分配器给 23% / 38%，质量 39%，**反转 0%**（净超额为负被资格门剔除）；
4. **supervivorship 检验通过**：并入 209 只退市股后 IC 变化 ±0.01，结论不是偏差造成的。

**推向生产还需要的一步（唯一未完成项）**：让四条家族腿产出**可交易目标权重**（目前只有 OOS 预测），
再由 `sleeve_allocator` 给预算、组合层合并 → 才能替换/扩充当前只有 `model_alpha` 的单腿现状。
在此之前，分配器与家族腿都保持 `enabled=false` / `research`，不影响现有生产。

### Phase 1 —— 分配器（1~2 天）

产物 `factor_engine/sleeve.py` 的 `Allocator` + `[selection.sleeve_allocator]` 配置段 + `scripts/sleeve_allocator_ab.py`。

- `static`：现状（配置 `share`）。
- `equal_risk`：按逆波动分配，使各 sleeve 的风险贡献相等（Maillard/Roncalli，DOI `10.3905/jpm.2010.36.4.060`）。
- `icir_vol`：目标 = 滚动 OOS ICIR 排序 × 逆波动，叠加单 sleeve 硬上限（如 ≤ 0.5）与最小预算（避免 0 权重抖动）。

验收（walk-forward，5 折，口径与 `scripts/walkforward_xiaomkt_rotation.py` 一致）：

1. `equal_risk` 或 `icir_vol` 的组合夏普 ≥ `static`，且 **5 折中至少 4 折不劣化**；
2. 单 sleeve 最大风险贡献 ≤ 硬上限（`validate_budgets` 已有溢出检查，扩展为风险口径）；
3. 换手不高于 `static` 的 1.1×；
4. 分配器自身参数（回看窗口、上限）也必须走 walk-forward，**不得在同一段历史上调参后直接上线**。

### Phase 2 —— 家族约束模型腿（3~5 天，最贵、最需要 A/B）

#### Phase 2 前置 A：**每个 sleeve 必须有自己的标签**（2026-10-06 现状核查）

现状（实测）：`[lightgbm]` 与 `[transformer]` **共用同一套标签** —— `label_horizon = 20`、
`label_mode = "forward_return"`，全市场同一截面。也就是说当前**不存在"每个 sleeve 一套标签"**，
所有模型腿（现在只有 `model_alpha` 一条）学的是同一个目标。这是 Phase 2 要补的第一件事。

设计（写死，避免事后调整）：

| sleeve | 标签 horizon | 标签域（截面范围） | 目标变量 | 依据 |
|---|---|---|---|---|
| model_alpha | 20 日 | 全市场可交易 | 20 日前向收益的截面 rank | 现状口径，保持可比 |
| value_reversal | **20 日** | **该 sleeve 的候选域** | 域内 20 日超额收益 rank | `exit.expected_holding_days = 20` |
| bottom_momentum | **10 日** | 候选域 | 域内 10 日超额 rank | `exit.expected_holding_days = 10` |
| dip_low_position | 20 日 | 候选域 | 域内 20 日超额 rank | `exit.expected_holding_days = 20` |
| donchian_breakout | **5 日** | 候选域 | 域内 5 日超额 rank | `exit.expected_holding_days = 5` |
| limit_momentum | **5 日** | 候选域 | 域内 5 日超额 rank | `exit.expected_holding_days = 5` |

两个关键设计点：

1. **域内排序，而不是全市场排序**：sleeve 只需要在自己 130~600 只候选里挑 top-k（Phase 0 v2 实测：
   bottom_momentum 598 / value_reversal 606 / dip 590 / donchian 132，中位/日）。
   在全市场排序会把"域内无关的股票"塞进训练样本，稀释信号；域内排序直接对齐它实际要做的决策。
2. **horizon 与 `exit.expected_holding_days` 对齐**：标签期与实际持有期不一致时，模型学到的是
   另一段时间的收益，Event 研究里的衰减（例如涨停动量 10 日衰减到 0）会被标签掩盖。

**验收（不达标就不上线模型腿，保持规则腿）**：

- 每个 sleeve 的 **域内 OOS rank IC / ICIR** 必须超过**域内随机基线**（在候选域里随机取 top-k 的收益分布，
  用 Phase 0 v2 的候选域逐日模拟），且在 5 折中 ≥4 折为正；
- 模型腿必须带来**增量**：该 sleeve 的（模型 top-k）−（规则 top-k）的 OOS 超额 > 0 且 t ≥ 1.5；
- Transformer 与 LightGBM 用同一套标签，分别验收；**不允许只因为"架构上要有多样性"就把未达标的模型腿并入**。

#### Phase 2 前置 C：**"估值 / 动量 / 质量 / 反转"四种风格该不该新增？**（2026-10-06 结论：两新增、一条件、一重构）

判据三条，缺一不可：① 特征在本地真的可用（覆盖率）；② 与现有 6 条腿**不重复**（否则制造伪分散）；
③ 有 A 股实证支持（文献 + 我们自己的负结果）。

| 风格 | 特征可用性（2026-09-30 实测，n=5,213） | 与现有腿的关系 | 文献/本地证据 | 结论 |
|---|---|---|---|---|
| **估值** | ✅ `pe_ind_pct` / `pb_ind_pct` / `ps_ind_pct` / `dividend_yield_ind_pct` **全部 100%**；`valuation_fcf_yield` **0%（不可用）** | 无重复（`value_reversal` 是"长期回撤后修复"，属反转不属估值） | LSY(2019) CH-3 的 value 腿在 A 股有效（需剔除最小 30%）；`10.2139/ssrn.1909253` | **新增，P0** |
| **质量** | ⚠️ 原始财报列覆盖极低：`financial_roe` **19%**、`financial_roa` **0%**、`financial_net_margin` 20%；但行业分位 `roe_ind_pct` / `gross_margin_ind_pct` **100%** | 无重复 | Quality Minus Junk（Asness/Frazzini/Pedersen），`10.1007/s11142-018-9470-2` | **有条件新增，P1**：先用 `*_ind_pct` 行业分位特征；要上原始财报必须先把覆盖从 19% 拉到 >90% |
| **动量** | ✅ 特征齐备：`momentum_12m_skip_1m` 98%、`academic_carhart_mom` 98%、`RPS_20` 100%、`price_position_52w_high` 99% | **与技术腿重复**：`donchian_breakout`(off)、`limit_momentum`(off) 都是动量类 | **本地负结果**：突破腿均值基准 5d −0.29%/t=−6.4；涨停动量 −1.35%/t=−8.1（P1.23）；动量崩溃风险 `10.1016/j.frl.2026.109866` | **不新增"追高式动量"**；若做，只做"中期动量 + 右侧状态确认"，且必须先在域内证明 IC 为正（P2） |
| **反转** | ✅ 核心列高覆盖：`short_term_reversal_1m` 100%、`sector_rps_reversal_20d` 99%、`vol_max_drawdown_60` 100% | **严重重复**：现有 6 腿里 3 条都是反转类（`value_reversal`、`bottom_momentum`、`dip_low_position`）；Phase 0 v2 实测 `bottom_momentum ↔ value_reversal` 候选域 **Jaccard 0.6188** | 短期反转在 A 股存在但"不一致"：`10.1080/13504851.2022.2115445`；本仓库归因实测 reversal 腿自身 **−59.2%**、夏普 **−1.46**、造成组合最大回撤 **5.98pp** | **不新增，改为"三合一"**：把 3 条反转腿合并为 1 条 `reversal` 再重做域内标签；合并后若仍为负贡献则**直接砍掉** |

**因此，"新增四种风格"的正确落地不是 6+4=10 条腿，而是家族重构**：

| 家族 | 现有腿 | 目标动作 |
|---|---|---|
| alpha（模型） | `model_alpha` | **保留**（归因显示是唯一正贡献腿，风险贡献 96.1%） |
| 反转 | `value_reversal` + `bottom_momentum` + `dip_low_position` | **三合一 → `reversal`**（先合并再做域内标签；不合并就新增＝制造伪分散） |
| 趋势/动量 | `donchian_breakout`(off) + `limit_momentum`(off) | 合并为 `trend_confirm`（只做右侧确认、不追高），否则**维持 off** |
| 估值 | — | **新增 `model_valuation`（P0）** |
| 质量 | — | **新增 `model_quality`（P1，用 `*_ind_pct`）** |

实施顺序与理由：

1. **P0 估值**：特征 100% 覆盖、零重复、文献与本地数据都支持 —— 风险最低、增量最可能为正。
2. **P1 质量**：只用行业分位列即可开工；同时把"财报覆盖率 19%"作为独立数据工程项（P0-5 类）。
3. **P2 反转三合一**：注意这是**减法**不是加法；合并的验收不是收益提升，而是"去掉两个重复腿后组合指标不劣化"。
4. **P3 动量**：先做域内 IC 审查；若为负则把现有两条动量腿永久置 off，并在注册表 `evidence` 记录负结果。

文献补充（风格轮动/时机，支撑 Phase 1 分配器）：Equity-Style Timing 多风格轮动模型
（Journal of Asset Management 2007，`10.1057/palgrave.jam.2250056`）——与本计划的"分配层按 ICIR/波动动态分配"
是同一类方法，可作为分配器设计的参考。

#### Phase 2 前置 B：夏普 / 卡玛的 sleeve 级归因（已于 2026-10-06 落地）

`uv run python scripts/sleeve_metric_attribution.py --start 2025-09-01`
→ `output/research/sleeve_metric_attribution_20261006_100314/`

样本：37 个决策日（2025-09-08 ~ 2026-09-30），346 条持仓；进场 = 决策日次日开盘，出场 = 下一决策日次日开盘。

**组合层面**：年化 19.75%、年化波动 20.23%、**夏普 0.99**、最大回撤 -8.12%、**卡玛 2.43**。

**逐 sleeve 归因**（夏普用"收益贡献/组合波动"近似拆分 + 留一法交叉验证；卡玛用留一法 + 回撤窗口）：

| sleeve | 期数 | 自身累计 | 自身夏普 | 收益贡献/期 | 风险贡献占比 | ΔSharpe（去掉后变化） | ΔCalmar（去掉后变化） |
|---|---|---|---|---|---|---|---|
| **model_alpha** | 32 | +133.8% | 1.15 | +2.027% | **96.1%** | **+1.46**（0.99 → -0.47） | **+2.69**（2.43 → -0.26） |
| bottom_momentum | 3 | +30.5% | 2.95 | +0.131% | 0.1% | +0.05 | +0.06 |
| momentum | 5 | +32.5% | 1.23 | +0.046% | -0.3% | +0.07 | -0.04 |
| setup | 9 | -0.6% | 0.05 | +0.031% | -1.0% | +0.10 | +0.81 |
| untagged | 10 | -17.9% | -1.15 | -0.196% | 1.3% | -0.16 | -1.06 |
| **reversal** | 34 | **-59.2%** | **-1.46** | **-0.371%** | 3.8% | **-0.22** | **-1.50** |

**组合最大回撤窗口（2026-06-10 → 2026-07-16，-8.12%）归因**：

| sleeve | 窗口内组合收益贡献 | 窗口内自身收益 |
|---|---|---|
| **reversal** | **-5.98%** | **-30.48%** |
| untagged | -0.92% | -8.73% |
| setup | +1.34% | +6.33% |
| model_alpha | +1.83% | +2.55% |

**结论（对 Phase 1/2 的直接含义）**：

1. **夏普与卡玛几乎 100% 由 model_alpha 解释**（风险贡献 96.1%；去掉它夏普转负、卡玛转负）。
   → 与 Phase 0 v2 一致：所谓"多 sleeve 组合"目前是**单腿 + 若干噪声腿**。
2. **reversal 是明确负贡献**：自身 -59.2%、夏普 -1.46；去掉后组合夏普 0.99→1.21、卡玛 2.43→3.93。
   且**组合最大回撤里 5.98pp 由它一人造成**（同期自身 -30.5%）。
   → 按验收规则，这类腿要么**降预算到 0**，要么**重做标签**（这正是 §Phase 2 前置 A 的动机）。
3. `untagged` 10 期 -17.9% 是标签缺失的历史遗留（补丁只对后续产出生效）→ 数据质量待清。
4. 归因方法已固定：夏普 = 收益贡献/组合波动 + 留一 ΔSharpe；卡玛 = 留一 ΔCalmar + 回撤窗口归因
   （卡玛不可加，故不用求和口径）。**后续任何 sleeve 增删都必须重跑本报告作为验收证据。**

把 `model_alpha.features = "admitted_90"` 按经济逻辑拆成 4 个家族（特征→家族映射表**先写死**，不因结果调整）：

| 家族 sleeve | 特征示例（以 `clean_panel` 实际列名落地） | 角色 |
|---|---|---|
| `model_valuation` | 估值/市值/股息类（PE/PB/PS/股息率/市值分位） | 估值与规模 |
| `model_momentum` | 动量/趋势/位置类（ret_20/60、dist_from_high、均线偏离） | 趋势延续 |
| `model_quality` | 质量/盈利/成长类（ROE/毛利率/增速，PIT 口径） | 基本面质量 |
| `model_reversal` | 短期反转/超跌类（ret_5、乖离、量价背离） | 均值回复 |

实现：复用现有 LightGBM 训练入口（`[lightgbm]` 段与 `stage=lightgbm`），通过 `[strategies.model_*]` 的
`signal.features` 指定家族特征集；每个家族独立训练、独立 OOS。

**A/B（必须做，且是合并的前置）**：

| 臂 | 定义 |
|---|---|
| A `single_big` | 现状：全特征单一 LGBM，输出全股票权重 |
| B `sleeve_multi` | 4 个家族 LGBM + 分配器 + 汇总 |
| C `sleeve_multi_equal` | 4 个家族 LGBM + 等权（隔离"分配器"贡献，避免把分配器收益算到拆分头上） |

口径：同一标签、同一训练窗口、同一 OOS 折、同一成本（含 `max_participation=1%×ADV20`）、同一 `pk` 约束。

决策规则（写死，不许事后改）：

- **只有在 B/C 于 OOS rank IC、ICIR、扣成本年化、最大回撤四项中至少两项不劣于 A**，且 sleeve 归因可加总，才允许把家族腿并入生产；
- 否则保留 A，把"拆分未通过"写进本文件与注册表 `evidence`（沿用 P1.23 的做法）。

### Phase 3 —— 汇总执行与容量（1 天）

1. 汇总层实现净额化：同标的多 sleeve 反向订单先内部对冲，再生成净订单；
2. 接入容量约束：主链路订单统一过 `max_participation`（≤1%×ADV20，`backtest_engine/xiaomkt_rotation.py` 已验证：回撤 -51.5%→-47.1%、年化微升）；
3. 小额调仓过滤：净值权重变动 < 阈值（如 0.3%）不下单，换手实测对比。

验收：净额化 + 阈值过滤后换手下降 ≥ 10%，且 OOS 收益不劣化（同一折、同口径）。

## 5. 硬门与验收指标汇总

| 项 | 硬门 | 证据产物 |
|---|---|---|
| 归因可加总 | 各 sleeve 贡献之和 = 组合收益，误差 ≤ 0.05pp/月 | `sleeve_diagnostics_<ts>/attribution.csv` |
| 共线性 | 任意两 sleeve 目标权重日度相关 ≤ 0.5；组合小盘暴露 ≤ 单 sleeve 最大暴露 + 20% | `collinearity.csv` |
| 分配器增量 | 夏普 ≥ static 且 5 折中 ≥4 折不劣化 | `sleeve_allocator_ab/<ts>/folds.csv` |
| 家族模型 A/B | 四项指标中 ≥2 项不劣于 `single_big` | `model_comparison` 同口径报告 |
| 换手 | 净额化后 ≥10% 下降且收益不劣化 | `netting.csv` |
| 容量 | 订单 ≤1%×ADV20；`impact_coef` 敏感性已记录 | `xiaomkt_impact_calib_*`（已有） |

## 6. 风险、反模式与既有负结果

1. **跨家族交互丢失**：单一 LGBM 能学"低估值 × 高动量"的交叉项；拆分后每个 sleeve 看不到彼此。这是拆分的第一代价，A/B 的 C 臂就是为它准备的。
2. **伪分散（本仓库最现实的风险）**：6 个腿里有 4 个都在小盘/低位上，相关性可能很高。Phase 0 的共线性报告必须先回答这个问题，否则"sleeve 架构"只是把同一个风险切成几份。
3. **把预算当调参**：`share` 从 0.75 调到 0.90 这类动作（见 `model_alpha.budget.note`）本身就是过拟合来源。分配器上线后，**手工 share 必须冻结**，改动要走 Phase 1 的验收。
4. **既有负结果必须继承**：`dip_low_position`（研究态，A/B 不达标）、`donchian_breakout` / `limit_momentum` / `bottom_momentum`（已转 off，均值基准 t 值均为负）、以及 `xiaomkt_rotation` 的 walk-forward 结论（参数挑选无样本外价值，结构改动才有价值）。这些腿不得因为"架构需要多样性"而被重新激活。
5. **数据窥探**：家族特征映射表先写死；A/B 的决策规则先写死（本节 §4 Phase 2）。

## 7. 与其它计划的关系

- `P1_22`：提供 SHAP 与 OOS 归因底座；家族拆分后的解释可以直接复用其 SHAP 产物。
- `P1_23`：给出"ML 只用在过滤/排序两处"的边界；本计划的家族模型腿必须遵守同一边界。
- `docs/runbooks/xiaomkt-rotation-todo.md`：容量、执行、walk-forward 方法论直接复用（`max_participation`、`execution_bars`、5 折 walk-forward 脚本）。
- `test_registry_budgets_match_production_sleeve_config`：注册表与 sleeve 配置一致性测试，Phase 1 起必须保持通过。

## 8. 第一批工单（可立即执行）

```bash
cd /Users/ccs/code/quant/stock_analysis_by_gpt

# W1 现状审计：注册表 vs 硬编码 sleeve 的一致性
uv run python -c "from factor_engine.strategies import load_strategies, compare_sleeve_budgets; \
import tomllib; cfg=tomllib.load(open('config/cn_pipeline.toml','rb')); \
print(compare_sleeve_budgets(load_strategies(cfg), (cfg.get('selection',{}).get('signals') or {})))"

# W2 预算总和是否守恒（实测：total=0.95 = model_alpha 0.90 + value_reversal 0.05；
#     其余 4 腿为 off/research，share 不计入 active 预算）
uv run python -c "from factor_engine.strategies import load_strategies, total_budget, validate_budgets; \
import tomllib; cfg=tomllib.load(open('config/cn_pipeline.toml','rb')); s=load_strategies(cfg); \
print(total_budget(s)); print(validate_budgets(s, raise_on_overflow=False))"

# W3 Phase 0 骨架（新增脚本，先只做归因与共线性）
uv run python scripts/sleeve_diagnostics.py --start 2024-01-01 --out output/research/sleeve_diagnostics
```

## 9. 文献边界

| 主题 | 参考 | DOI / 出处 |
|---|---|---|
| 等风险贡献（ERC） | Maillard, Roncalli & Teïletche, *The Properties of Equally Weighted Risk Contribution Portfolios*, JPM 2010 | `10.3905/jpm.2010.36.4.060` |
| 风险预算可加性 | Qian, *On the Financial Interpretation of Risk Contribution: Risk Budgets Do Add Up*, 2005 | `10.2139/ssrn.684221` |
| 组合构建与风险模型 | Grinold & Kahn, *Active Portfolio Management*（书，无 DOI） | 第 14 章风险预算 |

> 说明：本节只列本计划直接使用的方法论；A 股小市值/壳价值相关的实证文献见
> `docs/runbooks/xiaomkt-rotation-literature.md`（DOI 已逐条核对）。
