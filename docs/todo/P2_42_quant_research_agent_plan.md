# P2.42 量化投研 Agent：从「四层 + 智能体执行台」到可运行的循环

编写：2026-10-11。关联：`P1_25`（LLM 因子挖掘）、`P2_28`（每日自动化）、`P2_40`（上线闸门）、
`P2_41`（参考账户）、[`结论台账.md`](结论台账.md)、`docs/runbooks/cn-data-pipeline.md`。

**第二版（2026-10-11）**：用户补充"这个设计应该分成两类，一类是规则类策略；一类是因子流水线，
包含现行组合、非线性组合（LightGBM）" ⇒ 新增 **§0.4 两族分类**、**§2.2 族 A 的 6 步事件循环**、
§1 的按族重排表、§3 的对照行、§4 的分族口径、§5 的"不混口径排名"。

**落地进度（2026-10-11 第四十四轮）**：

* **§4.2 因子库注册表 —— ✅ 已落地**：`scripts/sync_factor_library.py` → `output/factor_library/{factors.json,factors.md}`。
  把五处来源合成**一张表**：协议层 1,366 个候选因子（含准入/剔除判决与簇冗余）、本次准入清单（A/B 两档 + 判据 + 与上次的差额）、
  LLM 会话里的提议（表达式 + 经济解释）、LLM 逐轮提议、以及**族 A 的条件**（与策略池同一份登记）。
  `kind=factor|condition` 两种；`status` 直接跟随产物判决（`admitted / rejected / excluded / proposed / candidate / in_use / retired`），
  **不自己再判一次**（准入清单是唯一口径，`test/test_factor_library.py` 会断言两者逐字一致）。
  实测：1,431 条 = 因子 1,424 + 条件 7；准入 100（A 58 / B 42）、提议 58、剔除 573（其中"同簇冗余"101）、质量列 693。
  每日自动化在 `ic-protocol` 之后自动刷新（`DAILY_FACTOR_LIBRARY=0` 可关）；接口 `/api/agent/factors` 支持 kind/status/关键词筛选与分页。
* **页面合并 —— ✅ 已落地**：「因子挖掘」并入「投研执行台」，`/agent` 变成三个标签页：
  **执行台**（四层 + 两族树 + 运行轨迹 + 门槛）、**因子挖掘**（会话工作台 + 因子库）、**提示词 / 人设**。
  老路由 `#/factor-mining` 重定向到 `#/agent?tab=mining`（与 `/portfolio` → `/performance` 同一处理），导航里的「因子挖掘」撤掉。
  合并理由：这三个问题以前分散在两个页面，而"挖出来的因子"与"跑出来的轨迹"本来就是同一件事的两端（因子挖掘也是一种 agent run）。
* 预检清单 24 → **25 个文件、229 passed**。

**落地进度（2026-10-11 第四十三轮）**：

* **§4.1 `agent_run` 运行轨迹 —— ✅ 已落地**：`scripts/agent_run.py`（`run` / `summarize` / `list` / `show`），
  产物 `output/agent_runs/<run_id>/{run.json,run.md}`（`run` 模式另有 `console.log`）；每日生产末尾自动
  `summarize`（`DAILY_AGENT_RUN=0` 可关、`DAILY_AGENT_RUN_BUDGET_SECONDS` 默认 25200s）。
  实测 2026-10-10 那条真实轨迹：**52 阶段 / 47 ok / 1 failed（notify）/ 4 warn**；门槛表
  `preflight=ok`、`data-completeness=warn`、`strategy-pool=warn`、`web-freshness=warn`、
  `promotion-check=ok`、`execution-parity=ok`、`ic-protocol=ok`；墙钟 **18,243s**（20:31:41 → 01:35:44）。
* **§4.6 前端「投研执行台」—— ✅ 首版落地**：路由 `/agent`（导航「研究」第一位）+ `backend/api/agent.py`。
  左=**两族树**（族 A 事件/公式/手臂三条支线、族 B 线性+非线性，逐条带新鲜度）、
  中=**运行轨迹时间线**（点开看阶段表 / 门槛表 / 回滚命令）、右=**提示词面板**。
