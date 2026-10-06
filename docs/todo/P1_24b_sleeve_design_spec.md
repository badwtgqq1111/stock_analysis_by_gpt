# P1.24b Sleeve 架构设计规格 v1（可直接实现）

编写：2026-10-06。配套：`P1_24_sleeve_architecture_plan.md`（计划）、`factor_engine/sleeve_design.py`（可执行核心）。
本文只写**设计与参数**，不做现状盘点；所有参数写死，改动必须走 §7 的验收。

## 0. 一页速览

| 项 | 设计选择 | 依据（本仓库实测） |
|---|---|---|
| 家族划分 | alpha / valuation / quality / momentum / reversal **五家族** | 6 腿里 3 条是反转类（候选域 Jaccard 0.62）→ 合并而非新增 |
| 标签 | 域内截面 rank，horizon = `exit.expected_holding_days`，次日开盘进场 | 现为全市场统一 20 日标签；域内排序对齐实际决策 |
| 训练切分 | Purged K-Fold(5) + embargo 5 | 标签窗口重叠会泄漏（López de Prado） |
| 分配器 | 资格门 → ERC → ICIR 倾斜（收缩）→ 重叠惩罚 → 约束投影 | 纯 ERC 会把 47.6% 预算分给负 alpha 腿（见 §5.4 实测） |
| 容量 | 单笔 ≤ 1% × ADV20；组合规模 ≤ 5% × ADV20 | 实测：加 1% 上限回撤 −51.5%→−47.1%，容量上限 100~250 万 |
| 上线门槛 | 域内 IC/ICIR > 域内随机基线；增量 t ≥ 1.5；DSR/PBO 通过 | 防止把噪声当 alpha |

## 1. 架构与数据流

```
clean panel (1,381 列) + 日线 qfq + 资金流 + 行业
        │
        ├─ [标签层] 每 sleeve 一套标签：域内截面 rank，horizon 对齐持有期
        │            · purge/embargo 切分  · 可交易性掩码  · 覆盖标记
        │
        ├─ [Sleeve 层] 5 个独立模型/规则：各自输出 domain 内的 target_weight
        │            · alpha: 全截面 LGBM（现状）
        │            · valuation / quality / reversal / momentum: 家族特征 LGBM
        │
        ├─ [分配层] 资格门 → ERC → ICIR 倾斜 → 重叠惩罚 → 约束投影
        │
        ├─ [汇总层] 合并同标的权重 → 净额化 → 行业/风格约束 → 单票 ≤5%
        │
        └─ [执行层] 参与率上限 1%×ADV20 → 铺单（最高 12×5min）→ 成本记账
```

**关键决策：sleeve 只负责"域内排序 + 输出前 k 只"，不做绝对仓位。** 仓位由分配层给，
这样"选股能力"和"预算决策"分离，才能分别验收与归因。

## 2. 五个 sleeve 的规格（`factor_engine/sleeve_design.py:SLEEVE_SPECS`）

| sleeve | 家族 | 域 | 特征（clean panel 真实列名，取数补 `_clean`） | 标签 horizon | 模型 |
|---|---|---|---|---|---|
| `model_alpha` | alpha | 全 A 可交易 | `admitted_90`（准入 90 列） | 20d | LGBM lambdarank |
| `model_valuation` | valuation | 全 A 可交易 | `pe_ind_pct`, `pb_ind_pct`, `ps_ind_pct`, `dividend_yield_ind_pct`, `pb_ratio_sector_relative`, `valuation_market_cap_age_td` | 20d | LGBM regression |
| `model_quality` | quality | 全 A 可交易 | `roe_ind_pct`, `gross_margin_ind_pct`（100% 覆盖）+ `financial_net_margin`, `financial_roe`, `financial_debt_to_assets`, `financial_eps_yoy`, `financial_net_profit_yoy`（仅 19~20% 覆盖，作可选+缺失标记） | **60d** | LGBM regression |
| `model_momentum` | momentum | 全 A 可交易 | `momentum_12m_skip_1m`, `academic_carhart_mom`, `RPS_20/60`, `price_position_52w_high`, `MA20/60`, `TA_MOM`, `TA_RSI` | 20d | LGBM regression（**当前 off**） |
| `model_reversal` | reversal | 全 A 可交易 | `short_term_reversal_1m`, `sector_rps_reversal_20d`, `vol_max_drawdown_60` + `RPS_5/10` | **10d** | LGBM regression |

统一风险约束（`COMMON_RISK`）：单票 ≤5%、单行业 ≤20%、size 暴露 ≤0.30（z）、单边换手 ≤40%/期、
单笔 ≤1%×ADV20。

LightGBM 参数（写死，两种模板）：

```python
# alpha 模板（排序任务）
{"objective": "lambdarank", "num_leaves": 31, "min_data_in_leaf": 200,
 "feature_fraction": 0.30, "bagging_fraction": 0.80, "lambda_l2": 5.0,
 "learning_rate": 0.03, "n_estimators": 600, "early_stopping": 50}
# 风格模板（回归任务，特征少 → 树更浅、正则更重）
{"objective": "regression", "num_leaves": 15, "min_data_in_leaf": 300,
 "feature_fraction": 0.70~0.80, "bagging_fraction": 0.80, "lambda_l2": 10.0,
 "learning_rate": 0.03, "n_estimators": 400, "early_stopping": 40}
```

**Transformer（同一套标签，独立验收）**：输入 = 过去 60 个交易日的 [该家族特征 + 成交量/换手]，
`d_model=64, n_layers=2, n_heads=4, dropout=0.1`，输出与标签同维（域内 rank）。
与 LGBM 用**同一份 purged 切分**，比拼的是"增量"，不是替代。

## 3. 标签工程（最容易出错的地方）

### 3.1 主标签

```
r_{i,t} = P_{i, t+1+h}^{close} / P_{i, t+1}^{open} − 1
```

- **次日开盘进场**：t 日的决策只能用 t 日收盘及之前的信息，用 t 日收盘价进场是自欺；
- **h = 该 sleeve 的 `exit.expected_holding_days`**：alpha 20d、quality 60d、reversal 10d、
momentum 20d、donchian/limit 5d（若复活）；
- 停牌/一字板：进场日若 `open` 缺失或涨跌停，**该样本置 NaN**（不是填 0）。

