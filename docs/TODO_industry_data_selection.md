# TODO — 行业数据补全与分行业选股重构

> 生成日期：2026-05-31  
> 背景：当前 LightGBM 选股已实现 quality、valuation、overheat、liquidity、cluster concentration 等优化，但 `cluster_id` 来自收益相关性聚类，不等同于真实行业。下一阶段目标是先补全行业/基本面/流动性数据，再把选股逻辑重构为“行业内选潜力股 + 组合层风险预算”。

> 延伸设计：关于“分行业选股核心层”与“行业择时增强层”的拆分、打分字段和回测验收，见 [sector_neutral_vs_timing_optimization.md](./sector_neutral_vs_timing_optimization.md)。

---

## 目标

构建一条可复用的数据与筛选链路：

1. 用真实行业分类替代当前 correlation cluster 作为主行业维度。
2. 保留 correlation cluster 作为“风格/拥挤度/交易相关性”辅助维度。
3. 在行业内计算估值、质量、动量、流动性相对排名。
4. 先做硬过滤，再做行业内 TopN 候选，再做组合层软约束。
5. 输出每只入选股票的行业、行业内排名、全市场排名、行业暴露和剔除原因。

---

## 当前进展

### 2026-05-31 P0.0 已完成：数据完整性与硬门禁地基

- 已扩展 `stock_info_registry` 与 `normalize_stock_info()`，支持 `industry_l1/l2/l3`、`theme_tags`、`industry_source`、`industry_updated_at`。
- 已新增港股行业补全器 `HKIndustryFetcher` 与 `run.py backfill-industry` 回填入口。
- 已新增本地行业 taxonomy 修正：东方财富单字段行业如 `软件服务`、`银行` 会归一为 `industry_l1/industry_l2`。
- `stock_info_registry` 普通行情/估值更新会保留已有行业字段，避免回填结果被后续同步冲掉。
- LightGBM 与 Factor 排名结果已透传行业字段、数据完整度字段、流动性/风险字段。
- 组合层已为 ranking 每行输出 `selection_eligible`、`eligibility_reasons`、`data_coverage_score`、`data_missing_fields`。
- `current_signal_actionable=False`、`liquidity_ok=False`、`setup_type=sideways` 不再能通过 fallback 补回 selected。
- Feature materialization 支持 ClickHouse 不可用时降级到本地 parquet；只读验证路径不再尝试写 feature store。
- 修复 factor batch 能力检测，避免实际走单股 fallback；LightGBM ranker 支持可替换模型对象。

### 仍未完成

- 尚未跑完真实全市场行业回填并验证 90%/80% 覆盖率；小样本 `00700/00005` 已可归一出 L1/L2。
- 尚未构建行业日频基准、行业 RPS、行业 breadth。
- 尚未实现按 `industry_l1/l2` 的行业内 TopN 候选生成与行业风险预算。
- 尚未把质量/估值原始指标升级为行业内标准化版本。

---

## 当前主要缺口

| 缺口 | 当前状态 | 风险 |
|---|---|---|
| 真实行业分类 | `core/sector_features.py` 使用 60 日收益相关性聚类 | cluster 不稳定，银行/消费/大盘股可能混在一起 |
| 行业内质量标准化 | `fetch_quality_scores()` 默认全市场 QMJ 标准化 | 不同行业财务结构差异被误判 |
| 行业内估值标准化 | PE/PB 按 correlation cluster 分位 | 行业可比性不足，生物医药/银行/消费不可直接混比 |
| 硬过滤入口 | 流动性/过热多为扣分或后处理 | 不可交易或极端风险标的仍可能被补回 selected |
| 行业选股逻辑 | 当前仍是全市场排名 + concentration penalty | 不是“每行业找最强潜力股” |
| 可解释输出 | selected CSV 缺行业名、行业内排名、剔除原因 | 难以判断组合为什么买/为什么没买 |

---

## P0 — 数据补全优先

### 1. 真实行业分类表

**目标**：新增稳定的行业主数据表，至少覆盖港股池。

**建议字段**：

| 字段 | 说明 |
|---|---|
| `stock_code` | 港股代码，5 位字符串 |
| `stock_name` | 股票名称 |
| `market` | HK |
| `industry_l1` | 一级行业，如 Financials / Consumer / Healthcare |
| `industry_l2` | 二级行业 |
| `industry_l3` | 可选，细分行业 |
| `theme_tags` | 可选，新能源、AI、医药、中特估等主题标签 |
| `industry_source` | 数据源 |
| `industry_updated_at` | 更新时间 |

**候选数据源**：

