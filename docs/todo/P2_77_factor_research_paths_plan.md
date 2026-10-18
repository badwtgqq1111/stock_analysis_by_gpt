# P2.77 · 因子挖掘、提纯、组合、迭代的操作系统计划

日期：2026-10-18  
状态：**计划已落盘，尚未实现**。本文把知乎回答当作“路线图”，不把其中的案例数字当作本项目证据。  
资料来源：

- `docs/reference/挖掘因子、研发因子、学习新因子的路径？ - 知乎.pdf`（问题页 + YangJ 回答 + 评论区转述，导出时间 2026-10-18）
- 现有 P2.71 因子池、P2.75 候选排序、`factor_inspector.py`、`factor_fitness.py` 的实测经验
- `docs/todo/结论台账.md` 中已记录的幸存者偏差、PIT、口径差异等边界

目标：把“找因子”从一次性灵感，变成一条**可登记、可证伪、可复算、可退役**的流水线。首个交付不是一批新因子，而是：

1. 统一的假设卡与试验台账；
2. 对价格量、资金流、财务、行业四类数据的分层验证；
3. 与现有因子池/策略池打通的晋级与退役机制；
4. 一套能区分“研究排序”和“准入裁判”的评分口径。

---

## 0. 结论先行

1. **文章最有价值的不是“三重境界”，而是四步骨架：挖掘 → 提纯 → 组合 → 迭代。** 这三步正好对应本仓库缺的三件事：假设登记、统一验证、退役机制。
2. **文章里的收益/夏普/偏差数字全部是二手陈述，不能直接引用。** 例如“小市值因子从 +35% 转 -22%”“低波因子最大回撤 40%”“九坤夏普 0.8→1.5”“Two Sigma 年化超额 6.2%”“幸存者偏差高估 30%”“因子 5-15 个最佳”“周频再平衡最佳”——这些都需要本地复现或删除，不得写进结论台账的“可信结论”。
3. **先做“可验证的因子研究台”，再做“更多因子”。** 没有假设卡和 trial ledger，挖得越多，N 越大，DSR 越难通过，最后只会得到一堆无法解释的最优值。
4. **四类数据分开立项，不混成一个“大因子池”。** 价量、资金流、财务、行业各有不同的 PIT 时点、覆盖率和失效模式；混在一起无法判断增量来自哪里。
5. **研究排序分 ≠ 准入分 ≠ 上线分。** 现有 `config/factor_fitness.json` 只适合给候选排序；准入必须另走 IC/残差/多重检验/成本门禁；上线还要走策略组合与前向影子。

---

## 1. 资料核对：哪些是方法，哪些是未经证实的案例

### 1.1 文章的结构

YangJ 的回答把因子研发分成：

| 层级 | 文章说法 | 可迁移的部分 | 不可直接采信的部分 |
|---|---|---|---|
| 传统因子（青铜） | 价值、动量、质量；有效性周期 10 年/3-5 年 | “先找经济机制，再找数据” | 具体有效期、失效年份和收益幅度没有本地证据 |
| 机器因子（白银） | GBDT 合成因子、非线性、高维 | 用模型做非线性组合/残差 | “夏普 0.8→1.5”“年化超额 6.2%”是案例陈述 |
| 混沌因子（黄金） | 期权曲面、新闻信息熵、动态适应 | 多频/另类数据是长期方向 | Citadel/Renaissance 的具体因子无法验证，不能作为设计依据 |
| 四步法则 | 挖掘、提纯、组合、迭代 | **本计划的主骨架** | “因子 5-15 个”“周频最佳”是待检验参数 |
| 七宗罪 | 未来信息、微观结构盲区、过拟合噪声 | 直接对应本仓库的泄漏与成本测试 | 具体损失金额是案例，不是统计结论 |
| 学习路线 | Kaggle、Qlib、每天记录观察 | 适合作为训练路径 | 不是研究验收标准 |

### 1.2 文章中没有给出、但必须补齐的东西

