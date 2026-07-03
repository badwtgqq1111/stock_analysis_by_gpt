# LightGBM 量化选股使用方式总结

本文档对比当前项目的 LightGBM 实现与业界标准做法（Qlib / RD-Agent），明确哪些做对了、哪些有问题、如何改进。

---

## 一、当前实现做对的地方

### 1. 使用 LGBMRanker + LambdaRank 目标

```python
model_params = {
    "objective": "lambdarank",
    "metric": "ndcg",
}
```

选股本质是横截面排序问题，使用排序学习目标函数方向正确。虽然 Qlib 用 MSE + CSRankNorm 也能达到类似效果，但 LambdaRank 是排序学习的正统做法。

### 2. 按交易日分组（group by trade_date）

```python
counts = frame.groupby("trade_date", sort=True)["stock_code"].count()
```

LambdaRank 需要 group 定义"同一组内做排序"，按交易日分组是正确的——同一天的股票互相比较。

### 3. 横截面分位数标签

```python
labels = pd.qcut(working[target_column], q=quantile_count, labels=False, duplicates="drop")
```

将连续收益率转为离散排名标签，是 LTR 的标准做法。

### 4. 时间序切分（非随机切分）

```python
train_dates = sorted(labeled_frame["trade_date"].dropna().unique().tolist())
valid_dates = set(train_dates[-valid_date_count:])
```

用时间尾部做验证集，避免了随机切分导致的未来信息泄露。

### 5. 特征自动发现

```python
feature_columns = [col for col in merged.columns if col not in blocked_columns and not col.startswith("target_")]
```

自动排除标签列和元数据列，避免标签泄露到特征中。

### 6. 横截面归一化预测分数

```python
return (valid.rank(pct=True) * 100.0).clip(0.0, 100.0)
```

对模型输出做横截面百分位排名，使不同日期的分数可比。

### 7. 特征重要性输出

提供 gain-based 特征重要性，便于研究人员理解模型决策依据。

---

## 二、当前实现的问题

### 问题 1：标签设计——复合标签污染了学习目标

**当前做法：**
```python
target_score = target_return - drawdown_penalty * weight + breakout_bonus
```

**问题：**
- 把收益率、回撤惩罚、突破奖励三个维度混在一个标签里
- 模型学到的是人为设计的"偏好函数"，不是市场本身的收益率排序规律
- `breakout_return_threshold=0.30`、`breakout_bonus_weight=0.35` 等超参数引入主观偏差
- 回撤惩罚和突破奖励的量纲不同，线性加减缺乏理论依据
- 调参空间爆炸：标签本身就有 4 个超参数，加上模型超参数，过拟合风险极高

**业界做法（Qlib）：**
```python
# 标签 = 纯前向收益率，一行搞定
label = "Ref($close, -2) / Ref($close, -1) - 1"
# 横截面排名归一化
label_processor = CSRankNorm()  # (rank_pct - 0.5) * 3.46
```

**原则：** 标签只反映"未来谁涨得多"，风控（回撤、止损）放在组合层和执行层。

**修复方向：**
- 标签改为纯 `forward_return_N` 的横截面排名
- 回撤惩罚移到选股后的权重分配阶段
- 突破奖励作为独立因子输入特征，而非标签的一部分

---

### 问题 2：没有滚动训练（Rolling）——模型无法适应市场变化

**当前做法：**
- 一次性训练一个模型，用全部历史数据的 80% 训练、20% 验证
- 模型固定不变，无法适应市场风格切换

**问题：**
- 2020 年训练的模型到 2024 年可能完全失效（市场结构变化）
- 单一模型对所有时期一视同仁，无法捕捉近期市场特征
- 没有"样本外"概念——模型对训练期数据的预测是过拟合的

**业界做法（Qlib RollingGen）：**
```yaml
# 每 20 个交易日重训练一次
step: 20
# 训练窗口：expanding（起点固定，终点滚动）
rtype: ROLL_EX
# 防泄露：训练集终点比测试起点提前 horizon+1 天
trunc_days: 21  # label_horizon=20, +1
```

