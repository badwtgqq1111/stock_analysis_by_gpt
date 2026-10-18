# P2.74 · STOCK-JEPA 复现与研究计划

> 输入资料：`docs/reference/stock_jepa/stock_jepa2610.07006v1.pdf`（arXiv 原文，26 页）、
> `stock_jepa.md` 与 `传统因子与深度学习如何结合？…JEPA…pdf`（QuantML 公众号解读，2026-10-12，8 页）。
> 在线核对时间：2026-10-17。

> **规范优先级（2026-10-17 用户确认）**
> 1. 论文是唯一规范：S1/S2/S3 的目标、窗口、损失、读出、评估口径以 arXiv 原文为准；
> 2. 外部仓库（`Jack1320/StockJEPA`、`Gen-Verse/JEPA-Anything`）只作工程/诊断参考，
>    不改变论文语义，不进入主表；
> 3. 任何偏离（数据源、描述符定义、预算、评估窗口）都登记在 Appendix B/C，并在报告中单列。

## 0. 一页结论

**做什么**：在本仓库的 A 股 PIT 面板上复现 STOCK-JEPA 的三段式（先验 → 锚点/修正表征 → 冻结读出），
用与论文同口径的 RankIC 与 top-30 组合检验"潜空间修正"是否比"直接喂上下文"更有增量，
再决定是否作为一条独立模型手臂进入策略池。

**不做什么**：不追论文的绝对收益数字（数据源、股票池、执行假设都不同），不做端到端收益微调，
不把 2024–2026 测试段用于任何调参。

**周期**：4 周（约 15–18 人日）；单人 M5 Max（128GB，MPS）可跑，单次完整训练预估 2–6 小时。

**四道门**：

| 门 | 判据 | 不通过怎么办 |
| --- | --- | --- |
| G0 数据契约 | PIT 审计通过、**38/40 维**有效样本覆盖 ≥95%、无未来函数 | 先补数据，不训练 |
| G1 先验可用 | S1-only 验证期 RankIC ≥ 5%（论文 CN 5.15%） | 检查标签/特征口径 |
| G2 表征有增量 | 完整模型验证 RankIC ≥ 最强基线 + 1pp | 停止，写负结论 |
| G3 测试可交易 | 2024–2026 RankIC ≥ 8%、净 Sharpe ≥ 0.8、MDD 不差于基线 5pp 以上 | 只留研究，不进池 |

## 1. 资料与事实核对

### 1.1 论文在说什么（只保留可执行信息）

- **S1 先验**：40 维日频描述符（28 个股 + 12 市场）→ 多输出 Ridge（α=20）预测 5/21 日的
  收益、波动、最大回撤、相对等权超额共 8 个目标；再把 8 个预测值与 8 个"历史残差尺度"
  拼成 16 维先验。按年扩展窗口拟合，只用"结果窗口在预测块开始前已完整"的样本。
- **S2 表征**：未来 21×40 窗口经目标编码器（EMA，0.996）得到 128 维目标；16 维先验经
  投影器（EMA，0.99）得到 128 维锚点；过去 126×40 经 4 层 Transformer（128 维、8 头、FFN 512）
  得到上下文；上下文 + 停止梯度的锚点 → 256→256→128 的修正预测器。
- **解耦**：锚点损失只更新投影器；修正损失只更新上下文编码器与修正器；锚点在修正分支的
  输入与监督标签里都 detach。修正目标做逐坐标 running mean/var 标准化并 clip ±8。
- **S3 读出**：冻结全部表征，输入 `[历史均值(40); 先验(16); 锚点(128); 修正(128)] = 312`，
  两层 MLP（312→256→1，LayerNorm/GELU/dropout 0.05），SmoothL1 拟合 21 日对数收益。
- **理论**：固定目标编码器时，锚点 = `E[Z|G]`，修正 = `E[Z|H] − E[Z|G]`；
  修正带来的 MSE 降幅恰为 `E||Δ||²`。这是"潜空间正交投影"的保证，**不保证下游经济收益**，
  也不覆盖有限样本/优化误差（论文自己在 Appendix A 的 Scope 里写明）。

