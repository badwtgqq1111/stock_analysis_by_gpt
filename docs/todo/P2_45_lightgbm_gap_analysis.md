# P2.45 为什么"LightGBM 选股"看起来跑不过传统策略（与中证500）—— 差距定位与修复清单

编写：2026-10-11。用户问："我的想法是 lightgbm 组和学习权重的模式 能超越 其他几个较好策略才对，
至少 比中证 500 收益曲线有较明显的优势才对。目前 比传统策略反而更差，搞不懂。"

结论先行：**"LightGBM 更差"这个判断目前不成立，因为它根本还没被公平地量过**；真正成立的是
**"模型层的口径、标签、训练样本、以及决策链的改写"四处都在削弱它**。下面全部用仓库里的配置与产物说话。

---

## 1. 先看"不可比"：两条曲线不是同一种东西

| | 传统策略曲线（jq 动态诊断 / box_arm / rot_arm / arm_select） | 模型策略曲线（`cn_ensemble`） |
|---|---|---|
| 口径 | 各自**历史回放**（逐笔或组合），窗口 8~10 年 | 策略池 `tracking`：**真实前瞻名单**（毛收益、未计费用/涨跌停/整手） |
| 观测数 | box_arm 4,880 笔、jq 全窗 2015—2026 | `tracking` 只有 **2 个日期（1 个观测）**；`forward_validation` 16 个决策日；PK 后名单 4 个决策日 |
| 基准 | 各自口径里的中证500 同期 | 中证500 价格指数（同上，但只有 1~2 个点） |

证据：`output/strategy_pool/performance.json` → `cn_ensemble.tracking.{start_date=2026-10-08, end_date=2026-10-09, observations=1}`
（原因是台账/名单归档 2026-10-08 才开始），而 `output/results_cn/forward_validation/summary.json` 里：
* `preselected`（**模型候选池**，16 个决策日 / 2025-09-08~2026-09-24，5 日口径）：期均 **+1.13%**、超额 **+0.43%**、胜率 **62%**、链式 **+18.6%**（基准 +11.2%）
* `notify`（**PK 之后真正要下单的名单**，4 个决策日）：期均 **−3.33%**、超额 **−1.32%**、胜率 **0%**、链式 **−12.8%**（基准 −7.9%）

> 也就是说：**模型挑出来的候选池是跑赢基准的**，但"PK 之后要买的名单"在 4 天里输给了基准。
> 样本小到不能下结论，但方向很明确：**问题更可能出在决策链，而不是模型分数本身**。

## 2. 四处结构性削弱（按影响排序，全部可核对）

### ① 标签用的是**原始前向收益**，不是超额
`config/cn_pipeline.toml`：`[lightgbm] label_mode = "forward_return"`、`label_horizon = 20`。
这正是"模型学到选大盘股/赌行业而不是 alpha"的经典失败模式（用户贴的那份说明第 5 条坑，也是 Gu-Kelly-Xiu (2020) 的第一课：
标签必须是**相对基准/行业/风格中性化后的超额**，否则模型把 beta 当 alpha 学）。
仓库自己也有对照：`P2_40` 的闸门从没量过"模型分数 → 超额"这一层，只量了名单层。

### ② 训练样本只有 **365 天**，且**关闭了早停**
`[pipeline] days = 365`、`[lightgbm] days = 365`、`validation_days = 60`、`embargo_days = 20`、
`n_estimators = 500`、`early_stopping_rounds = 0`、`eval_metric = daily_ic`。
* 20 日前瞻标签 + 365 天窗口 ⇒ 有效**独立**样本约 18 个（其余都是重叠样本，自相关极高）；
* 验证段 60 天 ≈ 3 个独立窗口；`early_stopping_rounds = 0` ⇒ 根本不按验证集挑选轮数，等于"固定 500 棵树硬训"；
* 对比：`scripts/factor_ic_protocol.py` 用的是 `--days 756`，而它算的只是**单因子 IC**。
  **因子筛选的样本比模型训练的样本长一倍**，这件事本身就说明训练窗口设置偏保守。

