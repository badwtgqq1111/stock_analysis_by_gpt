# 文献与专家实践速览：突破确认 / 底部反弹 / 回踩再突破

检索日期：2026-09-26。方法：用 OpenAlex `title.search` 逐篇核验（标题/年份/期刊/引用数/DOI 均来自 API 返回，未凭记忆书写）。
用途：为 `docs/todo/P1_23_two_rule_strategies_plan.md` 的两个策略提供文献边界与验收口径。

## 0. 一句话结论

1. **突破类**有扎实的学术证据，但证据集中在"长周期/52 周新高/时间序列动量 + 简单规则 + 过热过滤"，**不是**"任意通道突破"。
2. **反转类（底部反弹）**在 A 股是**主导效应**（中国三因子模型里甚至没有动量因子），但短期反转本身不稳定，**必须有确认条件**。
3. **"突破→回踩→再突破（retest）"在学术文献里没有直接证据**（`title.search` 检索为空）——它是实务派（O'Neil / Minervini / Weinstein 一脉）的经验构造，必须**自己用事件研究证明**，不能靠引用文献背书。
4. 止损/出场不是"越紧越好"：其有效性取决于标的的自相关结构；趋势/动量型标的适用，反转型标的上会损害收益。
5. 方法论上，"专家怎么防自欺"比"专家用什么指标"更值得抄：回测过拟合概率（PBO）、purge/embargo、元标注做 take/skip、三重障碍标签。

## 1. 突破 / 趋势跟随（含 52 周新高）

| 文献 | 年份 / 期刊 | 要点（对我们的含义） |
|---|---|---|
| Brock, Lakonishok, LeBaron, *Simple Technical Trading Rules and the Stochastic Properties of Stock Returns*（DOI 10.1111/j.1540-6261.1992.tb04681.x，cites≈2276） | 1992, JF | 移动平均/通道突破规则在百年道指上有统计显著的正向信息；奠定了"简单技术规则不是噪声"的实证基础 |
| Lo, Mamaysky, Wang, *Foundations of Technical Analysis…*（10.1111/0022-1082.00265，≈744） | 2000, JF | 用核回归自动识别形态（头肩、双底等），形态条件分布确实与随机游走不同；提示：**形态要可算法化定义**才能检验 |
| George, Hwang, *The 52-Week High and Momentum Investing*（10.1111/j.1540-6261.2004.00695.x，≈858） | 2004, JF | 接近 52 周新高的股票后续收益更强（锚定效应）；**"离高点越远越好"恰恰相反**——这与我们门禁的 `k_high` 方向相冲突（我们实测该分项 IC≈0） |
| Moskowitz, Ooi, Pedersen, *Time Series Momentum*（10.1016/j.jfineco.2011.11.003，≈1458） | 2011, JFE | 跨资产 12 个月时序动量；**趋势策略的收益与"波动率状态"相关** |
| Hurst, Ooi, Pedersen, *A Century of Evidence on Trend-Following Investing*（10.3905/jpm.2017.44.1.015，≈183） | 2017, JPM | 百年趋势跟随证据；规则极简（多窗口突破/均线）但**多品种分散**才成立 |
| Baltas, Kosowski, *Demystifying Time-Series Momentum Strategies*（10.1002/9781119599364.ch3，≈47） | 2020 | **波动率估计器与交易规则的选择显著影响表现**；单一规则对比要控制这两项 |
| Han, Yang, Zhou, *A New Anomaly: The Cross-Sectional Profitability of Technical Analysis*（10.1017/s0022109013000586，≈226） | 2013, JFQA | 横截面技术信号（动量/反转类）有显著 alpha，但**换手与成本吃掉了相当部分** |
| Gao, Han, Li, Zhou, *Market Intraday Momentum*（10.1016/j.jfineco.2018.05.009，≈226） | 2018, JFE | 日内"先动后动"延续效应；说明**时间尺度越小，动量的表现形式不同** |
| Li, Zhang, Zhou, *Do trend following strategies work in Chinese futures markets?*（10.1002/fut.21856，≈28） | 2017, JFM | 中国期货上趋势策略可行——但**股票现货**的动量弱得多（见 §2） |
| Zhu, Zhou, *Technical analysis: an asset allocation perspective…*（10.1016/j.jfineco.2008.07.002，≈300） | 2009, JFE | 均线/带宽规则在资产配置层面有价值，但**参数选择敏感**、需要样本外验证 |
| Lee, Swaminathan, *Price Momentum and Trading Volume*（10.1111/0022-1082.00280，≈1561） | 2000, JF | 动量与成交量交互：**放量确认的动量更持久**——支持"再突破需要放量" |
| Gervais, Kaniel, Mingelgrin, *The High-Volume Return Premium*（10.1111/0022-1082.00349，≈907） | 2001, JF | 异常放量后短期有正溢价；但**放量本身不等于可持续**，需与位置/趋势条件组合 |

