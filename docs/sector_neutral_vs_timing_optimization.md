# 分行业选股与行业择时叠加优化方案

> 生成日期：2026-06-03  
> 背景：项目已经具备真实行业字段、行业内候选池、硬过滤、行业权重表和 HHI 输出。下一步需要把策略框架明确拆成“分行业选股核心层”与“行业择时增强层”，避免在代码中把行业中性、行业超配、相关性 cluster 惩罚混成一套不可解释规则。

---

## 结论

项目当前方向应采用：

```text
Core: Within-Industry Selection
  先在每个真实行业内寻找个股 Alpha，作为稳定底座。

Overlay: Industry Timing / Selected Sectors
  只在行业景气、行业动量、breadth 和行业内候选质量同时确认时，提高候选名额或权重上限。
```

原因：

- 港股/A股行业财务结构差异大，PE、PB、ROE、质量指标必须行业内比较，否则会产生跨行业错配。
- 纯行业择时上限更高，但主动风险和回撤更大，不能作为核心选股入口。
- long-only 组合不应做机械行业中性；更适合“行业内选股 + 渐进行业超配/惩罚”。
- correlation cluster 应继续作为拥挤度/相关性风险维度，不应替代真实行业。

---

## 文献与研报补充

检索日期：2026-06-03。

### 1. 行业中性不是 long-only 的硬规则

Ehsani, Harvey & Li (2023) 在 *Financial Analysts Journal* 发表的 *Is Sector Neutrality in Factor Investing a Mistake?* 明确把股票特征收益拆成行业内与跨行业两部分。其核心启发不是“永远行业中性”，而是：long-short 与 long-only 的约束不同，long-only 组合强制行业中性可能牺牲收益弹性。

工程启发：

- 本项目不应把单行业权重做成硬性等权或硬性对标指数。
- 应保留 `industry_opportunity_score`，但用预算/惩罚控制它，而不是让它直接覆盖 `industry_alpha_score`。
- 行业中性更适合作为回测对照组：`core` 模式，而不是唯一实盘模式。

来源：

- Duke Scholars: https://scholars.duke.edu/publication/1579231
- DOI: https://doi.org/10.1080/0015198X.2023.2196931

### 2. 行业动量是真实但不稳定的 Alpha 来源

Moskowitz & Grinblatt (1999) 的 *Do Industries Explain Momentum?* 发现行业动量贡献了相当一部分中期动量收益。后续关于 business cycle and industry returns 的研究进一步说明，行业收益存在状态依赖，行业历史 Sharpe 在不同经济状态下有预测力。

工程启发：

- Overlay 不应只用短期 RPS，应加入更稳健的状态变量：
  - `industry_rps_20d`
  - `industry_rps_60d`
  - `industry_breadth_20d`
  - `industry_vol_60d`
  - `industry_regime_sharpe`
- 行业择时信号必须做 OOS 胜率验证。若行业择时胜率低于 60%-65%，只允许轻微调权，不允许改变候选池结构。

来源：

- AQR summary, *Do Industries Explain Momentum?*: https://www.aqr.com/insights/research/journal-article/do-industries-explain-momentum
- ScienceDirect, *Does history repeat itself? Business cycle and industry returns*: https://www.sciencedirect.com/science/article/abs/pii/S0304393219301886

### 3. 行业暴露是否有价值取决于因子类型

Vyas & van Baren 的 *Should equity factors be betting on industries?* 研究指出，不同因子风格的行业暴露差异很大，momentum 与 quality 内部也可能表现出不同的行业配置贡献。结论不是简单剔除行业暴露，而是要识别“哪些因子的行业暴露有收益、哪些只是风险”。

工程启发：

- 不同因子应分别记录行业暴露贡献：
  - momentum 类允许更高 `industry_opportunity_score` 权重；
  - quality/value 类更强调行业内 percentile；
  - low-vol/risk 类应防止行业集中。
- `factor_report` 应输出 factor × industry 的贡献矩阵，而不是只看总 IC。

来源：

- SSRN: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3423566

### 4. 质量因子应行业内标准化