| 优先级 | 数据源 | 用途 | 备注 |
|---|---|---|---|
| P0 | 现有 `stock_info_registry` 可扩字段 | 本地统一存储 | 先确认 Tencent/Eastmoney 是否返回行业字段 |
| P0 | 东方财富/腾讯 F10 行业字段 | 港股行业补全 | 与现有接口风格一致 |
| P1 | HKEX / 恒生行业分类 | 更权威行业映射 | 可能需要单独抓取/维护 |
| P1 | GICS/ICB 映射 | 统一国际分类 | 若免费数据不足，可先用内部映射 |
| P2 | LLM/规则辅助主题标签 | 主题热度分析 | 仅作辅助，不作为主行业 |

**实现落点**：

- `data/model/schemas.py`：扩展 `normalize_stock_info()` 字段。
- `data/store/warehouse.py`：扩展 `stock_info_registry` schema。
- `data/ingest/providers/hk_info.py`：抓取/解析行业字段。
- `data/ingest/providers/hk_industry.py`：独立行业补全器。
- `run.py backfill-industry`：批量回填入口。

**验收标准**：

- [ ] 90%+ 港股池有 `industry_l1`。实现入口已完成，待真实全市场回填验证。
- [ ] 80%+ 港股池有 `industry_l2`。实现入口已完成，待真实全市场回填验证。
- [x] 行业字段可从 `MarketDataWarehouse.get_stock_info()` 读出。
- [x] ranking/selected/watchlist 数据结构包含 `industry_l1`、`industry_l2`；CSV 导出沿用 ranking 字段。

### 2. 行业基准与行业动量数据

**目标**：为每个行业构建日频行业组合，支持行业 RPS、行业 breadth、个股相对行业收益。

**建议字段**：

| 字段 | 说明 |
|---|---|
| `trade_date` | 交易日 |
| `industry_l1/l2` | 行业 |
| `member_count` | 当日可交易成分数 |
| `industry_ret_5d/20d/60d` | 行业等权收益 |
| `industry_rps_20d/60d` | 行业横截面强度 |
| `industry_breadth_5d/20d` | 成分上涨占比 |
| `industry_vol_20d/60d` | 行业波动 |

**实现落点**：

- 新增 `core/industry_features.py`，替代或并行 `core/sector_features.py`。
- LightGBM 特征中新增：
  - `industry_rps_20d`
  - `industry_breadth_20d`
  - `stock_vs_industry_ret_5d`
  - `stock_vs_industry_ret_20d`
  - `industry_vol_60d`

**验收标准**：

- [ ] 行业特征不依赖未来数据。
- [ ] 每个交易日的行业特征只使用当日及以前价格。
- [ ] 输出里同时保留 `industry_*` 与 `cluster_*`，避免一次性替换导致回归。

### 3. 流动性与可交易性数据

**目标**：把“不可交易”从排序扣分改为候选池硬过滤。

**建议字段**：

| 字段 | 说明 |
|---|---|
| `turnover_amount_1d/20d` | 成交额 |
| `turnover_rate_20d` | 20 日换手率 |
| `amihud_illiq_20d` | Amihud 非流动性 |
| `zero_volume_days_20d` | 20 日零成交天数 |
| `suspended_flag` | 停牌/无交易标记 |
| `tradable_flag` | 综合可交易标记 |

**硬过滤建议**：

```text
tradable_flag == True
median_turnover_amount_20d >= 1,000,000 HKD
zero_volume_days_20d <= 2
amihud_illiq_20d 非极端分位
```

**验收标准**：

- [x] `liquidity_ok=False` 的股票不能进入 selected。
- [x] `current_signal_actionable=False` 的股票默认不能进入 selected。
- [ ] selected 里的每只股票都有明确可交易性证据。

### 4. QMJ 财务质量原始指标补全

**目标**：从“简化 quality_score”升级为可行业内标准化的原始财务指标。

**建议字段**：

| 维度 | 指标 |
|---|---|
| Profitability | ROE、ROA、毛利率、净利率、毛利/资产、经营现金流/资产 |
| Growth | 收入 YoY、利润 YoY、ROE 变化、毛利率变化 |
| Safety | 资产负债率、流动比率、利息覆盖、盈利波动、价格波动、beta |
| Payout | 股息率、派息率、净发行/回购、净债务发行 |

**关键要求**：

- 原始指标先按 `industry_l2` 做 rank/z-score。
- 行业样本不足时退回 `industry_l1`。
- 仍不足时退回全市场。
- 缺失值不要直接全给 50，应记录 `quality_data_coverage`。

**验收标准**：

