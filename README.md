# 港股量化研究与回测工具

## 项目概述
本项目已经从早期的单股行情抓取脚本，演进成一个可本地运行的港股量化研究工具箱。当前重点能力包括：

- 港股 / A 股历史数据同步与分层存储
- `Parquet + DuckDB` 数据底座
- `feature` / `signal` / `trade` 数据层读写
- Qlib 风格因子计算与因子验证初版
- 单标的事件驱动回测引擎
- Top N 组合构建与全港股股票池筛选
- CLI 方式运行单股分析、固定股票池比较、全港股 TopN 扫描与批次复盘

当前分析与回测默认走新架构数据层，核心路径如下：

- `assets/data/clean/ohlcv`: 主行情数据集
- `assets/data/meta/market_data.duckdb`: 新元数据层
- `assets/data/signal`: 批次扫描信号
- `assets/data/trade`: 回测交易结果

`assets/stock_data.duckdb` 属于遗留兼容库，当前 `stock_analyzer.py` 主链路已经不再依赖它。

如果你想看整体架构和演进路线，可以先读 [QUANT_SYSTEM_OVERALL_DESIGN.md](./QUANT_SYSTEM_OVERALL_DESIGN.md)。

## 项目结构

### 核心模块

```
stock/
├── main.py                  # 主程序入口，协调各个模块
├── data_fetcher.py         # 数据获取模块
│   ├── StockInfoFetcher    # 获取股票基本信息
│   └── HistoryDataFetcher  # 获取历史K线数据
├── data_display.py         # 数据显示模块
│   ├── StockInfoDisplay    # 显示基本信息
│   └── HistoryDataDisplay  # 显示历史数据
├── data_saver.py           # 数据保存模块
│   └── DataSaver           # 保存数据到JSON文件
├── chart_plotter.py        # 图表绘制模块
│   └── KLineChartPlotter   # 绘制K线蜡烛图
├── utils.py                # 工具函数模块
│   ├── setup_output_dir()  # 设置输出目录
│   ├── print_section()     # 打印章节标题
│   └── print_summary()     # 打印处理概要
├── requirements.txt        # 依赖包列表
└── README.md               # 项目文档
```

## 模块说明

### 1. data_fetcher.py - 数据获取模块
**职责**：从腾讯财经 API 获取股票数据

**主要类**：
- `StockInfoFetcher`: 获取实时股票信息（价格、成交量等）
- `HistoryDataFetcher`: 获取历史K线数据（1000条记录）

**使用示例**：
```python
from data_fetcher import StockInfoFetcher, HistoryDataFetcher

# 获取基本信息
info_fetcher = StockInfoFetcher('03633')
info = info_fetcher.fetch()

# 获取历史数据
data_fetcher = HistoryDataFetcher('03633')
df = data_fetcher.fetch()
```

### 2. data_display.py - 数据显示模块
**职责**：格式化和显示各类股票数据

**主要类**：
- `StockInfoDisplay`: 显示股票基本信息
- `HistoryDataDisplay`: 显示历史数据统计

**特点**：
- 清晰的表格格式
- 自动对齐和美化
- 支持中文显示

### 3. data_saver.py - 数据保存模块
**职责**：将获取的数据保存到本地文件

**主要类**：
- `DataSaver`: 保存数据为JSON格式

**保存内容**：
- 股票代码
- 记录数量
- 日期范围
- 时间戳
- 完整的K线数据

### 4. chart_plotter.py - 图表绘制模块
**职责**：生成专业的K线蜡烛图

**主要类**：
- `KLineChartPlotter`: 绘制K线图

**图表特点**：
- 支持上下影线表示最高/最低价
- 红色蜡烛表示上升（收盘 > 开盘）
- 绿色蜡烛表示下跌（收盘 < 开盘）
- 自动缩放和标签
- 添加图例和网格

### 5. utils.py - 工具模块
**职责**：提供通用工具函数

