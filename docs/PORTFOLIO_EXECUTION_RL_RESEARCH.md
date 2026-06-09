# 组合层与执行层强化学习扩展调研

> 目标：围绕 Quant67 文中未展开的“组合层用强化学习学习持仓决策”和“执行层用 LSTM/Transformer/强化学习学习订单簿冲击模型”，形成可落地的研究路线。
>
> 当前日期：2026-06-08

## 一句话结论

组合层和执行层都可以用强化学习，但它们不应该替代当前 `LightGBM 信号 -> 组合约束 -> 执行假设` 的主链路。更稳的路线是：组合层先做规则/优化器约束增强，再用 RL 做离线对照；执行层先建设 TCA、滑点、冲击和成交率数据，再用 RL 学“拆单节奏/参与率/限价偏移”。没有高质量交易和订单簿回放环境时，RL 很容易学到回测模拟器的漏洞，而不是市场规律。

## 1. 组合层：从排序信号到持仓权重

### 1.1 当前系统中的组合层角色

本项目当前组合层大致是：

```text
LightGBM ranking_score
-> selection_eligible / overheat / downtrend / watchlist
-> 行业分层和行业预算
-> TopN 组合
-> 权重、HHI、行业归因、回测
```

这条链路的优点是可解释、可调参、便于诊断。弱点是组合策略仍以规则为主：

- 行业权重上限是固定规则；
- 换手成本和冲击成本还不是优化目标；
- 市值桶、流动性桶、拥挤度约束仍需补强；
- 对 regime change 的反应主要靠上游信号和人工调参。

### 1.2 组合层 RL 可以解决什么

组合层 RL 的目标不是“预测股票涨跌”，而是在有信号的前提下学习动态持仓策略：

```text
state_t:
  当前持仓、现金、候选股票信号、风险暴露、行业/市值/流动性、市场状态、交易成本估计

action_t:
  目标权重、调仓比例、买/卖/持有、行业预算调整

reward_t:
  成本后组合收益 - 风险惩罚 - 换手惩罚 - 回撤惩罚 - 约束违约惩罚
```

适合探索的场景：

- 动态换手预算：信号强但成本高时少换；
- 市场 regime 下的仓位缩放；
- 行业预算在 hot/cold 行业间动态调整；
- 个股权重和组合集中度之间的非线性权衡；
- 组合回撤时的风险降档。

不适合一开始就让 RL 做的事：

- 直接从价格学习股票选择；
- 无约束地输出任意权重；
- 在日频少样本上训练深度策略；
- 不考虑交易成本、停牌、涨跌停、流动性的回测。

## 2. 组合层核心论文与框架

### 2.1 Jiang, Xu, Liang：金融组合管理 DRL 框架

《A Deep Reinforcement Learning Framework for the Financial Portfolio Management Problem》提出 model-free RL 组合框架，包含：

- EIIE：对每个资产使用相同评估器结构；
- PVM：Portfolio-Vector Memory，把上一期权重作为状态；
- OSBL：在线随机批训练；
- 显式 reward：直接优化组合财富增长；
- CNN/RNN/LSTM 三种实现。

启发：

- 上一期持仓必须作为状态输入，否则模型不知道换手成本；
- reward 不能只看收益，要扣交易成本；
- 多资产共享网络有助于泛化，但要注意不同股票池和市场制度差异。

### 2.2 FinRL：可复现实验框架

FinRL 提供市场环境、DRL agents 和金融应用三层结构，支持 DQN、DDPG、PPO、SAC、A2C、TD3 等，并内置交易成本、流动性和风险偏好约束。

启发：

- 对本项目来说，FinRL 更适合作为 research sandbox，而不是直接嵌进生产；
- 可以复用其环境设计思想：state/action/reward/transaction cost/backtest split；
- 所有 RL 实验必须和规则组合器、均值方差、TopK baseline 对照。

### 2.3 QlibRL：组合构建和执行可以嵌套

QlibRL 把 RL 用于 portfolio construction 和 order execution，并提出嵌套决策框架：组合层策略和执行层策略可以互相影响。例如组合层给出目标权重，执行层反馈成本和成交难度，组合层再调整目标。

启发：

- 本项目中长期可以把 `expected_transaction_cost` 和 `liquidity_capacity` 从执行/TCA 反馈到组合层；
- 组合目标不应只依赖 alpha score，还应依赖“能否便宜地买到/卖出”；
- 多资产执行约束下，买入前可能必须先卖出，组合和执行不能完全分离。

## 3. 组合层落地路线

### P0：先把规则组合器补完整

在 RL 前先补这些确定性约束：

