# 因子验证四件套 TODO

> 目标：基于 `wiki/因子验证四件套.pdf` 原文，整理出本文的方法论、关键阈值、对本项目现有 `factor_validation/` 的映射，以及下一步可落地任务。
>
> 当前日期：2026-07-03

## 来源

| 字段 | 值 |
|---|---|
| PDF | `stock_analysis_by_gpt/wiki/因子验证四件套.pdf` |
| 标题 | 因子验证四件套 |
| 收藏时间 | 2026-07-03 |
| 原文链接 | `https://mp.weixin.qq.com/...`（PDF 中已省略完整链接） |
| 文档来源 | 金山收藏助手生成，目录为“我的云文档/应用/金山收藏助手” |
| 页数 | 16 |

IMA 个人知识库也能搜到同名资料，`media_id=wechatarticle_4f62dc479775d00291fe84a82211db8b_330792aa96c62cd851e03e062956b739`，但 IMA OpenAPI 只返回标题和元数据；这次整理以本地 PDF 原文为准。

## 一句话结论

原文的核心不是“IC 不好就淘汰”，而是把因子拆成不同问题来验证：

```text
ADF：先排除非平稳导致的伪相关
IC / 分组回测：看连续幅度有没有方向感
PRF：看离散触发事件是否比基线更准
事件研究：看触发后收益是否持续、显著
Spearman 冗余：看它进组合后是不是重复信息
```

文章虽然叫“四件套”，实际报告模板是 `[A] ADF + [B] IC + [C] PRF + [D] Event Return + [E] Spearman Redundancy` 五个模块。更准确的理解是：ADF 是体检项，IC/PRF/事件研究/Spearman 是四件套主体。

## 原文案例

原文用 ATR 突破因子做案例，展示同一个因子在连续路径和离散路径下会得出完全不同的结论。

### 反直觉点

连续版 `atr_ratio`：

| 指标 | 结果 | 判断 |
|---|---:|---|
| 10D IC | `+0.070` | 正向但很弱 |
| 最佳 IC_IR | `0.188` | 低于 `0.5` 阈值 |
| 全样本 Spearman rho | `-0.063` | 与月度 IC 方向分歧 |

离散版 ATR 突破信号：

| 指标 | 结果 |
|---|---:|
| N=10 Precision | `61.86%` |
| N=10 基线 | `56.44%` |
| Lift | `+5.42 ppt` |
| 事件研究 T+10 平均收益 | `+3.14%` |
| T+10 显著性 | `p < 1e-37` |

结论：连续幅度信息不稳定，不代表“突破事件”没有价值。IC 问的是“因子值大小和未来收益排序是否同向”，PRF 问的是“信号触发后上涨频率是否高于基线”。这是两个问题。

## 五个模块

### A. ADF：平稳性体检

ADF 用来排除非平稳序列带来的伪相关。原文中 `atr_ratio` 是比率型变量，ADF 统计量约 `-7.90`，p 值约 `4.15e-12`，通过 `p < 0.05` 门槛。

落地判断：

| 项 | 标准 | 不通过怎么办 |
|---|---|---|
| ADF p-value | `< 0.05` | 重新设计因子，做差分、归一化或改成事件信号 |

对本项目的启发：

- `factor_validation/` 当前没有 ADF 检验。
- ADF 应作为连续型因子的预检查，不应该替代收益验证。
- 对价格、估值、财务水平类因子尤其要做平稳性标注。

### B. IC 连续路径：方向感

IC 用来验证连续因子值高低是否能对应未来收益高低。原文还先跑了五分位分组回测，看收益是否呈现单调阶梯。

原文的 ATR 连续路径结果：

| 项 | 结果 |
|---|---:|
| Q1 未来 10 日收益 | `+2.30%` |
| Q2 | `+2.58%` |
| Q3 | `+2.38%` |
| Q4 | `+1.36%` |
| Q5 | `+1.28%` |
| Q5-Q1 多空 | `-1.01%` |
| 简单年化 | `-12.8%` |
| Sharpe | `-0.44` |

IC 汇总：

| horizon | IC 均值 | IC_IR | 判断 |
|---|---:|---:|---|
| 5D | `+0.068` | `0.174` | 不达标 |
| 10D | `+0.070` | `0.188` | 不达标 |
| 20D | `+0.013` | `0.037` | 几乎为零 |

原文给出的连续路径判定：

| 维度 | 指标 | 标准 | 判定 |
|---|---|---|---|
| ADF 平稳性 | p 约 `4.15e-12` | `< 0.05` | 通过 |
| IC 方向 | 10D `+0.070` | `IC > 0` | 正向 |
| IC_IR 稳定性 | `0.188` | `>= 0.5` | 不达标 |
| 全样本 rho 同向 | `-0.063` | 与 IC 同向 | 分歧 |