* **提示词/人设（用户点名要的）**：默认角色 **「量化领域专家」**，可在页面上改角色、详细程度、系统提示词；
  保存即写 `config/agent_profile.json`（旧版本自动备份到 `output/agent_runs/profile_history/`，`POST /api/agent/profile/reset` 恢复默认）。
* 顺手关掉第四十一轮记的那颗红灯：`sync_strategy_pool.py` 的 `fresh` 从 `latest == reference` 改成 `latest >= reference`
  （新增 `is_fresh()` + 单测），预检清单 22 → **24 个文件、219 passed**（此前 196 passed + 1 failed）。
* 仍未做：§4.2 因子库注册表、§4.3 模型卡、§4.4 `promote_candidate.py`、§4.5 `pre_order_check.py`；
  §4.6 的"阶段清单可点击触发"未做（当前只读）。

参考对象：聚宽（JoinQuant）AI-投研页面设计图（用户 2026-10-11 提供；页脚自注"以上仅为示例演示"）。
画的是 **四层**（因子层 / 模型层 / 优化层 / 决策层）+ 每层的阶段流水线 + **智能体执行台**
（终端风格命令 `jqfactor_loop run`、环形进度"迭代次数 55 / 第 6 步因子构建 / 7 步一圈"、
右侧当前步骤的三个子任务清单）。

> 一句话：**骨架值得借，判据必须自己补。** 它的"分层 + 显式循环 + 执行台"比一个聊天框强得多；
> 但它**没有证伪机制**（门槛 / 预算 / 回滚 / 审计），而仓库里这些能力**大部分已经实现**，
> 缺的是把它们装进一个状态机、暴露成一条可回看的轨迹。

---

## 0. 对这份设计的评估（先给结论）

### 0.1 做对的三件事

| # | 设计元素 | 为什么对 |
|---|---|---|
| D1 | **四层分层**（因子 / 模型 / 优化 / 决策） | 投研的失败模式按层可归因：因子没信息量、模型过拟合、组合没风控、执行吃成本。分层才谈得上"定位问题在哪一层" |
| D2 | **每层有固定阶段流水线**（因子层 7 步：研究分析→提出想法→开发准备→开发实现→因子分析→因子构建→因子入库） | 把"研究"从黑箱变成**可预期的过程**；每一步都能定义输入/输出/判据 |
| D3 | **执行台**：命令 + 当前步骤 + 子任务清单 + 进度 | 信任来自**可观测的中间状态**。用户能看到"它现在在干什么、上一步的产出是什么"，而不是只看一句结论 |

### 0.2 缺的（也是危险的）四件事

| # | 缺口 | 风险 | 本仓库的对策（已有 → 待接线） |
|---|---|---|---|
| G1 | **没有门槛**：文案只说"挖掘高信息量的因子"，没有 IC/FDR/样本外/多重检验/与存量因子相关性阈值 | 把噪声当因子；跑得越多越容易"看起来显著" | 已有 `scripts/factor_ic_protocol.py`（月频秩 IC / ICIR / NW t / **BH-FDR** / Harvey-Liu-Zhu t≥3 / 稳定性 / 冗余聚类）+ 试验次数台账（DSR，见结论台账 A6）→ 待接进循环的**第 5/6 步闸门** |
| G2 | **"迭代次数 55 + 持续运行"把"活动"当"进展"** | 用跑的次数与时长代替证据；无人知道"离能用还差什么" | 改成展示**门槛通过情况**（本次 12 个提议：11 过 FDR、2 过 DSR…），不展示迭代次数；不常驻，按调度跑一次并留痕（`run_daily_production.sh` + `production.done` 就是答案） |
| G3 | **入库是单向的**（因子入库/模型迭代都没有"淘汰与回滚"分支） | 坏因子/坏模型只进不出；口径变更无法追责 | 已有先例：`config/strategies.json` 的 `stage: candidate/research/production/retired` + `promotion_check.py` 的 **KEEP/PROMOTE/REJECT** → 待做成"登记 + 淘汰原因 + 一键回滚" |
| G4 | **决策层直连实盘**（获取订单/拆单执行/成交监控）而画面只有"持续运行" | 实盘是唯一不可逆的一层：缺预算、缺清单、缺停机 | 已有：`run_cn_paper_account`（模拟盘）、`export_ths_watchlist.py`（导出自选）、`execution_assumptions_cn.json` + `execution_parity_check.py`（执行假设一致性）→ 实盘接入**单独一阶段**，且先过"下单前 checklist" |

