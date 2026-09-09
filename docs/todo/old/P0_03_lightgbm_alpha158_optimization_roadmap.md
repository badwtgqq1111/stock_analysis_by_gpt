# LightGBM + Alpha158/GTJA191 选股优化审计与路线图

> 日期：2026-06-07  
> 范围：`alpha158_hk` 因子集、LightGBM 选股、智能画像主题特征、行业分层与组合约束  
> 目标：基于当前实现、用户提供的两份方法论笔记、Qlib/LightGBM/LambdaMART/Alphalens/Quant67 等公开资料，形成可落地的工业级优化清单。

## 结论

当前系统已经不是“原始 Alpha158 + LightGBM”的简单基线，而是：

```text
Qlib Alpha158
  + 港股自定义因子
  + GTJA191 风格因子
  + 新闻/另类情绪
  + 智能画像主题机会特征
  + 行业质量/估值/分层约束
  -> LightGBM MSE 回归 + CSRankNorm 标签 + rolling OOS
```

这个方向是对的，尤其是已经具备 `T+1` 执行延迟、20 日预测周期、横截面标签归一化、滚动样本外评估、早停、行业约束和主题画像特征入口。但它还没有完全达到用户笔记里描述的“工业级 Alpha158/GTJA191 + LightGBM 选股框架”：

1. **训练目标还不是 LambdaRank/RankXENDCG**：当前 `LightGBMRankerPipeline` 实际使用 `LGBMRegressor(objective=regression, metric=mse)`，不是按交易日 group 的 `LGBMRanker`。
2. **双重中性化已接入生产默认，但还缺 A/B**：训练面板现在带 `industry_l1/industry_l2/market_cap/log_market_cap`，默认做 `industry_size` 残差化；但需要 Purged CV 和 walk-forward A/B 验证收益/回撤权衡。
3. **特征预处理已接入，但还缺处理审计**：输入因子已按交易日做 robust winsorize/zscore/fill；后续仍需输出缺失率、裁剪率和 feature preprocess hash。
4. **动量陷阱诊断不够系统**：已经有 overheat/downtrend 罚分，但缺少模型层面的“动量家族重要性、持仓高位比例、TopK 近期涨幅暴露、风格/市值暴露”报告。
5. **GTJA191 目前是兼容性版本**：接口有 191 个稳定特征，但大量公式是 GTJA 风格代理；原始 GTJA 的横截面 `RANK` 在当前单股物化引擎里被滚动时序百分位替代。
6. **智能画像特征需要收益闭环约束**：主题特征已能进 LightGBM 面板，但前次诊断显示覆盖率、持仓命中和非零分桶表现还不足，不能直接加大 overlay 权重。

最优整合方案不是把所有功能都打开，而是：

```text
价格/量价因子作为主干
  -> 严格预处理和中性化
  -> LightGBM 回归基线 + LambdaRank 对照
  -> OOS/行业/风格/动量陷阱诊断
  -> 画像特征只作为可验证增量因子
  -> 组合层做行业、市值、换手、过热约束
```

## 外部资料要点

### Qlib Alpha158 基线

Qlib 官方数据框架把 Alpha158 作为内置 handler，并提供 `DropnaLabel`、`RobustZScoreNorm`、`CSZScoreNorm`、`CSRankNorm`、`Fillna` 等处理器。Qlib 文档也明确说明其标签会考虑可交易延迟，例如 `Ref($close, -2) / Ref($close, -1) - 1` 用于表达 T+1 买入、T+2 卖出的可交易收益。

对本项目的启发：

- 保留当前 `T+1` execution delay 是正确的。
- 保留 `CSRankNorm` 标签是合理基线。
- 需要把 Qlib 风格的 processor 补到输入特征端，而不是只处理 label。

参考：<https://qlib.readthedocs.io/en/latest/component/data.html>

### LightGBM 排序目标

LightGBM 官方支持 `lambdarank` 和 `rank_xendcg`，并要求 ranking 数据提供 query/group 信息；Python/R wrapper 可以通过 Dataset 的 `group` 参数传入。官方文档还说明 `rank_xendcg` 通常更快、表现与 `lambdarank` 接近，排序标签需要是整数 relevance。

