# P0_14 周线形态信号实施计划（三星会师 / 二重炮三破顶 / 收盘破前三周 / 双龙抬头 / 堆量回踩五周线）

- 状态：待实施（先验证，再接入）
- 提出日期：2026-09-13
- 上游：`P0_13`（日线 Donchian/动量信号 + 选股接入 + 卖出规则），本文复用其事件研究方法与接入点
- 相关：`docs/runbooks/cn-data-pipeline.md`、`factor_engine/signals/`、`factor_engine/ml/panel_dataset.py`

---

## 0. 把口语规则翻译成可计算定义

所有规则都以**周线（W-FRI，当周最后一个交易日收盘）**为准，参数写在配置里，默认值如下。

| # | 名称 | 判定条件（草案默认值） | 语义 |
|---|---|---|---|
| 1 | **底部横盘很久** | 过去 `base_weeks=20` 周收盘极差 ≤ `base_range_max=15%`；收盘位于 52 周区间的下 `lower_third=1/3`；MA20 周线斜率 `|slope| ≤ slope_flat=0.5%`/周 | 长期横盘基底 |
| 2 | **三星会师** | 连续 3 周的（最高−最低）与实体都收敛：3 周收盘极差 ≤ `star_close_range=3%`，且 3 周高低点区间 ≤ `star_range_max=6%`，同时与前一周的 MA5 有重叠（`min(high3) ≥ MA5*0.98` 或 `max(low3) ≤ MA5*1.02`）；需要 1 成立 | 趋势拐点 |
| 3 | **二重炮** | 连续 2 周阳线，实体 ≥ `cannon_body=2%`，量 ≥ 前 `vol_lookback=5` 周均量 × `cannon_vol=1.5`；第 2 周收盘 > MA5 | 启动 |
| 4 | **三破顶（破前四周）** | 第 3 周最高价 > 前 4 周最高价（`close_break_prev`：收盘价突破前 3 周收盘最高价，两种口径分别记录） | 突破确认 |
| 5 | **双龙抬头** | MA5 上穿 MA20（前一周 MA5 ≤ MA20 且本周 > ），且两条均线斜率同时为正；收盘同时站上 MA5 与 MA20 | 均线拐点 |
| 6 | **周线堆量** | 连续 `pile_weeks=3` 周量递增，且每周量 ≥ 20 周均量 × `pile_ratio=1.2` | 资金进场 |
| 7 | **回踩不破五周线** | 堆量后 1-3 周内出现阴线周，其 `low ≥ MA5*(1-tol_break=1%)` 且收盘 ≥ MA5 | 上车机会 |

信号输出**不只是 0/1**：每个形态同时输出"强度"连续值（例如三星会师的收敛度、二重炮的量比、回踩深度），便于直接作为模型特征。

---

## 1. 现状与差距

| 层 | 现状 | 差距 |
|---|---|---|
| 数据 | `assets/data/clean/ohlcv` 只有 `daily` 与 5/15/30/60min | **没有周线**，需要新增聚合层 |
| 信号 | `factor_engine/signals/` 已有 `donchian_pullback` / `range_breakout` / `limit_momentum`（全为日线） | 需要周线 recipe |
| 特征 | clean panel 574 个特征全为日频；`panel_dataset._derive_price_features` 从日线派生 | 周线状态需要**对齐到日频**后才能进模型 |
| 模型 | LightGBM 用 1068 列；Transformer/CNN 从 550 个 clean 特征里按 `|IC|×覆盖率×log方差` 选 128 对，`lookback=60` 个交易日 ≈ **12 周** | 周线特征要么进特征池，要么单独分支（见第 3 节） |
| 标签 | `forward_return_20d`（20 个交易日 ≈ 4 周） | 与周线形态的持有期基本匹配 |

---

## 2. 实施方案（5 步）

### Step 1 周线聚合层（新增）
`factor_engine/expressions/weekly.py`

```python
def resample_weekly(bars: pd.DataFrame) -> pd.DataFrame:
    """日线 -> 周线（W-FRI）。trade_date 取该周最后一个交易日，用于 PIT 对齐。"""
    # open=周首开; high=max; low=min; close=周末收; volume/amount=sum
    # 停牌周：保留该周但不产生 bar（或标记 is_suspended_week=True）
```

要点：
- **PIT 语义**：周线 bar 的 `available_at` = 本周最后一个交易日收盘后；面板里只能用 `trade_date >= available_at` 的行。
- 复权口径沿用日线（qfq）；跨除权周需要用日线复权后价格聚合，不能先用未复权价再聚。
- 单元测试：手工构造 3 周数据，校验 open/high/low/close/volume 与 `trade_date` 归属。

### Step 2 周线指标（新增）
MA5/MA10/MA20/MA30 周线、周量均线（5/20）、周 MACD、周 RSI、周 ATR、52 周高低位置、MA 斜率、连续上涨/下跌周数、周线振幅。
放在 `factor_engine/expressions/weekly.py` 内，纯函数、无状态、可单测。

