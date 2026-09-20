# P1.17 路径标签、元标签门禁与“刚启动”特征落地方案

> 状态：W1/W3 已落地；W2 元标签门禁已实现并在四折 OOS 上完成判定——**学习型门禁不通过**（见 3.2），唯一通过的是显式阈值规则门禁（`meta_gate_mode = "rule"`，`dist_from_120d_low <= 0.15`）；W5 的可执行收益与超额收益标签已落地（基准 = 全市场等权）。四折 OOS 覆盖 40 个测试日期和 124,485 个共同样本（候选集 12,470 行）。路径标签下 LightGBM RankIC=0.1056、Transformer RankIC=0.0726，LightGBM 的 RankIC IR=1.403、Transformer=0.446，因此现阶段明确 LightGBM 为主模型，Transformer 不进入固定权重生产集成；W4/W6/W7 待验证。阻断项 1（资金流 PIT 审计）已关闭，见 0.2。路径标签默认不切换生产目标，需在配置中显式启用。
> 日期：2026-09-20
> 关联：`P0_12_transformer_oos_evaluation_plan.md`、`P1_15_moneyflow_plan.md`、`P1_15_moneyflow_evaluation_20260918.md`、`P1_16_three_locks_capital_features_plan.md`
> 目标：把选股目标从“未来 20 日截面收益排名”改为可表达“半年低点启动、涨幅有限、资金活跃、后续能吃到波段”的路径标签，并以元标签门禁替代信号重排，使二次资金流等稀疏事件信号进入可用位置。

### 0.1 首批落地（2026-09-20）

- `strategy_labels.py` 新增 T+1 开盘入场的 `label_mfe_*d`、`label_mae_*d`、三重障碍 `label_tb_*`、`label_path_score_*d` 与 `startup_price_eligible`。
- `run_cn_pipeline.py --stage strategy_labels` 支持路径窗口、低点距离、止盈止损参数；默认仍保留原研究标签。
- LightGBM/Transformer/OOS 可通过 `label_mode = "path_score_20d"` 和 `startup_only = true` 显式切换到启动路径目标；路径模式默认使用 60 日 embargo。
- Transformer/CNN 的稀疏二次资金流字段支持 `protected_features` 白名单，并在 manifest 中记录 applied/missing 字段；白名单从特征预算中扣减。
- 当前未实现元标签二级模型、训练折内中性化和在线聚合；不得把本次路径标签实现描述为已完成的 OOS 增益。
- 对齐修复：Transformer OOS 的 expanding split 仅按有标签启动端点建折；序列训练保留无标签的启动前历史作为上下文，监督只落在启动端点；OOS 配置与正式训练统一为 256 对特征、6 个资金流保护字段、20 epochs、3 seeds（OOS 评估使用同一特征准入与训练预算）。
- 本轮验证：`output/oos_predictions_p1_17_startup_aligned/` 已生成四折 LightGBM 与 Transformer 预测；`output/evaluations/cn_model_comparison_p1_17_startup_aligned.{md,json,csv}` 已生成。序列划分统一为整数日期键，并将 OOS `min_train_days` 提高到 180，避免 60 日 purge 后首折无训练上下文。四折共同样本上，LightGBM 的 Top 分位目标均值 0.0556、多空均值 0.0423；Transformer 分别为 0.0451 和 0.0242。该比较是路径标签目标的排序评估，不是成本后收益回测。

### 0.2 第二批落地（2026-09-20，W2 元标签门禁 + PIT 审计 + W5 标签）

**（1）阻断项 0/PIT 审计已关闭。** 审计结论：`cn_moneyflow.py` 的二次确认与滚动字段只使用当日及以前数据（`shift(1..4)`、`rolling` 均为后视），面板按 `(stock_code, trade_date)` 同日合并，因此"T 日盘后可得、T+1 开盘执行"是唯一合法口径。收口方式为**显式声明**而非移位：`oos_predictions.AVAILABILITY_RULE` 记录 `decision_point=session close of trade_date`、`execution_point=next session open`、`entry_delay_sessions=1`，并写入每个 OOS manifest；`test/test_cn_moneyflow.py` 新增两个截断不变性测试（用 T 之后的数据重算，T 及以前的特征必须逐列相等），`test/test_meta_labeling.py` 断言 manifest 中的 `entry_delay_sessions == 1`。

**（2）W2 元标签门禁已实现。** 新增 `factor_engine/ml/meta_labeling.py`（候选筛选、二级标签构造、门禁训练/打分/放行、扣成本评估、报告落盘），`oos_predictions.generate_lightgbm_meta_oos_predictions` 实现**嵌套内层**设计：每折训练窗尾部留作内层门禁块，与其前段之间留 `inner_purge_days`，先用内层主模型给内层块打分，再在候选集上训练二级模型，测试块由全训练窗主模型打分后过门禁。配置在 `[meta_labeling]`，两种形态（`learned` / `rule`）输出同一组 `meta_act` / `meta_weight` 列，单行可切回。

**（3）W2 判定：学习型门禁不通过，规则门禁通过。** 判定基于两次端到端管线运行（`config/cn_pipeline_p1_17_meta_learned.toml` 与 `config/cn_pipeline_p1_17_meta_rule.toml`，各 4 折、40 个决策日、12,469 条候选），详见 3.2 与 3.8。

**（4）W5 第一步已落地。** `strategy_labels.py` 新增 `forward_exec_return_{5,10,20,60}d`（T+1 开盘买入、第 h 个交易日收盘计价）与 `forward_excess_return_*`（减去同日全市场等权基准）。基准口径决策：**全市场等权**，因为仓库没有 PIT 有效的历史行业映射（见 1.4），行业等权要等 `industry_mapping_path` 具备 `available_at` 后才能作为并列口径加入。