对本项目的启发：

- 本项目已实现 `calculate_ic_by_date()`、`summarize_ic()`、分组收益、多空价差。
- 现有 `factor_scorecard` 应增加 `ic_path_status`：pass / weak / fail / switch_to_discrete。
- 月度 IC 与全样本 rho 分歧时，不应直接判废；要标记为“市场状态依赖”。

### C. PRF 离散路径：事件命中率

PRF 用于验证 0/1 触发事件。原文把 ATR 因子改成 `ATR14[t] > SMA(ATR14, 3)[t-1]`，问题从“幅度越大越好吗”变成“突破事件有没有预测力”。

指标解释：

| 指标 | 含义 |
|---|---|
| Precision | 信号触发后真涨的比例 |
| Recall | 真涨日子里被信号覆盖的比例 |
| Lift | Precision 减去同期基线上涨率 |
| F1 | Precision 与 Recall 的综合 |

原文强调：Lift 不能省。Precision 高不代表有 alpha，如果基线本来就高，超额可能很小。

原文 PRF 结果：

| horizon | Precision | 基线 | Lift | Recall | F1 |
|---|---:|---:|---:|---:|---:|
| 1 | `54.7%` | `52.9%` | `+1.8 ppt` | `22.3%` | `0.317` |
| 5 | `58.2%` | `54.5%` | `+3.7 ppt` | `22.7%` | `0.327` |
| 10 | `61.9%` | `56.4%` | `+5.4 ppt` | `22.7%` | `0.333` |
| 60 | `72.7%` | `62.2%` | `+10.5 ppt` | `22.1%` | `0.339` |

解读：

- N=1 Lift 只有 `+1.8 ppt`，隔日噪声大。
- N=5 到 N=10 才展开，说明 ATR 突破更像波段确认信号。
- Recall 约 `22%`，说明它不能单兵作战，只能覆盖一部分机会。

对本项目的启发：

- 当前 `FactorValidator` 主要验证连续因子，缺 PRF 事件验证。
- 可以把 `signal-report` 的事件思路下沉为通用 `binary_factor_report`。
- 对离散信号，准入门槛应优先看 `Lift > 0`，而不是只看 Precision。

### D. 事件研究：触发后收益持续性

事件研究把每次信号触发日作为 `t=0`，观察 T+1/T+5/T+10/T+20 累计收益，并用 t 检验判断是否显著大于零。

原文事件研究结果：

| horizon | 平均收益 | 胜率 | t 值 | p 值 |
|---|---:|---:|---:|---:|
| T+1 | `+0.43%` | `54.65%` | `+5.9` | `< 1e-8` |
| T+5 | `+1.74%` | `58.20%` | `+10.8` | `< 1e-18` |
| T+10 | `+3.14%` | `61.86%` | `+13.1` | `< 1e-37` |
| T+20 | `+4.73%` | `60.17%` | `+12.8` | `< 1e-37` |

原文重点：

- 收益从 T+1 到 T+20 单调递增，说明不是一日噪声。
- 统计显著不等于 alpha。事件研究不扣佣金、滑点、冲击成本，也不处理资金占用和多信号同日拥挤。
- 事件研究是“理想收益上限刻画”，还要进入回测和成本审计。

对本项目的启发：

- 当前 `factor_validation/` 没有通用事件研究表。
- `signal-report` 已有信号 recipe 验证入口，但还不是因子级标准报告的一部分。
- 需要输出 `event_return_by_horizon` 和 `event_return_summary`，并在后续接成本模型。

### E. Spearman 冗余：组队能力

单因子通过不等于组合可用。Spearman 冗余矩阵用于识别因子是否重复表达同一类信息。

原文规则：

| 相关性 | 判断 |
|---:|---|
| `|rho| > 0.7` | 高度冗余，同层二选一 |
| `|rho| ~ 0.5` | 中度警戒，允许共存但应降权 |
| 接近 0 | 信息更独立，组合边际价值更高 |

原文案例：

- `ma50` 与 `align` 相关约 `0.72`，超过红线，二选一。
- `atr` 与 `vol>k` 相关约 `0.49`，接近警戒线，可共存但降权。
- `macd` 和 `vol↑` 与其他因子大体独立，组合增量价值高。

对本项目的启发：

- 当前 `factor_scorecard` 缺少因子间相关性和家族去重。
- Alpha101、GTJA191、Alpha158 中容易出现大量同质代理因子。
- 需要按因子家族和同层维度输出 Spearman 冗余矩阵。

