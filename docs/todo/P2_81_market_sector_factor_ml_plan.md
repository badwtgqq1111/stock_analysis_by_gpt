# P2.81 市场因子、板块因子与策略元学习落地计划

日期：2026-10-22
状态：Phase 0-5 已落地；Phase 6 候选池 shadow + `b4a_cap_arm` 选股策略已落地（2026-10-23），逐笔执行 shadow 待做
目标：在不破坏现有生产链路的前提下，把「市场因子 + 板块因子 + 策略信号」接成一套可回测、可解释、可回滚的元学习层。

> 已完成的数据层与验证见 `output/research/p2_81/DATA_AUDIT.md`。
> 已落地：PIT 申万行业历史、市场因子/个股 beta、PIT 板块因子/个股相对板块、24 条策略因子映射、策略族聚合特征、统一元数据集、iFinD 月度/2024/2026 日频资金流、B0-B3 烟雾 A/B、3 折 rolling walk-forward、B2 候选池 shadow、B4 短样本、B4L 长历史（53特征）、B4 exact 长历史（100特征 + 市场/板块）、`b4a_cap_arm` 候选选股策略。

---

## 0. 先给结论

### 0.1 应该做什么

1. **市场因子**：用中证全指 `000985.CSI` 作为全市场核心，不用上证指数单独代表 A 股；再配沪深300/中证500做大小盘分段。
2. **板块因子**：用申万 L2 或证监会 L2 的行业内相对收益、反转、离散度、拥挤度、资金流、估值分位；不要默认「追最强板块」。
3. **策略因子提炼**：把每条策略拆成「因子暴露 + 信号强度 + 适用状态」，不是把策略名单直接当特征。
4. **机器学习**：现有 LightGBM/Transformer 继续做非线性映射；新增的是**元模型/元标签层**，不是把所有策略硬塞进一个更大的模型。
5. **PIT 优先**：板块历史成员必须先补 `in_date/out_date/available_at`；否则行业/概念回测只能标研究参考，不能晋级。

### 0.2 不应该做什么

- 不要把 20 多条策略的 0/1 选股名单直接拼成 20 多列特征：稀疏、共线、变体重复，151 个训练日撑不住。
- 不要把 `BETA5..60` 当市场 beta：它们源码是价格趋势斜率。
- 不要把 `academic_mkt_beta_proxy` 当指数 beta：它是个股自身收益的自相关；30 只样本与真实 60 日 beta 平均相关仅 0.08。
- 不要把 `pb_ratio_sector_relative` 当行业 PB：它是 52 周价格区间位置。
- 不要把负期望策略（`first_ban_lowxi`、`rbreaker_arm`）当正向特征；最多做风险/反例特征，且必须单独验证。
- 不要把 `live_holdings` 当策略特征：它是账户，不是信号。

---

## 1. 架构：三层，不是一锅炖

```text
原始数据（PIT）
  ├─ 个股日线/成交额/换手/资金流/财务/估值
  ├─ 宽基指数 + PIT 指数权重
  ├─ 行业/概念成员（必须有 available_at）
  └─ 宏观/利率/汇率
        ↓
第一层：因子层（可解释、可单测）
  ├─ 市场因子：收益、beta、波动、宽度、流动性、状态
  ├─ 板块因子：相对收益、反转、离散度、拥挤、资金流、估值
  └─ 个股因子：价值/质量/动量/反转/波动/流动性/资金流/结构
        ↓
第二层：策略信号层（每条策略一个 score/rank，不是原始名单）
  ├─ 规则策略：条件命中 → 0/1 + 强度
  ├─ 因子策略：因子加权分 → 截面 rank
  ├─ 模型策略：LightGBM/Transformer score → 截面 rank
  └─ 组合/覆盖层：波动率目标、风险预算、状态开关
        ↓
第三层：元模型（meta-model / meta-labeling）
  ├─ 输入：第一层因子 + 第二层策略 rank/共识 + 市场/板块状态
  ├─ 标签：未来 5/20 日超额收益（市场中性或行业中性）
  └─ 输出：上涨概率 / 预期超额 / 置信度
        ↓
组合层：风险约束、行业上限、流动性、整手、成本、执行
```

