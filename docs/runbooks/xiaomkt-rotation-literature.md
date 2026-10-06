# 小市值 / 微盘股策略：文献与研报索引

用途：为 `xiaomkt_rotation` 策略腿提供外部证据基线。**DOI 已用 Crossref API 逐条核对**；
中文卖方研报部分标注了核实状态（本机 SearXNG 检索实例返回污染结果，未能在线核对标题）。

## 1. 学术文献（DOI 已核对）

### 1.1 直接解释"A 股小市值效应"的三篇

| 文献 | 出处 | DOI | 关键结论 |
|---|---|---|---|
| Liu, Stambaugh & Yuan, **Size and value in China** | Journal of Financial Economics, 2019-10 | `10.1016/j.jfineco.2019.03.008` | 构建 CH-3 模型；**刻意剔除市值最小的 30%**，因为最小市值股票的价格里含"壳价值期权"，不是可持续的风险溢价。是 A 股 size factor 的基准文献 |
| Hu, Chen, Shao & Wang, **Fama–French in China: Size and Value Factors in Chinese Stock Returns** | International Review of Finance, 2019 | `10.1111/irfi.12177` | 检验 A 股 size/value 因子构造方式（含是否剔除小市值、是否用流通市值）对结论的敏感性 |
| Lee, Qu & Shen, **Reverse Mergers, Shell Value, and Regulation Risk in Chinese Equity Markets** | SSRN, 2017 | `10.2139/ssrn.3038446` | 壳价值 = 借壳上市期权；IPO 排队/管制越严，小微盘壳价值越高。注册制推进 → 壳价值衰减 → 小市值效应弱化 |

### 1.2 幸存者偏差 / 退市偏差（与本项目实测结论直接对应）

| 文献 | 出处 | DOI | 关键结论 |
|---|---|---|---|
| Shumway, **The Delisting Bias in CRSP Data** | Journal of Finance, 1997-03 | `10.1111/j.1540-6261.1997.tb03818.x` | 退市股被数据商直接从样本里删掉，会系统性高估收益 |
| Shumway & Warther, **The Delisting Bias in CRSP's Nasdaq Data and Its Implications for the Size Effect** | Journal of Finance, 1999-12 | `10.1111/0022-1082.00192` | **退市偏差会显著夸大 size effect** —— 与本项目实测（补回 209 只退市股后年化 40.69% → 11.29%）方向一致 |
| Banz, **The relationship between return and market value of common stocks** | Journal of Financial Economics, 1981-03 | `10.1016/0304-405x(81)90018-0` | size effect 的原始文献 |

### 1.3 近期复现与中国市场微观结构

| 文献 | 出处 | DOI | 备注 |
|---|---|---|---|
| **Repricing China: A 25-Year Replication of the Liu–Stambaugh–Yuan Three-Factor Model** | 2026 | `10.54254/2754-1169/2026.ab36301` | CH-3 的长样本复现，可用于核对注册制前后 size 因子的漂移 |
| **Research on Small-Cap Value Rotation Investment Strategy Based on "Size Effect"** | Journal of Applied Finance & Banking, 2023 | `10.47260/jafb/1365` | 与本次复现对象（小市值轮动）最接近的公开研究 |
| **The Liquidity Risk Channel of the Idiosyncratic Volatility Puzzle: Evidence from China** | SSRN, 2025 | `10.2139/ssrn.5707880` | 微盘股的流动性风险通道 |
| **Stock liquidity and stock price crash risk: evidence from China** | Applied Economics, 2024-04 | `10.1080/00036846.2024.2348185` | 低流动性 ↔ 崩盘风险 |
| **The robust "maximum daily return effect as demand for lottery" and "idiosyncratic volatility puzzle"** | Journal of Empirical Finance, 2018-06 | `10.1016/j.jempfin.2018.03.001` | 小盘股"彩票偏好"定价 |
| **Do Retail Investors Have a Preference for Low-priced Stocks?** | SSRN, 2022 | `10.2139/ssrn.4270135` | 低价股偏好（对应"高价股过滤"规则的另一面） |
| **The spillover effect of delisting risk on stock price synchronicity of regional peer firms** | Pacific-Basin Finance Journal, 2026-02 | `10.1016/j.pacfin.2025.103008` | 退市风险的溢出效应 |

