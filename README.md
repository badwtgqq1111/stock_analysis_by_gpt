# 港股量化研究与回测工具

基于 `ClickHouse + Parquet` 数据底座的本地量化研究工具箱。架构详见 [QUANT_SYSTEM_OVERALL_DESIGN.md](./QUANT_SYSTEM_OVERALL_DESIGN.md)。

## 环境部署

```bash
# 1. 安装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. 固定 Python 版本
uv python install 3.12.3
uv python pin 3.12.3

# 3. 安装依赖
uv sync --dev
```

macOS 上使用 LightGBM 需先安装 `libomp`：

```bash
brew install libomp
uv sync --dev
```

项目通过 `pyproject.toml` 的 `[tool.uv.sources]` 将 `akshare` 指向同级目录 `../akshare`，目录结构不同时需调整此路径。

## 命令流水线

命令之间有明确的上下游依赖，按顺序执行：

```
sync ──> generate-factors ──> validate-factors ──> select (factor 模式)
  │              │                    │
  │              │                    └────> factor-report
  │              │
  │              └──> select (lightgbm 模式，不依赖验证)
  │              │
  │              └──> signal-report (不依赖验证)
  │
  └──> backfill-industry ─────────────> select / 行业分层选股

fetch-alt ──────────────────────────> select (lightgbm 模式，可选增强)
```

### 1. 数据同步 `sync`

拉取全港股日线/分钟线（2014 年起），只需执行一次。

```bash
# 仅日线
uv run python run.py sync \
  --start-date 2014-01-01 \
  --max-workers 24 \
  --frequencies daily \
  --skip-existing \
  --show-progress

# 日线 + 分钟线（分钟线从 1min 重采样派生）
uv run python run.py sync \
  --start-date 2014-01-01 \
  --max-workers 24 \
  --show-progress
```

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--base-dir` | 数据根目录 | `./assets/data` |
| `--start-date` | 起始日期 | `2014-01-01` |
| `--max-workers` | 并发数 | 24 |
| `--frequencies` | 周期：`daily` / `1min` / `5min` / `60min` | 全部 |
| `--skip-existing` | 跳过已入库 | 关 |
| `--show-progress` | 显示进度 | 关 |
| `--data-source` | `eastmoney`（默认）或 `sina` | `eastmoney` |
| `--no-derive-intraday` | 不从 1min 派生分钟线 | 关 |
| `--min-daily-rows-for-intraday` | 日线不足此行数则跳过分钟线 | 3 |

数据落盘：`assets/data/clean/ohlcv`（日线 Parquet）、`assets/data/meta/stock_info_registry`（本地元数据 fallback）、`assets/data/signal`（批次信号）、`assets/data/trade`（回测结果）。设置 ClickHouse 环境变量后，features 与 stock info registry 会优先写入 ClickHouse；不可用时自动回退本地 Parquet。

### 2. 前置：启动 ClickHouse

因子数据存储在 ClickHouse，需要 Docker：

```bash
docker run -d --name clickhouse \
  -p 8123:8123 -p 9000:9000 \
  -e CLICKHOUSE_USER=default \
  -e CLICKHOUSE_PASSWORD=quant2024 \
  clickhouse/clickhouse-server
```

环境变量（可写入 `~/.zshrc`）：

```bash
export CLICKHOUSE_HOST=localhost
export CLICKHOUSE_PORT=8123
export CLICKHOUSE_USER=default
export CLICKHOUSE_PASSWORD=quant2024
```

未设环境变量时自动回退到 Parquet 后端。

### 3. 行业与标的类型补全 `backfill-industry`

> **依赖**: `sync` 至少已产生港股代码池；不依赖 `generate-factors`

如果已经同步过 OHLCV，不需要重新跑完整 `sync`。行业分类、ETF/基金/REIT/杠杆反向产品识别属于 stock info registry 的元数据补全，单独跑下面命令即可。

```bash
# 更新依赖锁文件：pyproject 已移除 duckdb，需联网刷新 uv.lock
uv lock

# 补全行业字段，并规范化已有行业与 instrument_type/is_fund_like
uv run python run.py backfill-industry \
  --force \
  --normalize-existing \
  --show-progress \
  --max-workers 8
