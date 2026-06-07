# 港股量化研究与回测工具

基于 `ClickHouse + Parquet` 的本地港股量化研究工具箱，支持数据同步、因子生成、LightGBM 排序选股、行业分层候选池、组合回测和 Web 看板。

架构详见 [QUANT_SYSTEM_OVERALL_DESIGN.md](./QUANT_SYSTEM_OVERALL_DESIGN.md)。

## 环境部署

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.12.3
uv python pin 3.12.3
uv sync --dev

# macOS 上 LightGBM 需要 libomp
brew install libomp
```

项目通过 `pyproject.toml` 的 `[tool.uv.sources]` 将 `akshare` 指向同级目录 `../akshare`。如果本地目录结构不同，需要调整该路径。

## 推荐 Runbook

README 只保留两条生产主线：行情因子线负责 LightGBM 基础选股，智能画像线负责证据、图谱、热度和主题机会特征。基础标签流水线已经退出推荐流程；LightRAG 只用于 evidence 索引和重点股票深挖，不作为全市场逐股在线问答引擎。

```text
日常选股:
sync -> backfill-industry -> generate-factors -> select

智能画像/主题特征刷新:
stock-intelligence-pipeline -> select
```

| 场景 | 跑什么 | 频率 |
|---|---|---|
| 第一次部署 | `uv sync --dev`，可选部署 ClickHouse、SearXNG、LightRAG | 只跑一次 |
| 第一次建库或行情更新 | `sync`、`backfill-industry`、`generate-factors` | 初次全量，之后按交易日增量 |
| 重新选股 | `select --analysis-mode lightgbm` | 每次要出组合 |
| 新股票、新主题、画像过期 | `stock-intelligence-pipeline` | 按周/月，或研究主题变化时 |

### 日常选股

```bash
uv run python run.py sync --start-date 2014-01-01 --frequencies daily --skip-existing --max-workers 24 --show-progress
uv run python run.py backfill-industry --force --normalize-existing --max-workers 8 --show-progress
uv run python run.py generate-factors --days 365 --factor-set alpha158_hk --max-workers 8 --show-progress
uv run python run.py select \
  --analysis-mode lightgbm \
  --top-n 10 \
  --days 365 \
  --factor-set alpha158_hk \
  --min-market-cap 30 \
  --min-daily-turnover 500 \
  --max-workers 8 \
  --export-csv output/results \
  --show-progress
```

`select --analysis-mode lightgbm` 会在单次命令内训练和预测，不依赖 `validate-factors`。只有旧的 `select --analysis-mode factor` 才需要先跑 `validate-factors`。

### 智能画像/主题特征刷新

```bash
uv run python run.py stock-intelligence-pipeline \
  --searxng-url http://127.0.0.1:8888 \
  --lightrag-url http://127.0.0.1:9621 \
  --engines bing,duckduckgo \
  --max-workers 8 \
  --query-workers-per-stock 2 \
  --profile-stage skip \
  --import-to-warehouse \
  --show-progress
```

默认不需要手动指定 `--theme` 或 `--stock-codes`：股票池从 deep evidence/本地股票 registry 自动识别，主题从图谱节点、别名和 evidence 自动发现。`--profile-stage skip` 是全市场生产默认值，会跳过昂贵的逐股 LightRAG 问答，直接用 evidence/别名/规则图谱生成选股特征；专项研究才加 `--themes 大模型 推理算力 机器人`，小样本调试才加 `--stock-codes 02513`。

生产刷新不要加 `--top-n`。流水线默认对每个主题输出全市场股票分数，用于 LightGBM 特征生产；`--top-n` 只适合临时调试或生成报告榜单。若生产时加了 `--top-n 100`，主题特征会被截断成每个主题只覆盖前 100 只，最终大部分股票画像分为 0，选股收益很难真实受益。

LightRAG 的合理用法是两段式：

```bash
# 全市场：只做搜索、索引、规则图谱、热度、主题特征，不逐股问答
uv run python run.py stock-intelligence-pipeline \
  --searxng-url http://127.0.0.1:8888 \
  --lightrag-url http://127.0.0.1:9621 \
  --engines bing,duckduckgo \
  --profile-stage skip \
  --import-to-warehouse \
  --show-progress