## 2. 中文卖方研报（**待核对**）

本机 SearXNG 实例返回污染结果（微软支持页、垃圾站），未能在线核对下列标题；下列按"团队 + 主题"记录，
引用前请用慧博/发现报告/公众号原文核对具体标题与发布日期。

| 团队 | 主题 | 与本策略的关系 |
|---|---|---|
| 开源证券金工（魏建榕团队） | 市场微观结构系列、微盘股策略系列 | 国内较早系统推广"微盘股指数"与微盘轮动的团队；与"每周调仓 + 小市值等权"思路同源 |
| 海通证券金工 | 小市值因子有效性 / 壳价值 | 讨论小市值 alpha 与壳价值的关系 |
| 华泰证券金工（林晓明团队） | A 股因子系列（含市值因子） | 市值因子的构造与中性化口径 |
| 国泰君安 / 招商证券 / 中信建投 / 方正证券金工 | 微盘股指数投资价值分析 | 指数层面（而非个股策略）的收益/回撤/流动性讨论 |
| 天风证券金工（吴先兴团队） | 因子择时与失效监控 | 对应 TODO 中的"策略失效监控" |

## 3. 指数与可得数据（本地已实际取到历史）

| 指数 | 代码 | 本项目中的用途 | 本地数据 |
|---|---|---|---|
| 上证指数 | `000001.SH` | 基准 | `artifacts/xiaomkt_rotation_20261001/cache/sse_index.parquet` |
| 国证2000 | `399303.SZ` | 小盘基准 | 同上（2014-01-02 ~ 2026-09-30，3100 根） |
| 中小综指 | `399101.SZ` | 文章原始股票池 | 同上；策略用深市 002/003 代理，年化差异仅 1pp |
| 中证2000 | `932000.CSI` | 小盘基准（2023 年发布，历史短） | 未取，可按需补 |
| 万得微盘股指数 | `8841431.WI`（Wind 终端） | 微盘股基准 | Wind 专有，本地/中继不可得 |

补充实测（本地数据，2014-01-06 ~ 2024-12-31，买入持有）：上证 +63.8%、国证2000 +100.0%、中小综指 +82.6%。

## 4. 这些文献和我们实测结论的对应关系

1. **LYS(2019) 的"剔除最小 30%"不是洁癖**：我们的实测显示，最小的一批股票同时是退市/ST 高发区，
   补回退市股后年化从 40.69% 掉到 11.29%（见 `artifacts/xiaomkt_rotation_bias_fix/bias_fix_comparison.md`）。
2. **Shumway & Warther(1999) 的结论在 A 股复现了**：退市偏差夸大 size effect，方向与幅度都对得上。
3. **壳价值解释了两段行情的差异**：2014-2016 微盘股的超额（IPO 堰塞湖 → 壳价值高）与 2021 年后注册制
   推进（壳价值衰减）+ 2024-01 微盘股流动性冲击，正好对应我们分年度收益里 2015 +274% 与 2024 年 1-2 月空仓的两端。
4. **彩票偏好/低价股偏好**解释了"为什么最小的股票波动这么大"：便宜、故事多、散户扎堆 → 涨跌都极端。

## 5. 建议阅读顺序

1. Liu, Stambaugh & Yuan (2019) 的 shell value 一节 —— 直接回答"最小的 5 只在真实数据里为什么不是免费午餐"。
2. Shumway & Warther (1999) —— 我们这轮所做的"退市股回补"的经典参照。
3. Repricing China (2026) —— 看注册制之后 size 因子还剩多少。
4. Lee, Qu & Shen (2017) —— 壳价值机制，理解策略的"时代背景依赖"。

## 6. 检索途径（本机实测）

| 途径 | 状态 | 用法 |
|---|---|---|
| Crossref API | ✅ 可用 | `https://api.crossref.org/works?query.bibliographic=<关键词>`，无需 key |
| Semantic Scholar API | ⚠️ 429 限流 | 需要退避重试 |
| 本机 SearXNG (`127.0.0.1:8888`) | ❌ 结果被污染 | 需修复引擎配置后再用于中文研报检索 |
| Tushare 中继 | ✅ 可用 | `index-daily` 可取上述指数日线 |