流程：
```
Window 1: train [2008-01 ~ 2014-12] → predict [2015-01 ~ 2015-02]
Window 2: train [2008-01 ~ 2015-02] → predict [2015-02 ~ 2015-03]
Window 3: train [2008-01 ~ 2015-03] → predict [2015-03 ~ 2015-04]
...
```

**修复方向：**
- 实现 walk-forward rolling，每 20 个交易日重训练
- 只用 OOS（样本外）预测做选股
- 训练集终点 = 预测起点 - label_horizon - 1（防标签泄露）

---

### 问题 3：对训练集做预测并用于选股——信息泄露

**当前做法：**
```python
# 对全部数据（含训练集）做预测
predict_frame["model_score_raw"] = model.predict(predict_frame[feature_columns])
```

**问题：**
- 模型在训练集上的预测分数是过拟合的（训练集 NDCG 接近 1.0）
- 用训练集的预测分数做选股，等于用"已知答案"选股
- 这是量化回测中最常见的信息泄露形式

**业界做法：**
- Qlib：严格区分 `DK_L`（训练用）和 `DK_I`（推理用），只用 test 区间的预测做选股
- 回测只看 OOS 区间的表现

**修复方向：**
- `fit_predict` 拆分为 `fit` 和 `predict`
- 只返回验证集/测试集的预测分数
- 或者实现 rolling 后，自然只有 OOS 预测

---

### 问题 4：分位数标签太粗糙（5 档）

**当前做法：**
```python
num_quantiles: int = 5  # 标签只有 0,1,2,3,4 五个值
```

**问题：**
- 5 档标签丢失了大量排序信息
- 同一档内的股票被视为"同等好"，但实际收益率可能差异很大
- 对 LambdaRank 来说，标签粒度越细，排序学习越精确

**业界做法：**
- Qlib 用 CSRankNorm 后做 MSE 回归，标签是连续值（保留全部排序信息）
- 如果用 LambdaRank，标签应至少 20–100 档

**修复方向：**
- 将 `num_quantiles` 提高到 20–50
- 或者直接用百分位排名（0–99）作为标签
- 最佳方案：改用 MSE 回归 + CSRankNorm 标签（Qlib 验证过的方案）

---

### 问题 5：模型容量偏小 + 缺少 Early Stopping

**当前参数：**
```python
learning_rate: 0.05
n_estimators: 120
num_leaves: 31
# 无 early_stopping
# 无 L1/L2 正则化
```

**问题：**
- 31 叶子 × 120 棵树，对 2659 只股票 × 171 特征 × 76 万行数据，模型容量不足
- 没有 early stopping，无法自动确定最优迭代次数
- 缺少正则化，噪声特征会干扰模型

**Qlib CSI500 配置：**
```yaml
learning_rate: 0.1
num_leaves: 250
max_depth: 8
lambda_l1: 205.6999
lambda_l2: 580.9768
colsample_bytree: 0.9
subsample: 0.9
# 代码中: num_boost_round=1000, early_stopping_rounds=50
```

**修复方向：**
```python
model_params = {
    "objective": "lambdarank",
    "metric": "ndcg",
    "learning_rate": 0.1,
    "n_estimators": 1000,
    "num_leaves": 128,       # 或 210–250
    "max_depth": 8,
    "lambda_l1": 200.0,
    "lambda_l2": 500.0,
    "subsample": 0.9,
    "colsample_bytree": 0.9,
    "min_child_samples": 20,
    "early_stopping_rounds": 50,
    "random_state": 42,
}
```

---

### 问题 6：标签未考虑执行延迟

**当前做法：**
```python
# forward_return_20 = 未来20天收益率
# 但没有考虑"信号产生后第二天才能买入"的延迟
```