### 1.2 论文关键数字（CN ALL，2024-01 至 2026-06，3 seed 均值）

| 模型 | RankIC | 净年化 | 净 Sharpe | 最大回撤 |
| --- | --- | --- | --- | --- |
| FMB-style | 8.76% | 9.51% | 0.67 | -16.36% |
| IPCA | 8.69% | 20.99% | 0.78 | -27.02% |
| MASTER | 9.84% | 41.81% | 1.23 | **-21.46%** |
| **STOCK-JEPA** | **11.22%** | **45.66%** | **1.50** | -31.09% |

美股（top30 − bottom30）：RankIC 12.96%，净年化 68.20%，Sharpe 1.24，MDD -40.12%
（FMB 的 MDD 为 -57.98%，这是论文"尾部风险"论点的主战场）。

**必须注意的三个口径**：

1. 论文的"净年化"是**日收益均值 × 252（算术年化）**，不是复利 CAGR；
2. CN 的最大回撤并不是第一（MASTER -21.46% 优于 JEPA -31.09%），"回撤最小"只在美国成立；
3. 测试期只有约 2.5 年、599 个预测日，且 21 日持仓重叠；3 个 seed 的标准差不是抽样误差，
   显著性要用 Newey-West / 月度块 bootstrap 重算。

### 1.3 在线相关资料与代码可得性

| 资料 | 关系 | 可得性 |
| --- | --- | --- |
| arXiv 2610.07006（2026-10-04） | 论文原文 | 可下载；无官方代码链接 |
| arXiv 2610.09048 *Towards Financial World Modeling*（2026-10-06） | 同一研究圈（Mahns/Balestriero/Levy），Market-1T 数据集 + 18 种编码器训练策略 | 可读；强调"预测指标相近的编码器，市场状态组织方式可能完全不同"——支持我们加表征诊断 |
| I-JEPA（CVPR 2023） / V-JEPA 2（2025–2026） | JEPA 方法源头与工程范式 | `facebookresearch/ijepa`、`facebookresearch/vjepa2` 官方 PyTorch |
| T-JEPA（ICLR 2025）、TS-JEPA（NeurIPS 2024 workshop）、Fin-JEPA（SSRN 6855118） | 论文的 3 个 JEPA 基线 | 论文给出配置；代码需逐篇找 |
| GitHub `Jack1320/StockJEPA` | 名为 StockJEPA 的 notebook | **不是本文实现**：37 维输入、S&P500、下一日 latent 预测 + SIGReg，没有先验锚点/修正分支；只能参考其训练与 walk-forward 写法 |
| MASTER / StockMixer / FactorVAE / IPCA / FMB | 主要基线 | MASTER 等有公开实现；IPCA/FMB 需按论文自写（实现量小） |

**结论：没有可直接复用的官方实现，按论文从零写；JEPA 的 EMA/detach/掩码范式可参考 I-JEPA。**

### 1.4 两个外部仓库的评估（2026-10-17 实读代码与运行输出）

#### A. `Jack1320/StockJEPA` → `JEPAprojV9.ipynb`（MIT）

**它是什么**：一个 S&P 500 的"下一日 latent 预测"notebook，不是本文实现。

- 输入 37 维 = 5 个日频量价特征（收益/振幅/实体/跳空/相对量）+ 32 维日历编码；
- 504 日上下文、1 层因果 Transformer、128 维、4 头；
- 目标：`z_{t+1}` 的 stop-grad 预测 + SIGReg 防塌缩（不是先验锚点/修正）；
- 读出：冻结编码器缓存 + 跨股票注意力 + 月度 walk-forward（2024-08–2025-07，12 折）；
- 股票池 488 只（标普成分，存续偏差），4.26M 观测。

**notebook 里已有的真实结果**（可直接当负面对照）：

