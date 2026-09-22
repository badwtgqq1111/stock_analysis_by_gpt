# P1.17 路径标签、元标签门禁与“刚启动”特征落地方案

> 状态：W1/W3 已落地；W2 元标签门禁**两种形态都不通过**——学习型在 4 折（40 日）上不迁移，规则型在 4 折上通过、但扩样本到 13 折（169 个决策日）后同样失败（门禁后 0.38% vs 无门禁 1.48%，放行 0.92% vs 拒绝 1.65%，t=−4.68），因此 `[meta_labeling] enabled = false`，不接入生产。干净面板已从 242 个交易日扩到 376 个（2025-03-10 起），OOS 从 4 折扩到 13 折。W5 的可执行收益与超额收益标签已落地（基准 = 全市场等权）。W4/W6/W7 待验证。阻断项 1（资金流 PIT 审计）已关闭。路径标签默认不切换生产目标，需在配置中显式启用。
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

**成本实测**（同一 4 折 OOS、同一 gate 形态，本机；下表的收益列为 4 折/40 日样本，已被 0.4 的 13 折/169 日结果取代，仅用于比较画像之间的差距）：

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

### 0.4 扩样本复检：W2 判定反转（2026-09-20）

**第一步：重建干净面板。** 由于因子库当前的 `feature_config_hash`（`236efa35ecb499f6`）已覆盖 2025-03-20 起的数据，扩窗**不需要重跑 `--stage features`**，只需把 `[clean_panel] days` 从 365 提到 560：

```sh
uv run python scripts/run_cn_pipeline.py --config config/cn_pipeline_p1_17_startup.toml --stage clean_panel
```

实测：**287.7s**（约 4.8 分钟），输出 `rows=1,981,870`（原 1,280,726）、`feature_count=683`、`primary_key_unique=true`、覆盖 **2025-03-10 → 2026-09-18 共 376 个交易日**（原 242）。耗时集中在按 50 只股票分批读因子长表（每批约 950 万行、2.8s），与特征数无关。

**第二步：13 折 OOS 复检门禁。** `config/cn_pipeline_p1_17_extended.toml`（`n_splits=13`、`min_train_days=180`、`purge_days=60`、LightGBM 单腿、全量画像），13 折 × 13 日 = **169 个决策日、41,964 条候选**，用时约 20 分钟。

| 口径 | 40 日（原） | 169 日（扩样本后） |
|---|---:|---:|
| 主模型 RankIC（路径标签） | +0.1056（IR 1.01） | **+0.0511**（IR 0.50） |
| 主模型 RankIC（20 日超额） | +0.1437（IR 1.52） | **+0.0688**（IR 0.67） |
| 无门禁 Top-20 扣成本 | +3.39% | **+1.48%** |
| 规则门禁 Top-20 扣成本 | +4.13% | **+0.38%** |
| 放行 vs 拒绝 | 3.86% vs 1.62%（t=9.87） | 0.92% vs 1.65%（**t=−4.68**） |
| 门禁−无门禁（逐日配对） | +0.83pp（t=1.60） | **−1.10pp（t=−3.75）** |

分折看：门禁 Top-20 优于无门禁的折数为 **8/13**，但"放行均值 > 拒绝均值"的折数只有 **3/13**；折 4、折 8 的损失最大（0.144→−0.006、0.186→−0.054）。也就是说，40 日样本上的正结论来自**特定时段**（2026-06~08 那段恰好对"离低点近"有利），不是稳定结构。

**结论与动作**：
1. `[meta_labeling] enabled = false`（生产路径回到"无门禁"）；`gate_mode` 保留 `learned`/`rule` 两条实现以便将来复检，但都不得默认开启。
2. 门禁这条线**到此关闭**，除非出现"跨折符号一致"的新形态（例如按 regime 分域、或多折同号 IC 稳定性筛选后的版本）再重开。
3. 主模型在 169 日上的 RankIC +0.069（IR 0.67）才是后续一切改动的比较基准；**不要再用 40 日样本上 +0.144 的数字**做判断。
4. 后续优先级回到 W4（残差中性化）与 W5（多周期/多任务头），W6/W7 仍然压后。

> **前置结论**（2026-09-20 修订）：原判断「瓶颈在标签与特征准入，先改标签」已被 §0.5 的 A1 对照否证——路径标签作为训练目标在 13 折上全面劣于旧的 `forward_return_20`。当前瓶颈仍不在模型架构，但也不在「把收益换成路径」这一层；后续应从特征准入、评估口径与组合层找增益。原文保留如下：`forward_return_20` 的截面 Rank 结构上无法区分“稳步上涨”与“先冲后回”，因此任何架构调整的收益上限都被标签封死。文献与本仓库自有数据同时指向：先改标签，再改门禁，最后动架构。

---

### 0.5 A1 标签对照结论（2026-09-20，13 折 / 169 个决策日）

同一骨架（13 折、全量画像、startup 资格、无门禁、无残差特征），只换训练标签：

| 指标 | 路径标签 `label_path_score_20d` | 旧标签 `forward_return_20` | 谁更好 |
|---|---:|---:|---|
| Top 10% 的 20 日波动中位数 | 0.3516 | **0.3349** | 旧标签（波动更低） |
| Top 10% 的 ATR% 中位数 | 0.0412 | **0.0391** | 旧标签 |
| Top 10% 已实现 20 日超额 | 1.11% | **1.41%** | 旧标签 |
| RankIC（路径标签口径） | 0.0511（IR 0.50） | **0.0681（IR 0.62）** | 旧标签 |
| RankIC（20 日超额口径） | 0.0688（IR 0.67） | **0.1042（IR 0.89）** | 旧标签 |
| Top-20 扣成本（169 日） | +1.59%（t=4.00） | **+2.22%（t=5.26）** | 旧标签 |

**结论**：把训练目标从"未来 20 日截面收益排名"换成"路径质量"（MFE−|MAE|）在扩样本上**全面变差**——不仅收益更低、波动更高，连对经济目标（20 日超额）的排序能力也下降。这直接否证了本方案开头"先改标签"的核心假设（§2 前置结论）：**标签改造本身没有带来模型增益**。

**保留的部分**：路径标签工作产出的**评估口径**（T+1 开盘入场的可执行收益、全市场等权超额收益、三重障碍结果、逐日配对与扣成本框架）是本轮所有判定的基础，继续使用；`forward_excess_return_*` 也已成为门禁/画像/标签对照的统一裁判口径。

**新基线**（后续一切 A/B 的比较基准）：`forward_return_20` + 全量画像 + startup 资格 + 13 折 / 169 个决策日 → RankIC(20 日超额) **0.1042（IR 0.89）**、Top-20 扣成本 **+2.22%（t=5.26）**、Top 10% 波动中位数 0.3349。

### 0.6 W4 判定与"同一面板上重做目标/选择/中性化"的总结论（2026-09-20）

三次 13 折 A/B（169 个决策日，基线 = `forward_return_20` + 全量画像 + startup 资格 + 无门禁）：additive 1.75%、replace 2.26%、基线 2.22%

| 改造 | 特征级效果 | 模型级效果 | 判定 |
|---|---|---|---|
| W1 换标签（路径标签） | — | RankIC(超额) 0.0688 vs 0.1042，Top-20 1.59% vs 2.22%，Top10% 波动 0.3516 vs 0.3349 | **否** |
| W2 元标签门禁（learned / rule） | — | 门禁后 0.38% vs 无门禁 1.48%（learned 迁移 AUC 0.42~0.51） | **否** |
| W4 残差中性化（additive / replace） | 控制暴露 0.113 → 0.014（-87%） | Top10% 波动 0.3351/0.3355 vs 0.3349（不降）；RankIC 0.1029/0.1036 vs 0.1042；Top-20 2.26%/1.75% vs 2.22% | **否（模型级）** |

**总结论**：在同一份面板上重做"目标函数（W1）、候选门禁（W2）、特征中性化（W4）"三类改造，都没有跑赢"原标签 + 全量特征"的朴素基线。收益的边际来源不在这三处，而在：

1. **面板之外的新信息**（更长历史、更多数据源、更细粒度资金流/Level-2）；
2. **组合与执行层**（权重约束、风格中性化到组合层面、调仓频率与成本）；
3. 评估口径本身（当前 Top-20 扣成本 2.22%/20 日 ≈ 年化 28% 是"重叠窗口、无容量约束"的理想值，尚未经过非重叠净值与容量检验）。

**下一步应据此调整**：W5 的多周期/多任务头仍值得做（它改的是"预测什么"，且标签已就绪），但 W6/W7 应继续压后，先做 §11.B2（评估口径）与组合层约束。

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

### 3.2 W2 元标签门禁（已实现；结论：两种形态都不通过，见 0.4）

> 本节下文记录的「规则门禁通过」是 **40 日样本**上的历史结果，扩样本后已被推翻（见 0.4），保留原文用于追溯判定过程。

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

**13 折复检（2026-09-20，见 0.4）**：扩样本后规则门禁同样失败（门禁后 0.38% vs 无门禁 1.48%，放行 0.92% vs 拒绝 1.65%，t=−4.68，逐日配对 −1.10pp）。因此本节此前"规则门禁通过"的结论**只成立于 40 日样本**，已被推翻；生产配置已改为 `enabled = false`。

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

