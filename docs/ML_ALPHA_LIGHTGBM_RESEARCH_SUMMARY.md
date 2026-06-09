# 机器学习选股与 LightGBM Alpha 信号研究总结

> 目标：总结 Quant67《机器学习选股：标签构造、防过拟合、SHAP 归因》，结合 LightGBM / Qlib / 机器学习资产定价论文，明确本项目 `alpha158_hk + LightGBM` 的标签、特征、验证和诊断实现方向。
>
> 当前日期：2026-06-08

## 一句话结论

机器学习选股的主战场不是“换更复杂模型”，而是把数据、因子和标签严格对齐后，用监督学习把数百个因子压缩成一个可验证、可解释、可监控的截面排序信号。对本项目来说，LightGBM 的生产默认应继续使用 **20 日未来收益的截面 rank-normalized 回归标签**；`lambdarank/rank_xendcg` 应作为 TopK 对照模型，用 `trade_date` 做 query group、用未来收益截面分位生成整数 relevance label。所有目标函数、特征集和中性化方案必须通过 walk-forward / purged CV / paper-trading A/B 验证后再切生产。

## 1. 原文完整总结

原文链接：<https://quant67.com/post/quant/12-ml-alpha/12-ml-alpha.html>

### 1.1 ML 在量化系统里的位置

原文把机构级量化系统拆成五层：数据层、因子层、信号层、组合层、执行层。机器学习可以出现在每一层，但本文聚焦第三层，也就是“信号合成”。

对选股任务，监督学习问题可写成：

```text
给定股票 i 在截面 t 的因子向量 x_{i,t}，
预测它在未来持仓窗口 [t, t+h] 内的相对收益/排名 y_{i,t}。
```

这个定位非常重要：ML 不是凭空发现 alpha，而是把研究员已经构造好的动量、反转、波动、估值、质量、主题、情绪等因子做非线性合成。它的基准不是“没有模型”，而是：

- 行业市值中性后的等权因子打分；
- Fama-MacBeth / Ridge / Lasso 截面线性模型；
- Qlib 风格 LightGBM MSE 回归基线。

如果 ML 相对这些基线没有稳定 OOS 提升，就说明复杂模型没有提供足够边际价值。

### 1.2 标签构造是第一性问题

原文强调，标签不是简单的“未来 5 日收益”。至少要定义：

- 收益口径：简单收益、对数收益、风险调整收益；
- 持仓窗口：1/5/10/20/60 日；
- 截面口径：原始收益、分位标签、rank-normalized 连续标签；
- 执行延迟：T 日信号是否只能 T+1 交易；
- 特殊状态：停牌、退市、复权、ST/风险警示；
- 标签分布：长尾、偏态、类别不均衡。

原文推荐的工业折中是 **截面 rank 转换的连续标签**：每个交易日内按未来收益排序，再做 rank 或 inverse-normal 变换。这样保留全部样本，并让模型学习“相对位次”，而不是不同市场环境下不可比的绝对涨跌幅。

Triple-Barrier / Meta-Labeling 更贴近真实交易退出机制，但实现复杂、参数敏感。它更适合作为下一阶段“风控/仓位置信度标签”，不建议直接替代当前主排序标签。

### 1.3 特征工程的核心纪律

原文的特征工程主线有四条：

- 截面去极值、标准化、rank / inverse-normal 变换；
- 行业 + 市值中性化，剥离行业 beta 和大小盘风格污染；
- 所有非行情特征必须有 `available_at`，按真实可得时间滞后；
- 滚动统计必须 shift 或使用 left-closed window，避免当前/未来泄漏。

尤其是行业市值中性化：对每个交易日，把因子对行业哑变量和 `log(market_cap)` 做截面回归，取残差作为中性化特征。原文提醒，中性化后还应再做 z-score 或 rank，否则尺度仍不统一。

### 1.4 模型选择：LightGBM 是合适工作点，但不是免检通行证

原文认为，LightGBM / XGBoost / CatBoost 是过去十年量化选股的主力非线性模型，优势是：

- 能处理非线性交互；
- 对缺失值和混合类型变量友好；
- 在 10 万到 1000 万行、50 到 500 个特征的表格面板上训练稳定；
- CPU 训练速度快。