对本项目的启发：

- 当前 `objective=mse` 不是错误，但它是 Qlib 风格回归基线，不是严格排序学习。
- 如果要落地 LambdaRank，需要按 `trade_date` 排序并生成 group sizes。
- 选股只关心 TopK 时，应配置 `ndcg_eval_at` 和 `lambdarank_truncation_level`，而不是默认全排序。

参考：<https://lightgbm.readthedocs.io/en/latest/Parameters.html>

### LambdaMART / NDCG

Microsoft Research 的 LambdaRank/LambdaMART 系列将排序指标变化转成梯度，核心适配的是“头部排序错误比尾部排序错误更严重”的问题。

对本项目的启发：

- LambdaRank 值得作为对照模型，但不应直接替代当前回归基线。
- 用户笔记里提到震荡市排序学习可能放大短期动量，这个判断成立；因此更适合做 A/B 和 ensemble，而不是一刀切。

参考：<https://www.microsoft.com/en-us/research/publication/from-ranknet-to-lambdarank-to-lambdamart-an-overview/>

### Alphalens 式因子诊断

Alphalens 的 tear sheet 支持 long-short、group neutral、by group、turnover、IC 等分析。其思想不是训练模型，而是先把单因子或模型分数的收益、IC、分组中性和换手看清楚。

对本项目的启发：

- 智能画像、GTJA191、行业特征都应该先经过因子诊断，再进入大权重生产。
- 当前已有 `theme-feature-diagnostics`，但还缺少统一的模型分数/特征家族/行业中性诊断。

参考：<https://quantopian.github.io/alphalens/alphalens.html>

### Quant67 机器学习选股工程纪律

Quant67 的《机器学习选股：标签构造、防过拟合、SHAP 归因》把 ML 选股拆成标签、特征、训练协议、解释和上线监控五层，和本项目当前阶段高度相关。它强调：

- ML 在量化里主要是信号合成器，不是替代数据层、组合层和执行层。
- 截面标签应优先使用 rank/正态化口径；Triple-Barrier 和 Meta-Labeling 更适合用于交易事件和仓位置信度增强。
- 特征处理要在每个 `trade_date` 截面内做 winsorize、z-score 或 rank transform，不能用全历史全局均值方差。
- 行业 + 市值中性化要在每个截面独立回归，残差再标准化。
- 固定窗口标签会产生相邻标签重叠，因此超参搜索不能用普通随机 K-Fold；应使用 walk-forward、Purged K-Fold、Embargo 或 CPCV。
- 上线模型必须有 SHAP/Permutation Importance/特征家族归因、模型版本化、训练推理一致性和线上漂移监控。

对本项目的启发：

- 当前 rolling OOS 是对的，但用于模型选择/超参搜索的验证协议还不够，应新增 `purged_cv` 作为研究端验证模式。
- 当前 metadata 有 OOS 指标和 top features，但还没有完整训练 manifest，无法复现每次模型所用特征 hash、预处理参数、依赖版本和 git commit。
- 当前特征解释更偏 gain importance，应该补 SHAP 持仓归因和 SHAP 漂移。
- 当前主题画像特征尤其需要 `available_at`/`evidence_updated_at` 约束，避免网页发布时间、抓取时间和可交易时间错位。

参考：<https://quant67.com/post/quant/12-ml-alpha/12-ml-alpha.html>

## 当前实现映射

