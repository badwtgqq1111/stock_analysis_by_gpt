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
| 资金流 | `moneyflow` | 个股资金流、来源交叉口径、龙虎榜及滚动特征 | 是（日期对齐） |
| 分时 | `intraday_bars` | 1/5/15/30/60 分钟线，服务于微结构和执行研究 | 否，默认关闭 |
| 基本面 | `fundamental` | 股票快照、历史估值、PIT 财务、行业分类 | 是 |
| 另类数据 | `alternative` | 新闻、公告、事件、搜索证据、主题机会的本地 PIT 导入 | 可选；当前导入后不会自动并入训练面板 |
| 因子 | `features` | `alpha_zoo_hk` 与财务横截面特征 | 是 |
| 清洗面板 | `clean_panel` | 已物化因子、日 K 派生量价、缺失/PIT/质量标记 | 是 |
| 模型 | `lightgbm` / `transformer` / `cnn` | 保存模型工件并输出训练验证指标 | 是 |
| 模型推理 | `model_scores` | 加载已保存模型，对最新 clean panel 截面打分 | 是 |
| 预选 | `preselection` | 模型取 `pipeline.preselection_model_slots` 只（当前 8），并按每种信号类型各取 2 只，形成候选池 | 是 |
| PK 持仓 | `pk` | 对预选池执行风险、成本、流动性和持仓数优化，生成最终组合 | 是 |
| 卖出评估 | `exits` | 对持仓清单逐日评估风控/兑现/结构规则，产出退出计划（含 ATR 缩放止损） | 否（只读） |
| 兼容选股 | `selection` | 旧版一步式 Top-N 选股入口；研究新流程应使用 `preselection`→`pk` | 是 |

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
uv run python scripts/run_cn_pipeline.py --stage moneyflow
uv run python scripts/run_cn_pipeline.py --stage intraday_bars
uv run python scripts/run_cn_pipeline.py --stage fundamental
uv run python scripts/run_cn_pipeline.py --stage features
uv run python scripts/run_cn_pipeline.py --stage clean_panel
uv run python scripts/run_cn_pipeline.py --stage lightgbm
uv run python scripts/run_cn_pipeline.py --stage transformer
uv run python scripts/run_cn_pipeline.py --stage cnn
uv run python scripts/run_cn_pipeline.py --stage model_scores
uv run python scripts/run_cn_pipeline.py --stage preselection
uv run python scripts/run_cn_pipeline.py --stage pk
```

重选与 `rebalance` 参数：

- `preselection` 遵守 `[selection].rebalance_stride_days`（当前为 `5`）。不足 5 个交易日时，默认命令可能返回 `carried_forward` 并沿用上一版预选池；需要立即重新生成模型 4 只和各信号 2 只时，必须加 `--force-rebalance`。
- `pk` 当前固定对传入的预选池重新做一次最终优化（内部使用 `rebalance_stride_days=1`），因此 `--force-rebalance` 不是必需的；保留它可以明确表达本次强制重算。
- 两步都基于最新分数与信号重选时：

```bash
uv run python scripts/run_cn_pipeline.py --stage preselection --force-rebalance
uv run python scripts/run_cn_pipeline.py --stage pk
```

只比较同一个预选池的最终权重时，只运行 `pk` 即可；它不会重新扫描全市场信号。

上述阶段也可以合并为一次单进程运行：

```bash
uv run python scripts/run_cn_pipeline.py --stage all
```

`daily_bars` 只下载并写入日 K，不会自动刷新基本面；`moneyflow` 按同一交易日窗口拉取资金流和龙虎榜，并保存原始快照与滚动特征；`intraday_bars` 也只处理分时。资金流默认要求与日 K 匹配率达到 98%；缺失保留 `is_missing`，不填零。
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
| 首次构建/数据修复 | `daily_bars`、`fundamental`、`features`、`regime`、`clean_panel`、训练、打分、`preselection`、`pk`、`exits` | 首次；历史数据或清洗契约变更后 | 新机器、重建历史数据、特征 schema/清洗版本变化 | clean panel、模型工件、最新候选与组合变更 |
| 每日生产 | `daily_bars`、`moneyflow`、`regime`、`features`、`clean_panel`、`model_scores`、`preselection`、`pk`、`exits`、`paper_account`、`paper_outcomes` | 每个交易日收盘数据完整后 | 有新的日 K 或新的选股日 | 最新分数、候选、最终组合、退出计划、纸面成交、净值和成熟信号收益 |
| 基本面刷新 | `fundamental` | 按数据源披露节奏，建议每周；财报季可每日 | 新财报、估值或行业信息需要刷新 | 股票快照、PIT 财务和估值数据 |
| 定期重训 | `lightgbm`、`transformer`，随后 `model_scores`、`preselection`、`pk`、`exits` | 每 20 个交易日或每月 | 训练窗口滚动到期；模型/特征/标签参数变化 | 新模型、新选股结果和新的退出计划 |
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
uv run python scripts/run_cn_pipeline.py --stage preselection --force-rebalance
uv run python scripts/run_cn_pipeline.py --stage pk
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

先加载环境文件。非交互式 shell（launchd / systemd / cron / 直接跑脚本）不读 `~/.bashrc`，
ClickHouse、Tushare 或代理变量缺失会让 `daily_bars`、`moneyflow` 立刻失败：

```bash
set -a; . config/scheduler_env.sh; [ -f config/notify.env ] && . config/notify.env; set +a
```

```bash
uv run python scripts/run_cn_pipeline.py --stage daily_bars
uv run python scripts/run_cn_pipeline.py --stage moneyflow
uv run python scripts/run_cn_pipeline.py --stage regime
uv run python scripts/run_cn_pipeline.py --stage features
uv run python scripts/run_cn_pipeline.py --stage clean_panel
uv run python scripts/run_cn_pipeline.py --stage model_scores
uv run python scripts/run_cn_pipeline.py --stage preselection --force-rebalance
uv run python scripts/run_cn_pipeline.py --stage pk
uv run python scripts/run_cn_pipeline.py --stage exits
uv run python scripts/run_cn_pipeline.py --stage paper_account
uv run python scripts/run_cn_pipeline.py --stage paper_outcomes
```

顺序与 `scripts/run_daily_production.sh` 的阶段序列一致（`daily_bars → moneyflow → regime →
features → clean_panel → model_scores → preselection → pk → exits → paper_account → paper_outcomes`）；
自动化脚本另外负责单实例锁、收盘时间判断、幂等标记和报告/通知，手工补跑时按上面逐条执行即可。

三个最容易踩的点：

- **`preselection` 受 stride 限制**：`[selection].rebalance_stride_days = 5`，距上次再平衡不足 5 个
  交易日时会返回 `carried_forward` 并沿用上一版预选池。需要当天立刻换池时加 `--force-rebalance`；
  `pk` 对传入的候选池固定按 `stride=1` 重算权重，因此它不需要这个参数（写上只是显式声明本次强制重算）。
- **`exits` 是独立阶段，不在 `pk` 里**：选股侧只产生买入组合，止损/减仓/风控退出由 `--stage exits`
  读 `config/holdings_cn.csv` 单独评估。漏跑它，持仓当天不会接受任何风控规则检查。
- **`model_scores` 不重训**：它只加载已保存的模型，对最新 clean panel 截面打分；当前生产模型的训练
  窗口是 2025-09-19~2026-04-27，所以每日只需要 `clean_panel` → `model_scores`。要让打分用上新数据
  学到的东西，必须同时重跑 `--stage lightgbm` 与 `--stage transformer`：集成权重同时使用两个模型，
  只重训一个会让两半来自不同特征版本。

跑完后的三个检查点：

```bash
# 1) 候选池与最终组合是不是当天、有没有通过整手可买约束
head -2 output/results_cn/cn_ensemble_preselected.csv
head -2 output/results_cn/cn_ensemble_selected.csv