关键点：

- **市场因子在横截面上没有差异**，单靠 `mkt_ret_20` 不能给股票排序；必须通过 `beta × 市场收益`、`size × 市场状态`、`流动性 × 市场换手` 这类交互进入模型。
- **板块因子有横截面差异**，可以直接做板块排序，也可以通过 `stock_vs_sector`、`stock_beta_to_sector` 进入个股模型。
- **策略信号是第二层特征**，不是第一层原始因子。元模型学习的是「在什么市场/板块状态下，哪类策略更可信」，而不是重新学一遍所有因子。

---

## 2. 当前数据审计

| 数据 | 现状 | 结论 |
|---|---|---|
| 全 A 日线（qfq） | 2015-2026，含 open/high/low/close/volume/amount/turnover | ✅ 可用 |
| 宽基指数 | 8 个，2015-2026，含中证全指 `000985.CSI`、沪深300、中证500 | ✅ 可用 |
| PIT 指数权重 | 沪深300/中证500，2016-2026 | ✅ 可用 |
| 市场宽度/总成交额 | 2015-2026 | ✅ 可用 |
| 涨停面板 | 2016-2026 | ✅ 可用 |
| 宏观/利率/汇率 | 2015-2026 | ✅ 可用 |
| 资金流 | 本地日频 2024-08 起；iFinD 日频 2024-01~08；iFinD 月度 2016-2024 | ⚠️ 日频长历史仍需逐日补 |
| 行业分类 | 申万 2021 只有 2026-10-12 一个快照 | ❌ 无 PIT 历史 |
| 概念板块 | `cn_board_membership` 70 个快照全在 2026-10-22 | ❌ 无历史深度 |
| 行业分位特征 | `pe_ind_pct` 等用当前行业映射回填历史 | ⚠️ 前视风险 |
| 非线性模型 | LightGBM/Transformer/CNN 已有 | ✅ 可用 |
| 训练窗 | 生产 LightGBM 151 个交易日、100 特征、20 日标签 | ⚠️ 偏短 |
| 标签 | 原始 20 日收益，非超额 | ⚠️ 市场 beta 会混入 |

2026-10-22 iFinD 试用版实测：实时行情、历史日线、基础数据、日期序列、自然语言选股（可查 2016/2018/2020/2024 任意日期的「主力资金流向」）可用；申万指数历史返回空，概念成员历史查询仍返回当前概念，不能补 PIT。已落地 2016-2024 月度资金流与 2024-01~08 日频资金流，详见 `output/research/p2_81/DATA_AUDIT.md`。

主要证据：

- `assets/data/meta/cn_benchmark_daily.parquet`
- `assets/data/meta/cn_market_turnover.parquet`
- `assets/data/raw/pit/market=CN/index_weight/`
- `assets/data/meta/cn_sw2021_industry.parquet`
- `assets/data/meta/cn_board_membership.parquet`
- `assets/data/feature/clean_feature_panel/_SUCCESS.json`
- `output/models/cn/lightgbm/alpha_zoo_hk/model_manifest.json`
- `output/research/factor_ic_protocol/factor_ic_admitted.json`
- `output/research/industry_momentum/REPORT.md`

---

## 3. 因子定义 v1

### 3.1 市场因子（10-15 个）

以 `000985.CSI` 为核心，`000300.SH`/`000905.SH` 做分段对照。

| 因子 | 定义 | 用途 |
|---|---|---|
| `mkt_ret_1/5/20/60` | 指数日收益复利 | 状态/交互 |
| `mkt_vol_20/60` | 日收益标准差 × √252 | 风险状态 |
| `mkt_dd_60` | 指数 / 60 日高点 − 1 | 风险状态 |
| `mkt_breadth_up` | 上涨家数 / 有报价家数 | 宽度 |
| `mkt_breadth_ma20` | 收盘 > MA20 的股票占比 | 宽度 |
| `mkt_limitup_ratio` | 涨停家数 / 有报价家数 | 投机温度 |
| `mkt_turnover_z` | 全市场成交额 / 20 日均值 − 1，或横截面换手 Z 中位数 | 流动性/拥挤 |
| `beta_60/120/250` | 个股收益对中证全指回归的斜率 | 暴露 |
| `idio_vol_60` | 市场回归残差波动 × √252 | 特质风险 |
| `downside_beta_60` | 市场下跌日的 beta | 下行风险 |
| `beta_x_mkt` | `beta_60 × mkt_ret_20` | 交互 |