### ③ 特征并非"全塞"，而是已按准入筛选 —— 但只筛了准入、没筛冗余
`[model_features] admission_mode = "apply"`、`admission_file = output/research/factor_ic_protocol/factor_ic_admitted.json`
⇒ 模型输入是**准入的 100 个因子**（这一步是对的，符合"先单因子检验再送模型"）。
但 `factor_ic_registry.csv` 里还有 **101 条"冗余（同簇有更强代表）"** 没被剔除；
同簇 |ρ|≥0.9 的重复列会把树的分裂点随机化（Gu-Kelly-Xiu 里明确讨论过相关性导致的特征重要性不稳定）。

### ④ 决策链把模型排序**改写了**
`[selection]`：`portfolio_mode = "mean_variance_cost_aware"`、`weighting = "rank_power"`（`alpha_power = 2.0`）、
`startup_gate.orthogonalize_blend = 1.0`（`score_strength = 0.2`）、`risk_control.target_volatility = 0.18`、
`max_weight = 0.6`、`max_holdings = 6`，且 `[selection] model = "ensemble"` 而 `ensemble_weights = {lightgbm: 1.0, transformer: 0.0}`。
* `output/results_cn/pk_guard/latest.json`（2026-10-10）：名单 8 只，**实际持仓只有 1 只**、`total_weight = 0.35`
  ⇒ 65% 现金、单票 35%，与"模型 top-10 等权"相差极远；
* `P2_39` 的历史记录更直接：**6 个 notify 决策日里 5 天把权重给了 rank>20 的票（83%）**，最狠一天 **59.5%** 给了 rank 4017/4055。
* 结论：**用户看到的"LightGBM 策略收益曲线"主要是 PK/组合构造层的表现**，不是模型分数的表现。

## 3. 修复清单（按性价比排序，每条都带验收）

| # | 动作 | 改动位置 | 验收 |
|---|---|---|---|
| A ✅（上游已落地） | **补一条同口径的模型臂曲线**：用 L3 逐笔回放框架跑 `cn_ensemble_selected.csv` 名单（或 `model_scores` 的 top-N 等权），与候选策略**同窗口**、并把中证500画上去 —— **2026-10-11 已落地上游那一半**：`scripts/build_strategy_pool_performance.py` 新增**台账口径**曲线（`selection_ledger.parquet` 的 `preselected` / `notify`，持有到下一个决策日 + 次日开盘建仓 + 分段持有段末链接，中证500同期归一）。实测：**候选池等权 top-10（21 个决策日 / 260 个交易日 / 2025-09-08~2026-10-09）累计 +46.61% vs 中证500 +3.70% ⇒ 超额 +42.90%**（毛收益、未计成本、无 ST/涨跌停过滤，回撤 −16.2%），与影子 `pool_top10_rank`（+38~40%，含成本）量级一致 ⇒ 交叉印证；**PK 后名单（8 个决策日 / 11 个交易日）+5.94% vs −5.28% ⇒ 超额 +11.21%**，但窗口仍太短，**不足以判"PK 是加分还是减分"** ⇒ 继续按决策日累积 | `scripts/build_strategy_pool_performance.py` + `output/strategy_pool/performance.json`（`model_arms` / `research`） | 上游曲线已在策略池页可见（`research` 槽位，含中证500）；下游待 20+ 决策日后再做同窗口配对 |
| B ✅（开关已就绪，A/B 待跑） | **标签改超额**：数据层**本来就支持** `label_mode=excess_return`（`forward_excess_return_<H>d`）；本轮补了不改生产配置的实验开关（见 §2.1） | `config/cn_pipeline.toml` + 训练脚本 | 同一窗口下 OOS rank IC 与 top-N 超额对比；未改善就回退 |
| C ✅（开关已就绪，A/B 待跑） | **训练窗口 365 → 3~5 年 + 开早停**，`early_stopping_rounds > 0`，保留 embargo 20 天（purged） | `[lightgbm] days/validation_days/early_stopping_rounds` | 验证段 IC 曲线 + 训练/验证间隔；与 B 一起做对照 |
| D | **把 PK 拆成"约束层"与"权重层"**，并加影子变体 **`model_topn_equal`（模型 top-N 等权，不做 PK 重排）** | `[selection]` + `run_shadow_variants.py` | `promotion_check.py` 同窗口逐期配对：模型直出 vs 现行 PK，KEEP/PROMOTE/REJECT |
| E | **冗余也筛掉**：把 `factor_ic_registry.csv` 的 101 条"同簇冗余"从模型输入里剔除（准入 ∩ 非冗余） | `[model_features]` + `factor_ic_protocol` 输出 | 输入维度 100 → ~60；看 OOS IC 与稳定性是否改善 |