# 2) 资格门 / universe 过滤是否按配置开火（读最新流水线报告的 preselection 摘要）
uv run python -c "import json,glob,os;f=max(glob.glob('output/pipeline_reports/*.json'),key=os.path.getmtime);d=json.load(open(f));print(os.path.basename(f));[print(' ',s['name'],json.dumps(s.get('summary',{}).get('startup_gate'),ensure_ascii=False),json.dumps((s.get('summary') or {}).get('universe_filter'),ensure_ascii=False)) for s in d['stages'] if (s.get('summary') or {}).get('startup_gate')]"

# 2b) 组合定权与波动目标
uv run python -c "import json;d=json.load(open('output/results_cn/cn_ensemble_portfolio_manifest.json'));print(d['constraints']['weighting'], d['risk_control']['target_volatility'], d['risk_control'].get('scale'))"

# 3) 卖出计划有没有接入 ATR 止损
grep "收益型止损" output/results_cn/cn_exit_plan.md
uv run python -c "import pandas as pd;d=pd.read_csv('output/results_cn/cn_exit_plan.csv');print(d[['stock_code','pnl_pct','atr_pct_14','effective_stop','action','reasons']].to_string(index=False))"
```

`cn_exit_plan.md` 会写成 `收益型止损：固定百分比关闭（样本期该规则无效）；ATR 缩放开启 = 2.5 ×
ATR14%，夹取 4.0%~10.0%`；逐票的 `effective_stop` 是当天真正生效的止损位，触发时 `reasons`
出现 `触发止损（ATR×2.5 = x%）`。若报告说"固定百分比关闭"但 `effective_stop` 全为空，说明
`[exits.rules].stop_loss_atr_multiple` 被改回 0（关闭）。

检查点 2 的输出里 `startup_gate.mode` 应为 `eligibility`、`universe_filter.enabled` 应为 `true`；
若某个开关的 `enabled` 是 `false`，说明该段过滤当天没有参与决策，选股结果不能按"低位启动 + 非放量"
口径解释。

`paper_account` 在 `pk` 之后每日运行：它会按 T+1 规则推进已有虚拟订单、更新持仓与净值。
`paper_outcomes` 也建议每日运行：它不重新训练，只把历史信号与新到达的 1/5/20/60 日结果对齐；
也可以按周补跑，但每日执行更容易及时发现成熟信号表现异常。

### 定期重训

模型不会因为读取新数据而自动重训。每 20 个交易日或每月执行一次；特征集合、清洗版本、标签
定义或模型参数变化后，也应立即重训并重新打分选股：

```bash
uv run python scripts/run_cn_pipeline.py --stage lightgbm
uv run python scripts/run_cn_pipeline.py --stage transformer
uv run python scripts/run_cn_pipeline.py --stage model_scores
uv run python scripts/run_cn_pipeline.py --stage preselection --force-rebalance
uv run python scripts/run_cn_pipeline.py --stage pk
uv run python scripts/run_cn_pipeline.py --stage exits
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

`preselection`/`pk` 只生成候选和目标权重，不代表已经验证盈利。建议每次选股后执行：

```bash
uv run python scripts/run_cn_pipeline.py --stage paper_outcomes
uv run python scripts/run_cn_pipeline.py --stage paper_account
```

- `paper_outcomes` 将信号与未来 1/5/20/60 个交易日收盘价对齐，输出毛收益、扣费收益、最大不利 excursion（MAE）、最大有利 excursion（MFE）及相对市场代理收益。文件位于 `output/paper_trading/cn_signal_outcomes.{csv,json,md}`。信号未满观察期会标记为 `pending`，不能当作亏损或盈利统计。
- `paper_account` 按 T+1 下一交易日开盘、手续费和滑点回放虚拟账户，输出 `orders.csv`、`fills.csv`、`positions.csv`、`nav.csv` 和 `paper_account_summary.json`。它是持仓事件记录，不是实盘委托。
- 质量判断至少观察滚动 20/60 日的命中率、平均净收益、超额收益、Sharpe、最大回撤、换手、成交率和 pending 比例；单次 Top-N 不能证明策略有效。正式评估应启用 `oos_predictions` 和 `model_comparison`，使用时间滚动、purge/embargo 的样本外预测。

当前配置已按小额虚拟账户设置：初始资金 `45,000`、PK 最多 `6` 只、总仓位按 regime 预算执行。预选候选由模型与各信号类型共同构成；PK 后的权重使用近 20 日年化波动率的逆波动率分配，并受单票上限、行业上限、成交容量和换手约束。缺少行业数据时不会把所有股票错误地视为同一行业，但行业中性约束需要补齐行业映射后才完整生效。

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

### 标签口径与特征剖面（缩小搜索空间的两个旋钮）

样本量变大后不需要把全部 `alpha_zoo_hk` 因子一次性塞给模型：面板是"一个因子 = 值 + 缺失掩码"
两列，全量约 680 因子；当前 LightGBM 工件 1,267 列、Transformer 500 列，扩窗口时成本按折数相乘。
两个官方旋钮：

**标签口径**：`[lightgbm].label_mode` 与 `[transformer].label_mode`（默认 `forward_return`）。