- **假设卡**：因子为什么应该有效？赚的是谁的钱？在什么市场状态下有效？什么时候会失效？
- **数据时点**：财务是报告期还是公告日？资金流是当日盘后还是次日可得？行业分类是不是用了今天的分类回填历史？
- **试验台账**：一共试了多少个表达式、多少个参数、多少个窗口？N 不登记，DSR 就是假的。
- **成本与容量**：换手多少？A 股佣金、滑点、印花税、涨跌停、停牌后还能不能成交？
- **退役规则**：IC 衰减到多少降权？ crowding 到什么程度停用？谁有权把因子从策略里摘掉？

### 1.3 本计划的证据纪律

1. 文章数字只进入“待核验清单”，不进入“可信结论”。
2. 任何因子结论必须带：数据版本、股票池、样本区间、标签定义、成本口径、试验次数。
3. 负结果与正结果同一张表；不允许只报成功窗口。
4. 同一因子在不同数据源/股票池上的结果分开报，不取最好看的一个。

---

## 2. 因子研发的六段流水线

```text
A 假设 intake
   │  经济机制、方向、期限、失效条件
   ▼
B 数据就绪
   │  PIT、覆盖率、单位、复权、退市/ST
   ▼
C 表达式实现
   │  DSL/Alpha101/AlphaGen、hash、版本
   ▼
D 验证漏斗
   │  T0 因果 → T1 单因子 → T2 稳健 → T3 成本 → T4 多重检验
   ▼
E 组合与策略
   │  硬约束、排序、风险模型、权重上限、换手
   ▼
F 监控与退役
   │  滚动 IC、拥挤、波动、回撤、前向影子
   ▼
   回到 A（新假设，不是旧参数）
```

### A. 假设 intake

每个候选因子先写一张假设卡，未通过 schema 不得进入数据计算。字段见 §3。

### B. 数据就绪

- 每个字段写清单位、频率、复权、可得时点、覆盖区间、缺失规则。
- 财务字段按**公告日**而非报告期对齐；无法确定公告日时，使用保守滞后（例如报告期后 90 天）并标记。
- 资金流字段按**入库/可得日**对齐；同股同日多源冲突要显式选择来源，不能随手去重。
- 行业分类必须 PIT；没有历史行业表时，只能用当前分类做“描述性研究”，并在标题/结论中标注前视风险。
- 退市股、ST、停牌、上市未满 N 日必须可标注；不能只保留今天仍在上市的股票。

### C. 表达式实现

- 优先使用现有 `p2_71_dsl`；AlphaGen/AlphaQCM 表达式用 `alphagen_v1`。
- 每个表达式计算内容 hash；编辑生成新版本，父版本不可变。
- 公式、变量定义、窗口、常量、方向、缺失处理全部落盘。
- 同一经济含义的不同实现（例如 rank 后相减 vs z-score 后相减）视为不同试验，计入 N。

### D. 验证漏斗

见 §6。核心原则：**先证伪，再优化；先单因子，再组合；先成本，再谈年化。**

### E. 组合与策略

- 硬约束（流动性、ST、停牌、涨跌停、行业上限）先于打分。
- 因子分数只在可行域内排序；不允许用线性加权把硬约束“抵消”。
- 组合层必须有风险模型、单票上限、行业上限、换手惩罚。
- 与 CSI500 同窗口比较；报告净 CAGR、Sharpe、最大回撤、换手、成本墙。

### F. 监控与退役

- 滚动 3/6/12 个月 IC、ICIR、正号占比；
- 因子暴露拥挤度（与现有因子/策略的相关、行业集中度）；
- 因子收益波动率、最大回撤；
- 前向影子 60-120 个交易日；
- 触发退役/降权规则，写入台账。

---

## 3. 假设卡模板

建议存为 `config/p2_77_hypotheses.json` 的一个数组元素；脚本校验必填字段。