### 3.2 板块因子（10-15 个）

板块口径优先申万 L2；概念板块只做补充。历史回测必须用 as-of 成员；没有 PIT 时只做当前截面/研究。

| 因子 | 定义 | 预期方向/用途 |
|---|---|---|
| `sector_ret_5/20/60` | 成员等权或中位收益 | 状态 |
| `sector_rev_20` | `-sector_ret_20` | 本地证据支持反转 |
| `sector_dispersion_20` | 成员 20 日收益标准差 | 轮动环境 |
| `sector_turnover_share` | 板块成交额 / 全市场成交额 | 拥挤 |
| `sector_turnover_z` | 板块成交额 / 20 日均值 | 拥挤 |
| `sector_limitup_density` | 板块涨停家数 / 成员数 | 投机温度 |
| `sector_moneyflow_z` | 成员资金流 Z 中位数 | 仅 2024-08 后 |
| `sector_val_pct` | 板块 PE/PB 分位 | 估值 |
| `stock_vs_sector_ret_20` | 个股 20 日收益 − 板块 20 日收益 | 行业内滞涨/强势 |
| `stock_beta_to_sector_60` | 个股对板块等权收益的 beta | 暴露 |
| `sector_crowding` | `z(turnover_share) + z(limitup_density) + z(sector_ret_20)` | 避拥挤（诊断/交互，不单独当 alpha） |

### 3.3 交互因子（先少量）

- `beta_60 × mkt_vol_20`
- `log_mktcap × mkt_ret_20`
- `turnover_z × mkt_turnover_z`
- `sector_rev_20 × mkt_breadth_up`
- `stock_vs_sector_ret_20 × sector_dispersion_20`
- `sector_crowding × stock_beta_to_sector_60`

原则：先加 6 个，不要一次加 60 个。每个交互都要有单变量 IC 和增量 A/B。

---

## 4. 策略因子提炼表

### 4.1 提炼原则

每条策略输出三类东西：

1. **因子暴露**：这条策略本质上在赌哪些因子（价值、质量、动量、反转、波动、流动性、资金流、结构）。
2. **信号强度**：不是只给 0/1，要给截面 rank/percentile、候选池内排名、距阈值距离。
3. **适用状态**：这条策略在什么市场/板块状态下更可信（高波动/低波动、宽度强/弱、资金流强/弱）。

### 4.2 当前策略映射（第一版）