| 写法 | 实际目标列 | 说明 |
|---|---|---|
| `forward_return` | `forward_return_{label_horizon}d` | 默认，收盘到收盘 |
| `excess` / `excess_return` / `excess_ret` | `forward_excess_return_{label_horizon}d` | 按 `label_horizon` 取 5/10/20/60 日横截面超额 |
| `excess_return_{N}d` | `forward_excess_return_{N}d` | 显式指定窗口，不复用 `label_horizon` |
| `path` / `path_score` / `path_score_20d` / `startup_path` | `label_path_score_20d` | 启动路径标签，`embargo_days` 默认抬到 60 |
| `path_score_60d` | `label_path_score_60d` | 60 日路径标签 |

`startup_only = true` 会把样本限制在 `startup_price_eligible`（或把不合格行标签置空，取决于
`preserve_startup_context`），这是"换样本域"，必须在同一样本域上比较标签口径。`extra_label_columns`
可以把同一次 `strategy_labels` 产出的其他列一并带出训练面板用于研究。

**待接线的标签**：`strategy_labels` 已经产出 `forward_exec_return_{N}d`（T+1 开盘成交口径）以及
波动率调整版标签列，但 `_clean_panel_training_data` 的 `label_mode` 目前只识别上表这些名字，
`exec_return` / `vol20` 一类写法会落进 `forward_return` 分支而静默换标签。接入前请用
`extra_label_columns` 显式带列做研究，不要在配置里写未支持的模式名。

**特征剖面**：`[model_features]` 支持

```toml
[model_features]
profile = "full"            # full | compact | core
min_feature_coverage = 0.05
drop_constant_features = true
include_features = []       # 额外白名单 glob（对应 feature_include_patterns）
exclude_features = []       # 额外黑名单 glob
include_families = []       # 家族白名单，取值：moneyflow | valuation | fundamental | academic | price_volume
exclude_families = []       # 家族黑名单（如 ["moneyflow"] 得到"无资金流"对照组）
```

- `compact`：只留价量/流动性/资金流家族，去掉财报与估值块；
- `core`：P1.17 论点显式短名单（半年低位位置、短周期动量与回调、缩量/放量、通道结构、资金流），
  约 60 个因子；
- `exclude_families = ["moneyflow"]` 展开为 `moneyflow*` + `flow_second_wave_*`，是"关闭资金流特征"
  的官方开关（未知家族名会直接报错，不会静默忽略），用来跑"含资金流 / 不含资金流"两组对照模型；
  两组各自训练后用同一 `preselection` → `pk` 流程比较选股结果。

剖面解析结果（命中、丢弃、未命中的 glob）会写进 manifest，便于审计；但**剖面本身不是信号主张**，
任何剖面/标签切换都要走 `oos_predictions` + `model_comparison` 或规则级 A/B 才能进入生产配置。

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

`preselection` 读取 `output/model_scores` 中的模型分数，不会调用旧 `core/lightgbm_analysis.py`，也不会
重新计算因子或重新训练模型。它只负责构造候选集，不分配最终仓位：

1. ensemble 模型按最新横截面分数取 `preselection_model_slots`（当前 8）；
2. 对每个命中的 `signal_type` 按 `signal_score` 排序，至少保留 2 只；
3. 模型候选与信号候选去重后写入 `output/results_cn/cn_ensemble_preselected.csv`；
4. 候选行保留 `selection_channel`（`model`/`signal_candidate`/`signal_override`）和信号证据，供下一阶段 PK。

`pk` 是独立的最终持仓构建阶段。它读取 `preselection_path`，不再扩大股票池或重新扫描信号，
从而避免最终持仓被全市场新信号悄悄改变。
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
预选池的规模可以大于最终持仓数；最终持仓由 PK 的 `max_holdings` 决定。当前默认最多 6 只，
即模型 4 只与信号候选共同竞争 6 个持仓名额。实现与验收记录见
[P0_13 Donchian 通道突破接入选股计划](../todo/P0_13_donchian_breakout_selection_plan.md)。

### PK 持仓的量化逻辑

PK 不是主观“看图挑股”，而是标准的 **candidate generation → portfolio construction** 两阶段架构：

1. **候选生成（preselection）**：保证模型 alpha 与不同信号 sleeve 都有代表性，避免单一模型垄断候选池；
2. **横截面排序**：保留模型分数、信号分数和信号类型，作为后续 alpha 输入；
3. **风险模型**：使用近 20 日波动率构造特异风险（当前为对角协方差近似）；
4. **交易成本与容量**：估算 ADV、冲击成本、参与率和换手，剔除不可执行权重；
5. **约束优化**：在 `gross_exposure`、单票上限、行业上限、最大持仓数、换手上限和整手可买约束下，
   先让强制 signal override 占位，再由模型/信号候选竞争剩余名额；
6. **风险定权**：默认 inverse-volatility，波动率越高权重越低，并经过成本、行业和容量修复；
7. **可执行修复**：目标权重买不到 100 股整手时移除弱候选并重新优化。

这对应量化组合管理中的标准做法：Barra/因子风险模型的简化版、带交易成本的 long-only constrained
portfolio construction，以及事件/信号 sleeve 的独立预算。它不是保证收益的打分器；应使用滚动 OOS、
purge/embargo、换手和成本后的 Sharpe、最大回撤、IC/IR 与容量指标验收。

两阶段运行：

```bash
uv run python scripts/run_cn_pipeline.py --stage preselection --force-rebalance
uv run python scripts/run_cn_pipeline.py --stage pk --force-rebalance
```

`[selection]` 关键配置：

```toml
[pipeline]
preselection_model_slots = 8   # 原为 4：模型 Top-4 会被 sleeve 下限整体挤出候选池

[selection]
preselection_path = "output/results_cn/cn_ensemble_preselected.csv"
rebalance_stride_days = 5     # 不足 5 个交易日则沿用上一版预选池；--force-rebalance 可跳过

[selection.signals]
recommendations_per_type = 2

[selection.portfolio_constraints]
max_holdings = 6
weighting = "rank_power"      # 原 inverse_volatility 完全忽略排名，排名 8 的票权重高于排名 2
alpha_power = 2.0
model_min_share = 0.80        # sleeve 下限合计 ≤ gross×20%，模型块承担主仓

[selection.risk_control]
vol_target_mode = "cap"       # cap = 只在天花板之上降杠杆（原 budget 会把风险预算部署出去）
target_volatility = 0.18
max_gross_exposure = 0.65
```

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
uv run python scripts/run_cn_pipeline.py --stage preselection --force-rebalance
uv run python scripts/run_cn_pipeline.py --stage pk
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

止损有两套开关（都读 `[exits.rules]`），默认只有"随波动缩放"的那套在打：