```json
{
  "hypothesis_id": "H_PV_MOM_12_1",
  "name": "12-1 动量",
  "family": "price_volume",
  "mechanism": "投资者对新信息反应不足，中期趋势延续；跳过最近一个月以避开短期反转。",
  "expected_sign": 1,
  "horizon_days": 20,
  "universe": "CSI500_PIT",
  "formula": "rank(close / delay(close,252) - 1 - (close / delay(close,21) - 1))",
  "expression_language": "p2_71_dsl",
  "inputs": ["close"],
  "neutralization": ["industry", "log_size"],
  "cost_bps_per_turnover": 15.5,
  "rebalance": "monthly",
  "failure_conditions": [
    "RankIC <= 0 in 2 of 3 subperiods",
    "net excess CAGR <= 0 at 15.5bps",
    "DSR < 0.95"
  ],
  "known_risks": ["趋势反转", "行业集中", "样本期依赖"],
  "source": {"title": "internal", "locator": "P2.77 backlog", "url": ""},
  "status": "draft"
}
```

必填字段：

| 字段 | 作用 |
|---|---|
| `hypothesis_id` | 稳定 ID；试验台账外键 |
| `mechanism` | 经济/行为解释；不能写“因为 IC 高” |
| `expected_sign` | 预注册方向；不允许看完结果再改 |
| `horizon_days` | 主评估期限；其他期限只作稳健性 |
| `universe` | 股票池及 PIT 口径 |
| `formula` / `inputs` | 可复算 |
| `neutralization` | 行业/市值/风格中性化清单 |
| `cost_bps_per_turnover` | 成本口径 |
| `rebalance` | 调仓频率 |
| `failure_conditions` | 预先写死的 kill criterion |

---

## 4. 数据源矩阵

| 数据 | 现有覆盖 | PIT 状态 | 可用角色 | 主要风险 |
|---|---|---|---|---|
| 日频 OHLCV/成交额 | 2015 起较完整 | 价格可复权；停牌/涨跌停需 `pit_data` 标注 | 价量因子主数据 | 复权版本、成交量单位、退市股 |
| 分钟/分时 | 覆盖不全，历史短 | 部分 | 只做辅助/执行研究 | 样本短、成本高、不可外推 |
| 资金流（`net_mf_amount` 等） | 约 2024-08 起 | 多源稀疏宽表；需选源 | 资金流因子、短周期 | 历史短、口径不统一、上游 z 含当日 |
| 财务（ROE/PE/PB/PS/股息率等） | PIT 特征层已有，但覆盖不均 | 需按公告日 | 质量/价值/成长 | 覆盖率、公告滞后、修订 |
| 行业分类 | 现有分类 | 历史行业表需确认 | 行业中性、行业轮动 | 用当前分类回填历史=前视 |
| 指数成分/权重 | `pit_data` 可取 | 已有 PIT 权重 | CSI500 基准、成分股 | 成分调整、权重缺失 |
| 停牌/ST/退市 | `pit_data` 部分 | 需补全 | 可交易域 | ST 名称变更缺口 |
| 研报/AI 结论 | 2026-10 起 | **不可历史回填** | 前向增强层 | 未来信息；只能影子 |
| 另类数据 | 基本没有 | — | 长期方向 | 数据采购、对齐、成本 |

### 4.1 数据就绪门禁

一个因子进入 T1 前，必须满足：

- 原始字段覆盖率 ≥ 80%（按决策日 × 股票计算；低于则缩小域并标注）；
- PIT 有效占比 ≥ 90%；
- 标签成熟样本 ≥ 60 个交易日 × 100 只股票；
- 价格/成交量/成交额单位通过手算抽查；
- 复权前后不产生负价格/零价格；
- 退市股和停牌日有明确处理。

不满足时，状态为 `data_blocked`，不得用“先跑个结果看看”绕过。

---

## 5. 候选因子 backlog

以下只是**待检验假设**，不是推荐。每条都要走同一漏斗；不得因为名字好听就跳过验证。

### 5.1 价量族