# 重点股票：对少量候选做 LightRAG 深挖画像
uv run python run.py stock-intelligence-pipeline \
  --lightrag-url http://127.0.0.1:9621 \
  --skip-aliases \
  --skip-research \
  --skip-lightrag-index \
  --stock-codes 02513 00700 \
  --profile-stage full \
  --profile-workers 2 \
  --profile-query-workers 3 \
  --top-k 20 \
  --chunk-top-k 10 \
  --show-progress
```

画像特征进入 LightGBM 训练面板默认开启；最终排名 overlay 默认关闭。确认主题特征 OOS 有效后，再用小权重打开：

```bash
uv run python run.py select \
  --analysis-mode lightgbm \
  --top-n 10 \
  --days 365 \
  --factor-set alpha158_hk \
  --theme-feature-set theme_opportunity \
  --theme-overlay-strength 0.05 \
  --export-csv output/results \
  --show-progress
```

`--theme-overlay-strength` 建议从 `0.05` 起步，最高通常不超过 `0.10-0.15`；主题分只做增强和解释，不应压过价格、质量、风险主信号。

跑完选股后，用诊断命令检查智能画像是否真的贡献了有效覆盖，而不是只生成了空特征：

```bash
uv run python run.py theme-feature-diagnostics \
  --ranking-csv output/results_alpha158_hk_ranking.csv \
  --selected-csv output/results_alpha158_hk_selected.csv \
  --theme-feature-csv output/theme_opportunity_features.csv
```

重点看 `theme_feature_stock_coverage_rate`、`theme_opportunity_score` 的 `non_zero_rate`、高分桶平均收益、持仓里 `non_zero` 的数量，以及主题分质量里的 `high_score_zero_relevance_rate`。若 `avg_stocks_per_feature_name` 接近 100 且覆盖率很低，通常是生产命令误加了 `--top-n`，先不带 `--top-n` 重跑智能画像特征；若 `high_score_zero_relevance_rate` 偏高，说明旧主题分被泛热度/泛证据顶高，需要用相关性闸门版本重跑主题分；若全量输出后非零覆盖仍低于 20%，再扩大主题体系或补充行业主题；若高分桶没有更高胜率，不要提高 overlay 权重。

选股导出文件语义：

| 文件 | 含义 |
|---|---|
| `*_ranking.csv` | 全市场排名和解释字段 |
| `*_selected.csv` | 最终当前持仓，只包含 `selected=True` |
| `*_candidates.csv` | 进入组合构建但未最终持有的候选 |
| `*_watchlist.csv` | 观察名单/弱信号/被风控降级的标的 |

## 数据与存储

默认写入本地 Parquet：

| 数据 | 路径 |
|---|---|
| OHLCV | `assets/data/clean/ohlcv` |
| 股票元数据 | `assets/data/meta/stock_info_registry` |
| 信号批次 | `assets/data/signal` |
| 回测交易 | `assets/data/trade` |
| ClickHouse 数据卷 | `assets/clickhouse` |

ClickHouse 可选。设置环境变量后，features 与 stock info registry 优先写入 ClickHouse；不可用时自动回退到 Parquet。

```bash
mkdir -p assets/clickhouse

docker run -d --name clickhouse \
  --restart unless-stopped \
  -p 8123:8123 -p 9000:9000 \
  -v "$(pwd)/assets/clickhouse:/var/lib/clickhouse" \
  -e CLICKHOUSE_USER=default \
  -e CLICKHOUSE_PASSWORD=quant2024 \
  -e CLICKHOUSE_DB=quant \
  clickhouse/clickhouse-server

