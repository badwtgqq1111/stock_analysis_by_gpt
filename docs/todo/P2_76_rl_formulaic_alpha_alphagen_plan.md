# P2.76 · AlphaGen 式强化学习公式因子挖掘计划

日期：2026-10-18  
状态：**计划已落盘，尚未实现**。不把论文数字、参考仓库默认参数或本文的预算估算当作本地业绩。  
资料来源：

- `docs/reference/基于强化学习的因子挖掘.pdf`（知乎文章，2025-02-02；转述 KDD 2023 论文 *Generating Synergistic Formulaic Alpha Collections via Reinforcement Learning*）
- [ICT-FinD-Lab/alphagen](https://github.com/ICT-FinD-Lab/alphagen)，固定审阅 commit `259687e8f316994426416c530a94842a2fe6405e`（2026-06-04）
- 既有 P2.71 AlphaQCM 链路：`docs/todo/P2_71_alphaqcm_factor_mining_research.md`、`docs/runbooks/alphaqcm-research.md`

目标：在现有 AlphaQCM 之外，增加一条**可复现、可对照、可回滚**的 AlphaGen 式 PPO 公式因子挖掘手臂；产出不是“更多公式”，而是经过同一套 PIT、等评价次数、walk-forward、成本和多重检验门禁的候选因子池。

---

## 0. 结论先行

1. **AlphaGen 的核心不是某个因子，而是“用组合池的下游表现做奖励”的搜索器。** 它把公式表达式当作动作序列，用 PPO 学“下一个 token”，奖励来自表达式加入线性因子池后的组合 IC。这个思想值得实现，但不能把论文的 `IC 0.0404 → 0.0725` 直接当作本项目预期。
2. **AlphaGen 与 AlphaQCM 是两条搜索手臂，不是替代关系。** AlphaGen 是 on-policy PPO + 动作掩码 + LSTM；AlphaQCM 是 distributional RL（IQN/QCM）+ 池更新。两者共享表达式语法、PIT 数据和外部评测器，才能在等评价次数下比较。
3. **先做“能否复现搜索过程”，再做“是否产生 alpha”。** 顺序是：合成面板 smoke → 20k 步真实小面板 → 60k 步 → 等评价次数对照 → walk-forward → 成本后组合回测。任何一步失败都归档为负结果，不跳到全量训练。
4. **测试集只在表达式、方向、池权重和超参数冻结后打开一次。** 训练进程不应拿到测试 calculator；这与 P2.71 已经建立的“搜索侧无测试窥视”纪律一致。
5. **首个可交付物是研究工具和证据链，不是生产策略。** 只有通过 P2.76 门禁的表达式才允许以 `unmeasured` 状态导入前端因子池；导入不等于晋级，更不等于自动交易。

---

## 1. 资料核对：文章说了什么，代码实际做了什么

### 1.1 文章中的实验口径

以下数字来自 PDF 的二手转述，**不是本地复跑结果**：

| 项目 | 文章表述 | 使用方式 |
|---|---|---|
| 方法 | 因子生成器 + 因子评估器交替运行；RPN 序列生成；动作掩码 PPO | 作为实现骨架 |
| 股票池 | CSI300、CSI500 成分股 | 本地必须用 PIT 成分，不用今天的名单回看历史 |
| 输入 | 6 个原始特征 | 论文兼容轨道按 OHLCV + VWAP 落地；原文截图段落未逐一列名，以代码为准 |
| 标签 | 未来 20 日收益率 | 本地同时保留“研究标签”和“可执行标签”，不可混用 |
| 切分 | 训练 2009-01-01～2018-12-31；验证 2019；测试 2020-2021 | 只作为论文兼容轨道；本地主实验另做 2021H1～2025H2 walk-forward |
| 对照 | GP、PPO、Qlib 的 XGBoost/LightGBM/MLP | 本地至少要有随机搜索、GP/DSO、AlphaQCM 三类对照 |
| 报告结果 | IC 从 0.0404 提升到 0.0725（约 +80%） | 仅记录为论文声明；不能作为本项目门槛或收益承诺 |

### 1.2 参考仓库的实际默认值

固定 commit 的代码事实：

| 项目 | 代码事实 | 对计划的影响 |
|---|---|---|
| 表达式最大长度 | `MAX_EXPR_LENGTH = 15` | 搜索空间比“无限长公式”可控；仍需防重复与爆炸 |
| 单局最大长度 | `MAX_EPISODE_LENGTH = 256` | 终止条件与无效动作处理必须测试 |
| 算子 | `Abs, Log, Add, Sub, Mul, Div, Greater, Less, Ref, Mean, Sum, Std, Var, Max, Min, Med, Mad, Delta, WMA, EMA, Cov, Corr` | `Greater/Less` 在 AlphaGen 中是逐元素 max/min，不是布尔比较；导出到前端必须写清语义 |
| 时间窗 | `[1, 5, 10, 20, 40]` | 与本地日频标签、流动性过滤一起做敏感性 |
| 常量 | `[-30, -10, -5, -2, -1, -0.5, -0.01, 0.01, 0.5, 1, 2, 5, 10, 30]` | 常量尺度会与复权价格单位冲突；先统一特征尺度 |
| 池容量 | 脚本默认 10/20/50/100，步数 200k/250k/300k/350k | 不是“越大越好”；容量要与样本长度、换手和 DSR 的 N 一起登记 |
| PPO | `MaskablePPO` + `LSTMSharedNet`，`gamma=1.0`，`ent_coef=0.01`，`batch_size=128` | 先复现该配置，再做消融；不要一开始就调参 |
| 奖励 | 默认 `MseAlphaPool` 返回加入后的组合 IC；`MeanStdAlphaPool` 可优化 ICIR | 主复现用组合 IC；生产候选另登记 ICIR 变体，二者不能混报 |
| 无效动作 | 非法表达式奖励 -1；NaN 奖励置 0 | 需要单独统计 `invalid_rate`，不能静默当正常样本 |

### 1.3 需要校正或不能直接照搬的地方

- 仓库 README 提到的 `train_maskable_ppo.py` 在当前固定 commit 的根目录不存在，实际入口是 `scripts/rl.py`；计划以代码树为准，不以 README 文件名推断。
- 参考实现依赖 Qlib/baostock 数据准备；本项目已有 Parquet/PIT 面板，**不引入 Qlib 作为生产依赖**，只实现 `AlphaCalculator` 适配层。
- 参考实现的 `try_new_expr` 在部分分支返回“当前池最优值”，不是严格的“本次增量”。论文兼容奖励与增量奖励要分开命名、分开记录。
- 参考实现的相关性过滤阈值是 0.99，且使用有符号相关；本地导出层要做绝对值 Rank 相关去重，覆盖负相关复制品。
- 论文的 IC 提升来自特定股票池、年份、预算和组合方式；FactorBench 的复跑中 AlphaGen 的 RankIC 甚至是负的（见 P2.71 §2.2）。**两个数字不矛盾，但都不能外推。**

---

## 2. 机制拆解：从表达式树到池奖励

### 2.1 表达式与动作

一个因子表达式是一棵树：

```text
Div(Sub($close, Mean($close,20)), Std($close,20))
```

将其转为逆波兰序列：

```text
$close $close 20 Mean Sub $close 20 Std Div
```

环境按 token 逐步生成：

- **State**：当前已生成的前缀 + 表达式构建器状态 + 可用动作类型。
- **Action**：下一个 token（算子、特征、常量、时间窗、`SEP`）。
- **Transition**：把 token 追加到前缀；构建器更新 arity/类型。
- **Action mask**：根据当前栈和 arity 屏蔽非法动作，保证最终能形成合法表达式。
- **Reward**：只有终止时非零；完整表达式加入池后，用池的目标函数值作为奖励；非法表达式 -1。

### 2.2 池目标函数

设池内因子值为 `f_i`，线性组合权重为 `w`，未来收益为 `y`。单因子 IC 为 `IC_i = corr(f_i, y)`，因子间相关矩阵为 `C`。参考实现的 MSE 型池优化目标可写成：

```text
min_w  w' C w - 2 w' IC + 1 + λ ||w||_1
```

其最优组合 IC 近似为 `w' IC / sqrt(w' C w)`。ICIR 型池则直接优化逐日 IC 的均值/标准差：

```text
max_w  mean_t(IC_t(w)) / std_t(IC_t(w)) - λ ||w||_1
```

本计划预注册两个奖励族：

| 奖励 | 定义 | 用途 | 风险 |
|---|---|---|---|
| `pool_ic` | 加入表达式后的组合 IC | 论文兼容主复现 | 偏向高 IC、可能忽视稳定性 |
| `pool_icir` | 组合 IC 的均值/标准差 | 生产候选主指标 | 对短样本和异常月敏感 |
| `pool_ic_increment` | `O(P_t) - O(P_{t-1})` | 信用分配消融 | 池非平稳，增量可能为负 |

**规则**：主复现只允许一个主奖励；其他奖励只能作为预注册消融，不得在看到测试结果后切换。

### 2.3 为什么不能只奖励单因子 IC

单因子 IC 高的表达式可能与其他因子高度重复。池奖励的价值在于直接衡量“新增因子对现有组合的边际贡献”。但池奖励也带来两个问题：

- 奖励随池状态变化，早期和后期不可直接比较；
- 池更新可能把“对当前池有用、但对未来无用”的因子留下。

因此必须同时记录：单因子 IC、边际 IC、池 IC、池 ICIR、因子间最大相关、表达式复杂度、换手率。只报一个池 IC 不足以判断。

---

## 3. 与 P2.71 AlphaQCM 的分工

| 维度 | AlphaGen 手臂 | AlphaQCM 手臂 | 共享 |
|---|---|---|---|
| 搜索算法 | on-policy PPO + 动作掩码 + LSTM | distributional RL（IQN/QCM）+ 池更新 | 表达式语法、PIT 面板、外部评测器 |
| 探索机制 | 策略熵、动作掩码、PPO 批次 | 分位数网络、均值+标准差探索奖励 | 等评价次数对照 |
| 奖励 | 组合池 IC / ICIR | 池 IC / QCM 目标 | 同一 `AlphaCalculator` 接口 |
| 训练稳定性 | 对超参、批次和奖励尺度敏感 | 对分位数参数、PER、池非平稳敏感 | 固定 seed、固定预算、完整 manifest |
| 计算 | 采样 + 更新，CPU/MPS 可先跑 | 采样 + 分位数更新，通常更重 | 先 toy，再小面板 |
| 主要风险 | 奖励 hacking、表达式重复、PPO 方差 | 分位数估计不稳、QCM 超参敏感 | 测试窥视、幸存者偏差、成本低估 |

**实施顺序**：AlphaGen 作为第二条手臂并行开发，但只在 P2.71 的同一数据契约和同一外部评测器上比较；不修改 AlphaQCM 现有产物的口径。

---

## 4. 目标架构

```text
PIT 面板 / 标签
      │
      ▼
表达式语法与动作掩码 ──► PPO 策略 ──► 候选表达式
      │                                   │
      │                                   ▼
      │                            线性因子池（训练/验证）
      │                                   │
      │                                   ▼
      └────────────── 奖励：池 IC / ICIR / 增量
                                          │
                                          ▼
                              表达式 + 权重 + 元数据冻结
                                          │
                                          ▼
                              独立 evaluator（只读测试集一次）
                                          │
                              ┌───────────┴───────────┐
                              ▼                       ▼
                        研究档案/负结果          因子池导入（unmeasured）
```

### 4.1 新增模块

| 路径 | 职责 | 备注 |
|---|---|---|
| `backend/services/alphagen_rl/__init__.py` | 命名空间 | 不污染现有 `alphagen_native` |
| `backend/services/alphagen_rl/grammar.py` | 表达式 token、arity、动作掩码、规范化 | 可复用 `backend/services/alphagen_expression.py` 的解析器 |
| `backend/services/alphagen_rl/env.py` | Gym 环境、状态/动作/终止/奖励 | 不直接依赖 Qlib |
| `backend/services/alphagen_rl/pool.py` | 线性池、权重优化、去重 | 支持 IC 与 ICIR 两种目标 |
| `backend/services/alphagen_rl/calculator.py` | 本地 `AlphaCalculator` 适配 | 输入 `date × asset` 面板 |
| `backend/services/alphagen_rl/trainer.py` | PPO 训练、checkpoint、resume、日志 | 先 CPU toy，再 MPS |
| `backend/services/alphagen_rl/export.py` | 冻结表达式、指标、lineage、JSON | 与因子池 schema 对齐 |
| `scripts/p2_76_alphagen_train.py` | 单次训练入口 | `--toy/--steps/--seed/--config` |
| `scripts/p2_76_alphagen_walk_forward.py` | 滚动窗口外层评估 | 复用 P2.71 的窗口与 PIT 选择 |
| `scripts/p2_76_alphagen_equal_evals.py` | 等评价次数对照 | 对随机搜索、GP/DSO、AlphaQCM |
| `scripts/p2_76_alphagen_import.py` | 导入因子池 | 只允许 `unmeasured` |
| `config/p2_76_alphagen.json` | 预注册配置 | 预算、种子、算子、奖励、门禁 |
| `test/test_p2_76_alphagen_env.py` | 环境/掩码/奖励测试 | 必须包含无效动作 |
| `test/test_p2_76_alphagen_pool.py` | 池优化/去重测试 | 与参考实现数值对照 |
| `test/test_p2_76_alphagen_no_test_peek.py` | 测试集隔离测试 | 拿掉测试标签后搜索 hash 不变 |
| `docs/runbooks/alphagen-rl.md` | 使用说明 | 训练、恢复、评估、导入、排障 |

### 4.2 输出目录

```text
output/research/p2_76/
  toy/<run_id>/
  real_20k/<run_id>/
  real_60k/<run_id>/
  equal_evals/<run_id>/
  walk_forward/<run_id>/
  portfolio/<run_id>/
  reports/
```

每个 run 至少包含：

```text
config.json           # 完整生效配置
manifest.json         # 数据 hash、代码 commit、seed、设备、依赖
metrics.jsonl         # 每轮 eval 的结构化指标
pool.json             # 表达式、权重、单因子 IC、池 IC
status.json           # running/completed/failed/stopped + 原因
stdout.log / stderr.log
```

---

## 5. 数据契约

### 5.1 面板

长表 schema：

```text
trade_date, stock_code, open, high, low, close, volume, amount, vwap
```

可选扩展列：

```text
net_mf_amount, roe, pe_ttm, pb, ps, dividend_yield, industry_code, in_index, index_weight
```

规则：

1. 价格统一为**同一复权版本**（优先后复权/qfq），并在 manifest 记录复权因子来源与生成时间。
2. `volume` 单位统一（股/手必须二选一），`amount` 单位统一（元），`vwap` 缺失时显式标记“OHLC3 代理”，不能静默替代。
3. 每个特征写清“可得时点”：`open` 为 t 开盘后，`close/high/low/volume/amount/vwap` 为 t 收盘后；财务与资金流按公告/入库时点，不按报告期。
4. 停牌、涨跌停、ST、退市、上市未满 N 日必须可标注；训练标签允许缺失并删失，不补 0。
5. 股票池使用 PIT 成分；若某日成分缺失，明确回退规则（例如最近可用成分 + 标记），不能拿今天的 CSI500 名单回填历史。

### 5.2 标签

保留两类标签，文件名和指标都必须带标签类型：

| 标签 | 定义 | 用途 |
|---|---|---|
| `research_fwd20` | `close[t+20]/close[t]-1` | 论文兼容、因子筛选 |
| `exec_fwd20` | `close[t+20]/open[t+1]-1`，扣除可执行约束 | 组合回测、成本评估 |

训练与验证只使用 `research_fwd20` 做搜索；组合层必须用 `exec_fwd20` 重新评估。两者不可混报。

### 5.3 切分与防泄漏

- 论文兼容轨道：train 2009-2018、valid 2019、test 2020-2021。
- 本地主轨道：2021H1～2025H2 滚动半年测试；每个窗口训练 4 年、验证 6 个月，验证末端按 20 日标签和 5 日 embargo 修剪。
- 任何滚动窗口的测试集不得出现在训练/验证 calculator 中；搜索进程只拿到 train/valid 路径。
- 标签重叠时使用 purge + embargo；默认 purge = 25 个交易日，embargo = 5 个交易日。
- 数据不足、常数序列、覆盖率过低、标签未成熟：显式失败或删失，不填 0。

### 5.4 数据审计

开工前必须产出 `data_contract.json`，至少包含：

- 日期范围、交易日数、股票数、行业数；
- 每个特征覆盖率、缺失率、极端值比例；
- 复权版本、停牌/涨跌停/ST 覆盖率；
- 标签成熟度、每年可用样本数；
- 与 P2.71 面板的字段差异。

---

## 6. 搜索空间与预注册配置

### 6.1 论文兼容空间

```json
{
  "features": ["open", "high", "low", "close", "volume", "vwap"],
  "operators": ["Abs", "Log", "Add", "Sub", "Mul", "Div",
                "Greater", "Less", "Ref", "Mean", "Sum", "Std", "Var",
                "Max", "Min", "Med", "Mad", "Delta", "WMA", "EMA", "Cov", "Corr"],
  "delta_times": [1, 5, 10, 20, 40],
  "constants": [-30, -10, -5, -2, -1, -0.5, -0.01, 0.01, 0.5, 1, 2, 5, 10, 30],
  "max_expr_length": 15,
  "max_episode_length": 256,
  "pool_capacity": 20,
  "reward": "pool_ic",
  "ppo": {"gamma": 1.0, "ent_coef": 0.01, "batch_size": 128, "n_layers": 2, "d_model": 128}
}
```

### 6.2 本地扩展空间

扩展必须作为独立 arm，不得与论文兼容 arm 混在一起报结果：

| Arm | 特征 | 目的 | 预注册 |
|---|---|---|---|
| `price_volume` | OHLCV + VWAP | 论文兼容 | 主复现 |
| `moneyflow` | 价量 + `net_mf_amount` 及其滚动特征 | 检验资金流增量 | 从 2024-08 起，样本短，只做描述/前向 |
| `fundamental` | 价量 + PIT ROE/PE/PB/PS/股息率 | 检验慢因子增量 | 2015 起，季度更新，注意公告滞后 |
| `industry` | 价量 + 行业代码/行业收益 | 检验行业轮动增量 | 行业分类必须 PIT |

### 6.3 去重

- 表达式规范化：统一空格、常量格式、算子别名；同一表达式只保留一个 hash。
- 相关性去重：池内两两计算**绝对值 Rank 相关**；`|ρ| > 0.95` 视为重复，保留验证 IC 更高者。
- 负相关复制品：方向由池权重吸收，不重复计入“新因子”。
- 复杂度惩罚：节点数、最大深度、窗口数量进入元数据；是否进入奖励由消融决定。

---

## 7. 训练协议

### 7.1 阶段

| 阶段 | 数据 | 步数 | 种子 | 目的 | 通过条件 |
|---|---|---:|---:|---|---|
| S0 合成 smoke | 随机游走面板 | 2,000 | 1 | 环境/掩码/奖励/导出通路 | 无异常，表达式合法率 ≥ 95%，导出可重算 |
| S1 真实小面板 | CSI500 抽样 100 只 | 20,000 | 3 | 搜索是否能填池 | 池非空，合法率 ≥ 90% |
| S2 中预算 | CSI500 PIT | 60,000 | 3 | 稳定性和对照 | 验证池 IC > 0，且优于随机同预算 |
| S3 论文预算 | CSI500 PIT | 200,000 | 3 | 主复现 | 与 S2 同向，无测试窥视 |
| S4 扩展预算 | 视资源 | 2,000,000 | 1-3 | 上限探测 | 只作为探索，不直接晋级 |

步数是 PPO 的 environment steps，不是“表达式评价次数”。表达式评价次数单独记录，等评价对照以它为准。

### 7.2 早停与恢复

- 早停只看验证池 IC/ICIR，连续 10 次 eval 无提升则停。
- checkpoint 必须包含：策略网络、优化器、RNG、当前池、表达式历史、评价计数。
- 只保存权重不算可恢复；恢复后必须验证动作、池、loss 一致。
- 中断 run 标记 `stopped`，不得当作完成实验。

### 7.3 资源

先做 CPU toy，再在 Apple Silicon 上测 MPS；不要在未测通前承诺工时。每个 run 记录：

- steps/s、表达式评价/s、峰值 RSS、设备、线程数；
- 单次 eval 耗时、池优化耗时；
- 预计 20k/60k/200k 步的墙钟时间。

---

## 8. 实验矩阵

| ID | 实验 | 对照 | 主指标 | 预注册门禁 |
|---|---|---|---|---|
| E0 | 合成面板通路 | 无 | 合法率、导出重算一致 | 合法率 ≥ 95%；重算误差 < 1e-10 |
| E1 | 20k 真实小面板 | 随机搜索同预算 | 验证池 IC、池非空率 | 池非空；IC > 0 |
| E2 | 60k 真实面板 | 20k、随机、GP | 验证池 IC/ICIR | 优于随机同评价次数 |
| E3 | 200k 主复现 | 60k、AlphaQCM | 验证池 IC/ICIR | 不劣于 60k；无测试窥视 |
| E4 | 等评价次数 | 随机、GP/DSO、AlphaQCM | 测试池 IC/RankIC | 至少优于随机；与 AlphaQCM 比不要求必胜 |
| E5 | walk-forward | 单窗口 | 各窗口 IC、正窗口比例 | ≥ 60% 窗口同向，测试不反转 |
| E6 | 消融 | 主配置 | IC/ICIR/换手 | 只允许一个主配置晋级 |
| E7 | 成本后组合 | CSI500 | 净 CAGR、回撤、换手 | 净超额 > 0，回撤不显著恶化 |
| E8 | 前向影子 | 无 | 60 日增量 IC | 只记录，不调权重 |

### 8.1 E6 消融清单

| 消融 | 变体 | 想回答的问题 |
|---|---|---|
| 动作掩码 | 有/无 | 掩码是否显著提高合法率与样本效率 |
| 策略网络 | LSTM/MLP | 序列记忆是否必要 |
| 奖励 | pool IC / pool ICIR / increment | 哪种奖励更稳 |
| 池容量 | 10 / 20 / 50 | 容量与 DSR 的 N 如何权衡 |
| 表达式长度 | 10 / 15 | 复杂度是否带来真实增量 |
| 去重阈值 | 0.90 / 0.95 / 0.99 | 去重是否改变池多样性 |
| 复杂度惩罚 | 无/有 | 是否抑制公式爆炸 |

---

## 9. 评估与门禁

### 9.1 搜索侧

| 指标 | 门槛 | 说明 |
|---|---:|---|
| 合法表达式率 | ≥ 90% | 低于此先修环境/掩码，不调模型 |
| 池非空率 | 100% 的完成 run | 空池记为失败，不重试到成功 |
| 重复率 | ≤ 30% | 去重后新表达式占比 |
| 评价次数 | 完整记录 | 等评价对照的唯一预算口径 |

### 9.2 信号侧

| 指标 | 候选门槛 | 说明 |
|---|---:|---|
| 验证池 IC | > 0.02 | 低于此不做组合回测 |
| 验证池 ICIR | > 0.5 | 与 IC 同时看 |
| 正 IC 月份占比 | ≥ 60% | 防止单月驱动 |
| 五风格残差 IC | > 0.01 | beta/波动/动量/反转/流动性中性后 |
| 测试 IC 保留率 | ≥ 50% | 测试 IC / 验证 IC |
| 符号反转 | 不允许 | 测试与验证方向相反直接 NO-GO |

### 9.3 组合侧

| 指标 | 门槛 | 说明 |
|---|---:|---|
| 净 CAGR vs CSI500 | > 0 | 成本后，不是毛收益 |
| 最大回撤 | ≤ CSI500 + 5pp | 超过则降杠杆或放弃 |
| 年换手 | ≤ 6 倍 | 按双边成交额/平均净值 |
| DSR | ≥ 0.95 | N 为全部尝试次数 |
| PBO | ≤ 0.50 | CSCV，按 Sharpe 或波动归一 |
| 成本墙 | 记录 | 10/20/30bps 下净值，找盈亏平衡成本 |

### 9.4 Go / No-Go

**GO（只进入前向影子，不直接生产）**：

- E4 等评价次数下优于随机搜索；
- E5 至少 60% 窗口同向，且无测试符号反转；
- E7 成本后净超额 > 0，DSR ≥ 0.95，PBO ≤ 0.50。

**NO-GO（归档）**：

- 合法率长期 < 90%，或池长期为空；
- 验证强、测试衰减超过 50%，或符号反转；
- 只有毛收益、成本后为负；
- 只有单一窗口/单一 seed 有效。

负结果必须写入 `output/research/p2_76/reports/NEGATIVE.md`，记录 N、预算、数据版本和失败原因；不得删除 run。

---

## 10. 与前端因子池、策略池的打通

### 10.1 导入契约

```json
{
  "factor_id": "ag_<hash8>",
  "name": "AlphaGen pool factor <n>",
  "formula": "Div(Sub($close,Mean($close,20)),Std($close,20))",
  "expression_language": "alphagen_v1",
  "description": "由 P2.76 run <run_id> 生成；训练/验证窗口、池权重、IC 见 metrics。",
  "sources": [{"title": "P2.76 run", "locator": "<run_id>/pool.json", "url": ""}],
  "change_note": "import from frozen P2.76 artifact",
  "evaluation_status": "unmeasured",
  "lineage": {
    "run_id": "...",
    "data_hash": "...",
    "code_commit": "...",
    "seed": 0,
    "reward": "pool_ic",
    "pool_weight": 0.123,
    "valid_ic": 0.031,
    "test_ic": 0.018
  }
}
```

规则：

1. 只导入冻结产物；训练中的中间表达式不导入。
2. `FactorStore.save` 使用 `expression_language='alphagen_v1'`；校验失败直接拒绝。
3. 新因子一律 `unmeasured`，不继承 run 的 IC，不自动绑定策略。
4. 前端展示来源、run、数据版本、验证/测试指标和“未复评”标记。
5. 二次编辑生成新版本，父版本不可变；编辑后状态回到 `unmeasured`。

### 10.2 API

新增：

```text
POST /api/factor-pool/alphagen/import
GET  /api/factor-pool/alphagen/run/{run_id}
GET  /api/factor-pool/alphagen/performance?run_id=...
```

导入接口只允许读取 `output/research/p2_76/` 下的 run，路径解析与 P2.71 的 AlphaQCM 导入保持一致，防止任意文件读取。

### 10.3 策略池

只有通过 E7 且进入前向影子的因子，才允许在 `config/strategies.json` 登记候选策略；登记时固定：

- `factor_id` + `version`；
- 组合权重与调仓频率；
- 成本口径；
- 基准（CSI500）；
- 证据路径。

未通过验证的因子只出现在因子池研究页，不出现在生产策略列表。

---

## 11. 风险与对策

| 风险 | 表现 | 对策 |
|---|---|---|
| 奖励 hacking | 池 IC 高但换手/回撤恶化 | 同时记录换手、回撤、复杂度；组合侧门禁 |
| 表达式爆炸 | 长公式、重复公式 | 最大长度、去重、复杂度惩罚、规范化 hash |
| 过拟合 | 验证好、测试坏 | 测试隔离、walk-forward、DSR/PBO、N 全量登记 |
| 数据泄漏 | 财务/资金流用未来值 | PIT 可得时点、负向测试、截断扰动测试 |
| 幸存者偏差 | 只用今天成分 | PIT 成分、退市股、ST 标注 |
| 成本低估 | 毛利可做、净利不可做 | 10/20/30bps 成本墙、A 股印花税/滑点 |
| 计算不足 | 20k 步池为空 | 先 toy、再小面板；预算与结论一起报告 |
| 包冲突 | 参考仓库 `alphagen` 覆盖本地模块 | 独立子进程/独立环境，不直接 `pip install` 到主环境 |
| 池非平稳 | 权重跳动、因子失效 | 滚动重估、权重上限、换手惩罚、衰减监控 |
| 解释性下降 | 公式不可读 | 导出简化表达式、经济解释卡、人工复核 |

---

## 12. 时间表

| 周 | 工作 | 产出 | 退出条件 |
|---|---|---|---|
| W1 | 表达式/环境/池适配；合成 smoke | `test_p2_76_*`、toy run | E0 通过 |
| W2 | 真实小面板 20k；日志/恢复 | S1 三个 seed | 池非空、合法率达标 |
| W3 | 60k 主跑；随机/GP 对照 | S2、E2 报告 | 优于随机同评价次数 |
| W4 | 200k 主复现；等评价对照 | S3、E4 报告 | 无测试窥视；结果可复算 |
| W5 | walk-forward + 成本组合 | E5/E7 报告、净值曲线 | 净超额 > 0 或明确 NO-GO |
| W6 | 因子池导入、前端、runbook | 导入产物、页面、文档 | 可重开、可回滚 |

若 W2 结束仍无法稳定填池，停止加预算，先修环境/奖励/数据；不进入 W3。

---

## 13. 交付物

### 13.1 代码与配置

- `backend/services/alphagen_rl/` 全套模块；
- `scripts/p2_76_alphagen_train.py`；
- `scripts/p2_76_alphagen_walk_forward.py`；
- `scripts/p2_76_alphagen_equal_evals.py`；
- `scripts/p2_76_alphagen_import.py`；
- `config/p2_76_alphagen.json`；
- 测试文件与 `docs/runbooks/alphagen-rl.md`。

### 13.2 研究产物

- `output/research/p2_76/reports/REPORT.md`：方法、数据、预算、结果、失败窗口；
- `output/research/p2_76/reports/NEGATIVE.md`：负结果；
- `output/research/p2_76/equal_evals/`：等评价对照；
- `output/research/p2_76/walk_forward/`：滚动窗口；
- `output/research/p2_76/portfolio/nav.csv`：策略净值 + CSI500；
- `output/research/p2_76/portfolio/metrics.json`：CAGR、Sharpe、回撤、换手、成本。

### 13.3 验收命令

```bash
.venv/bin/python -m pytest test/test_p2_76_alphagen_env.py test/test_p2_76_alphagen_pool.py -q
.venv/bin/python scripts/p2_76_alphagen_train.py --config config/p2_76_alphagen.json --toy --steps 2000
.venv/bin/python scripts/p2_76_alphagen_train.py --config config/p2_76_alphagen.json --steps 20000 --seed 0
.venv/bin/python scripts/p2_76_alphagen_equal_evals.py --config config/p2_76_alphagen.json
.venv/bin/python scripts/p2_76_alphagen_walk_forward.py --config config/p2_76_alphagen.json --first-year 2021 --last-year 2025
```

---

## 14. 验收清单

- [ ] 表达式语法与 AlphaGen 固定 commit 的算子/窗口/长度一致，差异有清单。
- [ ] 动作掩码有单元测试；非法动作不会进入池。
- [ ] 奖励函数有手算测试；IC/ICIR/增量三种奖励分开命名。
- [ ] 训练进程拿不到测试标签；移除测试标签后搜索 hash 不变。
- [ ] 20k/60k/200k 的预算、评价次数、墙钟时间、RSS 全部记录。
- [ ] 等评价次数对照包含随机搜索、GP/DSO、AlphaQCM。
- [ ] walk-forward 窗口严格 PIT，无验证→测试重叠。
- [ ] 成本后净值与 CSI500 同图；报告 CAGR、Sharpe、最大回撤、换手、成本敏感性。
- [ ] DSR/PBO 的 N 为全部尝试次数，不只统计“成功 run”。
- [ ] 负结果归档；没有“只报成功窗口”。
- [ ] 导入因子池的因子为 `unmeasured`，可编辑、可回滚、有来源。
- [ ] runbook 能从空环境复现一次 toy run 和一次 20k run。

---

## 15. 与现有计划的接口

| 现有资产 | 复用方式 | 不做什么 |
|---|---|---|
| `backend/services/alphagen_native.py` | 表达式校验、pandas 评测、前端预览 | 不在其中塞训练逻辑 |
| `backend/services/alphaqcm_adapter.py` | 数据加载、预算、artifact 冻结思路 | 不共享进程内的全局状态 |
| `backend/services/pit_data.py` | 停牌/ST/涨跌停/指数权重标注 | 不把标注当作完整 PIT 证明 |
| `scripts/p2_71_walk_forward.py` | 窗口定义、面板选择、padding 规则 | 不复制粘贴出第二套窗口口径 |
| `backend/services/factor_pool.py` | 版本、hash、编辑、回滚 | 不绕过 `checked()` 直接写 SQLite |
| `config/execution_assumptions_cn.json` | 成本、整手、涨跌停、停牌 | 不在脚本里硬编码成本 |

最终判断：**AlphaGen 值得做，但它的价值在于把“组合协同”变成可搜索的目标，而不是保证更高年化。** 如果 E4/E5/E7 不能同时通过，正确结论是“这条搜索手臂在本数据上没有增量”，并保留工具与负结果，不用调参把它救成正面结论。