### 0.3 一句话结论

> 借它的**骨架**（分层 + 显式循环 + 执行台），补它的**四件**（门槛 / 预算 / 可回滚 / 审计）。
> 我们不需要重写能力，需要的是**把已有能力装进一个状态机**：一次运行 = 一条轨迹 = 一张判决书。

### 0.4 本仓库必须分两族：规则类策略 / 因子流水线（用户 2026-10-11 补充）

聚宽那张图**只画了一条线**（因子层 → 模型层 → 优化层 → 决策层），那是"因子流水线"的走法。
但仓库在册的 8 条策略里有一半**根本不产生因子**：它们是**事件/条件触发的规则**（首板低吸、箱体突破、
行业/概念手臂）；另一半才走"因子 → 组合"的流水线，而流水线内部还分**线性**与**非线性**两支。
所以本方案的骨架不是一条流水线，而是 **两族入口 + 一条共享的优化/决策脊柱**：

| 族 | 支 | 在册条目（`config/strategies.json` + `output/strategy_pool/candidates/`） | 核心判据 | 产物 |
|---|---|---|---|---|
| **A 规则类策略** | 事件规则 | `first_ban_lowxi`（首板涨停+回调+企稳+低吸；**已证伪**，反例档案）、`box_arm`（横盘→首板→回调→箱体突破） | **逐笔净期望 > 0** + 按决策日聚类 NW t + 参数敏感性 + 容量 | `output/research/first_ban_lowxi/`、`output/research/box_breakout/`、`output/results_cn/box_arm_history/` |
| | 打分规则（公式，不训练） | `jq_multi_factor_size_roe`（低市值+高ROE）、`jq_roe_roa_diagnostic`（ROE＋0.25×ROA） | 分位单调性 + 组合级闸门（跑 `promotion_check.py`） | `output/results_cn/jq_multi_factor_selection.csv`、`output/results_cn/jq_roe_roa_selection.csv` |
| | 手臂规则（行业/概念/宽度） | `arm_concept`（概念手臂）、`rot_arm`（MST 大类 + 7 因子等权，最小大类 ≥8 行业）、`arm_select`（小市值手臂） | 同窗口逐期配对显著性 + 集中度 + 名单宽度 | `output/results_cn/arm_concept_history/`、`rot_arm_history/`、`arm_latest_selection.*` |
| **B 因子流水线** | **B1 线性组合**（现行组合的线性那一半） | `base_v1`（冻结 25 列 IC 加权线性合成）、`llm_new`（LLM 因子集：符号 + ICIR 绝对值权重）、`combo_50_50`（两条 z 分等权混合） | 月频秩 IC / ICIR / NW t / **BH-FDR** / 与存量字段相关性 + 组合级对照 | `output/factor_mining_sessions/`、`output/research/llm_factor_mining_*/sleeve_oos_*/arms_summary_daily.csv` |
| | **B2 非线性组合** | LightGBM / Transformer / CNN 打分（`run_cn_pipeline.py --stage model_scores`）；生产 `[portfolio] model = "ensemble"`，实测 `ensemble_weights = {lightgbm = 1.00, transformer = 0.00}`（2026-09-29 起 transformer 归零）⇒ **今天生产跑的就是 LightGBM 这一支** | 同 B1 + 样本外 IC/夏普 + 与上一版逐项对照（模型卡） | `output/models/cn/{lightgbm,transformer,cnn}/` |
| 共享脊柱（两族同用） | 优化 → 决策 | 影子组合 → 上线闸门 → 模拟盘/参考账户 → 自选导出 | `KEEP/PROMOTE/REJECT`（`promotion_check.py`）+ 两引擎执行假设一致（`execution_parity_check.py`） | `output/paper_trading/shadow/`、`output/results_cn/promotion_check/`、`output/results_cn/ths_watchlist/` |

**为什么必须分开**（三处真实差异，不是分类癖）：