| 策略 | 类型 | 主要因子暴露 | 元模型中的用法 |
|---|---|---|---|
| `cn_ensemble` | 模型 | 价量、动量、反转、波动、流动性、资金流、估值、质量（100 个准入特征） | baseline，不重复当原始特征；输出 rank/score 可做 meta-feature |
| `cn_ensemble_pk_legacy` | 模型旧版 | 同 `cn_ensemble`，PK 权重不同 | 只做对照/时代特征，默认不进模型 |
| `cn_ensemble_volcap` | 组合覆盖 | 预测波动率、相关矩阵、风险预算 | 风险状态特征，不做选股因子 |
| `arm_select` | 小市值手臂 | 市值、流动性、动量、反转、波动、估值、质量（v4 25 列） | `size`/`liquidity` 暴露 + 策略 rank |
| `arm_select_volrisk` | 组合覆盖 | 波动率倒数/预测波动 | 风险状态特征 |
| `arm_concept` | 板块轮动 | 板块动量、涨停密度、板块内滞涨、资金流、流动性 | `sector_*` + `stock_vs_sector` + 策略 rank |
| `rot_arm` | 板块轮动 | 1/3/6 月动量、3 月波动、成交额份额、换手份额、涨停密度 | `sector_*` + 策略 rank；注意本地动量方向为负 |
| `jq_multi_factor_size_roe` | 公式因子 | 市值、ROE | `size` + `quality` |
| `jq_roe_roa_diagnostic` | 公式因子 | ROE、ROA | `quality` |
| `first_ban_lowxi` | 事件规则 | 首板、非一字、换手、回调、市值、高开 | 负期望；只做反例/风险特征 |
| `box_arm` | 结构规则 | 60 日振幅、首板突破、回调、回踩 | `range_60` + `breakout` + `pullback` |
| `box_squeeze_arm` | 结构规则 | 60 日振幅、20 日压缩、突破、回调、回踩 | `range_60` + `compression_20` + `breakout` + `pullback` |
| `wyckoff_arm` | 结构规则 | 区间、spring、SOS、放量 | `range` + `spring` + `volume_expansion` |
| `moneyflow_arm` | 资金流规则 | 主力净流入 Z、流入持续性、20 日涨幅、MA20、成交额 | `moneyflow_*` + `liquidity` |
| `mainforce_arm` | 资金流+筹码 | 资金流 Z、筹码成本偏离 | `moneyflow_*` + `chip_cost_dev` |
| `donchian_breakout` | 通道规则 | 20 日通道、量比、5 日涨幅、ATR | `breakout_20` + `volume_ratio` + `volatility` |
| `rbreaker_arm` | 日内反转/趋势 | R-Breaker 六线、量能、流动性 | 负期望；只做反例 |
| `moneyflow_factor_equivalent` | 因子流水线 | 6 个资金流因子、硬门槛 | `moneyflow_*` + 策略 rank；与 `moneyflow_arm` 去重 |
| `moneyflow_factor_institutional` | 因子流水线 | 因子分、协方差、风险厌恶、换手惩罚 | `moneyflow_*` + 风险状态；与规则版去重 |
| `moneyflow_ml_nb` | ML 因子流水线 | 资金流、开盘阈值、ML 分数 | 只做研究/时代特征；历史回放有偏差 |
| `brain_quality_lowpe_momentum` | 公式因子 | ROE、PE 行业分位、20 日动量 | `quality` + `value` + `momentum` |
| `brain_panic_reversal_quality` | 事件+质量 | 单日跌幅、量比、MA20、PE 分位、资金流、次日反转 | `panic_reversal` + `quality` + `moneyflow` |
| `live_holdings` | 账户 | 无（真实持仓） | 不作为特征；只做对照/归因 |

### 4.3 不要直接把 20 多条名单拼成特征

错误做法：

```text
feature = [strategy_1_selected, strategy_2_selected, ..., strategy_23_selected]
```

问题：

- 90% 以上是 0，稀疏；
- 策略之间高度共线（`box_arm`/`box_squeeze_arm`、`moneyflow_*` 四个版本、`cn_ensemble`/legacy）；
- 151 个训练日，23 列 0/1 已经很容易过拟合；
- 元模型会学到「最近哪条策略刚好有效」，而不是稳定关系。

正确做法：

```text
feature = 原始因子块
        + 策略族聚合（family_score / best_rank / n_selected）
        + 策略共识（consensus_count / consensus_rank）
        + 市场/板块状态
```

再进一步，用 **meta-labeling**：第一层策略只负责「圈候选」，第二层模型只预测「这个候选未来 20 日跑赢基准的概率」，而不是重新预测全市场收益。

---

## 5. 分阶段落地

### Phase 0：数据与 PIT 审计（2-3 天）

**任务**

- 确认 `000985.CSI`、`000300.SH`、`000905.SH` 的交易日历一致；
- 检查指数权重快照日期与生效日；
- 补行业成员历史：`stock_code, industry_l1/l2, in_date, out_date, available_at`；
- 给概念板块成员补历史快照；没有就明确标「仅当前截面」；
- 给所有因子打 `pit_status`：`ready / partial / blocked`。

**产物**

- `output/research/p2_81/pit_audit.json`
- `output/research/p2_81/pit_audit.md`

**验收**

- 任一因子在 `t` 日只使用 `available_at ≤ t` 的数据；
- 截断扰动测试通过：把 `t` 之后的数据改掉，`t` 及之前的因子值不变；
- 行业/概念覆盖率 ≥ 90%；低于则缩小域并标注。

