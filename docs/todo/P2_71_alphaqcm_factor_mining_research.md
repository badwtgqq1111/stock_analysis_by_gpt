# P2.71 独立因子选股链路、因子池与 AlphaQCM 受控研究

日期：2026-10-16  
状态：**首批纵向实现、真实信号审计、公共无前视回放、精确Alpha101/P0契约、隔离AlphaQCM toy/20k pilot、PIT财务特征、前端原生因子池导入与中证500组合曲线均已完成；20k/10k pilot与财务组合回测均为否定结果，全量训练、PIT补全、walk-forward与前向影子尚未完成。**  
目标：从零建立独立于失败 LightGBM 的因子选股路径，以用户指定的 **主力资金流追踪（大单净流入 + 吸筹形态）`moneyflow_arm`** 为主要挑战基准，成本后年化收益追平或超越；中证500为辅助市场基准。收益目标是待检验假设，不是方法保证。

## 一、用户需求、边界与实施顺序（覆盖早期“接入旧池”设想）

### 1.1 新路径不是旧 LightGBM 修补工程

- [x] 独立命名空间与持久化：新因子库不写旧生成式 `factors.json`，不被每日同步覆盖。
- [x] 新信号路径不导入旧 LightGBM 训练/打分、preselect、PK、条件风控或旧策略回放引擎。共享只读原始行情和资金流输入；审计CLI可调用原规则作为对照。
- [x] 独立公共研究回放与费用ledger已实现；数据时点契约、组合器、实验ledger和前向影子继续实施。新的风控作为单独消融，不能把旧条件风控接回来当“新路径”。
- [x] 原策略代码、生产配置和历史曲线保持不变；不抹去失败路径，不重写历史。

### 1.2 验收对象与公平对比

- **主挑战基准**：原 `moneyflow_arm`；**辅助**：中证500（000905.SH）。只画中证500净值曲线不足以验收。
- 当前冻结历史参考：2024-08-27至2026-10-15，514个净值日期；原策略累计+171.0051%、旧口径年化+60.5232%、最大回撤−40.0436%；日历365.25天CAGR约+59.5917%。中证500价格指数同期累计+50.5856%、日历CAGR约+21.1595%、最大回撤−25.0474%。这是现有回放产物，不是本次新路径业绩，也不是严格可执行收益。
- [x] 冻结研究用共同区间、初始现金、当前股票池、交易日历、信号/成交约定、现金与成本；报告收益、日历CAGR、常规Sharpe、回撤、换手、交易数、暴露与成本敏感性。真实发布时点/PIT、容量和可执行约束仍待审计。
- [x] 原策略与新策略在**同一无前视评测器**重算；现有历史曲线单独保留“legacy reference”，不复制旧引擎问题以强行做漂亮的年化持平。见[公共回放报告](../../output/research/p2_71/replay_audit/REPORT.md)。
- 已审计差异：旧引擎每边15.5bp（文档“双边15.5bp”有歧义）；开盘预算引用当日close；买费未预留；缺失close会漏估持仓；止损基准为信号日close，不是实际买价；旧“Sharpe”为CAGR/波动率。上述均须账本测试后在新公共评测器明确，不静默修改原策略。
- [ ] 验收分三层：**信号一致 → 逐笔交易/NAV一致（同明确口径）→ 组合优化样本外成本后年化不低于原策略**。回撤/换手容忍范围须在收益实验前登记；失败结果同样归档。不事后选最好网格证明成功。

### 1.3 策略池：两个正交维度，而非“传统/因子”二分

| 家族 | 决策类型 | 实现版本 | 对照/状态 |
|---|---|---|---|
| moneyflow | rule | original | 原规则、冻结历史基准 |
| moneyflow | hybrid（硬门槛+因子排序） | equivalent_factorized | 固定6个因子v1，已验证信号及公共回放等价 |
| moneyflow | hybrid / factor_combination（实验时明确） | optimized | 待实现，不伪造收益或晋级 |

- [x] 新增家族配置、benchmark引用、固定factor_id/version；策略池显示已登记原策略的家族/维度，支持决策类型和实现版本筛选；因子池显示完整三版本设计。
- [x] 未审计的旧策略标为未标注，不按名字猜测分类。等价版已有独立研究回放，优化版尚无成交曲线，不塞入收益排行榜、不自动接每日生产调度。
- [ ] 公共回放通过后，将真实可运行的等价/优化版本注册为候选，接入共同窗口比较与前向记录。

### 1.4 因子池（首批真实前后端纵向实现）

- [x] 独立入口 `/factors`、SQLite人工因子库与REST API；6个资金流规则因子可解释、可编辑、校验、保存、重读。
- [x] 每因子明确公式、金融解释、所有输入变量的定义/单位/窗口/可得时点、算子和常量；来源保存标题、URL、作者、日期、页码/公式/代码位置。项目自研允许URL空，但出处位置必填。
- [x] 追加不可变历史版本、内容SHA256、父子关系；乐观锁冲突返回409，持久化事务防止并发覆盖。
- [x] 支持编辑、替换提案、优化提案、派生新因子、恢复历史内容为新版本，前端显示草稿公式/解释差异与历史。新版本一律 `unmeasured`，不继承旧版IC/收益，也不自动改变策略绑定。
- [x] 受控AST DSL：白名单算术/比较/and/or/mean/delay/rank/abs；拒绝未来引用、未知变量、属性/下标/任意调用；没有Python eval。负延迟、零分母、NaN/Inf、重复键与多源输入有测试。
- [ ] 扩充变量字典与来源认证、统一单位、历史发布时点、公式参数结构化编辑和更丰富可视化对比；对外部署前沿用项目部署认证策略。
- [ ] 增加因子诊断、版本绑定的IC/分层收益/相关性、评测任务与可信优化提案流程；“优化”按钮本批只保存提案。

### 1.5 主力资金流：先等价因子化，再组合优化

原策略已使用量化信号，因子不是与策略对立的概念。因子是数值特征；策略还要规定组合、筛选、排序、仓位、交易与退出。

固定等价版：

```text
mf_strength@1 >= 1.0
AND mf_persistence@1 >= 0.6
AND -0.10 <= mf_return20@1 <= 0.08
AND mf_price@1 > mf_ma20@1
AND mf_amount20@1 >= 20,000,000
AND mf_price@1 > 2
排名：mf_strength@1降序；并列按股票代码升序（新契约明确）
已验证研究执行契约：次日开盘；最多5仓；持有10交易日/信号价下8%止损；每边15.5bp
```