### 0.3 特征画像与"能不能只留基础量价+资金流"（2026-09-20 实测）

**问题**：扩样本后全量 683 因子是否太慢，能否只留"基础 OHLCV + 半年底部/短期回调 + 资金流"。

**成本实测**（同一 4 折 OOS、同一 gate 形态，本机）：

| 画像 | 因子数/模型列数 | 面板读入 | 面板内存 | 4 折 OOS 用时 | RankIC(路径标签) | RankIC(20 日超额) | Top-20 扣成本 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `full` | 683 / 1366 | 2.6s | 4.6 GB | 266s | +0.1056 | **+0.1437** (IR 1.52) | **+3.39%** |
| `compact`（量价+资金流族） | 141 / 282 | 0.7s | 1.1 GB | 109s | −0.0415 | −0.0084 | −1.98% |
| `core`（人工短名单） | 57 / 114 | 0.7s | 1.1 GB | 77s | −0.0328 | −0.0218 | −2.65% |

**结论：不能用直觉短名单替代全量。** 缩到 141/57 个因子后 RankIC 归零甚至转负——不是"少学一点"，而是**信号来源被删掉了**。全量模型 4 折平均 gain 归因（`output/oos_predictions_p1_17_profile_full/lightgbm_fold_*/model.txt`）显示权重分散在各族：TA 8.1%、moneyflow 7.4%、pv 6.2%、ALPHA101 6.1%、valuation 6.0%、academic 3.6%、liquidity 3.4%、financial 3.3%、tr 3.0%；单特征头部还有 `pb_ind_pct`(3.8%)、`valuation_pb`(2.0%)、`valuation_pe`(1.5%)、`valuation_market_cap_log`(1.5%)、`tr_implied_float_value`(2.0%)。也就是说，"估值/规模/质量"这一族（`compact`/`core` 都排除了）承担了可观权重的排序信息。

**成本结论：特征数不是扩样本的瓶颈。**
- 全量 4 折 OOS 只要 266s，扩到 13 折约 14 分钟/次；特征数只影响读入（2.6s vs 0.7s）与内存（4.6 GB vs 1.1 GB），不影响可行性。
- 真正贵的是**一次性重建 clean panel**（读 29 亿行长表）。而因子库按 market/exchange/…/year 分区、**不按 factor_name 分区**，所以"只读 60 个因子"并不能减少这次扫描——缩特征对这一步没有帮助。

**文献对照**（本节实测与文献的对应关系）：
- "一次性全塞"是主流基准做法，但前提是特征池本来就小而手工设计：Qlib 官方 LightGBM 基准 `DatasetH(handler=Alpha158)` 直接给全部 158 个特征 + 单标签，靠 `colsample_bytree=0.888` 与 `lambda_l1=205.7 / lambda_l2=581.0` 做隐式选择（`qlib/examples/benchmarks/LightGBM/workflow_config_lightgbm_Alpha158.yaml`）。
- "按模型容量决定要不要筛"同样有官方先例：**同一套 Alpha158**，Qlib 的 Transformer 基准在 `infer_processors` 里挂了 `FilterCol`，显式剔除 20 列（RESI5/WVMA5/RSQR5/KLEN/CORR5/CORD5/ROC60/…；`qlib/examples/benchmarks/Transformer/workflow_config_transformer_Alpha158.yaml`）。
- 真正面向"大候选池"的筛选姿势是**去冗余与稳定性**，不是主观短名单：DoubleEnsemble（arXiv 2010.01265，qlib `contrib/model/double_ensemble.py` 的 `feature_selection(df_train, loss_values)`）在模型内学特征 mask；因子挖掘系（Alpha-GPT / Chain-of-Alpha / AlphaForge 2406.18394 / AlphaSeek / R&D-Agent-Quant）在候选池上千时必须筛，而 RD-Agent 本地实现 `rdagent/scenarios/qlib/developer/factor_runner.py::deduplicate_new_factors` 用的是**逐日截面相关性**（IC_max 超阈值即丢弃新因子）——筛的是冗余，不是预测力。
- "把所有标签一起塞"（多任务）是少数派：MTL 文献存在（1809.10336、MiM-StocR 2509.10461），但主流仍是单目标；本轮门禁在 5 种二级标签上不稳定，也支持"标签不是越多越好"。

**落地**：新增 `factor_engine/ml/feature_profiles.py`（`full` / `compact` / `core` 三画像 + `include_features`/`exclude_features` 通配符），`[model_features] profile` 单行切换，审计结果（选中/丢弃清单、缺失的 core 项）写入 OOS 结果；`test/test_feature_profiles.py` 覆盖。**默认保持 `full`**，`compact`/`core` 只作为消融对照。

> **前置结论**：当前瓶颈不在模型架构，而在标签与特征准入。`forward_return_20` 的截面 Rank 结构上无法区分“稳步上涨”与“先冲后回”，因此任何架构调整的收益上限都被标签封死。文献与本仓库自有数据同时指向：先改标签，再改门禁，最后动架构。

---

## 1. 现状核查

### 1.1 目标画像的可计算定义

用户口径 → 可落地定义（全部为 `trade_date` 收盘后可得）：