### Phase 1：市场因子块（3-5 天）

**任务**

- 新建 `scripts/build_market_factors.py`；
- 输出 `assets/data/derived/cn_market_factors.parquet`，主键 `trade_date`；
- 计算 `mkt_ret_*`、`mkt_vol_*`、`mkt_dd_60`、`mkt_breadth_*`、`mkt_limitup_ratio`、`mkt_turnover_z`；
- 个股 beta 单独输出 `assets/data/derived/cn_stock_market_beta.parquet`，主键 `(trade_date, stock_code)`；
- 用 000985 主口径，300/500 做分段对照。

**验收**

- 2015-2026 覆盖率 ≥ 95%；
- beta 分布合理（全市场中位数约 0.8-1.1，极端值 winsorize）；
- 与 `BETA5..60` 的相关性检查：确认不是同一个东西；
- 单因子 IC/NW t 报告。

### Phase 2：板块因子块（5-8 天，取决于 PIT）

**任务**

- 新建 `scripts/build_sector_factors.py`；
- 用申万 L2 为主、证监会 L2 为备、概念板块为补充；
- 输出 `assets/data/derived/cn_sector_factors.parquet`，主键 `(trade_date, sector_id)`；
- 输出个股相对板块 `assets/data/derived/cn_stock_sector_features.parquet`，主键 `(trade_date, stock_code)`；
- 先做 `sector_rev_20`（本地证据支持反转），不要先做 `sector_mom_20` 正向追涨；
- 加 `sector_crowding`、`sector_dispersion_20`、`sector_limitup_density`、`stock_vs_sector_ret_20`。

**验收**

- 板块收益与成员等权收益可复算；
- PIT 成员回测与当前成员回测的差异报告；
- 行业动量/反转 IC 与 `output/research/industry_momentum/REPORT.md` 方向一致；
- 若 PIT 未补齐，输出标记 `research_only=true`，禁止晋级。

### Phase 3：策略因子提炼与去重（3-5 天）

**任务**

- 新建 `config/strategy_factor_map.json`（或在 `config/strategies.json` 增加 `factor_spec`）；
- 每条策略声明：`factor_families`、`primary_factors`、`signal_type`、`direction`、`pit_status`、`ml_usage`、`variant_of`；
- 自动生成策略族聚合特征：
  - `family_score`（族内 rank 均值）
  - `family_best_rank`
  - `n_strategies_selected`
  - `consensus_score`
- 对 `variant_of` 的变体只保留一个代表，其余做对照/时代特征；
- 负期望策略只允许 `risk_flag` 或 `counterexample`，不做正向特征。

**验收**

- 每个策略都有映射，无空白；
- 同一族内相关性 > 0.8 的只保留一个；
- 策略名单与因子映射能对上（同一决策日、同一代码）；
- 负期望策略不会以正权重进入元模型。

### Phase 4：元数据集与基线（3-5 天）

**任务**

- 新建 `scripts/build_meta_dataset.py`；
- 主键 `(trade_date, stock_code)`；
- 输入：市场块 + 板块块 + 个股因子块 + 策略族特征；
- 标签：
  - `forward_excess_return_5d/20d`（相对中证全指或行业中性）
  - `forward_rank_5d/20d`（截面 rank）
  - `forward_positive_20d`（二分类，用于 meta-labeling）
- 先跑三个基线：
  - B0：现有 100 特征 LightGBM
  - B1：B0 + 市场块
  - B2：B0 + 市场块 + 板块块
- 所有切分必须 purged + embargo。

**验收**

- 数据集无未来函数；
- 标签成熟样本 ≥ 60 个交易日 × 100 只；
- B0/B1/B2 使用同一训练/验证/测试窗口；
- 输出 `output/research/p2_81/meta_dataset_manifest.json`。

### Phase 5：非线性模型 A/B（5-8 天）

**任务**

- 模型：LightGBM 为主，Transformer 为辅；
- 目标：回归 `forward_excess_return_20d` + 二分类 `forward_positive_20d`；
- 特征组 A/B：
  - A0：现有
  - A1：+市场块
  - A2：+板块块
  - A3：+策略族特征
  - A4：+交互块
