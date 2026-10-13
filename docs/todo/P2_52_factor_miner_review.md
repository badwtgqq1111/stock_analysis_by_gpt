# P2.52 参考 FactorMiner（github.com/CharlesJ-ABu/FactorMiner）的评审与落地

编写：2026-10-12。触发（用户原话）：**"另外参考 https://github.com/CharlesJ-ABu/FactorMiner.git"**。
评审方式：浅克隆到 `/tmp/factor_miner_ref_*`（V4，13.6k 行 Python + React WebUI + FastAPI），
读 README / PRD / `core/inspector` / `core/evaluation` / `core/miner` / `core/storage` / `tests`，
逐条对照本仓库，**只抄能验证的部分**。

## 0. 它是什么（一句话）

面向**单标的/少标的、时序为主**的因子挖掘工作台：配置驱动引擎 + CLI/WebUI，
四种挖掘范式（**GP / RL / LLM / NN**）统一输出 `FactorExpression`，
每个因子落 `factor_db` 档案（逻辑、指标、来源、NN 权重），Inspector 做多维审查。

## 1. 对照表（它有 / 我们有）

| 维度 | FactorMiner | 本仓库 | 结论 |
|---|---|---|---|
| 目标市场 | 加密永续/单标的（CCXT 命名 `BTC/USDT:USDT`）、时序滚动 IC | A 股全市场横截面（5,224 只 / 日频 / T+1 整手） | **口径不同，不能直接搬指标** |
| 挖掘范式 | **GP / RL / LLM / NN 四范式** + 用户可扩展 Miner | LLM 提议（`llm_factor_mining.py`）+ 算子白名单 + 表达式树 | 我们缺 GP/RL/NN 三个范式（见 §3） |
| 算子 | `custom_operators/`（技术/数学）+ 动态加载 | 14 个白名单算子 + 表达式树 + **安全求值时点** | 各有千秋（我们的时点纪律是硬要求） |
| 评估 | IC / RankIC / **Turnover** / fitness（**可插拔 Fitness Hook**） | IC / ICIR / NW t / BH-FDR / HLZ t≥3 / 覆盖 / 簇冗余 | 我们多重检验更严；**缺 fitness 可配置与 turnover 口径** |
| 因子审查 | **Inspector**：滚动 IC、分位平均未来收益、换手、数据血缘，支持 2–5 因子对比 | 因子库列表（1431 条带分类/描述/性能），**没有单因子详情页** | **本轮抄这一条（已落地）** |
| 去重 | 源代码 **MD5 硬去重**；**相关性软去重未实现**（它自己写在"已知边界"里） | `factor_ic_protocol` 做抽样秩相关聚类（|ρ|≥0.9 归一簇）+ 每簇留代表 | **这一点我们更强**（评审里明确记下） |
| 档案与复现 | `factor_db/metadata/*.json` + 评估切片快照（timestamp/factor/forward_return），重启不丢 | `output/factor_library/factors.json`（指标齐，但**没有值快照**） | 我们缺"值快照"级的复现（见 §3） |
| 前端 | Dashboard / Launchpad（任务）/ Inspector / Data Center，**声明"不生成虚构曲线"** | 投研执行台（四层 + 两族 + 轨迹 + 可点动作 + 挖掘工作台 + 因子库 + 因子审查） | 我们多了闸门/影子/模拟盘这条**决策链** |
| 纪律 | README 明写"指标以落盘结果为准，不由前端模拟" | 同一纪律 + 更硬：负结果归档、闸门 ≥20 期、样本不足不下结论 | 一致，我们的判据更严 |

## 2. 本轮落地了什么（抄 Inspector 的维度，口径按本仓库）

新增 `scripts/factor_inspector.py` → `output/factor_library/inspector/<factor_id>.{json,md}` + `index.json`：