`factor_engine/ml/neutralization.py` 已有机制，已于 2026-09-20 扩展为通用形态：`control_columns` 传入额外数值控制列、`residual_suffix` 保留原列并新增残差列、`compute_correlation_audit` 输出原始与残差对控制变量的截面相关性对照。配置在 `[neutralization]`（默认 `enabled = true`；24 个特征列、5 个控制列：市值对数、成交额、换手、20 日涨幅、20 日波动）。**仍未完成**：验收测量与 13 折 A/B（见 11.B1）。

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
1. ~~用 `label_tb_class` 构造主模型 TopK 内二分类元标签，输出执行概率/下注规模~~ → **已完成并判定：学习型与规则型都不通过**（扩样本后规则型也失效，见 0.4）；
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

## 6. 评估修正（2026-09-20 已落地）

原问题：`model_comparison` 中 `active_return` / `max_drawdown` 为空——20 日标签按日滚动预测、持有窗口重叠，直接复利不是组合净值。

**已实现**（`factor_engine/ml/walk_forward.py`）：

| 项 | 实现 |
|---|---|
| 重叠修正 | `overlap_correction = "split_1_over_h"`：每个决策日投入 1/H 资金，当日期收益 = 该 tranche 的 H 日收益 ÷ H，并对该 tranche 的换手收费；窗口短于一年时不做年化外推（`annualized = cumulative`） |
| 非重叠情形 | 自动识别（`_forward_windows_overlap`），走普通复利 |
| 交易成本 | 往返 = 2×(佣金+滑点) + 卖出印花税，默认 5/5/5 bps = **25bps 往返**，全部走配置 |
| Top-K 与调仓频率 | `top_k` 显式指定，行内记录 `book_definition`、`horizon_days`、`turnover_mean` |
| 净值与回撤 | 逐折累计 + **跨折链式累计**（`chained_cumulative_return`）与逐折/最差折回撤 |
| 行业集中度 | `industry_hhi`（Top-K 按行业份额平方和）。**坑**：注册表 `industry_l1` 存的是分类法名称（"证监会行业分类"），真实行业在 `industry_l2`；用错列会得到 HHI≈1 的假象 |
| 多周期 | 同一批预测对 5/10/20/60 日标签分别评估（`target_col` 切换），horizon 从列名解析 |

**169 个决策日、13 折、Top-20、25bps 往返、1/H 修正后的结果**（`output/evaluations/cn_model_comparison_p1_17_b2*.md`）：

| 标签 | RankIC 5d | 10d | 20d | 60d | 链式净收益 5d | 10d | 20d | 60d |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `forward_return_20`（生产基线） | 0.0649 | 0.0849 | **0.1042** | 0.0954 | 12.0% | 14.6% | **20.3%** | 10.7% |
| 路径标签 | 0.0392 | 0.0520 | 0.0688 | 0.0595 | 13.8% | 19.2% | 14.1% | 8.4% |
| W4 残差替换 | 0.0627 | 0.0832 | 0.1029 | 0.0939 | **20.1%** | 19.2% | 20.7% | 9.8% |

（同期基线的最差折回撤 −2.0%（20d）/−4.6%（5d），换手 0.69~0.72，行业 HHI ≈ 0.10，即 Top-20 分布在约 10~20 个行业，未见集中。）

两个可执行的观察：
1. **模型在 20 日上确实最强**（RankIC 0.104 vs 5d 0.065 / 60d 0.095），说明现在的 20 日训练目标是匹配的；若要打短周期，需要单独训练而不是复用 20 日分数。
2. W4 残差版在 **5 日**上明显更好（20.1% vs 12.0%，但 RankIC 反而略低 0.0627 vs 0.0649）——差异来自持仓尾部而非排序；这只是单样本提示，尚不能作为结论。

## 7. 验收标准

| 工作项 | 验收 |
|---|---|
| W1 | **不通过（扩样本后）**：13 折 / 169 个决策日上，路径标签训练的模型 Top 10% 波动中位数 **0.3516 vs 0.3349**（更高）、已实现 20 日超额 **1.11% vs 1.41%**（更低）；RankIC(20 日超额) **0.0688 vs 0.1042**（IR 0.67 vs 0.89），Top-20 扣成本 **1.59% vs 2.22%**（t=4.00 vs 5.26）。→ 路径标签**不作为生产训练目标**，生产沿用 `forward_return_20`；路径标签产出的评估口径（可执行/超额收益、三重障碍）保留使用 |
| W2 | **不通过（两种形态）**：学习型在 4 折/40 日上不迁移；规则型在 40 日上看似通过（+0.83pp），扩到 13 折/169 日后失败（−1.10pp，放行 0.92% vs 拒绝 1.65%，t=−4.68）→ 生产默认关闭门禁 |
| W3 | 审计字段显示 6 个 `flow_second_wave_*` 全部进入特征集，且经 `preserve_binary_features` 后 flag 列标准化前后非零方差保持 |
| W4 | **不通过（模型级）**：特征级暴露达标——控制变量平均 |corr| **0.1129 → 0.0141**（替换模式下 0.113→0.014）；但模型级不达标：Top 10% 波动中位数 0.3351（replace）/0.3355（additive） vs 基线 0.3349（**没有下降**），RankIC(超额) 0.1029/0.1036 vs 0.1042，Top-20 扣成本 2.26%/1.75% vs 2.22%。结论：残差化能去掉特征里的风格成分，但组合的风格暴露由其余 680 个因子重新形成——要降暴露必须在**组合层**约束，不是在特征层 |
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
| ~~`neutralization.py` 是否支持逐日截面回归~~ | **已解决（2026-09-20）** | `neutralize_features` 已扩展：`control_columns`（通用数值控制列）、`residual_suffix`（保留原列、另存 `<列>_resid`）、`compute_correlation_audit`（对照原始与残差对控制变量的截面相关性）。剩余：面板无行业列、审计需向量化、残差开/关的 13 折 A/B 未跑 |
| 学习型门禁的负结果是否由窗口过短导致 | 已记录，不阻断 | 内层窗口仅约 5 个月；若要再试，须先做到跨折符号一致再接入 |
| `k_pt / k_sl` 初值是否需按市场状态分域 | 不阻断 | W1 完成后做敏感性分析 |

---

## 9. 实施顺序