但金融样本的“等价独立样本”远小于面板行数。10 年、数千股票、20 日标签窗口下，横截面相关和纵向标签重叠会显著降低有效样本量。因此 LightGBM 应控制复杂度：

- `num_leaves`: 64 到 128 起步；
- `max_depth`: 6 到 8 或适度限制；
- `min_child_samples`: 至少几十到几百；
- 强 L1/L2 正则；
- 子样本和特征采样；
- 早停和 rolling OOS。

### 1.5 防过拟合的重点是训练协议

原文把金融 ML 过拟合拆成三类：

- 参数太多、样本太少；
- 金融数据违反 IID：截面相关 + 标签窗口重叠；
- 多重检验：标签、特征、模型、超参反复试，最优结果被研究流程污染。

对应训练协议：

- 不用随机 K-Fold；
- 用 walk-forward；
- 对固定窗口标签使用 Purged K-Fold；
- 测试折后设置 Embargo；
- 超参搜索 trial 数量要受控；
- 对目标函数、特征集、主题 overlay 等做正式 A/B 记录。

### 1.6 模型解释和上线工程

原文认为上线模型必须回答：

- 本期预测由哪些因子贡献；
- 全局 top features 是否稳定；
- 最近模型行为是否漂移。

推荐监控：

- 日 IC / Rank IC 及 20/60 日均值；
- 预测分布 KS、均值、标准差；
- SHAP 均值和 top-10 特征漂移；
- 行业暴露、单票权重、换手；
- 新模型 paper-trading 1 到 3 个月后再切生产。

这与本项目当前已有的 `lightgbm_model_manifest.json`、`lightgbm_feature_importance.json`、`lightgbm-model-diagnostics` 方向一致，但还需要把 Purged CV 和线上漂移监控补完整。

## 2. 相关论文与资料综合

### 2.1 机器学习资产定价

Gu, Kelly, Xiu 的《Empirical Asset Pricing via Machine Learning》是机器学习横截面收益预测的核心参考。论文系统比较了线性、树模型和神经网络等方法，结论是树模型和神经网络通过捕捉非线性和交互项，能在资产风险溢价预测中带来经济收益，但必须防止高维过拟合。

对本项目启发：

- LightGBM 是合理基线，不是“落后模型”；
- 非线性收益来自稳定交互，而不是盲目堆因子；
- 输出应同时看统计指标和经济指标：Rank IC、TopK 收益、回撤、换手、成本后表现。

### 2.2 Qlib Alpha158 + LightGBM

Qlib 的 LightGBM Alpha158 benchmark 使用 `Alpha158` handler、`LGBModel(loss=mse)`、`TopkDropoutStrategy`，并把模型预测作为组合信号。它本质上是一个 **MSE 回归预测 + TopK 组合构建** 的工业基线。

对本项目启发：

- 当前 `regression_csrank` 默认目标与 Qlib 基线一致；
- `alpha158_hk` 在 Alpha158 基础上加入港股自定义因子和 GTJA191，特征空间已经足够大；
- 重点应转向标签质量、中性化、验证协议、组合约束，而不是继续无约束堆特征。

### 2.3 LambdaRank / LambdaMART

Burges 的 LambdaRank / LambdaMART 系列来自 Learning-to-Rank。核心思想是直接优化排序质量，尤其是 NDCG 这类上位排序指标。LightGBM 官方支持 `lambdarank` 和 `rank_xendcg`；ranking 数据需要提供 group/query 信息，`rank_xendcg` 是 XE_NDCG_MART 目标，通常训练更快、效果接近 `lambdarank`。

对选股的映射：

- query/group = `trade_date`；
- documents/items = 当天股票池；
- features = 当天每只股票的因子；
- label = 未来收益在当天截面的整数 relevance；
- metric = `ndcg@top_n`、`ndcg@20`、Rank IC、TopK OOS 收益。

LambdaRank 适合做 TopK 对照，但不要替代默认生产基线，除非在 OOS 和 paper-trading 中证明收益提升没有伴随追高、换手、行业拥挤和回撤恶化。

### 2.4 金融文本与事件因子

