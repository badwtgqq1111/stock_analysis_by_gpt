# P2.76 信息熵：理论映射、概率评价与数据依赖

编写：2026-10-22。输入材料：`docs/reference/信息熵.md`。

## 0. 结论先行

**不建议现在做成一条独立的“熵选股”策略。**

更合适的是两层结构：

1. **候选池概率评价层（主）**：对现有策略选出的 100～150 只股票，给每只股票一个
   `P(未来 h 日上涨/跑赢中证500)`，用于二次排序和仓位缩放。
2. **市场状态覆盖层（辅）**：用市场截面熵、行业/涨停分布熵、成交额带宽、熵变化速度，
   决定总仓位、候选池宽度和是否降低风险预算。

信息熵本身只度量不确定性、复杂度或信息流，不直接给方向。把“熵减”直接翻译成“要涨”
没有理论保证；最低熵也可能对应一字板、拥挤和即将反转。

## 1. 网上理论与可借鉴部分

| 方向 | 代表工作 | 可借鉴 | 不能直接推出 |
|---|---|---|---|
| 信息熵 | Shannon (1948), *A Mathematical Theory of Communication* | 用 `H=-Σp log p` 度量不确定性和信息量 | 熵低不等于收益为正 |
| 排列熵 | Bandt & Pompe (2002), *Permutation Entropy* | 对短噪声序列稳健，适合 5min 收益的序关系复杂度 | 序关系简单不代表趋势方向 |
| 近似熵 | Pincus (1991), *Approximate Entropy* | 衡量序列规律性/可重复性 | 历史规律性不等于可交易 alpha |
| 样本熵 | Richman & Moorman (2000) | 比近似熵更少自匹配偏差，适合滚动窗口 | 参数 `m/r` 会显著改变结论 |
| 转移熵 | Schreiber (2000), *Measuring Information Transfer* | 度量行业/指数/资金流到个股的方向性信息流 | 需要长、对齐、平稳的序列，估计误差大 |
| 股票收益与熵 | Maasoumi & Racine (2002), *Entropy and predictability of stock market returns* | 熵可用于检验收益可预测性和市场效率 | 不是选股公式 |
| 复杂度与机器学习可预测性 | Raubitzek & Neubauer (2022), *Entropy* | 近似熵/样本熵与 ML 可预测性相关；复杂度上升时预测力下降 | 只说明“条件预测力”，不说明涨跌 |
| 符号化金融序列 | *Symbolic Encoding Methods with Entropy-Based Applications to Financial Time Series Analyses* (2023) | 极端事件期间信息效率下降；分位数符号化有效 | 事件期特例不能外推为常态策略 |
| Rényi 转移熵 | Jizba et al. (2021) | 对尾部/跳跃信息流更敏感 | 小样本估计不稳定 |

参考链接：

- Shannon 1948: <https://doi.org/10.1002/j.1538-7305.1948.tb00917.x>
- Bandt & Pompe 2002: <https://doi.org/10.1103/PhysRevLett.88.174102>
- Schreiber 2000: <https://doi.org/10.1103/PhysRevLett.85.461>
- Pincus 1991: <https://doi.org/10.1073/pnas.88.6.2297>
- Richman & Moorman 2000: <https://doi.org/10.1152/ajpheart.2000.278.6.H2039>
- Maasoumi & Racine 2002: <https://doi.org/10.1016/S0304-4076(01)00125-7>
- Raubitzek & Neubauer 2022: <https://doi.org/10.3390/e24030332>
- Symbolic encoding 2023: <https://doi.org/10.3390/e25071009>
- Rényi transfer entropy 2021: <https://doi.org/10.3390/engproc2021005033>

## 2. 可计算的特征族

### A. 个股微结构熵

- 5min 收益分位符号熵：把收益分成 5～7 档，计算归一化 Shannon 熵。
- 排列熵：`m=3/4/5`、`delay=1`，窗口 20/60/120 根 5min。
- 样本熵：`m=2`、`r=0.2σ`，窗口 60/120 根。
- 成交量分布熵：成交量的集中/分散程度。
- 日内时段熵：开盘 30min、午盘、尾盘成交额占比的熵。
- 量价转移熵：`volume/moneyflow -> return` 的方向性信息流。

### B. 市场/行业状态熵

- 行业收益/成交额份额熵：资金是集中到少数行业还是全面扩散。
- 涨停行业分布熵：涨停家数在行业间的集中度。
- 连板梯队熵：1/2/3/4+ 板分布的均匀度。
- 涨跌家数熵和涨跌停比。
- 成交额“带宽”：全市场成交额、沪深300/中证500/中证1000 的成交占比。

### C. 策略共识熵

- 一只股票被多少条策略选中：`n_strategies`。
- 策略票数分布的熵：策略意见越分散，共识越低。
- 同一股票在候选池中的排名分位分布。
- AI 多空、研报 stance、威科夫、筹码评分的分歧度。

### D. 文本/事件熵（暂缓）

- 新闻主题分布熵。
- 公告事件类型熵。
- 研报评级/目标价分歧。
- 当前本地文本数据不足，不建议作为第一版核心特征。

## 3. 推荐建模方式

### 3.1 主任务：候选池概率评价

对每个候选股 `(code, date)` 建立：

```text
特征 = 策略先验
     + 个股熵特征
     + 行业/市场熵状态
     + 策略共识熵
     + 资金流/AI/研报/威科夫/筹码
     + 流动性/波动/ST/整手

标签 y = 1[未来 h 日收益 > 0] 或 1[未来 h 日超额 > 0]
```

推荐先做：