```text
0. ~~审计资金流 PIT~~                        ← 已关闭（2026-09-20）
1. ~~W1 路径标签：三重障碍 + 趋势扫描~~      ← 已落地；但作为**生产训练目标已被否**（§0.5），仅保留评估口径
2. ~~W2 元标签门禁~~                        ← 已判定：两种形态都不通过（扩样本后推翻）
3. ~~W3 特征筛选修复 + preserve_binary_features~~ ← 已落地
4. ~~W4 资金流残差中性化~~（已判定：模型级不通过，见 §0.6）（需先扩 `neutralize_features` 为通用控制列）  ← 下一步
5. W5 5/10/20 日多任务头（标签与评估口径均已就绪；B2 显示需单独训练才能打短周期）
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

本轮新增产物：`config/cn_pipeline_p1_17_extended.toml`（13 折复检配置）、`evidence/cn_meta_gate_extended.{md,json}`、`evidence/extended_gate_by_fold.csv`、`evidence/profile_compare.json`、`clean_panel_rebuild.log`、`clean_panel_rebuild_before.txt`、`modified_oos_extended.log`。

配置侧回滚不需要脚本：`[meta_labeling] enabled = false`（当前值）回到"无门禁"生产路径；`meta_gate_mode` 在 `learned` / `rule` 间单行切换；`label_mode` 改回 `forward_return` 即回到旧标签，旧标签列与旧 manifest 均未被覆盖。

---

### 0.7 资金流族开关与"选中形态"对照（2026-09-20）

**开关**：`factor_engine/ml/feature_profiles.py` 新增命名族 `FEATURE_FAMILIES`，配置里一行即可整族开关：

```toml
[model_features]
exclude_families = ["moneyflow"]   # = moneyflow_*（57）+ flow_second_wave_*（7），共 64 个基础因子 / 约 128 个模型列
```

已提供两份只差这一行的配置：`config/cn_pipeline_p1_17_mf_on.toml`（683 因子）与 `config/cn_pipeline_p1_17_mf_off.toml`（619 因子），其余（`forward_return_20` 标签、13 折、LightGBM、无门禁、无残差）完全相同。

**形态画像**（`scripts/compare_selection_profiles.py`，Top-20/日）：输出收益（RankIC、1/H 扣成本链式净值、回撤、换手）**以及选股形态**——60 日涨幅中位数、离 120 日低点距离、52 周高点位置、量比、缩量天数、ATR%、以及"已涨 50%/100%""离低点已翻倍""缩量占比"的比例。

**在含资金流的现有 13 折 OOS 上先量了一次**（169 个决策日；证据 `output/evaluations/smoke_profile*.csv`）：

| 指标 | Top-20 中位/占比 |
|---|---:|
| 60 日涨幅中位数 | **−11.0%**（近 35 个决策日：−21.0%） |
| 离 120 日低点距离中位数 | 0.129（近期 0.110） |
| 20 日量比中位数 | 0.924（<1 = 偏缩量） |
| 缩量占比（量比 < 0.8） | 34.2% |
| 已涨 50% / 已涨 100% 占比 | 0.0% / 0.0% |
| 离低点已翻倍占比 | 0.0% |

也就是说：**模型侧（含资金流）在 OOS 上调出来的就是"低位、缩量、未大涨"的名单**，与"选中已经翻倍的强势股"这一现象不符。若生产端确实看到翻倍股，来源更可能是：

1. 选择层的 sleeve/override（`[selection.signals]` 的 donchian / limit_momentum / range_breakout 会把放量突破与涨停动量名**以 `signal_override` 强制**进候选，并有 `forced_min_weight`）；配置注释里也记着 2026-09-15 出现过"模型 Top-4 被 sleeve 挤出"；
2. 生产用的是另一个模型产物（`[model_scores]` 指向 `output/models/cn/lightgbm/alpha_zoo_hk/model.txt`，与 p1_17 的 OOS 配置不是同一套）；
3. 只是最近 1~2 个决策日的个例。

`mf_on` / `mf_off` 两组跑完后用同一脚本对照即可分辨：如果两组形态指标接近，说明资金流族不是"选到翻倍股"的原因；如果 `mf_off` 明显更缩量/更低位，就按 `mf_off` 出图。

### 0.8 资金流开关 A/B 与"生产选到翻倍股"的根因（2026-09-20）

**A/B 结果**（两组配置只差 `exclude_families = ["moneyflow"]`；13 折、169 个决策日、Top-20、1/H 扣成本 25bps）：

| 指标 | mf_on（683 因子） | mf_off（619 因子） |
|---|---:|---:|
| RankIC(20 日超额) | **0.1042**（IR 0.888） | 0.0963（IR 0.812） |
| 链式净收益 | **20.51%** | 11.85% |
| 最大回撤 | −3.36% | −2.92% |
| 换手 | 0.589 | 0.588 |
| 60 日涨幅中位数 | −10.99% | −11.31% |
| 离 120 日低点距离中位数 | 0.1287 | 0.1276 |
| 20 日量比中位数 / 缩量占比 | 0.924 / 34.2% | 0.924 / 33.6% |
| 已涨 50% / 已涨 100% / 离低点翻倍占比 | 0% / 0% / 0% | 0% / 0% / 0% |

**结论一**：关掉资金流族会让模型明显变差（链式净收益 20.51% → 11.85%，−8.7pp；RankIC 0.104 → 0.096），
但**选股形态几乎不变**（两组都是"低位、偏缩量、60 日中位数 −11%"）。所以资金流不是"选到强势股"的原因，
不建议为了形态而关掉它。

**结论二（根因）**：生产选到的"已经翻倍"的股票来自**生产链路本身，与本次特征开关无关**。以 2026-09-18 的实际选股（12 只）为例：

- 只有 **50%** 满足启动资格（`startup_price_eligible`）；`dist_from_120d_low` 中位数 **0.275**（OOS 模型组合只有 0.129），
  60 日涨幅中位数 **+6.1%**（OOS 是 −11%）；最极端的 `301046.SZ` 离半年低点 **+113%**、`300592.SZ` **+93%**、`300153.SZ` **+56%**。
- 通道构成：`model` 4 只、`signal_override` 3 只、`signal_candidate` 5 只；**model 通道的 4 只里有 3 只不满足启动资格**
  （+113% / +93% / +47%），signal 通道再补进 +56% 等。
- 原因有两条，都可修：
  1. `[model_scores]` 指向的是 **`output/models/cn/lightgbm/alpha_zoo_hk/`**（manifest：label `forward_return_20d`、
     1267 特征、训练区间 2025-09-19~2026-04-27），**不是** P1.17 的启动门控模型（`alpha_zoo_hk_p1_17_startup`）；
  2. 打分阶段（`score_clean_feature_panel_models`）对**全市场**逐行打分，没有启动资格门（`startup_price_eligible` 只在训练侧生效），
     所以训练在"启动子集"上的模型（或全市场模型）都会给"已翻倍"的股票高分，选股层照单全收。

**建议（按性价比排序）**：

1. **在选股层加资格门**（最直接）：候选必须满足 `startup_price_eligible`（= `dist_from_120d_low ∈ [0.05, 0.30]`
   且 `return_60d ≤ 0.35` 且 `dist_from_60d_high ≤ −0.05`），或退一步只用 `dist_from_120d_low ≤ 0.30`；同时给
   `[selection.signals]` 的 `max_breakout_extension / max_runup_5d` 也作用到 model 通道（目前只约束 sleeve）。
2. **换生产模型产物**：把 `[model_scores]` 指向启动门控训练出来的模型（用 `forward_return_20` 标签、`startup_only = true`，
   不要用路径标签——§0.5 已证明路径标签更差）。
3. 资金流族保持开启（本次 A/B 的结论）；若仍要试 `mf_off`，应按"形态 + 收益"两张表一起看，而不是只看形态。

### 0.9 案例：2026-09-11 选股在未来一周的盈亏（用重放口径复现）

生产没有留存 2026-09-11 的选股文件，因此用 `--trade-date` 重放复现（先用 `--config config/cn_pipeline_replay_20260911.toml --stage model_scores` 生成 09-11 截面的模型分，再跑 `preselection` + `pk`）：
输出 `output/results_cn/replay_20260911/cn_ensemble_selected.csv`（12 只：model 5 / signal_override 3 / signal_candidate 4）。

持有窗口：**09-14 开盘买入 → 09-18 收盘**（T+1 入场、5 个交易日），扣 25bps 往返：

| 口径 | 净收益 | vs 全市场等权 |
|---|---:|---:|
| 组合（目标权重，6 只有仓位） | **+1.71%** | −1.05pp |
| 仅 `model` 通道（5 只等权） | **+3.40%** | **+0.64pp** |
| 全部 12 只等权（含 0 权重 sleeve 候选） | −1.20% | −3.95pp |
| 全市场等权（5,203 只，同窗口） | +2.76% | — |

个股：`300592.SZ` **+13.5%**（盘中最高 +21.7%，就是 §0.8 里"离低点 +93%"的那只）；`003030.SZ` +5.8%；`301127.SZ` +2.7%；拖累是权重最大的两只 `603117.SH` −3.5% / `603176.SH` −1.6%（合计 44% 权重）；`300913.SZ`（reversal override，17.9% 权重）−1.2%。零权重的 4 只 sleeve 候选平均 −4.4%（未计成本也仍是负）。

日路径（相对 09-14 开盘）：09-14 +2.78%（大盘 +0.98%）→ 09-15 +0.51% → 09-18 +1.96%，而大盘同期走到 +2.76%。

**读法**：

1. 一周维度上 `model` 通道小幅跑赢大盘（+0.64pp），但**按目标权重（含 override 与低分高权重）反而跑输 1.05pp**——问题更可能在**权重分配**，而不是"有没有选到高位股"；
2. 本次表现最好的恰好是"离低点 +93%"的 `300592.SZ`（+13.5%），说明"高位=必输"不成立，**不能用单周单个案例反推特征取舍**；
3. 单周样本太短。要做统计判断，应把同一流程对多个决策日重放（例如 2026-06~09 的每个周五），把"未来一周/两周"的净收益与超额累积成分布（§11 新增 D6）。

### 0.10 三项推进的结果（2026-09-21）：权重链路审计、启动资格门、15 周统计

**(1) 15 个决策日的"未来一周"统计**（每 5 个交易日一次，2026-06-03 ~ 2026-09-10；同一配置只差 `[selection.startup_gate].enabled`；T+1 开盘买入、5 个交易日收盘卖出、扣 25bps）：

| 口径 | gate OFF（现网） | gate ON（启动资格门） |
|---|---:|---:|
| 平均 1 周净收益 | **+1.65%** | −0.54% |
| 中位数 | +0.56% | +0.42% |
| t 值 | +0.68 | −0.28 |
| 正收益周数 | 8/15 | 9/15 |
| 平均 1 周超额（vs 全市场等权） | **+2.16%** | −0.03% |
| 平均持仓只数 | 9.5 | 11.6 |
| 同日配对（ON−OFF） | — | **−2.19%**（ON 赢 5/15） |

明细见 `evidence/survey_forward_weeks_parsed.csv`（30 行）。**读法**：

- 两者差异**不显著**（t 都 < 1），15 周样本很小；点估计反而偏向**不加门**。
- 门控把"极端周"拉平了：2026-06-03 由 −14.0% 变 +11.3%、2026-08-13 由 −3.4% 变 +1.4%；但同时切掉了大赢家：06-10（+24.1%）、06-17（+9.9%）、06-25（+12.9%）、08-27（+6.9%）。
- 结论：**门控是"降低高位股暴露"的工具，不是收益增强工具**。要收益就别开；要控风险/看得懂标的（不买已经翻倍的）就开——这是偏好选择，不是 alpha 选择。

**(2) 权重链路审计**（2026-09-11 重放，`cn_ensemble_portfolio_manifest.json` + explanations）：

- `weighting = "inverse_volatility"`，权重只与 20 日波动有关，**与排名无关**：`603176.SH`（rank 8、vol 0.326）拿 13.3%，`003030.SZ`（rank 2、vol 0.527）只拿 8.4%；权重/逆波动比在各只之间基本恒定。
- `signal_override` 有**保底权重**：`300913.SZ`（rank 4628、ensemble 分 5.3）通过 `forced_min_weight = 0.07` 拿到 **9.5%** 权重，而它是那一周最差的几只之一。
- 总仓位 53.4% 不是来自 35% 的基础设定，而是 `vol_target_mode = "budget"` 把"未用到的波动预算"放大 1.53× 后顶到 regime 上限的结果（`risk_control.scale = 1.5254`）。
- 可执行改进：如需让持仓反映模型排序，把 `weighting` 换成与分数/排名挂钩的方案（或对 override 的保底权重设上限）。

**(3) 启动资格门已落地**（`[selection.startup_gate]`，`factor_engine/ml/strategy_labels.py::apply_startup_gate` + `service._cn_startup_gate_frame`）：

```toml
[selection.startup_gate]
enabled = false            # 需显式开启
mode = "eligibility"       # eligibility | thresholds | both
max_dist_from_120d_low = 0.30
max_return_60d = 0.35
max_dist_from_60d_high = -0.05
```

关键实现细节：门必须**在取 Top-N 之前**施加（先定义可投 universe，再选前 N 名）；第一版放在截断之后，导致"候选被筛空"报错。开启后 2026-09-11 的选择完全换了一批（`688799.SH` / `301336.SZ` / `301001.SZ` / `603886.SH` / `600605.SH` …），当周净收益 **+4.84% vs 无门 +1.71%**（其中 `688799.SH` 单周 +22.2%）。

**佐证"生产为什么选到翻倍股"**：2026-09-11 全市场模型打分前 12 名里，10 只离半年低点 **+77%~+147%**（`003018.SZ` +147%、`300604.SZ` +125%、`688037.SH` +103%…），而全市场当日有 **2,961/5,207（56.9%）** 是合格的启动标的。也就是说**模型在全市场的头部本来就集中在高位股**，"启动"偏好只存在于启动子集内部（§0.8/§0.9 的两个现象由此统一）。

### 0.11 "低位刚启动是否更安全"的量化答案（2026-09-21）

同一批 15 个决策日（2026-06-03 ~ 2026-09-10）、T+1 开盘买入、5 个交易日、扣 25bps：

| 指标 | gate OFF（现网） | gate ON（启动资格门） |
|---|---:|---:|
| 平均 1 周净收益 | +1.65% | −0.54% |
| 中位数 | +0.56% | +0.42% |
| **周度波动（std）** | 9.39% | **7.42%** |
| **超额波动（vs 全市场等权）** | 8.11% | **5.46%** |
| 最差周 | −13.98% | −14.31% |
| **最差超额** | −10.30% | **−7.47%** |
| 亏损周占比 | 46.7% | 40.0% |
| 跌超 5% 的周占比 | 20.0% | 20.0% |
| 最好周 | +24.09% | +12.61% |
| 周度 Sharpe | +0.176 | −0.073 |

门控后组合的**形态**（15 个决策日，磁盘上的 gate=ON 选股）：离 120 日低点中位数 **0.209**、60 日涨幅中位数 **−3.9%**、**100% 满足启动资格**、**0% 处于"离低点已翻倍"或"60 日涨超 50%"**。

**结论："低位刚启动 → 风险更低"部分成立。**
- ✅ 相对波动更低（超额波动 −33%）、最差超额更小、亏损周更少；
- ❌ **绝对回撤没有改善**（最差周同样 −14%，跌超 5% 的周占比一样是 20%）——市场系统性下跌时低位股照跌；
- 代价：均值从 +1.65% 降到 −0.54%（t=−0.28，不显著）、上限从 +24% 压到 +12.6%。

同一批门控组合换更长持有期（5/10/20 日）也不改变结论：5 日 −0.54%、10 日 −0.88%（std 11.0%）、20 日 +0.48%（超额 +1.17%，胜率 6/12）——**都不显著**，样本 15 周太小。

**因此"我该怎么办"取决于目标**：
1. **要形态与相对稳定**（少踩高位股、看得懂、拿得住）→ 开门控：`[selection.startup_gate] enabled = true`（`mode = "eligibility"` 或更宽松的 `thresholds` + `max_dist_from_120d_low = 0.30`）；接受均值略降。
2. **要收益弹性** → 保持关闭（点估计 +1.65%/周、超额 +2.16%）。
3. **无论开不开，这两处都要改**，否则"偏好"落不到仓位：
   - `weighting = "inverse_volatility"` 与排名无关 → 换成与模型分数/排名挂钩；
   - `signal_override` 的 `forced_min_weight` 保底会让 rank 4628 的票拿 9.5% → 设上限或要求 override 也过门控。
4. 持有期与成本：当前周换手 ~0.59、单次 25bps，短持有期成本占比很高；B2 已显示 20 日持有（+20.3% 链式净收益）比 1 周口径更匹配模型，建议 `rebalance_stride_days = 5`（或更高）。
5. 风险底线：保留 `[exits]` 硬止损——低位不等于不会跌。

### 0.12 选股链路三项优化（2026-09-21）：ST/流动性过滤、排名挂钩权重、override 保底上限

**背景**（§0.10/§0.11 的结论）：权重是 `inverse_volatility`（与排名无关）、`signal_override` 有保底权重（rank 4628 的票拿 9.5%）、候选里混进 ST 与疑似停牌标的（两只 ST 一周合计 −1.28 万）。

**(1) universe 过滤**（`[selection.universe_filter]`，在取 Top-N **之前**施加）：

```toml
[selection.universe_filter]
enabled = true
exclude_st = true                  # 名称含 ST / *ST / 退
min_median_amount_20d = 10000000.0 # 近 20 交易日成交额中位数下限（元）
liquidity_window_days = 40
```

实现：`data/ingest/service.py::filter_selection_universe`（纯函数，可单测）+ `_cn_universe_filter_drop`（取名称与成交额）；审计记录 `st_dropped / illiquid_dropped / missing_amount`。2026-09-18 的组合里 `000615 *ST美谷` 已被剔除。

**(2) 权重与排名挂钩**（`weighting = "rank_power"`）：

```toml
[selection.portfolio_constraints]
weighting = "rank_power"
alpha_power = 2.0         # 第 k 名的分数项 = ((N-k+1)/N)^alpha_power
alpha_weight_floor = 0.25 # 保证末位入选者也有基础权重
vol_exponent = 0.25       # 波动只做轻微调整（0 = 纯按排名）
```

踩坑记录：先做了 `score_inverse_vol`（分数 × 1/波动），但**模型头部 96.8~99.3 分在"含信号票的归一化池"里几乎没有差别**，实际仍由波动主导（Spearman(rank, weight) = **+0.30**，排名第 5 的权重反而最大）。改成显式 rank_power 后：**Spearman = −0.70**，rank 1 拿到最大权重（13.6%）。

**(3) override 保底必须由分数支撑**（`forced_floor_score_scaling = true` + `forced_floor_max_weight = 0.05`）：保底权重按该票的归一化分数缩放并设硬上限。效果：rank 2696 的 `002268.SZ` 从 **9.5% → 6.75%**。

**验证**（同一天、同一周，2026-09-11 决策 → 09-14 开盘买入 → 09-18 收盘，扣 25bps）：

| 版本 | 组合净收益 | 超额 | 盈利/亏损 |
|---|---:|---:|---:|
| 无门 + 旧权重 | +1.71% | −1.05pp | 5/9 |
| 有门 + 旧权重（inverse_vol） | +4.84% | +2.08pp | 6/6 |
| **有门 + 新权重（rank_power）+ universe 过滤** | **+5.05%** | **+2.30pp** | 8/4 |

**仍待做**：用同一套 15 周 survey（§0.10 的脚本）在**新规则**下重跑，确认"收益/波动/回撤"三项是否同步改善；以及把 `[selection.signals] forced_max_weight`（当前 0.20）收紧，作为 override 的第二道闸。

### 0.13 新规则的 15 周复核（2026-09-21）：风险继续下降，收益也继续下降

2×2 对照（15 个决策日，2026-06-03 ~ 2026-09-10，T+1 开盘买入、5 个交易日、扣 25bps）：

| 组合 | 平均周净收益 | std | 盈利周 | 超额均值 | **超额 std** | 最差超额 |
|---|---:|---:|---:|---:|---:|---:|
| 旧：逆波动权重 · 无门 | **+1.65%** | 9.39% | 8/15 | **+2.16%** | 8.11% | −10.3% |
| 旧：逆波动权重 · 有门 | −0.54% | 7.42% | 9/15 | −0.03% | 5.46% | −7.5% |
| 新：排名权重 + universe 过滤 · 无门 | +0.13% | 8.95% | 8/15 | +0.65% | 7.19% | −10.3% |
| **新：排名权重 + universe 过滤 · 有门** | **−1.12%** | **5.31%** | 7/15 | −0.60% | **3.80%** | **−4.7%** |

（10 日口径：新规则·有门 mean −1.86%、std 7.69%、超额 std 4.54%。）

**显著性**（同日配对）：旧规则 ON−OFF = −2.19pp（t=−0.76，p=0.46，方差比 0.63）；新规则 ON−OFF = −1.25pp（t=−0.53，p=0.61，**方差比 0.35**）。

**读法**：

1. **风险端确实按设计生效**：从"旧·无门"到"新·有门"，超额波动 8.11% → 3.80%（−53%）、最差超额 −10.3% → −4.7%、单周 std 9.39% → 5.31%、最差单周 −14.0% → −10.8%。门控贡献了其中大部分（方差比 0.35 vs 0.63），排名权重与 universe 过滤只做了小幅改善（超额波动 8.11% → 7.19%）。
2. **收益端全部变差**，且方向单调：无门 +1.65% → 新无门 +0.13%；有门 −0.54% → 新有门 −1.12%。也就是说 2026-06~09 这段区间里，"低波动化"的每一步都在牺牲收益，没有免费的午餐。
3. **但差异不显著**：配对 t 检验 p=0.46/0.61，15 周、单一市场状态。**不能据此断言新规则更差**；同时"更低波动"也只是点估计（虽然方向在四个组合上完全单调，可信度高于收益差异）。
4. 门控在 15 天里只改变了部分日期的选股（其余日期两臂完全相同），说明"模型的头部是否落在合格区"本身是随行情变化的。

**结论与建议**：
- 如果目标是**低波动/拿得住/不买高位股**（你的原始偏好）→ 保留当前新规则（`rank_power` + universe 过滤 + 门控），并在真实资金上按小仓位起步；接受这段区间里收益偏低的事实。
- 如果目标是**这段区间的收益弹性** → 关掉门控（`[selection.startup_gate] enabled = false`），保留新权重（无门臂 +0.13% vs 旧无门 +1.65%，说明"排名权重 + 过滤"单独看也是略负贡献）。
- 两条路都**不显著**，所以最终应以"风险偏好"而不是"收益预期"来做决定；下一步值得做的是把样本拉长（回溯到 2026-03 或更早）再看同口径对比。

### 0.14 "缩量拉升 vs 放量出货"：文献核查 + 自有数据检验（2026-09-21）

**命题**（来自外部视频/实务叙事）：健康的拉升是缩量的（筹码锁定），散户眼中的"放量突破"是主力对倒诱多、随后派发。

**（1）自有数据检验**（clean panel + 可执行超额收益；336 个交易日、约 170 万股票日；分桶 = 5 日涨跌方向 × 20 日量比）：

| 桶 | 全市场 20 日超额均值 | 全市场离散度 | 启动域 20 日超额均值 | 启动域离散度 | 启动域命中率 |
|---|---:|---:|---:|---:|---:|
| 缩量上涨（vol<0.8 且上涨） | **+0.17%** | 14.03% | **+0.16%** | **11.71%** | 41.7% |
| 温和下跌 | +0.22% | 13.67% | −0.05% | 12.84% | 40.8% |
| 温和上涨（0.8–1.5） | +0.08% | 15.10% | −0.09% | 12.34% | 40.6% |
| 缩量下跌 | −0.06% | 13.22% | −0.32% | 11.76% | 38.5% |
| 放量下跌（vol>1.5） | −0.38% | 13.58% | −0.27% | 13.11% | 39.4% |
| **放量上涨（vol>1.5 且上涨）** | **−0.63%** | **17.42%** | **−0.75%** | **13.96%** | **37.0%** |

结论：**"放量上涨"是收益最差、离散度最高、命中率最低的桶**（两个域一致）；"缩量上涨"是上涨桶里最好的，且在启动域里离散度最低（11.71%）。**所以"避免放量上涨进场"这个过滤规则在我们数据上成立**——它同时改善收益与风险，而不是二选一。

**（2）文献核查**（Crossref 逐条核对过；标注"未复核"的是凭记忆、本轮未验证）：

| 主张 | 文献 | 方向 |
|---|---|---|
| 成交量冲击后短期反转 | Conrad, Hameed & Niden (1994, *Journal of Finance*) | 支持 |
| 真实操纵案例是"放量拉高后派发" | Aggarwal & Wu (2006, *Journal of Business*) | 支持（但只在被操纵的小样本内） |
| 高成交量后**正**超额（高成交量溢价） | Gervais, Kaniel & Mingelgrin (2001, JF；未复核) | **反对**"放量必是陷阱"的普适性 |
| 低换手溢价（量是独立定价维度） | Datar, Naik & Radcliffe (1998, *JFM*) | 限定：与"缩量=主力锁仓"无关 |
| 早期动量需放量确认 | Trading Volume and Momentum（本计划 §2.4 已引） | **反对** |
| 波动率管理提升 Sharpe、降回撤 | Moreira & Muir (2017, JF；NBER w22208) | 支持（与本命题无关但正是"收益高+回撤小"的正解） |
| 趋势择时降回撤 | Moskowitz, Ooi & Pedersen (2012, JFE；Faber 2007 未复核) | 支持 |
| 止损规则改善动量策略 | Kaminski & Lo (2014, JFM)、Grossman & Zhou (1993, Math. Finance) | 支持 |
| 低波动/低 beta 异象 | Frazzini & Pedersen (2014, JFE) | 支持 |

**判定**：叙事里"主力对倒"的**因果机制**没有学术支撑（操纵只在少数被查处的股票上被实证）；但"放量上涨后跑输 + 波动更大"这一**可观测事实**在文献与我们的数据里都成立，因此可以当**过滤器**用，不能当"主力行为"解释用。

**（3）要同时提高收益、降低回撤，有文献支撑的四条**（都能映射到现有配置）：

1. **波动率目标**（Moreira & Muir）：把 `[selection.risk_control].vol_target_mode` 从 `budget`（当前会把仓位放大 1.53× 到 53%）改成 `cap`，或直接把 `target_volatility` 降到 0.12–0.15；
2. **趋势/择时**：用已有的 `market_trend_20d` / `max_gross_exposure_by_regime` 把总仓位按市场状态缩放（Faber 式），而不是只在动量 sleeve 上用；
3. **止损**：`[exits]` 里加按 ATR 缩放的止损与时间止损（Kaminski & Lo：动量策略的止损能同时提高收益与降低回撤）；
4. **低波动倾斜**：把 §0.12 的 `rank_power` 与逆波动再结合（已部分实现）。

**（4）落地顺序建议**：① 加"放量上涨"过滤（`(pv_return_5d>0) & (pv_volume_ratio_20d>1.5)` → 剔除或降权；本次数据+文献双支持，且同时改善收益与波动）；② vol-target 改 `cap`；③ 收紧止损；④ 用同一套 15 周 survey 做 A/B（脚本已就绪）。

### 0.15 A/B：放量上涨过滤 + vol-target cap + 止损（2026-09-21）

改动：① `[selection.universe_filter] exclude_volume_breakout = true`（5 日上涨且 20 日量比>1.5 不进候选，在 Top-N 前施加）；
② `[selection.risk_control] vol_target_mode: budget → cap`（不再把仓位放大 1.53× 到 53%）；
③ 评估层叠加 6% 硬止损（跌破买入价 6% 视为在止损位成交，含 25bps 成本）。配置：`config/cn_pipeline_ab_fix.toml`。

15 个决策日（2026-06-03 ~ 2026-09-10，T+1 开盘买入、5 日、扣 25bps）：

| 组合 | 平均周净收益 | 中位数 | std | 最差单周 | 盈利周 | 超额均值 | 超额 std | 最差超额 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 基线（新规则·有门） | −1.12% | −0.46% | 5.31% | −10.78% | 7/15 | −0.60% | 3.80% | −4.67% |
| A/B fix（过滤 + vol cap） | −1.24% | −1.26% | **3.97%** | −9.93% | 6/15 | −0.73% | 3.80% | −6.49% |
| **A/B fix + 6% 止损** | **−0.99%** | −0.85% | **2.47%** | **−5.84%** | 5/15 | — | — | — |
| A/B fix · 10 日持有 | −2.03% | −0.52% | 5.89% | −14.43% | 6/14 | −1.51% | 4.26% | −10.26% |

**读法**：

1. **过滤 + vol cap 只降波动、不增收益**：std 5.31% → 3.97%，但均值 −1.12% → −1.24%，最差超额反而更差（−4.67% → −6.49%）。候选数从平均 11.1 只降到 7.3 只——这条过滤偏激进。
2. **止损是唯一"两头都改善"的杠杆**（同臂对比）：均值 −1.24% → **−0.99%**，std 3.97% → **2.47%**，最差单周 −9.93% → **−5.84%**；止损触发率 40.8%，方差比 0.39。显著性仍弱（t=+0.39，p=0.70；Levene p=0.22）。注意评估假设"在止损位成交"，跳空会高估效果，实盘建议 7–8% 或按 ATR 缩放。
3. **10 日持有更差**（−2.03%）——这段区间里拉长持有期没有帮助。
4. 与仓库既有结论的差异：`[exits.rules] stop_loss_pct = 0.0` 的原注是"样本期该规则无效"；本次同臂 A/B 显示 6% 止损同时改善均值与波动，但样本 15 周，建议以"降回撤"为目的开启（7–8%），不要指望它带来收益。

**结论**：在这一段行情里，"低波动化"三步（过滤、vol cap、止损）把周度 std 从 9.39%（旧·无门）压到 **2.47%**、最差单周从 −14.0% 压到 −5.84%；收益端始终没有改善（均值在 −1% 上下，均不显著）。要"收益更高 + 回撤更小"，组合层只能做到"回撤更小"这一半；另一半需要新的信息源（更长历史/更多数据源）或改进的预测（W5 多周期头）。

### 0.16 案例复盘：江苏新能、成都先导 + "资金流 vs 成交量"检验（2026-09-21）

**(1) 江苏新能 603693.SH —— "放量点诱多"得到证实**

| 决策日 | 当日涨跌 | 量/20日均 | 次日开盘买入 | 持有到 09-18 收盘 |
|---|---:|---:|---:|---:|
| 09-09 | +1.2% | 1.33 | 11.52 | +8.0% |
| 09-10 | +1.1% | 1.99 | 11.60 | **+7.2%** |
| **09-11** | **+10.0%（涨停）** | **5.27** | **12.80** | **−2.8%** |
| 09-14 | −3.2% | 4.24 | 12.37 | +0.6% |
| 09-15 | +6.9% | 3.94 | 12.85 | −3.2% |
| 09-16 | −3.8% | 2.76 | 12.50 | −0.5% |

放量涨停那天是**整个窗口最差的买点**（−2.8%），而它之前每一档都是 +7%~+11%。注意：**启动资格门在 09-11 仍判定它合格**（离 120 日低点 +16.8%、距 60 日高点 −5.7%、60 日涨幅 −5.3%）——真正会挡掉它的是 §0.15 新加的 `exclude_volume_breakout`（量比>1.5 且上涨）。

**(2) 成都先导 688222.SH —— 最佳买点是"缩量回调末端"，不是放量突破日**

| 决策日 | 当日涨跌 | 量/20日均 | 次日开盘买入 | 持有到 09-18 收盘 |
|---|---:|---:|---:|---:|
| 09-04 | −3.0% | 0.83 | 31.97 | +25.0% |
| 09-10 | −3.5% | 0.68 | 30.69 | +30.2% |
| **09-11** | −2.5% | **0.64** | **30.34** | **+31.7%** |
| 09-14 | +6.6% | 0.84 | 32.40 | +23.4% |
| **09-16** | **+14.0%** | **1.75** | **36.46** | **+9.6%** |
| 09-17 | +2.9% | 1.57 | 39.00 | +2.5% |

**结论与已知缺口**：买在"缩量回调末端"（09-10/09-11，量比 0.64~0.68）能拿到 +30% 以上；买在放量突破日（09-16）只剩 +9.6%。但**我们的启动门控会排除成都先导**——它离 120 日低点 +36.7%，超过门控 30% 的上限。即：门控在挡住"高风险高位股"的同时，也结构性放弃了这类"第一波之后的缩量回调"。

**(3) "资金流入变化"是否比成交量更好用？——不，成交量信息量更大**

启动域 753,385 个股票日（有量比与 20 日前瞻超额）：

| 仅按成交量 | n | 20 日超额均值 | 离散度 |
|---|---:|---:|---:|
| 缩量 <0.8 | 323,177 | −0.15% | 11.75% |
| 中性 | 354,669 | −0.07% | 12.58% |
| **放量 >1.5** | 75,539 | **−0.63%** | **13.77%** |

| 仅按资金流 z5 | n | 均值 | 离散度 |
|---|---:|---:|---:|
| 流出 <−0.5 | 228,238 | −0.22% | 12.32% |
| 中性 | 268,963 | −0.12% | 12.46% |
| 流入 >0.5 | 243,550 | −0.09% | 12.50% |

2×2（成交量 × 资金流）：

| vol \ flow | 流出<−0.5 | 中性 | 流入>0.5 |
|---|---:|---:|---:|
| 缩量<0.8 | −0.05% | −0.15% | −0.18% |
| 中性 | −0.23% | −0.00% | **+0.11%** |
| **放量>1.5** | **−0.75%** | −0.63% | −0.49% |

- **成交量的组间差 0.48pp，资金流只有 0.13pp** → 成交量是更强的决策变量；资金流方向正确但幅度小。
- 资金流最有价值的用法是**在放量名单里做二次确认**：放量+流出 −0.75% vs 放量+流入 −0.49%（差 0.26pp）。
- 覆盖：面板里 `moneyflow_net_z_5d` 覆盖 98.3%，但**个股/关键日存在缺口**（江苏新能、成都先导在 09-11 的 z5 为空）→ 适合"加权/确认"，不适合硬门禁。

**(4) 据此的三条调整建议**：
1. 把"放量上涨"从**硬剔除**改成**"放量 + 资金流出/中性才剔除"**（硬剔除会误杀 09-16 那类仍能涨 9.6% 的票）；
2. 资金流只做**确认与降权**，不做硬门禁；
3. 新增"**第二梯队**"小仓位：0.30 < `dist_from_120d_low` ≤ 0.55 且呈"缩量回调 + 资金流入"的标的给 1~2 个槽位（两个案例的最佳买点都在这一区）。

### 0.17 文献综合：成交量与资金流（2026-09-21，逐条经 Crossref 核对）

**（A）成交量 / 量价关系**

| 文献 | 核心结论 | 对我们的含义 |
|---|---|---|
| Karpoff (1987, *JFQA*) ✓ | 量与绝对价格变动正相关，上涨日更强 | 量是"状态"变量，不是方向变量 |
| Conrad, Hameed & Niden (1994, *JF*) ✓ | 高成交量 → **短期反转**；低成交量 → 延续 | 直接支持"放量买点差"（我们实测放量>1.5 的 20 日超额 −0.63%） |
| Gervais, Kaniel & Mingelgrin (2001, *JF*) ✓ | 异常高量后 **1 个月正超额**（高量溢价） | **反例**：不是"放量必坏"；差异来自持有期与市场结构 |
| Chordia & Swaminathan (2000, *JF*) ✓ | 高量放大收益的交叉自相关（信息扩散速度） | 量可作状态/节奏变量 |
| **Llorente, Michaely, Saar & Wang (2002, *RFS*) ✓** | **关键调节**：成交量由**知情交易**驱动 → 动量延续；由**流动性/对冲**驱动 → 反转 | 给"放量陷阱"提供可检验机制；A 股散户占比高 → 放量多为流动性驱动 → 偏反转（与我们的实测一致） |
| Statman, Thorley & Vorkink (2006, *RFS*) ✓ | 投资者过度自信 → 量升后收益回落 | 行为机制 |
| Lo & Wang (2000, *RFS*) ✓ | "量"的定义不同 → 结论不同 | 我们用的是"量/20 日均"，属粗粒度 |
| Aggarwal & Wu (2006, *JB*) ✓ | 被查处的操纵案例：放量拉高 → 派发 | 少数标的的因果证据，不能外推为普适规律 |
| Allen & Gale (1992, *RFS*)（未复核） | 操纵的理论框架 | 仅方向参考 |

**（B）资金流 / 订单流**

| 文献 | 核心结论 | 对我们的含义 |
|---|---|---|
| Chordia, Roll & Subrahmanyam (2002, *JFE*) ✓ | 市场层面订单不平衡预测短期收益 | 用的是**逐笔订单数据** |
| Chordia & Subrahmanyam (2004, *JFE*) ✓ | 个股层面同理 | 同上；我们的"资金流"只是其粗代理 |
| Easley, López de Prado & O'Hara (2012, *RFS*) ✓ | VPIN / 流动毒性预测**波动与流动性风险**，不预测方向 | 资金流类指标适合做**仓位缩放/风控**，不适合做方向 |
| Datar, Naik & Radcliffe (1998, *JFM*) ✓ | 低换手溢价（量是独立定价维度） | 与"缩量=锁仓"叙事无关 |
| Amihud (2002, *JFM*)（未复核） | 非流动性溢价 | 我们 universe 过滤的成交额下限是同一维度的极端 |
| — | 我们手上的"资金流"= 东财/同花顺/DC 按成交金额分档的**代理**，不是逐笔订单流 | 信息含量天生更低：实测组间差 **0.13pp** vs 成交量 **0.48pp** |

**（C）文献与我们数据的对照**（启动域 753,385 股票日，20 日前瞻超额）

| 主张 | 文献方向 | 我们的实测 |
|---|---|---|
| 放量后短期跑输 | Conrad et al. 1994；Llorente et al. 2002（流动性驱动） | 放量>1.5：**−0.63%**（缩量 −0.15%），离散度最高 13.77% |
| 高量溢价 | Gervais et al. 2001（美股 1 个月） | 20 日/5 日口径未见正溢价 —— 与 A 股 T+1 + 散户结构一致 |
| 知情 vs 流动性驱动 | Llorente et al. 2002 | 放量+流入 −0.49% vs 放量+流出 −0.75%；最好格"中性量能+流入" +0.11% → **方向一致** |
| 订单不平衡预测收益 | Chordia et al. 2002/2004 | 资金流代理组间差仅 0.13pp（成交量 0.48pp） |
| 毒性预测波动 | Easley et al. 2012 | 资金流 z 与离散度几乎无关（12.32% vs 12.50%） |
| 操纵=放量派发 | Aggarwal & Wu 2006 | 案例吻合（江苏新能放量涨停日买入 −2.8%；成都先导缩量回调末端 +31.7% vs 放量突破日 +9.6%），但不可外推为"放量必坏" |

**（D）综合结论**

1. **成交量是风险/状态变量**：放量 → 高波动 + 短期负超额（文献与自有数据一致），可作为**过滤器**。
2. **"放量=陷阱"不是普适定律**：高量溢价在美股 1 个月口径成立；差别来自**持有期**与**交易者结构**（Llorente et al. 2002）。A 股短期反转更占优。
3. **"量 + 资金流方向"联合判断优于单看量**（我们的 2×2 就是 Llorente 框架的粗粒度实现）：最差格"放量+流出" −0.75%，最好格"中性+流入" +0.11%。
4. **供应商资金流 ≠ 订单流**：边际信息量只有成交量的约 1/4，且个股/关键日有缺口 → 只能做**确认与降权**，不能做核心 alpha 或硬门禁。
5. **风控含义**：按 Easley et al. (2012)，资金流/毒性类指标更适合做**仓位缩放**而非方向判断。

**（E）据此的三条落地（下一步）**

1. 放量过滤升级为"量 + 资金流方向"（放量且资金流出/中性才剔除）；
2. 资金流做 tilt 降权/确认（加权，不设硬门禁）；
3. **接入龙虎榜/机构席位（`top_list` / `top_inst`）作为"知情交易"代理**——数据已在 raw 层（`assets/data/raw/moneyflow_snapshots/top_list_*.parquet`、`top_inst_*.parquet`，当前 7 个快照），是文献里最接近"主力真实动作"的可得代理，且可检验（上榜后 5/20 日超额、机构净买入 vs 游资净买入）。

### 0.18 A/B：资金流确认过滤 + 第二梯队 + 资金流 tilt（2026-09-21）

**改了三处**（配置 `config/cn_pipeline_ab_fix2.toml`）：

1. **放量过滤升级为"资金流确认"**：`volume_breakout_flow_z_min = 1.0`——放量上涨（量比>1.5 且 5 日上涨）只有在 `moneyflow_net_z_5d ≥ 1.0` 时才保留，否则剔除（缺数据按 `drop`）；依据 Llorente et al. (2002) 的"知情 vs 流动性驱动"框架。
2. **第二梯队**：`second_tier_enabled = true`——`0.30 < dist_from_120d_low ≤ 0.55` 且量比 < 1.0 且资金流 z ≥ 0 的标的进入候选（不受核心门控的 30% 上限限制），单只权重上限 5%（`weight_caps` 会把省下的预算按比例还给核心名）。
3. **资金流 tilt**：权重乘以 `1 + 0.25·tanh(z5)`（确认/降权，不设硬门禁）。

**15 个决策日（2026-06-03 ~ 2026-09-10，T+1 开盘买入、5 日、扣 25bps）**

| 组合 | 平均周净收益 | 中位数 | std | 最差单周 | 最好单周 | 盈利周 | 超额 std | 最差超额 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 旧·逆波动·无门 | +1.65% | +0.56% | 9.39% | −13.98% | +24.09% | 8/15 | 8.11% | −10.30% |
| 旧·逆波动·有门 | −0.54% | +0.42% | 7.42% | −14.31% | +12.61% | 9/15 | 5.46% | −7.47% |
| 新·有门（上一版基线） | −1.12% | −0.46% | 5.31% | −10.78% | +6.29% | 7/15 | 3.80% | −4.67% |
| fix（放量**硬**过滤 + vol cap） | −1.24% | −1.26% | **3.97%** | −9.93% | +6.76% | 6/15 | 3.80% | −6.49% |
| fix + 6% 止损 | −0.99% | −0.85% | **2.47%** | −5.84% | +4.12% | 5/15 | — | — |
| **fix2（资金流确认 + 二队 + tilt）** | **+0.55%** | +1.48% | 6.67% | −10.99% | **+17.27%** | 8/15 | 6.90% | −9.25% |
| **fix2 + 6% 止损** | **+0.68%** | +0.08% | 5.41% | **−6.25%** | +14.68% | 8/15 | — | — |

**配对显著性**（同日）：fix2 − 基线 = **+1.66pp**（t=+1.57，p=0.14，胜 10/15）；fix2+止损 − 基线 = **+1.80pp**（t=+1.45，p=0.17，胜 11/15）；fix2 − 旧·无门 = −1.10pp（t=−0.36，p=0.73，方差比 0.50）。fix2 同臂加止损：+0.55% → +0.68%（t=0.16），方差比 0.66。

**读法**：

1. **"资金流确认"救了被硬过滤误杀的收益**：fix 把放量一律剔除（均值 −1.24%、std 3.97%），fix2 只剔除"放量但资金流不支持"的（均值 **+0.55%**、std 6.67%）——与 §0.16 的"放量+流入 −0.49% vs 放量+流出 −0.75%"完全一致。代价是离散度回到 6.67%。
2. **第二梯队 + tilt 让中位数从 −0.46% 提到 +1.48%**（基线 → fix2），胜率 7/15 → 8/15；最好单周从 +6.3% 回到 +17.3%（说明不再系统性错过"第二轮"行情，如成都先导那类）。
3. **止损仍是唯一稳定改善尾部的杠杆**：fix2 加 6% 止损后最差单周 −10.99% → **−6.25%**、std 6.67% → 5.41%，均值还略升（+0.55% → +0.68%）。
4. **fix2+止损 vs 基线**：均值 +1.80pp、胜 11/15——但仍在 15 周样本内不显著（p=0.17）。相对旧·无门仍低 1.10pp（不显著），可是**尾部明显更好**（最差 −6.25% vs −13.98%、超额波动 6.90% vs 8.11%）。

**结论**：三条改进的方向是对的——**"资金流确认"避免误杀、"第二梯队"补回第二轮行情、"止损"压住尾部**；组合起来在 15 周里从"均值 −1.12%、最差 −10.8%"变成"均值 +0.68%、最差 −6.25%"。要在统计上站住，需要把样本拉长（建议回溯到 2026-03 或更早，用同一脚本复跑）。

### 0.19 样本扩展与止损口径（2026-09-21）：169 个 OOS 决策日的显著性检验

**(1) 把规则放到 169 个 OOS 决策日上检验（4× 于 survey，且无前视）**

survey 重放的是**生产模型**（其训练窗口覆盖 2026-04 之前），早期日期会带入前视；因此改用**13 折 OOS 预测**（每折只用严格在前的模型，169 个决策日 2025-12-02~2026-08-12、418,818 行）在其上套用选股规则：

```text
基线 : 启动资格域 -> 按模型分取 Top-10（等权）
fix2 : 再叠加"放量+资金流确认过滤"与"第二梯队准入" -> Top-10（等权）
```

| 口径（扣 25bps） | 基线 | fix2 规则 | 差值 | t | p |
|---|---:|---:|---:|---:|---:|
| 5 日超额 | +0.56% | **+0.68%** | **+0.12%** | +2.20 | **0.029** |
| 10 日超额 | +0.95% | **+1.14%** | **+0.19%** | +2.29 | **0.023** |
| 20 日超额 | +1.70% | +1.85% | +0.15% | +1.17 | 0.244 |
| 20 日最差单日 | −11.20% | −10.45% | — | — | 波动 6.85% → 6.83% |
| 离 120 日低点中位 | 0.137 | 0.136 | — | — | 持仓数中位 10 → 10 |

**这是本轮第一条统计显著的结果**：新规则在 **5 日（+0.12pp，p=0.03）与 10 日（+0.19pp，p=0.02）**上显著改善 Top-10 组合的扣成本超额收益，20 日中性（+0.15pp，p=0.24）。方向与"量/资金流是**入场时机**变量"的判断一致——它改善短周期，不改变长周期。

**(2) 止损口径：ATR 缩放优于固定百分比**

15 个决策日、5 日、扣 25bps：

| 组合 | 平均 | std | 最差 | 真实触发率 |
|---|---:|---:|---:|---:|
| baseline 无止损 | −1.12% | 5.31% | −10.78% | — |
| baseline + ATR×2.5（4%~10% 夹取） | −0.90% | **3.87%** | **−7.83%** | **22%** |
| fix2 无止损 | +0.55% | 6.67% | −10.99% | — |
| fix2 + 6% 固定止损 | **+0.68%** | 5.41% | −6.25% | 43% |
| fix2 归档选股 + ATR×2.5（离线、等权） | −0.93% | **3.67%** | **−7.77%** | **22%** |
| **fix2 + ATR×2.5（正式跑，加权口径）** | **+1.00%** | 6.09% | −9.33% | **19%** |

同臂对比：baseline 加 ATR 止损 −1.12% → −0.90%（t=+0.34，p=0.74，方差比 0.53）；fix2（等权）−0.96% → −0.93%（t=+0.05，p=0.96，方差比 0.51）。

**结论**：ATR 止损用**不到一半的触发率**（fix2 臂：19% vs 43%）换来相近的波动压缩——固定 6% 止损在震荡市里被反复扫损。
**fix2 + ATR×2.5 是全部对照里最好的一档**：均值 **+1.00%**（基线 −1.12%，差 +2.1pp）、最好单周 +17.27%、触发率 19%；
配对检验：fix2+ATR − 基线 = **+2.76pp**（t=1.43，p=0.18，胜 9/15）；fix2+ATR − fix2 无止损 = **+0.45pp**（t=1.51，p=0.15，胜 4/15——均值改善来自砍尾部，而不是逐日胜率）。
**建议把生产 `[exits]` 的止损改成 `k×ATR20`（k≈2.5，下限 4%、上限 10%）**，而不是固定百分比。

**(3) 顺带修正**：此前汇报的"止损触发率 40.8%/43.1%"是统计口径写错（把"止损后收益为负"当成了触发），已改为记录真实触发标记；正确值见上表。

### 0.20 生产落地（2026-09-21）：fix2 规则 + ATR 止损写入默认配置

`config/cn_pipeline.toml`（每日生产脚本 `scripts/run_daily_production.sh` 使用的默认配置）已写入本轮验证过的全部规则：

| 位置 | 参数 | 值 | 依据 |
|---|---|---|---|
| `[selection.universe_filter]` | `exclude_st` / `min_median_amount_20d` | true / 1e7 | §0.15/§0.16（ST 与疑似停牌标的） |
| | `exclude_volume_breakout` / `volume_breakout_ratio` | true / 1.5 | §0.14（放量上涨是最差桶） |
| | `volume_breakout_flow_z_min` / `volume_breakout_missing_flow` | 1.0 / drop | §0.16/§0.18（资金流确认；Llorente et al. 2002） |
| `[selection.startup_gate]` | `enabled` | true | §0.12 |
| | `second_tier_*`（0.30~0.55 缩量回调 + 资金流入，单只 ≤5%） | true | §0.16/§0.18（补回"第二轮"行情） |
| `[selection.portfolio_constraints]` | `weighting` / `alpha_power` / `vol_exponent` | rank_power / 2.0 / 0.25 | §0.12（排名与权重挂钩，Spearman −0.70） |
| | `forced_floor_score_scaling` / `forced_floor_max_weight` | true / 0.05 | §0.12（override 保底须由分数支撑） |
| | `flow_tilt_strength` / `flow_column` | 0.25 / moneyflow_net_z_5d_clean | §0.18 |
| `[selection.risk_control]` | `vol_target_mode` | **cap**（原 budget） | §0.15（budget 会把仓位放大 1.53× 到 53%） |
| `[exits.rules]` | `stop_loss_atr_multiple` / `min` / `max` | 2.5 / 4% / 10% | §0.19（触发率 19% 即可压住尾部） |
| | `stop_loss_pct` | 0.0（保持关闭） | §0.19（固定百分比被反复扫损） |

代码改动：`factor_engine/portfolio/exits.py`（新增 ATR 缩放止损：`stop_loss_atr_multiple` + 夹取区间，输出 `atr_pct_14`/`effective_stop` 便于审计）、`data/ingest/service.py`（在市场状态里计算 14 日均振幅 `atr_pct_14`）。

回归测试（`test/test_selection_universe_filter.py` 新增 3 例）：ATR 止损的夹取与触发、`multiple=0` 时关闭、**生产配置守卫**（断言 `cn_pipeline.toml` 里上述关键项仍然存在且取值正确，防止后续编辑静默回退）。全套 **167 passed**。

**复验**（用生产配置 `config/cn_pipeline.toml` 重跑 15 个决策日的 survey，ATR 止损）：

| 5 日 · 扣 25bps | 基线（上一版） | fix2 | fix2 + ATR | **生产配置复验** |
|---|---:|---:|---:|---:|
| 平均 | −1.12% | +0.55% | +1.00% | **+1.00%** |
| 中位数 | −0.46% | +1.48% | +0.74% | +0.74% |
| std | 5.31% | 6.67% | 6.09% | 6.09% |
| 最差单周 | −10.78% | −10.99% | −9.33% | −9.33% |
| 最好单周 | +6.29% | +17.27% | +17.27% | +17.27% |
| 盈利周 | 7/15 | 8/15 | 8/15 | 8/15 |
| 止损触发率 | — | — | 19% | 19% |

生产配置复验与 `fix2 + ATR` 臂**逐日 100% 一致**（同日同规则应完全一致），说明写入默认配置的规则与验证过的完全同一套。
与"基线 + ATR 止损"的同日配对：**+1.91pp（t=+2.09，p=0.06，胜 9/15）**。

## 11. 剩余待完善项（2026-09-20 盘点）

> 2026-09-21 更新：§11 的两项收尾工作已拆成独立计划 `P1_18_top_list_informed_proxy_plan.md`（龙虎榜知情代理特征接入 + 选股级 169 日复验）。


按"能否立刻做"排序。证据目录：`artifacts/path_labels_meta_labeling/`。

### A. 已落地但缺验收

| 项 | 现状 | 完成标准 |
|---|---|---|
| A1 W1 画像验收 | **已完成，结论：不达标（2026-09-20）**。同 13 折、同骨架（全量画像、startup 资格、无门禁、无残差）下：路径标签 Top10% 波动 0.3516 vs 0.3349、已实现超额 1.11% vs 1.41%、RankIC(超额) 0.0688 vs 0.1042、Top-20 扣成本 1.59% vs 2.22%。新增能力：`forward_return` 标签模式也会按 startup 资格过滤，保证两次运行行集一致 | 结论已回写 §7/§0.4；后续所有 A/B 以 **forward_return 标签** 为基线（见 §0.5） |
| A2 W3 二元特征保护 | 旧面板 4 折 transformer manifest 中 `protected_features_applied` 含 6 个 `flow_second_wave_*`、`missing` 为空 | 在 376 日新面板上重跑一次 transformer OOS，确认 flag 列标准化前后非零方差保持、审计字段完整 |
| A3 W5 多周期对照 | 5/10/20/60 日可执行与超额收益标签已落盘并通过测试 | 按 5/10/20 日分别报告 RankIC 与 Top-20 扣成本净值，确认或否证"二次资金流对 5~10 日更有效" |

### B. 做到一半

| 项 | 现状 | 缺什么 |
|---|---|---|
| B1 W4 残差中性化 | **已完成判定：不通过（2026-09-20）**。实现已优化：残差只在窄子集上计算（不再整面板复制），审计向量化为 groupby 聚合——全量验收测量从「跑不完」降到 **27.7s**；新增 `replace` 模式（残差替换原始列）。三次 13 折 A/B（基线 = forward_return 标签 + 全量画像）：additive 1.75%、replace 2.26%、基线 2.22%；Top10% 波动三者均 ≈0.335（无下降）；RankIC(超额) 0.1029~0.1036 vs 0.1042。同时修掉两处实验缺陷：① 4 个控制变量被误列为待残差化特征（新增 config 守卫测试）；② ”保留原列 + 加残差列”与验收标准自相矛盾（已补 replace 模式） |
| B2 §6 评估口径 | **已完成（2026-09-20）**：1/H 重叠修正、跨折链式净值、扣成本（25bps 往返，配置化）、Top-K/调仓频率记录、行业 HHI、5/10/20/60 日多周期评估全部落地（`test_walk_forward_evaluation.py` 7 用例）。踩过的坑：注册表 `industry_l1` 是分类法名称，行业要用 `industry_l2` | — |
| B3 门禁复检入口 | 生产已关闭（`enabled = false`），`learned`/`rule` 两套实现保留 | 把"跨折符号一致性"写成 manifest 准入指标（例如 gated>ungated 折数 ≥ 2/3 且 approved>rejected 同号），不满足不许开启 |

### C. 完全未开始

| 项 | 说明 |
|---|---|
| C1 W6 在线聚合 | 无代码；Bernstein 式权重序列落盘与净值对照都缺 |
| C2 W7 浅层 GRU/TCN | 只有 CNN（`TemporalCNN`），无 GRU；按 0.4 的顺序仍应压后 |
| C3 多任务/多标签训练 | 训练侧只支持单个 `label_column`，5/10/20 日多头需要新接口 |
| C4 稳定性特征筛选 | 本轮提出未实现：多折同号 IC 筛选、相关性聚类去冗余（对标 RD-Agent 的 `deduplicate_new_factors`）或 DoubleEnsemble 式可学习 mask |

### D. 工程与一致性隐患

| 项 | 风险 | 建议 |
|---|---|---|
| D1 `config/cn_pipeline.toml` 仍为 `clean_panel.days = 365` | 干净面板是**共享数据集**：用默认配置跑一次 `--stage clean_panel` 会把面板缩回 242 日，等于废掉 13 折基准 | 同步改为 560，或在 stage 内加"窗口不得小于现有面板"的保护与告警 |
| D2 `config/cn_pipeline_moneyflow_sources.toml` | TOML 解析直接报错（重复键，第 96 行） | 修键名或移除该研究配置 |
| D3 Transformer 未在新面板复跑 | §3.8 的"LightGBM 优于 Transformer"仍基于 4 折/40 日 | 在 376 日面板补一次 transformer OOS（可用 `transformer_max_samples` 控成本） |
| D4 决策频率独立性 | 相邻决策日 20 日前瞻窗口重叠约 95%，显著性被高估 | 补 `prediction_stride=5` 的周频对照 |
| D5 本轮 W4 改动未入 artifacts | 上一版 `VERIFICATION.txt` 只覆盖到扩样本那轮 | 补 `MODIFIED/`、`DIFF_FILE.diff`、`VERIFICATION.txt` 追加段 |

### 建议执行顺序

```text
1. A1 + A2（便宜，且直接影响结论：W1 画像是否成立、W3 保护在新面板是否仍成立）
2. B1 的 ①②（W4 审计向量化 + 13 折 A/B；当前唯一"已写代码但未验证"的项）
3. D1 + D2（配置隐患，几分钟）
4. B2（评估出真净值与回撤，之后收益口径才可比）
5. A3 + C3（多周期，需要训练接口）
6. C1 / C2 / C4（按需；C2 在 C1 之前没有意义）
```

---