FinBERT 系列论文说明，金融领域预训练语言模型在金融文本情绪分类上优于通用 BERT、词典法和传统 ML。它适合把公告、研报、电话会、新闻转为结构化特征。

对本项目启发：

- 文本不应直接变成最终买卖建议；
- 应先转成可验证的事件/主题/情绪特征；
- 需要 `event_time`、`publish_time`、`available_at` 三个时间戳；
- 需要去重、来源可靠性、实体解析和证据质量评分；
- 用 `theme-feature-diagnostics` / ablation 验证文本特征是否有 OOS 边际贡献。

### 2.5 高频微结构因子

DeepLOB 和 Sirignano/Cont 的限价订单簿深度学习研究说明，CNN/LSTM/空间神经网络可以从订单簿深层结构中学习短期价格变动特征。DeepLOB 使用卷积捕捉订单簿空间结构，用 LSTM 捕捉时间依赖，并强调跨标的泛化。

对本项目启发：

- 分钟线、tick、订单簿因子属于“因子层”，不是日频 LightGBM 的直接替代；
- 可以先把高频模型输出聚合成日频特征，如开盘后买压、尾盘冲击、盘口不平衡、短期流动性恶化；
- 港股数据可得性和成本要先评估，订单簿模型应独立成高频特征管线。

## 3. 本项目 LightGBM 的目标与特征应怎么实现

### 3.1 推荐生产默认：CSRankNorm 回归排序

生产默认继续用：

```text
objective_mode = regression_csrank
label_horizon = 20
execution_delay = 1
target = T+1 可执行后的未来 20 日收益
label = 每个 trade_date 内做截面 rank-normalized
metric = Rank IC / IR / TopK return / drawdown / turnover
```

原因：

- 与 Qlib Alpha158 LightGBM 基线一致；
- 连续 rank 标签保留全部样本，比 top/bottom 二分类更省样本；
- 对异常行情的绝对收益尺度更鲁棒；
- 输出分数可直接进入组合层做行业、过热、换手、权重约束。

### 3.2 LambdaRank 对照：整数 relevance + date group

`--model-objective lambdarank` / `rank_xendcg` 应这样定义：

```text
group/query: trade_date
X: 当天股票池全部特征
y_raw: future_return_20
y_relevance: 当天截面分位桶，例如 0..19
eval_at: [top_n, 20, 50]
lambdarank_truncation_level: max(30, top_n + 5)
```

注意点：

- label 必须是整数 relevance，不是连续收益；
- 每个 group 内至少要有足够股票，否则 NDCG 不稳定；
- group 必须按 `trade_date` 排序；
- 失败不能 fallback 到 regression，否则对照无效；
- `rank_xendcg` 可作为第二个 ranking objective 对照。

### 3.3 特征层建议

当前生产特征分三类：

| 特征族 | 当前状态 | 建议 |
|---|---|---|
| `alpha158_hk` | Qlib Alpha158 + 港股定制 + GTJA191 | 继续作为主特征集 |
| 行业/市值 | 已进入训练面板 | 默认 `industry_size` 中性化，但保留 `none/industry/industry_size` A/B |
| 主题画像 | 可进入 LightGBM | 默认可训练但 overlay=0；只有 ablation 有贡献才加权 |
| 价格过热/追高 | 已有诊断字段 | 既做特征，也做组合层惩罚与红旗诊断 |
| 文本/NLP 事件 | 部分通过主题特征进入 | 补 `available_at`、事件类型、证据质量、去重 |
| 高频微结构 | 暂无日频聚合 | 中长期建设，不要阻塞日频选股 |

### 3.4 特征预处理顺序

推荐顺序：

```text
raw features
-> PIT/available_at 过滤
-> 每日截面去极值
-> 行业/市值中性化（可选 none / industry / industry_size）
-> 每日截面 rank/zscore/robust normalize
-> 缺失值填充
-> LightGBM
```

对 GBDT，标准化不是为了数值稳定，而是为了：

- 降低异常值影响；
- 提高特征重要性和 SHAP 可比性；
- 保持训练/推理一致；
- 让中性化后的尺度统一。

### 3.5 标签口径建议

短期必须补齐的标签元数据：