> A 是**前置**：没有它，B~E 的改动全都无法判断好坏（这正是"搞不懂"的根源 —— 现在没有可比的尺子）。

## 4. 把"单因子 → 线性融合 → LightGBM"对到本仓库（用户贴的那份说明）

| 概念（用户贴的） | 本仓库的对应实现 | 状态 |
|---|---|---|
| ① 单因子：IC、分层收益 | `scripts/factor_ic_protocol.py`（月频秩 IC / ICIR / NW t / BH-FDR / HLZ t≥3 / 覆盖 / 簇冗余）+ 新增的 `output/factor_library/factors.json`（1,431 条都带**功能描述 + 性能评价**） | ✅ 已有 |
| ② 线性融合（IC 加权 / 等权） | `scripts/llm_sleeve_oos_backtest.py` 的三条臂：`base_v1`（25 列 IC 加权）、`llm_new`（LLM 因子集 ICIR 加权）、`combo_50_50` | ✅ 已有（B1 线性组合） |
| ③ 非线性融合（LightGBM） | `run_cn_pipeline.py --stage model_scores`（LightGBM / Transformer / CNN；`[selection].ensemble_weights` 目前 = lightgbm 1.0 / transformer 0.0） | ✅ 已有（B2 非线性组合） |
| "不能一次性全塞" | `[model_features] admission_mode = "apply"` ⇒ 只喂准入的 100 个；**缺**的是"冗余也筛"（见 E） | ⚠️ 部分完成 |
| "y 要做超额" | `label_mode = forward_return` ⇒ **没做**（见 B） | ❌ 待做 |
| "时间切分、不能 shuffle" | 时间切分 + `embargo_days = 20`（purged 口径） | ✅ 已有 |
| "pred 只是预测值，不是信号" | 决策层用规则收口（ST/流动性/涨跌停/整手/单票上限）—— 但也因此改写了排序（见 D） | ⚠️ 过度收口 |

**专业文献（按"从因子到模型"的顺序）**：
1. Kakushadze, *101 Formulaic Alphas* (2016, arXiv:1601.00991) —— Alpha101 家族；
2. 国泰君安《基于短周期价量特征的多因子选股体系：191 个短周期价量因子》（2017）—— GTJA191 家族；
3. Qlib / Alpha158：Yang et al., *Qlib: An AI-oriented Quantitative Investment Platform* (arXiv:2009.11189)；
4. Gu, Kelly, Xiu, *Empirical Asset Pricing via Machine Learning* (RFS 2020) —— 机器学习做截面预测的标准流程（特征/标签/时序切分/正则）；
5. Harvey, Liu, Zhu, *…and the Cross-Section of Expected Returns* (RFS 2016) —— 多重检验门槛（我们用的 t≥3 就来自这里）；
6. McLean & Pontiff, *Does Academic Research Destroy Stock Return Predictability?* (JF 2016) —— 因子衰减；
7. López de Prado, *Advances in Financial Machine Learning* (2018) —— purged CV / embargo / 回测过拟合（DSR、PBO）；
8. Lundberg & Lee, *A Unified Approach to Interpreting Model Predictions* (NeurIPS 2017) —— SHAP，用来看**非线性与交互**（我们的选股归因页已经在用）。

## 2.1 A/B 怎么跑（P2.45-B/C，**不碰生产模型文件**）

```bash
# 基线（现状口径）：365 天窗口 + 原标签 + 无早停 —— 已在 output/models/cn/lightgbm/alpha_zoo_hk（卡：独立窗口 7.8）
# 实验臂（超额标签 + 3~5 年窗口 + 早停）：写到**独立目录**，生产模型文件一个字节都不动
uv run python scripts/run_cn_pipeline.py --stage lightgbm \
    --label-mode excess_return --lightgbm-days 1260 --early-stopping-rounds 50 \
    --model-dir output/models/cn/lightgbm/alpha_zoo_hk_excess
uv run python scripts/build_model_cards.py --print      # 两张卡自动并列（新卡带 previous 对照）
# 只看"改没改好"，不看绝对值：卡上的 每票独立窗口 / valid IC / best_iteration + 后续影子变体配对
```

跑完若新臂更好：按 P2.47-R3 把新目录接进影子（`model_scores` 指向它）→ 过 `promotion_check` 才替换生产 `model.txt`。