- [ ] 输出 `quality_score`、`quality_data_coverage`、`quality_missing_fields`。
- [ ] 银行/保险/地产等特殊行业不会因缺字段全部变成 50。
- [ ] 行业内 quality 排名可解释。

### 5. 行业内估值数据

**目标**：估值只在同类公司内比较，避免跨行业误判。

**建议字段**：

| 字段 | 用途 |
|---|---|
| `pe_ttm` | 盈利公司估值 |
| `pb` | 金融/资产型公司估值 |
| `ps_ttm` | 亏损成长股估值 |
| `ev_ebitda` | 周期/工业股估值 |
| `dividend_yield` | 高股息/银行/公用事业 |
| `valuation_data_coverage` | 数据完整度 |

**行业内评分建议**：

```text
valuation_score = industry_relative_percentile(
    preferred_metric_by_industry
)
```

例子：

- 银行/保险：PB、股息率优先。
- 生物医药/成长亏损股：PS、市销率或现金 runway，不用 PE。
- 航运/周期：PB、EV/EBITDA、周期位置。
- 消费/品牌：PE、PS、利润增长匹配。

**验收标准**：

- [ ] 负 PE、不适用 PE 不再被简单当作异常或中性。
- [ ] 每只股票输出 `valuation_metric_used`。
- [ ] 行业内估值分与行业逻辑匹配。

---

## P1 — 筛选逻辑重构

### 6. 候选池硬过滤层

**目标**：任何进入行业 TopN 的股票都先通过基本可交易与风险过滤。

**建议过滤顺序**：

1. 股票池过滤：市值、成交额、上市天数、停牌。
2. 信号过滤：`current_signal_active=True` 且 `current_signal_actionable=True`。
3. 风险过滤：过热、回撤、下跌趋势、极端估值异常。
4. 数据质量过滤：核心字段覆盖率不足进入 watchlist，不进入 selected。

**建议新增字段**：

| 字段 | 说明 |
|---|---|
| `eligibility_pass` | 是否通过硬过滤 |
| `eligibility_reasons` | 失败原因数组 |
| `data_quality_score` | 数据完整度 |

**验收标准**：

- [x] selected 中不存在 `current_signal_actionable=False`。
- [x] selected 中不存在 `liquidity_ok=False`。
- [x] 被剔除股票可在 ranking 看到剔除原因。

### 7. 行业内 TopN 候选生成

**目标**：每个行业先选出若干个最强候选，再进入组合层优化。

**建议逻辑**：

```text
for industry_l2:
    eligible = stocks passing hard filters
    industry_candidates = top M by industry_score
merge all industry_candidates
final selected = portfolio optimizer / soft constraint selector
```

**行业内分数建议**：

```text
industry_score =
    0.35 * model_score_within_industry
  + 0.20 * quality_score_within_industry
  + 0.15 * valuation_score_within_industry
  + 0.15 * risk_adjusted_score_within_industry
  + 0.10 * liquidity_score_within_industry
  + 0.05 * stock_vs_industry_momentum
```

**注意**：

- 行业 TopN 不等于最终每行业必须买。
- 对样本数小的行业，使用 `min(ceil(industry_size * 10%), M)`。
- 热门行业可以多给候选名额，但最终组合仍需风控。

**验收标准**：

- [ ] ranking CSV 包含 `industry_rank`、`industry_score`。
- [ ] 每个行业至少输出 Top candidate 到 watchlist。
- [ ] selected 由行业候选池产生，而不是全市场直接截断。

### 8. 组合层行业软约束

**目标**：避免 hard cap 强行买入差行业，但控制行业集中风险。

**建议约束**：

| 约束 | 建议 |
|---|---|
| 单行业基础上限 | 30%-40% |
| 优质行业超配 | 行业 RPS 高、breadth 高、候选质量高时可放宽 |
| HHI 惩罚 | 行业集中度越高，后续同业候选扣分越重 |
| 风格 cluster 辅助惩罚 | correlation cluster 作为拥挤度，而非行业 |

**最终分数建议**：

```text
final_score =
    global_score
  + industry_opportunity_bonus
  - industry_concentration_penalty
  - correlation_cluster_crowding_penalty
  - liquidity_capacity_penalty
```

**验收标准**：

- [ ] 输出组合行业权重表。
- [ ] 输出行业 HHI。
- [ ] 同行业第 3/4 只股票入选时有可解释的超配理由。

### 9. 权重分配重构

**目标**：从简单等权/分数权重升级为“信号强度 + 风险预算 + 流动性容量”的组合权重。

**建议输入**：