| 模型 | 12 个月累计 L/S | Sharpe | 命中率 | 日均 IC |
| --- | --- | --- | --- | --- |
| 该 JEPA | +8.16% | 0.85 | 54.8% | +0.0088 |
| 线性 Ridge（5 特征） | -19.84% | -1.01 | 48.8% | -0.0084 |
| 动量（当日收益） | -15.20% | -0.79 | 48.4% | -0.0083 |
| "Today≈Tomorrow"（预测 0） | +5.97% | 1.57 | 52.4% | N/A |

**参考价值**：

1. **工程模板**：按 `N_DATES × N_STOCKS` 组结构化 batch、冻结编码器缓存、跨股票注意力读出、
   12 折月度 walk-forward，这几段代码可以直接改写进我们的 P2/P3。
2. **SIGReg**：作为 EMA + stop-grad 之外的防塌缩对照，值得进 P4 消融。
3. **负结论**：通用"下一日 JEPA"在美股大盘股上没有跑赢平凡基线，且线性探针 R² 多在
   0.001–0.036（表征弱）。这正好支持本文的动机：**没有先验锚点的 JEPA 不够**；
   我们的第一目标不是"再训一个 JEPA"，而是验证"锚点+修正"的增量。
4. **不要照抄的地方**：
   - "预测 0"基线在所有股票上常数，截面排序退化（实际按代码顺序分组），Sharpe 1.57
     是固定篮子效应，不是可解释的基线；我们改用"上一日收益/随机打破平局/截面均值"。
   - 1 年测试、488 只存续成分股、无费用/停牌/涨跌停，不能作为可交易结论。

**结论**：工程参考 + 负面对照；不作为复现基线，也不证明本文方法无效。

#### B. `Gen-Verse/JEPA-Anything`（Apache-2.0）

**它是什么**：领域无关的 JEPA 框架，核心是 **Orthogonal Predictive Factorization (OPF)**：
把 `d` 维潜状态分解为 `K × r` 个正交预测因子，每个因子一条预测通路，再合成完整状态；
配套一个小型、带类型的 PyTorch 库（`jepa-anything-core`）、审计工具、容量匹配基线和
任务设计脚手架（arXiv 2609.20800）。

**可直接复用的部分**（体量小，可 vendor 并保留版权声明）：

| 模块 | 用途 |
| --- | --- |
| `encoder_variance_loss` / `OnlineVarianceTracker` | 上下文编码器的逐坐标方差下限，防表征塌缩；带 `valid_mask`、Welford 流式统计 |
| `build_capacity_matched_predictors` / `capacity_match_report` | "单个 128 维修正" vs "K×r 多头修正"的**等参数量**对照，避免容量混淆 |
| `factor_activity_loss` / `factor_cross_correlation` | 修正向量的因子活跃度与相关性诊断 |
| `audit_*`（basis/activity/round-trip） | 几何与重构审计；我们只借用 activity/correlation/effective-rank 部分 |
| `StandardJEPABaseline` / `UnconstrainedMultiHeadJEPABaseline` | Ordinary-JEPA 与多头消融的干净基线实现 |
| 设计契约 + 脚手架 + contract tests | P0 的配置校验/接口测试模板 |

**不能直接套用的部分**：

1. OPF 强制 `K×r` 因子**正交且可无损合成**；STOCK-JEPA 的锚点由先验固定，修正只是
   "历史信息带来的增量"，并不要求与锚点向量正交（正交的是信息集，不是向量）。
   把 OPF 的 orthogonality loss 加到 anchor/revision 上会改变论文语义。
2. OPF 的因子是匿名、数据驱动的；我们的锚点有明确金融含义（收益/波动/回撤/超额），
   不能混为一谈。
3. JEPA-Anything 没有金融领域实验；其"10 个动力学任务全胜"不能外推到股票截面。

**结论**：把它当**工具库与诊断标准**，不是模型替代品。真正有价值的扩展是
"把修正向量再分解为 K 个互补因子（价值/质量/动量/资金流）并做等容量对照"，
这属于 P4 之后的可选 v2，不进入本次复现主线。

## 2. 与本仓库的差距

