# P2.62 补「主力追踪」的数据：十大流通股东（tushare）+ 资金流主力追踪策略

> 用户（2026-10-14）："没有数据补数据，tushare 是不是可以拿到 十大流通股东数据"

## 一、能拿到，已经补完（tushare 官方接口，直连不装包）

仓库里本来就有官方客户端 `data/ingest/providers/cn_tushare_official.py`，环境里也有 `TUSHARE_TOKEN`，
所以**不需要装 tushare 包**，直接 `POST api.tushare.pro`：

| 接口 | 内容 | 实测规模 | 覆盖 |
|---|---|---|---|
| `top10_floatholders` | 十大**流通**股东（持股数/占比/变动/股东类型） | **621,242 行** | 5,960 只 × 12 季（2023Q4~2026Q3） |
| `top10_holders` | 十大股东（总股本口径） | 632,753 行 | 6,008 只 × 12 季 |
| `stk_holdernumber` | **股东户数** | **241,367 行** | 5,616 只（2023-01~2026-10） |
| `stk_holdertrade` | 股东**增减持** | 32,500 行 | 4,226 只（2023~2026） |

* 脚本：`scripts/backfill_cn_shareholders.py`（断点续跑、留原始快照、分页与去重）
* 派生表：`assets/data/derived/cn_{top10_floatholders,top10_holders,holder_number,holder_trade}.parquet`
* **主力特征表**：`assets/data/derived/cn_shareholder_features.parquet`（62,445 行，每票每期一行）
  —— `top10_float_ratio`（筹码集中度 100%）、`float_holder_up/down/new`（98.9%）、
  `inst_holder_ratio`（92.7%）、`holder_num_value/holder_num_chg`（88.1%），**全部带 `ann_date`（PIT）**
* 数据说明：`output/research/shareholders/REPORT.md`

### 三个必须写下来的坑（都进了代码与测试）

1. `stk_holdernumber(period=...)` **不按报告期过滤**（返回跨年大结果集、按 ann_date 倒序、页间有重叠）
   ⇒ 必须按 ann_date 切成**季度窗** + 去重；
2. `offset` 上限 **100,000**（翻太深接口直接报错）⇒ 每窗限页，`fetch_paged` 出错返回**部分结果**并记录
   `frame.attrs["page_error"]`（不静默丢数据）；
3. `end_date=19000908` 这类**占位脏数据**（实测 3 行）会污染"户数环比" ⇒ 过滤；
   机构股东识别从"精确匹配"改成**关键词包含**（覆盖率 0.2% → 92.7%，实际取值是"开放式投资基金"这类长名）。

## 二、顺带落地：资金流口径的主力追踪策略 `moneyflow_arm`

在补股东数据之前，本轮先用**已有的**资金流特征（`cn_moneyflow_features.parquet`，三源）做了一条
"主力追踪"候选策略并登记进池：

| 项 | 值 |
|---|---|
| 规则 | 5 日主力净流入 z ≥1.0 + 10 日净流入天数占比 ≥60% + 近 20 日涨幅 ∈[-10%,+8%] + 收盘 >MA20 + 20 日均额 ≥2000 万 |
| 交易 | 次日开盘买、每日最多 5 只（z 降序）、持有 10 日或跌破入场价 −8% 止损、双边 15.5bp |
| 样本 | 2024-08-27 ~ 2026-10-12（资金流数据只有这么长） |
| 结果 | 累计 **+174.1%** vs 中证500 **+57.8%**（超额 **+73.7%** 比值口径 / **+116.3pp** 差值口径），267 笔、逐笔 +2.20%、胜率 48.7% |
| 风险 | 最大回撤 **−40.0%**（同期中证500 −24.8%）、夏普 1.15 |
| 稳健性 | 18 格参数网格 **17 格跑赢**中证500（中位 +68.5pp）；默认参数取**高原中心**，不取最高格（+226.6pp 是孤立高点） |
| 数据边界 | `moneyflow_net_z_5d` 被**硬截断在 ±1.79**（阈值 >1.79 恒为 0 信号，已写进脚本注释与测试） |

登记：`config/strategies.json` 的 `moneyflow_arm`（candidate / 不进生产权重）；接线走 P2.57 抽象层
（`strategy_registry --check`：12 条策略 / 8 个消费端，gaps=0），已进台账（4,872 行 / 506 个决策日）、
选股结果页、K 线自选、同花顺导出 `--sources auto`、每日刷新契约，并在策略池里与中证500 对比。

## 三、下一步（数据已经就位）

* **股东户数下降 + 筹码集中**（`holder_num_chg < 0` 且 `top10_float_ratio` 环比抬升）→ 次日开盘买；
* **机构进场**（`inst_holder_ratio` 抬升 + `float_holder_new` 家数增加）；
* **大额增持事件**（`holder_trade` 的 `in_de=IN` + `change_ratio` 分档）。
三条都能直接复用 `wyckoff_study.replay` 的组合引擎出净值曲线，再与中证500 对比、登记进池。

## 四、边界

* 股东数据是**季度**频率（另有不定期披露），公告日与报告期可能相差 1~4 个月 ⇒ 任何策略都必须用 `ann_date` 对齐；
* 2026Q3 只有 177 只披露（报告期刚开始披露），回测时要注意"最后一期不完整"；
* 本轮只补了 12 个季度（2023Q4 起）——股东户数补到 2023Q1；更早的历史可以按同样方式续跑（`--quarters N`）。