| ID | 因子 | 公式草图 | 机制 | 主要风险 |
|---|---|---|---|---|
| `PV_MOM_12_1` | 12-1 动量 | `rank(close/delay(close,252)-1 - (close/delay(close,21)-1))` | 中期反应不足 | 趋势反转、行业 beta |
| `PV_REV_5` | 5 日反转 | `-rank(close/delay(close,5)-1)` | 短期过度反应 | 换手高、成本墙 |
| `PV_LOWVOL_60` | 60 日低波 | `-rank(std(ret_1d,60))` | 低波异象/杠杆约束 | 利率与风格切换 |
| `PV_BAB_60` | 60 日 BAB | `-rank(beta_60)`（对 CSI500 回归） | 低 beta 相对高 beta 的补偿 | beta 估计不稳 |
| `PV_AMIHUD_20` | 非流动性 | `rank(mean(abs(ret_1d)/amount,20))` | 流动性溢价 | 容量小、成本高 |
| `PV_TURN_SHOCK` | 换手冲击 | `rank(mean(volume,5)/mean(volume,60))` | 关注度/拥挤 | 事件驱动、反转快 |

### 5.2 资金流族

| ID | 因子 | 公式草图 | 机制 | 主要风险 |
|---|---|---|---|---|
| `MF_STRENGTH` | 主力净流入强度 | 现有 `mf_strength` | 大单需求 | 口径非严格大单 |
| `MF_PERSIST` | 净流入持续性 | 现有 `mf_persistence` | 持续吸筹 | 样本短 |
| `MF_PRICE_DIV` | 资金-价格背离 | `rank(mean(net_mf_amount,5)) - rank(ret_5d)` | 吸筹但未涨 | 可能是出货 |
| `MF_AMOUNT_SURPRISE` | 成交额异常 | `rank(amount/mean(amount,20))` | 关注度突变 | 追高 |

资金流历史从 2024-08 起，**不能**用它做 2015-2023 的回测结论；只能做 2024-08 之后的样本内描述和 60 日前向影子。

### 5.3 财务族

| ID | 因子 | 公式草图 | 机制 | 主要风险 |
|---|---|---|---|---|
| `FND_Q_VALUE` | 质量-价值 | `rank(roe) - rank(pe_ind_pct)` | 好公司 + 不贵 | 行业/风格暴露 |
| `FND_ROE_STAB` | ROE 稳定性 | `rank(mean(roe,8)) - rank(std(roe,8))` | 盈利质量 | 财务修订 |
| `FND_GROWTH` | 净利同比 | `rank(net_profit_yoy)` | 成长 | 基数效应、一次性收益 |
| `FND_ACCRUAL` | 应计质量 | `-rank(accrual_ratio)` | 应计越低越好 | 数据覆盖 |

财务因子必须按公告日对齐；报告期字段不能直接当决策日可得。

### 5.4 行业族

| ID | 因子 | 公式草图 | 机制 | 主要风险 |
|---|---|---|---|---|
| `IND_MOM_60` | 行业 60 日动量 | `rank(industry_return_60)` | 行业趋势 | 行业分类 PIT |
| `IND_REV_5` | 行业 5 日反转 | `-rank(industry_return_5)` | 短期过度反应 | 换手/成本 |
| `IND_DISP` | 行业离散度 | `std(industry_return_20)` | 轮动环境指标 | 不是个股因子，只做 regime |

行业因子只能在行业分类 PIT 可用的区间评估；当前分类回填历史时，结论标为“前视风险，不可晋级”。

---

## 6. 验证漏斗

### T0：数据与因果

- 截断扰动：把 `t` 之后的价格/财务/资金流改掉，`t` 及之前的因子值必须不变；
- 覆盖率、重复键、NaN/Inf、常数序列、零分母；
- 复权、单位、交易日历、停牌/涨跌停；
- 标签成熟度：未来 20 日不足的日期删失，不填 0。

**失败**：任何未来引用、单位错误、覆盖率不足 → `data_blocked`，不进入 T1。

### T1：单因子

指标：

```text
RankIC_h       逐日横截面 Spearman，h ∈ {1,5,20}
ICIR_h         mean(RankIC_h) / std(RankIC_h)
NW_t           Newey-West 调整 t
positive_share  RankIC > 0 的日期占比
decile_spread  第 10 档 − 第 1 档的未来收益
monotonicity   分档收益的单调性
```