export CLICKHOUSE_HOST=localhost
export CLICKHOUSE_PORT=8123
export CLICKHOUSE_USER=default
export CLICKHOUSE_PASSWORD=quant2024
export CLICKHOUSE_DATABASE=quant
```

`--restart unless-stopped` 会让 Docker 服务启动后自动拉起 ClickHouse 容器；`-v "$(pwd)/assets/clickhouse:/var/lib/clickhouse"` 将 ClickHouse 数据文件保存在项目的 `assets/clickhouse` 下，删除或重建容器时数据不会丢。`/var/lib/clickhouse` 是必须持久化的数据目录，应用侧数据库名使用 `CLICKHOUSE_DATABASE`，默认建议为 `quant`。

## 行业分层选股

`backfill-industry` 依赖 `sync` 产生的港股代码池，不依赖因子生成。它会写入：

| 字段 | 用途 |
|---|---|
| `industry_l1/l2/l3` | 真实行业分类、行业内 TopN、覆盖率检查 |
| `instrument_type` | `common_stock` / `fund_like` / `reit` 等标的类型 |
| `is_fund_like` | 硬排除 ETF、基金、REIT、杠杆反向、结构化产品 |
| `tradable_flag` | 停牌或不可交易标的硬过滤 |

覆盖率优先看普通股口径：

如果当前网络环境无法从公开接口拿到行业信息，可以先准备与 `docs/hk_industry_registry.csv` 同格式的本地 CSV，再导入到同一个 `stock_info_registry`：

```bash
uv run python run.py backfill-industry \
  --industry-registry-csv docs/hk_industry_registry.csv \
  --normalize-existing \
  --show-progress
```

导入模式不会访问网络，CSV 中的 `stock_code`、`industry_l1`、`industry_l2`、`industry_source`、`instrument_type`、`is_fund_like`、`tradable_flag` 等字段会写入 registry；已有价格、市值、估值等非空字段会保留。

```bash
uv run python run.py industry-coverage --show-missing
```

重点指标是 `ordinary_l1_rate` / `ordinary_l2_rate`。港股 `03/09/28/30/31/34/72/73/75/77` 等代码段大量是 ETF、基金、杠杆反向或结构化产品，会被统计为 fund-like，不应按普通股票要求行业覆盖。

## 股票智能画像 Registry

行业表用于稳定分层；股票智能画像用于更细的主题、资源、产业链和业务关联。主流程使用 LightRAG-first 画像系统，不再依赖基础标签流水线。

| 文件 | 用途 |
|---|---|
| `docs/hk_entity_alias_registry.csv` | 股票别名、英文名、产品名和模型名，用于深度画像检索 |
| `docs/hk_stock_deep_evidence.csv` | source-aware 深度画像 evidence |
| `docs/hk_stock_profile.csv` | 股票画像摘要和结构化 JSON |
| `docs/hk_stock_deep_tag_registry.csv` | 产品/技术/催化/瓶颈等细分标签 |
| `docs/hk_stock_graph_nodes.csv` / `docs/hk_stock_graph_edges.csv` | 股票画像图谱节点和边 |

批量生成的单股画像报告、LightRAG context、临时图谱 CSV 和主题评分默认写入已忽略的 `output/`，不要提交到 `docs/`。`docs/` 只保留设计文档、小型 registry 和可复现命令；上千只股票的结构化结果应导入 Parquet/ClickHouse 仓库，可读报告和大 JSON 作为本地 artifact 保存在 `output/stock_profiles/HK/<stock_code>/`。

### 最优整合画像流水线

这是推荐主流程。它把别名、source-aware 搜索、LightRAG 索引/召回、强 schema 图谱、热度、主题机会分、LightGBM 标准特征串在一条命令里。DeepSeek 不再对每只股票做基础 tag 抽取，只在 LightRAG 服务内部用于文档索引/图谱抽取和必要召回，因此比 `extract-stock-tags-llm` 全量逐股抽 tag 更省 token，也更容易复用历史索引。

SearXNG 部署见 `docs/SEARXNG_SEARCH_INTEGRATION.md`，LightRAG 部署见 `docs/LIGHTRAG_DEPLOYMENT.md`。

全市场建议分两段：先全量搜索和索引，再按 `--profile-limit` 或候选池分批做画像召回。不要直接对 3000+ 只股票一次性做多维画像召回。

```bash
uv run python run.py stock-intelligence-pipeline \
  --searxng-url http://127.0.0.1:8888 \
  --lightrag-url http://127.0.0.1:9621 \
  --engines bing,duckduckgo \
  --max-workers 8 \
  --query-workers-per-stock 2 \
  --skip-profile-contexts \
  --skip-graph \
  --skip-enrich \
  --skip-attention \
  --skip-theme \
  --show-progress