1. **判据不同**：族 A 的证据单位是**一笔交易**（单笔毛/净期望、事件样本数、按日聚类显著性），
   族 B 的证据单位是**一个截面/一个组合**（秩 IC、ICIR、逐期配对的超额）。拿 IC 去评价"首板低吸"是错的，
   拿单笔期望去评价"25 列线性合成"同样是错的。
2. **失败模式不同**：族 A 死于"事件变少 / 成本上升 / 拥挤"——`first_ban_lowxi` 把文章的 8 个条件全叠上去
   （样本 8,412）单笔仍 **−0.265%**、扣费 **−0.575%**，再还原它的日内止盈止损（5 分钟模拟 3,279 笔）
   还是 **−0.324%** / 扣费 **−0.634%**；`box_arm` 的**参数即结论**：只有 weekly×5 那一格累计 **+28.17%**
   （同期中证500 +15.02%），换成 weekly×10 **−68.8%**、daily×5 **−76.6%**、daily×10 **−74.7%**。
   族 B 死于"过拟合 / 多重检验 / 风格切换"
   （A1：105 个月只有 2/20 过全部门槛、PBO=0.603）。
3. **回滚粒度不同**：族 A 回滚是"撤掉一条规则/一张名单"（`stage=retired`），族 B 回滚是"退回上一版模型/因子集"
   （模型卡 + 因子库注册表里记 `retired_reason`）。

两族的**入口不同、脊柱相同**：都将产出同一张 `selection_ledger` 行、同一份影子组合对照、同一套闸门判决。

---

## 1. 现状盘点：四层里我们已经有什么（对号入座）

| 层 | 聚宽画的阶段 | 本仓库已有实现 | 产物 / 证据指针 | 缺口 |
|---|---|---|---|---|
| **因子层** | 研究分析 / 提出想法 / 开发准备 / 开发实现 / 因子分析 / 因子构建 / 因子入库 | `scripts/llm_factor_mining.py`（假设→表达式→安全求值→IC/NW t→BH-FDR→冗余→top-3 回灌进化，支持月频/日频/**打板口径**/行业口径）；`scripts/factor_ic_protocol.py`（准入协议 + 漂移）；`scripts/dynamic_jq_factor_mining.py` | `output/factor_mining_sessions/*.json`、`output/research/llm_factor_mining_*/round_*/`、`output/research/factor_ic_protocol/{factor_ic_admitted.json,factor_ic_delta.json,REPORT.md}` | ① 没有**因子库注册表**（表达式/来源/面板版本/指标/状态/淘汰原因）；② 没有"一次挖掘 = 一条 trace"（现在散在目录里） |
| **模型层** | 模型设计 / 训练 / 评价 / 迭代 | `run_cn_pipeline.py --stage features/clean_panel/model_scores`（LightGBM / Transformer / CNN） | `output/models/cn/{lightgbm,transformer,cnn}/` | 缺**模型卡**：数据窗口、特征集、超参、样本外 IC、与上一版的对照、何时该退役 |
| **优化层** | 组合优化 / 风险归因 / 持续监控 | `portfolio_strategy/`、`promotion_check.py`（上线闸门：窗口可比 / 逐期配对 / 集中度 / 收益集中度）、`run_shadow_variants.py`（影子组合，含参考账户）、`pk_guard_report.py`、`execution_parity_check.py`、`build_forward_validation.py` | `output/results_cn/promotion_check/<ts>/{verdicts.csv,report.md}`、`output/paper_trading/shadow/{summary.json,<variant>/nav.csv}`、`output/results_cn/pk_guard/latest.json`、`output/verification/execution_parity.json` | 缺**自动串联**：候选 → 影子 → 闸门 → 判决 → 写回策略池，现在要人工按顺序跑 |
| **决策层** | 获取订单 / 拆单执行 / 成交监控 / 绩效反馈 | `run_cn_paper_account`（T+1 开盘、整手、涨跌停/停牌/退市显式建模）、`export_ths_watchlist.py`（四个源导出）、`notify_selection.py`（飞书/邮件/webhook） | `output/paper_trading/account*/`、`output/results_cn/ths_watchlist/` | 无实盘接入（**现阶段刻意不做**）；缺"下单前 checklist + 拒单原因归属" |
| **贯穿** | （页面上没有，但实际决定体验） | 策略池（`config/strategies.json` + 策略池页：列表/详情/公共显示项/优劣/对比曲线）、每日自动化（`run_daily_production.sh`，含预检硬门）、选股日历（台账 + 每源字母）、K 线页（个股资料/自选分组/方向徽章/AI 多空分析） | `output/strategy_pool/strategy_pool.json`、`output/results_cn/selection_ledger.parquet`、`output/pipeline_reports/daily/<date>/run.log` | 缺一个"**投研执行台**"页面把这些串成一条轨迹 |

