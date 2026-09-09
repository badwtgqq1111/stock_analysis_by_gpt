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
基本面阶段会在同一 Python 进程内依次调用 `MarketDataService` 的 stock info、估值历史、财务指标和行业补全方法，并作为一个报告层汇总展示。
如果确实需要历史估值与日 K 同批执行，可在配置的 `[daily_bars]` 中显式设置
`complete_data = true`，默认保持关闭以便分层重试。

每个基本面刷新阶段的失败结果会输出失败数量、错误类型汇总和最多 3 个示例代码；
完整失败明细保存在流水线 JSON 报告的 `failed` 字段中。

## 推荐执行顺序

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

日常增量只需按数据是否更新选择阶段，模型不会因为读取新数据而自动重训：

```bash
uv run python scripts/run_cn_pipeline.py --stage daily_bars
uv run python scripts/run_cn_pipeline.py --stage fundamental
uv run python scripts/run_cn_pipeline.py --stage features
uv run python scripts/run_cn_pipeline.py --stage regime
uv run python scripts/run_cn_pipeline.py --stage clean_panel
uv run python scripts/run_cn_pipeline.py --stage model_scores
uv run python scripts/run_cn_pipeline.py --stage selection
```

只有在需要更新模型时才执行：

```bash
uv run python scripts/run_cn_pipeline.py --stage lightgbm
uv run python scripts/run_cn_pipeline.py --stage transformer
```

训练阶段会复用已物化的 `clean_feature_panel`，不会每次打分时重新计算因子。

## 可选研究阶段

策略标签（底部反弹、趋势跟踪和首/二板的日线代理）：

```bash
uv run python scripts/run_cn_pipeline.py --stage strategy_labels
```

纸面收益评估和账户回放必须在 `selection` 之后运行：

```bash
uv run python scripts/run_cn_pipeline.py --stage paper_outcomes
uv run python scripts/run_cn_pipeline.py --stage paper_account
```

`paper_outcomes` 默认用横截面中位数作为市场代理；在 `[paper_outcomes]` 设置 `benchmark_path` 后才使用真实指数 CSV（列为 `trade_date,close` 或 `date,price`）。`paper_account` 输出订单、成交、持仓、净值和回撤 CSV，并写入独立的纸面交易数据集。

CNN 训练和打分：

```bash
uv run python scripts/run_cn_pipeline.py --stage cnn
```

执行前需在 `[model_scores]` 填写 CNN 的 model/manifest 路径；确认 OOS 增益后再把 CNN 纳入 ensemble。

严格 OOS 预测和模型比较：先在 `[oos_predictions]` 设置模型列表，再运行：

```toml
[oos_predictions]
models = ["lightgbm", "transformer", "cnn"]
```

```bash
uv run python scripts/run_cn_pipeline.py --stage oos_predictions
uv run python scripts/run_cn_pipeline.py --stage model_comparison
```

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