## 2. 反转 / 底部反弹（A 股尤其重要）

| 文献 | 年份 / 期刊 | 要点 |
|---|---|---|
| Lehmann, *Fads, Martingales, and Market Efficiency*（10.2307/2937816，≈1644） | 1990, QJE | 短期反转（1 周~1 月）的系统性证据 |
| Jegadeesh, *Evidence of Predictable Behavior of Security Returns*（10.1111/j.1540-6261.1990.tb05110.x，≈2791） | 1990, JF | 月度反转：上月输家下月跑赢 |
| Nagel, *Evaporating Liquidity*（10.1093/rfs/hhs066，≈566） | 2012, RFS | 短期反转与做市商库存/流动性冲击有关——**反转的收益很大一部分是提供流动性的补偿**，冲击成本敏感 |
| Da, Liu, Schaumburg, *A Closer Look at the Short-Term Return Reversal*（10.1287/mnsc.2013.1766，≈234） | 2013, MS | 剔除"流动性冲击"成分后的反转更稳定、换手更低 → 提示：**用"是否缩量止跌 + 相对强度转正"来区分"流动性冲击"与"基本面下修"** |
| Liu, Stambaugh, Yuan, *Size and Value in China*（10.1016/j.jfineco.2019.03.008，≈878） | 2019, JFE | 中国三因子（CH-3）里**没有动量**、反转显著——A 股规则库应以反转/价值为主轴 |
| Wu, *Momentum trading, mean reversal and overreaction in Chinese stock market*（10.1007/s11156-010-0206-z，≈81） | 2010, RQFA | 中国市场上反转/过度反应主导，动量策略长期不成立 |
| Lu, Lao, *Identifying inconsistency in short-term reversal: evidence from the Chinese stock market*（10.1080/13504851.2022.2115445，≈3） | 2022, AEL | 中国短期反转**不稳定**（分时段/分状态失效）→ 必须叠加状态与确认条件，不能裸做 |
| Chu, Gu, Zhou, *Intraday momentum and reversal in Chinese stock market*（10.1016/j.frl.2019.04.002，≈31） | 2019, FRL | 中国日内同样存在动量/反转结构（时间尺度决定符号） |

## 3. 出场与风控

| 文献 | 年份 / 期刊 | 要点 |
|---|---|---|
| Kaminski, Lo, *When do stop-loss rules stop losses?*（10.1016/j.finmar.2013.07.001，≈69） | 2013, JFM | 止损的有效性取决于收益的**自相关/动量结构**：动量型标的止损有益，反转型标的止损反而降低收益 → 两个策略要用**不同**的止损逻辑 |
| Dai, Marshall, Nguyen, *Do stop-loss rules add value in international equity market allocation?*（10.1080/00036846.2021.1980201） | 2021, Applied Economics | 跨国股票配置里止损价值有限，且依赖市场状态 |
| Daniel, Moskowitz, *Momentum Crashes*（10.1016/j.jfineco.2015.12.002，≈938） | 2016, JFE | 动量在"熊市反弹"中会崩溃；**趋势/突破策略必须做波动率或市场状态过滤**（对应我们 `require_market_trend_up` 与市场换手 Z 上限） |