**按 §0.4 的两族重排**：上面这张表是"按层"看的，四层在**族 A** 与**族 B** 上的落点并不一样 ——

| 层 | 族 A（规则类策略）落点 | 族 B（因子流水线）落点 | 两族共同的缺口 |
|---|---|---|---|
| 因子层 | **弱**：规则里的条件就是"因子"（首板/回调/箱体/市值分位），但没有因子级别的注册与 IC 体检（`factor_ic_protocol.py` 只吃族 B 的候选） | **强**：`llm_factor_mining.py` + `factor_ic_protocol.py` 全流程在跑 | 没有"两族共用的因子/条件**注册表**"（族 A 的条件现在只写在脚本里） |
| 模型层 | 不适用（规则不训练） | LightGBM / Transformer / CNN + `model = "ensemble"` | 缺模型卡（B2 换模型时无对照记录） |
| 优化层 | **同一套**：影子组合 + 闸门（`box_arm` / `rot_arm` 都已在 `run_shadow_variants.py` 里作为变体） | **同一套** | 缺自动串联（候选→影子→闸门→写回策略池仍要人工按顺序跑） |
| 决策层 | 只出名单（进日历/自选/AI 分析） | 生产名单进模拟盘 | 缺"下单前 checklist + 拒单原因归属" |

**已经在生产里跑过一轮（不是 PPT）**：trade date 2026-10-10 的每日自动化在 2026-10-11 01:35:44 打出 `done`
（`output/pipeline_reports/daily/2026-10-10/run.log`），其中 `data-step=jq_multi_factor ok`、
`data-step=rot_arm ok (33s)`、`data-step=ai_picks ok`（写出 `ai_pick_analysis/2026-10-10.json`）、
`ledger ok`、`shadow-variants ok`、`promotion-check ok` 都在链路里。

---

## 2. 建议的 Agent 循环：两族各一条闭环（族 B 的 7 步 IC 循环 / 族 A 的 6 步事件循环）

### 2.1 族 B（因子流水线，含线性/非线性组合）：带判据的 7 步

```
① 研究分析  输入：选题 + 数据概况        动作：写 hypothesis（必须含"可证伪的预测"）
② 提出想法  输入：字段菜单 + 上一轮 top-k  动作：LLM 产出 N 个因子/规则表达式（JSON）
③ 开发准备  输入：表达式                   动作：字段是否在面板、PIT 检查、覆盖率/缺失率
④ 开发实现  输入：表达式                   动作：白名单求值（禁 dunder/import/未来函数）
⑤ 因子分析  输入：因子值 + 标签            动作：秩 IC / ICIR / NW t / BH-FDR / 与存量因子 |ρ|
⑥ 因子构建  输入：通过⑤的因子             动作：组合级对照（影子组合 + 上线闸门：配对显著 + 前后半窗同号 + 集中度）
⑦ 入库      输入：过闸门的因子/策略        动作：登记（含来源与证据指针）→ 状态机 candidate→research→production→retired
```

**每一步的判据（必须机器可判，不能靠人看）**

| 步 | 通过条件（示例，均可调参但必须固定并留痕） | 不通过怎么办 |
|---|---|---|
| ③ | 字段全部存在；覆盖率 ≥80%；`ann_date < trade_date`（PIT） | 驳回提案，并把原因回灌给 LLM（这是进化信号） |
| ④ | 表达式安全求值成功；无 NaN 泛滥（缺失 <30%） | 同上 |
| ⑤ | `abs(IC) ≥ 0.02` 且 `NW t ≥ 3` 且 **BH-FDR q=0.05 通过** 且与存量字段的**最大相关系数 ≤ 0.7** | 记入"未通过"清单（**也是产物**，不是丢弃） |
| ⑥ | 闸门四项全过：窗口可比（重叠期 ≥20）、逐期配对显著、单票 ≤35%、收益集中度可接受 | 记 `KEEP`（继续观察）或 `REJECT`（带原因） |
| ⑦ | 人工确认（升 `production` 必须人工） | 保持 `candidate`，进池观察 |

