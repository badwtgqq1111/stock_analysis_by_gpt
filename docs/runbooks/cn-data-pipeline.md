# A 股数据分层与训练门禁

## 一键运行

```bash
uv run python scripts/run_cn_pipeline.py
```

该 Python 编排器用 [config/cn_pipeline.toml](../../config/cn_pipeline.toml) 建立任务顺序、运行参数和覆盖率阈值。默认 `all` 会按依赖顺序执行日 K、基本面、因子、市场状态、清洗面板、LightGBM、Transformer、模型打分和选股；配置中关闭的可选阶段会跳过。每次运行生成两份报告：

```text
output/pipeline_reports/cn_pipeline_<timestamp>.json
output/pipeline_reports/cn_pipeline_<timestamp>.md
```

报告包含每个阶段的直接 Python 方法结果、错误信息与覆盖率快照，可用于定位数据源失败、字段缺失或覆盖率不足。流水线本身不再通过 `run.py` 子进程调用命令。

## 配置复用

使用一个总配置，而不是为每条命令维护一份独立配置：

- `[pipeline]` 放市场、开始日期、复权、并发、因子集、选股数量与训练阈值。
- `[daily_bars]`、`[intraday_bars]`、`[alternative]` 只放该层独有的参数。
- `[stages]` 控制一键运行时哪些层启用。

需要一次性的日期、股票池或并发调整时，复制 `config/cn_pipeline.toml` 为新的运行配置，再以 `--config` 指定它。这样默认配置保持稳定，历史研究也可以复现。

```bash
cp config/cn_pipeline.toml config/cn_pipeline.research.toml
uv run python scripts/run_cn_pipeline.py --config config/cn_pipeline.research.toml
```

## 数据层

| 层 | 阶段 | 内容 | 是否训练硬门禁 |
|---|---|---|---|
| 日 K | `daily_bars` | 日频 OHLCV、成交额、换手率与复权标记 | 是 |
| 分时 | `intraday_bars` | 1/5/15/30/60 分钟线，服务于微结构和执行研究 | 否，默认关闭 |
| 基本面 | `fundamental` | 股票快照、历史估值、PIT 财务、行业分类 | 是 |
| 另类数据 | `alternative` | 新闻、公告、事件、搜索证据、主题机会的本地 PIT 导入 | 可选；当前导入后不会自动并入训练面板 |
| 因子 | `features` | `alpha_zoo_hk` 与财务横截面特征 | 是 |
| 清洗面板 | `clean_panel` | 已物化因子、日 K 派生量价、缺失/PIT/质量标记 | 是 |
| 模型 | `lightgbm` / `transformer` / `cnn` | 保存模型工件并输出训练验证指标 | 是 |
| 模型推理 | `model_scores` | 加载已保存模型，对最新 clean panel 截面打分 | 是 |
| 选股 | `selection` | 读取已保存模型的最新分数，生成 LightGBM、Transformer 或 ensemble Top-N | 是 |

日 K 与分时是独立阶段。日 K 默认启用，因为因子和 LightGBM 依赖它；分时默认关闭，只有需要微结构特征、TCA 或执行模型时才打开：

```toml
[stages]
daily_bars = true
intraday_bars = true

[intraday_bars]
lookback_days = 1095
frequencies = ["1min", "5min", "15min", "30min", "60min"]
min_daily_rows_for_intraday = 120
```

分时的 `lookback_days` 独立于日 K 的 `pipeline.start_date`；默认 `1095`，即最近三年。
若设置了 `pipeline.end_date`，分时窗口以该日期向前计算，便于历史研究复现。

单独运行或重跑任一层：

```bash
uv run python scripts/run_cn_pipeline.py --stage daily_bars
uv run python scripts/run_cn_pipeline.py --stage intraday_bars
uv run python scripts/run_cn_pipeline.py --stage fundamental
uv run python scripts/run_cn_pipeline.py --stage features
uv run python scripts/run_cn_pipeline.py --stage clean_panel
uv run python scripts/run_cn_pipeline.py --stage lightgbm
uv run python scripts/run_cn_pipeline.py --stage transformer
uv run python scripts/run_cn_pipeline.py --stage cnn
uv run python scripts/run_cn_pipeline.py --stage model_scores
uv run python scripts/run_cn_pipeline.py --stage selection
```

上述阶段也可以合并为一次单进程运行：

```bash
uv run python scripts/run_cn_pipeline.py --stage all
```