| 维度 | 定义（可复算） |
|---|---|
| `ic_by_horizon` | h ∈ {1,5,20} 日频**横截面秩 IC**：均值 / ICIR / NW t / 正号占比 / 期数 / 窗口（每日 ≥100 只股票） |
| `decay` | h1 与 h20 的比值（**不做半衰期外推** —— 我们没有足够 horizon 支撑插值） |
| `rolling_ic` | 滚动 12 期 IC 均值曲线（画图用） |
| `quantiles` | 5 档等频分位组合的**平均未来 20 日收益**（原始、未扣成本）+ 多空 Q5−Q1 均值与胜率 |
| `turnover` | Q5 组成分的期均更替率 |
| `redundancy` | 与**准入因子**（|ICIR| 前 40）的抽样秩相关，阈值 |ρ|>0.7 判冗余 |
| `lineage` | 面板窗口 / 行 / 日 / 股 / 覆盖率 / PIT 有效占比 / 标签定义 / 脚本 sha256 |

接口与前端：`GET /api/agent/factors/{id}/inspector`；动作 `factor_inspect`（因子 id 白名单）；
因子库表格**点一行** → 详情面板（滚动 IC 曲线 + 5 档分位条 + 血缘 + 冗余告警），没有档案时显示命令并给「跑审查」按钮。
实测：`tr_volatility_20_clean` h20 IC −0.0631 / ICIR −1.55 / NWt −21.4，5 档单调（Q1 +2.52% → Q5 +1.27%），
多空 −1.25%（胜率 17%），Q5 换手 9.8%，与 `vr_volatility_20_clean` 的 |ρ| = 0.812 ⇒ **冗余**。

**踩坑记录（写进测试）**：第一版把面板的 `pv_return_*_clean` 当标签 —— 那是**特征**（过去收益），
于是 moneyflow 的 h20 IC 算成 **+0.2565**，而因子库记录的月频 IC 是 **−0.139**（符号相反）。
修法：标签改为**自建前向收益** `close(t+h)/close(t) − 1`（与流水线 `label_mode = forward_return` 同口径），
并加不变式测试（自建标签的 IC 必须与因子库月频 IC **同号**）。

## 3. 值得抄但要排期的（尚未做）

1. **可插拔 Fitness Hook**（他们的 `user_workspace/custom_fitness/`）：把"因子好坏"从写死的 |IC| 变成可配置
   （权重 + 冗余惩罚 + 换手惩罚 + 衰减惩罚），落 `config/factor_fitness.json`，因子库里记 `fitness_score`。
2. **值快照级复现**：他们的 `factor_db` 存了与评估切片对齐的 `timestamp/factor/forward_return`，
   重启/换机器都能复算。我们只存指标 → 建议对**入池候选**存一份值快照（或存"重算命令 + 数据版本"的哈希）。
3. **GP / RL 范式（谨慎）**：他们的 GP 用遗传规划搜表达式，本仓库的纪律要求"搜索空间有界 + 多重检验预算"，
   所以只适合做成**受限实验**（算子白名单 + 训练窗口内搜索 + 样本外只评估 + DSH 计数），先不接生产。
4. **NN 因子（他们存权重与通道）**：与本仓库 B2 的 Transformer/CNN 重叠；他们多了"权重与通道落盘"的复现纪律，
   可以把这一条补进模型卡（`output/models/cn/<model>/card.json`）。
5. **Inspector 的 2–5 因子对比**：我们目前一次看一条；多选对比（滚动 IC 叠加）值得做，但要先有值快照/缓存。

## 4. 不抄的（明确理由）

* **加密/单标的时序口径**（CCXT 命名、8 线程固定评估）：与 A 股横截面/T+1 完全不同，指标不可比；
* **源代码 MD5 去重**：我们已有更强的相关性聚类，MD5 只能挡"完全一样"的表达式；
* **"四个范式"的范式繁荣**：本仓库已有负结果档案（lambdarank、状态门禁、超额标签），
  在没有可比较判据前**不引入新范式**，否则就是给多重检验再加一个自由度；
* **前端自己算指标**：他们明确不做，我们更不做（所有曲线都来自产物）。