**主要函数**：
- `setup_output_dir()`: 清理历史数据并创建输出目录
- `print_section()`: 打印格式化的章节标题
- `print_summary()`: 打印处理完成概要

### 6. main.py - 主程序
**职责**：按顺序调用各模块完成完整流程

**执行流程**：
1. 初始化 - 清理旧数据，创建输出目录
2. 下载数据 - 获取基本信息和历史数据
3. 显示数据 - 格式化输出数据统计
4. 保存数据 - 保存为JSON格式
5. 绘制图表 - 生成K线蜡烛图
6. 输出总结 - 显示完成信息

## 使用方法

### 基础使用
```bash
python main.py
```

### 项目目录

先进入项目目录：

```bash
cd /home/ccs/code/stock_analysis_by_gpt
```

建议优先使用虚拟环境里的 Python：

```bash
/home/ccs/code/stock_analysis_by_gpt/.venv/bin/python
```

### 港股批量同步验证命令

局部验证，先抓前 20 只港股，适合检查股票池、并发抓取和落库链路：

- `daily` 使用 `--start-date`
- `1min/5min/60min` 默认回补最近 3 年
- 批量同步按 `股票 + 周期` 做增量补数，默认带少量重叠窗口回补，避免漏掉最近修订数据
- `5min/60min` 原始源拿不到时，会优先尝试用 `1min` 数据本地合并生成

```bash
/home/ccs/code/stock_analysis_by_gpt/.venv/bin/python sync_hk_market.py \
  --db-dir ./assets \
  --start-date 2014-01-01 \
  --limit 20 \
  --workers 8
```

指定股票做小范围验证：

```bash
/home/ccs/code/stock_analysis_by_gpt/.venv/bin/python sync_hk_market.py \
  --db-dir ./assets \
  --start-date 2014-01-01 \
  --workers 4 \
  --code 00700 \
  --code 00005 \
  --code 09988
```

全港股批量回补，从 2014 年开始下载到今天：

```bash
/home/ccs/code/stock_analysis_by_gpt/.venv/bin/python sync_hk_market.py \
  --db-dir ./assets \
  --start-date 2014-01-01 \
  --workers 24
```

如果只想跑日线全量，不抓分钟级：

```bash
/home/ccs/code/stock_analysis_by_gpt/.venv/bin/python sync_hk_market.py \
  --db-dir ./assets \
  --start-date 2014-01-01 \
  --workers 24 \
  --frequencies daily
```

如果分钟级想改成别的时间范围，可以显式指定起始日期：

```bash
/home/ccs/code/stock_analysis_by_gpt/.venv/bin/python sync_hk_market.py \
  --db-dir ./assets \
  --start-date 2014-01-01 \
  --intraday-start-date 2023-01-01 \
  --workers 24
```

如果希望跳过已经入库的日线，只补新股票或未落库股票：

```bash
/home/ccs/code/stock_analysis_by_gpt/.venv/bin/python sync_hk_market.py \
  --db-dir ./assets \
  --start-date 2014-01-01 \
  --workers 24 \
  --skip-existing
```

如果只想先下载 K 线，不写 stock info 元数据，也不做最后压实：

```bash
/home/ccs/code/stock_analysis_by_gpt/.venv/bin/python sync_hk_market.py \
  --db-dir ./assets \
  --start-date 2014-01-01 \
  --workers 24 \
  --no-stock-info \
  --no-compact
```

命令执行完成后，主要数据目录如下：

- 日线 Parquet 数据集：`/home/ccs/code/stock_analysis_by_gpt/assets/data/clean/ohlcv`
- 元数据 DuckDB：`/home/ccs/code/stock_analysis_by_gpt/assets/data/meta/market_data.duckdb`
- 信号层：`/home/ccs/code/stock_analysis_by_gpt/assets/data/signal`
- 交易层：`/home/ccs/code/stock_analysis_by_gpt/assets/data/trade`
- 遗留兼容库：`/home/ccs/code/stock_analysis_by_gpt/assets/stock_data.duckdb`