门禁（主期限 20 日）：

- `|RankIC| ≥ 0.02`；
- `|ICIR| ≥ 0.5`；
- `|NW_t| ≥ 2.5`；
- 正号占比 ≥ 55%；
- 分档方向与预注册一致。

不满足 → `single_factor_failed`；不得用另一个期限的结果救活主期限。

### T2：稳健性

- 子区间：至少 3 段，方向一致；
- 股票池：CSI500、全 A、行业中性后各跑一次；
- 中性化：无/行业/行业+市值/五风格；
- 参数敏感性：窗口 ±20%、±50%；
- 极值处理：winsorize 1%/99%、2%/98%、不处理；
- 股票数/行业集中度。

门禁：

- 至少 2/3 子区间同向；
- 中性化后 RankIC 保留 ≥ 50%；
- 参数扰动不导致符号反转；
- 行业集中度不超过预设上限。

### T3：成本与换手

- 年换手（双边成交额/平均净值）；
- 成本 10/20/30bps（A 股含佣金、滑点、印花税）；
- 成本墙：净收益归零的成本；
- 容量：按成交额占比/冲击成本估算；
- 涨跌停/停牌不可成交的处理。

门禁：

- 15.5bps 下净超额 > 0（研究门槛）；
- 成本墙 ≥ 20bps；
- 年换手 ≤ 6 倍（超过则单独降权，不直接晋级）。

### T4：多重检验

- 试验台账 N：所有表达式、参数、窗口、股票池、标签变体；
- DSR（Deflated Sharpe Ratio）；
- PBO/CSCV；
- BHY-FDR / HLZ hurdle；
- block bootstrap 置信区间。

门禁：

- DSR ≥ 0.95；
- PBO ≤ 0.50；
- FDR q ≤ 0.10；
- bootstrap 下界不跨 0（或明确记录跨 0）。

### T5：组合与策略

- 与现有因子/策略的相关；
- 组合后 RankIC、净 CAGR、最大回撤、换手；
- 与 CSI500 同窗口；
- 权重上限、行业上限、单票上限；
- walk-forward：训练期定权重，下一段测试；
- 前向影子：60-120 个交易日。

门禁：

- 组合净超额 > 0；
- 最大回撤不超过基准 + 5pp；
- 增量 RankIC > 0（相对不含该因子的组合）；
- 前向影子未出现符号反转。

---

## 7. 组合与策略化

### 7.1 硬约束先于打分

```text
可交易域 = 非 ST
        ∩ 非停牌
        ∩ 上市满 N 日
        ∩ 成交额 ≥ 阈值
        ∩ 非涨跌停不可成交
        ∩ 行业/单票上限
```

因子分数只在可交易域内排序；不允许用加权把不可交易股票“算回来”。

### 7.2 排序分数

建议首版：

```text
score = Σ_k w_k * rank(factor_k)
```

约束：

- `w_k ≥ 0`，`Σ w_k = 1`；
- 单因子权重 ≤ 35%；
- 因子间 `|ρ| > 0.7` 时合并/降权；
- 换手惩罚进入组合优化，不进入单因子 IC。

### 7.3 组合优化

若使用均值-方差/风险模型：

```text
max_w  μ'w - λ w'Σw - γ turnover(w, w_prev)
s.t.   w ≥ 0, Σw ≤ 1, 单票 ≤ cap, 行业 ≤ cap
```

`μ` 只能来自训练期，`Σ` 用滚动窗口；不得用全样本协方差。

### 7.4 研究排序分与准入分分离

| 分数 | 用途 | 是否决定上线 |
|---|---|---|
| `fitness_score`（`config/factor_fitness.json`） | 给候选排期/排序 | 否 |
| `validation_score`（T1-T4） | 准入裁判 | 是 |
| `portfolio_score`（T5） | 组合贡献 | 是 |
| `forward_score` | 前向影子 | 是（上线前） |