## 4. 方法论（专家如何避免自欺）

| 文献 | 年份 | 要点 |
|---|---|---|
| Bailey, Borwein, López de Prado, *The Probability of Backtest Overfitting*（10.21314/jcf.2016.322） | 2016 | 用 PBO 量化"回测挑参数"的过拟合概率；多次试验要登记 |
| López de Prado, *Advances in Financial Machine Learning*（2018 书，≈360 引用） | 2018 | 三重障碍标签、元标注（take/skip）、purged CV —— 正是我们 P1.22 §13/P1.23 的 A1/A2 设计 |
| Gu, Kelly, Xiu, *Empirical Asset Pricing via Machine Learning*（10.1093/rfs/hhaa009，≈2514） | 2020, RFS | 树/神经网络在横截面上有增量，但**必须用同一 OOS 口径对照线性基线**；特征全量入模、不做全样本单因子预筛 |

## 5. 检索为空的重要发现

以下方向在 OpenAlex 标题检索里**没有对应论文**（多次改写查询仍为空）：
`resistance level retest breakout confirmation`、`volume price breakout confirmation intraday`、`support and resistance levels technical analysis profitability`、
`technical trading rules Chinese A-share market profitability`、`investor attention and stock reversals in China`。

含义：**"回踩后再突破确认"缺乏学术证据**，属于实务经验；我们能做的只有：①把条件写得可检验（回踩幅度/缩量比/再突破放量/不破突破位）；②自己跑事件研究拿基准率；③在多市场状态下分组验证；④承认这是"经验假设"而非"已被证明的规律"。

## 6. 对我们两个策略的直接结论

1. **底部反弹确认**：文献支持反转主轴（A 股尤甚），但要照 Da-Liu-Schaumburg 分"流动性冲击"与"基本面恶化"——我们的确认条件（站上 MA20、相对行业强度转正、量比 ≥1.1）方向正确；建议再加"缩量止跌"（回踩期量能收敛）与"不创新低天数"。出场按 Kaminski-Lo：**反转型标的不要用紧止损**（用时间止损 + 结构止损更合适）。
2. **箱体突破→回踩→再突破**：文献支持"52 周新高/长周期突破 + 放量确认 + 过热过滤"（George-Hwang、Lee-Swaminathan、Gervais 等），但**不支持"任意 20 日通道突破"**（我们自己的事件研究也是 −0.56%）。因此：①把"箱体"定义得更严（宽度、持续时间、前期高点次数）；②保留 `max_breakout_extension=0.05` / `max_runup_5d=0.20` 这类过热上限；③"回踩"必须缩量、"再突破"必须放量（Lee-Swaminathan 的交互效应）；④出场可用止损（动量型标的上 Kaminski-Lo 支持止损）。
3. **ML 的位置**：文献与专家实践一致——**规则产生信号，ML 做 take/skip 与排序**（López de Prado 的元标注 + Gu-Kelly-Xiu 的"同一 OOS 口径对照"），而不是让横截面 ranker 当主选择器。
4. **验收口径**：PBO/多次试验登记（Bailey 等）、purge/embargo、成本敏感性（Han-Yang-Zhou 指出技术信号换手高、成本吃掉相当部分 alpha）。

## 7. 参考文献（DOI，均经 API 核验）

见上表 DOI 列。补充说明：实务派资料（O'Neil《How to Make Money in Stocks》的 base breakout、Minervini 的 VCP、Weinstein 的阶段分析、Wyckoff 的吸筹/回踩）不在学术检索范围内，本文档只把它们作为"经验假设来源"，不作为证据。