`daily_bars` 只下载并写入日 K，不会自动刷新基本面；`intraday_bars` 也只处理分时。
日 K 默认以腾讯为主、BaoStock 和东方财富为回退；默认链路不使用新浪日线，避免 macOS 上
`py-mini-racer` / V8 在并发初始化时终止同步进程。只有排查特定新浪数据时才在独立配置中显式设置
`daily_bars.data_source = "sina"`；该调用会串行执行。
基本面阶段会在同一 Python 进程内依次调用 `MarketDataService` 的 stock info、估值历史、财务指标和行业补全方法，并作为一个报告层汇总展示。
如果确实需要历史估值与日 K 同批执行，可在配置的 `[daily_bars]` 中显式设置
`complete_data = true`，默认保持关闭以便分层重试。

每个基本面刷新阶段的失败结果会输出失败数量、错误类型汇总和最多 3 个示例代码；
完整失败明细保存在流水线 JSON 报告的 `failed` 字段中。

## 运行编排

不要把所有阶段作为每日任务。`--stage all` 是首次构建或完整重建的便利入口；默认配置中
LightGBM 和 Transformer 均启用，因此它会重训模型。日常生产应按下面的命令矩阵显式运行阶段，
避免把昂贵的训练和严格样本外评估混入每日选股。

| 工作流 | 阶段 | 建议频率 | 触发条件 | 主要产物 |
|---|---|---|---|---|
| 首次构建/数据修复 | `daily_bars`、`fundamental`、`features`、`regime`、`clean_panel`、训练、打分、选股 | 首次；历史数据或清洗契约变更后 | 新机器、重建历史数据、特征 schema/清洗版本变化 | clean panel、模型工件、最新候选 |
| 每日生产 | `daily_bars`、`features`、`regime`、`clean_panel`、`model_scores`、`selection`、`paper_account`、`paper_outcomes` | 每个交易日收盘数据完整后 | 有新的日 K 或新的选股日 | 最新分数、候选、纸面成交、净值和成熟信号收益 |
| 基本面刷新 | `fundamental` | 按数据源披露节奏，建议每周；财报季可每日 | 新财报、估值或行业信息需要刷新 | 股票快照、PIT 财务和估值数据 |
| 定期重训 | `lightgbm`、`transformer`，随后 `model_scores`、`selection` | 每 20 个交易日或每月 | 训练窗口滚动到期；模型/特征/标签参数变化 | 新模型和新选股结果 |
| 严格研究评估 | `oos_predictions`、`model_comparison` | 每月/每季；模型提升和晋升前 | 模型、标签、特征或训练配置变化 | 按折 OOS 预测和模型对比报告 |
| 可选研究 | `cnn`、`graph_temporal`、`intraday_bars`、`alternative`、`strategy_labels` | 按研究计划 | 对应数据和实验假设就绪 | 可选模型或研究数据集 |

从仓库根目录执行。首次准备或需要完整刷新时，建议按阶段运行，便于失败重试和查看报告：

```bash
cd /Users/ccs/code/quant/stock_analysis_by_gpt
uv run python scripts/setup_environment.py
uv run python scripts/check_cn_pipeline.py --skip-online
uv run python scripts/run_cn_pipeline.py --stage daily_bars
uv run python scripts/run_cn_pipeline.py --stage fundamental
uv run python scripts/run_cn_pipeline.py --stage features
uv run python scripts/run_cn_pipeline.py --stage regime
uv run python scripts/run_cn_pipeline.py --stage clean_panel
uv run python scripts/run_cn_pipeline.py --stage lightgbm
uv run python scripts/run_cn_pipeline.py --stage transformer
uv run python scripts/run_cn_pipeline.py --stage model_scores
uv run python scripts/run_cn_pipeline.py --stage selection
```

也可以直接执行默认配置中的启用阶段：

```bash
uv run python scripts/run_cn_pipeline.py
```

默认配置不会执行分时、CNN、另类数据、策略标签、纸面账户、OOS 和模型比较。显式指定 `--stage` 时，即使该阶段在 `[stages]` 中为 `false` 也会执行（另类数据没有 `input_path` 时会报告 `skipped`）。

### 每日生产

日 K 收盘数据可用后运行以下命令。`fundamental` 不必每日执行，只有处于刷新日时才插入到
`daily_bars` 与 `features` 之间。训练工件会被 `model_scores` 复用，因子只在 `features` 更新，
不会在打分阶段重算。