uv run python run.py stock-intelligence-pipeline \
  --profile-limit 200 \
  --skip-aliases \
  --skip-research \
  --skip-lightrag-index \
  --lightrag-url http://127.0.0.1:9621 \
  --import-to-warehouse \
  --show-progress
```

小样本调试或专项研究才显式指定股票/主题：

```bash
uv run python run.py stock-intelligence-pipeline \
  --stock-codes 02513 00700 03690 \
  --themes 大模型 推理算力 机器人 \
  --skip-aliases \
  --skip-research \
  --skip-lightrag-index \
  --lightrag-url http://127.0.0.1:9621 \
  --import-to-warehouse \
  --show-progress
```

这条命令生成的核心文件：

| 输出 | 用途 |
|---|---|
| `docs/hk_stock_deep_evidence.csv` | source-aware 深度 evidence，支持断点续跑 |
| `output/stock_profiles/HK/<code>/lightrag_profile_contexts.json` | 单股多维 LightRAG context |
| `output/stock_profiles/HK/<code>/graph_nodes.csv` / `graph_edges.csv` | 单股强 schema 图谱 |
| `output/stock_profiles/HK/graph_nodes_enriched.csv` / `graph_edges_enriched.csv` | 批量增强图谱 |
| `output/attention_signal.csv` | 热度/关注度信号 |
| `output/theme_opportunities.csv` | 主题机会分 |
| `output/theme_opportunity_features.csv` | LightGBM 可读取的 `theme_opportunity` 标准特征 |

### 手动拆分调试命令

下面这些命令只用于排错、单步验证和性能调参。生产优先使用 `stock-intelligence-pipeline` 聚合命令。

| 步骤 | 命令 |
|---|---|
| 别名 | `build-stock-entity-aliases` / `expand-stock-entity-aliases` |
| 搜索 | `research-stock-deep-profile` |
| RAG 索引与召回 | `lightrag-index-evidence` / `lightrag-retrieve-profile-contexts` |
| 图谱转换与增强 | `lightrag-context-to-stock-graph` / `enrich-supply-chain-graph` |
| 热度与主题分 | `derive-attention-signals` / `rank-theme-opportunities` |
| LightGBM 特征 | `export-theme-score-features` |

单只股票排错示例：

```bash
uv run python run.py lightrag-retrieve-profile-contexts 02513 --lightrag-url http://127.0.0.1:9621 --show-progress
uv run python run.py lightrag-context-to-stock-graph 02513 --context-json output/stock_profiles/HK/02513/lightrag_profile_contexts.json
uv run python run.py stock-subgraph 02513 --node-csv output/stock_profiles/HK/02513/graph_nodes.csv --edge-csv output/stock_profiles/HK/02513/graph_edges.csv --json
```

`rank-theme-opportunities` 输出的是可回测/可选股的结构化分数，包含技术、商业化、产业链、瓶颈、催化、热度、证据质量、流动性、趋势、风险和拥挤度组件。外部 Twitter/GitHub/HuggingFace/公众号 API 后续只需要补充写入同一张 `attention_signal` 表，排序器会自动接入。

LightGBM 选股默认会读取 `theme_opportunity` 标准特征进入训练面板；最终排名 overlay 默认关闭。确认主题特征 OOS 有效后，可以用小权重打开：

```bash
uv run python run.py select \
  --analysis-mode lightgbm \
  --factor-set alpha158_hk \
  --theme-feature-set theme_opportunity \
  --theme-overlay-strength 0.05 \
  --top-n 10 \
  --days 365 \
  --export-csv output/results