> 关键改动相对聚宽那张图：**"因子入库"不是终点而是登记点**；**"迭代次数"不是指标，"过了哪一档门槛"才是**。

### 2.2 族 A（规则类策略）：带判据的 6 步事件循环

族 A 的产物是**名单与逐笔证据**，不是因子值，所以循环短一步、判据整体换掉（用"笔"而不是"截面"）：

```
S1 事件定义  输入：规则描述        动作：把条件写成可用日线/5 分钟**复算**的判据（禁止"看图"），并把事件池落盘
S2 样本与口径 输入：事件池 + 价格   动作：事件样本数、T+1 基准价（必须用 T 日收盘）、停牌/涨跌停/一字板可成交性
S3 逐笔期望  输入：事件 × 次日/持有期收益  动作：毛期望、**净期望**（减双边成本：佣金2.5bp+滑点3bp+印花10bp）
S4 稳健性    输入：参数网格        动作：按决策日聚类 NW t、参数敏感性、容量、分年/分市值分层
S5 影子+闸门 输入：名单序列        动作：影子组合对照 + 上线闸门（`promotion_check.py`：同窗口逐期配对 + 集中度）
S6 登记      输入：过闸门的规则     动作：写 `config/strategies.json`（`kind=rule_strategy` / `research_arm`），人工确认
```

| 步 | 通过条件（机器可判） | 不通过怎么办 |
|---|---|---|
| S1 | 条件能用现有数据字段复算；事件池落盘（行数可核对） | 退回改描述，不进入回测 |
| S2 | 事件样本 ≥200 笔**或** ≥30 个决策日；T+1 价格用 **T 日收盘**为基准 | 样本不足 → 一律标"样本不足"，只观察不判决 |
| S3 | 单笔**净**期望 > 0 | 负期望 → 进**反例档案**（`first_ban_lowxi` 就是这样登记的），并把原因写进摘要 |
| S4 | 按决策日聚类 NW t ≥ 3 且参数不乱（同族参数不同量级结论就翻转 ⇒ 不采信） | 参数敏感 → 只做"观察候选名单"（`box_arm` 现状），不上生产 |
| S5 | 闸门四项全过（窗口可比 / 逐期配对 / 单票 ≤35% / 收益集中度） | KEEP 继续观察 / REJECT 带原因 |
| S6 | 人工确认 | 保持 `candidate` |

> 两族唯一共用的判据是 S5/⑥：**都得跟"它自己的对照"做同窗口逐期配对**。
> 反过来说，族 A 不许用 IC/FDR 给自己背书，族 B 也不许用"逐笔很赚"给自己背书（那是选样）。

---

## 3. 逐项对照：借什么 / 改什么 / 不借什么

| 聚宽设计元素 | 判断 | 我们的落地方式 |
|---|---|---|
| **单条流水线（因子→模型→优化→决策）** | **改** | 我们是**两族**：规则类策略走 6 步事件循环（§2.2，判据是"逐笔净期望 + 聚类显著性 + 参数稳"），因子流水线走 7 步 IC 循环（§2.1）且内部分线性（B1，`base_v1`/`llm_new`/`combo_50_50`）与非线性（B2，LightGBM/Transformer/CNN）两支（§0.4） |
| 四层分层 | **借** | 用现有 `stage` 概念表达；页面顶部一行四层，点进去看当前层与最近 3 次运行 |
| 每层固定阶段 | **借** | 阶段名保留（与 §2 的 7 步一致），每阶段绑定"判据 + 产物路径" |
| 环形进度 + 迭代次数 | **改** | 不显示迭代次数；显示**本次运行的门槛通过清单**（如 `11/12 过 FDR，2/12 过 DSR`）与"差在哪一档" |
| 终端风格执行台（`jqfactor_loop run`） | **借** | 保留"入口命令"（每个策略/每个 run 都已有 `entry`），旁边加 **本次运行 trace** 链接 |
| 「持续运行」常驻 | **不借** | 我们靠外部调度（launchd/cron/timer）+ 幂等 `.done`；常驻进程"看不见地改状态"是运维反模式 |
| 右侧"当前步骤的三个子任务" | **借** | 每阶段的验收清单（可核验项：产物存在？门槛过？耗时/预算内？） |
| 决策层「获取订单/拆单执行」 | **先不借** | 决策层停在**模拟盘 + 导出自选**；实盘接入单独阶段，先过 checklist（见 §4.5） |
| （它没有）回滚 / 预算 / 审计 | **补上** | 见 §4.1（run.json 含 rollback 与预算；照仓库 verification 四件套格式） |