- 每个模型 3 个随机种子，报告均值/方差；
- 置换重要性和 SHAP 按因子族聚合，不逐个特征讲故事。

**验收**

- OOS RankIC 提升 ≥ 0.01，或 Top decile 超额 ≥ 1%/月；
- NW t ≥ 2；
- 换手增加 ≤ 30%；
- 最大回撤不恶化超过 3pp；
- 收益不集中在单一年份/单一行业。

### Phase 6：组合接入与影子（5-10 天）

**任务**

- 把元模型分数接到 `preselection` 之后、`pk` 之前，作为候选排序/过滤；
- 不改生产权重，先做 shadow；
- 影子跑 ≥ 20 个决策日；
- 用 `promotion_check.py` 做同窗口配对；
- 通过后才改 `config/cn_pipeline.toml` 的生产预算。

**验收**

- shadow 相对生产：超额为正、回撤不劣、换手可接受；
- `promotion_check` 判定 PROMOTE；
- 有完整回滚路径（配置开关 + 旧模型保留）。

### Phase 5b：B4/B5 实际模型（不是 B2 替代生产）

B2/B3 是消融，不是最终形态。实际模型应按下面顺序：

| 模型 | 特征 | 目的 |
|---|---|---|
| B4a | 当前生产 100 个准入特征（含 Alpha101/Alpha158/GTJA191） | 基线 |
| B4b | B4a + 市场/beta 块 | 市场块增量 |
| B4c | B4a + 板块块 | 板块块增量 |
| **B4d** | **B4a + 市场块 + 板块块** | **实际候选** |
| B5 | B4d + 策略族聚合 + 资金流 | 元学习增量 |

原则：

- **Alpha101 要加，但不是 101 个原样全加**：走现有准入（IC/ICIR/NW t/覆盖率/相关性/BH-FDR），只保留有增量且不冗余的因子；
- 当前生产模型已经有 Alpha101/158/GTJA，B2 只是把它们拿掉做归因；不要把 B2 当成最终模型；
- B4 先去重：新市场/板块因子与已有 `BETA5/10`、`CORR*`、`sector_rps_reversal_20d`、`*_ind_pct` 做相关性检查，|ρ|>0.8 的只留一个；
- 模型选择：先用 LightGBM 做 B4a-d（表格因子 + 7-9M 行，树模型是强基线）；Transformer 只在“序列输入 + 明确增量”时再加，不因为生产里有 Transformer 就默认它更好；
- 标签用 `forward_excess_20d`（市场/行业中性至少做一种），不要只用原始收益；
- 验收仍是 rolling walk-forward + 成本/执行约束 + 换手/回撤，不看单次切分。

---

## 6. 验证协议

### 6.1 时间切分

- 训练：2016-2022
- 验证：2023-2024
- 测试：2025-2026
- 或 expanding walk-forward：每年重训，purge 20 日，embargo 5-20 日

### 6.2 指标

- RankIC / ICIR / NW t
- Top decile / Top quintile 超额
- Long-short spread
- 胜率、盈亏比
- 换手、成本后收益
- 最大回撤、Calmar、Sharpe
- 二分类：AUC、Brier、校准曲线

### 6.3 必须做的对照

- 市场中性标签 vs 原始收益标签
- 行业中性标签 vs 原始收益标签
- 有/无市场块
- 有/无板块块
- 有/无策略族特征
- 有/无交互块
- 当前成员 vs PIT 成员（差异 = 前视偏差）

---

## 7. 参考（在线资料）

1. Gu, Kelly, Xiu (2020), *Empirical Asset Pricing via Machine Learning*, Review of Financial Studies.
   - DOI: https://doi.org/10.1093/rfs/hhaa009
   - NBER: https://www.nber.org/papers/w25398
   - 关键点：树模型和神经网络的优势主要来自**非线性 predictor interactions**；主导信号包括动量、流动性、波动率。