### 单股分析

分析单只股票并输出图表：

```bash
/home/ccs/code/stock_analysis_by_gpt/.venv/bin/python stock_analyzer.py single 00700 --days 365
```

旧用法也兼容，直接给股票代码即可：

```bash
/home/ccs/code/stock_analysis_by_gpt/.venv/bin/python stock_analyzer.py 00700 --days 365
```

### 固定股票池 TopN

运行默认固定股票池分析：

```bash
/home/ccs/code/stock_analysis_by_gpt/.venv/bin/python stock_analyzer.py
```

运行多策略对比：

```bash
/home/ccs/code/stock_analysis_by_gpt/.venv/bin/python stock_analyzer.py suite --days 365 --top-n 3
```

导出多策略对比表：

```bash
/home/ccs/code/stock_analysis_by_gpt/.venv/bin/python stock_analyzer.py suite \
  --days 365 \
  --top-n 3 \
  --export-csv output/strategy_suite
```

### 全港股 TopN 筛选

前提：本地已经同步过港股日线数据。`all_hk` 模式会自动读取本地新架构数据层里的全部港股代码，然后按 `factor_engine -> 横截面打分 -> TopN` 做全市场选股。

当前默认模式：

- `--analysis-mode factor`
- `--factor-set qlib_alpha158`

也就是说，`all_hk` 现在默认不是靠 `strategy/current_strategy.py` 里的经验买点规则做全市场筛选，而是优先走因子框架主链路。经验策略模式仍保留为兼容入口。

运行全港股 Top 10：

```bash
/home/ccs/code/stock_analysis_by_gpt/.venv/bin/python stock_analyzer.py all_hk \
  --top-n 10 \
  --days 365 \
  --initial-capital 100000 \
  --analysis-mode factor \
  --factor-set qlib_alpha158
```

导出全市场排名、当前持有、观察名单：

```bash
/home/ccs/code/stock_analysis_by_gpt/.venv/bin/python stock_analyzer.py all_hk \
  --top-n 10 \
  --days 365 \
  --initial-capital 100000 \
  --analysis-mode factor \
  --factor-set qlib_alpha158 \
  --export-csv output/all_hk_top10
```

命令执行后会生成：

- `output/all_hk_top10_ranking.csv`
- `output/all_hk_top10_selected.csv`
- `output/all_hk_top10_watchlist.csv`

同时控制台会输出每只当前 TopN 股票的解释摘要，包括：

- `composite_score / trend_score / quality_score / risk_score`
- 组件权重
- 主要贡献因子 Top3

如果希望把这次扫描结果直接写入 `signal` 层，并绑定一个明确的批次号：

```bash
/home/ccs/code/stock_analysis_by_gpt/.venv/bin/python stock_analyzer.py all_hk \
  --top-n 10 \
  --days 365 \
  --analysis-mode factor \
  --factor-set qlib_alpha158 \
  --persist-signals \
  --batch-id hk_top10_20260508
```

如果同时既要导出 CSV，又要写入 `signal` 层：

```bash
/home/ccs/code/stock_analysis_by_gpt/.venv/bin/python stock_analyzer.py all_hk \
  --top-n 10 \
  --days 365 \
  --max-workers 8 \
  --show-progress \
  --analysis-mode factor \
  --factor-set qlib_alpha158 \
  --export-csv output/all_hk_top10 \
  --persist-signals \
  --batch-id hk_top10_20260508
```

参数说明：

- `--analysis-mode`: `factor` 或 `strategy`，默认 `factor`
- `--factor-set`: 因子集名称，默认 `qlib_alpha158`
- `--max-workers`: 全市场批量分析并发线程数
- `--show-progress`: 显示股票分析进度、成功数、耗时和 ETA
- `--fast-mode`: 跳过组合真实 replay，优先用于大范围选股扫描
- `--persist-signals`: 将 `ranking / selected / watchlist` 写入 `signal` 层
- `--batch-id`: 给本次扫描结果打批次号，便于后续 `review_batch` 复盘