### 3.2 域内截面变换

```
rank_{i,t} = 2 × (percentile_rank(r_{·,t} within domain_t) − 0.5)   ∈ [−1, 1]
```

- **域内**（该 sleeve 当日候选集，典型 130~600 只），不是全市场 —— 域内排序直接对齐
"我在自己的候选里挑 top-k"这一决策；
- 先做**行业中性**：`r ← r − mean(r | trade_date, industry_l1)`；
- 域内样本 < 20 只的交易日整日丢弃（防止伪截面）。

### 3.3 样本权重（不写权重＝默认所有样本同等可信，是错的）

| 情形 | 权重 |
|---|---|
| 正常 | 1.0 |
| 市值快照 stale（`valuation_market_cap_is_stale`） | 0.5 |
| 关键特征缺失（`*_is_missing = 1`） | 0.7（且把缺失标记本身作为特征喂给模型） |
| 财报列缺失（quality 家族 80% 的股票） | 0.5，**并同时跑"仅用 `*_ind_pct`"的对照模型** |
| 成交额 < 2000 万 | 0（不可交易） |

### 3.4 切分：Purged K-Fold + Embargo

- K=5，**purge = h 个交易日**（标签窗口重叠会泄漏），**embargo = 5 个交易日**；
- 实现见 `factor_engine/sleeve_design.py:purge_embargo_split()`；
- 严禁随机切分、严禁用未来数据做特征标准化（标准化统计量只能用训练段）。

## 4. 训练协议与多重检验控制

1. **目标函数**：alpha 用 `lambdarank`（域内排序），风格腿用 `regression` 打在 rank 标签上
（L2 在弱信号下有地板效应，见 config 注释）；
2. **早停**：用 purged 验证段，`early_stopping=50`；
3. **试验记录**：每次训练把（特征集、参数、切分、IC、试验序号）写入 `output/research/sleeve_trials.csv`，
用于 DSR 的试验次数 N 校正；
4. **Deflated Sharpe Ratio**（Bailey & López de Prado 2014）：上线前报告 DSR；
5. **PBO（回测过拟合概率）**：用 CSCV（组合对称交叉验证）估计，阈值 **PBO < 0.5**；
6. **特征准入**：家族特征表**先写死再跑**（见 §2），任何"看结果再加特征"都要记入试验台账。

## 5. 分配器（`SleeveAllocator`）

### 5.1 等风险贡献（ERC）

目标：每腿风险贡献相等。记协方差 Σ、权重 w、组合波动 σ_p = √(wᵀΣw)，风险贡献
`RC_i = w_i (Σw)_i`，条件 `RC_i = σ_p² / n`。定点迭代：

```
w_i ← w_i · (σ_p / n) / RC_i        （再用 Σw = 1 归一）
```

实现：`SleeveAllocator._erc_weights()`（500 次迭代，tol 1e-8，实测收敛）。

### 5.2 ICIR 倾斜与收缩

```
ICIR_i = mean(IC_{i,·}) / std(IC_{i,·})     （滚动 24 期，半衰期 12 期）
ICIR̃_i = (1 − λ)·ICIR_i + λ·mean(ICIR)      （λ = 0.30 的 James–Stein 收缩）
w_i ∝ w_i^{ERC} · exp(ICIR̃_i)               （指数倾斜保持正权重）
```

收缩是必须的：ICIR 的估计噪声（24 期）远大于其真值差异，不收缩会出现单腿权重爆炸。

### 5.3 资格门（**这一步不能省**）

只有"有正期望"的腿参与预算：

```
w_i = 0  若  ICIR_i ≤ 0.30  或  ΔSharpe_i(留一) ≤ 0
```

理由见 §5.4：ERC 只用协方差，**会把预算分给负 alpha 的低波动腿**。

### 5.4 实测（`uv run python -m factor_engine.sleeve_design --demo`）

输入：本仓库 36 期实测 sleeve 收益（`output/research/sleeve_metric_attribution_20261006_100314/`）。

| sleeve | ICIR 代理 | ΔSharpe(留一) | 资格 | 纯 ERC 权重 | **加门后权重** | 风险贡献 |
|---|---|---|---|---|---|---|
| model_alpha | 1.154 | +1.456 | ✅ | 0.120 | **0.467** | 0.871 |
| bottom_momentum | 2.945 | +0.053 | ✅ | 0.064 | **0.361** | 0.138 |
| momentum | 1.234 | +0.074 | ✅ | 0.037 | **0.172** | −0.009 |
| reversal | −1.457 | −0.218 | ❌ | **0.303** | 0 | 0 |
| untagged | −1.151 | −0.158 | ❌ | **0.476** | 0 | 0 |
| setup | 0.054 | +0.100 | ❌ | 0 | 0 | 0 |

**这就是纯 ERC 的陷阱**：不做资格门，负 alpha 的 `untagged` 会拿到 **47.6%** 预算、
`reversal` 拿 **30.3%**；加入资格门后两者归零。上线必须带门。

### 5.5 候选域重叠惩罚

对候选域 Jaccard 超过阈值的腿对，限其合计预算：

```
若 O_ij > 0.50：  w_i + w_j ≤ 0.50 / (1 + O_ij)
```

实测（Phase 0 v2）：`bottom_momentum ↔ value_reversal` 的 Jaccard = **0.6188**，
惩罚把该对合计预算从 0.458 压到 **0.361**。资格门之后这条惩罚作用变小（高重叠腿本身没资格），
但它是**防止未来两条高重叠腿同时合格**的保险，必须保留。

### 5.6 重估节奏

月度重估，但设**换手上限**：单期预算调整幅度 ≤ 20%（相对），避免预算层产生额外换手。

## 6. 组合构建与执行