2. Liu, Stambaugh, Yuan (2019), *Size and Value in China*, Journal of Financial Economics.
   - DOI: https://doi.org/10.1016/j.jfineco.2019.03.008
   - NBER: https://www.nber.org/papers/w24458
   - 关键点：中国 A 股需要专门的大小盘/价值因子设定，不能直接照搬美国 FF 模型。

3. *Factor models for Chinese A-shares* (2024), International Review of Financial Analysis.
   - DOI: https://doi.org/10.1016/j.irfa.2023.102975
   - 关键点：中国 A 股因子模型的设定与成熟市场有差异。

4. *Anomalies in the China A-share market* (2021), Pacific-Basin Finance Journal.
   - DOI: https://doi.org/10.1016/j.pacfin.2021.101607
   - 关键点：A 股异象清单与成熟市场不同，需本地复验。

5. Qlib: An AI-oriented Quantitative Investment Platform (2020).
   - arXiv: https://arxiv.org/abs/2009.11189
   - GitHub: https://github.com/microsoft/qlib
   - 文档: https://qlib.readthedocs.io/en/latest/component/data.html
   - LightGBM 基准: https://github.com/microsoft/qlib/tree/main/examples/benchmarks/LightGBM
   - 关键点：Alpha158/360、LightGBM/Transformer 基准、时序交叉验证工程化。

6. Kakushadze (2016), *101 Formulaic Alphas*.
   - arXiv: https://arxiv.org/abs/1601.00991
   - 关键点：101 个公式化 alpha，平均持有 0.6-6.4 天，两两平均相关约 15.9%；适合做因子库而不是单一模型。

7. Moskowitz & Grinblatt (1999), *Do Industries Explain Momentum?*, Journal of Finance.
   - DOI: https://doi.org/10.1111/0022-1082.00146
   - 关键点：美国市场行业动量显著；**A 股不能直接照搬**，本地 2024-2026 证据显示行业层是反转（1 月 IC −0.067、t −2.90）。

8. *Research on MSCI Barra CNE5 Model and Stock Selection* (2022).
   - DOI: https://doi.org/10.2991/aebmr.k.220405.252
   - 关键点：市场、行业、风格因子的结构化框架；可用于设计风险暴露和中性化。

9. *Meta-Labeling: Theory and Framework* (2022), Journal of Financial Data Science.
   - DOI: https://doi.org/10.3905/jfds.2022.1.098
   - 关键点：第一层模型/策略负责方向，第二层模型负责「是否下注/下注多大」，适合本计划的策略信号 → 元模型。

10. López de Prado (2018), *Advances in Financial Machine Learning*.
    - Wiley: https://www.wiley.com/en-us/Advances+in+Financial+Machine+Learning-p-9781119482086
    - 关键点：purged K-fold、embargo、meta-labeling、多重检验校正。

11. LightGBM 文档.
    - https://lightgbm.readthedocs.io/
    - 关键点：叶子生长、类别特征、正则化；适合中低维因子 + 交互，不是「特征越多越好」。

---

## 8. 交付物清单

| 阶段 | 交付物 |
|---|---|
| Phase 0 | `output/research/p2_81/pit_audit.{json,md}` |
| Phase 1 | `assets/data/derived/cn_market_factors.parquet`、`cn_stock_market_beta.parquet` |
| Phase 2 | `assets/data/derived/cn_sector_factors.parquet`、`cn_stock_sector_features.parquet` |
| Phase 3 | `config/strategy_factor_map.json`、策略族聚合特征 |
| Phase 4 | `scripts/build_meta_dataset.py`、`output/research/p2_81/meta_dataset_manifest.json` |
| Phase 5 | `output/research/p2_81/ablation_report.md`、模型卡 |
| Phase 6 | shadow 曲线、`promotion_check` 报告、回滚说明 |

---

## 9. 一句话回答「是不是每种策略都把因子提炼出来一起喂给 ML」

**是提炼，但不是把策略名单直接拼成特征。**

正确顺序是：

```text
策略 → 因子暴露 + 信号强度 + 适用状态
     → 策略族聚合/去重
     → 与市场/板块/个股因子一起进入元模型
     → 预测未来超额收益/上涨概率
     → 组合层风控与执行
```