| 模块 | 当前状态 | 评价 |
|---|---|---|
| 因子集 | `factor_engine/expressions/custom_factors.py` 中 `alpha158_hk` 已合并 Qlib Alpha158、9 个港股自定义因子、GTJA191 | 方向正确；特征空间已经足够大，后续重点是清洗、选择、诊断 |
| GTJA191 | `factor_engine/expressions/gtja_alpha.py` 输出 `GTJA001..GTJA191`，部分 exact-style，部分 proxy | 可以先进 LightGBM 做候选特征；不适合单独作为“精确 GTJA191”宣传 |
| 标签 | `factor_engine/ml/lightgbm_ranker.py` 使用 `forward_return_20`，先截面 winsorize，再 CSRankNorm | 合理基线；缺少行业/市值中性化标签选项 |
| 模型 | 当前是 `LGBMRegressor` / XGBoost / CatBoost 回归接口，metadata 中 objective=`mse` | 名字叫 Ranker，但实质是回归排序；需要明确文档和 CLI 命名 |
| 滚动训练 | expanding-window rolling，`trunc_days = label_horizon + execution_delay` | 这是重要优点，避免了主要的时间泄露 |
| 特征预处理 | `feature_preprocess=qlib_robust`，按 `trade_date` 做 winsorize/zscore/fill | 已接入生产；还缺缺失率/裁剪率审计 |
| 中性化 | 默认 `neutralization_mode=industry_size`，对特征和标签做行业 + log market cap 残差化 | 已接入生产；还缺 A/B 验证和命令行开关 |
| 画像特征 | `core/lightgbm_analysis.py` 合并 `theme_opportunity` 特征 | 入口已经打通，但需要覆盖率和 OOS 贡献约束 |
| 风控 | overheat/downtrend/tactical overlay、行业分层、HHI | 组合层已有框架；还应补市值桶和换手预算 |
| 训练协议 | expanding rolling OOS；OOS 指标已拆成 pure OOS，Purged CV split helper 已落地 | 还缺完整 `lightgbm-abtest` CLI |
| 解释与上线 | gain importance、部分前端解释、run manifest、基础 model manifest helper | 还缺 SHAP 持仓归因、SHAP 漂移和 manifest 落盘 |

## 与用户两份笔记的差距

### 1. “双重中性化”还没有工业级落地

用户笔记强调：

```text
X_neutral = residual(factor ~ industry_dummies + log_market_cap)
Y_neutral = residual(forward_return ~ industry_dummies + log_market_cap)
```

当前实现只做：

```text
feature_neutral = feature - cluster_mean
```

这两者不是一回事。`cluster_id` 是相关性聚类，不一定等于真实行业；减均值也无法剥离 log market cap、非线性缺失、行业小样本等问题。

优化方向：

- 新增 `neutralization_mode`：
  - `none`
  - `cluster_mean`
  - `industry`
  - `industry_size`
  - `industry_size_beta`
- 每个交易日独立回归，防止跨期泄露。
- 对特征和标签分别输出残差列，并保留原始列用于诊断。
- 中性化不是默认全开；应该先用 A/B 判断收益和回撤权衡。

### 2. “排序学习”不应直接替换 MSE

用户笔记提到 LambdaRank/NDCG 能直接优化 TopK，这个方向成立。但也指出震荡市可能放大短期动量。

当前实现的 MSE + CSRankNorm 有两个优点：

- 标签连续，保留截面排序信息。
- 对市场状态变化更稳，不一定比 LambdaRank 差。

建议落地为三模型对照：

| 模型 | 目标 | 用途 |
|---|---|---|
| `lgbm_regression_csrank` | `regression/mse` + CSRankNorm | 生产基线 |
| `lgbm_lambdarank` | `lambdarank/ndcg` + date group + relevance label | TopK 对照 |
| `hybrid_rank_regression` | 回归分 + ranker 分加权 ensemble | 生产候选，避免单目标偏置 |

不要写成“0.7 MSE + 0.3 NDCG”的单个 LightGBM loss，除非实现自定义 objective；更现实的是训练两个模型后做分数融合。

### 3. “动量陷阱”需要模型诊断，而不是只靠硬过滤

当前已有 `overheat_penalty_score`、`downtrend_penalty_score` 和部分价格位置因子，这是组合层保护。但如果模型本身严重依赖 `ROC/MOM/RSI/GTJA momentum`，硬过滤只能事后补救。

需要补：