**问题：**
- 信号在 T 日收盘后产生，最早 T+1 日才能买入
- 如果标签是 `close[T+20] / close[T] - 1`，包含了 T 日到 T+1 日的收益，这部分是无法获取的
- 这会导致回测收益虚高

**Qlib 的做法：**
```python
# T 日产生信号，T+1 买入，T+2 卖出（1日持有期）
label = "Ref($close, -2) / Ref($close, -1) - 1"
# 对于 N 日持有期:
label = "Ref($close, -(N+1)) / Ref($close, -1) - 1"
```

**修复方向：**
```python
# 20日持有期，T+1买入，T+21卖出
forward_return_20 = close[T+21] / close[T+1] - 1
```

---

### 问题 7：缺少样本外评估指标体系

**当前做法：**
- 只输出模型分数和特征重要性
- 没有 IC、ICIR、分组收益等标准评估指标

**业界标准评估指标：**

| 指标 | 含义 | 合格线 |
|---|---|---|
| IC (Information Coefficient) | 预测分数与实际收益的横截面相关系数 | > 0.03 |
| ICIR (IC Information Ratio) | IC 均值 / IC 标准差 | > 0.5 |
| Rank IC | 预测排名与实际排名的 Spearman 相关 | > 0.05 |
| 分组收益单调性 | Top 组 > 第2组 > ... > Bottom 组 | 严格单调 |
| Top 组年化超额 | Top 组相对基准的年化超额收益 | > 10% |
| 最大回撤 | 策略净值最大回撤 | < 20% |
| 夏普比率 | 年化收益 / 年化波动 | > 1.5 |
| 换手率 | 每期调仓比例 | < 50% |

**修复方向：**
- 在 rolling 预测完成后，计算每日 IC/Rank IC
- 按预测分数分 5 组，计算分组收益
- 输出 ICIR、年化收益、最大回撤、夏普

---

### 问题 8：缺少因子去重和筛选

**当前做法：**
- 171 个特征全部喂入模型，没有预筛选
- 高相关因子会导致模型不稳定

**RD-Agent 的做法：**
```python
# 新因子与已有因子的横截面 IC > 0.99 则视为重复，自动剔除
if cross_sectional_ic(new_factor, existing_factor) > 0.99:
    remove(new_factor)
```

**修复方向：**
- 训练前计算因子间相关矩阵，剔除相关系数 > 0.95 的冗余因子
- 或者用 LightGBM 的特征重要性做后验筛选，剔除 importance=0 的因子

---

## 三、业界标准流水线（目标架构）