现有 fitness 的 note 已经写明“fitness 高 ≠ 可以上线”；本计划把这句话变成代码门禁。

---

## 8. 迭代与退役

### 8.1 监控指标

| 指标 | 频率 | 预警 | 动作 |
|---|---|---|---|
| 滚动 3 月 RankIC | 日/周 | < 0.01 或连续 2 月为负 | 降权 50% |
| 滚动 12 月 ICIR | 月 | < 0.3 | 复查 |
| 因子收益波动 | 日 | > 历史 2 倍标准差 | 暂停新增 |
| 与现有因子最大相关 | 月 | > 0.8 | 合并/降权 |
| 行业集中度 | 月 | 超过上限 | 中性化/降权 |
| 回撤 | 日 | 超过策略预算 | 降杠杆/停用 |
| 前向影子 IC | 日 | 符号反转 | 停用并归档 |

### 8.2 退役规则

满足任一条件，进入 `retired`：

- 连续 6 个月 RankIC < 0；
- 滚动 12 月 ICIR < 0；
- 成本后净贡献连续 3 个月为负；
- 与现有因子相关 > 0.9 且无增量；
- 数据源中断/口径变更，无法复算；
- 前向影子出现符号反转且无法解释。

退役不是删除：保留版本、证据、退役原因；策略解绑生成新版本。

---

## 9. 实验矩阵

### 9.1 第一批（预注册 12 个主假设）

| 轨道 | 主假设 | 样本 | 主期限 | 对照 |
|---|---|---|---:|---|
| 价量 | `PV_MOM_12_1`、`PV_REV_5`、`PV_LOWVOL_60`、`PV_BAB_60` | 2018-2026，CSI500 PIT | 20 日 | 随机因子、现有 6 资金流因子 |
| 资金流 | `MF_STRENGTH`、`MF_PERSIST`、`MF_PRICE_DIV`、`MF_AMOUNT_SURPRISE` | 2024-08 起 | 5/20 日 | 价量基线 |
| 财务 | `FND_Q_VALUE`、`FND_ROE_STAB`、`FND_GROWTH`、`FND_ACCRUAL` | 2018-2026，PIT 公告日 | 20 日 | 价量基线、行业中性 |
| 行业 | `IND_MOM_60`、`IND_REV_5`、`IND_DISP` | 2018-2026，PIT 行业 | 20 日 | 行业等权、CSI500 |

说明：`IND_DISP` 是 regime 指标，不参与个股排序；单独评估“高/低离散度环境下其他因子是否更强”。

### 9.2 对照臂

每个主假设至少四个对照：

1. 同域随机排序；
2. 现有最强单因子；
3. 现有 6 因子等权组合；
4. CSI500 基准。

资金流派另加“价量同公式”对照，区分“资金流增量”与“价量本身”。

### 9.3 样本与切分

- 主样本：2018-01～2026-09；训练/验证/测试按半年滚动；
- 每个窗口训练 3 年、验证 6 个月、测试 6 个月；
- purge = 25 个交易日，embargo = 5 个交易日；
- 资金流轨道单独从 2024-08 起，不做长历史外推；
- 财务轨道按公告日，报告期缺失用保守滞后并标记。

### 9.4 指标表

每个假设输出：

```text
RankIC_1/5/20, ICIR_1/5/20, NW_t, positive_share,
decile_spread, monotonicity,
industry_neutral_ic, size_neutral_ic, residual_ic,
turnover, cost_wall, net_excess_cagr, max_dd,
DSR, PBO, FDR_q,
subperiod_consistency, seed/window_count
```

缺一项即 `incomplete`，不得晋级。

---

## 10. 试验台账

### 10.1 存储

建议 SQLite：`output/factor_research/ledger.sqlite3`，append-only。表：