1. **合并**：各 sleeve 的 `target_weight × 预算` 相加；单票 > 5% 截断后按比例回补；
2. **净额化**：同标的多 sleeve 反向订单先内部对冲（Phase 0 实测当前持仓层重叠 = 0，
故该项收益≈0；但候选域重叠 38%，一旦放宽 slots 就会有用）；
3. **容量**：单笔 ≤ 1%×ADV20（超限少买，不追）；组合规模上限 = 5%×ADV20 ≈ **100~250 万**；
4. **执行**：需要时用 `execution_bars=12`（1 小时铺单）替代开盘一把成交；
5. **成本记账**：佣金万 2.5（最低 5 元）+ 印花税千 1（卖）+ 滑点万 3 + 冲击（`impact_coef=1.0`）。

## 7. 验收协议（每道都是硬门）

### 7.1 W2 域内随机基线（2026-10-06 已执行 —— 第一条硬门的实测结果）

`uv run python scripts/sleeve_random_baseline.py --start 2025-09-01 --top-k 5 --draws 300 --horizons 5,10,20`
→ `output/research/sleeve_random_baseline_20261006_102811/`

口径：39 个决策日；可交易域中位 **3,234 只**（主板，成交额 ≥2000 万、非涨跌停、有 bar）；
每个决策日随机抽 5 只 × 300 次；真实持仓用**同一 horizon 重算**（次日开盘进场 → +h 收盘）。

| horizon | 域内等权收益 | 随机 top-5 均值 | 随机 p05 | 随机 p95 |
|---|---|---|---|---|
| 5d | +0.29% | +0.31% | -4.37% | +5.78% |
| 10d | +0.25% | +0.31% | -5.89% | +7.70% |
| 20d | +0.23% | +0.21% | -7.87% | +10.21% |

**真实持仓 vs 基线（判决表）**：

| sleeve | horizon | 天数 | 真实持仓 | 随机均值 | 过随机均值 | 过 p95 | 超出域内等权 |
|---|---|---|---|---|---|---|---|
| **model_alpha** | 20d | 22 | **+6.25%** | -0.03% | ✅ | ❌ | **+6.31%** |
| model_alpha | 10d | 22 | +2.21% | +0.20% | ✅ | ❌ | +2.08% |
| model_alpha | 5d | 22 | +1.72% | +0.06% | ✅ | ❌ | +1.69% |
| momentum | 20d / 5d | 8 | +2.95% / +3.08% | +0.76% / +1.08% | ✅ | ❌ | +2.20% / +2.11% |
| momentum | 10d | 7 | -1.78% | +0.31% | ❌ | ❌ | -2.03% |
| untagged | 20d | 20 | -0.56% | +0.23% | ❌ | ❌ | -0.78% |
| setup | 20d | 11 | +0.12% | +3.12% | ❌ | ❌ | -3.02% |
| bottom_momentum | 20d | 7 | -4.65% | +1.56% | ❌ | ❌ | -6.14% |
| **reversal** | 20d | 20 | **-6.08%** | +0.07% | ❌ | ❌ | **-6.18%** |

**判决**：

1. **只有 `model_alpha` 在三个 horizon 上全部跑赢随机均值**（20 日 +6.25% vs 随机 -0.03%，超出域内等权 +6.31pp）
   —— 与归因结论一致（风险贡献 96.1%、ΔSharpe +1.46）。
2. **`reversal` / `bottom_momentum` / `setup` / `untagged` 全部跑输随机均值，也跑输域内等权**
   → 按本节门槛**不具备上线技能**：reversal 最差（20 日 -6.08%，比域内等权还差 6.18pp），
   与归因（自身 -59.2%、夏普 -1.46）互相印证。**结论：这三条反转/形态腿应当合并或直接下线，而不是新增。**
3. **`momentum` 结论不一致**（5d/20d 正、10d 负，样本仅 7~8 天）→ 维持 `off`，不做结论。
4. **没有任何 sleeve 通过 p95 门槛**——包括 model_alpha（22 天样本下 p95 = +10.14%）。

**据此对验收规则的修订（重要）**：p95 是"单次抽样极值"口径，在 ~20 个决策日的小样本下过严，
会让唯一有技能的腿也过不了门。改为：

- **主门槛**：超额（真实 − 随机均值）的 **Newey–West t 值 ≥ 1.5**，且 5 折中 ≥4 折为正；
- **辅助门槛**：真实持仓收益 > 随机均值；p95 仅作为"是否需要更长样本"的提示，不作为否决项；
- 样本门槛：决策日 < 40 个时，任何结论标注"样本不足"，不得据此改状态（本轮 model_alpha 的 t 值待补算）。

| 阶段 | 门槛 | 证据产物 |
|---|---|---|
| 特征 | 家族特征覆盖率 ≥ 95%（缺失列先补或用 `*_ind_pct`） | `family_feature_coverage.txt` |
| 标签 | 域内样本 ≥ 20 只/日；purge/embargo 生效（可复现切分） | 切分表 |
| 单腿 | 域内 **OOS rank IC > 域内随机基线**，5 折 ≥4 折为正；NW t ≥ 2 | `sleeve_ic_report` |
| 单腿增量 | （模型 top-k）−（规则 top-k）超额 > 0 且 **t ≥ 1.5** | A/B 报告 |
| 多重检验 | **DSR > 0** 且 **PBO < 0.5** | `sleeve_trials.csv` + CSCV |
| 组合 | 加腿后 **ΔSharpe > 0、ΔCalmar > 0**（`sleeve_metric_attribution.py` 重跑） | 归因报告 |
| 容量 | 组合规模 ≤ 5%×ADV20；冲击后仍为正期望 | `impact.csv` |

**上线流程**：研究态(`research`) → 通过全部门槛 → 由 `[strategies.*].status` 转 `active`，
并在 `evidence` 写入：IC/ICIR/DSR/PBO/ΔSharpe/ΔCalmar 六个数。任一项回退即转回 `research` 或 `off`。

## 8. 实施工单