- `final_score`
- `recent_volatility`
- `latest_risk_score`
- `median_turnover_amount_20d`
- `industry_weight_budget`
- `kelly_position_ratio`

**约束建议**：

| 约束 | 初始值 |
|---|---|
| 单票最大权重 | 5%-8% |
| 单行业最大权重 | 30%-40% |
| 单票成交额占比 | 不超过 20 日均成交额的 5% |
| 总仓位 | 半凯利上限，且可手动 cap |

**验收标准**：

- [ ] 高波动票自动降权。
- [ ] 低流动性票即使入选也限权。
- [ ] 输出 `weight_reason` 或权重分解。

---

## P2 — 训练与回测升级

### 10. 模型特征拆分：行业内 Alpha + 行业 Beta

**目标**：避免训练阶段把行业信息全中性化，又在组合阶段想要行业机会。

**建议改造**：

- 保留原始行业特征：`industry_rps_*`、`industry_breadth_*`。
- 新增行业内特征：`*_within_industry_rank`。
- 模型输出拆分：
  - `stock_alpha_score`：行业内个股强弱。
  - `industry_beta_score`：行业/赛道强弱。
  - `combined_model_score`：组合用综合分。

**验收标准**：

- [ ] OOS 同时报告全市场 IC 和行业内 RankIC。
- [ ] 能看到模型收益来自行业选择还是行业内选股。

### 11. 分行业回测报告

**目标**：验证“分行业 TopN”是否真的改善组合，而不是只改善分散度。

**报告项**：

| 指标 | 说明 |
|---|---|
| 行业内 TopN 命中率 | 每行业候选的胜率 |
| 行业贡献 | 哪些行业贡献收益/回撤 |
| 行业换手 | 是否频繁追热点 |
| 行业暴露 HHI | 集中度 |
| 与旧逻辑对比 | old selected vs industry selected |

**验收标准**：

- [ ] 新旧逻辑在同一日期、同一股票池可对比。
- [ ] 至少输出 1 年滚动样本外表现。
- [ ] 成本、滑点、流动性容量纳入回测。

---

## 建议实施顺序

| 阶段 | 任务 | 预期产物 |
|---|---|---|
| 1 | 扩展 stock_info 行业字段 | `industry_l1/l2` 可读可导出 |
| 2 | 新增行业补全入口 | `HKIndustryFetcher`、`run.py backfill-industry` |
| 3 | 新增行业特征模块 | `industry_features.py` |
| 4 | 补流动性/可交易性字段 | `tradable_flag`、`amihud_illiq_20d` |
| 5 | QMJ 原始指标补全 | 行业内 `quality_score` |
| 6 | 行业内估值评分 | `valuation_metric_used`、`valuation_score` |
| 7 | 候选池硬过滤 | `eligibility_pass/reasons` |
| 8 | 行业内 TopN 候选 | `industry_rank/industry_score` |
| 9 | 组合层软约束 | 行业权重、HHI、拥挤度惩罚 |
| 10 | 回测与报告 | 新旧逻辑对比 |

---

## 最小可用版本

如果先做一个 MVP，建议只做这 5 件事：

1. 补 `industry_l1/industry_l2`。
2. ranking/selected CSV 输出行业字段。
3. selected 强制要求 `current_signal_actionable=True` 与 `liquidity_ok=True`。
4. 每个 `industry_l2` 先取 Top 2 候选进入候选池。
5. 最终 selected 从候选池按 `ranking_score - concentration_penalty` 选 TopN。

这样可以先解决当前最痛的两个问题：假行业分组、不可交易/非 actionable 股票混入 selected。

---

## 待确认问题

- [ ] 主行业分类优先使用哪套标准：东方财富/腾讯、HKEX、恒生行业、GICS/ICB？
- [ ] 分行业粒度使用一级还是二级？初始建议：二级为主，一级兜底。
- [ ] 每行业候选数量固定 Top 2，还是按行业股票数量动态分配？
- [ ] 是否允许高 RPS 行业超配？若允许，上限是多少？
- [ ] 亏损成长股估值如何处理：PS/市值/现金流 runway 是否可获取？
- [ ] 港股通/非港股通是否作为可交易性过滤条件？

---

## 完成标准

- [ ] 数据层：行业、流动性、财务质量、估值字段完整入库。
- [ ] 特征层：行业动量、行业 breadth、个股相对行业收益可用。
- [ ] 筛选层：硬过滤、行业内 TopN、组合软约束完成。
- [ ] 输出层：CSV/LLM 报告展示行业排名、入选原因、剔除原因。
- [ ] 验证层：新旧筛选逻辑有同口径回测对比。