```bash
uv run python scripts/run_cn_pipeline.py --stage daily_bars
uv run python scripts/run_cn_pipeline.py --stage features
uv run python scripts/run_cn_pipeline.py --stage regime
uv run python scripts/run_cn_pipeline.py --stage clean_panel
uv run python scripts/run_cn_pipeline.py --stage model_scores
uv run python scripts/run_cn_pipeline.py --stage selection
uv run python scripts/run_cn_pipeline.py --stage paper_account
uv run python scripts/run_cn_pipeline.py --stage paper_outcomes
```

`paper_account` 在 `selection` 之后每日运行：它会按 T+1 规则推进已有虚拟订单、更新持仓与净值。
`paper_outcomes` 也建议每日运行：它不重新训练，只把历史信号与新到达的 1/5/20/60 日结果对齐；
也可以按周补跑，但每日执行更容易及时发现成熟信号表现异常。

### 定期重训

模型不会因为读取新数据而自动重训。每 20 个交易日或每月执行一次；特征集合、清洗版本、标签
定义或模型参数变化后，也应立即重训并重新打分选股：

```bash
uv run python scripts/run_cn_pipeline.py --stage lightgbm
uv run python scripts/run_cn_pipeline.py --stage transformer
uv run python scripts/run_cn_pipeline.py --stage model_scores
uv run python scripts/run_cn_pipeline.py --stage selection
```

### 严格样本外评估

`oos_predictions` 不是每日推理任务。它会在每一个时间滚动折重新训练模型并生成严格隔离的历史
预测，适合模型晋升前、模型/数据契约变更后和每月或每季的研究复核。`model_comparison` 读取这些
已落盘预测进行比较，不重训模型，必须紧跟在 OOS 预测更新后执行；没有新的 OOS 输出时无需每日运行。

```bash
uv run python scripts/run_cn_pipeline.py --stage oos_predictions
uv run python scripts/run_cn_pipeline.py --stage model_comparison
```

`model_comparison` 的 IC、RankIC、IR、分组收益和换手用于比较模型，不等同于纸面账户的可实现收益。
观察期、交易成本、成交约束与持仓规则仍需在 `paper_account` 和 `paper_outcomes` 中验证。

## 选股质量验证与虚拟持仓

`selection` 只生成候选和目标权重，不代表已经验证盈利。建议每次选股后执行：

```bash
uv run python scripts/run_cn_pipeline.py --stage paper_outcomes
uv run python scripts/run_cn_pipeline.py --stage paper_account
```

- `paper_outcomes` 将信号与未来 1/5/20/60 个交易日收盘价对齐，输出毛收益、扣费收益、最大不利 excursion（MAE）、最大有利 excursion（MFE）及相对市场代理收益。文件位于 `output/paper_trading/cn_signal_outcomes.{csv,json,md}`。信号未满观察期会标记为 `pending`，不能当作亏损或盈利统计。
- `paper_account` 按 T+1 下一交易日开盘、手续费和滑点回放虚拟账户，输出 `orders.csv`、`fills.csv`、`positions.csv`、`nav.csv` 和 `paper_account_summary.json`。它是持仓事件记录，不是实盘委托。
- 质量判断至少观察滚动 20/60 日的命中率、平均净收益、超额收益、Sharpe、最大回撤、换手、成交率和 pending 比例；单次 Top-N 不能证明策略有效。正式评估应启用 `oos_predictions` 和 `model_comparison`，使用时间滚动、purge/embargo 的样本外预测。

当前配置已按小额虚拟账户设置：初始资金 `45,000`、最多 `3` 只、总仓位 `95%`。候选仍由模型分数排序，入选后的权重使用近 20 日年化波动率的逆波动率分配：波动率越高，目标权重越低；同时受单票上限、行业上限、成交容量和换手约束。缺少行业数据时不会把所有股票错误地视为同一行业，但行业中性约束需要补齐行业映射后才完整生效。

当前最近一次结果写入 `output/results_cn/cn_ensemble_selected.csv`：3 只持仓目标权重合计 95%，纸面账户记录在 `output/paper_trading/account/`。由于信号日期为最近交易日且后续行情尚未满观察期，`paper_outcomes` 可能全部为 `pending`；待未来交易日到达后重复执行即可自动成熟并更新统计。

## 可选研究阶段

策略标签（底部反弹、趋势跟踪和首/二板的日线代理）：

```bash
uv run python scripts/run_cn_pipeline.py --stage strategy_labels
```

