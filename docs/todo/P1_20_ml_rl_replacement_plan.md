# P1.20 用机器学习 / 强化学习替换流水线中的人工规则

> 状态：规划中（2026-09-22 建立）。按"先做收益最确定、验证最便宜的三项"排期。
> 关联：`docs/runbooks/cn-data-pipeline.md`（全链路现状）、`P1_17_path_labels_meta_labeling_plan.md`（验收口径）、
> `P1_18_top_list_informed_proxy_plan.md`（W3 否证：规则事件在 5/20 日无 alpha）、`P1_19_cn_data_hygiene_plan.md`（数据卫生）
> 目标：把流水线里**手写阈值/查表**的部分换成可学习组件，同时保留数据层与组合优化器的确定性。

---

## 0. 一句话结论

全链路 8 处是人工规则，其中 **3 处高价值**（`regime` 硬标签、风控缩放/敞口查表、出场与再平衡节奏），
**2 处明确不做**（组合优化器主体、执行/TCA 模型，规模与审计都不匹配）；
所有替换都必须走 13 折 / 169 日 OOS + 容量匹配 + 固定 seed 的验收，否则不予合并。

---

## 1. 现状盘点（逐段，均核对当前代码与配置）

| 阶段 / 组件 | 现状实现（核对位置） | 可换 | 判定依据 |
|---|---|---|---|
| `daily_bars`/`moneyflow`/`fundamental`/`features`/`clean_panel` | 抓取 + 公式 + PIT 校验（`data/ingest/service.py`） | ❌ | 确定性流程，换 ML 只会引入不可复现风险；只加异常检测做质量告警（P1_19 §3） |
| `regime` | `build_cn_market_regime(trend_window=60, breadth_window=20, volatility_window=20, hysteresis_days=3)` → bull/sideways/bear **硬标签** | ✅ 高 | 三段阈值 + 迟滞是纯人工映射；输出不可微、无概率 |
| `model_scores` | LightGBM + Transformer 打分（已 ML） | ⬆️ | 升级方向是 MoE / 学习型 router（本文 W4） |
| `preselection` 模型排序 | 模型 Top-N（已 ML） | ⬆️ | 可升级为 listwise ranking；meta-labeling 已在 P1.17 落地 |
| `preselection` 信号 sleeve | 规则阈值：`min_score=60`、`min_model_score=75`、`max_runup_5d=0.20`、`max_breakout_extension=0.05`、`volume_dryup_ratio=0.70`、`volume_expansion_min=1.30`、`channel_width_max=0.35`、`max_market_turnover_z=1.5~2.0` | ⚠️ 暂不做 | P1.18 W3 已否证"规则事件的条件期望"（机构净买入 5 日 −0.20pp, t=−0.20） |
| `selection.startup_gate` | 规则阈值 `max_dist_from_120d_low=0.30`、`max_return_60d=0.35`、`max_dist_from_60d_high=−0.05` + 第二梯队 | ⚠️ 可（W5） | 与 P1.17 meta-labeling 的二阶门同源，可用学习型资格分类器替换 |
| `selection.universe_filter` | ST / `min_median_amount_20d=1e7` / 放量剔除（`exclude_volume_breakout`） | ❌ 保留规则 | 可解释、防未来函数、合规；只做告警不做 alpha |
| `pk` 组合优化 | mean-variance + 成本 + 约束（`factor_engine/portfolio/optimizer.py`） | ❌ | 凸优化器，RL 只会更差更难审计；要换的是**输入** |
| `pk` 风控缩放 | `max_gross_exposure_by_regime` = bull 0.85 / sideways 0.70 / bear 0.60；`risk_scalers`(5 个) 手写 `threshold/slope/floor`（如 `tr_zscore_20: 1.5/0.25/0.5`）；`market_deleverage: market_tr_z 0.8/0.25/0.6` | ✅ 高（W2） | 全是手填启发式；`vol_target_mode=cap`、`risk_parity_blend=0.35` 也是固定权重 |
| 再平衡节奏 | `[selection].rebalance_stride_days = 5`（固定 5 个交易日） | ✅ 高（W2） | 成本与信号衰减都是可观测的，固定 stride 是明显次优 |
| `exits` | `exclude_st`、`min_median_amount_20d=5e7`、`min_model_percentile=30`、`take_profit_pos=0.80`、`take_profit_ret20=0.15`、`reduce_ratio=0.34`、`max_weight=0.35`、`stop_loss_atr_multiple=2.5`（夹取 [4%,10%]） | ✅ 高（W3） | 我们自己的 A/B：固定 6% 止损触发 43%、5 日 +0.68%；ATR×2.5 触发 19%、+1.00% → 阈值本身就是应该被学习的对象 |
| 成本模型 | `costs.py`: `slippage = spread*0.5 + vol*18`；`impact = 4 + 85*sqrt(participation)*vol` | ⚠️ 低 | 4.5 万账户 + `max_participation=0.05`，收益极小 |
| 执行 / 下单 | 整手修复、`--profile` 预算、lot rounding | ❌ | RL 执行只在机构规模有意义 |
| 报告 / 通知 / 调度 / 幂等标记 | 基础设施 | ❌ | —— |