```

建议 `--theme-overlay-strength` 从 `0.05` 起步，最高不超过 `0.10-0.15`；主题分负责增强召回和排序，不应压过 LightGBM 的价格/质量/风险主信号。

不走 LightRAG 时，`extract-stock-profile-llm` 仍可作为实验性兜底，但不再是推荐主流程。

### 搜索兜底

主流程优先使用本地免费的 SearXNG。Tavily 仅作为付费/额度兜底，Playwright 仅用于小样本诊断或疑难补查；如果改用这些 evidence 文件，把它们作为 `stock-intelligence-pipeline --evidence-csv` 或 `lightrag-index-evidence --evidence-csv` 的输入。`stock-intelligence-pipeline` 默认跳过已成功缓存的深度 evidence，网络恢复后可以直接续跑。LLM/RAG 低置信输出不会直接覆盖最终选股信号，必须先进入强 schema 图谱、热度和主题机会分。

## 验证顺序

改动行业数据、硬过滤、组合选择或画像特征后，先跑小样本 smoke，再跑全市场。

```bash
uv run pytest test/test_portfolio_builder.py -q

uv run python run.py backfill-industry \
  --stock-codes 00700 00005 00941 01299 03690 \
  --force \
  --normalize-existing \
  --show-progress

uv run python run.py select \
  --analysis-mode lightgbm \
  --stock-limit 50 \
  --top-n 3 \
  --days 365 \
  --factor-set alpha158_hk \
  --export-csv output/industry_smoke \
  --show-progress
```

小样本通过后再跑全市场：

```bash
uv run python run.py backfill-industry \
  --force \
  --normalize-existing \
  --max-workers 8 \
  --show-progress

uv run python run.py industry-coverage --show-missing

uv run python run.py select \
  --analysis-mode lightgbm \
  --top-n 10 \
  --days 365 \
  --factor-set alpha158_hk \
  --min-market-cap 30 \
  --min-daily-turnover 500 \
  --max-workers 8 \
  --export-csv output/results \
  --llm-report \
  --show-progress
```

全市场重点检查：

| 文件 | 检查项 |
|---|---|
| `output/results_alpha158_hk_ranking.csv` | 包含 `selection_eligible`、`eligibility_reasons`、`industry_rank`、`industry_score`、`industry_cap` |
| `output/results_alpha158_hk_selected.csv` | 全部 `selection_eligible=True` 且 `industry_rank <= industry_cap` |
| `output/results_alpha158_hk_industry_weights.csv` | 行业权重、预算和 HHI 是否过度集中 |
| `output/results_alpha158_hk_industry_attribution.csv` | 行业内 Alpha、行业机会分、Hot/Cold bucket、OOS gate |
| `docs/report/{日期}_llm.md` | 入选/剔除理由是否能解释行业、质量、估值、流动性 |

行业 Core/Overlay AB test 可按需跑：

```bash
# Core：只做行业内选股，推荐作为基准
uv run python run.py select \
  --analysis-mode lightgbm \
  --top-n 10 \
  --days 365 \
  --factor-set alpha158_hk \
  --industry-selection-mode core \
  --export-csv output/ab_core \
  --llm-report \
  --show-progress

# Core + Overlay：OOS gate 胜率达标后才加行业预算
uv run python run.py select \
  --analysis-mode lightgbm \
  --top-n 10 \
  --days 365 \
  --factor-set alpha158_hk \
  --industry-selection-mode core_overlay \
  --industry-overlay-strength 0.2 \
  --export-csv output/ab_core_overlay \
  --llm-report \
  --show-progress

# Timing only：研究/压力测试用，不建议作为默认实盘模式
uv run python run.py select \
  --analysis-mode lightgbm \
  --top-n 10 \
  --days 365 \
  --factor-set alpha158_hk \
  --industry-selection-mode timing_only \
  --industry-overlay-strength 1.0 \
  --export-csv output/ab_timing_only \
  --llm-report \
  --show-progress