```

`backfill-industry` 会同时写入：

| 字段 | 用途 |
|---|---|
| `industry_l1/l2/l3` | 行业分层、行业内 TopN、覆盖率检查 |
| `instrument_type` | `common_stock` / `fund_like` / `reit` 等标的类型 |
| `is_fund_like` | 硬排除 ETF、基金、REIT、杠杆反向、结构化产品 |
| `tradable_flag` | 停牌或不可交易标的的硬过滤入口 |

补全后的覆盖率要看 `ordinary_industry_l1_rate` / `ordinary_industry_l2_rate`，不要只看全市场 `industry_l1_rate`。港股 `03/09/28/30/31/34/72/73/75/77` 等区间大量是 ETF/基金/杠杆反向产品，会被统计为 fund-like，不应按普通股票要求行业覆盖。

### 4. 因子生成 `generate-factors`

> **依赖**: `sync` 完成

```bash
uv run python run.py generate-factors \
  --days 365 \
  --factor-set alpha158_hk \
  --max-workers 8 \
  --show-progress
```

`--stock-limit N` 可限制股票数；不加则跑全部。已入库自动跳过。`alpha158_hk` 在 `qlib_alpha158` 基础上多了 7 个港股定制因子（详见 [factor_engine/expressions/custom_factors.py](factor_engine/expressions/custom_factors.py)），推荐作为默认选择。

**TA-Lib 技术指标**：Alpha158 已集成 35 个 TA-Lib 算子（动量/超买超卖 11 + MACD 族 3 + 趋势 11 + 波动 5 + 量价 3 + 特色 1），总共 **193 个特征**，LightGBM 自动做特征选择。详见 [factor_engine/expressions/ta_operators.py](factor_engine/expressions/ta_operators.py)。

### 5. 因子验证 `validate-factors`

> **依赖**: `generate-factors` 完成

```bash
uv run python run.py validate-factors \
  --days 365 \
  --factor-set alpha158_hk \
  --max-workers 8 \
  --show-progress \
  --export-csv output/validation_scorecard
```

产出 IC 质量报告和权重缓存，供 `select`（factor 模式）使用。

### 6. 选股回测 `select`

> **依赖**: `generate-factors` 完成（lightgbm 模式）；建议先完成 `backfill-industry`，否则行业分层和 fund-like 过滤不完整；额外依赖 `validate-factors` 完成（factor 模式）

```bash
# LightGBM 排序模式（推荐）
uv run python run.py select \
  --analysis-mode lightgbm \
  --top-n 10 \
  --days 365 \
  --max-workers 8 \
  --show-progress \
  --factor-set alpha158_hk \
  --min-market-cap 30 \
  --min-daily-turnover 500 \
  --export-csv output/results \
  --llm-report

uv run python run.py select --analysis-mode lightgbm --top-n 10 --days 365

# 带信号 recipe + LLM 报告
uv run python run.py select \
  --analysis-mode lightgbm \
  --top-n 10 --days 365 \
  --max-workers 8 --show-progress \
  --factor-set alpha158_hk \
  --signal-recipes low_price_setup,range_breakout,box_pullback \
  --min-market-cap 30 --min-daily-turnover 500 \
  --export-csv output/results \
  --llm-report
```

lightgbm 模式在一次命令中完成训练和预测，不依赖 `validate-factors` 的权重缓存。

如果刚做完行业/instrument 补全，通常不需要重跑 `sync`；但需要重跑 `select`，因为 ranking、`selection_eligible` 和最终持仓会重新读取 stock info registry。

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--analysis-mode` | `lightgbm`（推荐）或 `factor` | `lightgbm` |
| `--top-n` | 组合持有数量 | 3 |
| `--days` | 分析天数 | 365 |
| `--max-workers` | 并发线程数 | 0（自动） |
| `--show-progress` | 显示进度 | 关 |
| `--factor-set` | 因子集名称 | `alpha158_hk` |
| `--model-type` | `lightgbm` / `xgboost` / `catboost` | `lightgbm` |
| `--max-features` | 按 importance 筛到 TopN 重训，0 = 全部 | 0 |
| `--signal-recipes` | 形态信号，逗号分隔 | `low_price_setup` |
| `--min-market-cap` | 最低市值（亿港元） | 无 |
| `--min-daily-turnover` | 最低日成交额（万港元） | 100 |
| `--min-ipo-days` | 最低上市天数（交易日） | 无 |
| `--export-csv` | 导出路径前缀 | 不导出 |
| `--persist-signals` | 写入 signal 层 | 关 |
| `--batch-id` | 批次号 | 自动生成 |
| `--fast-mode` | 跳过组合净值回放 | 关 |
| `--backtest-date` | 仅用指定日期之前的数据选股 | 无 |
| `--llm-report` | 选股后自动生成 AI 报告 | 关 |