---

## 2. 文献依据（已逐条核对 arXiv 摘要）

| 结论 | 来源 | 对本方案的含义 |
|---|---|---|
| regime 信息**只用于路由**、不要拼进预测输入；soft routing 优于 hard routing | RG-ResMoE, arXiv:2608.12251（1,027 只美股、容量匹配 walk-forward，日本面板复现） | W1/W4 的门只吃 regime（概率）变量 |
| 统计跳跃模型给 regime 概率、可降下行风险 | arXiv:2402.05272；arXiv:2406.09578；arXiv:2509.26029 | W1 的实现选项 |
| 自适应再平衡间隔 | arXiv:2510.14985（DeepAries）；arXiv:2410.01864 | W2 直接对应 `rebalance_stride_days` |
| 最优停止的深度实现 | arXiv:1804.05394（Deep Optimal Stopping） | W3 出场策略的方法骨架 |
| DRL 组合/配置 | arXiv:1706.10059；arXiv:2010.04404；arXiv:2005.13665（端到端可微 Sharpe） | W2 的 RL 备选路线 |
| DL 结果对随机种子敏感 | arXiv:2207.07578（不确定性感知专家路由） | 所有 DL/RL 变更必须报 seed 方差 |
| 多门 MoE / 多任务组合 | arXiv:2406.08742（Multi-Gate MoE 动量组合） | W4 的骨架 |
| 经典血统（gate × 回归专家） | Jacobs et al. 1991；Jordan & Jacobs 1994；Quandt 1972 switching regression | W4/W5 只要"softmax 门 + 专家"，专家可以是 GBDT |

---

## 3. 工作项

### 3.1 W1（先做）regime 从硬标签换成 soft 概率

落点：`data/ingest/service.py::build_cn_market_regime` 增加 `regime_model ∈ {rules, jump, hmm}`；
输出新增 `p_bull/p_sideways/p_bear`、`regime_entropy`、`regime_source`；`regime.v2` 版本号。

```toml
[regime]
regime_model = "jump"     # rules | jump | hmm
min_segment_days = 10     # 防止概率在噪声上抖动
output_dir = "output/regime"
```

验收：13 折 OOS 下 `RankIC` 与 Top-20 净不劣化；regime 切换次数与现规则对照（±30% 以内）；
概率的 Brier score / 状态持续性报告落盘。

### 3.2 W2 学习型风控缩放 + 自适应再平衡

1. 把 5 个 `risk_scalers`（`tr_zscore_20`/`tr_volatility_20`/`tr_percentile_252`/`vol_cs_percentile`/`vol_cs_percentile_low`）
   与 `market_deleverage` 的 `threshold/slope/floor` 参数化为
   `scale = clip(a + b·z + c·regime_prob + d·crowding_z, floor, 1)`，用 OOS 目标（扣成本链式净 / CVaR）拟合；
2. `rebalance_stride_days` 改为"成本感知的最优触发"：状态 = (距上次再平衡天数, 目标权重漂移, 当前成本估计, regime 概率)，
   动作 = {继续持有, 再平衡}；先做监督版（预测"再平衡后 H 日净收益增量 > 成本"），RL 作为备选。

验收：扣 25bps 后链式净 ≥ 基线，换手不上升超过 10%，最差折不劣化；`rebalance` 触发次数的分布要合理（避免每周都换）。

### 3.3 W3 出场策略：从阈值规则换成最优停止

落点：`factor_engine/portfolio/exits.py` 增加 `policy ∈ {rules, optimal_stop}`；
状态 = (持仓盈亏、模型分位、波动、regime 概率、距买入天数、通道位置)；
先做**离线最优停止**（Deep Optimal Stopping 的回归版），再考虑 RL。