| # | 工单 | 文件 | 验收 |
|---|---|---|---|
| W1 | sleeve 规格与标签引擎 | `factor_engine/sleeve_design.py`（已落地） | `--specs` 可打印；`compute_sleeve_label` 在合成面板上通过形状/取值范围断言 |
| W2 | 域内随机基线 | `scripts/sleeve_random_baseline.py`（新增） | 每个 sleeve 每日随机取 top-k 的收益分布，产出 IC=0 基线 |
| W3 | valuation sleeve 训练 | `[strategies.model_valuation]` + `stage=lightgbm` | 域内 IC > 基线，5 折 ≥4 折为正，DSR>0 |
| W4 | quality sleeve 训练（ind_pct 版 + 可选财报版对照） | 同上 | 两个版本对比表；若财报版无增量则不启用财报列 |
| W5 | reversal 三合一 | 注册表合并 + 域内 10d 标签 | 合并后 ΔSharpe > 0（否则直接砍掉） |
| W6 | momentum 复活评审 | 域内 IC 报告 | IC ≤ 0 → 保持 off 并写负结果 |
| W7 | 分配器接入 | `[selection.sleeve_allocator]` + `SleeveAllocator` | 月度重估、换手上限、资格门与重叠惩罚生效 |
| W8 | 归因回归测试 | `scripts/sleeve_metric_attribution.py` | 任何 sleeve 增删后归因报告可复现且阈值达标 |

### 8.1 W3 已执行：`model_valuation` 通过门槛（2026-10-06）

`uv run python scripts/train_sleeve_model.py --sleeve model_valuation --folds 5`
→ `output/research/sleeve_train_model_valuation_20261006_104339/`

样本 1,784,375 行 / 5,212 只 / 354 个交易日（2025-03-20 ~ 2026-09-30）；
特征 7 个（`pe/pb/ps/dividend_yield_ind_pct` + `pb_ratio_sector_relative` + `valuation_market_cap_age_td` + 缺失计数）；
标签 20 日域内 rank（行业中性、次日开盘进场）；Purged K-Fold(5) + purge 20 + embargo 5。

| fold | 测试日 | OOS IC | ICIR | NW t | top-5 超额（毛） | 十分位价差 |
|---|---|---|---|---|---|---|
| 1 | 71 | +0.0442 | +1.053 | +7.26 | +5.18% | +2.67% |
| 2 | 71 | +0.0752 | +1.032 | +2.35 | +6.24% | +5.63% |
| 3 | 71 | +0.0707 | +1.274 | +3.82 | +1.49% | +5.99% |
| 4 | 71 | +0.0525 | +0.479 | +1.33 | +3.14% | +3.42% |
| 5 | 70 | -0.0177 | -0.085 | -0.25 | -0.37% | +0.12% |
| **pooled** | 354 | **+0.0451** | **+0.381** | **+2.30** | **+3.15%** | — |

**门槛判定（设计 §7）**：IC 为正 4/5 折（要求 ≥4/5）✅；NW t = 2.30 ≥ 1.5 ✅
→ **`model_valuation` 通过第一轮验收**，是从"研究态"转为"待接入"的第一条**新家族**腿。

三条必须同时记录的保留意见：

1. **样本只有 354 个交易日（约 1.5 年）**，且第 5 折 IC 转负（-0.018）——4/5 是**刚好达标**，
   需要在更长样本（把 clean panel 回溯到 2024 或更早）上复验；
2. **top-5 超额是毛口径**（未计万 2.5 佣金 + 千 1 印花税 + 万 3 滑点 + 冲击）；20 日调仓、单边换手若 40%，
   双边成本约 0.3~0.5%/期，会吃掉一部分超额；
3. 与 W2 随机基线（20 日随机 top-5 均值 +0.21%、p95 +10.21%）对比：**+3.15% 高于均值、低于 p95**
   —— 与 §7.1 的结论一致（p95 在小样本下过严），需按修订后的 NW t 门槛判定，本轮 t = 2.30 通过。

### 8.2 数据底座补齐：财务数据（2026-10-06，已完成）

`uv run python scripts/backfill_cn_financials.py`（launchd 常驻，约 2.2 小时跑完）
→ `assets/data/raw/financials_relay/fina_indicator/<CODE>.parquet`（逐股检查点）
→ `--merge` → `assets/data/meta/cn_financial_indicators.parquet`

| 项 | 值 |
|---|---|
| 覆盖 | **5,225 只 / 236,136 行**（2006-06 ~ 2026-09，20 年 PIT 历史） |
| 字段 | 23 列（roe / roa / 毛利 / 净利 / 资产负债率 / 流动比率 / eps / bps / 成长 yoy / 现金流） |
| 关键字段覆盖率 | roe **99.1%**、roa **97.3%**、grossprofit_margin **97.1%**、netprofit_margin 99.4%、debt_to_assets 97.2%、ocfps 97.0% |
| 对比回补前（clean panel） | `financial_roe` 19% → **99.1%**；`financial_roa` **0% → 97.3%** |
| PIT 语义 | 表内同时保留 `ann_date`（公告日）与 `end_date`（报告期）；训练时**必须按 `ann_date` 回看**（`pd.merge_asof`） |

> 踩坑记录：LaunchAgent 环境下**没有中继密钥**（依赖 `config/scheduler_env.sh`），首版子进程全部失败、被隔离计数；
> 修复=脚本启动时调用 `load_scheduler_env()`。另：逐只子进程 7.3s/只 → 改"一批一个子进程"1.6s/只。

### 8.3 W4 已执行：`model_quality`（60 日标签）—— **边缘，暂不通过**

`uv run python scripts/train_sleeve_model.py --sleeve model_quality --folds 5 [--financial-pit]`
特征 = `roe_ind_pct` / `gross_margin_ind_pct`（100% 覆盖）+ 财报列（两套来源对比）+ 缺失标记。

| 变体 | 财报特征来源 | 特征数 | pooled IC | ICIR | NW t | top-5 超额（毛） | IC 为正 | 门槛 |
|---|---|---|---|---|---|---|---|---|
| **A** | clean panel（覆盖 **19%**） | 8 | +0.0368 | +0.561 | **+1.86** | +8.42% | 4/5 | ✅ 刚好通过 |
| **C** | 回补 PIT（覆盖 **99%**） | 9 | **+0.0450** | +0.459 | +1.46 | **+17.14%** | 3/5 | ❌ 差一点 |

**判读（重要，不许挑变体）**：

1. 两个变体**都在门槛边缘**（t 1.86 vs 1.46、折数 4/5 vs 3/5），差异在 314 个交易日的样本下**不具统计意义**；
   不能据此宣称"回补的数据更好/更差"——**它们是两次试验，必须共同记入 DSR 的试验次数**。