```text
max_single_stock_weight
max_industry_weight
max_theme_weight
max_turnover
min_liquidity_capacity
market_cap_bucket_budget
overheat_budget
drawdown_de_risk_rule
transaction_cost_model
```

这些规则既是生产保护，也是以后 RL 的 reward / constraint 基线。

### P1：做离线“组合环境”

构建一个不下单的 Gym-like 环境：

```text
Observation:
  date
  current_weights
  candidate ranking_score
  risk_adjusted_score
  industry_l1/l2
  market_cap_bucket
  turnover/liquidity
  overheat/downtrend flags
  realized volatility

Action:
  target_weights 或 delta_weights

Reward:
  next_period_portfolio_return
  - transaction_cost
  - turnover_penalty
  - drawdown_penalty
  - concentration_penalty
  - constraint_violation_penalty
```

### P2：先做轻量 RL / imitation

不要一开始训练复杂深度网络。推荐顺序：

1. 规则组合器作为 expert policy；
2. 行为克隆学习 `ranking_score -> target_weight`；
3. PPO/SAC 只学习少数连续参数，如行业 overlay、仓位缩放、换手预算；
4. 最后再考虑直接输出多资产权重。

### P3：评估门槛

组合 RL 上线前必须赢过：

- 当前 TopN + 行业分层；
- TopKDropout 风格策略；
- 均值方差 / 风险平价 / 最大分散度；
- 固定规则换手预算；
- 成本后收益、最大回撤、换手、行业偏离、成交容量。

特别要看：

- 是否通过提高换手换来虚假收益；
- 是否集中到少数行业/主题；
- 是否在极端行情中雪崩；
- 是否对交易成本参数高度敏感。

## 4. 执行层：从目标持仓到订单流

### 4.1 执行层要解决的问题

执行层面对的是：

```text
给定目标订单 Q，在指定时间窗口内完成成交，
同时最小化滑点、冲击成本、机会成本和信息泄露。
```

常见基线：

- TWAP：按时间均匀拆单；
- VWAP：按预期成交量曲线拆单；
- POV：按市场成交量参与率下单；
- Implementation Shortfall：围绕 arrival price 优化成本；
- Almgren-Chriss：在风险和交易冲击之间做解析权衡。

机器学习和 RL 应该先打败这些基线，而不是直接和“理想成交价”比。

### 4.2 Almgren-Chriss：执行层的传统基准

Almgren-Chriss 把执行成本拆成：

- 永久冲击；
- 临时冲击；
- 价格波动风险；
- 风险厌恶参数下的成本-风险权衡。

它给出从慢速低冲击到快速低风险的一条 efficient frontier。即使未来用 RL，AC 仍应作为：

- baseline；
- reward sanity check；
- pre-trade cost estimator；
- TCA 对照。

### 4.3 订单簿深度学习

DeepLOB 使用 CNN 捕捉订单簿价格/量的空间结构，用 LSTM 捕捉时间依赖，用于预测短期价格方向。Sirignano 的空间神经网络强调利用订单簿深层信息，并建模未来订单簿状态分布。

对执行层启发：

- LOB 模型可以预测短期 mid-price move、spread widening、liquidity fade；
- 执行策略可用这些预测调整 aggressiveness；
- 模型输入应包含 bid/ask 多档价格量、成交、撤单、队列变化、时间段、盘口不平衡；
- 输出不一定是买卖信号，也可以是冲击/滑点/成交概率预测。

### 4.4 执行层 RL

QlibRL 和相关研究把执行视为序贯决策：

```text
state:
  剩余订单量、剩余时间、订单簿、成交量曲线、spread、volatility、短期预测、当前成交进度

action:
  下一时间片成交比例、限价偏移、market/limit 选择、参与率、是否暂停

reward:
  - implementation shortfall
  - market impact
  - opportunity cost
  - unfinished penalty
  + price improvement
```

可先做 single-asset order execution，再扩展 multi-asset。多资产执行更难，因为买入可能受卖出回笼现金约束，多个订单会竞争同一时间段的流动性。

## 5. 执行层落地路线

### P0：先建设 TCA 数据

没有 TCA，执行 RL 没有可信 reward。需要落盘：

```text
order_id
stock_code
side
decision_time
arrival_mid
arrival_bid/ask
target_qty
filled_qty
fill_price
fill_time
market_volume
participation_rate
vwap
close_price
spread
slippage_bps
implementation_shortfall_bps
opportunity_cost_bps
```

如果暂时没有真实订单，可从回测成交假设开始，但要明确这是 simulated TCA。

### P1：先实现规则执行模拟器