---

## 4. 落地 TODO（分期，每期都有验收）

**每项都要同时服务两族**（族 A 的"因子"是**条件/事件**，族 B 的是**因子值**）：
4.1 的轨迹要同时记录"事件循环的一次运行"（如 `first_ban_lowxi_study`）与"IC 循环的一次运行"（如 `llm_factor_mining`）；
4.2 的注册表同时登记因子与条件（`kind=factor|condition`）；4.4 的 `promote_candidate.py` 两族通用；
4.6 的执行台按族分组（左侧树的第二层就是"规则类策略 / 因子流水线·线性 / 因子流水线·非线性"）。

### 4.1 运行轨迹 `agent_run`（最高优先，别的都挂在它上面）—— ✅ 2026-10-11 已落地
* **做什么**：任何"一次运行"（每日生产 / 因子挖掘 / 口径对比 / 闸门 / 影子）都产出一份
  `output/agent_runs/<ts>/run.json`：`{run_id, kind, started_at, finished_at, trigger, budget:{tokens,requests,seconds}, stages:[{name,status,verdict,reason,artifacts[],command,duration}], rollback:{...}}`，
  并生成 `run.md`（人读）。
* **怎么接**：先包一层薄壳——`scripts/agent_run.py run -- <命令>`，从既有产物/日志里**抽取**阶段状态
  （不重写各脚本的日志格式）；`run_daily_production.sh` 末尾追加一次汇总。
* **验收**：跑一次 `llm_factor_mining` + 一次 `run_daily_production.sh --dry-run` → 两份 `run.json` 齐全；
  随机抽 3 条 `artifacts` 路径全部存在；`rollback` 能在**副本**上执行成功（照现有 VERIFICATION 四件套格式）。

### 4.2 因子库注册表 `factor_library` —— ✅ 2026-10-11 已落地
* **做什么**：`output/factor_library/factors.json`，每条：
  `{id, expression, source(LLM会话/人工/论文DOI), panel_version, label, ic_mean, icir, nw_t, fdr_pass, max_corr, status, first_seen, retired_reason}`。
  声明式登记 + 运行时扫描（照抄 `config/strategies.json` + `sync_strategy_pool.py` 的模式，已验证好用）。
* **验收**：登记后，`factor_library.json` 覆盖**现有所有 LLM 会话里通过 FDR 的因子**；页面上能按
  `status / |IC| / t / 面板版本` 筛选；与策略池的"证据指针"能互相跳转。

### 4.3 模型卡 `model card` —— ✅ 2026-10-11 已落地
* **做什么**：`output/models/cn/<model>/card.json`：数据窗口、特征集（面板版本）、超参、样本外 IC/夏普、
  与上一版的逐项对照、训练耗时、何时该退役。
* **验收**：流水线页能看到"当前生产用的是哪一版、什么时候训的、对上一版赢在哪"；
  换模型时必须先在闸门里出判决（禁止"直接覆盖生产模型文件"）。

### 4.4 上线流水线一条命令
* **做什么**：`scripts/promote_candidate.py <strategy_id>`：登记影子变体 → 跑影子 → 跑闸门 → 写 `verdicts.csv`
  → 输出"下一步要人工做什么"（升 stage 需要人工确认，遵循仓库既有约定）。
* **验收**：给一个真实候选（例如 `box_arm` 换口径）→ 一条命令得到 `KEEP/PROMOTE/REJECT` + run trace；
  人工确认那一步在页面上显式可点（不自动改 `config/strategies.json`）。