如果希望先做因子验证，再把 `recommended_factor_weight` 自动回填到 `all_hk` 评分链路，可以打开验证驱动权重模式：

```bash
/home/ccs/code/stock_analysis_by_gpt/.venv/bin/python stock_analyzer.py all_hk \
  --top-n 10 \
  --days 365 \
  --max-workers 8 \
  --show-progress \
  --analysis-mode factor \
  --factor-set qlib_alpha158 \
  --use-recommended-factor-weights \
  --validation-days 365 \
  --validation-horizons 1,5,10,20 \
  --validation-quantiles 5 \
  --validation-min-observations 5 \
  --validation-stock-limit 500 \
  --export-csv output/all_hk_top10_validation_weighted
```

这组参数的含义是：

- 先用 `validation-stock-limit` 指定的样本池做因子验证
- 再根据 `factor_report` 同口径生成 `recommended_factor_weight`
- 最后把推荐权重回填到 `trend / quality / risk` 三个组件内，继续做全市场 TopN 筛选
- 验证结果会默认缓存到 `assets/data/meta/factor_weight_cache/`，相同参数再次运行会直接复用
- 在 `all_hk --use-recommended-factor-weights` 下，默认只验证当前打分链路实际使用的因子，不会把整个 `qlib_alpha158` 全量横截面都算一遍

相关参数：

- `--use-recommended-factor-weights`: 开启验证驱动权重模式
- `--validation-days`: 验证窗口天数，默认跟 `--days` 一致
- `--validation-horizons`: 验证 horizon 列表
- `--validation-quantiles`: 验证分组数
- `--validation-min-observations`: 验证最小样本数
- `--validation-stock-limit`: 参与验证的股票上限
- `--validation-factor-scope`: `scoring_only` 或 `all`
- `--refresh-recommended-factor-weights`: 强制重算，不使用本地缓存

默认建议：

- `all_hk --use-recommended-factor-weights` 用 `scoring_only`
- `factor_report` 做全因子研究时用 `all`

如果你看到日志停在：

```text
[PROGRESS] validation cross_section start stocks=100 feature_rows=4228970
```

这不一定是卡死，通常表示已经完成逐股票因子生成，正在做统一横截面验证。现在默认 `scoring_only` 后，这一阶段的数据量会明显下降；如果你显式传 `--validation-factor-scope all`，耗时会重新上来，这是预期行为。

### 因子验证报告

如果你想系统回答下面三个问题：

- 现在用到的因子质量到底怎么样
- 哪些因子更强，哪些因子该降权
- 当前配置权重和验证结果是否一致

可以直接跑 `factor_report`：

```bash
/home/ccs/code/stock_analysis_by_gpt/.venv/bin/python stock_analyzer.py factor_report \
  --days 365 \
  --factor-set qlib_alpha158 \
  --max-workers 8 \
  --show-progress \
  --validation-factor-scope all \
  --export-csv output/factor_report_alpha158
```

如果先做小样本验证，可以限制股票数：

```bash
/home/ccs/code/stock_analysis_by_gpt/.venv/bin/python stock_analyzer.py factor_report \
  --days 365 \
  --factor-set qlib_alpha158 \
  --stock-limit 200 \
  --max-workers 8 \
  --export-csv output/factor_report_alpha158_sample
```

支持参数：

- `--horizons`: 逗号分隔，例如 `1,5,10,20`
- `--quantiles`: 分组数
- `--min-observations`: 最小样本要求
- `--stock-limit`: 限制参与验证的股票数量
- `--validation-factor-scope`: `all` 表示验证整个因子集，`scoring_only` 表示只验证当前评分链路依赖的因子

导出结果包括：

- `*_factor_scorecard.csv`: 因子质量总表
- `*_ic_summary.csv`: IC / RankIC 汇总
- `*_quantile_summary.csv`: 分组收益汇总
- `*_long_short_summary.csv`: 多空 spread 汇总
- `*_turnover_summary.csv`: 换手率汇总
- `*_decay_summary.csv`: 衰减汇总
- `*_stock_summary.csv`: 单股票验证摘要
- `*_metadata.json`: 本次验证配置