```toml
[exits.rules]
stop_loss_pct = 0.0             # 固定百分比止损：样本期无效，保持关闭
stop_loss_atr_multiple = 2.5    # ATR 缩放止损：k × ATR14%，夹取到 [stop_loss_min_pct, stop_loss_max_pct]
stop_loss_min_pct = 0.04
stop_loss_max_pct = 0.10
```

- `stop_loss_pct` 默认 0（关闭）：2024-2026 样本里 20 日跌幅 > 15% 的标的未来 20 日超额
  +1.6~2.2%，固定百分比止损会卖在期望最优的状态上。
- `stop_loss_atr_multiple` 生产配置为 2.5（代码默认 0，即关闭）：止损位 = `k × ATR14%` 之后再夹取到
  `[stop_loss_min_pct, stop_loss_max_pct]`。依据见
  [P1_17 §0.19](../todo/P1_17_path_labels_meta_labeling_plan.md) 的 15 个决策日 A/B：固定 6% 止损
  触发率 43%、5 日收益 +0.68%；ATR×2.5 触发率 19%、5 日收益 +1.00%，同时把 5 日 std 从 6.67%
  压到 6.09%、最差单日从 −10.99% 抬到 −9.33%。要压尾部就压"随波动缩放"的止损，不要用固定百分比。
- 生效止损位与依据逐票写入 `cn_exit_plan.csv` 的 `atr_pct_14` / `effective_stop`；触发时
  `cn_exit_plan.md` 的说明列会写成 `触发止损（ATR×2.5 = x%）`。

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

---

# 运维补充（2026-09-16 更新）

## 运行环境：统一使用 `uv run`

所有阶段一律通过项目环境运行：

```bash
uv run python scripts/run_cn_pipeline.py --stage <stage>
```

`uv run` 会先同步 `uv.lock` 再执行，保证解释器与依赖版本一致（当前 `.venv` 即 uv 管理的环境：
Python 3.12.3 + pandas 3.0.3 + numpy 2.4.5）。直接调用 `.venv/bin/python` 在依赖漂移后会与
文档/锁文件不一致，排障时容易误判为代码缺陷。

## 历史回放：`--trade-date`

用指定交易日重放整条决策链（模型截面、regime 行、可成交价格窗口、风险窗口全部按该日期截断，不读未来数据）：

```bash
uv run python scripts/run_cn_pipeline.py --stage preselection --trade-date 2026-09-15 --profile rich
uv run python scripts/run_cn_pipeline.py --stage pk           --trade-date 2026-09-15 --profile rich
uv run python scripts/run_cn_pipeline.py --stage paper_outcomes --trade-date 2026-09-15 --profile rich
```

- 输出隔离到 `output/results_cn/replay_<YYYYMMDD>[_<profile>]/`，`paper_outcomes` 同理写到
  `output/paper_trading/replay_<YYYYMMDD>_<profile>/`，**不会覆盖生产文件**。
- 指定 `--trade-date` 时会绕过 `rebalance_stride_days` 的"沿用上一版"逻辑。
- 用固定日期做打分（例如让新模型对新旧模型同一天对比）：

```bash
cp config/cn_pipeline.toml config/cn_pipeline_score0915.toml   # 改 end_date 与 [model_scores] output_dir
uv run python scripts/run_cn_pipeline.py --config config/cn_pipeline_score0915.toml --stage model_scores
```

## 账户形态：`--profile`

`[selection.profiles.<name>]` 定义资金、持仓数、模型槽位、价格上限与卫星槽位，`--profile` 选中其一：

```toml
[selection.profiles.rich]
initial_capital = 300000.0
max_holdings = 6
model_slots = 8
max_price = "auto"        # auto = equity * gross_exposure / max_holdings / lot_size
sleeve_slots_max = 2
```

`max_price = "auto"` 的含义是"一个持仓槽位的预算刚好买得起一手"：
300k 档 ≈ 175 元、45k 档 ≈ 26.25 元。价格上限同时作用于模型短名单与信号名字。

## 选股域：启动资格门与 universe 过滤（P1.17）

选股的第一步不是排序，而是决定"哪些票有资格进候选池"。两段过滤都在 Top-N 截断**之前**执行
（在 Top-N 之后再过滤会把池子清空——门槛排除的名字恰好就是未经门槛时排名最高的那批）。

```toml
# 启动资格门（P1.17 §0.8/§0.9）：把"已经涨上去"的名字挡在池外
[selection.startup_gate]
enabled = true
mode = "eligibility"           # eligibility | thresholds | both
max_dist_from_120d_low = 0.30  # 距 120 日低点涨幅上限
max_return_60d = 0.35
max_dist_from_60d_high = -0.05
# 第二梯队：确实开始启动，但缩量回调 + 资金流入确认 → 小仓位准入
second_tier_enabled = true
second_tier_min_dist_from_120d_low = 0.30
second_tier_max_dist_from_120d_low = 0.55
second_tier_volume_ratio_max = 1.0
second_tier_flow_z_min = 0.0
second_tier_max_weight = 0.05

# 选股层 universe 过滤（P1.17 §0.12/§0.18）
[selection.universe_filter]
enabled = true
exclude_st = true
min_median_amount_20d = 10000000.0
exclude_volume_breakout = true      # 剔除"放量上涨"，除非资金流确认
volume_breakout_ratio = 1.5
volume_breakout_flow_z_min = 1.0
volume_breakout_missing_flow = "drop"
```

- `mode = "eligibility"` 直接复用 `strategy_labels` 的 `startup_price_eligible`：`dist_from_120d_low ∈
  [0.05, 0.30]` 且 `return_60d ≤ 0.35` 且 `dist_from_60d_high ≤ −0.05`，只用决策日当天可得数据
  （收盘后判定，T+1 开盘执行）；`thresholds` 只用配置里的上限，`both` 两者都要满足。
- 第二梯队命中的标的打上 `selection_tier = second`，在 PK 阶段按 `second_tier_max_weight`
  （默认 5%）封顶，避免"半启动"的票占满仓位。
- `exclude_volume_breakout` 只剔除"放量上涨且资金流没有确认"的名字：`pv_volume_ratio_20d ≥ 1.5`
  且 `moneyflow_net_z_5d < 1.0`；资金流缺失按 `missing_flow = "drop"` 处理（剔除），不按"未知即放行"。
- 任一过滤器把候选清空会直接抛错（而不是静默返回空组合）：空池意味着阈值与当前 regime 不匹配，
  应当人工复核，而不是当作正常结果。