- `feature_family_importance`：把特征按 `momentum/reversal/volume/volatility/value/quality/theme/industry/gtja_proxy` 分组，看 gain 占比。
- `selected_momentum_exposure`：持仓过去 5/20/60 日涨幅分位数、距 52 周高点、MA60 乖离。
- `topk_high_position_ratio`：TopK 中位于近 52 周高位或短期涨幅过热的比例。
- `momentum_trap_warning`：
  - 动量家族重要性 > 35%
  - TopK 过去 20 日涨幅分位 > 80% 的股票占比 > 50%
  - 高位桶未来收益低于中位桶

### 4. 智能画像特征必须服从 OOS 增益

前次 `theme-feature-diagnostics` 暴露过典型问题：

- 主题特征覆盖股票只有 162/1124。
- 大量持仓主题分为 0。
- 高分桶未必带来更高胜率。

所以主题画像在选股里应当遵循：

```text
先作为候选特征进入 LightGBM
  -> 只看 OOS feature contribution / SHAP / ablation
  -> 非零覆盖率和分桶收益达标
  -> 再允许小权重 overlay
```

建议默认保持：

```text
theme_overlay_strength = 0
theme_features_enabled = true
```

即让模型自己决定画像特征是否有用，而不是人工强行加分。

### 5. 超参搜索必须有 Purged CV / Embargo

当前 rolling OOS 适合生产预测和回测，但如果要比较：

- 中性化开关；
- LambdaRank vs MSE；
- 画像 overlay 权重；
- LightGBM 超参；
- GTJA proxy vs panel 版本；

就不能只看某一次 rolling 结果，也不能随机交叉验证。固定 20 日标签意味着相邻样本的标签窗口大量重叠，普通 K-Fold 会让训练集看到测试期的一部分行情。

建议新增研究协议：

```text
walk_forward_production:
  用于生产选股和最终 OOS 回放

purged_cv_research:
  用于超参、特征集、objective_mode A/B
  purge = label_horizon + execution_delay
  embargo = max(label_horizon, feature_lookback_sensitive_window)

cpcv_optional:
  用于重大模型升级前的稳健性验证
```

### 6. SHAP 归因要从“解释结果”升级为“模型监控”

gain importance 能告诉我们模型用过哪些特征，但不能回答“这只持仓为什么入选”和“模型近期行为有没有变”。Quant67 文档建议把 SHAP 连接到持仓归因、风格暴露监控和稳定性诊断，这正好补齐当前系统缺口。

建议输出：

```text
stock_shap_contribution:
  stock_code, trade_date, feature_name, shap_value, feature_family

portfolio_shap_exposure:
  trade_date, feature_family, weighted_shap, direction

shap_drift_report:
  feature_name, train_mean_shap, recent_mean_shap, sign_changed, ks_stat
```

当 top-10 特征的 SHAP 均值变号、某个特征家族贡献突然翻倍、或主题画像 SHAP 长期为 0 时，应触发人工复核。

## 优先级路线图

### P0：先补诊断，不改变生产选股

目标：先知道模型到底靠什么赚钱，避免“看起来收益高但其实是高位动量/小盘/行业 Beta”。

新增命令建议：

```bash
uv run python run.py lightgbm-model-diagnostics \
  --ranking-csv output/results_alpha158_hk_ranking.csv \
  --selected-csv output/results_alpha158_hk_selected.csv \
  --feature-importance-json output/lightgbm_feature_importance.json \
  --theme-feature-csv output/theme_opportunity_features.csv \
  --output-json output/lightgbm_model_diagnostics.json
```

诊断内容：

- 特征家族重要性占比。
- SHAP 持仓归因和特征家族归因。
- TopK 近期涨幅、52 周高位、MA60 乖离。
- 行业、市值、流动性、质量覆盖暴露。
- 主题画像非零覆盖、持仓命中、分桶收益。
- OOS IC 衰减、RankIC 稳定性、行业内 IC。
- 预测分布漂移：当前分数分布 vs 训练验证分布的 KS、均值、标准差。

验收标准：

- 单一特征家族 gain 占比不超过 35%。
- TopK 高位过热比例不超过 50%。
- 持仓里主题画像非零命中数逐步提升，但 overlay 未开启时不强求。
- OOS IC 与 RankIC 同向，且不是只由 1-2 个行业贡献。
- Top 特征 SHAP 方向稳定，没有在最近窗口集中变号。