其中 `factor_scorecard.csv` 最关键，里面会同时给出：

- 当前打分链路里配置的 `configured_factor_weight`
- 按验证指标推导的 `recommended_factor_weight`
- `mean_ic / mean_rank_ic / mean_spread / mean_turnover`
- 综合排序用的 `validation_score`

这份报告就是后续把手工权重升级成验证驱动权重的基础。

### 如何看 TopN 入选原因

当前因子模式下，每只股票的入选原因来自三层解释：

1. 组件层
   - `trend_score`
   - `quality_score`
   - `risk_score`
   - `composite_score`

2. 权重层
   - `trend_score` 权重默认 `0.46`
   - `quality_score` 权重默认 `0.34`
   - `risk_score` 权重默认 `0.20`

3. 单因子层
   - `trend` 组件当前默认会看：`MA5/MA20/MA60/MAX20/MAX60/RSV20/CNTD20/SUMD20`
   - `quality` 组件当前默认会看：`VMA20/VSTD20/WVMA20/VSUMD20/CORD20/CNTP20/SUMP20/RSQR60/RESI20`
   - `risk` 组件当前默认会看：`STD20/STD60/WVMA20/VSTD20`

运行 `all_hk` 后，控制台会直接打印 TopN 的主要贡献因子；导出的 `ranking/selected` 结果里也会带 `factor_explanation` 信息，方便后续做进一步展示或落库。

如果你要显式切回旧经验策略模式：

```bash
/home/ccs/code/stock_analysis_by_gpt/.venv/bin/python stock_analyzer.py all_hk \
  --top-n 10 \
  --days 365 \
  --analysis-mode strategy
```

### 批次复盘

如果某次全港股扫描已经通过 `--persist-signals` 写进了 `signal` 层，可以按批次号回看并重新导出：

```bash
/home/ccs/code/stock_analysis_by_gpt/.venv/bin/python stock_analyzer.py review_batch hk_top10_20260508
```

导出该批次的 `ranking / selected / watchlist`：

```bash
/home/ccs/code/stock_analysis_by_gpt/.venv/bin/python stock_analyzer.py review_batch \
  hk_top10_20260508 \
  --export-csv output/review_hk_top10_20260508
```

会生成：

- `output/review_hk_top10_20260508_summary.csv`
- `output/review_hk_top10_20260508_ranking.csv`
- `output/review_hk_top10_20260508_selected.csv`
- `output/review_hk_top10_20260508_watchlist.csv`

其中 `summary.csv` 会包含这一批扫描结果的简要统计，例如：

- `ranking_count`
- `selected_count`
- `watchlist_count`
- `ranking_avg_score`
- `selected_avg_score`
- `watchlist_avg_score`

### Python API 示例

如果你想直接在 Python 里调用：

```python
from analyzer_core import StockAnalyzer

analyzer = StockAnalyzer(db_dir="./assets")

# 本地全部港股 TopN
result = analyzer.backtest_hk_market(
    days=365,
    top_n=10,
    initial_capital=100000,
    max_workers=8,
    analysis_mode="factor",
    factor_set="qlib_alpha158",
)

# 或者显式走统一入口
result = analyzer.backtest_portfolio(
    stock_codes=None,
    days=365,
    top_n=10,
    max_workers=8,
    analysis_mode="factor",
    factor_set="qlib_alpha158",
)
```

### 当前能力边界

现在已经可以做两类事情：

- 全港股因子横截面选股
- 基于因子信号的研究型 TopN 组合回测与结果导出

这里的“回测”目前更偏研究平台语义：

- 支持横截面 TopN 组合构建
- 支持组合净值回放
- 支持扫描结果持久化与批次复盘

但它还不是完整的工业级订单管理系统，还缺：