审计：两段过滤的结果现在会随选股结果一起返回，并出现在 `output/pipeline_reports/*.json` 的
`preselection` 阶段摘要里（`startup_gate` / `universe_filter` 两个键，含
`universe_before`/`universe_after`、`second_tier_codes`、`st_dropped`、`illiquid_dropped`、
`volume_breakout_dropped` 等）。逐票理由仍看 `cn_ensemble_explanations.md`。

## 组合风控：`[selection.risk_control]`

```toml
[selection.risk_control]
target_volatility = 0.18      # 组合年化波动上限，超过则整体降杠杆、留现金（不加杠杆）
vol_target_mode = "cap"       # cap = 只在天花板之上降杠杆；budget = 把没用掉的风险预算部署出去
max_gross_exposure = 0.65
max_name_risk_share = 0.35    # 单名占组合方差上限（迭代风险预算：压超限名字并再分配）
# max_downside_vol = 1.20     # 资格门槛：默认关闭
# min_reward_risk = 0.10
# min_drawdown_60 = -0.45
```

**为什么资格门槛默认关闭**：全市场横截面的规律（深回撤更差、低盈亏比更差）套到"模型 Top-N 内部"会双重过滤。
实测在 2026-09-15 回放中，`min_drawdown_60=-0.45` + `min_reward_risk=0.10` 剔掉了模型头部
300776/301396/688515/603929（强势股回撤 -0.5~-0.6），rich 组合命中率从 6/6 掉到 2/5。
开启任一门槛前请单独立项回测，并把结论写入 `output/verification/<topic>/VERIFICATION.txt`。

风控在 PK 阶段执行；波动率/盈亏比/量能等**信息**作为特征放在预选（模型排序）与输出列里。

## 信号层新增配置项

| 配置 | 作用 | 依据 |
|---|---|---|
| `require_market_trend_up` | sleeve 仅在 `median_return_20d > 0` 时开火 | 上行 +0.30%/+0.80% vs 下行 +0.11%/+0.04% |
| `max_breakout_extension` | 收盘价超出通道上沿的比例上限（默认 0.05） | 超出 >5% 后 5 日超额 -0.50%（t=-2.15） |
| `max_runup_5d` | 近 5 日累计涨幅上限（默认 0.20） | 5 日涨幅 >20% 后 -0.32% |
| `entry_delay_days` | 用"上一交易日"的事件（决策日买入 = 事件日+2 开盘） | 涨停动量 T+1 +0.01% → T+2 +0.35% |
| `sleeve_slots_max` | 强制信号最多占用几个槽位 | 小账户每 sleeve 占一槽会把组合塌成 1.4 只 |
| `model_min_share` | sleeve 下限合计 ≤ gross×(1-model_min_share)（默认 0.80） | 模型 Top-8 的 20 日超额 +9.2%（t=3.7），sleeve 只有 0.2~1.0% |

## 新增因子集（例如量能路径 / 波动率 / 盈亏比）

1. 在 `factor_engine/expressions/` 下新建因子集并用 `@register_factor_set("...")` 注册，
   在 `factor_engine/expressions/__init__.py` 导出。
2. 把因子集名加入 `ALPHA_ZOO_HK_COMPONENTS`（生产模型训练用的 `alpha_zoo_hk` 会随之扩展）。
3. 重跑链路：`features` → `clean_panel` → `lightgbm`/`transformer` → `model_scores`。
4. 验证：`uv run python -m pytest test/test_volume_volatility_features.py -q`，再用
   `output/verification/volume_volatility_20260916/feature_increment_study.py` 做增量检验，
   最后用 `output/verification/retrain_20260916/compare_models.py` 比较新旧模型的同日打分。

已实现的扩展：`volume_volatility_hk`（33 个特征：量能路径 12 + 多口径波动率 17 + 盈亏比 4）。

## 故障排查（2026-09-16 实测踩坑记录）

| 现象 | 根因 | 处理 |
|---|---|---|
| `DateParseError: day is out of range for month: 0` | 新因子集用 `frame.index` 当输出索引，而 worker 给的是 `RangeIndex + trade_date`，`pd.concat` 把日期与 `0,1,2…` 并集 | 因子集内统一用 `trade_date` 构造 DatetimeIndex |
| `MergeError: incompatible merge keys [0] dtype('<M8[us]') and dtype('<M8[s]')` | 旧特征库分区是秒级、新帧是微秒级 | `parquet_store.upsert_frame` 前统一到 `datetime64[us]`；如需彻底重建，先把 `assets/data/feature/features` 改名备份再以空目录重跑 `features` |
| 特征列"算出来了但没落库"/全为 NaN | 在 RangeIndex 上计算、再贴 DatetimeIndex 标签 → 按标签对齐 → 全 NaN，随后 `dropna(feature_value)` 把整列丢掉 | 计算前先做**位置式**索引赋值（`series.index = date_index`），并加"非空率"回归测试 |

排查脚本：`output/verification/retrain_20260916/`（`feat_probe.py`、`worker_cols.py`、`worker_vals.py`、`merge_trace.py`）。

## 数据卫生与已知问题（2026-09-21 实测，详见 [P1_19](../todo/P1_19_cn_data_hygiene_plan.md)）

这一节的每一条都带有实测数字，处理方式未落地前不要假设数据是干净的。