```json
{
  "label_horizon": 20,
  "execution_delay": 1,
  "return_type": "forward_adjusted_close_return",
  "label_method": "CSRankNorm",
  "suspension_policy": "exclude_or_forward_fill_with_resume_gap_policy",
  "delisting_policy": "explicit",
  "universe_filter_time": "as_of_trade_date",
  "available_at_cutoff": "trade_date_close_or_next_open"
}
```

中期可加两条研究标签：

- `risk_adjusted_forward_return_20`: 未来收益 / 过去 20 日波动；
- `triple_barrier_meta_label`: 用于仓位置信度或过滤，不直接替代主排序标签。

### 3.6 验证与诊断必须成为门槛

任一新目标/特征上线必须通过：

- rolling OOS；
- Purged CV / Embargo；
- `none/industry/industry_size` 中性化 A/B；
- `regression_csrank/lambdarank/rank_xendcg` objective A/B；
- theme on/off ablation；
- 成本后组合回测；
- 追高诊断：`selected_high_chase_rate`、60/120 日翻倍比例、52 周高位比例；
- feature family importance；
- paper trading。

### 3.7 行业实践：模型不负责自控，组合层负责纪律

行业内通常不会把 LightGBM / XGBoost / 神经网络的 TopK 直接买入。更常见的分层结构是：

```text
alpha model
-> risk model
-> transaction cost model
-> portfolio optimizer / constraint engine
-> execution
```

也就是说，模型只负责输出相对收益信号，组合层负责回答“能不能买、买多少、是否值得承担这笔交易成本”。机构实践里，Barra / Axioma 一类多因子风险模型会把组合暴露拆成行业、规模、动量、波动率、流动性等维度；组合优化器再把 alpha、风险、成本、换手和各种约束一起放进目标函数，而不是简单买模型排名前十。

因此，“一个月几倍”的股票被模型排到前面并不罕见。真正的问题不是模型会不会偏爱强动量，而是组合层有没有把追高、拥挤、流动性、冲击成本和事件风险挡住。行业内通常会把这类票放进 watchlist 或 momentum-only 策略，而不是混进普通 Alpha TopN 组合。

### 3.8 反追高：必须是硬闸门，而不是模型愿望

LightGBM 可以把短期强势学成有效特征，也可能把短期暴涨误学成 alpha。仅靠 `CSRankNorm`、中性化和 Purged CV 只能降低追高倾向，不能保证避免追高。反追高必须有组合层硬约束：

```text
LightGBM ranking_score
-> high_chase_score 过滤
-> 20/60/120 日 multibagger 过滤
-> 52w high / ma_gap 过滤
-> liquidity / ADV 过滤
-> industry/theme cap
-> cost_adjusted_score
-> final TopN
```

建议先用保守阈值：

| 风险项 | 建议规则 | 处理 |
|---|---|---|
| 20 日涨幅过大 | `price_return_20d_pct > 80` | 默认不新买，进入 watchlist |
| 60 日涨幅过大 | `price_return_60d_pct >= 100` | 默认禁入 selected |
| 120 日涨幅过大 | `price_return_120d_pct >= 180` | 默认禁入 selected |
| 综合追高分 | `high_chase_score >= 80` | 禁入 selected，除非有显式人工豁免 |
| 接近 52 周高点 | `price_position_52w_high >= 95` 且 `ma60_gap_pct > 40` | 禁入或强降权 |
| TopK 追高集中 | `selected_high_chase_rate > 20%` | 整体组合不通过诊断 |
| 60 日翻倍持仓 | `selected_60d_multibagger_rate > 0` | 生产默认不通过诊断 |

这些阈值不是永久参数，而是初始安全边界。后续可以通过 OOS / paper-trading 调整，但不能让模型输出绕过它们。

更细的行业做法是把反追高分成三类：

- **禁入规则**：短期暴涨、极高乖离、流动性异常、事件不可解释；
- **降权规则**：高位但仍有基本面/主题证据，降低目标权重；
- **隔离策略**：如果确实要做强动量，把它放到单独策略桶，单独设置止损、换手、容量和回撤预算。

本项目当前最重要的生产门槛应是：