```sql
trial(
  trial_id TEXT PRIMARY KEY,
  hypothesis_id TEXT NOT NULL,
  factor_id TEXT,
  formula_hash TEXT NOT NULL,
  data_hash TEXT NOT NULL,
  universe TEXT NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  label TEXT NOT NULL,
  horizon INTEGER NOT NULL,
  neutralization TEXT NOT NULL,
  cost_bps REAL NOT NULL,
  rebalance TEXT NOT NULL,
  rank_ic REAL,
  icir REAL,
  nw_t REAL,
  turnover REAL,
  dsr REAL,
  pbo REAL,
  fdr_q REAL,
  verdict TEXT NOT NULL,
  reason TEXT,
  artifact_path TEXT,
  created_at TEXT NOT NULL
)
```

规则：

- 每次运行先写 trial（`status=running`），结束更新结果；
- 失败也写，记录 `reason`；
- `N` 从表里按 `hypothesis_id`/`data_hash` 统计，不手填；
- 不允许覆盖旧 trial；修订用新 trial_id。

### 10.2 报告

`scripts/p2_77_factor_ledger.py` 生成：

- 按轨道/状态/ verdict 的计数；
- 每个假设的最佳/中位/最差窗口；
- N、DSR、PBO 的分布；
- 失败原因 Top 10；
- “看起来好但成本后为负”的清单。

---

## 11. 与现有代码的映射

### 11.1 新增

| 路径 | 职责 |
|---|---|
| `backend/services/factor_research.py` | 假设卡校验、trial ledger、状态机 |
| `backend/services/factor_validation.py` | T1-T4 指标封装；调用现有 IC/DSR/PBO |
| `backend/services/factor_decay.py` | 滚动 IC、拥挤、退役规则 |
| `scripts/p2_77_factor_card.py` | 创建/校验假设卡 |
| `scripts/p2_77_factor_funnel.py` | 单因子跑 T0-T5 |
| `scripts/p2_77_factor_matrix.py` | 批量跑预注册矩阵 |
| `scripts/p2_77_factor_ledger.py` | 台账汇总报告 |
| `config/p2_77_hypotheses.json` | 预注册假设 |
| `config/p2_77_gates.json` | 门禁阈值 |
| `test/test_p2_77_factor_research.py` | schema/状态机/台账 |
| `test/test_p2_77_factor_validation.py` | 手算 IC/DSR/成本 |
| `docs/runbooks/factor-research.md` | 使用说明 |

### 11.2 复用

| 现有资产 | 复用方式 |
|---|---|
| `backend/services/factor_dsl.py` | 表达式校验/评测 |
| `backend/services/alphagen_native.py` | AlphaGen 表达式 |
| `backend/services/factor_pool.py` | 版本、hash、编辑、回滚 |
| `backend/services/pit_data.py` | 停牌/ST/涨跌停/指数权重 |
| `scripts/factor_inspector.py` | 单因子体检、滚动 IC、分位、换手、冗余 |
| `scripts/factor_fitness.py` | 研究排序分（不用于准入） |
| `scripts/p2_71_walk_forward.py` | 窗口/PIT 面板选择 |
| `config/execution_assumptions_cn.json` | 成本/整手/涨跌停 |

### 11.3 前端

因子池页面增加四列/四个区块：

1. **研究阶段**：`draft / data_blocked / single_factor / robust / costed / admitted / retired`；
2. **证据**：RankIC、ICIR、NW t、DSR、PBO、成本墙；
3. **来源**：假设卡、数据版本、trial_id、公式 hash；
4. **退役状态**：最近监控、触发规则、解绑策略。

策略池详情页增加：

- 绑定的因子版本；
- 组合权重；
- 与 CSI500 的净值曲线；
- “未通过验证/仅研究”水印。

---

## 12. 时间表

| 周 | 工作 | 产出 | 退出条件 |
|---|---|---|---|
| W1 | 假设卡 schema、ledger、状态机 | `factor_research.py`、测试 | 能创建/校验/记录 trial |
| W2 | 数据审计、PIT 字段核对 | `data_audit.json` | 四类数据可用域明确 |
| W3 | 价量四假设 T0-T2 | 价量报告 | 至少 1 条进入 T3 |
| W4 | 资金流四假设（短样本） | 资金流报告 | 明确“历史不足”边界 |
| W5 | 财务四假设 | 财务报告 | 公告日对齐通过 |
| W6 | 行业三假设 | 行业报告 | PIT 行业可用或标前视 |
| W7 | T3-T5、DSR/PBO、组合 | 组合报告、净值曲线 | 至少 1 条候选进入影子 |
| W8 | 前端、runbook、负结果 | 页面 + 文档 | 可重开、可回滚 |