`paper_outcomes` 默认用横截面中位数作为市场代理；在 `[paper_outcomes]` 设置 `benchmark_path` 后才使用真实指数 CSV（列为 `trade_date,close` 或 `date,price`）。`paper_account` 输出订单、成交、持仓、净值和回撤 CSV，并写入独立的纸面交易数据集。

CNN 训练和打分：

```bash
uv run python scripts/run_cn_pipeline.py --stage cnn
```

执行前需在 `[model_scores]` 填写 CNN 的 model/manifest 路径；确认 OOS 增益后再把 CNN 纳入 ensemble。

严格 OOS 预测和模型比较：先在 `[oos_predictions]` 设置模型列表，再在模型晋升前或定期研究复核时运行：

```toml
[oos_predictions]
models = ["lightgbm", "transformer", "cnn"]
# 每 5 个交易日取一个决策日，模拟周度调仓；设为 1 才会逐日评估。
prediction_stride = 5
```

```bash
uv run python scripts/run_cn_pipeline.py --stage oos_predictions
uv run python scripts/run_cn_pipeline.py --stage model_comparison
```

当前配置为 `models = ["transformer"]`，以便先快速复核 Transformer 的 OOS 基线。需要横向比较时，将 LightGBM 与 Transformer 同时加入 `models`，它们会使用同一批折叠、同一批决策日比较 IC、IR、超额收益和回撤。每个 Transformer 折只训练一次，并将该折所有目标日放入同一段因果上下文批量评分；CNN 默认不加入，确认前两者的基线后再配置 `models`。运行日志会先输出每折的训练区间、测试区间和实际评分日期数，再显示训练 epoch、批量评分上下文和 sequence 进度；`Transformer OOS folds: 0/5` 表示第一折正在做首次数据准备，并非任务停滞。

时序模型不会直接将全部因子展开成序列。训练时从当前训练折中按标签相关性、覆盖率和方差筛选 `transformer_max_feature_pairs` 个 `*_clean` 因子，并保留对应的 `*_is_missing` 掩码；`transformer_max_samples` 在全股票池、全时间段均衡抽取序列窗口。默认值为 128 个因子对、12,000 条样本，避免 60 日、千维因子面板在序列复制时占用数十 GB 内存。模型工件记录最终特征列，因此推理仍严格使用与训练一致的特征集合。

图时序 OOS 还必须提供带 `stock_code,industry_l1,available_at` 的历史行业映射 CSV，并把图模型预测文件加入 `[model_comparison].prediction_paths`：

```toml
[oos_predictions]
models = ["lightgbm", "transformer", "cnn", "graph_temporal"]
industry_mapping_path = "input/cn_industry_pit.csv"

[model_comparison]
prediction_paths = { lightgbm = "output/oos_predictions/cn_lightgbm_oos_predictions.csv", transformer = "output/oos_predictions/cn_transformer_oos_predictions.csv", cnn = "output/oos_predictions/cn_cnn_oos_predictions.csv", graph_temporal = "output/oos_predictions/cn_graph_temporal_oos_predictions.csv" }
```

另类数据使用本地证据 CSV，先配置 `[alternative].input_path`，再执行：

```bash
uv run python scripts/run_cn_pipeline.py --stage alternative
```

CSV 至少要有 `stock_code` 和 `published_at` 或 `available_at`；导入结果会做 PIT 时间校验并生成报告，但当前不会自动改写 `clean_feature_panel`，因此不能直接宣称已进入模型训练。

## 训练门禁与可用样本

原则是“有多少合格样本就使用多少”，而不是要求全市场每只股票都具备所有字段。下载报告会给出各层覆盖率；低覆盖的可选字段保留缺失标记，不能让它们抹掉可用的价量样本。

生成因子、清洗面板和模型训练前，编排器只要求：

1. `daily/qfq` OHLCV 行数达到 `min_ohlcv_rows` 的股票不少于 `pipeline.min_training_stocks`。
2. `selection` 阶段的合格因子股票数同样不少于 `min_training_stocks`。

股票快照、历史估值、财务和行业的覆盖率仍按 `min_fundamental_coverage` 报告，但它们是可选特征的质量目标，不会阻断已有合格价量/因子样本。分时和另类数据也不是当前 LightGBM 的硬依赖。

## 训练模型

推荐按以下顺序执行：