## 标准化报告模板

原文建议每个因子都跑同一张卷子，方便跨因子 diff：

```text
Factor Validation Report — <factor_id>
Layer       : <Trend / Momentum / Volume / Volatility / ...>
Signal Type : <Continuous / Discrete>

[A] Stationarity (ADF)
ADF Stat: ...
p-value : ...
PASS / FAIL

[B] Continuous Path — Spearman IC
IC_5D / IC_10D / IC_20D
IC_IR
PASS / FAIL / SWITCH_TO_DISCRETE

[C] Discrete Path — PRF
Base / Precision / Lift / Recall / F1
PASS / FAIL

[D] Event Return Analysis
T+1 / T+5 / T+10 / T+20 return, win rate, t-stat, p-value

[E] Redundancy
Top correlations
Acceptable / Warning / Redundant

STATUS: VERIFIED / WATCH / REJECT
```

原文模块表：

| 模块 | 作用 | 不通过怎么办 |
|---|---|---|
| ADF | 排除非平稳序列伪相关 | 重新设计因子，做差分或归一 |
| IC_IR | 测连续幅度预测力 | 切到离散路径，不直接放弃 |
| PRF | 测离散信号比随机选股强多少 | `Lift <= 0` 直接淘汰 |
| 事件研究 | 测信号触发后收益持续性 | `p > 0.05` 淘汰；显著但收益小则备选 |
| 冗余 | 测因子是否带来独立信息 | `|rho| > 0.7` 同层去重 |

## 因子使用边界

原文对 ATR 突破的使用边界很关键：

- 不能单用：Recall 只有约 `22.74%`，会漏掉大多数上涨机会。
- 不适合短线：N=1 Lift 只有约 `+1.8 ppt`，更像 5-10 天以上的波段确认。
- 不能盲目迁移：原文样本是美股科技/AI 主题股票，两年日频；换到港股、A 股、期货都要重跑。
- 更适合作为“波动率确认层”：趋势、动量、量能过线后，用 ATR 突破确认波动正在加速。

原文组合实验中，Top 5 入场组合有 4 个包含 ATR 突破；去掉 ATR 后综合分明显下降，说明它的价值是“独立确认维度”，不是连续幅度本身。

## 当前项目映射

### 已有能力

| 原文模块 | 本项目状态 | 位置 |
|---|---|---|
| 分组回测 | 已有 | `FactorValidator.calculate_quantile_returns_by_date()` |
| IC / RankIC | 已有 | `FactorValidator.calculate_ic_by_date()` / `summarize_ic()` |
| 多 horizon | 已有 | 默认 `1,5,10,20` |
| 多空价差 | 已有 | `long_short_by_date` / `long_short_summary` |
| 换手 | 已有，原文未作为主模块但本项目有价值 | `turnover_by_date` / `turnover_summary` |
| 衰减 | 已有，适合补充到报告 | `decay_summary` |
| 报告导出 | 已有 CSV | `cli/factor_report.py` |
| 权重缓存 | 已有 | `cli/validate_factors.py` |
| run manifest | 已有 | `cli/helpers.py` |

### 缺口

| 原文模块 | 缺口 | 优先级 |
|---|---|---|
| ADF | 没有平稳性检验 | P0 |
| PRF | 没有通用离散信号 Precision/Recall/F1/Lift | P0 |
| 事件研究 | 没有因子级事件触发收益表 | P0 |
| Spearman 冗余 | 没有因子间相关矩阵、同层去重、冗余降权 | P0 |
| 标准化报告 | 没有单因子 Markdown/HTML tear sheet | P0 |
| 使用边界 | scorecard 不能表达“确认层/不能单用/适用 horizon” | P1 |
| 样本迁移 | 缺市场/行业/主题分层后的验证结论 | P1 |

## P0 TODO

### P0.1 增加 ADF 体检

- [ ] 在 `factor_validation/validator.py` 增加连续因子的 ADF 检验。
- [ ] 输出 `adf_stat`, `adf_pvalue`, `stationarity_status`。
- [ ] 对非平稳因子给出建议：差分、归一化、转离散事件或直接 watch。
- [ ] 在 `factor_scorecard` 中增加 `stationarity_status`。

### P0.2 增加 PRF 离散路径

- [ ] 支持把连续因子按阈值转成 0/1 信号，或直接读取二值因子。
- [ ] 对每个 horizon 输出 `base_rate`, `precision`, `recall`, `lift`, `f1`, `event_count`。
- [ ] 将 `Lift <= 0` 标记为 reject。
- [ ] 把“连续路径弱但离散路径强”标记为 `switch_to_discrete_candidate`。