- `h = 5` 和 `h = 10` 两个 horizon。
- 主标签：相对中证500超额 > 0。
- 辅助标签：绝对收益 > 0。
- T+1 开盘可成交，扣双边成本，purged/embargo walk-forward。

输出不是单一“熵分”，而是：

```text
p_up_5d, p_excess_5d, p_up_10d,
entropy_coverage,
confidence_band,
top_drivers
```

概率必须校准：Brier score、log-loss、reliability curve、top-decile lift。

### 3.2 辅任务：市场熵覆盖层

不直接选股，只调整已有组合：

```text
gross_exposure_t = f(market_entropy_pct, ΔH, breadth, bandwidth)
n_candidates_t = g(market_entropy_pct, breadth)
```

候选假设：

- 熵高且宽度低：减少总仓位和候选数。
- 熵快速下降且宽度扩散：允许增加仓位。
- 熵极低且集中度极高：进入“拥挤/脆弱”降仓状态，而不是追高。

### 3.3 不建议的做法

- 不直接用 `H_t` 的横截面排序买股票。
- 不把“低熵”当买入信号。
- 不用当日全样本分布计算熵后再假设可以在同日收盘前交易。
- 不把新闻情绪、研报评级和价格熵混成一个未校准的“概率”。

## 4. 数据依赖与现有覆盖

| 数据 | 用途 | 当前来源 | 覆盖/限制 | 状态 |
|---|---|---|---|---|
| 日线 OHLCV/成交额/换手 | 日频熵、收益标签、流动性 | `assets/data/clean/ohlcv/market=CN` | 2016 至 2026-10，较完整 | ✅ 可用 |
| 5min K 线 | 日内排列熵/样本熵/时段熵 | `assets/data/raw/intraday_baostock/frequency=5min` | 约 2025-10 至 2026-10，只有约一年 | ⚠️ 仅研究/短窗 |
| 5min 日聚合特征 | 快速基线 | `assets/data/meta/cn_intraday_daily_features.parquet` | 124.7 万行，2025-10 至 2026-09 | ⚠️ 短 |
| 资金流 | 资金流熵、转移熵、流向确认 | `assets/data/derived/cn_moneyflow_features.parquet` | 2024-08 至 2026-10，三源 | ✅ 可用但样本约 2 年 |
| 涨停/连板面板 | 涨停行业熵、梯队熵 | `output/research/zt_board/zt_board_panel.parquet` | 2016 至 2026-10-08，当前需刷新 | ⚠️ 可补 |
| 行业/概念成分 | 行业/概念熵 | `assets/data/meta/cn_board_membership.parquet` | 约 1.5 万行，单快照，非 PIT | ❌ 不能做长历史无偏回测 |
| 策略名单/台账 | 策略共识熵、候选池 | `output/results_cn/selection_ledger.parquet` | 2025 起，约 3 万条 | ✅ 可用但历史短 |
| AI 多空 | 意见分歧/概率先验 | `output/results_cn/ai_pick_analysis/` | 部分日期、部分股票 | ⚠️ 有选择偏差 |
| 研报/公告证据 | 研报分歧、事件熵 | `bynewlt_research` / `cn_public_research` | 59/116 份 dossier；新闻字段多为空 | ⚠️ 只适合小样本 |
| 财务指标 | 质量/估值控制变量 | `cn_financial_indicators.parquet` | 2006 起，按公告日 | ✅ 可用 |
| ST/退市/名称变更 | PIT 过滤 | `cn_stock_namechange.parquet` | 覆盖较好 | ✅ 可用 |
| 龙虎榜 | 游资/机构事件熵 | `cn_lhb/lhb_daily.parquet` | 2026-09 起，约 1.7k 行 | ⚠️ 太短 |
| 估值日频 | PE/PB 分位控制 | `daily_basic_relay` | 月末快照为主，不是每日 | ⚠️ 只能做月频控制 |
| 新闻全文/社交媒体 | 文本主题熵 | 当前无完整历史库 | 只有零散公开证据 | ❌ 暂不可回测 |
| Level-2/订单簿 | 真实订单流熵 | 当前无 | 仅有资金流代理 | ❌ 不可用 |

## 5. 最小可行版本（MVP）

第一版不要等所有数据齐：

1. 日线收益分布熵（20/60/120 日）。
2. 成交量集中度熵（20/60 日）。
3. 市场行业/涨停分布熵（用行业分类或涨停面板）。
4. 策略共识熵（台账里 `n_strategies`、策略票数分布）。
5. 资金流确认/分歧。
6. 现有策略先验 + AI/研报/威科夫/筹码。

先对 100～150 只候选做 5 日和 10 日概率评价，并与当前 `candidate_rank`
（prior + AI + research + wyckoff + chip）做同窗对照。

## 6. 验收门槛

- 概率校准：Brier/log-loss 优于常数基线和逻辑回归基线。
- 排序：top-decile lift 稳定，且不是只靠某一天。
- 增量：相对现有 composite rank 的 IC/AUC/净超额有正增量。
- 风险：加入熵后最大回撤和换手不能显著恶化。
- PIT：所有特征在决策时点可得；5min/板块/研报按发布时间截断。
- 前向：至少 20～30 个独立决策日；不足则只展示，不给“可靠概率”标签。

## 7. 结论

**信息熵适合作为“概率模型的特征/状态变量”，不适合当前直接替代策略池做一个独立选股器。**

最合理的落地顺序是：

```text
候选池不变
→ 增加熵特征
→ 训练并校准 5/10 日上涨/超额概率
→ 只用于二次排序和仓位
→ 再用市场熵变化做总仓位覆盖
```