```bash
uv run python scripts/run_cn_pipeline.py --stage features
uv run python scripts/run_cn_pipeline.py --stage clean_panel
uv run python scripts/run_cn_pipeline.py --stage lightgbm
uv run python scripts/run_cn_pipeline.py --stage transformer
uv run python scripts/run_cn_pipeline.py --stage cnn
```

`clean_panel` 从已经写入 feature 层的因子长表和 clean 日 K 读取数据，输出
`clean_feature_panel`，不会再次执行因子公式。LightGBM 会保存到
`output/models/cn/lightgbm/<factor_set>/model.txt`，Transformer 保存到对应目录的
`model.pt`；CNN 保存到 `output/models/cn/cnn/<factor_set>/model.pt`。模型目录均有
`model_manifest.json`，记录特征列、清洗版本、训练窗口和标签定义。默认 `--stage all` 训练
LightGBM 和 Transformer，并执行模型打分与持久化分数选股；CNN 默认关闭，需显式执行 `--stage cnn`
并完成 OOS 验证后再启用。

模型训练还会执行 `[model_features]` 质量过滤：全缺失、低覆盖和常量特征不会进入新模型；过滤
结果写入 `model_manifest.json` 的 `extra.feature_quality`。该规则只影响新训练工件，因此修改阈值后
需要重训对应模型，已存在的模型仍按自身 manifest 推理。

`model_scores.min_cross_section_coverage = 0.95` 防止一只股票的孤立新日期覆盖完整市场截面。评分器会
选择满足该阈值的最近交易日，并在输出中记录 `score_date_quality`，其中包括原始最新日期、其股票数和
是否跳过该不完整日期。
只下载或只更新数据时，指定对应的数据 stage，避免启动训练。

`clean_panel` 的输入依赖是：

- `assets/data/feature/features` 中与配置一致的 `market=CN`、`frequency=daily`、`adjust=qfq`、`feature_set=alpha_zoo_hk` 因子长表；
- `assets/data/clean/ohlcv` 中同市场、同频率、同复权方式的日 K；
- `[clean_panel]` 的日期窗口和 `cleaning_version`。

清洗阶段默认每 10 股批量读取因子，一次性透视为 `(trade_date, stock_code)` 紧凑宽表，再生成
量价派生特征、缺失掩码和 PIT 标记，写入 `assets/data/feature/clean_feature_panel`。训练宽表使用
float32 值和 bool 掩码，不再生成数亿行审计长表；raw 因子继续由 `features` 层保留。因子数据量
很大时，阶段会显示 `clean panel stocks` 和 `feature batch loaded` 进度，不会把整个长表一次性
读入内存。批大小可通过 `[clean_panel].feature_batch_size` 调整。`cn_backtest_coverage_report` 将
`backtest_ready`（存在可用 OHLCV 股票且读取链路正常）与 `full_universe_ready`（全市场股票均达到
`min_ohlcv_rows`）分开报告。未达到行数阈值的股票作为 `coverage_warnings` 和
`ohlcv.excluded_stock_codes` 记录，不会让已有的合格股票停止训练；控制台只显示数量和样本代码。
真正阻断训练的是 `clean_feature_panel` 为空、PIT 无效或合格股票数低于
`pipeline.min_training_stocks`。

训练成功后可直接运行 `model_scores`。该阶段校验 manifest 的特征 schema 指纹，读取最近
`days` 天的 clean panel，仅输出最新交易日的排序分数：

```text
output/model_scores/cn_lightgbm_scores.csv
output/model_scores/cn_transformer_scores.csv
output/model_scores/cn_cnn_scores.csv
```
缺少模型文件、manifest、clean panel 或 schema 不一致时，阶段失败并写入流水线报告，不会静默
回退到重新计算因子或重新训练。

`selection` 读取 `output/model_scores` 中的模型分数，不会调用旧 `core/lightgbm_analysis.py`，也不会
重新计算因子或重新训练模型。
默认等权组合 LightGBM 与 Transformer 的百分位分数，并导出：

```text
output/results_cn/cn_ensemble_selected.csv
```

在 `[selection]` 中将 `model` 设为 `lightgbm`、`transformer`、`cnn` 或 `ensemble`。当前 ensemble
要求 LightGBM 和 Transformer 在相同最新交易日都有分数，避免用不同时点的预测静默混合；CNN 先单独
进行 OOS 比较，确认增益后再进入 ensemble。

### 价量形态信号层（Donchian 通道突破）

`[selection.signals]` 在纯模型排序之外增加一层规则信号。启用后，选股阶段会：