### 4.5 执行层：下单前 checklist（先模拟，后谨慎）
* **做什么**：`scripts/pre_order_check.py`：数据新鲜度（交易日 == 最新）、涨跌停/停牌、资金与整手、
  单票上限、与影子组合/参考账户一致性（复用 `execution_parity_check.py`）、当日名单是否已归档。
  拒单/拦截都要归因（数据 / 规则 / 资金 / 市场）。
* **验收**：checklist 在"模拟盘下单前"全绿；实盘接入**另立阶段**，且要求"连续 N 个月模拟盘对照 + 闸门 PROMOTE"。

### 4.6 前端「投研执行台」页（把上面串起来）—— ✅ 2026-10-11 落地（已并入「因子挖掘」为三标签页）
* **布局**：左=四层状态（点层看阶段）｜中=运行轨迹时间线（每次 run 一条，点开看阶段表）｜右=当前阶段清单
  （判据逐条打勾/打叉 + 命令 + 产物链接）。
* **验收**：能在页面上回答四个问题——"它现在在干什么 / 上次为什么失败 / 这个因子过了哪几档门槛 / 怎么回滚"；
  Playwright 验收（照 `output/verification/p1_*/verify_ui.py` 的写法）+ 截图。

---

## 5. 明确不做（边界）

1. **不做常驻后台**：不学"持续运行"；一律外部调度 + 幂等标记（避免看不见的进程改状态）。
2. **不自动下单**：决策层停在模拟盘 + 自选导出；实盘必须人工确认 + checklist 全绿。
3. **不用"迭代次数/跑了多久"衡量进展**：一律用"过了哪一档门槛"。
4. **不引入 Qlib 依赖**：理由与实测见 `P1_25 §1`（本机无 Qlib 数据；我们自己的长面板 + IC 协议已够用）。
5. **不让 LLM 直接改生产配置**：LLM 只能产出候选与证据；改 `config/strategies.json` 的 `stage` 必须人工。
6. **不把两族混在一张净值曲线里排名**：族 A 的证据是"逐笔期望 + 事件样本"，族 B 是"组合 IC/逐期配对超额"，
   两者口径不同（`first_ban_lowxi` 的累计 −97.62% 是**逐笔簇**口径，`cn_ensemble` 的 +x% 是**组合**口径）。
   策略池里各自展示、各自标注口径，**只在同族同口径内比大小**。

---

## 6. 风险与"不可信边界"

* 那份设计图**页脚自己写了"以上仅为示例演示"** → 不能当成能力承诺；我们只借结构，不借数字。
* **多重检验是这类 agent 的头号风险**：跑得越多，"看起来显著"的越多。所以第 ⑤ 步必须
  **固定口径 + 记录试验次数**（我们已有 DSR / trials ledger，结论台账 A6）。
* **保命规则在本仓库多次被证伪**（止损 A15/A16、择时风控 A17、ST 过滤 A18 都是负贡献）→
  agent 的"优化层"**禁止直接改生产规则**，必须先过闸门 A/B；这条要写进 agent 的系统提示与代码约束。
* **短样本不许下结论**：曲线观测 <30 一律标"样本不足"、<252 不参与年化排名（已在策略池页面落地）。
* **决策层的可逆性**：模拟盘可回滚，实盘不可。因此 §4.5 的 checklist 与 §4.1 的 rollback 是实盘前的硬前置。

---

## 7. 参考

* 聚宽 AI-投研页面设计图（用户 2026-10-11 提供）：四层 + 7 步流水线 + 智能体执行台（环形进度 / 迭代次数）。
* `QuantaAlpha`（工作区 `/Users/ccs/code/quant/QuantaAlpha`）：LLM 自进化因子挖掘（假设→代码→评估→进化，论文 arXiv:2602.07085）；
  本仓库 `scripts/llm_factor_mining.py` 借的就是这个循环范式（见 `P1_25`）。
* 关联文档：`P1_25`（因子挖掘与结论）、`P2_28`（每日自动化）、`P2_40`（上线闸门）、`P2_41`（参考账户）、
  [`结论台账.md`](结论台账.md)、`docs/runbooks/cn-data-pipeline.md`。