Asness, Frazzini & Pedersen (2019) 的 QMJ 把质量拆成 profitability、growth、safety、payout。其核心不是简单买高 ROE，而是把质量定义成多维特征，并结合估值解释“高质量不一定贵到足以抹掉未来收益”。

工程启发：

- `quality_score` 不应直接用全市场分，必须输出：
  - `quality_score_within_industry`
  - `quality_peer_group`
  - `quality_data_coverage`
  - `quality_missing_fields`
- 行业样本不足时，不应给 50 后继续当有效信号，应标记为 `peer_group_fallback`。

来源：

- CBS Research Portal, *Quality Minus Junk*: https://research.cbs.dk/en/publications/quality-minus-junk-2/
- DOI: https://doi.org/10.1007/s11142-018-9470-2

### 5. 国内研报支持“行业 + 个股 Alpha 双驱”，但要求胜率门槛

华泰证券关于行业配置落地到指数增强的研究提到，行业轮动策略胜率达到 65%-70% 后，对指数增强的超额才有稳定正贡献；胜率不足时，行业轮动对组合的边际收益不稳定。广发金工“龙头扩散效应”系列进一步把行业优选组合落到个股组合，结论是“先选行业，再在行业内分别筛选个股”的方案优于只在行业池里统一筛固定数量个股。

工程启发：

- Overlay 必须有胜率门控：
  - `industry_timing_oos_win_rate < 60%`：禁用候选名额加成；
  - `60%-65%`：只允许权重小幅调整；
  - `>=65%`：允许 Hot 行业 `candidate_cap + 1`；
  - `>=70%`：允许 Hot 行业更高权重预算，但仍受 HHI 和单行业上限约束。
- 行业优选后的个股筛选应采用“各优选行业内分别选股”，而不是把优选行业股票混在一起统一排序。

来源：

- 华泰证券研究摘要： https://bigquant.com/square/paper/235d78a8-5707-4326-9c27-7b2c29477d94
- 广发金工转载摘要： https://finance.sina.com.cn/stock/stockzmt/2025-11-19/doc-infxxsmv3593203.shtml

### 资料使用边界

- Ehsani/Harvey/Li、Moskowitz/Grinblatt、QMJ、business cycle and industry returns 属于可作为方法论依据的论文或论文页面。
- 华泰、广发条目来自公开摘要/转载，适合转化为工程上的保护阈值和实验假设，不应直接当成已复现结论。
- 本项目最终是否启用行业择时增强，必须以后续本地港股股票池的 OOS 回测为准。文献只决定“怎么设计实验”，不直接决定“参数一定有效”。

---

## 现有实现诊断

### 已具备的地基

| 模块 | 当前能力 | 判断 |
|---|---|---|
| `backfill-industry` | 补 `industry_l1/l2/l3`、`instrument_type`、`is_fund_like`、`tradable_flag` | 可作为真实行业主数据入口 |
| `core/industry_features.py` | 行业收益、RPS、breadth、行业波动、个股相对行业收益 | 已有行业择时/行业强弱特征地基 |
| `core/industry_scoring.py` | 行业内质量和估值标准化 | 已有行业内相对评分地基 |
| `IndustryCandidateSelector` | 硬过滤、行业内候选、行业集中惩罚、`industry_rank/score/cap` | 已实现核心候选池 |
| `TopNPortfolioBuilder` | 最终持仓从行业候选池产生，并同步 selected 标记 | 已修复导出口径 |
| 输出 | ranking/selected/watchlist、行业权重、HHI、剔除原因 | 可解释性已基本可用 |

### 主要缺口