| # | 问题 | 实测证据 | 现行处理 |
|---|---|---|---|
| D1 | **128 个上证指数代码（`000001.SH` 上证指数 … `000148.SH`）被当成个股落库** | OHLCV 库 395,008 行、2014-01-02→2026-09-09；clean panel 47,232 行；训练窗口 2025-09-19~2026-04-27 里占 18,176 行 = 2.43%（每天 128 行），价量特征填充率 1.000、资金流/估值全 NaN；`stock_info_registry` 仍有这 128 个代码 | 抓取侧自 2026-09-10 起不再收，但**历史与面板仍有**；修复见 P1_19 §1（前缀白名单 + 清理 + 重跑 `clean_panel`） |
| D2 | **universe 断点**：2026-09-10 单日行数 −129（5,335→5,206） | 全库单日行数最大跳变就是这一天（D1 的副作用） | 跨 09-10 的回测/折要显式说明前后 universe 不同 |
| D3 | **停牌股表现为"缺行"**，不是抓漏 | 09-21 有 12 只无 bar，实时行情全为 `vol=0`（中金/东兴/信达/广汽/园林/华之杰/*ST清越/奥克/奥联/*ST元道/*ST萃华/*ST康佳A）；09-21 vs 09-18 只差 4 只（出 300082/300585 停牌，入 600301/600825 复牌），独立源 K 线逐日一致 | 停牌期不写行、复牌只写当天、**不回填**；`exits` 用 `max_suspend_sessions=5` 兜长期停牌 |
| D4 | 预检告警 `cn_ohlcv_rows_below_threshold` | = universe 代码集里有窗口内行数 <120 的代码（D1 的指数 + D3 的停牌 + 次新），不阻塞（`backtest_ready=true`） | 建议拆成具名原因（P1_19 §3） |
| D5 | **自动化停摆**：`launchctl print gui/501/com.quant.cn-pipeline` 返回 `Could not find service`；09-18 的 `features` 跑到 96%（52 分钟）被杀、没写 `production.done`，09-19/20 周末、09-21 无触发 | `launchd.out.log` 最后一行停在 09-18 20:53；机器自 09-09 连续运行 12 天（排除重启） | `bash deploy/daily-cn-pipeline/macos/install.sh` 重装加载，`RunAtLoad` 会自动补跑当天；加固项见 P1_19 §4.1/§4.2 |

**每日收盘后最少核对三条**（都在 `output/pipeline_reports/daily/<date>/`）：

```bash
grep -c "stage=.*FAILED"  output/pipeline_reports/daily/$(date +%Y-%m-%d)/run.log   # 期望 0
ls output/pipeline_reports/daily/$(date +%Y-%m-%d)/production.done                    # 期望存在
launchctl list | grep com.quant.cn-pipeline                                           # 期望进程仍在
```

## 验证与回滚约定

涉及策略/模型改动的任务，产物统一放在 `output/verification/<topic>_<date>/`：

```
BASELINE/      改动前的文件快照 + SHA256SUMS
MODIFIED/      改动后的快照 + SHA256SUMS
*.diff         BASELINE → MODIFIED 的统一 diff
ROLLBACK.sh    可执行；restore 并逐文件校验哈希
VERIFICATION.txt  命令 / 输入 / 输出 / 退出码 / 回滚后行为
```

评估工具（均为 `uv run python ...` 调用）：

| 脚本 | 用途 |
|---|---|
| `output/verification/event_study_20260916/event_study.py` | 各 sleeve 的事件研究（1/5/20 日、分 regime、涨幅/跳空分桶） |
| `.../exit_policy_study.py` | 退出规则与尾部集中度对比 |
| `.../shock_study.py` | 次日弱势后的进场时点（"等一天还是立刻进"） |
| `output/verification/preselect_optimization_20260916/portfolio_backtest.py` | 多日组合回测（`--stride 4` 得到非重叠样本） |
| `.../validate_20260915.py` | 指定决策日 → 次日验证（命中率/超额/置信区间） |
| `output/verification/retrain_20260916/compare_models.py` | 新旧模型同日打分对比（Top-N 命中率、rank IC） |

---

# 每日自动化与部署（2026-09-17）

## 选股入口：用 `preselection` + `pk` 取代 `selection`

`[stages] selection = false`，配置里已默认关闭一步式 `selection`；每日生产固定三步：

```bash
uv run python scripts/run_cn_pipeline.py --stage preselection   # 阶段一：模型 + 各 sleeve 候选池
uv run python scripts/run_cn_pipeline.py --stage pk             # 阶段二：组合与风控
uv run python scripts/run_cn_pipeline.py --stage exits          # 阶段三：持仓卖出/风控规则
uv run python scripts/render_two_stage_report.py                # 两阶段报告（Markdown + JSON）
```

- `preselection` 产出 `output/results_cn/cn_ensemble_preselected.csv`（模型 Top-8 + 各 sleeve 候选/强制项）；
  它遵守 `rebalance_stride_days`，当天必须换池时加 `--force-rebalance`。
- `pk` 在冻结的候选池上做风险预算与定仓，产出 `cn_ensemble_selected.csv` + `cn_ensemble_portfolio_manifest.json`；
- `exits` 独立读 `config/holdings_cn.csv` 逐日给出退出计划（含 ATR 缩放止损），不受选股 stride 影响；
- 两阶段报告默认写到 `output/results_cn/two_stage_report_<date>.md/json`
  （回放时用 `--replay-dir output/results_cn/replay_<date>_<profile>`）。
- 历史对比/回放一律用 `--trade-date`（输出隔离到 `replay_<date>[_<profile>]/`），不要覆盖生产文件。

## 一键每日生产脚本

```bash
bash scripts/run_daily_production.sh              # 收盘后自动判断 + 幂等 + 生成报告
bash scripts/run_daily_production.sh --dry-run    # 只打印将执行的阶段
bash scripts/run_daily_production.sh --force      # 当天重跑
bash scripts/run_daily_production.sh --skip-retrain   # 跳过 features/clean_panel（重量级）
```

行为：① 本地时间早于 `DAILY_EARLIEST_HOUR`（默认 16）直接退出；② 当天成功过就跳过
（标记 `output/pipeline_reports/daily/<date>/production.done`）；③ 阶段顺序
`daily_bars → moneyflow → regime → features → clean_panel → model_scores → preselection → pk →
exits → paper_account → paper_outcomes`；④ 每阶段独立日志 + 失败即停 + 末尾生成两阶段报告；
⑤ 运行前先加载 `config/scheduler_env.sh` 与 `config/notify.env`，并跑一遍
`DAILY_PREFLIGHT_CMD`（默认 3 个回归测试文件）把代码级错误挡在重活之前。

## 调度方式（三选一）

| 环境 | 方式 | 触发 | 说明 |
|---|---|---|---|
| **macOS 本地** | launchd（`deploy/daily-cn-pipeline/macos/`） | 每天 19:30 / 20:30 / 22:00 + 开机 `RunAtLoad` | `bash install.sh` 一键安装；能用到 Apple GPU（MPS） |
| **Linux 云主机** | systemd timer（`deploy/daily-cn-pipeline/linux/`）或 cron | `Mon..Fri 19:30/20:30/22:00`（北京时间）+ `Persistent=true`（开机补跑） | 阿里云/腾讯云 ECS；`TZ=Asia/Shanghai` 必设 |
| **容器** | `deploy/daily-cn-pipeline/docker/` | 宿主 cron / 云定时任务调用 `docker compose run` | 环境可复现；**CPU 推理** |

容器化与硬件加速（macOS Docker vs Linux Docker）：

| 维度 | macOS 上的 Docker | Linux 上的 Docker |
|---|---|---|
| 运行时 | Linux VM（Virtualization.framework） | 共享宿主内核 |
| Apple GPU（Metal/MPS） | **不可用** | 不适用 |
| Apple NPU（ANE） | **不可用**（仅原生 macOS 进程） | 不适用 |
| NVIDIA GPU | 不可用 | 需 NVIDIA Container Toolkit + `--gpus all` |
| 大文件 I/O | 受 VM 限制，明显偏慢 | 接近原生 |

因此：**本机每日生产用原生 venv + launchd（全速、可用 MPS）**；容器用于云端或需要同构复现的场景，
并在配置里显式 `[transformer] device = "cpu"`，避免"以为在用 MPS 其实在跑 CPU"。
三种调度器都只调用同一个 `scripts/run_daily_production.sh`，因此行为完全一致。

## 数据与状态

- 必须持久化：`assets/`（OHLCV、特征库、模型）、`output/`（选股、报告、paper 账户）、`config/`；
  容器部署时挂卷或云盘，不要放进镜像。
- 节假日：脚本按"本地时间 + 幂等标记"判断，节假日会空跑（数据源无新交易日）；要更严格可接
  `output/regime/cn_market_regime.csv` 的最新交易日校验。
- 建议监控：`output/pipeline_reports/daily/<date>/run.log` 与各阶段日志；
  ClickHouse 被降级（`_clickhouse_disabled_reason`）会静默改变 stock_info 读取来源，值得单独告警。

## 飞书通知（已部署）

每日生产在生成两阶段报告后，会调用 `scripts/notify_feishu.py` 把选股结果发到飞书：

```bash
uv run python scripts/build_feishu_report.py          # 生成 output/results_cn/feishu_<date>.md|.json
uv run python scripts/notify_feishu.py --dry-run      # 预览将发送的内容
uv run python scripts/notify_feishu.py                # 真实发送
```

通知结构（每只一行，字段与运营表一致）：

| 字段 | 来源 |
|---|---|
| 股票 / 名称 / 行业 | `stock_info`（名称、industry_l2） |
| 路径与选择原因 | `selection_channel`/`selection_sleeve`/`signal_type`/信号分/量比/反弹 ATR/止损价 |
| 模型分 / 排名 | `cn_ensemble_preselected.csv` 的 `model_score` / `rank` |
| RPS(5/10/20/30/60) | 横截面分位：各周期收益在全市场的百分位排名 |
| PK 状态与风险 | `cn_ensemble_selected.csv` 权重与手数、整手剔除/保底记录、风控减仓系数、成交额中位数与流动性评级 |

身份与收件人通过环境变量覆盖：

- `FEISHU_IDENTITY`（默认 `bot`；`user` 需要 `im:message.send_as_user` scope，当前账号缺该 scope，会报
  `missing_scope`，此时用 bot 身份即可）；
- `FEISHU_USER_ID`（默认 `ou_53cf03fd06b21154a774194516dc2e95`，即当前账号）；
- `DAILY_NOTIFY_FEISHU=0` 可关闭通知（通知失败不会影响生产结果，只写日志）。

## 本机自动化已部署（2026-09-17）

```bash
bash deploy/daily-cn-pipeline/macos/install.sh      # 安装/更新 launchd
launchctl print gui/$(id -u)/com.quant.cn-pipeline  # 查看状态
launchctl kickstart -k gui/$(id -u)/com.quant.cn-pipeline   # 立即手动跑一次
```

- 计划：每天 **19:30 / 20:30 / 22:00** + `RunAtLoad`（开机补跑）；`DAILY_EARLIEST_HOUR=16` 保证
  16:00 之前触发只会直接退出（收盘数据未就绪）。幂等标记
  `output/pipeline_reports/daily/<date>/production.done`（当天成功过就跳过，`--force` 重跑）。
- 日志：`output/pipeline_reports/daily/<date>/run.log` 与各阶段 `*.log`；
  launchd 的 stdout/stderr 在 `output/pipeline_reports/daily/launchd.{out,err}.log`。
- 若当天要手工补跑：`bash scripts/run_daily_production.sh --force`。
- **健康自检（2026-09-21 事故后新增）**：agent 可能处于未加载状态（`launchctl print
  gui/$(id -u)/com.quant.cn-pipeline` 报 `Could not find service`），此时三种触发全部失效且
  **不会有任何告警**（告警脚本本身也是被它调起来的）。每周（或每次看结果前）跑一次：

  ```bash
  launchctl list | grep com.quant.cn-pipeline                      # 没输出 = agent 未加载
  launchctl print gui/$(id -u)/com.quant.cn-pipeline | grep state   # 期望 state = running
  bash deploy/daily-cn-pipeline/macos/install.sh                    # 重装并立即补跑（RunAtLoad）
  ```

  另外，`features` 这类长阶段被杀不会触发失败告警（阶段没返回非 0），只表现为"当天没有
  `production.done`"。所以当天没收到选股通知时，先看 `run.log` 最后一行停在哪。

## 微信群 / 企业微信通知（2026-09-17）

**结论：个人微信没有官方群机器人接口，无法合规地自动发到"微信群"。可自动化的等价物是企业微信
内部群的群机器人 webhook**，本仓库已支持，配好 URL 即生效。

| 目标 | 能否自动发 | 方案 |
|---|---|---|
| 企业微信**内部群**（同事群） | ✅ | 群机器人 webhook（`WECOM_WEBHOOK_URL`），本仓库已支持并已用本地 mock 验证 |
| 企业微信**外部客户群**（群里有微信用户） | ❌ 机器人不支持 | 只能走"群发/客户群群发"（需人工确认），或把消息发到内部群再转发 |
| 企业微信**成员单聊** | ✅ | 企业自建应用 `corp_id/corp_secret/agentid` + 应用消息（未接入，需要企业应用凭据） |
| **个人微信群** | ❌ 无官方 API | 非官方方案（Wechaty/itchat 等 hook 个人微信）违反服务协议、封号风险高、需长期在线扫码，**不建议接入每日生产** |
| 飞书 / 钉钉 / 自建服务 | ✅ | 飞书 bot（已部署）、钉钉 webhook、通用 webhook，同一脚本内多选 |

### 三步启用企业微信群机器人

1. 企业微信里打开目标**内部群** → 右上角设置 → 群机器人 → 添加机器人 → 复制 **webhook 地址**；
2. 本机创建 `config/notify.env`（模板见 `config/notify.env.example`），写入：
   ```bash
   WECOM_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx
   NOTIFY_CHANNELS=feishu,wecom
   ```
   （该文件已加入 `.gitignore`；launchd 不需要任何改动，脚本会自动 source 它。）
3. 验证：
   ```bash
   uv run python scripts/notify_selection.py --dry-run        # 预览将发送的内容与渠道
   uv run python scripts/notify_selection.py                  # 真实发送
   ```

### 多渠道通知脚本

```bash
uv run python scripts/notify_selection.py --channels feishu,wecom,dingtalk,generic
uv run python scripts/notify_selection.py --print-payload     # 只打印内容不发送
```

- 每条 webhook 消息用 `msgtype=markdown`，正文与飞书通知一致（股票/名称行业/路径原因/模型分排名/
  RPS/PK 状态与风险，最多 12 只，超出会被截断到 4000 字）；
- 未配置的渠道记为 `skip`（不影响退出码），真实发送失败才返回非 0；
- 每日生产脚本在生成两阶段报告后自动调用；`DAILY_NOTIFY=0` 可整体关闭。

## 邮件通知（QQ 邮箱，2026-09-17 已支持）

邮件渠道同样是 `scripts/notify_selection.py` 的一个通道，正文与飞书通知一致，并额外提供
**HTML 表格**（六列：股票 / 名称行业 / 路径与选择原因 / 模型分排名 / RPS / PK 状态与风险），
邮件客户端里直接呈现为表格。

```bash
# 配置（写到 config/notify.env，已 gitignore；脚本会自动 source）
MAIL_TO=badwtg2222@qq.com
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_TLS=ssl
SMTP_USER=badwtg2222@qq.com
SMTP_PASSWORD=<QQ 邮箱"授权码"，16 位>

# 预览与发送
uv run python scripts/notify_selection.py --channels email --dry-run
uv run python scripts/notify_selection.py --channels email
```

**QQ 邮箱授权码获取**：QQ 邮箱网页版 → 设置 → 账户 → "POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务"
→ 开启 **SMTP 服务** → 按提示发送短信 → 生成 16 位授权码（**不是 QQ 登录密码**）。
`SMTP_PASSWORD` 未配置时邮件渠道会明确报错（不会静默失败），其余渠道不受影响。

验证：`test/test_selection_notify.py`（5 项）覆盖 MIME 多部分结构（纯文本 + HTML 表格）、
未配置授权码的报错、dry-run 计划输出、企业微信 payload 结构、未配置渠道按 `skip` 处理；
发送链路另用本地 mock SMTP（`SMTP_HOST=127.0.0.1 SMTP_TLS=none`）实测通过。

产物：`output/results_cn/feishu_<date>.{md,html,json}`（同一份数据同时供飞书/邮件/微信使用）。

### 邮件渠道已上线（2026-09-17 实测）

`config/notify.env` 配置完成并实测投递成功：

```text
ok   email: sent to badwtg2222@qq.com via smtp.qq.com:465 (ssl)
```

- `scripts/notify_selection.py` 现在会**自动加载** `config/notify.env`（不覆盖已有环境变量），
  因此手动运行、launchd、systemd、cron 四种方式的行为一致，凭据只维护一份；
- 每日生产脚本仍会额外 source 一次该文件（双保险）；
- 收件人与渠道在 `config/notify.env` 中调整：`MAIL_TO`、`NOTIFY_CHANNELS=feishu,email[,...]`、
  `DAILY_NOTIFY=0` 可整体关闭通知。

## 调度任务如何拿到 `~/.bashrc` 里的环境变量（2026-09-17）

**结论：拿不到，必须显式处理。** 原因是 shell 启动文件的分工：

| 启动方式 | 读取的文件 | 说明 |
|---|---|---|
| 登录 shell（Terminal 打开、`bash -l`） | `/etc/profile` → `~/.bash_profile` → `~/.bash_login` → `~/.profile` | macOS 的 bash 默认走这里；**不读** `~/.bashrc`（除非 `.bash_profile` 里显式 source） |
| 交互式非登录（在已登录终端里敲 `bash`） | `~/.bashrc` | 你平时手动执行 `uv run ...` 走的就是这条路径，所以一切正常 |
| **非交互式**（launchd / systemd / cron / 脚本调用） | **都不读** | bash 只会在设置了 `BASH_ENV` 时读那个文件；`~/.zshrc`、`~/.bashrc` 一律不参与 |
| zsh（macOS 默认交互 shell） | 登录：`~/.zprofile`；交互：`~/.zshrc` | 同上，调度任务都不会读 |

本项目 `~/.bashrc` 里恰好放着这个工程**必需**的东西：`CLICKHOUSE_*`（含密码）、
`TUSHARE_RELAY_KEY`、`CN_INDUSTRY_RELAY_BASE_KEY`、`HF_TOKEN`、`DEEPSEEK_API_KEY`、
`HTTP(S)_PROXY`，以及把 `~/.local/bin`（`uv` 所在目录）加进 `PATH` 的那一行。
更麻烦的是它开头有：

```bash
case $- in
	*i*) ;;
	*) return ;;
esac
```

即**非交互式 shell 里手动 source 它也会立刻 return**，什么都拿不到（这正是 18:30 那次
`uv: command not found` 的根因）。

### 处理方式（已落地）

1. **固化环境文件**：`bash scripts/capture_scheduler_env.sh` 会启动一个真正的交互式 shell
   （`bash -ic env`）采集完整环境，过滤出与本工程相关的变量（ClickHouse、数据源 key、
   LLM key、代理、`PATH`），写成 `config/scheduler_env.sh`（`export` 形式，权限 600，已 gitignore）。
2. **调度脚本显式加载**：`run_daily_production.sh` 在开头 `set -a; . config/scheduler_env.sh; set +a`，
   并在每次运行的第一行打印脱敏环境摘要，便于日后排查：
   ```
   env: uv=/Users/ccs/.local/bin/uv | CLICKHOUSE_HOST=localhost CLICKHOUSE_PASSWORD=set | TUSHARE_KEY=set | proxy=http://127.0.0.1:7897
   ```
3. **`BASH_ENV` 双保险**：launchd plist 里设置 `BASH_ENV=<repo>/config/scheduler_env.sh`，
   任何被任务启动的非交互式 bash 也会自动加载同一份变量。
4. **`PATH` 兜底**：脚本自身把 `$HOME/.local/bin`、`/opt/homebrew/bin`、`/usr/local/bin` 前置；
   `install.sh` 还会把 `uv` 所在目录写进 plist 的 `EnvironmentVariables.PATH`。

### 维护

- 改过 `~/.bashrc`（新增 key、改代理、换 ClickHouse 密码）后，重新采集一次即可：
  `bash scripts/capture_scheduler_env.sh`；
- 也可以直接编辑 `config/scheduler_env.sh` 追加 `export KEY=VALUE`；
- 验证调度环境是否完整（模拟 launchd 的干净环境）：
  `env -i HOME="$HOME" PATH=/usr/bin:/bin /bin/bash scripts/run_daily_production.sh --dry-run --force`