导出文件：`{base}_ranking.csv`、`{base}_selected.csv`、`{base}_watchlist.csv`。

#### 信号 recipe

`--signal-recipes` 在因子评分基础上叠加形态过滤：

| recipe | 识别形态 | 适合用途 |
|---|---|---|
| `low_price_setup` | 低价突破前、底部反弹、横盘惩罚 | 默认稳健筛选 |
| `range_breakout` | 横盘压缩后放量突破 | 捕捉启动日 |
| `box_pullback` | 箱体突破后缩量回踩 | 等待二次确认买点 |

#### LLM 自动分析报告

设置 `DEEPSEEK_API_KEY` 环境变量，加 `--llm-report` 即可在选股完成后自动生成 AI 分析报告，归档到 `docs/report/{日期}_llm.md`。

### 7. 信号报告 `signal-report`

> **不依赖** `validate-factors`；需因子数据

验证 recipe 触发后的未来收益：

```bash
uv run python run.py signal-report \
  --days 365 \
  --signal-recipes low_price_setup,range_breakout,box_pullback \
  --horizons 20,40,60 \
  --max-workers 8 \
  --show-progress \
  --export-csv output/signal_report
```

`--signal-cooldown-days`（默认 20）合并同股票同 recipe 的连续触发；`--signal-event-policy` 选 `first` / `latest` / `best_score`。

### 8. 因子报告 `factor-report`

> **不依赖** `validate-factors`；需因子数据

独立评估因子质量，不受验证缓存影响：

```bash
uv run python run.py factor-report \
  --days 365 \
  --factor-set alpha158_hk \
  --max-workers 8 \
  --show-progress \
  --export-csv output/factor_report
```

### 9. 另类数据 `fetch-alt`（可选）

> **不依赖**其他命令；产出可供 `select --analysis-mode lightgbm` 使用

抓取港股个股新闻并做情感分析，产出 `alt_sentiment_*` 特征：

```bash
uv run python run.py fetch-alt \
  --stock-limit 100 \
  --show-progress \
  --persist-signals
```

新闻抓取每只股票有 0.3s 限速，全市场约 14 分钟，建议 cron 调度。`select --analysis-mode lightgbm` 会自动从 feature 层加载情感特征（无数据则跳过）。

## 工具命令

以下命令独立运行，不参与流水线：

```bash
# 单股深度分析
uv run python run.py single 00700 --days 365

# 固定池多策略对比
uv run python run.py suite --days 365 --top-n 3

# 批次复盘
uv run python run.py review hk_top10_20260516 --export-csv output/review

# 兼容旧模式（验证 + 选股一体）
uv run python run.py all --top-n 10 --days 365 --max-workers 8 --show-progress
```

## Python API

```python
from core import StockAnalyzer

analyzer = StockAnalyzer(db_dir="./assets")

# 全市场 TopN 回测
result = analyzer.backtest_hk_market(
    days=365, top_n=10, initial_capital=100000,
    max_workers=8, analysis_mode="lightgbm", factor_set="alpha158_hk",
)

# 因子验证
report = analyzer.build_factor_validation_report(
    stock_codes=analyzer.get_all_stocks(),
    days=365, factor_set="alpha158_hk",
    horizons=(1, 5, 10, 20), quantiles=5, max_workers=8,
)
```

## Web 界面

基于 **Vue 3 + lightweight-charts + FastAPI** 的看板，包含选股结果、因子 IC 分析、K 线图表、组合回测四个页面。

```bash
# 后端 (port 8000)
uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

# 前端 (port 5173, 需要 Bun)
cd frontend && bun install && bun run dev
```

生产部署：`cd frontend && bun run build`，FastAPI 自动托管 `frontend/dist/`，访问 `http://localhost:8000`。

| 页面 | 路由 | 功能 |
|------|------|------|
| 选股结果 | `/` | LightGBM Top10 排序、评分柱状图、SHAP 特征解释 |
| 因子 IC 分析 | `/factor-ic` | IC/RankIC 时序图、Top10 因子柱状图、汇总表 |
| K 线图表 | `/kline` | Canvas K 线 + MA 均线 + 成交量 + 信号标记 + 筹码分布 |
| 组合回测 | `/portfolio` | 净值曲线、回撤曲线、收益指标卡、当前持仓表 |