| 口径 | 可计算定义 | 特征/标签归属 |
|---|---|---|
| 半年内低点启动 | `close / min(low, 120d) - 1` 落在 `(0, 0.30]` | 特征（连续变量，非硬过滤） |
| 涨幅有限 | `dist_from_252d_high < 0`、`price_pct_rank_250d` 处于中低分位 | 特征 |
| 资金活跃 | 换手/量比/资金流 z-score 相对自身历史抬升，而非绝对额大 | 特征（须中性化，见 3.4） |
| 吃到大波段 | 前向最大有利波动 `MFE` 大且最大不利波动 `MAE` 小 | **标签**（见 3.1） |
| 买点在起点 | 趋势扫描 `|t|` 最大窗口的起始位置 | **标签**（见 3.1） |

关键判断：前两项是特征，后两项是标签。当前实现把四项全压在特征侧，标签只有一个固定 20 日收益 Rank，这是核心错配。

### 1.2 当前标签无法表达该画像

`forward_return_20` 不含路径信息。先涨 30% 再回撤 25% 的股票与稳步涨 5% 的股票，20 日收益可能相同，截面 Rank 也相同。模型因此无法区分“能持有的启动”与“冲高回落的强势股”，只能学到“未来 20 日绝对涨幅在截面中靠前”这一件事。

同时，现有 Transformer Top 10% 画像（20 日波动率中位数 4.00% vs 全市场 2.92%）是上述标签的直接后果，而非模型偏好：在只看终点收益的目标下，高波动样本的期望终点收益更分散，头部更容易被高波动占据。

### 1.3 代码核查结果（两处与先前诊断不一致）

**（1）二次资金流特征被删的原因不是相关性筛选。**