| 能力 | 现状 | 缺口 |
| --- | --- | --- |
| PIT 数据 | `backend/services/pit_data.py`：上市/退市/ST 区间/停牌/指数权重/涨跌停/复权因子 | 财务指标的"公告日 as-of"需再审计 |
| 特征面板 | `assets/data/meta/cn_sleeve_features_long_v4.parquet`：10,465,437 行 × 53 列，2014–2026 | 缺 12 个市场级描述符；需补齐并做 40 维冻结清单 |
| 股票池 | 基础表 5,911 只（含 339 只退市） | 与 Wind 5,669 只的差异要登记 |
| 评估 | `scripts/p2_71_walk_forward.py`：2021H1–2025H2 半年度滚动、严格 PIT | 缺 21 日重叠组合的 NAV/费用引擎 |
| 基线 | LightGBM 生产链、alphaqcm 因子评估 | 缺 FMB/IPCA/MASTER；至少补前两个线性基线 |
| 训练 | LightGBM/Transformer/CNN 管线 | 缺 JEPA 双分支、EMA、掩码、先验 Ridge |
| 算力 | M5 Max / 128GB / MPS；torch 2.12 | 无 8×5090，需要 lite 配置与资源探针 |

## 3. 研究设计

### 3.1 样本与时间

- 股票池：A 股全市场（含退市、ST、停牌），预测日有 126 日历史且未来 21 日窗口完整。
- 主协议（对齐论文）：2014–2015 先验初始拟合；2016–2021 表征/读出训练；2022–2023 验证；
  2024-01 至 2026-06 测试。
- 稳健性协议（对齐本仓库）：`p2_71_walk_forward.py` 的 2021H1–2025H2 半年度窗口，
  每窗只用窗口前的数据训练。
- 边界样本：目标窗口跨训练/验证/测试边界的样本一律剔除；停牌日不伪造价格。

### 3.2 40 维描述符（冻结清单，先写进 config 再训练）

个股 28 维（尽量复用 v4 面板，全部按预测日 as-of）：

`ret_1d, ret_5, ret_20, ret_120, mom_12m_skip_1m, rps_20, rps_60, pos_52w, ma20_dev, ma60_dev,
rev_5d, vol_20, mdd_60, turnover_daily, log_mktcap, circ_ratio, pe_ind_pct, pb_ind_pct,
pe_pb_combo, roe_ind_pct, roa_ind_pct, grossprofit_margin_ind_pct, netprofit_margin_ind_pct,
debt_to_assets_ind_pct, netprofit_yoy, or_yoy, ocfps, listed_days`

市场 12 维：

`idx_ret_1d, idx_ret_5, idx_ret_20, idx_vol_20, xs_dispersion, up_ratio, limit_up_ratio,
turnover_agg, moneyflow_intensity, shibor_3m, usdcnh_ret_5, commodity_ret_5`

处理：按训练集统计量标准化、clip ±8、非有限值置 0；**禁止**使用全样本分位数。

### 3.3 先验（S1）

- 目标：5/21 日累计对数收益、未年化波动、最大对数回撤、相对等权市场超额（8 个）。
- 拟合：按年扩展窗口，Ridge α=20，只用在块开始前结果已完整的样本；每年重拟合一次。
- 输出：8 个标准化预测 + 8 个 `log(residual_scale)` = 16 维；写入
  `output/research/p2_74_stock_jepa/prior/block=YYYY.parquet`，训练时只读不重算。

### 3.4 基线与消融

| 组 | 配置 | 目的 |
| --- | --- | --- |
| 线性 | FMB-style、IPCA | 论文同口径的传统基线 |
| 树模型 | 现有 LightGBM（20 日标签） | 本仓库生产基线 |
| 表征 | S1-only、S2-only、S1+S2、S1+S2+S3 | 复现论文 Table 4 |
| 读出 | R0 `[s;P]`、R1 `+anchor`、R2 `+revision`、R3 `+context` | 复现 Table 5，核心是 R2 vs R3 |
| 机制 | Separate-EMA、Separate-online、Joint-online、Ordinary-JEPA | 复现 Table 6 |
| 稳健 | 费用 5/10/20bp、剔除涨停不可买、按市值/行业中性 | 本仓库增量 |