1. 取模型短名单（`scan_top_k`，默认 1200）作为扫描池；
2. 读取截至选股日的 `lookback_sessions`（默认 120）个交易日行情，不含未来数据；
3. 运行 `donchian_pullback`（上轨 `max(High[t-N..t-1])`、下轨 `min(Low[t-N..t-1])`）与 `range_breakout`；
4. 命中 `allowed_setup_types`、`signal_score >= min_score` 且模型分位 `>= min_model_score` 的标的，
   按 `override_rank`（默认 `volume_ratio_20`，即突破放量强度）排序，取前 `max_overrides` 个作为
   `signal_override` 候选并入候选池；
5. 组合优化时这些标的优先占位，并获得不低于 `forced_min_weight` 的目标权重。

```toml
[selection.signals]
enabled = true
recipes = ["donchian_pullback", "range_breakout"]
min_score = 60.0
min_model_score = 85.0
scan_top_k = 1200
max_overrides = 2
forced_min_weight = 0.08
override_rank = "volume_ratio_20"

[selection.signals.recipe_params.donchian_pullback]
window = 20
tol_low = 0.03
```

`setup_type` 语义：`donchian_breakout` 为当日收盘或盘中上穿上轨（放量确认，对应"次日回调买入"的
入场信号日）；`donchian_pullback` 为突破后 `1..max_sessions_since_breakout` 个交易日内收盘守在
上轨附近且缩量。`enabled = false` 时完全回到纯模型 Top-N 行为，输出与未启用该层时逐行一致。
由于信号标的会占用持仓名额，启用时应同步调整 `[selection.portfolio_constraints].max_holdings`
（默认 6 = 2 个信号位 + 4 个模型位）。实现与验收记录见
[P0_13 Donchian 通道突破接入选股计划](../todo/P0_13_donchian_breakout_selection_plan.md)。

### 让模型学到 Donchian 通道状态

`clean_panel` 会从日 K 派生价量特征，其中包含 9 个 Donchian 通道状态列：

```text
pv_donchian_pos_20            (close - min(low,20)) / (max(high,20) - min(low,20))
pv_donchian_width_20          通道宽度 / close
pv_donchian_dist_upper_20     close / max(high,20) - 1
pv_donchian_break_20          close > max(high[t-20..t-1])   收盘突破（无前视）
pv_donchian_break_count_20    近 20 日突破次数
pv_donchian_since_break_20    距最近一次突破的交易日数（上限 60）
pv_donchian_break_volume_20   突破 × volume_ratio_20d
pv_donchian_pierce_20         盘中上穿上轨、收盘落回通道内
pv_donchian_pierce_volume_20  回踩 × volume_ratio_20d
```

模型输入取自面板中所有 `*_clean` 与 `*_is_missing` 列，因此这 9 列会自动进入 LightGBM、
Transformer 和 CNN 的训练与推理，无需额外的特征开关。要让它们真正影响结果，需重建面板并重训：

```bash
uv run python scripts/run_cn_pipeline.py --stage clean_panel
uv run python scripts/run_cn_pipeline.py --stage lightgbm
uv run python scripts/run_cn_pipeline.py --stage transformer
uv run python scripts/run_cn_pipeline.py --stage model_scores
uv run python scripts/run_cn_pipeline.py --stage selection
```

相关约束：`[model_features] min_feature_coverage`（默认 0.05）会剔除低覆盖特征；Transformer / CNN
的 `max_feature_pairs`（默认 128）决定有多少对时间序列表征进入网络。标签窗口由
`label_horizon` 控制（默认 20 个交易日，标签为 `forward_return_{N}d`），突破类事件若要用更短的
收益窗口，改小该值即可，`embargo_days` 会按标签名中的 `_Nd` 自动解析。

在同一进程里先后训练 LightGBM 与 PyTorch 模型会在 macOS 上因共用 OpenMP 运行时互相阻塞，
因此时间序列模型应放在独立进程中训练/打分（`scripts/score_cn_model.py` 每个模型一个 worker）。

### LightGBM 早停与评估指标

标签是日内横截面排名，其方差（≈1/12）就是 L2 的下界，因此 L2 无法区分「学到排序」与
「常数预测」。默认配置按日内 IC 早停并关闭早停裁剪：

```toml
[lightgbm]
eval_metric = "daily_ic"      # daily_ic | l2 | none
early_stopping_rounds = 0     # LightGBM 会把保存的 booster 裁剪到 best_iteration
min_trees = 100               # 裁剪后树数不足时自动去掉早停重训
n_estimators = 500
```