[factor_engine/ml/model_training.py:1067](../../factor_engine/ml/model_training.py#L1067) 的 `_select_temporal_feature_pairs` 中不存在任何特征间相关性剪枝。打分公式为：

```python
score = correlation * coverage * np.log1p(variability.clip(lower=0.0))
```

即 `|corr(feature, label)| × 覆盖率 × log(1 + 标准差)`，排序后取 TopN。

[data/ingest/providers/cn_moneyflow.py:388-394](../../data/ingest/providers/cn_moneyflow.py#L388-L394) 中七个 `flow_second_wave_*` 字段全部带 `.where(source_count > 0)`，二次确认属稀有事件，覆盖率天然低，三项连乘被压至接近零。在覆盖率接近的前提下，真正的区分项是 `log(1 + std)`：

| 字段 | 类型 | 方差特性 | 当前结果 |
|---|---|---|---|
| `flow_second_wave_pullback_volume_ratio` | 连续比率 | 方差大 | **存活** |
| `flow_second_wave_flag` | 0/1 | `std ≤ 0.5` | 淘汰 |
| `flow_second_wave_source_count` | 小整数 | 方差小 | 淘汰 |
| `flow_second_wave_max_z5` | 稀疏连续 | 覆盖率低 | 淘汰 |
| `flow_second_wave_seed_age` | 稀疏整数 | 覆盖率低 | 淘汰 |
| `flow_second_wave_price_recovery_1d` | 稀疏连续 | 覆盖率低 | 淘汰 |

存活的恰好是方差最大的连续变量，与公式完全吻合。结论：**是覆盖率×方差惩罚淘汰了二元事件旗标，不是相关性筛选**。修复方式因此是白名单准入，而不是调整相关性阈值。

**（2）特征预算是 128 对，不是 500。**

`config/*.toml` 中 `transformer_max_feature_pairs = 128`（多个配置一致）。128 为 `_clean` 特征对数，加上 `_is_missing` 掩码后约 256 列。预留 8~12 个字段对的相对成本约为原估计的两倍，需在 128 预算内显式扣减。

**（3）已存在但未启用的钩子。**

`_fit_sequence_scaler(frame, features, *, preserve_binary_features=())`（model_training.py:1345）已预留二元特征保护参数，当前调用未传值。强制保留 flag 类字段后必须同时启用，否则大部分为 NA 的二元列经 MAD 标准化会被压成噪声。

**（4）`_resolve_embargo_days` 与路径标签冲突（新发现，影响 3.1）。**

`_resolve_embargo_days`（model_training.py:1122）通过正则 `_(\d+)d` 从标签列名推断 embargo 天数。路径标签持有期可变（5~60 日），列名无法编码单一天数。若不改，purge 长度会被低估，产生泄露。修复方式见 3.1 验收项。

### 1.4 未能验证的两项

| 项 | 核查方式 | 结果 |
|---|---|---|
| 资金流链路 T+1 门禁 | `grep available_at` 全仓 + 2026-09-20 人工审计 | `cn_moneyflow.py` 中**未命中**；`available_at` 仅出现在财务报表（`core/lightgbm_analysis.py:339`）与 alternative evidence 路径。**已关闭**：二次确认字段全部由当日及以前数据算出（`shift(1)`、`shift(2)`、`shift(4)`、后视 `rolling`），面板按同日键合并，故合法口径是"T 日盘后决策 + T+1 开盘执行"，已写入 `AVAILABILITY_RULE` 与 OOS manifest，并由截断不变性测试守门（见 0.2 与 5） |
| `forward_excess_return_*` 标签列 | `grep` 全仓 `.py` / `config/*.toml` | **未命中**（原状）。仓库原有 `forward_return_20/40/60`（`core/signals.py:60-62`、`core/factor_analysis.py:156-158`）。**已新建**：`strategy_labels.build_cn_strategy_labels` 现输出 `forward_exec_return_{5,10,20,60}d` 与 `forward_excess_return_{5,10,20,60}d`（全市场等权基准），见 0.2(4) 与 3.5 |

第二项影响 3.5 的工作量估计：多周期超额收益不是加两列，而是先要定义超额基准（全市场等权 / 行业等权），再落三个周期。

---

## 2. 文献依据

### 2.1 架构选择

| 结论 | 来源 | 对本项目的含义 |
|---|---|---|
| 中等样本（~10K）表格数据上树模型仍是 SOTA，原因是对无信息特征鲁棒、旋转不变、易学不规则目标函数 | [Grinsztajn et al., NeurIPS 2022](https://arxiv.org/html/2207.08815v1) | 与本次四折结果一致：LightGBM RankIC 0.1056 > Transformer 0.0726；主模型保留 GBDT |
| 树与神经网络同属最优组，但最佳深度约 3 层，更深退化，归因于收益数据信噪比低 | [Gu, Kelly & Xiu, RFS](https://dachxiu.chicagobooth.edu/download/ML.pdf) | 序列块应减层而非加深 |
| 调优后的普通 LSTM 在预测精度与买卖稳定性上稳定优于 Transformer | [Vanilla LSTMs Outperform Transformer-based Forecasting](https://arxiv.org/html/2601.00197v1) | 换架构前先试 GRU/LSTM 基线 |
| Transformer MSE 更低但 XGBoost 与集成日均收益更高（统计精度与经济收益分离） | [Pre-Training Transformers for Stock Return Prediction](https://arxiv.org/abs/2605.23962) | 见 2.5 关于本项目方向相反的说明 |

### 2.2 多专家与聚合

| 结论 | 来源 | 对本项目的含义 |
|---|---|---|
| 风格专家动态路由，CSI300 超额年化 24%，超前 SOTA 8 个百分点，CSI500/1000 同样占优 | [MIGA, arXiv 2410.02241](https://arxiv.org/html/2410.02241v1) | A 股基准，最相关；但为端到端重构，排在后期 |
| Bernstein 在线聚合多个 ML 预测，Sharpe 高于任何单模型且换手相当 | [Expert Aggregation for Financial Forecasting](https://arxiv.org/abs/2111.15365) | 成本最低的“多专家”收益来源，优先于 MoE |
| MoE + 多头注意力串联，专家逐级精炼，随风格漂移重加权 | [FactorMoE](https://link.springer.com/article/10.1007/s40747-026-02307-2) | “主模型 + 事件模型”的成熟形态，长期目标 |
| 共享表示 + 注意力的多任务优于单任务；多任务 + listwise 排序提升排序与收益指标 | [MTL for Financial Forecasting](https://arxiv.org/html/1809.10336v1)、[MiM-StocR](https://arxiv.org/html/2509.10461v2) | 支持 5/10/20 日多头方案 |
| 极端排序加权 listwise loss + 多样性正则 | [MAPLE](https://arxiv.org/html/2607.24131v1) | 替代 Huber 的候选，排在标签改造之后 |

### 2.3 标签方法

| 方法 | 来源 | 用途 |
|---|---|---|
| 三重障碍法：止盈/止损/时间三障碍，水平障碍按波动率缩放，标签为先触及者；无需真实开仓即可生成 | [López de Prado, AFML 第 3 章](https://www.oreilly.com/library/view/advances-in-financial/9781119482086/c04.xhtml) | 把“吃到波段”与“无大回撤”写进标签 |
| 趋势扫描：不用固定时间障碍，从候选窗口中选线性拟合 `|t|` 值最大者 | 同上 | 把“启动”写进标签 |
| 元标签化：主模型定方向，二级模型定是否执行与下注规模；二级标签来自主模型信号是否触及止盈障碍 | 同上；[Hudson & Thames / JFDS 代码库](https://github.com/hudson-and-thames/meta-labeling) | 报告 Sharpe 与最大回撤同时改善 |

### 2.4 “刚启动”特征

| 结论 | 来源 |
|---|---|
| 基于成交量的**早期阶段动量**策略在 37 国中 34 国击败传统动量 | [Trading Volume and Momentum: The International Evidence](https://www.researchgate.net/publication/284195178_Trading_Volume_and_Momentum_The_International_Evidence) |
| 52 周高点动量在 20 个市场中 18 个盈利、10 个显著 | [The 52-week high momentum strategy in international stock markets](http://isiarticles.com/bundles/Article/pre/pdf/19345.pdf) |
| 价格穿越历史区间上下边界时成交量显著放大，归因处置效应与锚定 | [Management Science](https://pubsonline.informs.org/doi/10.1287/mnsc.1080.0920) |

“早期阶段 + 资金活跃”这一核心命题在跨国样本上成立，是本方案证据最强的一条。

### 2.5 反向证据与不宣称

- **缩量筑底作为独立可检验信号，未找到同行评议证据。** VCP / 缩量企稳属 O'Neil–Minervini 实务传统。两个必须处理的陷阱：(a) 盘整期缩量基础率极高，任何横盘都机械压缩成交，不做“缩量后失败”对照组则判别力可能接近零；(b) 该形态以前期价格强度为条件，朴素回测会把动量暴露误记为量能信号的功劳。
- 有研究发现在控制非线性趋势、成交量、波动率与货币供应后，接近近期高点对价格的影响转为**负**（[Tandfonline](http://www.tandfonline.com/doi/abs/10.1080/14697680903220356)）；q 因子文献亦称 52 周高点异象在投资与盈利因子定价后消失。**位置类特征按条件变量使用，不作为独立 alpha 宣称。**
- 本次四折 OOS 不支持“Transformer 提供可直接叠加的增量”：其 RankIC、RankIC IR、Top 分位目标和多空目标均低于 LightGBM。Transformer 的换手较低（0.524 vs 0.660），可作为后续低权重辅助或序列形状研究对象，但在完成独立增量检验前不应进入固定权重生产排序。
- 上述文献的发表日期与部分 arXiv 编号未逐一核验，引用仅作方向依据，不作为定量预期。MIGA 的 24% 超额为其自身实验设定下的相对比较，不可直接迁移为实盘预期。

---

## 3. 工作项

### 3.1 W1 路径标签（三重障碍 + 趋势扫描）

**落点**：`factor_engine/ml/strategy_labels.py`（现 33 行，已有 `drawdown_60d`、`bottom_rebound_candidate` 等日线标签，是自然扩展位）。

**新增标签列**：

```text
label_tb_class          ∈ {-1, 0, 1}   先触止损 / 时间到期 / 先触止盈
label_tb_ret            触障时点收益
label_tb_hold_days      实际持有日数
label_ts_window         趋势扫描选中窗口
label_ts_tvalue         该窗口线性拟合 t 值
label_mfe_60d           前向最大有利波动
label_mae_60d           前向最大不利波动
label_mfe_mae_ratio     波段质量比
```

**参数**：`σ_t` 取 20 日收益 EWMA 或 `ATR20 / close`；止盈 `+k_pt × σ_t`、止损 `−k_sl × σ_t`，`k_pt / k_sl` 初值 `(3.0, 1.5)`；趋势扫描候选窗口 `{5, 10, 15, 20, 30, 40, 60}`。

**目标分工**：主回归目标用 `label_mfe_mae_ratio` 的截面 Rank（表达“吃到波段”）；`label_tb_class` 留给 3.2 的元标签二分类。

**必须同步修改**：`_resolve_embargo_days` 无法从路径标签列名推断天数。路径标签的 embargo 必须取候选窗口上界（60 日）而非正则结果，否则 purge 不足导致泄露。实现时为路径标签显式传入 `embargo_days=60`，并在 manifest 中记录。

### 3.2 W2 元标签门禁（已实现，已判定）

**结构（已落地）**：主模型输出候选与方向 → 二级模型输出 `act / don't-act` 概率 → 概率用于下注规模。二级标签来自主模型信号是否触及 `label_tb_class = 1`。门禁只在主模型已选中的子集上做取舍，不重排整体截面——这一点保持原设计不变。

**嵌套内层设计**（`generate_lightgbm_meta_oos_predictions`）：折内训练窗按 70/30 切分，内层门禁块与其前段之间留 20 日 purge；内层主模型给内层块打分 → 取候选 → 训练二级模型 → 测试块由全训练窗主模型打分 → 过门禁。四折内层块合计 39,155 个候选样本，测试块 12,470 行（40 个决策日）。

**端到端实测**（`--config config/cn_pipeline_p1_17_meta_learned.toml --stage oos_predictions`，测试块 2026-06-25..2026-08-19，4 折 × 10 个决策日，12,469 条候选；Top-20，双边佣金 5bps + 滑点 5bps + 卖出印花税 5bps = 25bps 往返；评估口径 `forward_excess_return_20d`，成本前 `mean_gross`／成本后 `mean_net` 并列）

学习型门禁在该次运行中同样不通过：门禁后净值 2.28% vs 无门禁 3.31%，放行 2.10% vs 拒绝 3.51%（t=−6.26），折内验证 AUC 依次 0.490 / 0.505 / 0.548 / 0.569，而内层候选的先触止盈基础率从 29.0% 降到 10.8%（越靠近测试期越稀有）。

为排除"单一标签定义"的运气，另行在缓存候选帧上对五种二级标签逐一复算（同口径、同超参）：

学习型门禁（LightGBM 二分类，29 个候选级特征），五种二级标签定义逐一实测，全部不通过：

| 二级标签 | 折内验证 AUC | 测试折迁移 AUC | 门禁后净值 | 无门禁净值 | 放行均值 | 拒绝均值 | t(放行−拒绝) |
|---|---:|---:|---:|---:|---:|---:|---:|
| `tb_class == 1`（三重障碍先触止盈） | 0.525 | 0.450 | 2.29% | 3.17% | 1.97% | 3.66% | −7.54 |
| `excess20 > 0` | 0.532 | 0.467 | 2.83% | 3.17% | 2.16% | 3.47% | −5.82 |
| `excess20 > 当日中位数` | 0.510 | 0.461 | 2.33% | 3.17% | 2.18% | 3.45% | −5.64 |
| `excess5 > 0` | 0.512 | 0.506 | 2.37% | 3.17% | 2.47% | 3.16% | −3.03 |
| `exec20` 前 1/3 | 0.545 | 0.457 | 2.59% | 3.17% | 2.20% | 3.44% | −5.51 |

降容量复核（内层按 AUC≥0.53/0.55 筛特征 + L2 逻辑回归，C=0.1）：迁移 AUC 在四折间为 0.35~0.66，符号在折间翻转；门禁后净值 2.63%~2.96%，仍低于无门禁的 3.17%。

**结论**：学习型门禁在 40 个 OOS 日上不迁移——折内 AUC 0.50~0.58 但测试折迁移 AUC 0.42~0.51，且净值在全部 5 种标签 × 4 折组合下都低于无门禁基线。原因是二级问题本身信号弱（候选集内先触止盈基础率仅 12.6%）、内层窗口只有约 5 个月，以及"触及 +20% 障碍"这一目标与 20 日截面超额收益口径不匹配（前者偏向高波动、后者惩罚高波动，与 1.2 的诊断同源）。**默认不启用 `gate_mode = "learned"`。**

**通过的是规则门禁**。在候选集内用显式阈值裁剪"离 120 日低点过远"的样本（`dist_from_120d_low <= 阈值`，该列同时是启动资格的定义变量之一，阈值不是拟合出来的权重）。

端到端运行（`--config config/cn_pipeline_p1_17_meta_rule.toml --stage oos_predictions`，同 4 折同 40 日）在阈值 0.15 上给出：**门禁后净值 4.13% vs 无门禁 3.31%（+0.83pp/20 日）**，放行均值 3.86% vs 拒绝均值 1.62%（t=9.87），放行比例约 53%（6,580/12,469）。

阈值敏感性（缓存候选帧上同一集合的复算，与端到端运行相互独立）：

| 阈值 | 放行比例 | 放行均值 | 拒绝均值 | t | 门禁后净值 | 无门禁净值 |
|---:|---:|---:|---:|---:|---:|---:|
| 0.10 | 29.1% | 4.82% | 1.99% | 11.57 | 3.41% | 3.17% |
| 0.12 | 38.6% | 4.34% | 1.85% | 10.84 | 4.12% | 3.17% |
| 0.15 | 52.7% | 3.80% | 1.72% | 9.24 | 3.91% | 3.17% |
| 0.18 | 65.0% | 3.59% | 1.38% | 9.30 | 4.06% | 3.17% |
| 0.20 | 72.8% | 3.38% | 1.29% | 8.17 | 4.10% | 3.17% |
| 0.25 | 88.6% | 3.05% | 1.02% | 5.58 | 3.46% | 3.17% |

0.12~0.20 一整段都给出 +0.74~+0.95pp/20 日的净增益（不是单点最优），端到端运行在同一段上落在 +0.83pp，因此配置默认取 `rule_threshold = 0.15`（放行约 53%，样本量足够）。分折看：折 1、折 2 强（放行 7.0%/3.5% vs 拒绝 −1.6%/0.6%），折 3、折 4 走平（2.6% vs 2.9%、1.9% vs 2.4%），**存在收益随折衰减的可能，不能按无衰减外推**。

**门禁前提的复算（重要负证据）**：原方案引用的"Transformer Top10% + 二次确认 = +1.43%"未能在本面板复现。候选集内 `flow_second_wave_flag = 1` 的 20 日超额均值为 +3.04%（n=874），未确认组 +2.80%（n=11,559），差 +0.24pp，且分折不稳定（折 3 为 −1.13pp）。`flow_second_wave_pullback_pct` 的分位表现反而是单调的（q1 0.98% → q4 4.92%），说明二次资金流字段里有条件信息，但载体是回撤深度而不是那个二元确认旗标。

**特征级迁移证据**（候选集内、测试折、四折同号）——这是后续做显式规则而非拟合门禁的依据：

| 特征 | 折 1 | 折 2 | 折 3 | 折 4 | 均值 |
|---|---:|---:|---:|---:|---:|
| `vol_atr_pct_14` | 0.516 | 0.584 | 0.576 | 0.576 | 0.563 |
| `dist_from_120d_low` | 0.534 | 0.538 | 0.544 | 0.531 | 0.537 |
| `pv_volume_ratio_20d` | 0.543 | 0.523 | 0.540 | 0.524 | 0.533 |
| `pv_return_20d` | 0.560 | 0.517 | 0.517 | 0.518 | 0.528 |
| `dist_from_60d_high` | 0.550 | 0.509 | 0.522 | 0.528 | 0.527 |
| `flow_second_wave_pullback_pct`（分位） | q4−q1 = +0.70pp | +4.98pp | +7.25pp | +0.22pp | 单调 |

（AUC 为单特征对"20 日超额 > 0"的排序能力。）

### 3.3 W3 特征筛选修复

1. `_select_temporal_feature_pairs` 增加 `protected_features` 参数，白名单绕过打分直接准入；配置项 `transformer_protected_features`。
2. 白名单上限 12 对，**从 128 预算内扣减**，避免无界膨胀。
3. 首批白名单：`flow_second_wave_flag`、`flow_second_wave_max_z5`、`flow_second_wave_source_count`、`flow_second_wave_seed_age`、`flow_second_wave_price_recovery_1d`、`flow_second_wave_pullback_pct`。
4. 同步为 flag / count 类列启用 `_fit_sequence_scaler(preserve_binary_features=...)`。
5. 审计字段：`protected_feature_count`、`protected_features_applied`、`protected_features_missing`（白名单字段不在面板中时必须显式报告，不可静默跳过）。
6. 测试：`test/test_panel_training.py` 已有 `max_feature_pairs=1` 用例，增加一例断言低方差 flag 在白名单下存活。

### 3.4 W4 资金流残差中性化

`factor_engine/ml/neutralization.py`（145 行）已有机制，复用而非新写；实现前需确认其函数签名与是否支持逐日截面回归。

保留原始字段，另增残差列：

```text
moneyflow_residual_after_size
moneyflow_residual_after_turnover
moneyflow_residual_after_volatility
moneyflow_residual_after_industry
```

回归自变量：市值对数、成交额对数、换手率、20/60 日涨幅、20 日波动率、行业哑变量。逐交易日截面拟合，训练折内拟合、验证/测试冻结。

### 3.5 W5 多周期多任务（标签侧已完成，多任务头待做）

**基准决策（已完成）**：全市场等权。理由：仓库没有 PIT 有效的历史行业映射（`industry_mapping_path` 要求 `available_at`，现有行业标签对历史 OOS 无效，见 1.4 与 `graph_temporal` 的同类约束），行业等权要等映射补齐后才能作为并列口径。

**已落地列**（`strategy_labels.build_cn_strategy_labels`，T+1 开盘入场）：

```text
forward_exec_return_{5,10,20,60}d      T+1 开盘买入、第 h 个交易日收盘计价
forward_excess_return_{5,10,20,60}d    同上，减去当日全市场等权基准
```

`excess_benchmark="none"` 关闭超额列；`exec_horizons=(5,)` 只保留部分周期。超额列的当日截面均值恒为 0（测试已断言），后续 RankIC 与门禁评估读到的都是同日内相对口径。

**仍待做**：把 5/10/20 日三个头接进 LightGBM/Transformer 训练目标（多任务或分开建模），在门禁形态确定后做消融。二次资金流"对 5~10 日更有效"的假设本轮只在候选集内做了 5 日与 20 日对照（见 3.2 特征级证据），完整多周期 RankIC 对照待多任务头落地。

### 3.6 W6 在线聚合

以 Bernstein 在线聚合替代 LightGBM / Transformer 的固定权重。**只在组合层聚合预测，不进模型内部**，保持可回滚。权重序列须落盘以便审计。

### 3.7 W7 架构调整（最后）

```text
主模型   LightGBM + 路径标签
         依据：Grinsztajn — 500 特征面板上对无信息特征鲁棒
序列块   浅层 GRU / TCN（2-3 层），只吃 20-30 个量价资金流序列特征
         依据：GKX 最佳约 3 层；“首波→缩量回撤→二次流入”本质是时序形状
门禁     元标签二分类器，输出下注规模
合成     在线聚合，非固定权重
```

“首波资金 → 缩量回撤 → 二次流入”是序列形状问题，GBDT 靠手工特征近似，序列模型可直接学——这是序列模型唯一不可替代之处，把它限定在此。

---

## 3.8 本轮 OOS 门禁结论

| 项目 | 结果 | 决策 |
|---|---:|---|
| LightGBM RankIC | 0.105583；IR 1.402858 | 作为当前主模型；先优化标签、资金流中性化和元标签门禁，不据此扩大仓位 |
| Transformer RankIC | 0.072555；IR 0.446122 | 暂作辅助研究，不进入固定权重生产主排序；先做 GRU/TCN 与元标签门禁对照 |
| OOS 覆盖 | 4 折、40 个测试日期、124,485 个共同样本 | 样本已足以否定当前 Transformer 优于 LightGBM，但仍不是成本后非重叠回测 |
| 保护特征 | 6 个 `flow_second_wave_*` 在 4 个 Transformer manifest 中均准入 | 保护字段准入生效；尚未证明字段自身的条件收益，必须做消融和条件分组 |

因此本计划的下一步顺序固定为：
1. ~~用 `label_tb_class` 构造主模型 TopK 内二分类元标签，输出执行概率/下注规模~~ → **已完成并判定：学习型门禁不通过，改为规则门禁**（见 3.2）；
2. 在训练折内加入资金流对市值、成交额、换手、波动和行业的截面残差（W4，尚未开始）；
3. 以 5/10/20 日超额收益多任务做消融（W5 标签已就绪，多任务头待做）；
4. 只有在上述门禁改善后，才比较 GRU/TCN 或在线专家聚合。

本轮把"门禁"从待验证变成已判定，并得到两条可执行结论：(a) 学习型门禁在本样本上不可用，任何把二级分类器写进生产排序的方案都要先复现出正迁移；(b) 规则门禁（收紧启动窗口上界）是唯一通过验收的形态，但分折衰减提示它应按"上限收紧 + 小仓位"使用，不能当成稳定 alpha。

## 4. “刚启动”特征清单

按证据强度排序。所有窗口只使用当日及以前数据。

```text
位置类（52 周高点 / 锚定文献支持）
  dist_from_180d_low          close/min(low,120d)-1，连续变量不做硬过滤
  dist_from_252d_high
  price_pct_rank_250d

早期动量类（早期阶段动量文献，证据最强）
  mom_20d_x_volume_change     动量与量变交互项，“早期”的核心定义
  turnover_accum_60d          前期换手累积，低者为早期
  mom_60d_residual            对市值/行业中性化后的动量

盘整/收缩类（实务概念，须建对照组，见 2.5）
  atr20_over_atr60            波动收缩比
  pullback_depth_shrink       连续回撤幅度递减（VCP 代理）
  days_since_60d_high
  consolidation_length

量能类
  vol_dryup = vol5/vol60      回撤期缩量
  vol_expansion = vol1/vol20  突破日放量
  turnover_zscore_60d

资金流残差（见 3.4，机制已存在）
  moneyflow_residual_after_{size,turnover,volatility,industry}
```

盘整/收缩类必须附带“缩量后失败”对照组统计，否则不得进入生产特征集。

---

## 5. PIT 与泄露规则

- 特征行主键 `market + stock_code + trade_date`，沿用 P1.16 约定。
- 资金流 T 日盘后才完整：**T 日资金流只能在 T 日收盘后的决策里使用，执行最早 T+1 开盘**。2026-09-20 审计已关闭该阻断项（见 1.4）：`cn_moneyflow.py` 的二次确认与滚动字段只用当日及以前数据，面板按同日键合并，因此采用"显式声明"收口——`oos_predictions.AVAILABILITY_RULE` 与每个 OOS manifest 记录 `decision_point = session close`、`execution_point = next session open`、`entry_delay_sessions = 1`。注意**不要**改成 `available_at = next_trade_date`：`compact_training_panel` 会把 `available_at > trade_date` 的行判为 PIT 无效并置 NaN，那等于把当日资金流整列丢掉，与"盘后决策"的真实时间线不符。
- 该口径由两个截断不变性测试守门（`test_cn_moneyflow.py::test_second_wave_confirmation_features_never_read_future_bars`、`::test_moneyflow_rolling_features_are_truncation_invariant`），以及 `test_meta_labeling.py` 对 OOS manifest `entry_delay_sessions == 1` 的断言。
- 路径标签的 embargo 取窗口上界 60 日，不使用列名正则推断（见 3.1）。
- winsorize、MAD、rank、残差回归系数、元标签阈值全部训练折内拟合，验证/测试/线上冻结同一 manifest。
- 缺失保留 `is_missing`，禁止填 0；“事件未发生”与“抓取失败”不可混淆。

---

## 6. 评估修正

当前 `model_comparison` 中 `active_return` 与 `max_drawdown` 为空，根因是 20 日标签按 5 日滚动预测、持有窗口重叠，无法直接累计净值。

下一轮必须补齐：

| 项 | 要求 |
|---|---|
| 多周期收益 | 5 / 10 / 20 日分别报告 |
| 非重叠持有窗口 | 或按重叠窗口每日分配 1/H 资金的标准修正法，二者择一并写明 |
| Top-K 组合收益 | K 与调仓频率显式记录 |
| 交易成本 | A 股口径：双边佣金、卖出印花税、滑点，参数写入配置而非硬编码 |
| 最大回撤 | 基于非重叠净值曲线 |
| 行业集中度 | Top-K 的行业 HHI |
| 换手后净收益 | 扣成本后的净值 |

---

## 7. 验收标准

| 工作项 | 验收 |
|---|---|
| W1 | 路径标签落盘且 embargo=60 写入 manifest；`label_mfe_mae_ratio` Rank 目标下 Top 10% 的 20 日波动率中位数相对现状下降，且 Top 10% 收益不低于现状 |
| W2 | **已判定**：规则门禁（`dist_from_120d_low <= 0.15`）Top-20 扣成本净值 3.91% vs 无门禁 3.17%，放行 3.80% vs 拒绝 1.72%（t=9.24）→ 通过；学习型门禁 5 种标签全部为负（1.97%~2.83% vs 3.17%）→ 不通过，默认关闭 |
| W3 | 审计字段显示 6 个 `flow_second_wave_*` 全部进入特征集，且经 `preserve_binary_features` 后 flag 列标准化前后非零方差保持 |
| W4 | 残差特征与市值/换手/波动/行业的相关性绝对值低于原始字段；Top 10% 的风格暴露下降 |
| W5 | **部分完成**：可执行/超额收益标签（5/10/20/60 日、全市场等权基准）已落盘并通过测试；二次资金流的候选集内条件收益已复算（flag 差 +0.24pp、分折不稳定），完整多周期 RankIC 对照待多任务头落地 |
| W6 | 聚合权重序列落盘；聚合净值 Sharpe 不低于单模型最优 |
| W7 | 仅在 W1~W6 完成且评估口径修正后启动 |

失败也是结论：W5 若显示二次资金流在三个周期上均无增益，应记录并考虑下线该特征族，而非继续调参。

---

## 8. 阻断项与待确认

| 项 | 类型 | 影响 |
|---|---|---|
| ~~资金流 `available_at` / T+1 门禁是否存在~~ | **已关闭（2026-09-20）** | 审计结论：二次确认字段只用当日及以前数据；口径固化为"T 日盘后决策 + T+1 开盘执行"，写入 `AVAILABILITY_RULE`、OOS manifest 与截断不变性测试 |
| ~~超额收益基准口径~~ | **已决策：全市场等权** | 行业等权待 PIT 行业映射补齐后作为并列口径 |
| `neutralization.py` 是否支持逐日截面回归 | 阻断 W4 | `neutralize_features` 已按 `trade_date` 逐日回归，但只支持 `industry` / `log_market_cap` 两类控制变量；W4 需扩展为通用控制列（成交额、换手、20/60 日涨幅、波动） |
| 学习型门禁的负结果是否由窗口过短导致 | 已记录，不阻断 | 内层窗口仅约 5 个月；若要再试，须先做到跨折符号一致再接入 |
| `k_pt / k_sl` 初值是否需按市场状态分域 | 不阻断 | W1 完成后做敏感性分析 |

---

## 9. 实施顺序

```text
0. ~~审计资金流 PIT~~                        ← 已关闭（2026-09-20）
1. ~~W1 路径标签：三重障碍 + 趋势扫描~~      ← 已落地
2. ~~W2 元标签门禁~~                        ← 已判定：学习型不通过、规则型通过
3. ~~W3 特征筛选修复 + preserve_binary_features~~ ← 已落地
4. W4 资金流残差中性化（需先扩 `neutralize_features` 为通用控制列）  ← 下一步
5. W5 5/10/20 日多任务头（标签已就绪）
6. W6 在线聚合替代固定权重
7. W7 架构浅层化与职责拆分                  ← 最后
```

与先前排序的差异：架构与特征后移，标签前移。依据是文献（标签决定模型学什么）与本项目自有数据（门禁结构已验证有效）两方面，而非单纯谨慎。

**当前 Transformer 继续保留，但在 W1~W6 完成前不提高其组合权重。**

---

## 10. 回滚

按 `artifacts/` 既有约定，本次工作项产物在 `artifacts/path_labels_meta_labeling/`：`BASELINE/`、`MODIFIED/`、`DIFF_FILE.diff`、`ROLLBACK.sh`、`VERIFICATION.txt`、`baseline_tests.txt`、`modified_tests.txt`、`rollback_test.txt`、`modified_hashes.txt`。回滚方式：

```sh
./artifacts/path_labels_meta_labeling/ROLLBACK.sh <target-root>   # 恢复基线文件并删除本次新增文件
```

配置侧回滚不需要脚本：`[meta_labeling] enabled = false` 回到"无门禁"生产路径；`meta_gate_mode` 在 `learned` / `rule` 间单行切换；`label_mode` 改回 `forward_return` 即回到旧标签，旧标签列与旧 manifest 均未被覆盖。