- [x] 原规则vs独立因子表达式：合成边界、NaN、资产排列、未来扰动、固定版本不受编辑影响等测试。
- [x] 真实数据首轮：2026-07-01至2026-10-15，warmup自2026-05-01；571,625行日线；**4,933条信号、71个信号日，入选集合、所有信号数值字段及显式排序完全一致**。见[审计报告](../../output/research/p2_71/signal_audit/REPORT.md)与[完整manifest](../../output/research/p2_71/signal_audit/manifest.json)。当前basic非PIT；此报告本身只审信号，NAV见后续公共回放。
- 数据口径新发现：资金流表为多源稀疏宽表。同股同日多行；原策略通用字段实际上来自 `source=moneyflow`。新路径显式选择该源，并验证其他源通用字段全空，拒绝有冲突的“随手去重”。
- 上游z实际为 **(当日净额−5观测均值)/样本标准差**（含当日、ddof=1、min_periods=2），完整5点理论边界约±4/√5，不是硬截断，也不是5日累计净额的z；正流入比例min_periods=1，上游净额缺失比较会算false。名称“大单/主力”继承策略名，当前输入为 `net_mf_amount`，尚非严格仅大单定义。
- [ ] 扩展全历史日期、参数/门槛和独立手算测试；修复/审计原始数据可得时点、历史复权与PIT，不把公式因果性测试当作原始数据无泄漏证明。
- [x] 实现公共无前视回放；逐日名单、排序、成交、持仓、现金与NAV已核对；并列按代码升序。旧回放不稳定排序不复制进新引擎，legacy差值不作为alpha。
- [x] 已建立独立LightGBM组合实验协议与固定模型；验证期改善但留出期衰减，未通过“不低于两版基线”门槛，保留等价因子基线。
- [x] 完成首轮数学组合实验：硬 `AND` 继续作为可行域；等权截面rank、训练窗IC权重、训练窗PCA、75%规则rank+25% PCA rank 均只在可行域内改变排序。结果见[数学组合报告](../../output/research/p2_71/composition_v1/REPORT.md)：等权−11.45%、PCA−2.71%、PCA混合+5.70%；IC权重留出+105.89%但验证−17.75%，因此按预注册验证门禁拒绝，全部 `RETAIN_BASELINE`。
- [x] 完成首轮非LightGBM模型矩阵：Ridge、ElasticNet、Logistic、ExtraTrees、RandomForest、HistGradientBoosting。所有模型复用冻结的6个因子变换、同一标签切分、成本与公共账本；仅验证期门禁可登记候选，留出期不回流选参。
- [x] 矩阵结果已归档至[模型矩阵报告](../../output/research/p2_71/model_matrix_v1/REPORT.md)：Ridge/ElasticNet/RandomForest验证期通过门禁，但留出期CAGR分别为−20.68%、−22.38%、−25.19%；Logistic −0.11%、ExtraTrees −2.08%、HistGradientBoosting −4.64%，均低于共同基线留出CAGR +24.66%，全部 `RETAIN_BASELINE`。当前留出仅186个收益观测，且属回顾式研究窗口，不能晋级或声称超越。
- [x] 新增独立模型适配器和可复现实验入口；模型只消费因子列，不接旧LightGBM训练、preselect、PK、条件风控或旧回放。实现见[模型适配器](../../backend/services/factor_model_matrix.py)、[矩阵脚本](../../scripts/p2_71_model_matrix.py)、[协议](../../config/p2_71_model_matrix.json)与[测试](../../test/test_p2_71_model_matrix.py)。
- [x] 已补充“为什么机器学习会输”的诊断：公共留出基线 RankIC 为 −0.0152；模型 RankIC 为 +0.0024～+0.0320，但 Top5 与基线重合仅约10.3%～18.4%，预测标签与实际止损/持有/费用账本错配；例如 ExtraTrees 留出 RankIC 最高之一（+0.0287）、平仓胜率55.10%，仍为 −2.08% CAGR，因组合路径/回撤和退出时点决定最终NAV，而非单一RankIC。报告同时记录买卖笔数、止损次数、持仓到期次数、平仓胜率和费用。
- [x] 完成交易目标对齐试验：标签改为次日开盘入场、信号收盘价下8%止损、10个有效收盘日到期、次日开盘退出，并扣除双边15.5bp费用；模型分数只以25%/50%权重对规则排名做受限倾斜，避免完全破坏资金流先验。结果见[交易目标对齐报告](../../output/research/p2_71/trade_aligned_v1/REPORT.md)：Ridge@25%留出CAGR −5.58%，Ridge@50% −18.86%，ExtraTrees@25% −9.64%，HistGradientBoosting@25% −18.06%，仍低于共同基线 +24.66%，全部 `RETAIN_BASELINE`。
- [x] 新增交易目标标签实现[trade_labels](../../backend/services/factor_trade_objective.py)、实验脚本[交易目标对齐研究](../../scripts/p2_71_trade_aligned_study.py)、协议[p2_71_trade_aligned.json](../../config/p2_71_trade_aligned.json)与测试[test_p2_71_trade_objective.py](../../test/test_p2_71_trade_objective.py)。这表明“标签错配”是原因之一但不是全部；仍需滚动状态特征、容量/PIT和更长前向窗口，不能把本轮回顾式结果当作晋级。
- **组合诀窍**：先决定哪些是不可补偿硬约束，再对通过者用可比尺度的因子评分；不要把元、比例、z直接相加。线性加权允许优势抵消劣势，不等于AND规则；相关因子堆叠可能只是重复暴露。用消融检验新增因子在成本后的边际贡献，而不是因子数量。
- **AND 的数学表示**：对每个谓词 `g_j(x)∈{0,1}`，`AND_j g_j(x)=∏_j g_j(x)=1_{x∈⋂_j G_j}`；在实现中是逐行布尔交集，NaN按false处理。它不是多维向量，也不是 PCA。通过 AND 后，因子向量 `z(x)∈R^K` 才用于排序，例如 `S(x)=Σ_k w_k rank_k(z_k(x))`。PCA 只是在训练集拟合的线性投影 `PC1(x)=vᵀD^{-1}(x−μ)`，用于压缩/去相关或诊断排序，不能替代硬门槛；软 AND（sigmoid/t-norm）只可用于研究优化，不能放宽生产约束。
- [x] 新增组合原语[hard_and/indicator_and/soft_and/weighted_rank_score/PCAComposer](../../backend/services/factor_composition.py)、研究入口[数学组合研究](../../scripts/p2_71_composition_study.py)、协议[p2_71_composition.json](../../config/p2_71_composition.json)和测试[test_p2_71_factor_composition.py](../../test/test_p2_71_factor_composition.py)。
- [x] 完成概率语义试验：将因子值映射为训练集经验CDF概率，比较独立性乘积、logit和规则概率混合。结果见[概率组合报告](../../output/research/p2_71/probability_v1/REPORT.md)：独立乘积留出+7.46%、logit+24.94%、规则混合+7.58%，但三者验证门禁均失败，全部 `RETAIN_BASELINE`。`P(A∩B)=P(A)P(B)` 只有独立假设下才成立；相关因子必须用联合/条件概率或直接保留硬集合交集，不能把概率乘积当真实联合概率。
- [x] 明确门禁原因：Logit 验证CAGR **72.52%** 确实高于基线 **57.77%**，但验证最大回撤 **−29.21%**，基线为 **−9.49%**；本协议允许最多额外3个百分点，即候选回撤必须不差于 **−12.49%**，因此验证门禁失败。留出CAGR **24.94%** 仅比基线 **24.66%** 高0.28个百分点，留出只有186个收益观测且不能反向改变验证选择，不能单独宣称超越。
- [x] 完成回撤控制消融[报告](../../output/research/p2_71/drawdown_overlay_v1/REPORT.md)：Logit + 截面20日波动率过滤（保留当日波动率低于70%分位）留出CAGR **32.40%**、最大回撤 **−15.66%**、Sharpe **1.173**，相对Logit回撤改善2.48个百分点；但验证CAGR仅24.85%，低于基线57.77%，所以仍未通过验证门禁。Logit回撤节流（soft 8%/hard 15%/floor 25%）把留出回撤降至−16.18%，但CAGR降至0.96%，不接受。
- [x] 新增独立研究回撤节流参数[ReplayConfig](../../backend/services/factor_replay.py)：只缩小新开仓，不强平已有仓位；默认None保持历史基线不变。新增[回撤消融脚本](../../scripts/p2_71_drawdown_overlay_study.py)、[协议](../../config/p2_71_drawdown_overlay.json)和`34`项回放测试覆盖。当前最有价值的后续方向是用训练/验证冻结波动过滤阈值，并在至少252个留出观测和前向影子中验证，而不是按本段留出结果回选。
- [x] 完成首版机构式组合优化器：在独立因子链路内使用训练期概率Logit分数作为收益代理，滚动60日样本协方差作为风险模型，目标函数为 `μ'w − λw'Σw − γ turnover`，并施加 long-only、单票上限、总目标仓位和正则化协方差约束。实现见[组合优化器](../../backend/services/factor_portfolio_optimizer.py)、[研究脚本](../../scripts/p2_71_institutional_study.py)、[协议](../../config/p2_71_institutional.json)与[测试](../../test/test_p2_71_portfolio_optimizer.py)。
- [x] 机构版已登记策略池候选[主力资金流追踪（机构式组合版）](../../config/strategies.json)，仅研究回放，不接生产调度。与传统版同公共账本对比见[机构版报告](../../output/research/p2_71/institutional_v1/REPORT.md)：传统版留出CAGR +24.66%、回撤−20.32%；机构均衡版留出CAGR −4.42%、回撤−21.98%；机构保守版 +5.13%、回撤−27.87%。验证期机构均衡版CAGR +87.66%但留出衰减，全部 `RETAIN_BASELINE`。
- [x] 机构版已接入策略池绩效链：`comparison_nav.csv` 同窗提供传统规则版、等价因子版和中证500对比曲线；策略池详情页显示机构版研究净值及两条冻结比较曲线。事务验证见[机构组合事务记录](../../output/research/p2_71/institutional_transaction/VERIFICATION.txt)，包含原始/修改/回滚哈希、基线/修改/回滚命令与结果。
- [x] 新增[概率原语](../../backend/services/factor_probability.py)、[概率研究脚本](../../scripts/p2_71_probability_study.py)、[协议](../../config/p2_71_probability.json)和[测试](../../test/test_p2_71_factor_probability.py)。因子策略可以用纯数学表示，但完整策略还必须补齐数据时点、组合、交易、费用和退出；数学表达式本身不保证收益。

公共回放首轮结果（2024-08-27至2026-10-15，warmup 2024-06-01）：原规则与等价因子版本逐笔、现金、持仓和NAV完全一致；成本后累计 **+171.8733%**、日历CAGR **+59.8312%**、最大回撤 **−20.1393%**、日均Sharpe **1.7364**。中证500价格指数累计 **+50.5856%**、CAGR **+21.1595%**。这只是同口径回顾式等价基线；不等于优化版、盲测或未来年化达标，旧legacy曲线（CAGR 59.5917%、回撤−40.0436%）仅作不同执行口径参考。完整现金/费用勾稽和输入hash见[回放报告](../../output/research/p2_71/replay_audit/REPORT.md)与[manifest](../../output/research/p2_71/replay_audit/manifest.json)。