| 缺口 | 表现 | 风险 |
|---|---|---|
| Core 与 Overlay 未显式拆分 | `industry_score/final_score` 混合了全市场分、行业内分、集中惩罚 | 难以判断收益来自选股还是押行业 |
| 行业内分数仍不够“行业内” | `_compute_industry_score()` 用原始 `ranking_score/quality/value`，没有统一转换为 within-industry percentile | 不同行业尺度差异仍会残留 |
| 行业择时没有独立预算 | 高 RPS/breadth 行业目前没有清晰的“可超配但有上限”规则 | 要么错失主线，要么隐性追热点 |
| 行业样本数小的处理不足 | L2 小行业直接排名，容易过拟合 | 银行/保险/细分行业样本少时排名不稳定 |
| 归因报告不足 | 缺少 stock alpha vs industry beta 的 OOS 拆解 | 不知道模型到底在赚个股 Alpha 还是行业 Beta |
| 组合权重缺行业预算 | 已有单票 cap/波动率/Kelly，但行业权重预算还偏弱 | 选股正确但组合暴露可能不稳定 |

---

## 目标架构

```text
Ranking rows
  │
  ├─ Hard filters
  │    tradable / liquidity / signal freshness / weak signal / drawdown / quality coverage
  │
  ├─ Core: Within-Industry Selection
  │    industry_alpha_score
  │    industry_rank
  │    base_candidate_cap
  │
  ├─ Overlay: Industry Timing
  │    industry_opportunity_score
  │    sector_budget_multiplier
  │    candidate_cap_bonus
  │
  └─ Portfolio Construction
       final_score
       industry_weight_budget
       cluster_crowding_penalty
       vol/liquidity/Kelly sizing
```

核心原则：

- Core 层只回答：同一行业里哪家公司更好？
- Overlay 层只回答：这个行业当前是否值得多给一点风险预算？
- Portfolio 层只回答：在风险、流动性、行业集中度约束下买多少？

---

## 优化点

### P0：把分数拆成两个可解释输出

新增字段：

| 字段 | 含义 |
|---|---|
| `industry_alpha_score` | 行业内个股 Alpha 分，行业中性核心分 |
| `industry_opportunity_score` | 行业景气/动量/广度分，行业择时 Overlay 分 |
| `combined_selection_score` | Core + Overlay 后的候选排序分 |
| `selection_layer` | `core` / `overlay_boosted` / `fallback` |

建议公式：

```text
industry_alpha_score =
    0.35 * model_score_within_industry
  + 0.20 * quality_score_within_industry
  + 0.15 * valuation_score_within_industry
  + 0.15 * risk_adjusted_score_within_industry
  + 0.10 * liquidity_score_within_industry
  + 0.05 * stock_vs_industry_rank

industry_opportunity_score =
    0.35 * industry_rps_20d
  + 0.25 * industry_rps_60d
  + 0.20 * industry_breadth_20d * 100
  + 0.10 * industry_ret_20d_rank
  - 0.10 * industry_vol_60d_rank
```

落点：

- `backtest_engine/industry_selector.py`
- `core/industry_features.py`
- `core/industry_scoring.py`
- `backtest_engine/portfolio.py`

验收：

- ranking CSV 同时输出 `industry_alpha_score` 与 `industry_opportunity_score`。
- selected 中每只股票能解释是“行业内优胜”还是“行业机会叠加”。

### P1：将行业内排名从原始分改为百分位分

当前 `_compute_industry_score()` 仍直接读取原始 `ranking_score/quality_score/value_score`。建议先在行业内做 percentile/z-score，再合成。

改造方式：

```text
for each industry_l2:
    model_score_within_industry = pct_rank(ranking_score)
    quality_score_within_industry = pct_rank(quality_score)
    valuation_score_within_industry = pct_rank(valuation_score)
    risk_score_within_industry = pct_rank(risk_adjusted_score)
```

小行业 fallback：

```text
if l2_member_count >= 8:
    use industry_l2
elif l1_member_count >= 15:
    use industry_l1
else:
    use global rank + mark peer_group="global_fallback"
```

验收：

- 银行/公用事业/科技不再因 PE/PB/ROE 尺度不同产生错位排名。
- 输出 `industry_peer_group_used` 与 `industry_peer_count`。

### P2：行业择时只改变“预算”，不直接替代选股

行业择时不要直接说“只买这几个行业”。更稳的方式是：