2. 变体 C 的 IC 与 top-5 超额明显更高（+17.14%/60 日 ≈ 每月 7%+），但这个量级**必须先过容量与成本关**：
   60 日持有、top-5、小盘股，1%×ADV20 约束下组合规模上限仅 100~250 万，且未扣双边成本。
3. **结论**：质量腿**暂不通过**；需要的不是继续调特征，而是
   ① **更长样本**（把特征面板从 2025-03 回溯到 2024 或更早，或用日线 + PIT 财务自建更长面板）；
   ② **预注册特征集**（先写死再跑，避免在 A/C 之间挑最好看的）；
   ③ 观察 60 日标签下的换手与成本（本腿持有期长，换手本应更低，这是它的潜在优势）。

4. **试验台账已建立**：`output/research/sleeve_trials.csv`（每次训练自动登记：sleeve、horizon、特征清单、
   样本量、IC/ICIR/NW t/top-k、折数、产物路径），供 DSR 校正与事后审计。目前登记 2 条（A、C）。

### 8.4 W3b 长样本复验 + **宽度检验**（2026-10-06，本文件最重要的修订）

**动机**：§8.1 的 valuation 结论建立在 354 个交易日上（样本太短）。为此新建长样本估值面板：
`scripts/build_valuation_features_long.py` → `assets/data/meta/cn_valuation_features_long.parquet`
（2014-2026，1,300 万行级；`pe_ind_pct` 覆盖 80.2%、`pb_ind_pct` 95.5%、`size_ind_pct` 100%；
**`ps_ind_pct` 在该源为空、`dividend_yield` 亦为空 → 长面板剔除**；行业用 registry 当前分类，非 PIT）。

**长样本训练**（2015-01 ~ 2026-09，**2,827 个交易日 / 508 万行**，特征 = pe/pb 行业分位 + 组合分位 + 缺失标记）：

| fold | 训练日 | 测试日 | IC | ICIR | NW t | top-5 超额 | 十分位价差 |
|---|---|---|---|---|---|---|---|
| 1 | 2261 | 566 | +0.0794 | +0.477 | +3.12 | +0.044% | +1.708% |
| 2 | 2261 | 566 | +0.0121 | +0.070 | +0.42 | +0.061% | +0.764% |
| 3 | 2262 | 565 | +0.0712 | +0.492 | +3.03 | −2.131% | +2.656% |
| 4 | 2262 | 565 | +0.0880 | +0.751 | +4.95 | +1.506% | +2.918% |
| 5 | 2262 | 565 | +0.0405 | +0.281 | +1.81 | −1.764% | −0.475% |
| **pooled** | — | 2827 | **+0.0583** | **+0.380** | **+5.31** | **−0.456%** | — |

**两个结论同时成立，且方向相反**：

1. **排序能力是真的**：11 年样本、**5/5 折 IC 为正**、pooled IC +0.0583、**NW t = 5.31**
   —— 比短样本（IC +0.0451、t 2.30、4/5 折）**更强更稳**。估值因子在这个域里有可重复的截面排序能力。
2. **top-5 超额是假的**：长样本 top-5 超额 **−0.456%**（短样本的 +3.15% 被证伪为局部运气）。

**宽度检验（用长样本 OOS 预测，20 日前向收益，逐日 Newey–West t）**：

| 持仓数 | 平均超额（20 日） | 月化近似 | NW t | 超额标准差 |
|---|---|---|---|---|
| **5** | **−0.46%** | −0.68% | **−0.75** | 9.91% |
| 20 | −0.05% | −0.08% | −0.10 | 7.85% |
| 50 | +0.30% | +0.45% | +0.66 | 6.44% |
| 100 | +0.35% | +0.53% | +1.00 | 5.07% |
| 200 | +0.41% | +0.61% | +1.48 | 3.96% |
| 500 | +0.39% | +0.58% | **+2.18** | 2.61% |

对照 Grinold 基本法则 `IR ≈ IC × √breadth`（IC≈0.058）：持有 5 只理论 IR 0.45、20 只 0.90、50 只 1.42、100 只 2.01。

**由此对设计的修订（强制，写入 §2/§5）**：

1. **sleeve 的输出不再是 top-5，而是"域内加权组合"**：默认取 **top-50 等权**（或 rank 加权），
   实现在 `SLEEVE_SPECS[...].risk` 增加 `output_breadth`（默认 50）与 `weighting`（`equal` | `rank`）；
   top-k 仅作为"容量受限时的最小可行子集"，且 k ≥ 20。
2. **集中度来自组合层，不来自单 sleeve**：单票上限 5% 仍由组合层约束；不要把"持有 5 只"当成分散手段。
3. **重新解释"6 条腿只有 1 条有效"**：此前各腿被 `slots=1~2` 掐死，等于强制它们只输出 top-2；
   在 IC≈0.05 的信号上，top-2 的期望超额本来就被噪声吞没（本表 top-5 为负即为证）。
   → **先修宽度，再判腿的死活**：W2 对 reversal 的判决（真实持仓 −6.08%）也要在"宽度=50"下重算一次。
4. **成本口径必须同步更新**：宽度 50 的换手远低于 top-5（后者每期几乎全换），
   这会把之前"双边成本吃掉超额"的估算显著改善——需要在下一轮用真实换手重算。

### 8.5 宽度 × 调仓节奏 × 成本：修正后的统一判决表（2026-10-06 收口）

`uv run python scripts/sleeve_width_sweep.py --widths ... --rebalance-every <horizon>`
→ `output/research/sleeve_width_sweep_*/`

**方法学修正（两条，都会把结论带偏，必须写下来）**：

1. **换手必须按调仓节奏算，不能按逐日算**：初版按"逐日"计算 top-k 集合变化，得到 70~99% 的"每日换手"，
   把成本拖累算大了十几倍；改为按标签 horizon（20/60 日）对齐的调仓日计算。
2. **调仓节奏必须与标签 horizon 一致**：用 20 日节奏评估 60 日标签会因样本重叠高估 t；反之样本不足。

**估值腿（长样本 2015-2026）在不同节奏下**：