### 1.6 后续里程碑与首批产物

| 顺序 | 工作 | 当前状态 |
|---|---|---|
| M1 | 独立因子池+版本+DSL+资金流拆解+UI | 已实现并测试，仍属研究功能 |
| M2 | 真实日级信号等价、版本不变性、来源审计 | 全历史共同窗口通过；PIT/数据时点审计待做 |
| M3 | 独立因果回放、成本账本、交易/NAV共同口径 | 已完成：514日回顾窗口两版逐笔/NAV完全一致；CAGR 59.8312%，不作OOS承诺 |
| M4 | 因子组合优化；同口径追平/超越moneyflow_arm | 已完成首版机构式组合层与传统/等价因子/中证500对比曲线；留出期未晋级，RETAIN_BASELINE；继续做PIT、容量与前向影子 |
| M5 | 精确Alpha101、隔离AlphaQCM toy/小面板、无测试窥视 | 已完成：71条精确基线、隔离adapter、20k步3seed pilot；pilot未显示优于随机搜索 |
| M6 | 多seed/walk-forward/前向影子及候选接线 | 待做，依据真实结果决定晋级 |

首批代码：[独立存储](../../backend/services/factor_pool.py)、[DSL](../../backend/services/factor_dsl.py)、[信号](../../backend/services/factor_moneyflow.py)、[API](../../backend/api/factor_pool.py)、[因子池前端](../../frontend/src/pages/FactorPool.vue)、[种子](../../config/factor_pool_seed.json)、[策略家族](../../config/factor_strategy_families.json)、[审计CLI](../../scripts/p2_71_factor_audit.py)、[测试](../../test/test_p2_71_factor_pool.py)。

```bash
.venv/bin/python -m pytest test/test_p2_71_factor_pool.py -q
.venv/bin/python scripts/p2_71_factor_audit.py
.venv/bin/python scripts/p2_71_replay_audit.py
(cd frontend && bun run build)
```

已有的资料阅读与固定commit静态审阅保留如下。论文结果不代表本项目年化承诺；文中“复用”限协议和只读数据，不意味着依赖旧模型/筛选/风控链路。

## 二、文章说了什么，原始证据支持什么

### 2.1 来源与实验范围