```text
┌─────────────────────────────────────────────────────────────────┐
│                    量化 LightGBM 选股标准流水线                      │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  1. 标签设计                                                     │
│     label = forward_return(T+1 → T+1+N)                        │
│     label = CSRankNorm(label)  # 横截面排名归一化                  │
│                                                                 │
│  2. 特征工程                                                     │
│     Alpha158 / Alpha360 (自归一化因子)                            │
│     ZScoreNorm(fit_period_only)  # 仅用训练期统计量               │
│     因子去重: 剔除相关系数 > 0.95 的冗余因子                       │
│                                                                 │
│  3. 模型训练                                                     │
│     loss: mse (回归) 或 lambdarank (排序)                        │
│     lr=0.1, num_leaves=210, max_depth=8                         │
│     L1=200, L2=580, early_stopping=50                           │
│     n_estimators=1000 (由 early_stopping 自动截断)               │
│                                                                 │
│  4. 滚动训练 (Rolling)                                           │
│     每 20 个交易日重训练                                          │
│     expanding window (起点固定, 终点滚动)                         │
│     trunc_days = label_horizon + 1 (防标签泄露)                  │
│     只用 OOS 预测做选股                                          │
│                                                                 │
│  5. 组合构建                                                     │
│     TopK + Dropout (top50, 每次换 5 只)                          │
│     交易成本: 买入 0.05%, 卖出 0.15%, 最低 5 元                   │
│     涨跌停限制: 不买涨停, 不卖跌停                                │
│                                                                 │
│  6. 评估体系                                                     │
│     IC / ICIR / Rank IC / Rank ICIR                             │
│     分组收益单调性                                                │
│     年化收益 / 最大回撤 / 夏普 / 信息比率                         │
│     换手率 / 交易成本敏感性                                       │
│                                                                 │
│  7. 风控 (组合层, 不在标签层)                                     │
│     仓位限制 / 行业中性 / 单票上限                                │
│     止损止盈 / 回撤控制                                          │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## 四、改进优先级

| 优先级 | 改进项 | 影响 | 工作量 |
|---|---|---|---|
| P0 | 标签改为纯收益率 + CSRankNorm | 消除标签污染，最根本的改进 | 小 |
| P0 | 只用 OOS 预测做选股 | 消除信息泄露 | 小 |
| P1 | 实现 rolling 训练 | 模型适应市场变化 | 中 |
| P1 | 标签加执行延迟 (T+1 买入) | 消除不可获取收益 | 小 |
| P1 | 加 early stopping + 增大模型容量 | 提升模型质量 | 小 |
| P2 | 加样本外评估指标 (IC/ICIR/分组) | 量化模型有效性 | 中 |
| P2 | 因子去重和筛选 | 提升模型稳定性 | 中 |
| P3 | 分位数标签改为连续排名或增加档数 | 保留更多排序信息 | 小 |

---

## 五、Qlib CSI500 基准配置参考

```yaml
# /qlib/examples/benchmarks/LightGBM/workflow_config_lightgbm_Alpha158_csi500.yaml
model:
    class: LGBModel
    kwargs:
        loss: mse                    # 不是 lambdarank
        colsample_bytree: 0.9
        learning_rate: 0.1
        subsample: 0.9
        lambda_l1: 205.6999
        lambda_l2: 580.9768
        max_depth: 8
        num_leaves: 250
        num_threads: 20

dataset:
    segments:
        train: [2008-01-01, 2014-12-31]   # 7 年训练
        valid: [2015-01-01, 2016-12-31]   # 2 年验证
        test:  [2017-01-01, 2020-08-01]   # 3.5 年测试

strategy:
    class: TopkDropoutStrategy
    kwargs:
        topk: 50          # 持有 50 只
        n_drop: 5         # 每次最多换 5 只 (控制换手)

backtest:
    exchange_kwargs:
        limit_threshold: 0.095    # 涨跌停限制
        deal_price: close
        open_cost: 0.0005         # 买入手续费 0.05%
        close_cost: 0.0015        # 卖出手续费 0.15%
        min_cost: 5               # 最低 5 元
```

---

## 六、RD-Agent 自动因子挖掘参考

RD-Agent 的核心思路：用 LLM 自动化因子研发循环。

```text
循环流程:
1. Propose: LLM 生成因子假设 (如 "尝试价量背离因子")
2. Code:    LLM 生成因子代码 (factor.py)
3. Run:     执行因子计算 + Qlib 回测
4. Evaluate: 对比 SOTA (IC/年化收益/最大回撤)
5. Feedback: LLM 分析结果, 决定是否纳入 SOTA 因子库
6. Repeat:  累积有效因子, 持续改进

关键设计:
- 因子去重: 新因子与已有因子 IC > 0.99 则剔除
- 单调累积: SOTA 因子库只增不减 (除非被更好的替代)
- 评估标准: 年化收益有任何提升即纳入
- Bandit 选择: Thompson Sampling 决定下一步做因子还是做模型
```

---

## 七、一句话总结

当前实现的**框架方向正确**（LambdaRank + 横截面分组 + 时间切分），但在**标签设计、训练方式、信息泄露防护**三个核心环节存在根本性问题。最关键的改进是：**标签回归纯收益率 + 实现 rolling 训练 + 只用 OOS 预测**。这三项改完，模型的实际选股能力会有质的提升。