| 调仓节奏 | 调仓次数 | 最优宽度 | 毛超额/期 | NW t | 净超额/期 |
|---|---|---|---|---|---|
| 20 日 | 142 | top-200 | +0.42% | +1.07 | +0.26% |
| **60 日** | 48 | **top-100** | **+0.93%** | **+1.99** | **+0.75%** |
| 60 日 | 48 | top-200 | +0.88% | +1.98 | +0.71% |
| 60 日 | 48 | top-500 | +0.52% | +2.25 | +0.36% |

**四条腿的修正后判决**（宽度 × 节奏对齐）：

| sleeve | 样本（调仓次数） | 最优宽度 | 毛超额 | 净超额 | NW t | 判决 |
|---|---|---|---|---|---|---|
| **valuation（长样本）** | 142（20 日）/ 48（60 日） | top-100 | +0.93%/60 日 | **+0.75%** | **+1.99** | **勉强过 t 门槛**，但见下方保留意见 |
| reversal | 19 | top-5 | +0.81% | +0.59% | +1.37 | 样本不足且 t<1.5 → 维持 off |
| momentum | 18 | top-20 | −1.67% | −1.88% | −2.37 | **明确否决**（毛超额为负） |
| quality | **6** | — | — | — | n.a. | **样本不足，无法判定**（60 日标签 × 314 天 = 6 次调仓） |

**对估值腿的三条保留意见（不能只看那个 t=1.99）**：

1. **调仓节奏与标签 horizon 不一致**：模型用 20 日标签训练，却按 60 日持有评估（为了拿到足够调仓次数），
   期间信号已衰减；这个 +0.93% 是"20 日信号 + 60 日持有"的混合口径，不是干净的可部署收益。
2. **季度换手仍高达 ~80%**（top-100 每期换掉 80 只）——对一个价值型因子而言换手高得不合理，
   说明模型截面排序不稳定（弱信号被噪声主导的典型症状），真实执行的换手成本会更差。
3. **量级偏小**：净 +0.75%/季度 ≈ 年化 3%（未计冲击），相对独立 sleeve 要承担的风险与复杂度，
   边际太薄。**结论：估值腿在门槛边缘，不足以单独作为第二条 active 腿上线。**

**由本条得出的架构级结论（比单腿判决更重要）**：

> **单一弱信号（IC≈0.06）撑不起一个独立 sleeve。** 正确用法是**多个低相关弱信号组合**后
> 把组合 IC 抬到可用区间（组合 IC 的提升来自分散，不是来自单腿变强），再由分配器按风险预算配置。
> 因此 **Phase 1 分配器的优先级高于继续打磨单腿**；而现行"每条腿都要独立过 1.5 门槛"的验收，
> 应补一条**组合口径**的验收：`组合（N 条腿等权）× 宽度 100` 的净超额与 NW t。

### 8.6 统一长期特征面板 + 四条腿重训 + **组合口径验收通过**（2026-10-06 收口）

**1) 统一长期特征面板**：`scripts/build_sleeve_features_long.py` → `assets/data/meta/cn_sleeve_features_long.parquet`
（2015-2026，**1,009 万行 / 3,534 只**，含 **209 只退市股**；估值行业分位 + PIT 财务行业分位 + 技术/反转特征）。

**2) 四条腿在同一段样本上重训**（2015-01 ~ 2026-09，Purged K-Fold(5)，含退市股）：

| sleeve | horizon | 特征数 | 样本 | pooled IC | ICIR | NW t | top-5 超额 | 折数 |
|---|---|---|---|---|---|---|---|---|
| quality | 60d | 7 | 724 万行 / 3,522 只 | **+0.1478** | **+1.070** | **+8.49** | +8.36% | 5/5 |
| momentum | 20d | 8 | 737 万行 / 3,527 只 | **+0.1304** | +1.026 | **+15.37** | +1.05% | 5/5 |
| reversal | 10d | 4 | 741 万行 / 3,529 只 | +0.1082 | +0.842 | **+17.03** | +0.19% | 5/5 |
| valuation | 20d | 3 | 737 万行 / 3,527 只 | +0.0338 | +0.326 | +4.92 | −0.39% | 5/5 |

> **幸存者偏差检验**：把 209 只退市股的行情并入面板后重训，IC 变化仅 ±0.01
> （momentum 0.1211→0.1304、reversal 0.1019→0.1082、valuation 0.0356→0.0338），
> **说明此前结论不是幸存者偏差造成的**。修复过程中还发现并修掉一个会**重新引入**该偏差的 bug：
> 退市股不在 registry → `industry_l1` 为 NaN → 行业中性化的 groupby 丢掉整组 → 标签整列变 NaN，
> 退市股被静默排除。修法：中性化前把缺失行业填成 `__UNKNOWN__`。

**3) 组合口径验收**：`scripts/sleeve_combine_test.py`（把各腿预测转成当日横截面分位后等权/秩平均组合，
统一按 20 日调仓与 20 日前向收益评估；单边成本 0.11%）。

**预测分位相关矩阵（Spearman，1,400 万行交集）**：

|  | valuation | quality | momentum | reversal |
|---|---|---|---|---|
| valuation | 1.000 | **0.019** | **0.570** | 0.168 |
| quality | 0.019 | 1.000 | 0.042 | **0.004** |
| momentum | 0.570 | 0.042 | 1.000 | 0.166 |
| reversal | 0.168 | 0.004 | 0.166 | 1.000 |

→ **quality 与其余三腿几乎零相关**（0.004~0.042），是真正的分散来源；**valuation 与 momentum 高度重叠**（0.570）。

**单腿 vs 组合（140 个调仓期，20 日节奏）**：