历史故障：在平坦 L2 上早停于第 3 轮，保存的模型只剩 3 棵树（500 棵被裁剪），打分区间
塌缩成 100 只并列最高分，选股排序实际由 Transformer 单独决定。修复后 500 棵树全部保留，
5335 只股票的打分互不相同，日内 IC ≈ 0.072。排查时用 `validation_ic` 而不是
`validation_mse` 判断模型质量。

`[selection] ensemble_weights` 可覆盖 regime 发布的模型权重；regime 权重会保留在输出的
`regime_model_weights` 列中，便于对比。信号扫描与候选排序都使用生效权重。

#### 两个独立 sleeve 与风控过滤

事件研究（2024-01..2026-09，全市场 3.42M 股票日，市场中性）显示：20 日通道突破在样本期是
零到负期望（+1 日 +0.15% / +5 日 -0.29%；放量确认后 +5 日 -0.56%），而涨停型动量是唯一
稳健为正的事件（+1 日 +1.30% / +5 日 +0.68%，10 日衰减到零）。因此信号层拆成两个独立 sleeve：

```toml
[selection.signals]
max_overrides = 2        # setup sleeve 名额
forced_min_weight = 0.08
forced_max_weight = 0.20 # setup sleeve 单只上限

[selection.signals.momentum_sleeve]
enabled = true
slots = 1                # 动量独立名额
min_weight = 0.05
max_weight = 0.10        # 动量独立上限
min_model_score = 0.0    # 模型在该事件上无证据优势，用独立预算与止损控制风险

[selection.signals.risk_filters]
exclude_st = true
min_market_cap = 5000000000.0
min_median_amount_20d = 50000000.0
```

`limit_momentum` recipe 只识别强势动量日，输出 `stop_price`（min(当日最低价, 收盘×(1-5%))）与
`expected_holding_days = 5`；被拒标的及原因写入 `signals.risk_rejected` 供审计。

注意：动量 sleeve 的扫描域**不继承模型短名单**。实测 2026-09-11 全市场 24 只"涨停 + 流动性 +
市值 + 非 ST"标的中 0 只位于模型 top-1200，因此实现里单独做了一次轻量涨停预筛
（`_momentum_candidates`，只读最近 14 天收盘）。

#### 持仓卖出规则（`--stage exits`）

流水线的选股侧只排名买入；卖出侧由 `--stage exits` 独立评估，输入是持仓清单
`config/holdings_cn.csv`（`stock_code, shares, cost_price`），输出
`output/results_cn/cn_exit_plan.csv` 与 `.md`：

```bash
uv run python scripts/run_cn_pipeline.py --stage exits
```

规则分三类（依据见 `factor_engine/portfolio/exits.py` 的模块说明与
[P0_13 第 16 节](../todo/P0_13_donchian_breakout_selection_plan.md)）：

- **风险型（硬退出）**：ST 名称、停牌 > 5 个交易日、20 日中位成交额 < 5000 万、模型排名跌出
  `min_model_percentile`（默认 30 分位）；
- **兑现型（减仓 1/3，不清仓）**：Donchian 位置 ≥ 0.80 或 20 日涨幅 ≥ +15%；
- **结构型（减到上限）**：单只权重 > `max_weight`（默认 35%）。

`[exits.rules].stop_loss_pct` 默认 0（关闭）：2024-2026 样本里 20 日跌幅 > 15% 的标的未来
20 日超额 +1.6~2.2%，固定百分比止损会卖在期望最优的状态上。需要传统止损时显式设置该值。

#### 执行层约束：整手可买 + 每周再平衡

```toml
[selection]
rebalance_stride_days = 5   # 距上次再平衡不足 5 个交易日则沿用上一版组合；exits 仍每日运行

[selection.affordability]
enabled = true
equity = 43137.82           # 账户总资产（持仓 + 现金）
lot_size = 100
budget_ratio = 0.15         # 单只预算占比 ≈ gross_exposure / 持仓数
# => 可买价格上限 = equity * budget_ratio / lot_size = 64.71 元
```

候选池先按价格上限过滤，组合优化后再跑"整手修复"循环：最终权重买不到 1 手的标的会被剔除
并重新优化（策略强制的 sleeve 名额不剔除），保证输出的目标权重都能以 100 股整数倍执行。
输出新增 `one_lot_value / lots_at_target / lot_fillable` 三列与 `lot_affordability`、
`lot_execution` 汇总。`--force-rebalance` 可跳过 stride 立即再选。