### 3.5 评价

- 预测：逐日截面 IC / RankIC，均值 + Newey-West t（lag=21）+ 月度块 bootstrap 95% CI。
- 组合：top-30 等权、21 日持有、次日开盘成交、5bp/边；同时报 CAGR 与算术年化。
- 风险：最大回撤、换手、容量（按成交额 1% 估算）、行业/市值暴露。
- 主判据只认一个：**测试期 RankIC 与 R2−R3 的增量**；其余为辅助。

## 4. 实施阶段

### P0 数据契约（已完成，2026-10-17）

- 冻结 40 维清单 + 标签定义 + 样本过滤规则到 `config/p2_74_stock_jepa.toml`。
- 生成 `output/research/p2_74_stock_jepa/`：11,571,958 行特征 + 9,529,849 个连续样本。
- 样本量对齐论文：train 3,877,518（论文 3,883,006）、valid 2,128,153（1,990,842）、
  test 2,903,375（2,965,433）。
- 覆盖：有效样本上 38/40 维 ≥95%；`pe_ttm` 82.5%（亏损股）与 `mom_12m_skip_1m` 94.7%
  （上市不足 252 日）单列，模型时标准化后置 0。
- 补数：2014–2017 月末 `daily_basic` 快照 48 天已用 Tushare 中继补齐（0 错误）。
- 验收：`test/test_p2_74_stock_jepa.py` 3 条通过；明细见
  `output/research/p2_74_stock_jepa/DATA_CONTRACT.md`。
- 待办（P0.5）：随机抽 20 个样本人工核对日期边界；补 `pit_data` 的 ST/停牌标记到样本索引
  （当前只按"是否有 bar"判连续，ST 状态留给评估期过滤）。

### P1 先验与张量（3 天）

- `scripts/p2_74_prior.py`：8 目标 → Ridge → 16 维先验；输出分块缓存与拟合报告。
- `scripts/p2_74_panel.py`：把面板转成按股票连续的 float32 memmap（约 2.9GB），
  提供 `(code, date) → 126×40` 与 `→ 21×40` 切片。
- 验收：G1（S1-only RankIC ≥5%）；先验残差尺度分布合理；无 NaN。

### P2 表征训练（5 天）

- `scripts/p2_74_jepa_train.py`：双优化器、EMA、detach、掩码、修正标准化；
  合成数据先跑通"慢/快状态恢复"（复现论文 Appendix K 的方向，不追数值）。
- batch 采用 `N_DATES × N_STOCKS` 的结构化采样（参考 `JEPAprojV9`），保证每个 batch
  都有可计算的截面结构；SIGReg 作为 `--anti-collapse sigreg|ema|none` 的可选对照。
- 资源探针：记录 s/step、峰值内存；MPS 不可用则自动降级到 lite 配置。
- 验收：G2；`Separate-EMA` 的验证损失优于 `Joint-online` 与 `Ordinary-JEPA`；
  锚点分支对修正损失无梯度（单测）。

### P3 读出与评估（4 天）

- `scripts/p2_74_readout.py`：冻结表征，312→256→1 MLP，日期分组 batch。
- 读出消融（可选）：跨股票注意力读出（参考 `JEPAprojV9` cell 6），检验"截面交互"是否
  比逐股票 MLP 更适合 top-30 排序；主表仍以论文 MLP 为准。
- `scripts/p2_74_eval.py`：IC/RankIC、组合 NAV、费用、限额、回撤、bootstrap。
- 基线：FMB/IPCA 自写；LightGBM 直接复用生产预测；平凡基线必须包含
  "上一日收益"、"截面均值（随机打破平局）"，**不要**用常数 0 预测（排序退化）。
- 验收：G3；产出 `report.md` + 图（NAV、RankIC 时序、回撤）。

### P4 消融与稳健性（4 天）