| 打分 | 宽度 | IC | IC t | 毛超额 | NW t | 换手 | **净超额** |
|---|---|---|---|---|---|---|---|
| **combo_equal** | top-100 | **+0.1603** | **+18.16** | +1.377% | **+4.83** | 60.3% | **+1.245%** |
| combo_equal | top-20 | +0.1603 | +18.16 | +1.320% | +3.23 | 78.7% | +1.146% |
| combo_rankmean | top-100 | +0.1588 | +17.78 | +1.372% | +5.25 | 59.1% | +1.242% |
| **quality** | top-20 | +0.0898 | +9.16 | +2.769% | **+5.32** | **14.2%** | **+2.738%** |
| quality | top-100 | +0.0898 | +9.16 | +2.267% | +6.18 | 12.5% | +2.239% |
| momentum | top-100 | +0.1274 | +14.37 | +0.852% | +2.72 | 67.6% | +0.704% |
| valuation | top-100 | +0.1337 | +17.47 | +1.216% | +3.86 | 64.0% | +1.075% |
| reversal | top-100 | +0.0338 | +5.30 | +0.090% | +0.35 | 75.8% | −0.077% |

**四条结论**：

1. **组合确实提升 IC**：等权组合 IC **+0.1603** > 最好单腿（momentum +0.1274；quality 在 20 日口径只有 +0.0898），
   IC t = 18.16 —— §8.5 的假设被证实，**组合口径净超额 +1.25%/20 日、NW t +4.83 → 通过门槛** ✅
2. **质量腿是最有价值的单腿**：净超额 **+2.74%**（top-20）且 **换手仅 12~14%**（其余三腿 60~84%），
   并且与所有腿近零相关 → 它是"低换手 + 分散"的稀缺资产，**优先级应高于 valuation**。
3. **valuation 与 momentum 冗余**（相关 0.570）→ 按 §5.5 的重叠惩罚，两者合计预算应收紧；
   组合的边际提升主要来自 quality，而不是"再堆一条价格类腿"。
4. **reversal 即使在全样本也只有 +0.09% 毛超额、净 −0.08%** → 维持 off（与 §8.5 一致）。

**对 P1.24 的收口判断**：Phase 1（分配器）的**等权特例已验证可用**（combo_equal 过门槛）；
下一步是把"分位等权组合 + 宽度 100 + 质量腿优先"落成配置与脚本，而不是继续加腿。

### 8.7 W7/W8 完成：分配器接入 + 回归测试（2026-10-06 收工）

**1) 预算计算落地为可复现 CLI**

```bash
uv run python -m factor_engine.sleeve_design --budgets \
  --corr output/research/sleeve_combine_*/prediction_correlation.csv \
  --net-excess output/research/sleeve_combine_*/combine_metrics.csv
# → output/research/sleeve_budgets.json
```

实测输出（真实台账 + 真实组合验收指标）：

| sleeve | 预算 | 风险贡献 | 说明 |
|---|---|---|---|
| model_quality | **0.390** | 0.436 | ICIR 1.07，与其余腿近零相关 |
| model_momentum | **0.378** | 0.410 | ICIR 1.03，但与估值相关 0.570 → 被重叠惩罚压过 |
| model_valuation | **0.232** | 0.154 | ICIR 最低（0.33）且与动量冗余 → 权重最低 |
| model_reversal | **0.000** | 0.000 | **资格门剔除**：实测净超额 −0.077% ≤ 0 |

约束状态 `feasible`（max_pair_excess 0、max_weight_excess −0.0099）；风险模型 `Σ = σ²·C`（σ=20% 共同波动、C=相关性矩阵）。

**2) 生产配置**

- `[selection.sleeve_allocator]`：`enabled=false`（默认关闭，走静态 `share`，避免静默改预算）、`mode="icir_erc"`、
  `min_icir=0.30`、`min_net_excess=0.0`、`overlap_threshold=0.50`、`overlap_pair_cap=0.50`、`output_breadth=100`。
- `[strategies.model_valuation|model_quality|model_momentum]`：**research**，`share=0`，evidence 带 IC/ICIR/NW t/净超额/相关性；
  `[strategies.model_reversal]`：**off**（净超额为负）。`total_budget` 仍为 0.90（只有 `model_alpha` active）。

**3) 修复的四个真 bug（都由 W8 回归测试暴露）**

| bug | 现象 | 修法 |
|---|---|---|
| 归一化破坏上限 | 4 条腿时最大权重 0.4188 > 上限 0.40 | 改为"裁剪→收紧重叠对→再归一化"的迭代投影，并做超限再分配 |
| 重叠对上限不收敛 | 目标上限 0.263，实际 0.397 | 迭代投影 + 不可行检测（`constraint_status`） |
| 静态模式被改写 | 配置 0.9/0.1 被裁剪成 0.667/0.333 | `mode="static"` 走纯过账路径，不加任何约束 |
| `strategy_for_pick` 误认领 | 新增 4 条 model 腿后，"model" 通道的票被归到 `model_valuation` | 只允许 **active** 的模型腿认领生产票 |

**4) 回归测试**：`test/test_sleeve_allocator.py`（7 条：资格门 / 重叠惩罚 / 权重守恒 / 静态兼容 / 端到端 reversal=0 / 不可行检测 / 生产配置守护）
+ 更新 `test/test_strategy_registry.py` 三条断言以反映新生产状态（value_reversal off、多模型腿）。

```
uv run pytest test/test_strategy_registry.py test/test_sleeve_allocator.py -q
→ 24 passed
```

**5) 产线冒烟**：`--stage preselection --force-rebalance` → status completed；
`cn_ensemble_preselected.csv` 8 行、channel 全 model、**sleeve 全 `model_alpha`**（标签修复保持有效）。

**剩余未完成（明确记录）**：把 `sleeve_allocator` 真正接进选股链路——需要让四条家族腿产出**可交易目标权重**
（当前只有 OOS 预测），再由分配器给预算、由组合层合并。这一步依赖"家族腿产出层"的工程化，属于下一阶段工作。

### 8.8 目标权重产出层完成 + 接入 pk 实测通过（2026-10-06 收尾）

**1) 模型持久化**：`train_sleeve_model.py` 现在除了 OOS 预测，还把最终模型落盘
`output/model_scores/sleeve_<name>.txt`（LightGBM booster）+ `sleeve_<name>.meta.json`
（特征清单 / 标签期 / 训练窗口 / IC/ICIR/NW t / 训练时间）。四个模型共约 3.6 MB。