附件是 QuantML 于 2026-10-08 发布的文章，核心依据为 [FactorBench 原论文](https://arxiv.org/abs/2610.06947)。证据优先级为：原论文/附录 → 固定版本实现 → 二次文章；项目可用性最终由本地冻结评测决定。

FactorBench 比较 9 个自动挖掘方法、5 个市场、每市场 3 个随机种子，共 **135 次运行、4,914 个候选**；Alpha101 另列为参考库。市场为 SP100、恒生、CSI300、FTSE100、日经225，其中 CSI300 使用 **272 只固定成分股**。

- 训练：2017—2021；验证：2022—2023；测试：**2024-01-01 至 2025-10-31**，不是完整的2025年。
- Yahoo 日频数据、固定成分股，存在幸存者偏差；不能直接外推到 A 股全市场 PIT 股票池。
- 各方法保留原生搜索流程，计算预算与输出数量不齐；不是等算力下的纯算法优劣证明。
- 统一组合采用 ridge、验证集相关性贪心去重（阈值0.8）、20交易日调仓；多头为前20%等权，多空为前后20%各 +50%/−50%。
- 论文成本口径为每交易金额10bps，代码默认融券年费50bps；成交金额与换手的换算仍需审计，见任务 P0-4。
- 五类控制为 beta252、波动率60、12–1动量、21日反转、log(20日平均成交额)。缺少行业/基本面控制；**金额中性不等于 beta/风格中性，残差 IC 不等于纯 alpha**。

### 2.2 核对后的关键数字

下表摘自 FactorBench Table 2：**先在每市场内平均种子，再对五市场等权平均**；均为测试期组合信号/组合结果，不是中国市场单独业绩，也不是本项目复跑结果。

| 方法 | IC | RankIC | 残差 IC | 多头净 CAGR | 多头净夏普 | 多空净 CAGR | 多空净夏普 | 多空年换手 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Alpha101 | 0.0238 | 0.0176 | 0.0237 | 26.5% | 1.241 | 2.6% | 0.319 | 8.7 |
| AlphaGen | 0.0064 | −0.0128 | −0.0080 | 28.0% | 1.127 | 2.3% | 0.297 | 5.6 |
| **AlphaQCM** | **0.0191** | **0.0056** | **0.0175** | **29.6%** | **1.313** | **3.7%** | **0.668** | **4.6** |
| R&D-Agent | 0.0253 | 0.0046 | 0.0011 | 31.2% | 1.193 | 6.5% | 0.679 | 7.8 |

解读：AlphaQCM 比 Alpha101 的净多空夏普高、换手低，但 RankIC、残差 IC 较低，多空最大回撤也更深（−8.5% 对 −6.8%）。其残差/原始 IC 的聚合比值约91.6%，只说明对这五项控制的保留程度，不能称“91.6%是真实 alpha”。五市场均值及三个种子本身不构成统计显著性证据。

附录的风格中性化多空组合中，AlphaQCM净CAGR从3.7%降至1.3%，夏普从0.668降至0.283。它仍是该设置下唯一保持正净收益的自动挖掘方法，但经济收益明显变弱；上表的0.668不能称为“风格中性后的夏普”。

### 2.3 二次文章中的表述需要校正

- AlphaQCM 是 **ICML 2025** 论文；个别仓库说明中的2026年份不采用。
- AlphaForge 是生成—预测框架（GAN式生成），不是扩散模型。
- FactorBench 中 R&D-Agent 输出 Python；AlphaAgent、QuantaAlpha 使用 DSL 表达式，不是所有 LLM 方法都直接输出 Python。
- AlphaForge 的1,572个、QuantaAlpha的1,619个候选，是**15次运行的合计**，不是单次运行产量。
- “AI仍跑不赢Alpha101”是标题概括；原研究更准确的结论是**没有一个范式在所有维度稳定占优**。

## 三、AlphaQCM 的机制与可迁移部分

### 3.1 机制

[原论文](https://proceedings.mlr.press/v267/zhu25ag.html)名为 *AlphaQCM: Alpha Discovery in Finance with Distributional Reinforcement Learning*。

- 在 AlphaGen 式 token/表达式环境中逐步构造公式，完整表达式执行后才获得评价，奖励较稀疏。
- 优化的是**互补因子池**，而不是把单因子 IC 最高的若干公式简单拼接。池更新使奖励分布变化；这里的非平稳性首先指搜索过程，不代表已解决真实市场制度/行情漂移。
- 分开训练均值 Q 网络与分位数网络（IQN 或 QR-DQN）；QCM 指 **quantiled conditional moments**。
- 核心选动作逻辑可概括为 `argmax_a [Q_mean(s,a) + lambda_std * QCM_std(s,a)]`。标准差项是搜索探索奖励，不是组合波动惩罚；默认高阶偏度/峰度系数为0。
- 论文关于方差估计的性质依赖其假设；不扩大成对任意收益分布和训练状态都成立的保证。
- 原生池使用 IC 目标与 L1 优化（`l1_alpha=0.005`）；FactorBench 的统一 ridge 是**导出公式后的公共组合层**，两者分开复现。
- 参考代码 `try_new_expr` 虽计算了 `increment`，实际返回当前池 IC，不是 IC 增量。奖励改成增量属于新变体，不能悄悄当作原版。

输入通常为 OHLCV + VWAP；参考配置表达式最大长度20，滞后窗口10/20/30/40/50。FactorBench 的 IQN 配置为约200万步、pool=10、batch=128、PER、gamma=1、start_steps=10,000、eval_interval=1,000，分位抽样 N/N_dash/K=64/64/32。复现时仍以固定版本配置文件为准，并导出完整 manifest。

### 3.2 为什么原论文的 IC 看起来更高

原论文按2010—2019训练、2020验证、2021—2022测试，10个种子，验证集选池。其表中 AlphaQCM 的 IC 分别为 CSI300 **8.49%±1.03%**、CSI500 **9.55%±1.16%**、全市场 **9.16%±1.61%**。

这些数字与 FactorBench 的0.0191不矛盾：股票池、年份、预算、目标、组合和评测流程不同。**不把任何一个论文数字当作本项目验收门槛或预期收益。** AlphaGen 提供互补池思想，AlphaForge提示把搜索质量和动态组合贡献分开做消融，Alpha101提供强人工公式基线。

## 四、参考实现静态审计：先处理的差异

固定审阅版本：

- [用户 RL-Final](https://github.com/lishazha-ops/RL-Final/tree/e0af0faed1076d22dc9acefffc32134a19ddb3cb)：`e0af0faed1076d22dc9acefffc32134a19ddb3cb`。
- [官方 AlphaQCM](https://github.com/ZhuZhouFan/AlphaQCM/tree/dffc695f9ea54e6a54e4dac86ee357be314fead4)：`dffc695f9ea54e6a54e4dac86ee357be314fead4`。
- [FactorBench-Release](https://github.com/ZhuoHan1998/FactorBench-Release/tree/8dbc5b42e005a4038dd52e4790b30b467a5c1575)：`8dbc5b42e005a4038dd52e4790b30b467a5c1575`。

| 问题 | 已观察证据 | TODO / 验收 |
|---|---|---|
| 训练过程可见测试集 | [BaseAgent.evaluate](https://github.com/lishazha-ops/RL-Final/blob/e0af0faed1076d22dc9acefffc32134a19ddb3cb/fqf_iqn_qrdqn/agent/base_agent.py#L265) 每轮计算 valid/test，并保存 test-best | 搜索进程不接收测试 calculator/标签；只按验证选池；冻结后由独立 evaluator 一次性评价。存在此路径不证明原论文采用了 test-best |
| 数据源与市场配置不一致 | [stock_data.py](https://github.com/lishazha-ops/RL-Final/blob/e0af0faed1076d22dc9acefffc32134a19ddb3cb/alphagen_qlib/stock_data.py#L43) 用 us_stocks 路径但 REG_CN；通用入口改成近年美股，CSI300入口保留旧时期 | 市场、日历、股票池、区间均显式配置；禁止由脚本名推断实际数据 |
| buffer 被静默裁短 | [日历边界处理](https://github.com/lishazha-ops/RL-Final/blob/e0af0faed1076d22dc9acefffc32134a19ddb3cb/alphagen_qlib/stock_data.py#L59) 裁剪回看/未来区间，且末端索引可能越界 | 数据不足时明确失败或删失标签；不补0、不改变日期与张量切片含义 |
| 相关性过滤并非最终去重标准 | [alpha_pool.py](https://github.com/lishazha-ops/RL-Final/blob/e0af0faed1076d22dc9acefffc32134a19ddb3cb/alphagen/models/alpha_pool.py#L77) 阈值0.99；内部判断使用有符号相关 | 原版行为保留为基线；公共导出层增加绝对 Rank 相关与表达式规范化，覆盖负相关复制品 |
| 保存权重不等于可恢复训练 | [模型保存与加载](https://github.com/lishazha-ops/RL-Final/blob/e0af0faed1076d22dc9acefffc32134a19ddb3cb/fqf_iqn_qrdqn/agent/base_agent.py#L137) 不包含完整 replay/optimizer/RNG/pool；require_mean 分支疑似装载到错误网络 | 做恢复前后动作、池和loss一致性测试；完整快照未完成前，只允许标记为重新启动 |
| 依赖与设备较旧 | Python3.8、torch1.13.1/CUDA11.6一代依赖；部分入口硬编码CUDA | 独立环境/进程，不降级项目环境；先CPU toy smoke，MPS支持需单独验证 |

### FactorBench 适配可以借鉴，但不是免审计替代品

- [PanelStockData](https://github.com/ZhuoHan1998/FactorBench-Release/blob/8dbc5b42e005a4038dd52e4790b30b467a5c1575/factor_mining/methods/alphaqcm/data.py) 直接把面板转为 `[date, feature, asset]`，可绕过 Qlib bin 数据导入。优先评估这个思路，复用本地 Parquet。
- [AlphaQCM runner](https://github.com/ZhuoHan1998/FactorBench-Release/blob/8dbc5b42e005a4038dd52e4790b30b467a5c1575/factor_mining/methods/alphaqcm/run.py) 导出 valid-best，并有 IQN/QCM 数值与 PER 采样保护；但仍向原 agent 传入 test calculator。本地要求更严格：训练侧根本拿不到测试数据。
- runner 可在中断后导出部分结果；必须记录 `status/steps_completed/stop_reason`，部分运行不计为完成的200万步实验。
- [组合回测](https://github.com/ZhuoHan1998/FactorBench-Release/blob/8dbc5b42e005a4038dd52e4790b30b467a5c1575/factor_bench/portfolio/backtest.py) 类默认5日调仓，而论文主表20日；显式传20，不能依赖默认值。
- 同一回测以 `0.5*sum(abs(delta_weight))` 算换手，再乘成本bps，和“按每交易金额收费”的注释存在约定核对需求；不能未经交易账本验证就修改/背书论文净绩效。
- Alpha101移植也含缺失相关值置0等实现选择；精确基线需要检查缺失值、常数序列和排序语义，不直接认定第三方库天然一致。

## 五、与项目既有能力的关系：读取证据，不沿用失败路径

- 原始行情、资金流和参考论文可共享只读；旧策略仅作为冻结对照。新路径源码不得导入LightGBM/preselect/PK/条件风控或其业务状态。
- 既有fitness、IC、snapshot、OOS的统计原则和报表字段可供审计参考，但新流程需要显式时间契约、独立运行ID/数据hash和测试；不通过调用旧pipeline获得隐性筛选/权重/风控。
- 旧因子库由同步脚本生成；人工公式存在独立SQLite，不向生成产物回写。现有单股Alpha101近似保留，精确面板基线另命名。
- 既有投研执行台可承接未来隔离miner的运行日志；新的因子池承担可编辑版本与来源管理，不替代策略池。
- 开发机Apple Silicon，先CPU toy；MPS迁移需实测。论文A100每市场/seed约8.19小时不是本机工时预测。

## 六、P0：开始性能实验前必须完成

### P0-1 数据、标签和时间契约

- [x] 固定 `date × asset × feature` schema、市场交易日历、时区、单位、复权版本、数据可得时点和缺失规则；审计 volume股/手、amount、VWAP与复权的一致性。缺真实VWAP时显式标记代理并单列实验，不静默用OHLC均值替代。见[数据契约审计](../../output/research/p2_71/data_contract_v1/REPORT.md)。
- [ ] 审计历史上市/退市、停牌、ST、历史成分；PIT股票池用于项目主实验，固定成分池仅作论文兼容轨道。历史缺失/退市结算显式处理，不能只留下今天仍上市的股票。
- [x] 两类标签分别命名并留公式、起止日期：论文兼容close-to-close与本地下日可执行标签；禁止一个标签训练、另一个标签打分却称同一实验。
- [x] 特征仅使用截至t可得数据；负向Ref等未来引用只允许在隔离标签层。warmup可来自过去，未来buffer只能用于成熟标签，跨边界标签须purge。
- [x] 覆盖不足、常数序列、NaN/Inf、未成熟20日收益保留失败/删失原因；不把缺标签填0。给出最早/最晚日期、完整回看长度和可评股票数。
- **验收**：玩具面板手算标签/窗口一致；修改t之后的价格不改变截至t的特征；分割交界无越界标签；资产排列变化不改变对齐结果。

### P0-2 精确 Alpha101 与公共评测器

- [x] 新增独立命名空间 `alpha101_exact71`，对应FactorBench无行业数据的71条公式，逐条登记原公式、可执行/可评分、差异；同时保留现有近似库为legacy对照。见[精确Alpha101审计](../../output/research/p2_71/alpha101_exact_v1/REPORT.md)。
- [x] 截面rank、时序rank、delay/decay/correlation、行业中性化、缺失和零分母分别测试；不把缺行业信息的公式伪装成精确实现。公开基准71条可执行、70条可评分，是该基准的数据结果，不强求本地相同。
- [x] 统一逐日Pearson IC、逐日Spearman RankIC及聚合代码；匹配同日有效资产集合，区分简单日均、按样本数加权、ICIR与年化ICIR。
- **验收**：小矩阵手算、独立参考实现和排列不变性测试通过；报告legacy与exact的值/排序差异后，才允许使用“超越Alpha101”措辞。

### P0-3 隔离适配与无测试窥视

- [x] 固定上述commit，在独立环境/子进程接入本地面板；防止多个仓库同名 `alphagen` 包互相覆盖；审计可分发依赖与许可证清单。
- [x] 搜索接口只接收train与inner-valid；冻结表达式、方向、过滤规则、预处理、组合权重和超参数后生成artifact hash，外部 evaluator 才读取test。
- [x] 增加测试集访问失败测试；固定seed改变/移除测试标签，搜索输出hash须不变。验证集反复选择次数与所有尝试进入trial ledger。
- [x] 先CPU合成数据约2,000步验证token合法性、mask、奖励、导出/重算；再20,000步真实小面板通路测试。两者均不作收益优劣结论。见[20k pilot](../../output/research/p2_71/alphaqcm_pilot_v1/REPORT.md)。
- [ ] 固定均值网/分位网加载语义、QCM数值保护、PER非法叶子采样、恢复状态；记录失败率与异常，不静默丢弃坏seed。
- **验收**：toy前向/反向与导出重算一致；测试窥视检查通过；中断恢复有证据；environment lock、seed、设备和停止原因完整。

### P0-4 成交时点和成本账本

- [x] 显式设置20日调仓；用时间线确认信号日、下单日、成交价、持仓生效日和第一笔收益。检查 `i+lag+1` 的实际语义，不只看“next-day”注释。见成本账本的`rebalance_timeline/periodic_rebalances`实现与测试。
- [x] 构造现金→单股、A股→B股、部分减仓、+50%/−50%多空转换例子，按真实买卖名义金额逐笔计算费用；报告单边换手/双边成交额的映射与初始建仓收费。见[成本账本](../../output/research/p2_71/cost_ledger_v1/REPORT.md)。
- [x] 分列佣金、税费、滑点、冲击、借券；成本0/10/20/50bps作预登记敏感性。A股生产方向另加T+1、涨跌停、停牌、整手和容量约束。
- **验收**：交易账本与净值逐笔勾稽；所有方法共用同一执行/成本实现；修订成本时完整重算对照，不只重算AlphaQCM。

## 七、P1：受控实验矩阵

### 7.1 假设、样本与预算

预登记三个假设：H1，QCM在同预算下比无探索奖励更稳定地产出有效互补因子；H2，这些因子超出Alpha101与五类风格代理；H3，在独立新路径组合中引入QCM能相对冻结因子基线改善成本后增量收益/风险，而不是只改善单因子IC。

- [x] 首轮只做一个经过审计的A股股票池；算法比较只用共同OHLCV/VWAP。基本面、资金流等增强输入另开实验，避免把数据优势归因给算法。
- [ ] 若数据覆盖允许，设论文兼容轨道2017—2021/2022—2023/2024—2025-10-31；本地PIT轨道按覆盖审计另定起点（可从2018年开始），不得拼接成同口径结果。
- [x] 已公开、已被研究反复使用的历史段称“回顾式OOS”，不冒充从未见过的盲测。新证据来自冻结后的前向影子。
- [ ] 外层walk-forward每fold从train重新挖掘，内层valid选池；按**标签结束日期**purge，方向、缩放、去重与组合参数都只由train/inner-valid确定。
- [x] pilot固定seed=0/1/2；预设墙钟、表达式评价次数、步数、池大小、验证次数和预算停止条件。v1/v2等评价次数对照已完成，wall-clock与评价次数均记录；两轮均未显示QCM优于随机搜索。
- [ ] 全量IQN约200万步只能在P0通过后批准执行；先估测每千步时间/内存，再决定本机CPU或隔离Linux/CUDA。资源不足按partial归档，不外推完整结果。

### 7.2 最小对照与消融

| 组 | 候选来源 | 核心目的 |
|---|---|---|
| A | 精确Alpha101可执行子集 | 强人工基线；legacy近似另报，不代替A |
| B | 独立新路径冻结基线（含主力资金流等价版） | 不依赖旧LightGBM池；冻结因子/组合版本 |
| C | 相同DSL的随机搜索、GP | 检查搜索复杂度是否值得；预算及失败评价均计入 |
| D | AlphaGen PPO | 区分互补池RL与分布式RL的贡献 |
| E | IQN + QCM（原版目标） | 主候选 |
| F | IQN但 `lambda_std=0` | 探索奖励消融；其余设置固定 |
| G | IQN + 普通分位数离散度探索 | 分离QCM矩估计与泛化的不确定性奖励效应 |
| H（后续） | QR-DQN + QCM | 验证是否依赖特定分位网络；不挤占首轮资源 |

- [ ] 所有组导出候选后，使用**相同预处理、验证去重、ridge调参与组合执行**作为主比较；Alpha101候选数单列，另做统一pool-cap敏感性，不隐藏原生规模差异。
- [ ] 原生L1池单列诊断；可选LightGBM组合器固定特征处理与同一超参数搜索预算，作为次级实验，不与ridge主表混排宣称搜索器提升。
- [ ] 核心配对：`B` vs `B+E`；补充 `A` vs `A+E`。池合并后按同一训练/验证规则重新拟合权重，不利用测试集挑组合。
- [x] 超参数至少冻结pool=10、长度20、QCM系数和上述时间预算；扩网格前登记新增试验。3个seed是pilot，不足时明确标记结论不确定。

## 八、指标、统计与准入标准

### 8.1 每次运行必须交付的报告

- [ ] **有效性**：提案数、可解析/可执行/可评分数、无效原因、日期/资产覆盖、NaN率、耗时、峰值内存、实际步数和停止状态。
- [ ] **预测性**：逐日IC/RankIC、未年化ICIR、分年/分股票池表现、方向稳定性；重叠20日标签用HAC或交易日block bootstrap（块长至少20并做敏感性），报告置信区间和有效时间样本量，不把股票×日期当独立样本。
- [ ] **残差与增量**：先做FactorBench五项控制，再接项目25项及行业/规模控制；控制项须当时可得。raw/residual使用同一有效样本，列明回归、截距和缺失处理，不把留存比例当纯alpha证明。
- [ ] **差异性**：训练/验证绝对Rank相关、反向复制品、规范化表达式hash、聚类、有效秩、对Alpha101/现有池相似度；测试期相似度只作冻结后诊断，不回流筛选。
- [ ] **经济性**：毛/净收益、超额收益、净IR/Sharpe、最大回撤、换手、风格暴露、持仓重合、容量与成本敏感性；单独报告 `B+E−B` 的配对时间序列及区间。
- [ ] **多重试验**：trial ledger包含被拒候选、seed、超参数、预算与适应性验证次数；复用FDR/DSR/PBO，但逐项检查统计输入。RL步数不是独立试验数，传统FDR也不自动消除适应性搜索偏差。

### 8.2 分层验收，不设论文数字导向的门槛

1. **通路通过**：P0全部测试通过，语义/时间/成本一致，实验可复查；失败即不进入收益排名。
2. **研究候选**：通过项目既有coverage、稳定性、冗余及统计协议，不能为AlphaQCM事后放宽门槛。[现有OOS脚本](../../scripts/factor_oos_deflated.py)默认残差IC门槛0.02，与论文五市场均值0.0175不是同一统计对象，既不据此直接淘汰，也不自动豁免。
3. **增量证据**：预先冻结主指标（建议成本后增量IR）、对照和容忍回撤/换手范围；结合多个seed/外层fold及配对block区间判断。点估计改善但区间跨0，只列观察，不称验证成功。
4. **前向影子**：表达式与组合规则冻结后，遵循[既有晋级门禁](P2_40_promotion_gate.md)的最少期数等要求；门禁通过也不等于可以省略人工审查和执行约束。
5. **否定结果同样交付**：若没有稳定增量，归档因素重叠/风格解释/成本侵蚀/预算不足等证据，停止扩算，不按测试表现反复挖到好看。

研究多空组合仅用于诊断；不能把论文假设的可借券金额中性组合直接标成A股可执行策略。生产方向优先多头/现有袖套增量，使用同一真实执行约束。

## 九、P2：AlphaQCM矿机通过研究门槛后接入工作台（不阻塞首批人工因子池）

- [x] 定义miner公共契约：`propose / evaluate / export`；输出表达式AST/DSL版本、origin、seed、数据/标签hash、split、pool状态、预算、源码commit和trial lineage。研究侧已由[隔离adapter](../../backend/services/alphaqcm_adapter.py)实现；前端因子池导入与原生表达式版本编辑已接通，挖掘工作台的任务式API接线仍未做。
- [ ] 在既有[挖掘API](../../backend/api/factor_mining.py)增加miner类型与隔离任务入口，复用session、停止/恢复、日志和fitness展示；矿机执行沿用工作台，人工公式管理使用首批独立因子池页面。
- [ ] 复用[值快照](../../scripts/snapshot_factor_values.py)保存导出值与重算校验；奖励、训练IC、验证IC、冻结后测试IC分字段，不混成一个“最佳IC”。
- [ ] 先只读研究库，后人工选择进入影子；不自动覆盖现有公式、组合权重或线上配置。
- [ ] 晋级记录保留原池/原权重版本；回退时停用新增候选并恢复上一冻结版本，再验证持仓/信号状态。生产策略未变；首批文件回退已另行验证，矿机晋级回退仍待未来实施。

### AlphaQCM研究执行顺序（先完成上文M1—M4，再按此推进）

| 顺序 | 任务 | 依赖 | 交付/完成标准 |
|---|---|---|---|
| 1 | P0-1 + P0-2 | 无 | 数据/标签manifest、精确基线清单、手算与一致性测试 |
| 2 | P0-3 | 1 | 隔离adapter、无测试窥视测试、toy及小面板smoke报告 |
| 3 | P0-4 | 1 | 成交/成本合成账本与统一回测测试 |
| 4 | P1 pilot | 1—3 | 固定配置、3seed、预算/失败ledger、A—G统一指标 |
| 5 | 外层滚动与增量验证 | 4通过 | 每fold独立搜索记录、B对B+E配对OOS报告、否定结果 |
| 6 | P2影子接入 | 5通过 | 可审计miner契约、快照、前向记录与回退验收 |

拟建成果：`config/alphaqcm_research.yaml`、隔离的miner adapter与tests、按run_id归档的manifest/trials/factor_pool/metrics/report。这些矿机训练产物尚未创建；本次已交付上文M1/M2/M3首批代码、API/UI、测试与信号/公共回放审计，不把信号审计称作RL训练或年化达标。

### 后续实施可复用提示词

```text
按P2.71实施P0，不启动全量训练、不修改生产策略。
先按独立新路径固定数据与标签契约，不导入旧LightGBM/preselect/PK/条件风控，
给legacy Alpha101和精确截面Alpha101分命名空间；以独立环境适配
固定commit的AlphaQCM，训练进程不得访问test数据或标签。
先写时间/排序/成本/测试窥视不变性测试，再跑CPU toy smoke。
输出真实修改、测试命令与结果、artifact路径、剩余差异及预算估测；
只有P0通过后，另行执行预登记seed/预算的P1，不把smoke称为业绩验证。
```

## 十、资料索引与溯源

1. [附件PDF](<../reference/AI 因子挖掘，为什么依然跑不赢十年前的 Alpha101？.pdf>)；[QuantML文章](https://mp.weixin.qq.com/s/zAGZb4_9XtD41jfwC3Smtg)。附件SHA256：`c4c9a6cfd2ac322ab67f87cec40a1c3943dbacee1bda259d5a9a9f37a09135a5`。
2. [FactorBench论文](https://arxiv.org/abs/2610.06947)、[v1全文/附录](https://arxiv.org/html/2610.06947v1)、[发布仓库](https://github.com/ZhuoHan1998/FactorBench-Release)。数字以Table1/2与数据/实现附录为准。
3. [AlphaQCM：ICML 2025 / PMLR267](https://proceedings.mlr.press/v267/zhu25ag.html)、[OpenReview](https://openreview.net/forum?id=3sXMHlhBSs)、[官方实现](https://github.com/ZhuZhouFan/AlphaQCM)。
4. [用户参考RL-Final](https://github.com/lishazha-ops/RL-Final)：本轮为静态审计，不是该实现的运行验收。
5. [AlphaGen论文：2306.12964](https://arxiv.org/abs/2306.12964)、[作者代码](https://github.com/RL-MLDM/alphagen)：互补因子池与RL基线。
6. [AlphaForge论文：2406.18394](https://arxiv.org/abs/2406.18394)：生成—预测与动态组合，支持“搜索/组合分开消融”的设计。
7. [Alpha101原论文：1601.00991](https://arxiv.org/abs/1601.00991)：人工公式基线；原文平均持有期约0.6—6.4天，FactorBench的20日目标只是特定公共任务，不是其所有用途。

审阅边界：本轮阅读公开原文/附录并检查固定版本代码，尚未独立复核论文原始行情与运行产物；以上论文数值均为原文转述，项目结论需待上述实验产生。


### 2026-10-17 实施续记：M3与端口

- 新增[独立回放器](../../backend/services/factor_replay.py)、[回放审计CLI](../../scripts/p2_71_replay_audit.py)和[33项回放测试](../../test/test_p2_71_replay.py)。与既有61项合计94项通过。修正的是新公共研究执行，不改旧策略与旧曲线。
- 真实共同窗口：2,936,378行含warmup行情、7,835,108行稀疏资金流、31,136条信号、514个交易日期。274买/269卖、期末5仓；逐日现金、持仓市值及每边费用勾稽通过；3个缺close持仓日使用已知价格留痕。
- 默认保持原信号日close止损和有效close计时；另提供entry_price止损与market_sessions计时的显式研究选项。开盘估值不引用当日close，预留买费一次，缺open卖单延后，期末不强平。
- 正式服务8000，Vite开发端口5173未改。18771为已停止的临时浏览器验收服务；旧8000进程重载后因子池API返回200，浏览器使用8000。
- 下一步M4：先登记可用日期/PIT与执行约束，再冻结训练/验证划分与组合消融协议；禁止把本窗口试出来的最优权重当作盲测达标。AlphaQCM尚未启动训练。

固定成本敏感性（同一信号/共同窗口，不据此挑最优参数）：

| 每边成本bp | 两版共同CAGR | 最大回撤 |
|---|---:|---:|
| 0 | +72.6267% | -18.2635% |
| 10 | +64.2600% | -19.4723% |
| 15.5 | +59.8312% | -20.1393% |
| 20 | +56.2925% | -20.6810% |
| 50 | +34.5741% | -24.1986% |


### 2026-10-17 策略池与LightGBM续记

- 等价因子版已登记到[策略声明](../../config/strategies.json)为 `research / factor_pipeline`，不接每日生产刷新，不生成独立名单；策略池绩效使用公共因果回放产物，展示因子净值、中证500、原规则同评测器对照和252交易日年化/日历CAGR。
- [独立LightGBM协议](../../config/p2_71_lightgbm.json)固定训练/验证/留出窗口、特征、超参数、标签purge与晋级门槛。结果见[实验报告](../../output/research/p2_71/lightgbm_v1/REPORT.md)：验证CAGR 63.6124% vs 基线57.7666%，但留出CAGR **−26.4503%** vs 基线 **+24.6568%**，留出仅186个收益观测且未通过252门槛；状态 `RETAIN_BASELINE`，未替换因子版/原规则版。
- “年化不低于两版”现在是硬准入条件，并要求回撤、样本数、PIT/执行/前向门禁同时通过；不通过只保留基线。LightGBM属于独立组合器实验，不是旧LightGBM训练链路回接。


### 2026-10-17 Trae ML 剔涨停版接入与复核

- [x] 复跑[独立研究脚本](../../scripts/moneyflow_ml.py)：累计+249.0%、原244日年化+77.4%、最大回撤−54.4%；原0.73为CAGR/波动率，非标准Sharpe。统一252日年化80.98%、标准Sharpe 1.038，换口径不是收益提升。
- [x] 登记 `moneyflow_ml_nb` 到[策略池声明](../../config/strategies.json)，研究曲线对照中证500；K线选股源 `cn_moneyflow_ml_nb` 最新5只，日期2026-10-14，历史531日/2584条。历史导出删除未来标签，标明“次日开盘事后筛选候选”，不解释成当日已知名单或成交。
- [x] 研究展示与排名资格解耦：`ranking_eligible=false`，保留净值、指标、选股入口，取消年化/累计/超额榜名次；旧规则与其他策略不受本项退出影响。
- [x] 本轮177项相关测试通过，前端构建通过；8000后端重载后API名单与策略池读回成功。副本回滚及哈希记录见[事务验证](../../output/research/moneyflow_ml/integration_transaction/VERIFICATION.txt)。本轮浏览器连接未建立，未新增UI实测结论。
- [ ] 先按[接入口径审计](../../output/research/moneyflow_ml/INTEGRATION_AUDIT.md)修复公共交易日/真实涨跌停/停牌/卖出执行、早停日期切分与标签成熟purge；规则与ML同公共因果账本重算。
- [ ] 再预登记Top10/20、行业上限、波动率预算、换手约束等消融，冻结训练/验证/留出与全部尝试记录；不因当前历史高收益替换基线。


### 2026-10-17 M5/P0 实施续记：精确Alpha101、隔离AlphaQCM与否定pilot

本轮把M5从“待做”推进到“可复现的受控基线”，没有把pilot当成收益晋级。

#### 1. 数据/标签契约（P0-1）

- 新增[market_contract](../../backend/services/market_contract.py)：固定`trade_date × stock_code × feature`、Asia/Shanghai日频、qfq价格、收盘后可得、次日开盘执行；显式区分`close[t+20]/close[t]-1`（paper）与`open[t+21]/open[t+1]-1`（executable）。
- [数据契约审计](../../output/research/p2_71/data_contract_v1/REPORT.md)：11,961,750行原始日线，按上市日过滤后11,566,738行；5,224只股票、3,106个日期、2014-01-02→2026-10-15；过滤掉395,012条上市前记录。
- 发现并记录原生VWAP与qfq OHLC不同尺度（`vwap/close`中位数1.0469）。默认公式改用OHLC3代理，原生VWAP只作单列对照，不静默混用。
- 训练/验证按标签结束日期purge 20个可观测交易日；测试段442个日期、约223万条非缺失20日标签。
- 仍未解决：退市日期、历史ST/*ST、历史指数成分、真实涨跌停/停牌状态、逐日复权因子。报告明确标为非完整PIT，不冒充全市场盲测。

#### 2. 精确Alpha101（P0-2）

- 新增[alpha101_exact71](../../backend/services/alpha101_exact.py)，从FactorBench固定commit `8dbc5b42` 移植71条无行业中性化公式；保留原单股时序rank近似库为legacy对照。
- [精确Alpha101审计](../../output/research/p2_71/alpha101_exact_v1/REPORT.md)：8,734,127行、5,142只股票、2,143个日期；测试段422个RankIC日期。
- 测试期paper标签RankIC最高为`alpha040` +0.1034（NW t +4.44）、`alpha094` +0.0784、`alpha013` +0.0689；这些是回顾式单因子统计，不是组合成本后收益。
- legacy对照（30只抽样、前20条）平均秩相关0.6902，逐值相等比例10.33%；证明旧库的时序rank代理与精确截面实现不是同一对象，后续不得用旧值冒称“超越Alpha101”。

#### 3. 隔离AlphaQCM与20k CPU pilot（P0-3/M5）

- 新增[alphaqcm_adapter](../../backend/services/alphaqcm_adapter.py)、[worker](../../scripts/alphaqcm_worker.py)、[evaluator](../../scripts/alphaqcm_evaluate.py)和[expression parser](../../backend/services/alphagen_expression.py)。训练子进程只收到train/valid parquet；test只在artifact hash冻结后由独立进程评测。
- vendored官方AlphaQCM commit `dffc695f9ea54e6a54e4dac86ee357be314fead4`到[vendor/alphaqcm](../../vendor/alphaqcm)，保留MIT许可证；QCM奇异矩与PER边界采样有重试保护，训练侧不再调用test calculator。
- 合成通路先跑通约200/10,000步；真实小面板固定60只训练期流动性股票、20,000步、seed 0/1/2。[pilot报告](../../output/research/p2_71/alphaqcm_pilot_v1/REPORT.md)归档9个run（QCM std_lam=1、std_lam=0消融、随机搜索）。
- pilot结果：`std_lam=1`测试RankIC均值+0.0205（3 seed标准差0.0419），`std_lam=0`为−0.0123，随机搜索为+0.0546；三组valid IC均值均为负。结论为**NEGATIVE_OR_INCONCLUSIVE_PILOT**：20k CPU步数下没有证据表明QCM优于随机搜索，按预注册保留基线，不做seed/超参数回选。
- 等表达式评价次数对照：v1 `T=11`，QCM +0.0463 vs random +0.0558；v2 `T=62`，QCM +0.0134 vs random +0.1507。两轮均未显示QCM优于随机搜索，仅作描述性结果。
- 2M步全量训练仍未执行；20k步单seed约30–40秒（60只股票、CPU），不能外推论文A100/2M步结果。中断恢复/checkpoint续训仍未实现。

#### 4. 执行与成本账本（P0-4）

- 新增[execution_ledger](../../backend/services/execution_ledger.py)与[成本审计](../../output/research/p2_71/cost_ledger_v1/REPORT.md)，覆盖现金→单股、A→B切换、部分减仓、多空穿越四个场景。
- 费用分列佣金、卖出印花税、过户、滑点、冲击；借券年费不混入逐笔费用。单边换手定义为`(买入名义+卖出名义)/2/初始现金`，双边成交比为`(买入+卖出)/初始现金`。
- 仍未把20日调仓、T+1、涨跌停、停牌、整手和容量接入AlphaQCM组合回放；本账本不声称生产可执行。

#### 5. 验证与产物

- `pytest test/test_p2_71*.py -q`：**130 passed**。
- 新增测试覆盖：数据契约/手算标签/purge、精确Alpha101逐条计算与排列/未来扰动、成本账本四场景、AlphaQCM无test可见/冻结artifact评测/表达式白名单。
- 复现命令：

```bash
.venv/bin/python scripts/p2_71_data_contract_audit.py
.venv/bin/python scripts/p2_71_alpha101_audit.py --legacy-assets 30 --min-assets 30
.venv/bin/python scripts/p2_71_cost_ledger_audit.py
.venv/bin/python scripts/p2_71_alphaqcm_pilot.py --steps 20000 --seeds 0,1,2 --std-lams 1.0
.venv/bin/python scripts/p2_71_alphaqcm_pilot.py --steps 20000 --seeds 0,1,2 --std-lams 0
.venv/bin/python scripts/p2_71_alphaqcm_pilot.py --steps 20000 --seeds 0,1,2 --random
.venv/bin/python scripts/p2_71_alphaqcm_summary.py
.venv/bin/python -m pytest test/test_p2_71*.py -q
```

剩余关键路径：补齐PIT/退市/ST/成分与执行约束；外层walk-forward与前向影子；把财务/原生因子池候选接入成本后组合回测并做B vs B+E增量；只有这些通过后才讨论晋级。


### 2026-10-17 财务特征、因子池与中证500曲线续记

本轮把 AlphaQCM 从“纯价量研究产物”推进到可导入因子池、可用财务特征、可对中证500出组合曲线的研究闭环。结果仍是研究回放，不是生产晋级。

#### 1. PIT财务与行业相对估值特征

- 新增[fundamental_data.py](../../backend/services/fundamental_data.py)：
  - 财务字段：`roe, roa, grossprofit_margin, netprofit_margin, debt_to_assets, netprofit_yoy, or_yoy, ocfps`；
  - 行业相对估值字段：`pe_ind_pct, pb_ind_pct, size_ind_pct`；
  - 财务按 `ann_date` 做 as-of join，不使用 `end_date` 预知未来；缺失保留NaN，不填0。
- AlphaQCM vendored `FeatureType` 从6个价量特征扩展到17个；`PanelStockData` 对缺失特征列填全NaN tensor列，保证action space与tensor宽度一致。
- 财务/估值特征默认启用（`--no-fundamentals` 可关闭）；旧6特征checkpoint不能在新17特征空间上续跑。

#### 2. 前端因子池原生表达式打通

- 新增[alphagen_native.py](../../backend/services/alphagen_native.py)：白名单解析 + pandas评测，支持Add/Sub/Mul/Div/Pow/Greater/Less/Abs/Log/Sign/Ref/Mean/Sum/Std/Var/Max/Min/Med/Mad/Rank/Delta/WMA/EMA/Cov/Corr，只允许正窗口。
- `FactorStore` 增加 `expression_language`：`p2_71_dsl` 或 `alphagen_v1`；原生表达式可保存、校验、预览、编辑、追加版本。
- 新增[导入脚本](../../scripts/p2_71_import_alphaqcm_factors.py)：把冻结run的`factors.json`写入同一个`output/factor_pool_v2/factors.sqlite3`；默认跳过重复ID，`--update`追加版本。
- `/api/factor-pool` 返回原生因子；`/api/factor-pool/validate`、`/preview` 支持`expression_language=alphagen_v1`；`/api/factor-pool/alphaqcm/import` 只允许导入`output/research/p2_71`下的run。
- 前端[FactorPool.vue](../../frontend/src/pages/FactorPool.vue) 显示语言徽标、支持原生公式编辑，并增加“AlphaQCM 冻结组合 · 中证500 对比”面板与“把该run候选导入因子池”按钮。
- 导入候选仍标为`unmeasured`，不自动改变策略引用，不构成晋级。

#### 3. 中证500组合回测

- 新增[alphaqcm_portfolio.py](../../backend/services/alphaqcm_portfolio.py)与[回测脚本](../../scripts/p2_71_alphaqcm_backtest.py)：信号t收盘选top20，t+1开盘等权成交，20日调仓，每边15.5bp，长仓、分数股、close估值、期末不强平。
- 首个财务特征run（10,000步、60只股票、seed 0）挖出2个表达式：`$netprofit_yoy` 与 `Min($pe_ind_pct,40)`；valid IC +0.0275，冻结test RankIC −0.1201。
- 2023-01-01至2026-10-15组合回测：AlphaQCM组合累计 **−15.34%**、252日年化 **−4.49%**、最大回撤 **−36.83%**；中证500累计 **+13.66%**、年化 **+3.60%**、最大回撤 **−31.15%**；相对中证500累计超额 **−25.51%**。
- 结论：该轮财务特征候选**没有跑赢中证500**，按预注册保留基线；曲线用于暴露失败而非挑选好看结果。报告见[alphaqcm_portfolio_v1](../../output/research/p2_71/alphaqcm_portfolio_v1/REPORT.md)。
- 头脑风暴的两个候选已登记策略池：`brain_quality_lowpe_momentum`（累计+27.63%、年化+6.97%、回撤−23.90%，相对中证500累计超额+12.30%）与`brain_panic_reversal_quality`（40触发、累计+1.36%、年化+0.37%、回撤−2.20%，相对超额−10.82%）；均为research阶段。
- `$netprofit_yoy`/`Min($pe_ind_pct,40)`是另一轮AlphaQCM财务实验，不属于这两个头脑风暴候选；其组合回测见上条。

#### 4. 验证

- `pytest test/test_p2_71*.py -q`：**154 passed**。
- `bun run build`：通过。
- 新增测试覆盖PIT财务as-of、17特征tensor、原生表达式解析/评测、因子池原生导入与版本、组合回测曲线。
- 集成验收与命令输出见[alphaqcm_integration/REPORT.md](../../output/research/p2_71/alphaqcm_integration/REPORT.md)及[VERIFICATION.txt](../../output/research/p2_71/alphaqcm_integration/VERIFICATION.txt)。
- 复现命令：

```bash
.venv/bin/python scripts/p2_71_alphaqcm_pilot.py --steps 10000 --seeds 0 --assets 60 --fundamentals --output output/research/p2_71/alphaqcm_fin_v1
.venv/bin/python scripts/p2_71_alphaqcm_backtest.py --run output/research/p2_71/alphaqcm_fin_v1/iqn_lam1_s0_10000 --output output/research/p2_71/alphaqcm_portfolio_v1 --fundamentals --start 2023-01-01 --end 2026-10-15
.venv/bin/python scripts/p2_71_import_alphaqcm_factors.py --run output/research/p2_71/alphaqcm_fin_v1/iqn_lam1_s0_10000
(cd frontend && bun run build)
```

剩余：财务字段的公告时点/修订版本审计、行业分类历史PIT、真实涨跌停/停牌/整手/容量接入组合回测、全量2M/walk-forward与前向影子。

#### 5. 个股头脑风暴模式与宇通客车样例

- 新增[stock_brainstorm.py](../../scripts/p2_71_stock_brainstorm.py)：按月份输出价量、资金流、财务、估值、行业中位、中证500对照、事件/恐慌日、前瞻收益和可测试假设。
- 样例[600066.SH 2026-03/06 报告](../../output/research/p2_71/stock_brainstorm/600066_2026_03_06/REPORT.md)：
  - 2026-03 +21.18%，同期中证500 −12.02%、汽车行业中位 −9.61%；3/31年报ROE 38.27%、营收+11.31%、净利+34.94%，是质量/分红重估事件。
  - 2026-06 −16.76%，同期中证500 +9.15%；6/16单日−6.72%、量比2.91，6/17资金流转正，随后20日+10.62%、40日+13.13%。这支持“恐慌超跌后的战术反弹”，不支持“6月整月趋势机会”。
  - 3/31涨停但资金流z5=−1.77、净额−11.0亿，提示追高/派发风险。
- 该样例仅用于假设生成，事件样本极少，不晋级为策略。
- 两个头脑风暴候选已做300只流动性面板回测（2023-01-03至2026-10-15、top20/20日调仓/15.5bp）：
  - `brain_quality_lowpe_momentum`：累计+27.63%、年化+6.97%、最大回撤−23.90%；中证500累计+13.66%、年化+3.60%，相对累计超额+12.30%。
  - `brain_panic_reversal_quality`：40个触发，累计+1.36%、年化+0.37%、最大回撤−2.20%；相对中证500累计超额−10.82%。
  - 两者均为research阶段；质量候选值得继续做PIT/成本/容量与前向影子，事件候选样本仍少。

### 2026-10-17 研究管线续记：续训校验、等评价次数对照、面板/特征修正、PIT审计、walk-forward与2M启动

本轮补齐 M5 之后四条工程线：可续训的全量checkpoint、等评价次数对照（修正后口径）、PIT数据层审计、外层walk-forward框架，并启动2M步全量训练。全部为回溯性研究结果，不构成晋级；凡口径变更均有独立说明文件。

#### 1. 检查点/续训（工程口径）

- 新增[alphaqcm_checkpoint.py](../../backend/services/alphaqcm_checkpoint.py)：完整状态快照（online/target网络与优化器、replay memory、训练计数、episode中断点与探索RNG），按步数间隔落盘、只保留最近2份；快照22.9MB/份。
- worker支持`--resume`；停止信号（步数/评价次数/墙钟）与评价计数随快照持久化。
- 验证：同总预算下“中途续训”与“一次直跑”产物按位一致（bitwise），测试见`test/test_p2_71_alphaqcm_checkpoint.py`。

#### 2. 等评价次数对照（v1→v3）

- v1/v2为**指数面板、6特征历史变体**（修复前口径，仅搜索机制结论）：T=11时 qcm +0.0463 vs random +0.0558；T=62时 qcm +0.0134 vs random +0.1507（描述性，N=3）。
- v3为修正口径（60只股票面板、17特征、当前代码）：参考相60,000步×2模式×3 seeds，评价次数 qcm 67/104/54、random 82/96/69 → **T=54**（先写盘冻结后开跑，全部arm在54次评价处停止）。等评价结果：qcm均值test RankIC **−0.0252**（−0.0663..+0.0251，停止步数均值49,930）；random均值**−0.0153**（−0.0490..+0.0261，均值40,884）。
- 全部6对 ref/cmp 因子清单**逐位一致**：池在≤54次评价已饱和，追加评价不改变冻结组合；“等评价次数”在该面板上等价于“等池收敛”。
- 报告：[alphaqcm_equal_evals_v3/REPORT.md](../../output/research/p2_71/alphaqcm_equal_evals_v3/REPORT.md)。

#### 3. 面板与特征集修正（有据可查）

- 面板：`select_panel` 增加股票池交集（`stock_only`），修复“按流动性选出60个上证指数”事故；登记与影响见[PANEL_SELECTION_CORRECTION.md](../../output/research/p2_71/PANEL_SELECTION_CORRECTION.md)。pilot v1与equal-evals v1/v2保留为指数面板历史变体，不重写。
- 特征集：FeatureType 6→17扩展引入的一致性缺陷已修复——`PanelStockData`恒定按17列建张量、缺失列以全NaN补齐（越界不可能）；真实运行统一17列口径（`--no-fundamentals`仅限作用域化合成面板）。登记见[FEATURE_SET_EXTENSION_20261017.md](../../output/research/p2_71/FEATURE_SET_EXTENSION_20261017.md)。
- 旧6特征checkpoint不能在新17特征空间续跑（宽度不一致），仅作历史归档。

#### 4. PIT 数据层审计

- 拉取：退市表339只、指数权重（000905.SH/000300.SH，103,200行）、停复牌65,487行（2013-2026，S/R类型）；60只面板的raw日线/涨跌停/复权因子全量到位（399个数据单元）。残存62个`daily_raw`失败条目为历史全量扫描遗留（非研究universe条目为主），全市场刷新时清理。
- 审计（[pit_v1/REPORT.md](../../output/research/p2_71/pit_v1/REPORT.md)）：面板122,870行；涨跌停/原始收盘/ST/停牌标注覆盖率均100%；收盘涨停870天中866天（99.54%）原始涨跌幅落在±5/10/20/30%规则±0.1pp内；面板ST=0、官方停牌1行；中证500成分快照129期（2016-01至2026-09），19只面板股曾入选；qfq年度基期跳变>0.5%为0/60（2017-2025窗口；2026-09基期变动区不在审计窗口、也不在任何挖掘窗口内）。

#### 5. 外层 walk-forward（框架演示）

- 修正：valid切分按标签期限回切32个交易日、严格不进入test（与pilot的±2月padding为**有记录的口径差异**，两套结果不可直接对比）；每窗口面板只用test开始前数据按流动性选样（严格PIT）；张量切片前补110/后补30个交易日；空池窗口记录`eval_error`跳过（不再崩溃）。
- 演示（[walk_forward_v1/REPORT.md](../../output/research/p2_71/walk_forward_v1/REPORT.md)，20,000步/窗口×10窗口×seed0）：窗口均值test RankIC **−0.0307**（std 0.1036，范围−0.1755..+0.1291），正IC窗口50%，跨窗口t=−0.94。此为框架验证演示；全量外层评测用`--steps 2000000`。

#### 6. 2M 步全量训练（已启动）

- 门槛满足：完整状态快照机制就位（25,000步间隔、保留2份）；探针记录→[budget_estimate.json](../../output/research/p2_71/alphaqcm_2m_v1/budget_estimate.json)：1,500步探测1.845 s/1k步、worker峰值RSS 528MB、外推约1.03小时（探针前段为随机相位，偏低；v3三个qcm参考run稳态实测2.3–3.2 s/1k → 全量约1.3–1.8小时为参考区间）。
- 已detached启动（[launch.json](../../output/research/p2_71/alphaqcm_2m_v1/launch.json)：pid 65983，日志`logs/run_20261017_073956.log`），可`--resume`续训；test指标在artifact哈希冻结后由独立进程评测；如中断，产物按partial归档、不外推。

#### 7. 验证

- `pytest test/test_p2_71_alphaqcm_checkpoint.py test/test_p2_71_alphaqcm_isolation.py -q`：14 passed；`test/test_p2_71_pit_data.py`+`test/test_p2_71_fundamental_data.py`：14 passed。
- `py_compile`：pilot/equal-evals/2M/walk-forward全部通过。
- 端到端完成：walk-forward 10窗口、PIT审计、equal-evals v3（12 runs）、2M探针。
- 顺手修复equal-evals报告模板中的步数硬编码typo（`20k步`→实际步数；仅影响后续run文案，v3已出报告数字不变）。

#### 8. 剩余

- 2M run完成后的冻结test评测（runs.jsonl）与结论归档；
- 全市场PIT刷新（先删`universe.json`重新生成股票池universe；清理残存失败条目）并重跑审计；
- 等评价/等池饱和结论需更高预算或更大面板重复验证；walk-forward的2M预算版本；成本后组合回放与前向影子沿用既有边界。