日常推理应加载已批准模型，只处理新日期的 clean panel；模型 schema、清洗版本或标签定义变化时
必须重新训练。LightGBM 支持通过 `warm_start_path` 使用旧 Booster 增量加树，但仍按固定周期从
replay window 全量重训。

Transformer 同样支持 checkpoint 增量微调：在 `[transformer]` 中同时设置
`warm_start_path` 和 `warm_start_manifest_path`。训练会校验清洗版本、因子集、特征列顺序和网络
结构，任何不一致都会终止，而不会部分加载权重。

CNN 使用相同的 `[sample, lookback, feature] + missing mask` 数据集，通过 1D 卷积提取局部时间模式，
也支持 CUDA/MPS/CPU 自动选择。目前 CNN 提供完整训练、保存、加载和打分路径；checkpoint 增量微调
将在 CNN 的独立 OOS 基线验证后启用。

### 模型公平评估

`factor_engine.ml.walk_forward` 提供 expanding walk-forward 折和统一指标计算。LightGBM、Transformer
和 CNN 的比较必须使用相同股票池、标签 horizon、purge/embargo、交易成本和 Top-N 规则，报告
RankIC、Top/Bottom 分组收益、多空收益、超额收益、换手和最大回撤。当前评估器可复用历史预测结果
生成 CSV/JSON/Markdown 报告；每折重训并产出严格 OOS 预测后，才可将结果用于生产模型晋级。

### Transformer 设备选择

`[transformer].device` 和 `[model_scores].transformer_device` 默认均为 `"auto"`。设备优先级为：

```text
CUDA（NVIDIA） -> MPS（Apple Metal GPU） -> CPU
```

因此 Apple Silicon（包括 M5 Max）使用 PyTorch MPS 在 GPU 上训练和推理；Linux/Windows 有 NVIDIA
GPU 时使用 CUDA；其余环境自动使用 CPU。可以将配置明确设为 `"mps"`、`"cuda"` 或 `"cpu"`，
请求不可用设备会直接报错，不会静默改变设备。模型 manifest 记录实际训练设备。

Apple Neural Engine 不是当前 PyTorch 自定义 Transformer 的训练后端。MLX 同样主要使用 Apple GPU
和统一内存；它适合作为未来独立的 Apple Silicon 后端，但需要单独实现模型和训练循环，不能与
当前 PyTorch checkpoint 混用。

当前 Transformer 使用连续数值 token：每日特征、缺失掩码、线性 feature embedding 和可学习位置
编码。它不是文本模型，因此不对量价/因子使用 NLP tokenizer。后续优化优先级和文本、日历、PIT
对齐、walk-forward 评估要求见 [数据质量与清洗面板](data-quality-cleaning.md)。

模型输入按股票日期过滤：某条样本缺少核心 OHLCV 或因子窗口时剔除；缺少财务、行业或另类字段时保留该样本，并传递 `is_missing` / `is_imputed` 特征。Transformer/CNN 则保留同样的样本规则，并额外使用 sequence mask。

## A 股另类数据与公司搜索

旧的 `fetch-alt` 仍只解析港股股票池，不能用于 A 股。CN 使用 `alternative` 阶段导入本地证据 CSV；默认关闭是有意的，因为导入结果目前只落地为 PIT 安全的另类数据层和报告，尚未自动并入 `clean_feature_panel`：

- `stock_code`：统一 A 股代码，例如 `600000.SH`。
- `available_at`：数据在当时可观察到的时间，避免未来函数。
- `source`、`title`、`content`/`score`：保留来源和原始证据。
- `event_type` 或标准化特征列：新闻情绪、公告、研报、搜索热度、产业链事件等。

公司相关搜索信息应先落为带时间戳的 evidence/事件表，再通过 CN importer 转为日频特征并写入 feature layer。只有完成 PIT 时间对齐、覆盖率检查和缺失标记后，才可以加入 LightGBM 或 Transformer 训练集；在当前实现中请把它视为独立研究输入，不要假定已经参与模型训练。

目前仓库的 `research-stock-tags`、`searxng-research-stock-tags` 和 `stock-intelligence-pipeline` 仍以 HK registry 为默认输入。它们不能直接视为 A 股另类数据管道；CN 需要单独的股票名称/别名 registry 与 importer，避免代码映射、来源覆盖和可得时间出错。