直接把 20 多条策略的 0/1 名单丢进 LightGBM，会把「策略选择偏差 + 变体共线 + 短样本过拟合」一起放大；先把策略拆成因子和族，再用元模型学习非线性关系，才是可回测、可解释、可回滚的做法。

## 10. 实施结果（2026-10-23）

### 10.1 B4 exact（2016-2026，100 特征 + 市场/板块消融）

严格 walk-forward，3 折，20 日标签；`output/research/p2_81/B4_EXACT.md`。

| 变体 | Mean test RankIC | Top20 净超额/20d | 3 折累计超额 |
|---|---:|---:|---:|
| B4a current100 | **+0.1310** | +3.959% | +569.27% |
| B4b +market | +0.1185 | +3.217% | +383.68% |
| B4c +sector | +0.1297 | +4.571% | +755.52% |
| B4d +both | +0.1071 | +3.471% | +405.42% |

结论：**在 100 个 Alpha101/158/GTJA 特征之上再加市场/板块块，没有稳定增量**；
B4c 的累计超额更高但 RankIC 更低，属于折间波动，不能当作「板块因子有效」的证据。

### 10.2 可交易性 shadow（行业≤2 + 日均额≥20M + 市值门槛）

| 变体 | 20d 净超额 | 累计超额 | 年化超额 | 超额回撤 |
|---|---:|---:|---:|---:|
| B4a Top20 | +4.420% | +569.27% | +66.47% | −23.7% |
| + 行业≤2 + 额≥20M | +3.520% | +359.02% | +50.46% | −16.3% |
| **+ 行业≤2 + 额≥20M + 市值≥10 亿** | **+2.456%** | **+188.86%** | **+32.89%** | **−15.2%** |
| 同上，市值≥20 亿 | +1.515% | +89.06% | +18.62% | −21.6% |
| 同上，市值≥50 亿 | +0.671% | +29.15% | +7.10% | −22.9% |

关键结论：**收益高度依赖小市值暴露**（市值门槛 10 亿 → 50 亿，年化超额从 32.9% 掉到 7.1%）；
分年 2023 +0.92% / 2024 +1.22% / 2025-26 +4.17%（每 20 日净超额），
命中率 61.7%，47 个不重叠区间。**这条曲线是标签模拟，不是逐笔可交易曲线。**

### 10.3 已落地的策略

`config/strategies.json` → `b4a_cap_arm`（stage=`candidate`，owner=`research`）：

- 训练：`scripts/train_b4a_cap_model.py`（LightGBM，100 特征，`output/models/p2_81/b4a_cap/`）
- 选股：`scripts/select_b4a_cap_arm.py` → 2026-10-22 Top20
- 曲线：`scripts/build_b4a_cap_curve.py` → `output/research/p2_81/b4a_cap/curve/nav_engine.csv`
- 已接入：`build_selection_ledger.py`、`strategy_registry.py`、`refresh_strategy_lists.py`、
  `backend/api/agent.py`（`ml_factor` 分支）、`config/strategy_factor_map.json`
- 验证：`strategy_registry.py --check` 24 条策略通过；`/api/selection?source=cn_b4a_cap_arm` 200/20 行；
  全量 preflight 439 passed

**仍未做**：逐笔执行 shadow（T+1 开盘、涨跌停、停牌、100 股整手、真实费用）。
在补完这一步之前，`b4a_cap_arm` 保持 `candidate`，不进实盘权重。

### 10.4 数据层收口

- iFinD 日频主力资金流：2016-01-04 ~ 2026-10-22 连续，2,622 个交易日、14.63M 行、0 缺口；
  已 merge 进 `cn_meta_dataset`（`mf_ifind_*` 逐年 99.9%+）。
- 5min 分时：5,228 只全部对齐到 2026-10-22（stale=0）；每日链路加新浪/腾讯兜底 + 状态快照；
  深度回补（2020+）由 `backfill_cn_intraday_history.py` 断点续跑，受 BaoStock 黑名单冷却约束。
  详见 `docs/runbooks/intraday-history.md`、`output/research/p2_81/INTRADAY_COVERAGE.md`。