### P1：补齐特征预处理和中性化

新增配置建议：

```text
feature_preprocess = qlib_robust
label_mode = raw_csrank | industry_size_neutral_csrank
feature_neutralization = none | industry | industry_size
```

实现细节：

- 每个 `trade_date` 独立处理。
- X 端：
  - replace inf -> nan
  - winsorize 1%/99% 或 MAD clip
  - `CSZScoreNorm` 或 `RobustZScoreNorm`
  - `CSZFillna` 或行业内中位数填充
- Y 端：
  - `forward_return_20`
  - 可选 `industry + log_market_cap` 回归残差
  - 残差再 CSRankNorm
- 元数据记录：
  - `feature_preprocess_hash`
  - `neutralization_mode`
  - `label_mode`
  - `market_cap_source`
  - `available_at_policy`
  - 缺失率和被裁剪比例

验收标准：

- OOS IC 允许略降，但回撤、行业暴露和高位动量集中度应下降。
- 低覆盖财务数据股票不因填充值获得异常高分。
- 训练日志可复现每次的 preprocessing config。
- 训练和推理使用同一套 transformer/列顺序，不允许隐式依赖 DataFrame 当前列排序。

### P2：加入 LambdaRank / RankXENDCG 对照

新增模型模式：

```text
--model-objective regression_csrank
--model-objective lambdarank
--model-objective rank_xendcg
--model-objective hybrid
```

实现要点：

- 使用 `LGBMRanker` 或 `lightgbm.Dataset(group=...)`。
- 数据必须按 `trade_date` 排序。
- group 为每个日期的股票数。
- relevance label 建议先用 20 档或 50 档，而不是 5 档。
- `ndcg_eval_at` 对齐 TopK，例如 `[10, 20, 50]`。
- `lambdarank_truncation_level` 设置为目标 TopK 略高，例如 `top_n + 5` 或 30。

生产策略：

- 先跑 A/B：
  - 回归基线
  - LambdaRank
  - RankXENDCG
  - 回归 + ranker ensemble
- A/B 用 `purged_cv_research` 做第一层过滤，再用 walk-forward 复核。
- 如果 LambdaRank 提升 TopK 收益但提高过热比例，则只纳入 hybrid 小权重。

### P2.5：加入研究端 Purged CV / Embargo

新增验证模式：

```bash
uv run python run.py lightgbm-abtest \
  --factor-set alpha158_hk \
  --validation-mode purged_cv \
  --n-splits 5 \
  --embargo-days 20 \
  --compare objective_mode,neutralization_mode,theme_features
```

实现要点：

- 每个样本记录 `label_start_date` 和 `label_end_date`。
- 测试折内的日期区间确定后，训练集 purge 所有标签窗口与测试区间重叠的样本。
- 测试折后追加 embargo，避免滞后特征吸收测试期行情冲击。
- 输出 fold-level IC、RankIC、Top-decile return、turnover、行业/市值暴露，而不是只输出均值。
- 所有 ablation 和超参搜索记录实验次数，避免多重检验污染。

### P3：组合层补市值桶、换手和拥挤度约束

当前行业约束和 HHI 已经有基础，但还应补：

- `market_cap_bucket_exposure`：大/中/小市值桶偏离。
- `turnover_budget`：限制模型频繁追热点。
- `crowding_score`：由成交额放大、换手、短期涨幅、社媒热度共同构成。
- `theme_concentration`：防止主题画像把组合推到同一叙事。

建议组合分数结构：

```text
final_score =
  model_score * 0.70
  + quality_score * 0.10
  + valuation_score * 0.08
  + industry_score * 0.07
  + verified_theme_score * 0.05
  - overheat_penalty
  - liquidity_penalty
  - concentration_penalty
```

注意：这些权重只是初始值，必须通过 walk-forward 重新校准；不要根据单次选股结果手工调权。

### P4：GTJA191 精确化

当前 `gtja_alpha191` 的价值是“给 LightGBM 更多 GTJA 风格候选特征”，不是“百分百复刻国泰君安 191 原始公式”。