| 行业机会等级 | 条件 | 候选名额 | 权重预算 |
|---|---|---|---|
| Hot | RPS20/60 高、breadth 高、波动不过热 | `base_cap + 1` | 上限提高 1.2-1.5 倍 |
| Neutral | 行业强弱一般 | `base_cap` | 基础预算 |
| Cold | RPS 弱或 breadth 走坏 | `max(1, base_cap - 1)` | 降低预算但不完全清零 |
| Broken | 行业回撤/波动/下跌趋势极端 | 仅保留最强候选到 watchlist | selected 禁入或极低权重 |

新增字段：

- `industry_timing_bucket`
- `industry_timing_oos_win_rate`
- `industry_timing_oos_ir`
- `candidate_cap_base`
- `candidate_cap_overlay`
- `industry_weight_budget`
- `industry_budget_reason`

胜率门控：

```text
if industry_timing_oos_win_rate < 0.60:
    candidate_cap_overlay = candidate_cap_base
    overlay_strength = 0.00
elif industry_timing_oos_win_rate < 0.65:
    candidate_cap_overlay = candidate_cap_base
    overlay_strength = min(config_strength, 0.10)
elif industry_timing_oos_win_rate < 0.70:
    candidate_cap_overlay = candidate_cap_base + hot_bonus_if_confirmed
    overlay_strength = min(config_strength, 0.20)
else:
    candidate_cap_overlay = candidate_cap_base + hot_bonus_if_confirmed
    overlay_strength = min(config_strength, 0.30)
```

验收：

- 高 RPS 行业入选第 2/3 只股票时，CSV 能看到明确的 `industry_budget_reason`。
- 行业择时错误时，不会让非主线行业完全消失，避免错失行业内 Alpha。
- 若最近 OOS 行业择时胜率不足 60%，`core_overlay` 自动退化为近似 `core`。

### P3：组合层增加行业风险预算

当前已有 HHI 与单票权重 cap，但缺少行业预算显式约束。

建议初始参数：

| 参数 | 建议值 |
|---|---|
| 单行业基础权重上限 | 25%-30% |
| Hot 行业上限 | 35%-40% |
| Cold 行业上限 | 10%-15% |
| 单行业最小覆盖 | 非 Broken 行业至少保留 watchlist，不强制 selected |
| cluster 拥挤惩罚 | 保留，用于相关性风险，不等同行业 |

组合层 final score：

```text
final_score =
    0.70 * industry_alpha_score
  + 0.20 * industry_opportunity_score
  + 0.10 * global_model_score
  - industry_concentration_penalty
  - cluster_crowding_penalty
  - liquidity_capacity_penalty
```

注意：

- `industry_opportunity_score` 权重不宜太高，否则会退化成行业择时策略。
- Core 仍然占 70% 左右，保持长期 Sharpe。

### P4：新增收益归因报告

必须验证“分行业选股”是否真的提供稳定 Alpha。

新增报告：

| 报告项 | 用途 |
|---|---|
| 全市场 IC / RankIC | 看模型总预测力 |
| 行业内 RankIC | 看纯个股 Alpha |
| 行业间 RankIC | 看行业择时能力 |
| selected 行业贡献 | 哪些行业贡献收益/回撤 |
| Overlay 命中率 | Hot 行业是否真的跑赢 Neutral/Cold |
| 行业换手 | 是否追热点导致成本过高 |
| Core-only vs Core+Overlay | 同口径比较夏普、回撤、换手 |

建议输出：

- `output/factor_report_industry_attribution.csv`
- `output/results_alpha158_hk_industry_attribution.csv`
- LLM 报告中新增“收益来源：行业内 Alpha / 行业 Beta”段落。

### P5：增加策略模式开关

为了做 AB test，不要把所有逻辑写死。

建议参数：

```bash
--industry-selection-mode core              # 只做行业内选股
--industry-selection-mode core_overlay      # 默认：行业内选股 + 行业择时增强
--industry-selection-mode timing_only       # 研究用，不建议实盘
--industry-overlay-strength 0.2             # Overlay 权重
--max-industry-weight 0.35
--hot-industry-weight-multiplier 1.3
```

验收：

