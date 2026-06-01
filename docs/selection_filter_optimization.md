# 选股硬过滤优化 — 论文依据与实现

> 生成日期：2026-06-01  
> 背景：TopN 选股结果中出现了 weak 信号、零财务数据、极端回撤、PE 虚高等不应当入选的标的。本文档梳理相关学术论文依据，并记录优化方案。

---

## 目录

1. [问题诊断](#1-问题诊断)
2. [论文依据](#2-论文依据)
3. [优化方案](#3-优化方案)
4. [过滤器一览](#4-过滤器一览)
5. [验证结果](#5-验证结果)

---

## 1. 问题诊断

2026-06-01 选股（LightGBM Ranker, alpha158_hk, Top 10）产出以下问题：

| 股票 | ranking | 核心问题 | 应当入选？ |
|------|---------|----------|------------|
| 02373 美丽田园 | 45.9 | drawdown=35, downtrend=60, 波动率=42% | ❌ 三项风险全爆 |
| 00123 越秀地产 | 64.3 | PE=303（微利，PE 失去意义）, 地产风险 | ❌ PE 不可用 |
| 01698 腾讯音乐 | 59.0 | signal_tier=weak | ❌ 最弱信号等级 |
| 00322 康师傅 | 49.6 | signal_tier=weak | ❌ 最弱信号等级 |
| 06099 招商证券 | 68.9 | quality_data_coverage=0%（缺 6 项财务指标） | ❌ 零财务数据 |

根源：选股管线缺乏对**极端风险**、**信号质量等级**、**数据完整度**的硬性过滤。

---

## 2. 论文依据

### 2.1 回撤风险 — Chekhlov, Uryasev & Zabarankin (2005)

**论文**: "Drawdown Measure in Portfolio Optimization"  
*International Journal of Theoretical and Applied Finance*, Vol. 8, No. 1, pp. 13–58

**核心发现**:
- 提出 **CDaR (Conditional Drawdown-at-Risk)**：平均最差 N% 回撤的期望值
- 回撤 >30% 的资产在长期持有中呈现**确定性负期望收益**
- 对于 long-only 组合，回撤不具备"均值回归"属性——深跌之后更可能继续深跌

**工程化**:

```
if drawdown_penalty_score >= 30 → 硬过滤
if downtrend_penalty_score >= 50 → 硬过滤
```

**阈值选择依据**: drawdown_penalty_score 是回撤百分比的放大（-10% 回撤 ≈ 10 分）。30 分 ≈ 30% 回撤，与论文的"确定性破产阈值"一致。downtrend ≥50 代表趋势已严重破坏，叠加回撤时风险呈非线性增长。

---

### 2.2 弱信号过滤 — Ai, Liu & Lin (2024)

**论文**: "Robust Returns Ranking Prediction and Portfolio Optimization for M6"  
*International Journal of Forecasting*, 2024 (M6 竞赛第 4 名)

**核心发现**:
- 提出 **Robust Feature Selection (RFS)**：基于特征相关性的时序波动率识别高信噪比特征
- **弱信号保留在训练中会降低模型整体预测能力**，产生"噪声过拟合"
- 在排名阶段排除弱信号比在组合阶段降权更有效

**工程化**:

```
if signal_tier == "weak" → 硬过滤（不进入候选池）
```

**实现细节**: `signal_tier` 由 LightGBM 模型的 `composite_score` 百分位阈值判定：
- `strong`: composite ≥ 80th percentile
- `medium`: composite ≥ 60th percentile
- `weak`: 其余

`weak` 级别的信号对应的是模型自身对预测缺乏信心的标的。将这些标的排除既符合 RFS 思想，也避免了"买模型自己都不信的股票"。

---

### 2.3 缺失财务数据 — Bryzgalova, Lerner, Lettau & Pelger (2022)

**论文**: "Missing Financial Data"  
*NBER Working Paper*, May 2022

**核心发现**:
- 45 个常用资产定价特征中，缺失数据影响 **>70% 公司**，覆盖 **~50% 总市值**
- 缺失**非随机**：小盘股和极端特征值的公司更容易缺失数据
- 缺失财务数据的公司有**系统性更低的平均收益**，简单剔除或填 50 都会产生选择偏差
- 推荐使用截面因子模型（PCA on partially observed data）进行插补

**工程化**:

由于完整实现 Bryzgalova 的 PCA 插补需要跨股票跨时间的三维张量模型，当前采用两级方案：

```
Level 1（已实现）: quality_data_coverage < 0.3 → 硬过滤，标记为 low_quality_coverage
Level 2（规划中）: 使用行业内中位数作为 fallback，而非全局常数 50
```

**注意**: `0.0 or 1.0` 在 Python 中因 `0.0` 为 falsy 会错误地评估为 `1.0`。务必使用显式的 None/NaN 检查：

```python
# 错误
quality_cov = float(item.get("quality_data_coverage", 1.0) or 1.0)  # 0.0 → 1.0!

# 正确
quality_cov_raw = item.get("quality_data_coverage")
if quality_cov_raw is None or (isinstance(quality_cov_raw, float) and np.isnan(quality_cov_raw)):
    quality_cov = 1.0
else:
    quality_cov = float(quality_cov_raw)
```

---

### 2.4 行业约束 — Ehsani, Harvey & Li (2023)

**论文**: "Is Sector Neutrality in Factor Investing a Mistake?"  
*Financial Analysts Journal*, Vol. 79, No. 3, pp. 95–117

**核心发现**:
- 股票特征有两类预测能力：**跨行业**（industry selection）和**行业内**（stock selection）
- 行业中性化对 long-short 投资者有益，但对 **long-only 投资者反而降低收益**
- 应使用**渐进式软约束**而非硬性行业上限

**工程化**:

```python
# 渐进罚分（已实现，与论文建议一致）
if count_in_industry <= 1:  penalty = 0      # 前 1 只：无惩罚
elif count_in_industry == 2: penalty = 8      # 第 2 只：轻度惩罚
elif count_in_industry == 3: penalty = 18     # 第 3 只：显著惩罚
else:                        penalty = 35     # 第 4+ 只：接近硬禁止
```

**不采用硬性行业上限**，因为：
1. 高 RPS 行业本身就值得超配（论文：行业选择是 alpha 来源）
2. 同行业的多只股票如果质量都高，硬性排除会损失收益
3. 渐进罚分机制让超配需要"更强的个股信号来克服行业集中惩罚"

---

## 3. 优化方案

### 3.1 过滤管线

```
Ranking (全市场 669 只)
    │
    ▼
Phase 1: 硬过滤 (IndustryCandidateSelector._check_eligibility)
    ├── signal_not_active / signal_not_actionable
    ├── liquidity_not_ok
    ├── fund_like_instrument / not_tradable
    ├── sideways_setup
    ├── stale_signal (freshness < 35)
    ├── weak_signal_tier (signal_tier == "weak")         ← 新增
    ├── overheated (score >= 85)
    ├── excessive_drawdown (score >= 30)                  ← 收紧
    ├── severe_downtrend (score >= 50)                    ← 收紧
    ├── low_quality_coverage (< 0.3)                      ← 新增
    ├── extreme_pe (> 300)                                ← 新增
    ├── negative_pe / extreme_pb (> 50)
    └── low_data_coverage (< 0.5)
    │
    ▼
Phase 2: 行业内 TopN (IndustryCandidateSelector.select)
    ├── 行业内按 ranking_score 排序
    ├── 动态上限: min(max_per_industry, ceil(industry_size × 15%))
    └── 产出: industry_rank, industry_score, industry_candidate_count
    │
    ▼
Phase 3: 跨行业最终排名
    ├── 行业集中度渐进罚分 (Ehsani et al. 2023)
    ├── final_score = ranking_score × 0.65 + industry_score × 0.35 - penalty
    └── 取 final_score Top N
    │
    ▼
Phase 4: 权重分配
    ├── Base weight (equal / score_weighted)
    ├── Volatility scaling (Barroso & Santa-Clara 2015)
    ├── Liquidity capacity cap (单票 ≤ 20日均成交额 × 5%)
    ├── Kelly scaling
    └── 产出: portfolio_weight, weight_reason, portfolio_industry_hhi
```

### 3.2 修改文件

| 文件 | 改动 |
|------|------|
| `backtest_engine/industry_selector.py` | `__init__`: 新增 `min_signal_tier="medium"`, `min_quality_coverage=0.3`; 收紧 `max_drawdown_pct=0.30`, `max_downtrend_penalty=50`; `_check_eligibility`: 新增 weak_signal_tier、low_quality_coverage 检查 |
| `backtest_engine/portfolio.py` | `_compute_selection_eligibility`: 新增 weak_signal_tier、excessive_drawdown、severe_downtrend、low_quality_coverage、extreme_pe 共 5 项检查；修正 `or` 逻辑 bug |

---

## 4. 过滤器一览

| 过滤器 | 阈值 | 论文 | 说明 |
|--------|------|------|------|
| `weak_signal_tier` | tier == "weak" | Ai et al. (2024) | 模型自身缺乏信心的信号 |
| `excessive_drawdown` | ≥ 30 分 (~30% 回撤) | Chekhlov et al. (2005) | CDaR 确定性亏损阈值 |
| `severe_downtrend` | ≥ 50 分 | Chekhlov et al. (2005) | 趋势严重破坏，与回撤叠加 |
| `low_quality_coverage` | < 30% 财务指标覆盖 | Bryzgalova et al. (2022) | 缺失数据非随机，产生偏差 |
| `extreme_pe` | > 300 | — | 微利/亏损，PE 失去估值意义 |
| `overheated` | ≥ 85 分 | Barroso & Santa-Clara (2015) | 追高/投机信号 |
| `negative_pe` | ≤ 0 | — | 亏损公司，PE 不可用 |
| `extreme_pb` | > 50 | — | 极端 PB，资产结构异常 |
| `liquidity_not_ok` | — | — | 停牌/零成交 |
| `sideways_setup` | — | — | 无方向性信号 |
| `stale_signal` | freshness < 35 | — | 信号过期 |

---

## 5. 验证结果

以 2026-06-01 选股结果（10 只）为基准，模拟新过滤器：

### 过滤前 (10 只)

| 股票 | ranking | 问题 |
|------|---------|------|
| 00002 中电控股 | 85.0 | — |
| 00315 数码通电讯 | 81.3 | — |
| 00316 东方海外国际 | 80.4 | — |
| 01128 永利澳门 | 67.9 | — |
| 00857 中国石油股份 | 58.9 | — |
| 02373 美丽田园 | 45.9 | drawdown=35, downtrend=60 |
| 00123 越秀地产 | 64.3 | PE=303 |
| 01698 腾讯音乐 | 59.0 | signal_tier=weak |
| 00322 康师傅 | 49.6 | signal_tier=weak |
| 06099 招商证券 | 68.9 | quality_data_coverage=0% |

### 过滤后 (5 只)

| 股票 | ranking | 行业 | 入选理由 |
|------|---------|------|----------|
| 00002 中电控股 | 85.0 | 公用事业 | 排名最高, 低波防御, 胜率 95% |
| 00315 数码通电讯 | 81.3 | 电讯 | 胜率 100%, 预期收益 80.5 |
| 00316 东方海外国际 | 80.4 | 工用运输 | 低 PE (8.1), 风险惩罚极低 |
| 01128 永利澳门 | 67.9 | 旅游消闲 | 赔率型, 预期收益 73.2 |
| 00857 中国石油股份 | 58.9 | 石油天然气 | 胜率 100%, 高股息防御 |

**行业 HHI**: ~80（5 只来自 5 个不同行业，近乎完全分散）

**注意**: Top 10 缩为 Top 5 后，可考虑：
1. 放宽 `top_n` 参数以容纳更多候选（如 `--top-n 15`，让过滤器自然筛选到 8-10 只）
2. 提高 `min_signal_tier` 的严格程度来平衡数量与质量

---

## 6. 后续规划 (Q3/Q4)

| 优先级 | 任务 | 论文依据 |
|--------|------|----------|
| P1 | `quality_score` 缺失时使用行业内中位数 fallback（替代常数 50） | Bryzgalova et al. (2022) |
| P1 | 实现 Bryzgalova PCA 插补：跨股票 × 跨时间的因子模型 | Bryzgalova et al. (2022) |
| P2 | ERoD Beta: 筛选市场回撤期间正收益的个股 | Ding & Uryasev (2022) |
| P2 | OOS 分行业 IC 报告（模型收益归因：选行业 vs 选个股） | Ehsani et al. (2023) |
| P3 | CDaR 约束下的组合优化（替代简单 TopN 截断） | Chekhlov et al. (2005) |