要精确化，需要改因子物化架构：

- 从 single-stock transform 升级为 panel transform。
- 在同一 `trade_date` 横截面计算原始 `RANK(x)`。
- 保留现有 `GTJA001..GTJA191` 字段名，增加版本：
  - `gtja_alpha191_proxy_v0`
  - `gtja_alpha191_panel_v1`
- 跑 proxy vs panel 的边际 IC 和 LightGBM ablation。

验收标准：

- panel 版本能稳定输出 191 个字段。
- 与 proxy 版本相比，OOS IC/RankIC/TopK 回测至少有一项显著改善。
- 如果没有改善，生产继续用 proxy 或由特征选择自动剔除。

### P5：智能画像特征闭环

智能画像不是替代 Alpha158，而是提供“结构化基本面/产业链/叙事变化”的慢变量和催化变量。

需要补齐：

- 主题 taxonomy 自动扩展，但必须有 relevance gate。
- 每个主题输出全市场分数，不允许生产时 `--top-n` 截断。
- 画像特征写入 feature store 时记录：
  - source_count
  - evidence_quality
  - freshness_days
  - attention_velocity
  - graph_distance_to_theme
  - risk_penalty
- 每次选股后输出画像 ablation：
  - with theme features
  - without theme features
  - overlay 0 / 0.05 / 0.10
- 每条画像证据必须带 `available_at` 或可交易滞后策略：
  - 网页抓取时间；
  - 原文发布时间；
  - 证据进入 feature store 时间；
  - LightGBM 可使用的最早交易日。

验收标准：

- 主题特征覆盖率 > 50%。
- 高分桶未来收益/胜率优于 0 分桶。
- TopK 至少有一部分持仓具备正主题分，但主题分不应成为唯一入选原因。

### P6：模型版本化与上线监控

每次 LightGBM 训练/选股需要落盘：

```text
model_manifest:
  run_id
  git_commit
  python_version
  lightgbm_version
  pandas_version
  factor_set
  factor_set_version
  feature_columns
  feature_hash
  feature_preprocess_config
  neutralization_config
  label_config
  objective_mode
  train_date_range
  valid_date_range
  oos_metrics
  top_feature_families
  selected_stock_snapshot
```

线上监控建议：

| 指标 | 频率 | 触发条件 |
|---|---:|---|
| 日 IC / RankIC 移动均值 | 日 | 20 日均值低于训练均值 50% |
| 预测分布 KS | 日 | KS > 0.1 |
| 持仓换手 | 日 | 高于近 5 日均值 50% |
| 行业暴露 | 日 | 单行业偏离配置上限 |
| 市值桶暴露 | 日 | 小/中/大盘桶偏离配置上限 |
| SHAP 漂移 | 周 | Top 特征均值变号或贡献翻倍 |
| 主题画像覆盖 | 每次画像刷新 | 覆盖率下降或高分桶收益失效 |

触发告警后的处置顺序：

```text
人工复核 -> 降低 overlay/仓位 -> 强制重训 -> 回退上一模型 -> 暂停模型信号
```

## 推荐落地顺序

1. **先做 P0 诊断**：不改变收益曲线，最快发现当前收益是否来自高位动量、行业 Beta、小市值或主题特征噪声。
2. **再做 P1 预处理/中性化 A/B**：这是最可能提升实盘稳定性的部分。
3. **补 P2.5 Purged CV**：让后续所有超参、objective、画像权重对照有可靠研究协议。
4. **然后做 P2 排序学习对照**：LambdaRank 是增强项，不是默认替代项。
5. **并行做 P5 画像闭环**：画像特征只要覆盖率和 OOS 增益不过关，就继续作为候选特征，不加 overlay。
6. **补 P6 模型版本化与监控**：让每次结果可复现、可回退、可告警。
7. **最后做 P4 GTJA 精确化**：工程量较大，应该在诊断确认 GTJA 家族有贡献后再做。

## 建议新增文档/产物