### Step 3 周线形态 recipe（新增）
`factor_engine/signals/weekly_patterns.py`，注册 recipe 名 `weekly_patterns`，内部按第 0 节的 7 条判定，
输出结构（沿用现有 `SignalRecipeResult` 约定）：

```
setup_type ∈ {star_convergence, double_cannon_breakout, dragon_cross, pile_pullback, neutral}
setup_score (0-100)
features: {
  base_weeks, base_range, base_pos_52w, ma5_week, ma20_week, ma5_slope, ma20_slope,
  star_weeks, star_close_range, star_range, star_score,
  cannon_weeks, cannon_body, cannon_vol_ratio, break_prev4_high, break_prev3_close,
  dragon_cross, dragon_gap, pile_weeks, pile_ratio, pullback_weeks, pullback_depth,
  pullback_hold_ma5, vol_streak, ...
}
```

### Step 4 两处接入（互相独立，可分别开关）

**(a) 选股侧**：`[selection.signals]` 增加周线 recipe，作为**第三个 sleeve**（与 setup / momentum 并列），
独立名额与权重预算：

```toml
[selection.signals.weekly_sleeve]
enabled = true
setup_types = ["star_convergence", "double_cannon_breakout", "dragon_cross", "pile_pullback"]
slots = 2
min_weight = 0.05
max_weight = 0.15
timeframe = "weekly"       # 扫描时用周线 bar 评估
```

**(b) 特征侧**：把周线状态列（约 15-20 个）加入 clean panel：
- 在 `panel_dataset._derive_price_features` 之外新增 `_attach_weekly_features(panel, bars)`；
- 对齐规则：周线值在**该周最后一个交易日**可见，其后一周内 forward-fill（带上限 `max_carry_weeks=1`），更早的行保持 NaN（否则就是前视）；
- 列名统一 `wk_*`（例如 `wk_star_score`、`wk_ma5_slope`、`wk_pullback_hold_ma5`），进面板后自动变成模型的 `wk_*_clean` + `_is_missing`。

### Step 5 先验证再上线（沿用 P0_13 的方法）
用事件研究脚本（`output/verification/donchian_exits/exit_rule_simulation.py` 同款框架）逐个形态测：

| 指标 | 通过门槛（草案） |
|---|---|
| 未来 20 日超额收益 | > 0.5% |
| t 值 | > 3 |
| 分年度（2024/2025/2026） | 至少 2 年同号 |
| 按 regime 分层 | 至少 bull + sideways 非负 |
| 与现有信号的重叠率 | 与 donchian_breakout 命中重叠 < 60%（否则只是重复买同一个东西） |

**未通过这些门槛的形态不接入选股**（这是 P0_13 的教训：日线"突破→回踩"在 2024-2026 样本上是负期望，
但当时已经先接入了；周线形态必须先验证）。

---

## 3. 要不要放进 Transformer？——分层回答

**结论：先做"日频面板 + 周线状态特征"，不要单独训周线模型。**

1. **必须先进 clean panel，而不是直接进 Transformer**
   训练层是特征无关的（`feature_columns = 面板中所有 *_clean / *_is_missing`），所以周线状态一旦进面板，
   LightGBM 自动就用（1068 → ~1100 列）；Transformer/CNN 则会按 `|IC| × 覆盖率 × log(方差)`
   从 550 个 clean 特征里选 **128 对**——周线特征必须自己够强才会被选中
   （实测 Donchian 的 `break_count_20` / `since_break_20` 就是被自动选中的）。

2. **不建议单独训周线模型**
   样本量按周只有日频的 1/5；Transformer 的 `max_samples=12000` 在周频下会覆盖更少股票/更短历史，
   过拟合风险上升，而且和现有日频模型无法直接比较。

3. **推荐的三个阶段**
   | 阶段 | 做法 | 成本 | 风险 |
   |---|---|---|---|
   | 一 | 周线状态进日频面板 → 重训 LightGBM/Transformer | 低（改 `panel_dataset` + 重跑 clean_panel/训练） | 低 |
   | 二 | 若阶段一的 OOS IC 无提升，再考虑**双分支**（日线序列 + 周线序列共享头部） | 中高（需改模型结构 + OOS 对照） | 中 |
   | 三 | 独立周频模型（仅当选股节奏改为周频且样本足够） | 高 | 高 |

   验收以 `--stage oos_predictions` 的样本外 IC/ICIR 为准，不以单次回测收益为准。

---

## 4. 配置草案

```toml
[weekly_features]
enabled = true
rule = "W-FRI"              # A 股以周五为周收盘
min_bars_per_week = 3       # 少于 3 个交易日视为不完整周
max_carry_weeks = 1         # 周线值向后携带的上限（PIT）
include = ["wk_ma5", "wk_ma20", "wk_ma5_slope", "wk_ma20_slope", "wk_star_score",
           "wk_cannon_vol_ratio", "wk_break_prev4_high", "wk_dragon_cross",
           "wk_pile_ratio", "wk_pullback_hold_ma5", "wk_base_range", "wk_pos_52w"]

[signals.weekly_patterns]
base_weeks = 20
base_range_max = 0.15
slope_flat = 0.005
star_close_range = 0.03
star_range_max = 0.06
cannon_body = 0.02
cannon_vol = 1.5
vol_lookback = 5
pile_weeks = 3
pile_ratio = 1.2
tol_break = 0.01
min_score = 60.0
```