- 跑 Table 4/5/6 的对应配置（3 seed）；费用与可交易性敏感性；
- 表征诊断：修正向量的有效秩、截面相关性、事件日位移（论文 Fig.3/4 的简化版）。
- 借用 `jepa-anything-core` 的 `encoder_variance_loss`、`factor_activity_loss`、
  `factor_cross_correlation` 与 `capacity_match_report`：前者做防塌缩对照，后两者做
  "修正 vs 等宽上下文"的等容量比较（避免 R2−R3 被参数量解释）。
- 可选 v2（仅当主线通过）：用多头把修正分解为 K 个互补因子，做等容量/正交性对照，
  但**不把 OPF 的正交损失加到锚点分支**。
- 验收：主结论在 2/3 seed 与两套窗口下方向一致；否则降级为研究结论。

### P5 集成决策（3 天）

- 若 G3 通过：作为 `stage=research` 的独立模型手臂接入每日打分（只产分数，不直接下单），
  影子运行 4 周；监控 RankIC、换手、回撤、与现有策略相关性。
- 若不通过：写负结论与失败原因（数据/实现/市场差异），保留代码与产物。

## 5. 工程规范

```
config/p2_74_stock_jepa.toml
scripts/p2_74_prior.py
scripts/p2_74_panel.py
scripts/p2_74_jepa_train.py
scripts/p2_74_readout.py
scripts/p2_74_eval.py
scripts/p2_74_ablate.py
test/test_p2_74_stock_jepa.py
output/research/p2_74_stock_jepa/
  prior/  checkpoints/  embeddings/  eval/  report.md  run_manifest.json
```

关键超参（论文值，lite 值另记）：

| 项 | 论文 | 本机 lite |
| --- | --- | --- |
| d_model / layers / heads | 128 / 4 / 8 | 96 / 3 / 6 |
| 上下文 / 目标窗口 | 126 / 21 | 同 |
| batch | 256 | 128 |
| 表征训练 | 4 epoch × ≤1000 step | 3 × 600 |
| 学习率 / wd / clip | 1e-4 / 0.01 / 1.0 | 同 |
| EMA（target / anchor） | 0.996 / 0.99 | 同 |
| 读出 | 6 epoch × ≤1000 step | 4 × 600 |

所有 run 写 `run_manifest.json`：git sha、配置哈希、数据版本（parquet mtime）、seed、耗时、峰值内存。

## 6. 泄漏与正确性测试（必须全部通过）

1. 目标窗口严格晚于上下文（逐样本断言 `ctx_end < tgt_start`）。
2. 先验只用块开始前已完成的样本（构造未来数据篡改，先验不得变化）。
3. 锚点分支：`revision_loss.backward()` 后投影器梯度为 0。
4. EMA 模块无梯度、参数确实按 decay 更新。
5. 掩码只在训练期生效；推理期恒等。
6. 标准化统计只来自训练集；验证/测试不更新 running stats。
7. 组合回测：信号日 t、成交日 t+1 开盘；停牌/涨停不可买；退市按最后价格清算。
8. 同一配置重复运行，固定 seed 下指标可复现（容差 1e-6）。
9. 容量匹配：`revision(128)` 与 `context(128)` 的读出参数量必须完全相等
   （用 `capacity_match_report` 断言），否则 R2−R3 不成立。
10. 平凡基线：常数预测必须随机打破平局；断言其多头组合不是"按代码顺序的固定篮子"。

## 7. 风险与对策

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| 没有官方代码，复现偏差 | 结论不可比 | 先复现合成数据（慢/快状态），再上真实数据；所有偏离登记在 Appendix C |
| 财务/行业分位未来函数 | 虚高 IC | P0 审计 + 篡改测试；财报用公告日 |
| 21 日重叠导致 Sharpe 虚高 | 误判可交易 | Newey-West / 月度块 bootstrap；报告有效样本数 |
| 测试期短、regime 少 | 过拟合测试集 | 只用一次测试；另跑 2021H1–2025H2 滚动窗口 |
| 算力不足 | 训练跑不完 | lite 配置 + 资源探针；必要时租 1×A100 跑 3 seed |
| 交易约束（涨停/停牌/容量） | 实盘不可执行 | 用 PIT 涨跌停与停牌过滤；加 10/20bp 与容量敏感性 |
| 多重比较 | 假阳性 | 预注册主指标；所有配置全报，不挑 seed |
| 外部 notebook 的"预测 0"基线 | 误以为随机基线很强 | 只采用"上一日收益/随机平局"，并在报告里注明常数基线的退化 |
| OPF 正交损失误用 | 改变锚点语义 | 只借用诊断/容量工具；正交损失仅用于 v2 的因子分解，不用于 anchor/revision |