- 同一日期、同一股票池、同一模型可导出三套 selected 对比。
- 默认模式为 `core_overlay`，但 overlay strength 控制在低权重。

### P6：固定回测矩阵，防止参数调优过拟合

建议把优化验证拆成四组固定实验：

| 实验 | 目的 | 通过标准 |
|---|---|---|
| `global_baseline` | 当前全市场统一排序基准 | 作为对照，不要求胜出 |
| `core` | 只用行业内 Alpha | Sharpe/回撤优于 baseline，行业暴露更稳定 |
| `core_overlay_light` | 低强度行业预算叠加 | 净收益提升，换手和回撤不能显著恶化 |
| `timing_only` | 纯行业择时压力测试 | 只用于证明风险，不作为默认实盘模式 |

固定评估指标：

- 年化收益、Sharpe、Calmar、最大回撤。
- 换手率、交易次数、交易成本后收益。
- 行业内 RankIC、行业间 RankIC、Overlay 命中率。
- 行业 HHI、单行业最大权重、Hot/Cold 行业收益贡献。
- 分年份结果，尤其检查 2022、2024、2025-2026 等不同市场环境。

通过逻辑：

```text
if core beats global_baseline on risk-adjusted metrics:
    keep within-industry selection as default core

if core_overlay_light beats core after costs and drawdown is controlled:
    enable overlay with capped strength
else:
    keep overlay fields for reporting only

if timing_only has high return but drawdown/turnover unstable:
    keep it as research mode, not production default
```

---

## 与现有 TODO 的对应关系

| 原 TODO | 本文档补充 |
|---|---|
| 行业内 TopN 候选 | 明确拆为 `industry_alpha_score` |
| 组合层软约束 | 增加显式 `industry_weight_budget` |
| 模型特征拆分 | 增加 Core/Overlay 输出字段和 AB test 模式 |
| 分行业回测报告 | 增加行业内 RankIC、行业间 RankIC、Overlay 命中率 |
| 行业动量数据 | 用作预算调整，而不是直接筛掉其他行业 |

---

## 实施顺序

| 阶段 | 任务 | 产物 |
|---|---|---|
| 1 | 给 ranking 增加 `industry_alpha_score`、`industry_opportunity_score` | CSV 字段可见 |
| 2 | `_compute_industry_score()` 改为行业内 percentile 合成 | 行业内排名更稳 |
| 3 | 新增行业择时 OOS 胜率/IR 统计 | Overlay 有启用门槛 |
| 4 | 实现 `industry_timing_bucket` 与候选 cap overlay | 高景气行业有可解释超配 |
| 5 | 权重层加入 `industry_weight_budget` | 行业权重受控 |
| 6 | 新增 Core-only vs Core+Overlay 回测 | 确认 overlay 是否真的增厚收益 |
| 7 | LLM 报告加入收益来源拆解 | 报告可解释 |
| 8 | 固化回测矩阵与默认降级规则 | 防止 overlay 过拟合上线 |

---

## 当前不建议做的事

- 不建议把组合改成纯行业中性硬约束。long-only 策略会被迫买差行业里的相对好股票，降低收益弹性。
- 不建议只保留 2-3 个行业选股。除非明确运行的是卫星策略，否则会显著提高回撤和换手。
- 不建议用 correlation cluster 替代真实行业。cluster 应只作为交易相关性/拥挤度风险。
- 不建议用全市场 PE/PB/ROE 直接排序。行业异质性太强，会产生结构性偏差。

---

## 完成标准

- [ ] `ranking.csv` 中每行都有 Core 分、Overlay 分、行业层级、行业 peer group。
- [ ] `selected.csv` 中每只股票都有 `selection_layer` 和可解释入选理由。
- [ ] 行业权重表输出基础权重、overlay 后预算、实际权重、HHI。
- [ ] Overlay 有最近 OOS 胜率/IR；胜率低于 60% 时自动降级。
- [ ] 回测能比较 `core`、`core_overlay`、`timing_only` 三种模式。
- [ ] LLM 报告明确说明本期收益预期来自行业内 Alpha 还是行业机会叠加。