```text
selected_high_chase_rate <= 0.20
selected_60d_multibagger_rate == 0
selected_120d_multibagger_rate == 0
Top10 中 high_chase_score >= 80 的股票数量 == 0
```

如果这些指标不达标，说明模型排序可能有 alpha，但组合层还没有达到生产纪律。

## 4. 建议落地路线

### P0：巩固当前 LightGBM 主线

- 默认 `factor_set=alpha158_hk`；
- 默认 `objective_mode=regression_csrank`；
- 默认 `neutralization_mode=industry_size`，但保留命令行 A/B；
- 训练输出 manifest、feature hash、特征列表、objective、label config；
- 诊断命令必须能读取 ranking、selected、feature importance。

### P1：完成研究协议

- 把 `purged_time_series_splits` 接到真实 LightGBM A/B；
- 输出 `lightgbm_purged_cv_report.csv/json`；
- 限制超参搜索 trial 数；
- 记录每次试验，避免多重检验污染。

### P2：LambdaRank 正式对照

运行：

```bash
uv run python run.py select \
  --analysis-mode lightgbm \
  --model-objective lambdarank \
  --top-n 10 \
  --days 365 \
  --factor-set alpha158_hk \
  --export-csv output/results_lambdarank \
  --show-progress
```

对照项：

- `regression_csrank` vs `lambdarank` vs `rank_xendcg`；
- `none` vs `industry` vs `industry_size`；
- theme on/off；
- `top_n=10/20/50`。

### P2.5：补反追高组合闸门

状态：已落地第一版。组合层已使用统一 `HIGH_CHASE_GUARD`，并导出机器可读阻断字段；诊断层已增加 `production_gate_pass` / `production_gate_failures`。

把反追高从诊断项升级为组合构建硬约束：

- `high_chase_score >= 80` 默认不能进入 selected；
- `price_return_60d_pct >= 100` 默认不能进入 selected；
- `price_return_120d_pct >= 180` 默认不能进入 selected；
- 接近 52 周高点且 `ma60_gap_pct` 过高时强降权；
- Top10 中最多允许 0 到 1 只高追高候选；
- 导出 `blocked_by_high_chase`、`blocked_by_multibagger`、`blocked_by_52w_ma_gap` 原因字段。

同时保留 watchlist：强势票可以被记录和跟踪，但不能未经约束进入生产持仓。

### P3：文本事件因子工程化

不要把公告/NLP直接喂给 LLM 做主观判断。应输出结构化日频特征：

```text
event_count_1d/5d/20d
positive_event_score
negative_event_score
policy_tailwind_score
earnings_guidance_score
litigation_risk_score
management_change_score
evidence_quality
source_diversity
duplicate_cluster_count
```

并用 `available_at` 防未来函数。

### P4：高频微结构因子

先做低成本分钟线聚合：

```text
open_30m_return
close_30m_return
intraday_vwap_gap
volume_u_shape_deviation
large_trade_imbalance
turnover_burst_score
closing_auction_pressure
```

订单簿深度模型等数据稳定后再上。

## 5. 参考资料

- Quant67：《机器学习选股：标签构造、防过拟合、SHAP 归因》：<https://quant67.com/post/quant/12-ml-alpha/12-ml-alpha.html>
- Gu, Kelly, Xiu, “Empirical Asset Pricing via Machine Learning”, Review of Financial Studies, 2020：<https://academic.oup.com/rfs/article/33/5/2223/5758276>
- Microsoft Qlib LightGBM Alpha158 benchmark：<https://github.com/microsoft/qlib/blob/main/examples/benchmarks/LightGBM/workflow_config_lightgbm_Alpha158.yaml>
- Burges, “From RankNet to LambdaRank to LambdaMART: An Overview”：<https://docslib.org/doc/11743505/from-ranknet-to-lambdarank-to-lambdamart-an-overview>
- LightGBM Parameters：<https://lightgbm.readthedocs.io/en/v4.3.0/Parameters.html>
- DeepLOB, “Deep Convolutional Neural Networks for Limit Order Books”：<https://arxiv.org/abs/1808.03668>
- Sirignano, “Deep learning for limit order books”：<https://experts.illinois.edu/en/publications/deep-learning-for-limit-order-books>
- FinBERT financial text model：<https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3910214>