## 8. 时间表与算力

| 周 | 内容 | 产出 | 人日 |
| --- | --- | --- | --- |
| W1 | P0 + P1 | 数据契约、样本索引、先验缓存、G1 | 5 |
| W2 | P2 | 训练脚本、合成数据验证、首个完整 run | 5 |
| W3 | P3 | 读出、基线、测试集报告、G3 | 4 |
| W4 | P4 + P5 | 消融、稳健性、决策 memo | 4 |

算力：MPS 单次表征训练预估 2–6h（lite 更快）；3 seed × 2 协议约 20–40 GPU·h；
先验与读出各 <1h。若 MPS 吞吐低于 5 step/s，改用云 GPU。

## 9. 交付物

1. 本计划（本文档）。
2. `output/research/p2_74_stock_jepa/report.md`：主表（RankIC/净收益/Sharpe/MDD）、
   消融、稳健性、与论文差异登记。
3. 可复现命令与 `run_manifest.json`。
4. 决策：进策略池（影子）或研究归档。

## 附录 A · 论文超参速查

- Ridge：α=20；输入 clip ±8；残差尺度下限 1e-4；预测块按年扩展。
- Transformer：4 层、8 头、FFN 512、dropout 0.1、summary token、位置编码。
- 掩码：连续 5–21 天块，直到 ≥30% 上下文被掩；特征组独立置零 p=0.25（6 组）。
- 修正标准化：前 100 次更新用 batch 统计，之后 EMA 0.99；clip ±8。
- 优化：两个 AdamW，lr 1e-4，wd 0.01，clip 1.0，bf16，batch 256。
- 读出：LayerNorm(312)→312→256→GELU→dropout 0.05→1；SmoothL1；6 epoch。
- 组合：top-30 等权；21 日持有；次日开盘；5bp/边；不建模冲击、借券费、保证金。

## 附录 B · 与论文的已知差异（预登记）

1. 数据源：Wind/Norgate → 本仓库 Tushare/akshare/巨潮；股票数与退市处理不同。
2. 40 维描述符：论文未给完整字段名，本文档的清单是**我们的定义**，不是论文原表。
3. 市场级 12 维：论文只给类别，我们选用本地可得的 12 个。
4. 训练预算：8×5090 → 单机 MPS + lite 配置；步数与模型宽度可能下调。
5. 评估：论文 2024–2026 单段测试；我们额外跑 2021H1–2025H2 滚动窗口。
6. 交易约束：论文未建模涨跌停/停牌/容量；我们加入 PIT 过滤与费用敏感性。

## 附录 C · 外部仓库采用清单

| 仓库 | 许可 | 采用 | 不采用 |
| --- | --- | --- | --- |
| `Jack1320/StockJEPA`（JEPAprojV9.ipynb） | MIT | 结构化 batch、冻结缓存、跨股票注意力读出、月度 walk-forward、SIGReg 对照 | 37 维特征/504 日/1 层结构（非本文）；常数 0 基线；488 只存续股票池 |
| `Gen-Verse/JEPA-Anything` | Apache-2.0 | `encoder_variance_loss`、`OnlineVarianceTracker`、`build_capacity_matched_predictors`、`capacity_match_report`、`factor_activity_loss`、`factor_cross_correlation`、audit 的 activity/correlation 部分、设计契约模板 | OPF 正交损失（不用于 anchor/revision）；匿名因子语义；其非金融领域的性能结论 |

引用方式：若 vendor 代码，保留原 LICENSE 与文件头版权声明，并在 `run_manifest.json`
记录来源 commit。

