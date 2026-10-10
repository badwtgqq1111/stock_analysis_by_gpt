# 策略池：每日名单与中证500绩效口径

## 五个策略

| 策略 ID | 当前阶段 | 每日名单 | 历史归档 |
|---|---|---|---|
| `cn_ensemble` | production | `cn_ensemble_selected.csv` | `selection_history/` |
| `arm_select` | production | `arm_latest_selection.csv` | `arm_selection_history/` |
| `arm_concept` | production | `arm_concept_selection.csv` | `arm_concept_history/` |
| `jq_multi_factor_size_roe` | candidate | `jq_multi_factor_selection.csv` | `jq_multi_factor_history/` |
| `jq_roe_roa_diagnostic` | candidate | `jq_roe_roa_selection.csv` | `jq_roe_roa_history/` |

多因子名单使用历史沪深300成分、连续63交易日正成交量/收盘价、信号日以前已公告的最近财报 ROE、信号日总市值；按 `rank(roe)-rank(market_cap)` 选前20股，各 5%。它不是动态 LLM 选出的 `roe_only`，也不进生产订单。`select_jq_multi_factor.py` 默认在最新中证500交易日生成名单；同日重跑覆盖当日归档。历史回放另按15交易日调仓，不能把逐日新名单当成同一条回放账户。

动态诊断候选按 `rank(roe)+0.25*rank(roa)` 单独生成20股等权名单，ROE/ROA使用相同的严格公告日PIT。百分位在合格股票池计算，再剔除缺失得分，与研究表达式一致。默认 `--variant both` 生成两份互不覆盖的名单；也可指定 `original` 或 `dynamic`。这项每日研究步骤不接入生产订单或改变生产权重。

动态候选全窗累计 **174.83%**，高于 ROE-only 的 **160.72%** 和原低市值+ROE的 **2.61%**。此前只展示原版漏掉了动态诊断候选，现新增独立候选卡，不用原版名称套动态曲线。全窗最高不等于升生产：训练2015—17半窗相对ROE-only为 **-2.50个百分点**，未过预设双半窗稳健门槛；2021—2023验证累计 **-52.29%**，同期中证500 **-16.25%**。冻结研究选择仍为 `rank(roe)`；动态项仅用于前瞻观察。

## 曲线与指标

`build_strategy_pool_performance.py` 生成 `output/strategy_pool/performance.json`；`sync_strategy_pool.py` 把它嵌入策略池 API。所有曲线基准均为 `000905.SH` 中证500**价格指数**，按各自起始日归一为1。

- **名单跟踪毛收益**：只读完整归档、取末尾连续交易日信号。收盘信号于下一交易日开盘按目标权重再平衡，剩余权重留现金；缺持仓价格即报 `data_gap`，不填零。此为估计跟踪，不含手续费、滑点、涨跌停、停牌执行、整手及账户现金处理，非实盘净收益。
- **同窗比较**：五个名单跟踪都有已实现收益时，截取共同起止日期重新归一；共同历史不足则不生成跨策略排名。
- **独立研究回测**：原版使用 `jq_multi_factor/nav_original_size_roe.csv`，动态诊断项使用 `jq_multi_factor_dynamic/nav_dynamic_diagnostic.csv`；均为2015-01-05—2026-10-09 的15交易日调仓研究回放，含原研究费用假设。两项的来源、表达式、曲线和方法说明分别标注，与逐日名单跟踪分标签展示，不能直接把它们与其余三条短期曲线排名。
- **指标**：累计收益、最大回撤始终按可用观测计算；达到252个收益观测才计算年化收益、卡玛比、无风险利率按0的夏普。卡玛为年化收益 / `abs(最大回撤)`；若最大回撤为0则留空。相对基准累计超额为 `策略期末净值 / 中证500期末净值 - 1`。

当前归档只覆盖少量交易日：生产三策略只有1—2个**连续可实现收益**，多因子逐日名单尚没有次日行情。页面展示状态而不虚构长期业绩。需继续积累完整每日名单，并补齐执行成本/交易约束及样本外检验，才适合据收益、回撤和卡玛讨论升生产。