---

## 5. 验收标准

1. **数据正确性**：周线聚合与手工核算一致；`available_at` 全部 ≤ 使用该值的交易日（无前视）；
2. **信号正确性**：7 条判定各有单测（构造周线数据 + 期望判定结果）；
3. **有效性**：第 5 节事件研究表逐项通过（超额收益/t/分年度/regime/重叠率）；
4. **集成**：`--stage selection` 报告中出现 `weekly_sleeve` 命中与强制名单；`clean_panel` manifest 中出现 `wk_*` 列；
5. **回归**：现有 78 个测试全绿；若周线 sleeve 未启用，输出与当前逐行一致。

---

## 6. 风险与注意事项

| 风险 | 说明 | 处置 |
|---|---|---|
| **前视偏差** | 周线 bar 必须"当周收盘后"才可用 | `available_at` 硬约束 + 单测断言 |
| **停牌/涨跌停** | 停牌周缺 bar，涨跌停会让"实体/放量"失真 | 聚合时标记 `is_suspended_week`，形态判定要求连续周存在 |
| **样本量** | 单票周线事件太少（一年 ~50 个观测） | 只做横截面统计，不做单票时间序列优化 |
| **共线性** | 双龙抬头/二重炮与日线 donchian 突破高度重叠 | 强制做重叠率检查，重叠 >60% 的形态不单独占名额 |
| **regime 依赖** | 2024-2026 是反转市，突破类在此样本为负期望 | 分 regime 验证；`sideways` 下默认不启用突破型周线形态 |
| **参数过拟合** | 7 个形态 × 若干阈值容易过拟合 | 每类形态只允许 2-3 个自由参数；在 OOS 折上验证 |

---

## 7. 实施清单（工作量估计）

| # | 任务 | 产出 | 估计 |
|---|---|---|---|
| 1 | 周线聚合 + 指标 | `factor_engine/expressions/weekly.py` + 单测 | 0.5 天 |
| 2 | 周线形态 recipe | `factor_engine/signals/weekly_patterns.py` + 单测 | 1 天 |
| 3 | 事件研究验证 | `output/verification/weekly_patterns/*.csv` + 结论 | 0.5 天 |
| 4 | 选股侧 weekly sleeve | `[selection.signals.weekly_sleeve]` + 服务接入 | 0.5 天 |
| 5 | 面板周线特征 | `panel_dataset._attach_weekly_features` + clean_panel 重建 | 0.5 天 |
| 6 | 重训 + OOS 对照 | LightGBM/Transformer 重训 + `--stage oos_predictions` 对照 | 1 天 |
| 7 | 文档与验证产物 | runbook + VERIFICATION + ROLLBACK | 0.5 天 |
| | 合计 | | **约 4.5 天** |

**建议顺序**：1 → 2 → 3（验证）→ 4 或 5（先选一个接入）→ 6 → 7。
若第 3 步任一形态未通过门槛，就只做特征不做选股名额（避免重复 P0_13 的教训）。

---

## 8. 可行性预检（2026-09-13 实跑，非正式验证）

用本地日线（2024-01-02 ~ 2026-09-11）按 W-FRI 聚合，抽查 3 只标的：

| 标的 | 周线根数 | MA5 周线 | MA20 周线 | 20 周振幅(base_range) | 3 周振幅(star_range) | 判定 |
|---|---:|---:|---:|---:|---:|---|
| 600460.SH 士兰微 | 139 | 32.64 | 34.70 | 0.778 | 0.291 | **周线空头**（MA5<MA20），横盘条件不成立 |
| 300503.SZ 昊志机电 | 139 | 67.37 | 75.19 | 0.774 | 0.151 | **周线空头**，且 3 周振幅已收敛但方向向下 |
| 603693.SH（本次动量票） | 139 | 11.65 | 13.286 | 0.572 | 0.166 | MA5 仍低于 MA20，最新一周放量长阳（量 5,719 万 vs 20 周均量），属"启动第一周" |

结论：
1. **聚合与指标可算**：2.7 年日线 → 139 根周线，MA5/MA20/振幅/堆量/回踩判定均可向量化；
2. **用户的持仓在周线维度仍是空头结构**（MA5 < MA20），与 P0_13 第 16 节的"HOLD（但受集中度约束）"结论一致；
3. **`base_range` 的默认阈值 15% 偏严**：三只样本全部远超（0.57~0.78），说明 A 股近两年的波动下，
   20 周振幅 ≤15% 的"底部横盘"在 4800 只票里可能极少；实施时应先用全市场分布定阈值
   （例如取过去一年通过率 5%~10% 的分位），而不是拍 15%；
4. **"三星会师"的 3 周振幅阈值 6% 同样需要按分布标定**（样本里 0.15~0.29，即 15%~29%）。

> 这正是第 5 节"先验证再接入"的必要性：口径阈值必须用全市场历史分布标定，否则形态会几乎永不触发
> （或触发得毫无选择性）。
