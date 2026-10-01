# 为什么 Alpha101 这类基础因子权重低？是"量纲差太多"吗？

编写：2026-09-26。证据来自 2026-09-24 面板（5,208 只 × 683 列）与当前 LightGBM 模型（1,284 列）。

## 0. 结论

**不是量纲问题。** 两条硬证据：

1. **我们的入模特征已经同量纲**：LightGBM manifest 的预处理是 `qlib_robust`，阶段为
   `per_date_winsorize_robust_zscore_clip_fill` —— 每个交易日、每一列都做了 winsorize + 稳健 z-score + clip + 缺失填 0。
   也就是说模型看到的每一列都是"当日横截面 z 分数"。
2. **GBDT 对单调变换不变**：树只在阈值上分裂，把某一族整体乘 1e6 不会改变树结构（只会改变阈值位置）。

真正的原因有四条，按影响排序：**冗余 > 信息量 > 覆盖/掩码 > 归因稀释**。

## 1. 证据一：族内相关 vs 族外最大相关（9/24 面板，逐日 z 化后）

| 族 | 列数 | 族内平均 \|corr\| | **与族外最大 \|corr\| 中位** | gain 占比（当前模型） |
|---|---|---|---|---|
| valuation/质量 | 17 | 0.239 | **0.204** | **27.9%** |
| Alpha158/其他 | 170 | 0.277 | 0.777 | 19.7% |
| GTJA191 | 191 | 0.223 | 0.721 | 13.5% |
| vol_ | 17 | 0.387 | n/a | 7.8% |
| moneyflow | 52 | 0.207 | 0.345 | 6.0% |
| TA-Lib | 35 | 0.376 | **0.990** | 5.9% |
| Alpha101 | 101 | 0.208 | **0.552** | 3.5% |
| pv_ | 18 | 0.290 | 0.835 | 1.8% |
| liquidity | 5 | 0.382 | **1.000** | 3.5% |
| vr_ / tr_ | 12 / 12 | 0.274 / 0.261 | 0.825 / 0.829 | 1.1% / 2.4% |
| financial | 6 | 0.093 | 0.224 | 2.8% |

**读法**：gain 占比高的族（valuation/质量）恰恰是**最独特**的（族外最近邻只有 0.204）；
而 TA-Lib（0.990）、liquidity（1.000）、pv_（0.835）几乎与别的族**完全重复**。
Alpha101 的每一列，平均都有一个族外列与它相关 **0.55**；最极端的几位：

| 与 Alpha101 最相关的族外列 | \|corr\| |
|---|---|
| `GTJA013_clean` | **0.823** |
| `GTJA041_clean` | **0.823** |
| `moneyflow_dc_amount_scale_clean` | 0.755 |

即：部分 Alpha101 因子与 GTJA 因子**是同一个信号**（0.82 相关），树只需分裂一次，gain 全记在其中一列。

## 2. 证据二：逐列信息量（59 个交易日，2026-06~08）

| 族 | 日均 \|IC\|（被使用列） |
|---|---|
| pv_价量 | 0.144 |
| moneyflow | 0.101 |
| flow_second_wave | 0.090 |
| financial | 0.046 |
| valuation/质量 | 0.037 |
| GTJA191 / Alpha101 / Alpha158 / TA-Lib | **0.013~0.014**（接近单因子噪声底） |

## 3. 证据三：死列大多是"掩码"而不是因子

当前 1,284 列中 582 列 gain=0：**446 列是 `*_is_missing` 掩码（覆盖率高的因子，掩码恒为常量）**，
只有 **136 列是真正的因子值列**（占 649 个值列的 21%）。→ 先治覆盖率/掩码，能省掉约 1/3 的列。

## 4. 证据四：相关特征会把归因摊薄（文献）

树模型在同一"信号簇"里只会挑一列分裂，gain/SHAP 便集中到该列；这不是"那族没用"，
而是**归因在相关特征下天然不可靠**（Strobl 等 2007；Lundberg & Lee 2017；Covert 等 2020）。

## 5. 什么情况下"量纲"真的会伤人