```

## 命令速查

| 命令 | 依赖 | 用途 |
|---|---|---|
| `sync` | 无 | 拉取 OHLCV，产生代码池 |
| `backfill-industry` | `sync` | 补行业、标的类型、fund-like、可交易性 |
| `generate-factors` | `sync` | 生成 `alpha158_hk` 等因子 |
| `select --analysis-mode lightgbm` | `sync`、`generate-factors`，建议先 `backfill-industry` | 推荐选股模式，单次命令内训练和预测 |
| `validate-factors` | `generate-factors` | 仅供 factor 模式选股使用 |
| `select --analysis-mode factor` | `generate-factors`、`validate-factors` | 验证权重驱动的选股模式 |
| `signal-report` | `generate-factors` | 验证信号 recipe 触发后的未来收益 |
| `factor-report` | `generate-factors` | 独立评估因子 IC/RankIC |
| `fetch-alt` | 无 | 可选新闻情感特征，LightGBM 有数据则自动加载 |
| `stock-intelligence-pipeline` | SearXNG、LightRAG、别名表 | 推荐主流程，一条命令生成画像图谱、热度、主题分和 LightGBM 特征 |
| `build-stock-entity-aliases` | `stock_info_registry`、人工别名可选 | 生成股票/公司/产品/模型别名 |
| `expand-stock-entity-aliases` | 深度 evidence、别名表 | 从 evidence 里补产品名、模型名、技术名 |
| `research-stock-deep-profile` | 别名表、本地 SearXNG | 主动检索股票画像/图谱 evidence |
| `lightrag-index-evidence` | 深度 evidence、LightRAG 服务 | 把研究语料写入 LightRAG |
| `lightrag-retrieve-profile-contexts` | LightRAG 索引、别名表 | 多维召回单股/主题画像 context |
| `lightrag-context-to-stock-graph` | LightRAG context | 转成本项目强 schema 图节点/边 |
| `extract-stock-profile-llm` | 深度 evidence、DeepSeek key | 抽取股票画像、deep tags、图谱边 |
| `enrich-supply-chain-graph` | 图节点/边、深度 evidence | 增强产业链、瓶颈、卡点关系 |
| `derive-attention-signals` | evidence、图节点/边 | 生成热度/关注度信号 |
| `rank-theme-opportunities` | 图谱、热度、evidence | 生成主题机会分 |
| `export-theme-score-features` | 主题机会分 | 导出 `theme_opportunity` 标准特征供 LightGBM 读取 |
| `stock-subgraph` | 图节点/边 CSV 或仓库 | 查看某只股票的画像子图 |

常用命令模板：

```bash
uv run python run.py sync --start-date 2014-01-01 --frequencies daily --skip-existing --max-workers 24 --show-progress
uv run python run.py backfill-industry --force --normalize-existing --max-workers 8 --show-progress
uv run python run.py generate-factors --days 365 --factor-set alpha158_hk --max-workers 8 --show-progress
uv run python run.py validate-factors --days 365 --factor-set alpha158_hk --export-csv output/validation_scorecard --show-progress
uv run python run.py signal-report --days 365 --signal-recipes low_price_setup,range_breakout,box_pullback --horizons 20,40,60 --export-csv output/signal_report --show-progress
uv run python run.py factor-report --days 365 --factor-set alpha158_hk --export-csv output/factor_report --show-progress
uv run python run.py fetch-alt --stock-limit 100 --persist-signals --show-progress
```

刚补完行业或 instrument 元数据后，不需要重跑 `sync`，但需要重跑 `select`。`alpha158_hk` 在 `qlib_alpha158` 基础上加入港股定制因子，推荐显式指定。

生成物放置规则：小型 registry 和设计文档可放 `docs/`；上千只股票的报告、LightRAG context、临时图谱、主题评分 CSV 放 `output/`；会参与选股和回测的结构化结果用 `--import-to-warehouse` 写入 Parquet/ClickHouse。

导出文件：

| 文件 | 内容 |
|---|---|
| `{base}_{factor_set}_ranking.csv` | 全市场排名、硬过滤原因、行业排名 |
| `{base}_{factor_set}_selected.csv` | 最终持仓 |
| `{base}_{factor_set}_watchlist.csv` | 观察名单 |
| `{base}_{factor_set}_industry_weights.csv` | 组合行业权重 |
| `{base}_{factor_set}_industry_attribution.csv` | 行业 Core/Overlay 归因 |

行业分层字段：

| 字段 | 含义 |
|---|---|
| `industry_l1/l2/l3` | 真实行业分类 |
| `selection_eligible` / `eligibility_reasons` | 硬过滤结果与剔除原因 |
| `industry_rank` / `industry_score` / `industry_cap` | 行业内候选排名、分数和候选上限 |
| `industry_alpha_score` | 行业内个股 Alpha 分，Core 选股底座 |
| `industry_opportunity_score` | 行业 RPS、breadth、波动率合成的 Overlay 机会分 |
| `combined_selection_score` / `selection_layer` | Core+Overlay 后候选分，以及 `core`/`overlay_boosted`/`fallback` |
| `industry_timing_bucket` | `Hot` / `Neutral` / `Cold` / `Broken` |
| `industry_timing_oos_win_rate` / `industry_timing_oos_ir` | 行业机会分相对 OOS/forward-return 代理收益的胜率与 IR |
| `candidate_cap_base` / `candidate_cap_overlay` | 基础候选名额与 Overlay 后候选名额 |
| `industry_weight_budget` / `industry_budget_reason` | 行业权重预算与预算原因 |
| `industry_concentration_penalty` / `final_score` | 行业集中度惩罚和最终分 |
| `quality_data_coverage` / `quality_missing_fields` | 财务质量数据覆盖 |
| `valuation_metric_used` / `valuation_data_coverage` | 行业内估值指标与覆盖 |
| `portfolio_industry_hhi` | selected 组合行业集中度 |
| `portfolio_industry_hhi_invested` | 已投资仓位归一化后的行业集中度，避免现金仓位稀释 HHI |

`Broken` 行业不会进入 selected；如个股本身信号强，会被放入 watchlist 供观察。Overlay 的 OOS gate 当前使用本次可得的 OOS/forward-return 汇总做代理检验，后续如接入完整历史行业轮动 panel，可替换为严格 walk-forward 口径。

## 辅助功能

```bash
# 单股分析 / 策略套件 / 批次复盘 / 旧一体化入口
uv run python run.py single 00700 --days 365
uv run python run.py suite --days 365 --top-n 3
uv run python run.py review hk_top10_20260516 --export-csv output/review
uv run python run.py all --top-n 10 --days 365 --max-workers 8 --show-progress

# LLM 报告：设置密钥后，在 select 加 --llm-report
export DEEPSEEK_API_KEY=...
uv run python run.py select --analysis-mode lightgbm --top-n 10 --days 365 --llm-report
```

## Python API

```python
from core import StockAnalyzer

analyzer = StockAnalyzer(db_dir="./assets")

result = analyzer.backtest_hk_market(
    days=365,
    top_n=10,
    initial_capital=100000,
    max_workers=8,
    analysis_mode="lightgbm",
    factor_set="alpha158_hk",
)
```

## Web 界面

```bash
# 后端
uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

# 前端，需要 Bun
cd frontend
bun install
bun run dev
```

生产部署：`cd frontend && bun run build`。FastAPI 会托管 `frontend/dist/`，访问 `http://localhost:8000`。

| 页面 | 路由 | 功能 |
|---|---|---|
| 选股结果 | `/` | LightGBM TopN、评分、SHAP 特征解释 |
| 因子 IC 分析 | `/factor-ic` | IC/RankIC 时序和因子汇总 |
| K 线图表 | `/kline` | K 线、均线、成交量、信号标记 |
| 组合回测 | `/portfolio` | 净值、回撤、收益指标、当前持仓 |