| 产物 | 作用 |
|---|---|
| `output/lightgbm_model_diagnostics.json` | 每次训练的模型健康报告 |
| `output/lightgbm_feature_family_importance.csv` | 特征家族贡献 |
| `output/lightgbm_momentum_trap_report.csv` | 高位动量暴露 |
| `output/lightgbm_neutralization_abtest.csv` | 中性化 A/B |
| `output/lightgbm_objective_abtest.csv` | MSE/LambdaRank/Hybrid 对照 |
| `output/theme_feature_ablation.csv` | 画像特征收益贡献 |
| `output/lightgbm_purged_cv_report.csv` | Purged CV / Embargo 研究验证 |
| `assets/data/meta/model_manifests/*.json` | 模型、特征、预处理、依赖版本记录 |
| `output/lightgbm_shap_drift.csv` | SHAP 持仓归因和漂移监控 |

## 当前最需要修正的认知

1. **`alpha158_hk` 可以继续作为生产主因子集**，但现在已经包含 GTJA191，不需要生产命令再单独切到 `gtja_alpha191`。
2. **LightGBM 当前是回归排序基线，不是 LambdaRank**。这不是 bug，但需要在 README 和模型 metadata 里说清楚。
3. **收益高不等于画像特征有效**。画像是否有效要看 ablation、覆盖率、分桶收益和持仓命中。
4. **中性化不是越多越好**。对 long-only 港股组合，严格行业中性可能牺牲行业选择 alpha；应做 `none/industry/industry_size` 多档 A/B。
5. **动量不是错误，但过度动量是风险**。成熟系统应该允许模型使用趋势，同时识别“高位追涨集中”并降权。

## 近期开发任务清单

### 任务 1：模型诊断命令

文件：

- `run.py`
- `factor_engine/ml/lightgbm_ranker.py`
- 新增 `factor_engine/ml/diagnostics.py`

输出：

- 特征家族重要性
- 动量陷阱报告
- 行业/市值暴露
- 画像特征贡献
- SHAP 漂移
- 预测分布 KS

### 任务 2：Qlib 风格 X 端预处理

文件：

- `factor_engine/ml/preprocessing.py`
- `factor_engine/ml/lightgbm_ranker.py`

能力：

- per-date winsorize
- robust zscore
- CS fillna
- preprocessing metadata

### 任务 3：行业 + 市值双重中性化

文件：

- `factor_engine/ml/neutralization.py`
- `core/lightgbm_analysis.py`
- `factor_engine/ml/lightgbm_ranker.py`

能力：

- feature neutralization
- label neutralization
- neutralization A/B report

### 任务 4：LambdaRank 对照模型

文件：

- `factor_engine/ml/lightgbm_ranker.py`

能力：

- `objective_mode`
- date group construction
- relevance label generation
- NDCG@K OOS report

### 任务 5：主题画像 ablation

文件：

- `run.py`
- `data/ingest/service.py`
- `core/lightgbm_analysis.py`

能力：

- with/without theme feature 对比
- overlay strength sweep
- coverage/return bucket 自动报告

### 任务 6：Purged CV 与模型 manifest

文件：

- `factor_engine/ml/validation.py`
- `factor_engine/ml/model_manifest.py`
- `run.py`

能力：

- Purged K-Fold / Embargo
- fold-level ablation report
- 训练/推理配置 manifest
- 依赖版本和 git commit 记录

## 成功标准

短期成功不是“单次组合预估收益更高”，而是：

- OOS IC/RankIC 稳定，衰减可解释。
- TopK 不再被单一动量/行业/市值暴露主导。
- 中性化后回撤或过热暴露下降，收益没有灾难性损失。
- 智能画像特征在 ablation 中贡献为正，或被模型自然降权。
- 每次选股结果都能回答：
  - 模型主要靠哪些特征家族？
  - 持仓是否追高？
  - 行业和市值是否集中？
  - 画像特征有没有真实贡献？
  - 这次收益来自个股 alpha、行业 beta，还是市场风格？
  - 这次模型是否可以被完整复现和回退？
  - 最近预测分布、IC 和 SHAP 是否发生漂移？

这才是从“能跑出高收益”走向“能长期迭代、能解释、能风控”的关键。