1. **未标准化的线性模型 / 神经网络**（Lasso、Ridge、MLP、Temporal Transformer）：必须逐列标准化，否则量纲大的特征会主导。
   我们的面板已经逐日 z 化，所以这条在**当前 LightGBM 上不成立**（Transformer 同样吃 z 化后的面板）。
2. **我们的启动门禁**：`k_dist`（对数收益差）、`k_volume`（无量纲量比）、`k_flow`（截面 z）按 **1.0/0.6/0.4/0.6** 手设权重相加——
   这才是真正的"量纲混用 + 权重无依据"，也是它无法表达"联合状态"的根本原因（见 `continuous_state_and_phase.md`）。
3. 跨族比较原始值时（例如拿市值（元）与 PE 混算距离/相似度）—— 应统一到 z 或分位。

## 6. 因子设计类文献（本轮 OpenAlex `title.search` 核验）

| 主题 | 文献 |
|---|---|
| 因子库设计 | Kakushadze (2016) *101 Formulaic Alphas*, Wilmott, DOI 10.1002/wilm.10525（cites≈66）；Yang, Liu, Zhou et al. (2020) *Qlib: An AI-oriented Quantitative Investment Platform*, arXiv:2009.11189（≈30，Alpha158/360 的来源） |
| 因子筛选与多重检验 | Harvey, Liu, Zhu (2015) *…and the Cross-Section of Expected Returns*, RFS, DOI 10.1093/rfs/hhv059（cites≈2169） |
| 联合检验 / 去冗余 | Feng, Giglio, Xiu (2020) *Taming the Factor Zoo*, JF, DOI 10.1111/jofi.12883（≈746） |
| 收缩优于稀疏 | Kozak, Nagel, Santosh (2019) *Shrinking the cross-section*, JFE, DOI 10.1016/j.jfineco.2019.06.008（≈787） |
| 非参数特征筛选（自适应组 lasso） | Freyberger, Neuhierl, Weber (2019) *Dissecting Characteristics Nonparametrically*, RFS, DOI 10.1093/rfs/hhz123（≈643） |
| 最大相关-最小冗余（mRMR） | Peng, Long, Ding (2005) *Feature selection based on mutual information…*, TPAMI, DOI 10.1109/tpami.2005.159（cites≈10693） |
| 归因在相关特征下不可靠 | Strobl et al. (2007) *Bias in random forest variable importance measures*, BMC Bioinformatics, DOI 10.1186/1471-2105-8-25（≈3769）；Lundberg & Lee (2017) *A Unified Approach to Interpreting Model Predictions*, arXiv:1705.07874（≈7625）；Covert, Lundberg, Lee (2020) *Understanding Global Feature Contributions…*, arXiv:2004.00668（≈206） |
| 因子相关性结构 | Ehsani & Linnainmaa (2022) *Factor Momentum and the Momentum Factor*, JF, DOI 10.1111/jofi.13131（≈233） |
| ML 全量特征 vs 线性基线 | Gu, Kelly, Xiu (2020) *Empirical Asset Pricing via Machine Learning*, RFS, DOI 10.1093/rfs/hhaa009（≈2514，前轮已核验） |

## 7. 处置建议（可执行）

1. **不按 gain 名次删列**：改用**相关性聚类 + 每簇保留一列**（mRMR / 组 lasso 思路），保留簇代表的因子名与经济含义。
2. **族级消融（M1）**：全量 vs 去 Alpha101 vs 去 GTJA191 vs 去 Alpha158，比样本外净收益——只有消融能回答"这一族有没有增量"。
3. **先治覆盖率/掩码**：446 个常量掩码列直接剔除（-35% 列数，几乎零信息损失）。
4. **IC/覆盖率只做监控**，不做入模门槛（P1.22 §15 口径）。
5. **顺序上先修标签与作用域**：刚测出 1 周口径下所有臂都不优于随机（`output/research/weekly_validation/REPORT.md`），
   说明当前主要矛盾不在"哪族因子权重高"，而在"标签/作用域/成本是否对齐"。