- 多资产订单生命周期
- 更完整的持仓快照
- OMS / 风控 / 模拟交易闭环

### 当前因子覆盖是否完整

目前答案是：**还不完整**。

现状分三层：

1. `factor_engine`
   - 已有 `qlib_alpha158`
   - 已有 `qlib_alpha360`

2. 实际用于 `all_hk --analysis-mode factor` 打分的因子
   - 只使用了其中一部分代表性因子
   - 主要分成 `trend / quality / risk` 三个组件

3. 还没做的
   - 没有把 `Alpha158/360` 全量因子全部接入选股打分
   - 没有根据 `factor_validation` 自动学习或回填权重
   - 没有基本面、估值、行业中性化、风格暴露等更完整工业级因子层

如果你的目标是工业级量化研究框架，下一步应该把：

- `factor_validation` 的 IC / RankIC / spread 结果
- 因子白名单 / 黑名单
- 因子权重自动生成

正式并到 `all_hk` 的评分链路里，而不是继续手工固定权重。

### 当前 TopN 结果怎么判断质量

以当前已经导出的 `output/all_hk_top10_selected.csv` 为例，现阶段结果只能说 **中等偏弱，还不够工业级**：

- Top10 平均单股回测收益约 `3.08%`
- 中位数约 `0.57%`
- 平均胜率约 `45.94%`
- 正收益 `5` 只，负收益 `5` 只

更关键的是，当前排名第一的 `08613` 虽然 `ranking_score` 很高，但 `backtest_return = -43.85%`。这说明：

- 现在的因子组合和权重仍然偏手工
- 还没有充分让 `factor_validation` 结果反向约束评分链路
- 高分不等于高质量，仍需继续做验证驱动调权

返回结果里通常会包含：

- `ranking`: 全市场评分排序
- `selected`: 当前建议持有
- `watchlist`: 观察名单
- `portfolio_replay`: 组合回放净值与成交
- `analysis_results`: 单股票分析明细

### 输出目录
所有生成的文件保存在 `./output/` 目录下：
- `hk_stock_03633_YYYYMMDD_HHMMSS.json` - 股票数据
- `kline_chart_03633_YYYYMMDD_HHMMSS.png` - K线图表

## 依赖包
- requests >= 2.31.0      # HTTP请求
- pandas >= 2.0.0         # 数据处理
- matplotlib >= 3.5.0     # 图表绘制

## 数据来源
- API: 腾讯财经(腾讯行情)
- 更新频率: 实时更新
- 历史数据: 最多1000条K线记录

## 功能特点

1. **模块化设计**
   - 各模块职责清晰
   - 低耦合高内聚
   - 易于维护和扩展

2. **健壮的错误处理**
   - 网络错误捕获
   - 数据验证
   - 编码兼容性

3. **清晰的输出信息**
   - 分阶段显示处理信息
   - [INFO] [OK] [ERROR] 标签区分
   - 完成后显示总结

4. **专业的图表输出**
   - 高分辨率PNG图表
   - 自动布局和标签
   - 支持中文显示

## 扩展说明

### 添加新的数据显示方式
在 `data_display.py` 中添加新的类：
```python
class CustomDisplay:
    @staticmethod
    def display(data):
        # 自定义显示逻辑
        pass
```

### 添加新的图表类型
在 `chart_plotter.py` 中添加新的类：
```python
class VolumeChartPlotter:
    def plot(self, hist, output_dir):
        # 绘制成交量图表
        pass
```

### 添加数据导出格式
在 `data_saver.py` 中添加新的方法：
```python
class DataSaver:
    @staticmethod
    def save_csv(data, output_dir):
        # 保存为CSV格式
        pass
```

## 注意事项

1. 首次运行时会自动创建 `output` 目录
2. 每次运行会清理之前的历史数据
3. 需要网络连接才能获取数据
4. 图表生成需要安装 matplotlib

## 版本信息
- Python 3.7+
- 最后更新: 2026-03-06