验收：与 ATR×2.5 基线配对比较 5/20 日收益、最差单日、最大回撤；触发率落在 15%~35%（过低=形同虚设，过高=频繁割肉）。

### 3.4 W4 集成层：从固定权重换到 MoE / 学习型 router

现状：`ensemble_weights = {lightgbm=0.50, transformer=0.50}` + regime 查表权重 = **退化版 MoE**。
按 2608.12251 的做法：门只吃 regime 类变量（`market_tr_z`、breadth、vol、行业动量），
专家保持 LightGBM/Transformer 不变，容量匹配（专家数不变、树数不变），加 load-balancing 正则防专家坍缩。

验收：容量匹配下 RankIC 不降、Top-20 净不劣化；报告门权重的分布（是否坍缩为单一专家）。

### 3.5 W5（暂缓）`startup_gate` 与规则 sleeve 的 ML 化

`startup_gate` 可用学习型资格分类器替换；规则 sleeve 的阈值学习**暂缓**——
P1.18 W3 已证明同类人工事件在 5/20 日没有条件 alpha，投入产出比最低。

### 3.6 明确不做

1. 组合优化器主体换成 RL（凸问题，只会更差更难审计）；
2. 执行 / TCA 模型（4.5 万账户 + `max_participation=0.05`）；
3. 数据层、报告/通知/调度（确定性基础设施）。

---

## 4. 统一验收标准

| 项 | 门槛 |
|---|---|
| 通用 | 13 折 / 169 日 OOS（`config/cn_pipeline_p1_17_extended.toml` 家族）+ 容量匹配 + 固定 seed |
| W1 | RankIC 与 Top-20 净不劣化；regime 切换次数 ±30%；概率校准（Brier）优于规则硬标签 |
| W2 | 扣 25bps 链式净 ≥ 基线；换手 ≤ +10%；最差折不劣化 |
| W3 | 配对差 ≥ +1pp 或最差单日改善 ≥ 1pp；触发率 15%~35% |
| W4 | 容量匹配 RankIC 不降；门权重非坍缩（最大权重 < 0.9） |
| 全部 | 报 seed 方差（≥3 个 seed）；产物走 `output/verification/<topic>_<date>/` 四件套（baseline/modified/diff/rollback/VERIFICATION） |

---

## 5. 阻断项与待确认

1. **账户规模**：4.5 万元、6 个槽位 → RL 的样本效率差，除 W2 的敞口/减仓（低维动作）外优先监督方法。
2. **样本量**：169 个决策日 / 13 折；任何门控类改动都容易过拟合，必须报"训练/验证/测试"三段与 seed 方差。
3. **历史可回算性**：W1 的 soft 概率能否回算到 2025-03 以来全窗口（现有 regime 文件只有少量历史），
   不能回算就必须先在 OOS 折内重算而不是复用文件。
4. **算力与自动化冲突**：`features`+`clean_panel` 每天占约 1 小时；W1~W4 的 A/B 需避开 19:30 的自动生产，或统一用 `--trade-date` 回放隔离。
5. **数据卫生前置**：W4 依赖干净面板（P1_19 §1 已完成），W3/W5 依赖 `exits` 的持仓数据完整性。

---

## 6. 执行顺序与估算

| 顺序 | 工作项 | 估算 | 依赖 |
|---|---|---|---|
| 1 | W1 regime soft 概率（rules / jump 双跑对照） | 1 天 | 无 |
| 2 | W2 学习型缩放（先监督） | 1–2 天 | W1 的概率输出 |
| 3 | W2b 自适应再平衡触发 | 1 天 | W2 |
| 4 | W3 出场最优停止 | 2–3 天 | W1、W2b |
| 5 | W4 学习型 router（MoE） | 2 天 | W1、容量匹配工具 |
| 6 | W5 `startup_gate` ML 化 | 按需 | W4 结论 |

---

## 7. 与其它计划的接口

* **P1.17**：验收口径（13 折 / 169 日、扣 25bps、1/H 修正）直接复用；meta-labeling 的门就是 W4 的简化版。
* **P1.18**：W5（选股级复验）与本计划的 A/B 共用"每折分数 → preselection+pk"回放工具，建议先做 P1.18 W5 再上 W2/W3（否则无法判断替换带来的收益是真 alpha 还是组合口径差异）。
* **P1.19**：数据卫生已完成（指数行 0、面板 5221 只），本计划所有 A/B 都建立在该面板上。