**2) 新增阶段 `sleeve_targets`**（`factor_engine/sleeve_targets.py`）：读长面板最新截面 → 四腿打分 →
可交易性过滤（成交额 ≥2000 万、非一字板）→ 每腿取 `output_breadth`（默认 100）→ 腿内等权/rank 加权 →
乘该腿预算 → 合并 → 单票 ≤5% → 落盘。

实测（as_of **2026-09-30**，候选 3,534 只）：

| sleeve | 预算 | 产出只数 | 权重合计 |
|---|---|---|---|
| model_quality | 0.390 | 100 | 0.390 |
| model_momentum | 0.378 | 100 | 0.378 |
| model_valuation | 0.232 | 100 | 0.232 |
| model_reversal | **0.000**（资格门剔除） | 0 | 0.000 |
| **组合** | — | **292 只** | **1.000000** |

组合特征：最大单票 **0.622%**（远低于 5% 上限）、**8 只被两腿同时持有**（其余 284 只为单腿）——
与相关性矩阵一致（多腿在 top-100 层面也近乎正交）。

**3) 接入 pk 实测通过**：`sleeve_targets` 同时导出 **pk 兼容格式** `output/results_cn/sleeve_preselected.csv`
（`trade_date/stock_code/model_score/target_weight/selection_channel="sleeve"/selection_sleeve=...`）。
把 `[selection].preselection_path` 指向该文件后跑 `--stage pk` → **status ok**，说明现有组合层可以直接消费
多 sleeve 组合，无需改 pk 代码。

> 副作用提示（已处理）：pk 的 `output_dir` 是共享的，用临时配置测试会覆写 `cn_ensemble_selected.csv`；
> 已用默认配置重跑 `--stage pk` 还原生产产物。

**至此，P1.24b 的设计链路全部落地**：规格 → 标签 → 训练（含落盘）→ 组合验收 → 预算分配 → 目标权重 → pk 消费。
默认仍是 `[selection.sleeve_allocator].enabled = false`（研究态），**开闸只需两步**：
① 把 `[selection].preselection_path` 指向 `sleeve_preselected.csv`（或在分配器接入代码里做路由）；
② 观察一个调仓周期的实盘/模拟偏差后再考虑替换现有 `model_alpha` 单腿。

### 8.9 **口径修订（2026-10-06，按账户现实）**：候选 10 只 / 买入 5 只

**问题**：§8.4 得出"宽度 100"，但账户容量上限只有 100~250 万（§6 实测），
实际只能"选 10 只、买 5 只"。宽度 100 与可执行规模矛盾，必须修口径。

**先量清楚"窄宽度到底还剩多少 alpha"**（长样本 2015-2026，20 日调仓，单边成本 0.11%）：

| 口径 | top-3 | **top-5** | top-10 | top-20 |
|---|---|---|---|---|
| combo_equal 净超额 | +0.778% | **+0.719%**（t 2.05） | +0.967% | +1.230% |
| combo_rankmean 净超额 | +0.885% | **+0.809%**（t 1.90） | +0.936% | +1.000% |
| quality 单腿净超额 | +2.929% | **+2.999%**（t 4.78） | +2.602% | — |
| momentum 单腿净超额 | +0.659% | +0.576% | +0.426% | +0.456% |
| valuation 单腿净超额 | — | 负 | — | — |

**三条结论（决定了产出层怎么改）**：

1. **宽度从 3 加到 20，组合净超额只从 +0.72% 升到 +1.23%** —— 窄宽度的代价**不致命**；
   原因是我们已经把分散做在了**腿间**（多 sleeve），而不是腿内的 100 只上。
2. **但必须换口径**：不是"某条腿的 top-5"，而是"**多腿共识排名的 top-5**"。
   单腿 top-5（valuation）为负，多腿共识 top-5 净 +0.72~0.81%、NW t ≈ 1.9~2.1，**过 1.5 门槛**。
3. **质量腿在窄宽度上最强**（top-5 净 +3.0%、t 4.78、换手仅 15.8%）→ 它是 5 只组合里最值得给预算的腿。

**产出层修订（`factor_engine/sleeve_targets.py`）**：

- 不再"每腿各挑 100 只再合并"，改为 **共识分**：每腿先做横截面分位 → 按预算加权平均 → 得全市场共识分；
- `candidate_pool = 10`（候选）→ `holdings = 5`（最终），等权 **20%**；
- **行业分散**：同一行业至多 1 只（窄组合的分散主要来自行业，不是宽度）；行业列取 registry 的
  `industry_l2`（`industry_l1` 存的是分类标准名，是个坑）。

**实测（as_of 2026-09-30）**：

| 项 | 结果 |
|---|---|
| 候选 10 只 | 000989.SZ / 603323.SH / 601838.SH / 002818.SZ / 600919.SH / 002841.SZ / 002925.SZ / 002154.SZ / 601009.SH / 600036.SH |
| **最终 5 只**（等权 20%） | 000989.SZ（医药）· 603323.SH（货币金融）· 002818.SZ（商务服务）· 002841.SZ（电子）· 002154.SZ（纺织服装） |
| 权重 | 合计 1.000000，每只 20% |
| 共识结构 | 5 只全部**被 3 条腿同时选中**（quality + momentum + valuation 的分位都在 0.35~0.39 高位） |

> 行业约束确实生效：601838（银行）与 603323（银行）同行业 → 后者在前，前者被跳过；
> 600919（银行）同样被跳过。最终 5 只分属 5 个不同行业。

## 9. 风险清单与失效监控

| 风险 | 触发信号 | 处置 |
|---|---|---|
| 伪分散（候选域重叠） | 两腿 Jaccard > 0.5 且同时合格 | 重叠惩罚生效；仍超限则合并两腿 |
| 单腿主导 | 单腿风险贡献 > 60% | 触发复审；必要时下调该腿上界 |
| 标签泄漏 | OOS IC 显著高于 IS IC | 立即停用，检查 purge/embargo 与特征时点 |
| 过拟合 | PBO > 0.5 或 DSR < 0 | 拒绝上线，回滚特征 |
| 风格漂移 | size/value 暴露月度漂移 > 0.2σ | 中性化或减预算 |
| 容量超限 | 单笔 > 1% ADV20 的比例 > 20% | 降规模或延长铺单窗口 |