至少实现：

- TWAP；
- VWAP；
- POV；
- urgency-based IS；
- AC-style schedule。

并输出成交率、滑点、冲击和未完成惩罚。

### P2：学习成本模型

在 RL 前先做监督学习：

```text
impact_bps = f(order_size/ADV, participation_rate, volatility, spread, time_of_day, liquidity)
fill_probability = f(limit_offset, queue_proxy, spread, imbalance, volatility)
short_horizon_return = f(LOB/minute features)
```

模型可以从线性 / LightGBM 起步，再考虑 LSTM/Transformer。

### P3：RL 只学习执行节奏

初始 action space 要小：

```text
action = {下一分钟成交目标比例: 0%, 5%, 10%, 20%}
或
action = participation_rate in [0, max_pov]
```

不要一开始让 agent 同时决定股票、方向、价格、数量、时间。执行层只接收组合层给定的订单。

### P4：与组合层闭环

执行层输出：

```text
expected_cost_bps
expected_fill_rate
liquidity_capacity
execution_risk_score
```

组合层使用这些字段：

- 降低高成本股票权重；
- 对低成交容量股票设置上限；
- 在调仓时考虑卖出释放现金的执行难度；
- 把交易成本纳入 TopK 选择和换手预算。

## 6. 项目建议：先做“成本感知组合”，再做 RL

短期更有价值的不是直接上 RL，而是补一条成本感知链路：

```text
LightGBM alpha score
-> liquidity/cost model
-> cost_adjusted_score
-> portfolio constraints
-> execution simulator
-> TCA report
```

建议新增字段：

```text
adv_20d
turnover_20d
spread_proxy
volatility_20d
order_size_to_adv
expected_slippage_bps
expected_impact_bps
liquidity_capacity_score
cost_adjusted_ranking_score
```

这比直接训练组合 RL 更快、更稳，也能为后续 RL 提供环境和 reward。

## 7. 风险清单

### 组合 RL 风险

- 学到回测撮合器漏洞；
- 对交易成本假设过度敏感；
- 输出集中持仓；
- 在 regime change 下失效；
- 难解释，难复盘；
- 样本外退化比 LightGBM 更快。

### 执行 RL 风险

- 没有真实订单簿回放时 reward 不可信；
- LOB 数据清洗成本高；
- 市场冲击有反事实问题：如果不这么下单，市场会怎样不可观测；
- 高频模型容易过拟合时段、标的、交易制度；
- 真实执行涉及风控、合规、券商接口和异常处理。

## 8. 分阶段交付物

### 阶段 A：成本感知组合

- `output/tca_simulated_report.csv`
- `output/liquidity_capacity.csv`
- `cost_adjusted_ranking_score`
- README 增加成本后选股流程。

### 阶段 B：组合环境

- `factor_engine/rl/portfolio_env.py`
- `factor_engine/rl/reward.py`
- 规则 policy / expert policy
- PPO/SAC 小样本实验 notebook 或 CLI。

### 阶段 C：执行模拟器

- TWAP/VWAP/POV/IS/AC baseline；
- simulated order book / minute bar replay；
- execution report。

### 阶段 D：执行 RL

- single-asset execution env；
- action = participation / schedule；
- reward = negative implementation shortfall；
- 与 baseline 比较。

## 9. 参考资料

- Quant67 原文：<https://quant67.com/post/quant/12-ml-alpha/12-ml-alpha.html>
- Jiang, Xu, Liang, “A Deep Reinforcement Learning Framework for the Financial Portfolio Management Problem”：<https://arxiv.org/abs/1706.10059>
- FinRL, “A Deep Reinforcement Learning Library for Automated Stock Trading in Quantitative Finance”：<https://arxiv.org/abs/2011.09607>
- Qlib Reinforcement Learning in Quantitative Trading：<https://qlib.readthedocs.io/en/stable/component/rl/toctree.html>
- Qlib RL overview for portfolio construction and order execution：<https://qlib.readthedocs.io/en/v0.9.0/component/rl/overall.html>
- Almgren and Chriss, “Optimal Execution of Portfolio Transactions”：<https://docslib.org/doc/1384720/optimal-execution-of-portfolio-transactions>
- DeepLOB, “Deep Convolutional Neural Networks for Limit Order Books”：<https://arxiv.org/abs/1808.03668>
- Sirignano, “Deep learning for limit order books”：<https://experts.illinois.edu/en/publications/deep-learning-for-limit-order-books>
- Millea, “Deep Reinforcement Learning for Trading: A Critical Survey”：<https://www.mdpi.com/2306-5729/6/11/119>
