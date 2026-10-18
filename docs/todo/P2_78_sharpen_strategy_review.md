# P2.78 · Sharpen 策略与验证体系审阅：可借鉴什么、不能相信什么

日期：2026-10-18  
状态：**审阅 + 落地计划，尚未实现**。所有收益数字均为仓库作者的历史模拟/论文口径，不是本项目复跑，也不是未来承诺。  
审阅对象：[bigcan/sharpen](https://github.com/bigcan/sharpen)，固定 commit `468a1509d2dc424bb29db6aea9781c68d67550c5`（2026-10-10，Apache-2.0）。  
辅助材料：仓库 `README.md`、`NEGATIVE_RESULTS.md`、`docs/METHODOLOGY.md`、`configs/tailwind_v1.gates.yaml`、`scripts/research/xsec_momentum_falsification.py`、`results/portfolio_frontier/frontier.json`。

目标：回答三个问题：

1. 这个仓库里有没有“年化收益率高且可复现”的策略？
2. 哪些策略思想可以迁移到 A 股/中证500？
3. 哪些验证机制值得直接移植到本仓库，优先级如何？

---

## 0. 结论先行

1. **没有可直接复制的高年化策略。** 仓库自己承认的唯一“幸存”研究候选是 18 只 ETF 的跨资产时间序列动量（TSMOM）：2006-2026、月度调仓、2bps 成本下净 Sharpe 约 **0.60**；在 10% 波动目标下年化约 **6.0%**、最大回撤约 **-13.9%**。这不是“高年化”，而是“低波动、可解释、成本后仍为正”。
2. **仓库最有价值的不是策略，而是“证伪机器”。** 预注册、七层验证漏斗、DSR/PBO/FDR、block bootstrap、timing null、power analysis、成本墙、lockbox 前向孵化、负结果台账——这些比任何单一因子都更值得移植。
3. **高年化数字基本都伴随杠杆、幸存者偏差、数据污染或事后选池。** 例如 FinRL-X 在 0bps 下 CAGR 22.7%，但 0/35 个 2017 年可选的随机列表能在 10bps 下跑赢 QQQ；VIX 期限结构毛 Sharpe 0.734，但最大回撤 -50.1%；期权 VRP 的 0.61 锚点是数据污染，清洗后为负。
4. **Sharpen 的“幸存者”也没有过自己的门禁。** 组合 mom+BAB 的诚实 DSR 为 **0.896 < 0.95**；PBO 按 Sharpe 重算为 **0.5902 > 0.50**；X1 复核 DSR 在 N=77 时 **0.881**，结论是 `BLOCK`。所以正确读法是“有一个值得继续研究的方向”，不是“有一个可上线策略”。
5. **对 A 股的正确借用顺序**：先移植统计与证伪工具，再测行业 TSMOM、低波/BAB、12-1 动量、短期反转四条 sleeve；不要先上 RL 择时/执行。

---

## 1. 审阅范围与方法

### 1.1 仓库事实

| 项目 | 事实 |
|---|---|
| 定位 | “agentic-AI, full-featured quant strategy builder”：从假设到验证、训练、paper/live |
| 规模 | 约 45MB；Python；约 3,300 个默认测试（作者声明） |
| 数据 | **不附带市场数据**；免费数据连接器 + 用户自备数据 |
| 策略记录 | 8 个家族、77 条预注册问题，逐条给 verdict |
| 许可证 | Apache-2.0；代码可复用，名称/商标除外 |
| 重要声明 | 所有业绩来自历史模拟或 paper run；不是投资建议 |

### 1.2 本审阅怎么读数字

- 只把仓库明确写了样本、股票池、成本、再平衡和 verdict 的数字放进表；
- 不把 README 的宣传语当结果；
- 不把“毛 Sharpe”与“净 Sharpe”混在一起；
- 不把“论文/作者声明”与“本项目复跑”混在一起；
- 对没有数据/无法复现的条目，标“不可复核”，不纳入策略候选。

---

## 2. 策略家族与总体结果

`NEGATIVE_RESULTS.md` 的汇总：

| 家族 | 关闭数量 | 主要死因 |
|---|---:|---|
| 方向性 RL 及其补救 | 14 | 样本外无 edge；补救无效 |
| 期权与波动率溢价 | 7 | 数据污染、幸运子样本、尾部风险 |
| 套利/ Carry/ 做市 | 7 | 真实结构存在，但成本/执行吃不掉 |
| 股票横截面与因子 | 13 | 幸存者偏差、行业 beta、事后信息、被套利到成本 |
| 动量书上的 sleeve/overlay | 11 | 稀释、相关、单一子区间收益 |
| 散户技术/日内 | 10 | 成本杀死；胜率不是 edge |
| 免费数据预注册探针 | 11 | 伪影、通不过 deflation、不可收割 |
| 自动挖掘 substrate | 4 | 在成本强制的时间尺度上 power 不足 |
| **合计** | **77** | — |

这不是“77 个失败策略”的清单，而是“77 个问题各自被什么证据关闭”的清单。它的价值在于：**每个 NO-GO 都带 kill criterion 和证据路径**。

---

## 3. 收益数字核对

### 3.1 唯一幸存研究方向：跨资产 TSMOM

仓库的 `xsec_momentum_falsification.py` 固定参数：

| 项目 | 值 |
|---|---|
| 样本 | 2006-01-01～2026-06-01 |
| 标的 | 18 只 ETF：equity 5、rates 3、commodity 5、fx 5 |
| 信号 | 63/126/252 日趋势的符号平均，跳过最近 5 日 |
| 波动目标 | 单资产 10%，组合 10% |
| 杠杆上限 | 单资产 2.0x |
| 再平衡 | 月度 |
| 成本 | frictionless / 2bps / 10bps |
| 结果 | 2bps 净 Sharpe **0.601**；4/4 资产类别净正；32 只未参与开发的 ETF 上 Sharpe **0.389** |

组合 mom+BAB 的诚实口径：

| 指标 | 值 | 门槛 | 结论 |
|---|---:|---:|---|
| 组合 Sharpe（curated） | 0.732 | — | 描述 |
| 组合 Sharpe（honest） | 0.579 | — | 描述 |
| DSR（N=24，honest） | 0.896 | ≥0.95 | **FAIL** |
| DSR（N=77，X1 复核） | 0.881 | ≥0.95 | **BLOCK** |
| PBO（Sharpe 口径） | 0.5902 | ≤0.50 | **FAIL** |
| 32 只宽基组合 DSR（N=23） | 0.920 | ≥0.95 | **FAIL** |

### 3.2 把 Sharpe 翻译成年化

Sharpen 的 `results/portfolio_frontier/frontier.json` 中的诚实组合（mom+rates，Sharpe 0.601）。注意：mom+BAB 的诚实 Sharpe 是 0.579，幅度略低；这里用 frontier 的 mom+rates 网格展示“Sharpe 固定时，年化由波动/杠杆决定”：

| 波动目标 | 年化收益 | 最大回撤 | 杠杆（相对 10% 波动） |
|---:|---:|---:|---:|
| 7.5% | 4.5% | -10.6% | 0.75x |
| 10% | **6.0%** | **-13.9%** | 1.0x |
| 15% | 9.0% | -20.3% | 1.5x |
| 20% | 12.0% | -26.4% | 2.0x |
| 30% | 18.0% | -38.1% | 3.0x |
| 50% | 30.1% | -61.1% | 5.0x |
| 100% | 60.1% | -97.3% | 10.0x |

仓库自己算的“100% 年化”：

```text
honest combined Sharpe 0.601
需要约 166% 年化波动（16.6x 杠杆）才能到 100% 年化
对应最大回撤约 -100%（ruin）
```

**结论：高年化不是策略性质，是杠杆选择。** 在 Sharpe 0.6 的水平上，想要 100% 年化只能靠极端杠杆，而回撤会先让人出局。

### 3.3 其他“高收益”案例

| 案例 | 表面数字 | 真实口径/问题 | 能否作为 A 股策略模板 |
|---|---|---|---|
| FinRL-X 自适应轮动 | 0bps 下 4.91x、CAGR 22.7%；10bps 下 CAGR 15.3% | 固定 2017 年成长股列表，事后选池；0/35 个当时可选的随机列表在 10bps 下跑赢 QQQ | **不能**；只能作为“事后选池会制造高收益”的反例 |
| VIX 期限结构 carry | 毛 Sharpe 0.734，4/4 子区间正 | 最大回撤 -50.1%，最差单日 -19.9%；同一 VIX 家族的另一条 vol-targeted sleeve DSR 0.315 | 不能直接上；只能做小权重尾部对冲研究 |
| 期权 VRP | 0.61 deploy anchor | 后来发现是 USDC 线性数据污染；清洗后所有结构为负 | **不能**；这是数据污染案例 |
| SG-1 BTC 方向性 RL | 早期 PF 1.689 | 修正数据时点后 PF 1.016，0/8 folds；早期数字是 timing bug | **不能**；这是泄漏/时点 bug 案例 |
| 跨资产 TSMOM | 净 Sharpe 0.60 | 成本后为正，但 DSR/PBO 未过；32 只未开发 ETF 降到 0.389 | **可研究**；不是高年化，是可分散的慢策略 |

### 3.4 其他被关闭的方向

| 方向 | 结果 | 对 A 股的启示 |
|---|---|---|
| 方向性 RL（BTC/黄金/SPX） | 14 条关闭；0/54 风险 overlay 达到 PF≥1 | 不要先用 RL 做单资产方向 |
| 期权卖方/0DTE/wheel | 成本后负；尾部大 | 没有期权数据/执行前不要碰 |
| 做市/资金费套利/ETF 配对 | 毛结构有，成本/执行吃不掉 | 高频/套利不是当前数据与基础设施的优势 |
| 股票横截面动量 | 方向正确、幸存者稳健，但被套利到成本附近 | 可做基准/对照，不要期待高超额 |
| PEAD | 可投资域内已死，只在不可交易微盘存活 | 作为负对照，不作为主策略 |
| 短期反转+困境过滤 | 反转 edge 与幸存者暴露是同一笔交易 | 必须 PIT 退市/ST，否则是假 alpha |
| 低波/BAB | 18 ETF 内 BAB 独立 Sharpe 0.412，与动量相关 -0.043，4/4 子区间正 | 可作为分散 sleeve，不是高收益引擎 |
| 价值因子 | 净 Sharpe -0.364，2011 后衰减 | 不能单独作为 A 股价值模板 |
| 国家/商品/加密 TSMOM | 国家 0.409 但相关 0.693；商品 0.356 相关 0.485；加密 -0.067 | 分散化不等于可加入；要看边际 Sharpe |
| 多速度 TSMOM | 差异 +0.145，CI [-0.325,+0.594]，DSR 0.090 | 不要用“多参数混合”救单速度 |
| 杠杆 ETF 替代 | 杠杆对 Sharpe 中性，有 decay tax | 不要用杠杆产品伪造收益 |
| 日内 HMA/VWAP/ORB/MACD | 0/40 ORB 正；HMA timing null p=0.582；VWAP/MACD 全 NO-GO | 高换手技术规则在成本下基本死 |

---

## 4. 为什么“高年化”经常是假的

### 4.1 数学：收益 = Sharpe × 波动

```text
CAGR ≈ Sharpe × σ - 0.5σ²（连续近似）
```

在 Sharpe 0.6 时：

- 10% 波动 → 约 6% 年化；
- 20% 波动 → 约 12% 年化；
- 50% 波动 → 约 30% 年化；
- 100% 波动 → 约 60% 年化；
- 166% 波动 → 约 100% 年化，但回撤接近 -100%。

**所以“年化 100%”首先是一个杠杆/波动问题，不是选股问题。** 任何只报年化、不报波动/回撤/杠杆的策略，都无法判断风险。

### 4.2 成本：2bps 与 A 股不是一回事

Sharpen 的 ETF 主结果是 2bps/10bps 每边；A 股现实口径至少包括：

- 佣金约 2.5bps（双向）；
- 滑点约 3bps（双向）；
- 印花税 10bps（仅卖出）；
- 涨跌停/停牌不可成交；
- 小市值冲击成本。

`config/execution_assumptions_cn.json` 的基线是 **15.5bps 左右**，是 Sharpen ETF 标准成本的数倍。任何高换手策略在 A 股必须先过 15.5/30bps 成本墙。

### 4.3 多重检验：77 次尝试后的“最佳”

Sharpen 的 trial ledger 记录 77 个预注册问题；DSR 的 N 取 77（bracket 64/77/96）。这意味着：

- 不是“跑了一个策略，Sharpe 0.6”；
- 而是“在 77 个问题里，最好的一个 Sharpe 0.6”；
- 经过多重检验后，DSR 只有 0.881，不能证明真实 Sharpe > 0。

本仓库同样有这个问题：P2.71 的模型矩阵、P2.75 的综合排序都试了多个变体；如果不把 N 记全，DSR 会系统性高估。

### 4.4 幸存者/事后选池

- 18 只 ETF 是 2026 年手选的幸存者；32 只未参与开发的 ETF 上 Sharpe 从 0.601 降到 0.389；
- FinRL-X 的 2017 年列表是事后选的；0/35 个当时可选的随机列表能跑赢 QQQ；
- 价值/小盘/低波的历史收益常混有退市股缺失。

**规则：任何股票池/ETF 池，如果是今天选的，必须做“当时可选池”的随机对照；否则只能叫 upper bound。**

### 4.5 数据污染与时点 bug

- 期权 VRP 的 0.61 来自 USDC 线性数据污染；
- SG-1 BTC 的 1.689 来自数据时点修复前的 bug；
- 这些不是“模型不好”，而是“数据/时钟错了”。

**规则：先做 truncation tripwire 和 timing parity，再谈模型。**

---

## 5. 值得移植的 12 个机制

按优先级排序：

### P0：先做，成本最低，收益最大

| # | 机制 | Sharpen 做法 | 本仓库落地 |
|---|---|---|---|
| 1 | 预注册 + 内容 hash | 假设、门槛、kill criterion 先提交；信号 spec hash 冻结 | `config/p2_78_gates.json` + 每份报告记录 commit/data hash；P2.77 假设卡 |
| 2 | 一问一 verdict | 一个 stage 只回答一个问题；禁止 train+tune+test 融合 | 每个脚本只产出一个 `verdict.json`；组合回测单独 stage |
| 3 | 门禁在配置，不在代码 | `configs/*.gates.yaml` 被代码读取 | 新增 `config/p2_78_gates.json`；脚本不写死阈值 |
| 4 | 负结果台账 | 77 条 NO-GO，带 kill criterion/证据 | 扩展现有 `结论台账.md` 或新建 `output/research/p2_78/NEGATIVE.md` |
| 5 | 成本墙 | 2/10bps 多档；报“edge 消失的成本” | A 股 10/15.5/30bps；报 break-even cost |

### P1：统计核心

| # | 机制 | 作用 | 本仓库落地 |
|---|---|---|---|
| 6 | DSR | 对 N、偏度、峰度修正后的 Sharpe 显著性 | `backend/services/validation_stats.py::deflated_sharpe` |
| 7 | PBO/CSCV | 衡量“样本内最优”在样本外落到中位以下的概率 | 同上；按 Sharpe/波动归一，不用均值伪影 |
| 8 | BHY-FDR / HLZ | 批量假设的假发现率/文献多重检验门槛 | 复用/扩展现有 IC 工具 |
| 9 | Block bootstrap + Newey-West | 自相关/重叠标签下的置信区间 | 21 日 block；标签重叠时用 NW 有效样本 |

### P2：证伪工具

| # | 机制 | 作用 | 本仓库落地 |
|---|---|---|---|
| 10 | Timing null | 保留暴露/交易次数/持有期，随机平移时点；看规则是否真的知道“何时” | 对行业 TSMOM/择时 sleeve 做 circular shift |
| 11 | Power analysis | 先算“这个测试能看见多大 edge”，再决定是否跑 | 每个 sleeve 先出 minimum detectable effect |
| 12 | Lockbox / 前向孵化 | 候选只在 hypothesis 之后的新 bar 上评估 | 60-120 交易日前向影子；不达标不晋级 |

### P2 补充

| # | 机制 | 作用 |
|---|---|---|
| 13 | LLM proposer 对分数盲 | 防止 LLM 看着结果编假设 |
| 14 | Sim-live parity / kill switch | 回测与 paper 同一成本/成交语义 |
| 15 | Gate reachability | 先算动作空间能否达到门槛，再烧 GPU |

---

## 6. A 股/中证500 可测试的策略矩阵

以下不是“推荐买入”，而是**预注册候选**。每条都要过 §7 的门禁。

### 6.1 候选 sleeve

| ID | 策略 | 机制 | 数据 | 频率 | 预期角色 | 主要 kill criterion |
|---|---|---|---|---|---|---|
| `CN_IND_TSMOM` | 行业/板块 TSMOM | 行业趋势延续；月度轮动 | PIT 行业指数/成分 | 月 | 核心卫星 | 15.5bps 净超额 ≤0；DSR<0.95 |
| `CN_LOWVOL_BAB` | 行业中性低波/BAB | 低风险异象、杠杆约束 | 日线 + 行业 | 月/季 | 防御 sleeve | 行业 beta 解释后无增量 |
| `CN_XSEC_MOM` | CSI500 内 12-1 动量 | 中期反应不足 | PIT 成分 + 日线 | 月 | 核心卫星 | 成本后超额 ≤0；换手过高 |
| `CN_REV_5` | 5 日反转 + 流动性过滤 | 短期过度反应 | 日线 + 成交额 | 周 | 负对照/卫星 | 15.5bps 成本墙 <20bps |
| `CN_QV` | 质量-价值 | ROE/PE 百分位 | PIT 财务 | 季 | 慢 sleeve | 风格中性后无增量 |
| `CN_PEAD` | 盈余公告后漂移 | 信息扩散慢 | PIT 公告 + 财务 | 事件 | **负对照** | 可投资域内无正收益 |
| `CN_MF_ROT` | 资金流行业轮动 | 资金流领先价格 | 2024-08+ 资金流 | 周/月 | 前向研究 | 历史不足；只做影子 |
| `CN_VOL_TARGET` | 波动目标 overlay | 控回撤，不产生 alpha | 指数/组合 | 日 | 风险管理 | 只允许改善回撤，不单独作为策略 |

### 6.2 不纳入首批的方向

- **单资产 RL 择时**：Sharpen 14 条关闭；本仓库也没有证明 RL 能稳定择时。
- **高频做市/套利**：数据、执行、基础设施都不匹配。
- **期权卖方/VRP**：没有期权链数据；且 Sharpen 的 VRP 锚点是数据污染。
- **杠杆 ETF 替代**：杠杆对 Sharpe 中性，且 A 股杠杆工具受限。
- **日内技术规则**：成本墙大概率直接杀死；只做负对照。

### 6.3 组合目标

首版不要把目标设成“年化 100%”。更现实、可检验的目标：

```text
相对 CSI500 的净超额 CAGR > 0
最大回撤 ≤ CSI500 + 5pp
年换手 ≤ 6x
DSR ≥ 0.95
PBO ≤ 0.50
```

如果连这个都做不到，正确结论是“当前数据/成本下没有可复现的 A 股 sleeve”，而不是加杠杆。

---

## 7. 落地验证漏斗

### T0：数据与因果

- 截断扰动：改未来价格，过去因子值不变；
- OHLC 清洗、复权、单位、交易日历；
- PIT 成分/行业/财务公告日；
- 停牌/涨跌停/ST/退市；
- 覆盖率与样本量。

### T1：信号

- 多期限 IC/RankIC（1/5/20/60）；
- ICIR、NW t、正号占比；
- 分位组合单调性；
- 多空/多头超额；
- 换手。

### T2：稳健

- 子区间（至少 4 段）；
- 股票池（CSI500、全 A、行业中性的）；
- 参数敏感性（窗口 ±20%/±50%）；
- 极值/缺失处理；
- 行业/市值/五风格中性。

### T3：成本

- 10/15.5/30bps；
- 成本墙；
- 涨跌停/停牌不可成交；
- 容量/冲击；
- 换手预算。

### T4：多重检验

- DSR（N=全部 trial）；
- PBO/CSCV（Sharpe 口径）；
- BHY-FDR；
- block bootstrap CI；
- HLZ hurdle。

### T5：组合

- 与 CSI500 同窗口；
- 与现有策略/因子的相关；
- 组合净 CAGR/Sharpe/MaxDD；
- 权重上限/行业上限；
- walk-forward；
- 前向影子。

---

## 8. 实施计划

### Phase 0：统计库（W1）

新增 `backend/services/validation_stats.py`：

```text
deflated_sharpe(returns, n_trials, benchmark_sr=0)
probability_of_backtest_overfitting(return_matrix, metric="sharpe")
block_bootstrap_sharpe_ci(returns, block=21, n_boot=2000)
newey_west_t(ic_series, lag=...)
bhy_fdr(p_values)
hlz_hurdle(...)
minimum_detectable_sharpe(...)
```

要求：

- 与 Sharpen 的公式逐项对照；
- 用手算/合成数据做单元测试；
- 对杠杆不变性、warm-up、均值 vs Sharpe 口径做测试；
- 不把“均值 CSCV”当 PBO（Sharpen 自己踩过这个坑：0.0009 → 0.5902）。

### Phase 1：信号漏斗（W2）

新增 `backend/services/signal_funnel.py`：

- 输入：信号面板、标签、股票池、成本、再平衡；
- 输出：T0-T5 scorecard + verdict；
- 与 `factor_inspector.py` / `factor_fitness.py` 对接；
- 结果写 `output/research/p2_78/<sleeve>/scorecard.json`。

### Phase 2：A 股 sleeve 矩阵（W3-W4）

脚本：`scripts/p2_78_sleeve_matrix.py`

首批 4 条：

1. `CN_IND_TSMOM`
2. `CN_LOWVOL_BAB`
3. `CN_XSEC_MOM`
4. `CN_REV_5`

每条跑：

- 2 个股票池（CSI500 PIT、全 A 可交易域）；
- 3 个再平衡频率（周/月/季）；
- 3 个成本（10/15.5/30bps）；
- 4 个子区间；
- 行业/市值中性两档。

### Phase 3：组合与基准（W5）

- 通过 T1-T4 的 sleeve 进入组合；
- 风险平价/逆波动/等权三种组合；
- 与 CSI500 同图；
- 输出 `nav.csv`、`metrics.json`；
- 做 DSR/PBO（N 含所有 sleeve/参数/窗口）。

### Phase 4：前端与台账（W6）

- 策略池详情页加“Sharpen 风格证据”卡：
  `DSR / PBO / cost wall / timing null / power / verdict`；
- 因子池页加同样字段；
- 负结果台账；
- 数据/回测/前向影子链接。

### Phase 5：前向影子（W7-W8）

- 通过 T5 的 sleeve 进入 60 交易日前向影子；
- 每日记录信号、成交假设、净值；
- 不达标不晋级；
- 影子期不调参。

---

## 9. 实验矩阵

| ID | 实验 | 目的 | 主指标 | 门禁 |
|---|---|---|---|---|
| E0 | 统计库对照 | 公式是否正确 | 与已知值/手算一致 | 误差 <1e-10 |
| E1 | 行业 TSMOM | 行业趋势是否可收割 | 净超额 CAGR、DSR | 净超额>0；DSR≥0.95 |
| E2 | 低波/BAB | 防御 sleeve 是否增量 | 组合边际 Sharpe | 相关<0.7；增量>0 |
| E3 | 12-1 动量 | CSI500 内动量 | RankIC、净超额 | 成本后>0 |
| E4 | 5 日反转 | 负对照/成本墙 | 成本墙 | ≥20bps 才继续 |
| E5 | 质量-价值 | 慢 sleeve | 残差 IC | 风格中性后>0 |
| E6 | 资金流轮动 | 短样本增量 | 前向 IC | 只做影子 |
| E7 | 组合 | 相对 CSI500 | 净 CAGR/MaxDD | 回撤≤基准+5pp |
| E8 | timing null | 规则是否知道何时 | p 值/随机对照 | 不显著则降级 |
| E9 | DSR/PBO | 多重检验 | DSR/PBO | DSR≥0.95；PBO≤0.5 |

### 9.1 关键对照

- CSI500 买入持有；
- 等权全 A；
- 现有 6 资金流因子组合；
- 随机排序；
- 同 sleeve 但用“今天的成分/行业”回填（用于量化前视偏差，不作为策略）。

### 9.2 指标表

```text
start, end, universe, rebalance, cost_bps,
ann_return, ann_vol, sharpe, max_dd, calmar,
turnover, trades, win_rate,
rank_ic_20, icir_20, nw_t,
excess_cagr_vs_csi500, tracking_error, information_ratio,
dsr, pbo, fdr_q, cost_wall_bps,
subperiod_consistency, timing_null_p, power_mde
```

---

## 10. 代码映射

### 10.1 新增

| 路径 | 职责 |
|---|---|
| `backend/services/validation_stats.py` | DSR/PBO/block bootstrap/NW/FDR/HLZ/power |
| `backend/services/signal_funnel.py` | T0-T5 scorecard/verdict |
| `backend/services/sleeve_backtest.py` | sleeve 组合、成本、基准、净值 |
| `scripts/p2_78_validation_stats.py` | 统计库自检/对照 |
| `scripts/p2_78_sleeve_matrix.py` | 首批 sleeve 矩阵 |
| `scripts/p2_78_walk_forward.py` | 滚动窗口 |
| `scripts/p2_78_report.py` | 报告/图表/负结果 |
| `config/p2_78_gates.json` | 门禁阈值 |
| `test/test_p2_78_validation_stats.py` | 统计公式测试 |
| `test/test_p2_78_signal_funnel.py` | 漏斗/verdict 测试 |
| `docs/runbooks/sharpen-review.md` | 使用说明 |

### 10.2 复用

| 现有资产 | 复用方式 |
|---|---|
| `backend/services/pit_data.py` | PIT 成分/停牌/ST/涨跌停 |
| `backend/services/factor_dsl.py` | 信号表达式 |
| `backend/services/factor_pool.py` | 因子版本 |
| `backend/services/factor_replay.py` | 成本/成交/NAV 账本 |
| `scripts/factor_inspector.py` | 单因子体检 |
| `scripts/p2_71_walk_forward.py` | 窗口/padding |
| `config/execution_assumptions_cn.json` | A 股成本/整手/涨跌停 |

### 10.3 前端

策略池详情页新增：

```text
Sharpen 证据
  DSR 0.xx (N=xx)   PBO 0.xx   cost wall xx bps
  timing null p=0.xx   power MDE=0.xx
  verdict: GO / NO-GO / INCONCLUSIVE
```

没有证据的字段显示“未测”，不显示 0 或绿色。

---

## 11. 时间表

| 周 | 工作 | 产出 | 退出条件 |
|---|---|---|---|
| W1 | 统计库 + 测试 | `validation_stats.py` | 手算/对照通过 |
| W2 | 信号漏斗 + 与因子池对接 | `signal_funnel.py` | 能对单因子出 scorecard |
| W3 | 行业 TSMOM + 低波/BAB | 两条 sleeve 报告 | 至少一条进入 T3 |
| W4 | 12-1 动量 + 5 日反转 | 两条 sleeve 报告 | 反转作为负对照 |
| W5 | 组合 + CSI500 曲线 | `nav.csv`/`metrics.json` | 净超额/回撤门禁 |
| W6 | DSR/PBO/timing null | 多重检验报告 | N 全量登记 |
| W7 | 前端 + 台账 | 页面/负结果 | 可重开/可回滚 |
| W8 | 前向影子 | 影子记录 | 60 日起步 |

若 W3-W4 没有任何 sleeve 过 T3，停止组合层，直接写负结果报告；不要用 RL/调参救。

---

## 12. 交付物

- `output/research/p2_78/reports/REPORT.md`：审阅 + A 股实验；
- `output/research/p2_78/reports/NEGATIVE.md`：失败 sleeve；
- `output/research/p2_78/<sleeve>/scorecard.json`；
- `output/research/p2_78/portfolio/nav.csv`：策略 vs CSI500；
- `output/research/p2_78/portfolio/metrics.json`；
- `config/p2_78_gates.json`；
- 前端证据卡；
- `docs/runbooks/sharpen-review.md`。

### 验收命令

```bash
.venv/bin/python -m pytest test/test_p2_78_validation_stats.py test/test_p2_78_signal_funnel.py -q
.venv/bin/python scripts/p2_78_validation_stats.py --self-test
.venv/bin/python scripts/p2_78_sleeve_matrix.py --config config/p2_78_gates.json --sleeve CN_IND_TSMOM
.venv/bin/python scripts/p2_78_walk_forward.py --first-year 2018 --last-year 2026
.venv/bin/python scripts/p2_78_report.py --run output/research/p2_78
```

---

## 13. 风险与边界

| 风险 | 说明 | 对策 |
|---|---|---|
| 把 Sharpen 当收益模板 | 它的策略大多关闭，幸存者也未过门禁 | 只借方法，不借收益 |
| 成本口径错配 | 2bps ETF vs A 股 15.5bps | 统一用 `execution_assumptions_cn.json` |
| 幸存者偏差 | 18 ETF 手选；A 股今天成分 | PIT 成分 + 随机当时可选池 |
| N 低估 | 只统计成功 run | trial ledger 全量 |
| PBO 伪影 | 均值 CSCV 被杠杆放大 | 用 Sharpe/波动归一 |
| 短样本 | 资金流 2024-08 起 | 只做前向/描述 |
| 行业分类前视 | 当前行业回填历史 | PIT 行业；否则标前视 |
| 过度中性化 | 把真实 alpha 中性掉 | 原始/中性两套都报 |
| 数据污染 | 期权 VRP 类 | 数据源审计 + 截断测试 |
| 杠杆幻觉 | 100% 年化靠 16.6x | 报波动/回撤/杠杆，不单报年化 |

---

## 14. 验收清单

- [ ] 统计库 DSR/PBO/block bootstrap/NW/FDR 有手算与合成测试。
- [ ] PBO 使用 Sharpe/波动归一，不用均值伪影。
- [ ] 每条 sleeve 有预注册 kill criterion。
- [ ] 成本用 10/15.5/30bps 三档，报成本墙。
- [ ] 股票池 PIT，退市/ST/停牌纳入。
- [ ] 与 CSI500 同窗口、同成本、同再平衡。
- [ ] DSR 的 N 来自 trial ledger，含失败/放弃。
- [ ] timing null / power analysis 至少对择时 sleeve 做。
- [ ] 组合净超额、最大回撤、换手、DSR、PBO 全部落盘。
- [ ] 负结果与正结果同一报告；没有只报最佳窗口。
- [ ] 前端“未测”不显示成 0/通过。
- [ ] 任何高年化数字同时给波动、回撤、杠杆、样本期。

---

## 15. 最终判断

Sharpen 值得学的不是“某个策略”，而是它的**研究纪律**：

```text
先写死失败条件 → 先测数据能不能看见 → 先算成本墙
→ 再做组合 → 再用多重检验 → 最后才谈年化
```

对本项目最现实的路线：

1. **先移植统计与漏斗**（W1-W2）；
2. **用 A 股数据测四条慢 sleeve**（W3-W4）；
3. **只把通过 T1-T5 的 sleeve 放进组合**（W5-W6）；
4. **不碰高频/期权/单资产 RL**，直到数据和执行能力匹配；
5. **接受“没有高年化策略”这个结果**，如果它就是数据给出的答案。

如果最终只有一条低波/行业动量 sleeve 能提供个位数净超额，那也是一个可复现、可解释、可监控的结果；它比一个靠 16 倍杠杆做出来的 100% 年化曲线更有用。