若 W2 数据审计发现某轨道覆盖率 < 60%，该轨道直接降级为“前向/描述性”，不占用后续周次。

---

## 13. 交付物

- `output/factor_research/ledger.sqlite3`；
- `output/research/p2_77/reports/REPORT.md`；
- `output/research/p2_77/reports/NEGATIVE.md`；
- `output/research/p2_77/<track>/<hypothesis>/metrics.json`；
- `output/research/p2_77/portfolio/nav.csv`（策略 vs CSI500）；
- 前端因子池“研究阶段/证据”区块；
- `docs/runbooks/factor-research.md`。

### 验收命令

```bash
.venv/bin/python -m pytest test/test_p2_77_factor_research.py test/test_p2_77_factor_validation.py -q
.venv/bin/python scripts/p2_77_factor_card.py --validate config/p2_77_hypotheses.json
.venv/bin/python scripts/p2_77_factor_funnel.py --hypothesis H_PV_MOM_12_1
.venv/bin/python scripts/p2_77_factor_matrix.py --config config/p2_77_hypotheses.json
.venv/bin/python scripts/p2_77_factor_ledger.py --report
```

---

## 14. 风险与边界

| 风险 | 说明 | 对策 |
|---|---|---|
| 文章案例误导 | 二手数字没有样本/成本 | 只做待核验清单 |
| N 爆炸 | 每个窗口/参数都算一次试验 | trial ledger；DSR 用全量 N |
| 数据前视 | 财务/行业/资金流时点错 | PIT 对齐 + 截断测试 |
| 幸存者偏差 | 只用今天上市股票 | 退市股、PIT 成分 |
| 成本低估 | A 股印花税/滑点/涨跌停 | 15.5bps 基线 + 30bps 压力 |
| 单因子英雄 | 只看 IC 最高 | 组合边际贡献 + 冗余 |
| 过度中性化 | 把真实 alpha 也中性掉 | 保留原始/中性两套结果 |
| 短样本外推 | 资金流仅 2024-08 起 | 只做前向/描述 |
| 研究排序绑架准入 | fitness 高就上线 | 分数分离 + 硬门禁 |

---

## 15. 验收清单

- [ ] 每个候选因子都有假设卡，且 `failure_conditions` 在看结果前写入。
- [ ] trial ledger 记录所有尝试，包括失败和放弃。
- [ ] T0 截断测试通过；财务/资金流按可得时点对齐。
- [ ] T1-T4 指标可手算复现，缺项标 `incomplete`。
- [ ] 成本按 `config/execution_assumptions_cn.json`，并做 30bps 压力。
- [ ] DSR/PBO 的 N 来自 ledger，不手填。
- [ ] 组合回测与 CSI500 同窗口、同成本、同再平衡。
- [ ] 退役规则可执行，退役不删除证据。
- [ ] 因子池页面能看到研究阶段和证据，不把 `unmeasured` 显示成“有效”。
- [ ] 负结果报告包含失败窗口，不只报最佳。

---

## 16. 最终判断

文章给出的是一条**方向正确的研发路线**，但它把“案例”讲成了“规律”。本计划只保留它的骨架：

```text
假设 → 数据 → 表达式 → 证伪 → 组合 → 监控 → 退役
```

真正决定成败的不是再挖多少个公式，而是：

1. 能不能在看完结果前写死失败条件；
2. 能不能把每一次尝试计入 N；
3. 能不能在成本后仍然成立；
4. 能不能在样本外不复现时，诚实地说“这条不成立”。

这四件事做完，因子数量反而应该少而可解释；这比“因子池里有一万个公式”更有价值。