建议输出：

```text
prf_by_horizon.csv
feature_name,horizon,event_count,base_rate,precision,recall,lift,f1,status
```

### P0.3 增加事件研究

- [ ] 以信号触发日为 `t=0`，输出 T+1/T+5/T+10/T+20 平均收益、胜率、t 值、p 值。
- [ ] 支持事件去重和冷却期，避免同一股票连续触发导致样本过密。
- [ ] 输出累计收益曲线数据，不只输出汇总表。
- [ ] 标注“事件研究未扣成本”，后续与成本模型对接。

建议输出：

```text
event_return_summary.csv
feature_name,horizon,event_count,mean_return,win_rate,t_stat,p_value,status
```

### P0.4 增加 Spearman 冗余矩阵

- [ ] 按 `trade_date` 对因子值做秩相关，输出全样本和分日期平均 Spearman。
- [ ] 增加 `factor_family/layer`，支持同层去重。
- [ ] 对 `|rho| > 0.7` 标记 redundant，对 `|rho| >= 0.5` 标记 warning。
- [ ] `factor_scorecard` 增加 `top_correlated_factors`, `redundancy_status`, `cluster_id`。

建议输出：

```text
factor_redundancy_matrix.csv
factor_redundancy_edges.csv
```

### P0.5 生成标准化单因子报告

- [ ] 新增 `factor-validation-tearsheet` 或扩展 `factor-report`。
- [ ] 每个因子输出一份 Markdown，包含 `[A]` 到 `[E]` 五个模块。
- [ ] 给出最终状态：`VERIFIED / WATCH / REJECT`。
- [ ] 报告必须写明 `Signal Type`：Continuous / Discrete / Both。
- [ ] 报告必须写明建议用途：主信号、确认层、过滤层、watchlist、reject。

建议目录：

```text
output/factor_validation_tearsheet/
  <run_id>/
    summary.md
    factor_scorecard.csv
    factors/
      <factor_id>.md
```

### P0.6 调整当前 scorecard 评分逻辑

现有 `_build_factor_scorecard()` 已把 RankIC、IC、spread、positive rate、turnover 合成一个 `validation_score`。按原文思路，需要拆成路径评分：

```text
continuous_score = IC / IC_IR / quantile monotonicity / long-short spread
discrete_score   = Lift / Precision / Recall / F1 / event return
redundancy_score = Spearman independence penalty
final_status     = gate(continuous_score, discrete_score, event_score, redundancy)
```

TODO：

- [ ] 不再让低 IC 自动压死离散强信号。
- [ ] 对 `IC fail + PRF pass + Event pass` 给出“离散信号候选”。
- [ ] 对 `single pass + redundancy fail` 给出“同层替补/降权”。

## 推荐命令

现有全市场默认验证：

```bash
uv run python run.py validate-factors \
  --days 365 \
  --factor-set alpha_zoo_hk \
  --export-csv output/validation_scorecard \
  --show-progress
```

现有报告导出：

```bash
uv run python run.py factor-report \
  --days 365 \
  --factor-set alpha_zoo_hk \
  --export-csv output/factor_report \
  --show-progress
```

建议新增命令形态：

```bash
uv run python run.py factor-validation-tearsheet \
  --days 365 \
  --factor-set alpha_zoo_hk \
  --horizons 1,5,10,20 \
  --quantiles 5 \
  --export-dir output/factor_validation_tearsheet \
  --show-progress
```

## 验收标准

- [ ] 每个因子都有 `[A] ADF`、`[B] IC`、`[C] PRF`、`[D] Event Return`、`[E] Redundancy` 模块。
- [ ] 连续因子弱但离散事件强时，系统能保留为事件候选，不直接 reject。
- [ ] PRF 报告必须包含基线和 Lift。
- [ ] 事件研究必须标注未扣成本，并输出 T+1/T+5/T+10/T+20。
- [ ] Spearman 冗余能按 `0.5` 警戒、`0.7` 红线生成去重建议。
- [ ] `factor_scorecard` 输出最终状态和用途建议：主信号、确认层、过滤层、watch、reject。
- [ ] 报告中的例子能复现原文核心分歧：`IC weak` 不等于 `event invalid`。

## 参考

原文列出的参考方向：

- Grinold & Kahn《Active Portfolio Management》：IC 与 IC_IR。
- De Prado《Advances in Financial Machine Learning》第 3 章：Precision、Lift 等分类指标。
- MacKinlay《Event Studies in Economics and Finance》：事件研究。
- Spearman 冗余经验阈值：`0.7` 高度冗余红线，`0.5` 中度警戒。
