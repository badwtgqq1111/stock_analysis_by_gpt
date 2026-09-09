# 数据清洗与统一特征面板实施规划

> 状态：P0.1/P0.2 已实现，P0.3 持久化模型训练与选股已接通；P1.2 Transformer/CNN 数据集基础链路已实现。
>
> 当前完成：清洗面板、PIT/缺失审计、LightGBM/Transformer/CNN 训练器、CUDA/MPS/CPU 设备选择、模型保存/加载、purged/embargo 切分、持久化模型打分、Top-N 选股和统一 walk-forward 指标评估器均已落地；相关回归测试 68 项通过。
>
> 下一步（已调整）：分时微结构后置；组合风险/成本约束、T+1 纸面账户及 trade 层持久化、另类证据 PIT 导入、策略研究标签、LightGBM/Transformer/CNN OOS 生成和 PIT 行业图 OOS 已接入；后续仅在提供基金共持 PIT 数据和指数历史文件后扩展多关系图与真实基准评估。

### 最新实现进度（2026-09-06）

- `--stage regime` 已接入 `MarketDataService`，从已落库日 K 计算 PIT 横截面市场代理、牛/熊/震荡/insufficient 状态、hysteresis 和模型权重，输出 `output/regime/cn_market_regime.{csv,json,md}`。
- `selection` 默认仍为 `ensemble`；若存在最新 regime 报告，会按状态权重合并 LightGBM/Transformer/CNN 分数，并把 `regime`、版本、日期和权重写入选股结果。
- 新增 `paper_outcomes` 阶段和 `factor_engine/ml/paper_trading.py`：对持久化选股结果按 1/5/20/60 个交易日计算成熟/待观察状态、毛/净收益、市场代理超额收益、MAE/MFE 和报告；缺少未来行情时保持 `pending`。
- 新增可选 `graph_temporal` 阶段和 `factor_engine/ml/graph_temporal.py`：先落地可审计的 PIT 行业图基线（归一化邻接矩阵、门控邻居消息、时序面板训练、图版本 manifest）。它是 GrifFinNet 路线的实验基线，不宣称复现研报；机构共持图和真正逐层时空交织仍需后续 OOS 消融后接入。
- 新增可选 `oos_predictions` 阶段：对每个 expanding fold 只使用该折训练期重新训练，再生成测试日预测与已实现 forward return；`model_comparison` 已改为仅消费这些历史 OOS 文件，禁止将线上最新 `model_scores` 当作回测预测。默认先跑 LightGBM；Transformer 需要在同一 TOML 中显式加入，因其会逐 OOS 决策日进行序列推理。
- 已新增 `mean_variance_cost_aware` 组合模式：可审计的波动风险快照、ADV/成本快照、行业/单票/总仓位/换手/参与率约束，以及 `target_weight` 输出。默认仍为 `topn`；切换须在 `[selection]` 中配置 `portfolio_mode`。
- 已新增 `paper_account`：基于日线以 T+1 开盘（无开盘则收盘）模拟成交，保存订单、成交、持仓和逐日 NAV/回撤 CSV；与单次信号 outcome 分开。
- 已新增 A 股另类数据本地 PIT 导入：`alternative.input_path` 指向带 `stock_code` 和 `published_at`/`available_at` 的 CSV 时，导入器保留可得时间并生成覆盖报告；未配置路径不会抓取或伪造数据。
- 已新增日线策略研究标签：底部反弹、趋势跟踪、首板/二板百分比代理。首板/二板均标记 `execution_ready=false`，待涨跌停/开板/成交可得性数据接入后才可进入纸面账户。
- `graph_temporal` 已支持 OOS：仅在 `oos_predictions.industry_mapping_path` 提供带 `stock_code,industry_l1,available_at` 的 PIT 行业映射时可启用；每折均按测试起点截断映射。缺少该文件时明确阻断，不以当前行业注册表回填历史。
- `paper_account` 的 orders/fills/positions/nav 现同时导出 CSV 并写入 trade 层独立 parquet 数据集；`paper_outcomes.benchmark_path` 可接入带 `trade_date,close` 的真实指数文件，未配置时报告明确标注为横截面市场代理。
- 运行：`uv run python scripts/run_cn_pipeline.py --stage regime`；纸面结果：`uv run python scripts/run_cn_pipeline.py --stage paper_outcomes`。
>
> 目标：建立一个可审计、PIT 对齐、可复用的 `clean_feature_panel`，使 LightGBM、Transformer 和 CNN 使用同一份合格数据，而不是各自重新清洗原始行情、基本面或因子。

## 目标与非目标

### 目标

1. 对日 K、分时、基本面、估值、行业和特征层建立统一的数据质量规则。
2. 对每个 `stock_code + trade_date` 保存数值特征、缺失标记、来源、可得时间和质量状态。
3. 在训练前输出覆盖率、极值、异常、泄漏和样本量报告，并只用最小可训练阈值阻止不合格训练。
4. 让二维截面模型和三维时序模型从同一面板派生输入。

### 非目标

- 不把未完成 PIT 对齐的新闻、搜索和公告特征直接放入训练集。
- 不在本阶段改变 Alpha101/GTJA191 的因子公式。
- 不以填充后的数值替代原始数据；原始层继续保留用于回溯和重新清洗。

## 数据契约

### 面板主键

`clean_feature_panel` 的唯一键为：

```text
market, stock_code, trade_date, frequency, adjust, feature_set, feature_name
```

宽表训练视图按 `market, stock_code, trade_date` 聚合；长表仍作为可追溯的存储格式。

### 每个特征必须附带的元数据

| 字段 | 含义 |
|---|---|
| `value_raw` | 原始或因子生成值，不覆盖 |
| `value_clean` | 清洗后供模型使用的值 |
| `is_missing` | 原始值是否缺失 |
| `is_imputed` | 是否做过填补 |
| `is_outlier` | 是否触发极值规则 |
| `quality_status` | `valid` / `warning` / `invalid` |
| `source` | 数据源或因子族 |
| `available_at` | 数据真正可观察到的时间 |
| `cleaning_version` | 规则和阈值版本 |

`value_raw`、`value_clean` 与质量标记必须同时保存。零值不得作为缺失值的通用替代。

## 分层规则

### 1. 日 K

- 规范化股票代码、交易日、市场、复权方式和时区。
- 去除重复的 `stock_code + trade_date + frequency + adjust` 记录，并保留来源优先级和最后更新时间。
- 校验 `open/high/low/close` 为正；`high >= max(open, close, low)`；`low <= min(open, close, high)`；成交量和成交额非负。
- 标记停牌、零成交、极端单日跳变、价格倒挂和连续缺日，不静默补造价格。
- 对复权方式分区存储，不允许 qfq/raw/hfq 混合进入同一训练面板。

### 2. 分时

- 与日 K 分开清洗和质量统计；`intraday_bars` 不得覆盖日 K。
- 检查交易时段、重复 bar、乱序时间戳、负成交量、异常价差和无效成交额。
- 按交易日聚合前先标记有效 bar 比例；低于阈值的交易日不生成微结构特征。
- 分时缺失只能通过 padding + mask 进入时序模型，不能以无成交的真实 bar 混淆。

### 3. 基本面与估值

- 所有财务字段按 `available_at` 对齐到首次可交易日，而非报告期结束日。
- 历史估值只使用对应交易日可得字段；禁止用今天的 PE/PB/市值回填过去。
- 对报表修订保存版本或更新时间；重述数据不能改写已回测的历史可得面板。
- 对缺失财务字段保留 `is_missing`，允许有限横截面/行业中位数填补，但必须保留 `is_imputed`。
- 对行业分类缺失和多次变更保存生效时间，不允许使用未来行业映射。

### 4. 因子

- 因子计算前拒绝 `NaN`、`inf`、`-inf` 和不足 lookback 的输入窗口。
- 每个交易日、市场、行业分组计算缺失率、常数率和有效样本数。
- 截面因子使用训练期定义的 winsor 阈值、标准化和行业/市值中性化规则。
- 时间序列因子不能用全样本统计量归一化；滚动统计只使用当时及以前数据。
- 标记高度相关、低方差、覆盖不足和已知 proxy 因子，供模型特征选择读取。

### 5. 另类数据和搜索证据

- 先落原始 evidence/事件表，再生成特征；不允许直接把搜索结果文本并入训练面板。
- 事件至少包含 `stock_code`、`available_at`、`source`、`event_type`、原始内容/URL 与解析版本。
- 公开搜索、新闻和公告按可得时间做防未来函数截断；抓取时间不等于发布时间时需同时保存。
- 对 A 股建立独立名称/别名 registry，不能复用 HK-only `fetch-alt` 股票池。
- 证据覆盖率低时作为可选特征或缺失标记，不能默认为零情绪/零热度。

## 共享输出与模型输入

### 样本可用性原则

训练集按 `stock_code + trade_date` 过滤，而不是按“全市场所有字段是否完整”过滤：

- 核心 OHLCV、目标标签、必要 lookback 或生成因子缺失时，该股票日期不可训练。
- 财务、行业、另类数据和可选因子缺失时，该股票日期仍可训练，且必须带缺失/填补标记。
- 每次训练报告有效股票数、有效股票日期数、每个字段覆盖率和各字段实际进入模型的比例。
- 硬门禁只检查 `min_training_stocks`、有效时间窗口、PIT 合法性和非空特征矩阵。

这样可最大化使用已有数据，同时避免把“未观测到”伪装为零值或完整值。

### 量价与因子输入契约

所有模型都可以使用量价数据和 Alpha158、GTJA191、财务等因子，但不能直接拼接未经处理的原始 OHLCV。面板应先生成统一的输入特征：

| 类别 | 推荐输入 | 禁止做法 |
|---|---|---|
| 价格 | qfq 收盘价派生的对数收益、区间收益、振幅、均线偏离、滚动波动率 | 把不同价格水平的原始 close 直接与因子拼接 |
| 成交量/额 | `log1p(volume)`、`log1p(amount)`、换手率、量比、量价相关性 | 将停牌零量和缺失量视为相同数值 |
| 技术因子 | Alpha158、Alpha101、GTJA191 等值，加因子缺失/质量标记 | 对全样本使用同一均值方差标准化 |
| 基本面/另类 | PIT 对齐后的数值、事件分数和来源覆盖标记 | 以前向填充或零值伪造未披露信息 |
| 市场/行业状态 | 指数收益、行业动量、截面排名，只使用当时可得值 | 将未来指数、未来行业分类或未来统计量带入样本 |

截面因子可以在同一交易日内做 rank 或 winsorize；时序特征的标准化器只能在训练折或当前时点之前拟合。不同特征族的变换、缺失策略和版本必须记录在 cleaning manifest。

### 模型输入形状

| 模型 | 消费视图 | 输入形状 | 量价使用方式 |
|---|---|---|---|
| LightGBM | 单日截面宽表 | `[stock_date, features]` | 使用当日已生成的收益、波动、成交量、技术因子和基本面特征；不把历史 OHLCV 平铺为长向量 |
| 1D CNN | 滚动序列视图 | `[sample, lookback, features]` | 输入过去 N 日的处理后量价和因子序列；卷积提取局部时间形态 |
| Transformer | 滚动序列视图 + mask | `[sample, lookback, features]` 与 mask | 输入同样的处理后序列；注意力学习长依赖和多源特征关系 |

例如，`lookback=60` 时，一个时序样本可包含过去 60 个交易日的量价派生特征、Alpha158/GTJA 因子和可用基本面，而不是“60 天原始 OHLCV 加因子”的无约束拼接。

### LightGBM

从 `clean_feature_panel` 取单日截面宽表。允许有限缺失，但训练输入必须包含同名 `*_is_missing` 特征；特征选择前剔除 `quality_status=invalid`、低覆盖和常数特征。LightGBM 的输入是由历史量价窗口派生出的截面特征，不是原始多日 OHLCV 的平铺数组。

### Transformer/CNN

从同一面板按股票和交易日构造：

```text
X: [sample, lookback_window, feature]
mask: [sample, lookback_window, feature]
y: [sample, horizon]
```

- 窗口不足的样本默认丢弃；若业务要求保留，再显式 padding 并输出 mask。
- 日 K 停牌、缺日或分时缺 bar 不能直接前向填充成真实价格/成交；只能丢弃窗口，或用显式 mask 表达缺失。
- 标准化器只在训练折拟合，然后冻结到验证、测试和线上推理。
- 每个样本的最后可得时间必须早于标签收益起点。
- Purged split 与 embargo 按标签 horizon 应用，避免相邻滑动窗口泄漏。

### 时序 Transformer 基线

深度模型第一版采用 **encoder-only 时序 Transformer**，而不是 NLP 的 encoder-decoder 文本模型：

```text
X:    [batch, lookback=60, feature]
mask: [batch, lookback, feature]
y:    [batch, horizon]
```

- 每只股票在一个预测日的过去 `lookback` 个交易日构成一个样本。
- 输入为清洗后的量价派生特征、Alpha158/GTJA 因子及当时可得的可选特征。
- 输出预测未来 5/20 日超额收益或横截面排名分数。
- 使用 encoder-only、位置编码、attention mask 和训练折 scaler；不在第一版引入 decoder 或文本 token 机制。
- 先与相同面板、相同标签和相同 Purged CV 下的 LightGBM 比较，再决定是否增加 TFT、交叉注意力或多模态结构。

### 图时序网络与 GrifFinNet 路线

根据已提供的国信证券研报摘要，`GrifFinNet` 是面向股票收益预测的图时序模型：它以行业归属和机构共持等多关系图表达股票联动，并使用自适应门控将个股自身表征与邻居信息逐层融合。当前仓库没有该研报原文、官方代码或既有实现；接入前必须归档原始研报、具体模型版本/commit、许可和输入输出约定到 model manifest，避免把不同同名或近似图模型混为一谈。

图神经网络不是 Transformer 的天然替代，而是为“股票之间的关系”提供额外归纳偏置：

| 模型 | 擅长信息 | 主要前提 |
|---|---|---|
| 时序 Transformer | 单只股票跨时间的长依赖、多源特征融合 | 高质量时间序列面板 |
| GrifFinNet 类图时序网络 | 多关系图中的行业/机构共持联动、个股与邻居的动态权衡 | 每个预测日可得、可审计的关系图 |
| LightGBM | 强表格基线、少量数据和高可解释性 | 合格截面特征 |

图模型只有在关系图本身提供稳定增量信息时才可能优于时序 Transformer。行业静态图、未来才披露的共同持仓、全样本相关系数或未来供应链关系都会造成无效比较或泄漏。

推荐接入方式是逐层交织的时空模块，而不是“先完整跑图、再完整跑 Transformer”的简单串联：

```text
X_t: [N_stocks, T_days, D_features]
A_industry,t + A_holding,t
  -> repeated temporal attention + multi-relation graph message passing
  -> adaptive gate: own representation <-> neighbor representation
  -> ranking/regression head -> score per stock
```

门控权重是模型诊断的一部分：高邻居权重通常表示行业轮动或系统性风险阶段的关联信息更强；高自身权重表示个股特征更重要。该解释只能作为研究信号，不能单独作为投资结论。

其中 `G_t` 必须按预测日快照生成：

- 行业边使用当日生效的 `industry_l1/l2/l3`。
- 机构共持边按基金季报等披露时间更新，而不是按报告期末立即生效；节点、基金权重、边权计算方法和 `available_at` 必须保存。第一版可使用“共同持有基金数”或权重重叠度，后续再比较 Jaccard、min-weight 和余弦权重。
- 滚动收益相关边只使用 `t` 及之前窗口计算，并保存窗口、阈值和边权版本。
- 供应链、主题、公告关联等边必须携带 `available_at`；无日期的边不得进入历史训练。
- 每张图保存节点 universe、边类型、边权、生成时间和 `graph_version`。

实验顺序：

1. LightGBM：清洗截面面板基线。
2. 时序 Transformer：同一面板的单股时序基线。
3. 图时序模型：先只加 PIT 行业图，再加入按披露时间更新的机构共持图。
4. 消融行业图、共持图、门控和无图 Transformer；滚动相关或其他关系仅在前述模型稳定后加入。
5. 仅在图模型相对时序 Transformer 的 OOS 增量通过门槛后，才进入生产候选。

评估必须固定相同股票池、标签、Purged CV folds、交易成本和调仓规则，报告 RankIC/ICIR、Top 组收益、换手、容量、回撤、训练时间、图覆盖率和门控权重分布。不能只比较训练损失或单一回测区间。可参考 Qlib 的 HIST、KRNN 等图/关系建模数据接口和评估组织形式，但不直接将其结果视为 GrifFinNet 复现。

## 数据质量报告

每次 `clean-feature-panel` 应生成 JSON、CSV 和 Markdown 报告，至少包括：

| 模块 | 指标 |
|---|---|
| 宇宙 | 股票数、交易日数、日 K/分时覆盖率、最新交易日 |
| 基本面 | stock info、估值、财务、行业的股票覆盖率和可得时间滞后 |
| 因子 | 每因子缺失率、常数率、极值率、有效截面数、相关性、版本 |
| 时序样本 | 每个 lookback/horizon 的可用样本数、padding 比例、mask 比例 |
| 泄漏 | `available_at > trade_date`、未来收益重叠、标准化拟合范围违规 |
| 门禁 | 阈值、实际值、通过/阻断原因 |

报告路径建议为：

```text
output/data_quality/clean_feature_panel_<market>_<timestamp>.json
output/data_quality/clean_feature_panel_<market>_<timestamp>.md
```

## 实施阶段

### P0.1：质量规则与审计表

- 定义 `DataQualityRule`、`DataQualityIssue` 和 cleaning manifest schema。
- 为日 K、分时、基本面建立基础校验，先只报告，不改变训练输入。
- 在 `cn-coverage-check` 之外新增按字段、因子和日期的质量摘要。

验收：固定输入可重复生成相同 issue 和报告；原始数据不被修改。**已完成**：
`DataQualityRule`/`DataQualityIssue`、日 K/分时/PIT 校验、确定性聚合和 JSON/CSV/Markdown
报告已落地；CN 批量同步会在写入前生成质量摘要。

### P0.2：PIT 基本面和清洗面板

- 实现 `clean_feature_panel` 长表和宽表读取接口。
- 将 `available_at`、缺失/填补/极值标记写入估值、财务和行业特征。
- 实现日频截面 winsorize、标准化和训练期冻结的 scaler manifest。

验收：任一历史日期的面板不读取未来财务和未来统计量。**基础能力已完成**：
`clean_feature_panel` 保留 raw/clean/缺失/填补/极值标记，PIT 违规行标记 invalid，
并输出可冻结、可复用的 scaler manifest；接入具体财务字段和训练入口列入后续增量。

### P0.3：LightGBM 接入与强门禁

- 让 `select --analysis-mode lightgbm` 读取清洗宽表和缺失标记。
- 训练前执行质量报告门禁；失败时不训练，并导出明确缺失字段和股票列表。
- 保存模型使用的 `cleaning_version`、阈值和特征清单。

验收：人为注入低覆盖/未来数据后训练被拒绝；合格面板可复现同一训练输入。**基础训练器已实现**：
`clean_panel` 写入带版本和质量标记的面板，`lightgbm` stage 保存 `model.txt` 与
`model_manifest.json`，支持兼容 Booster warm-start；`selection` 已切换为读取持久化模型分数，
不再在日常选股时重训或重算因子。

### P1.1：分时与微结构（后置）

- 对分钟线添加 session、bar 完整率与异常价量规则。
- 只从合格分时日生成微结构特征；与日 K 特征分开统计。

验收：缺 bar、乱序 bar 和半日交易样本不会静默形成完整窗口。

当前安排：暂不阻塞日 K、清洗面板、模型训练和选股。等模型公平评估与纸面交易闭环完成后，
再接入分时微结构特征。

### P1.2：Transformer/CNN 数据集

- 实现滚动窗口 dataset、mask、训练折 scaler 和 purged/embargo split。
- 复用 `clean_feature_panel`，不在深度模型目录复制清洗逻辑。
- 先实现 encoder-only 时序 Transformer，并与 LightGBM 做相同面板的 OOS 对照。

验收：窗口边界、mask、标签时间和训练折标准化均有单测。

当前进度：Transformer 与 CNN 已复用 `clean_feature_panel`，实现滚动窗口、原始缺失 mask、训练折
scaler、按交易日截面去极值/鲁棒标准化、purged/embargo 训练验证切分、MPS/CUDA/CPU 设备选择和
checkpoint 保存/加载；Transformer 支持兼容 warm-start。统一多折 walk-forward 评估与 CNN warm-start
仍待实现。

### P1.2.1：统一 walk-forward 评估（当前进行）

- 使用 expanding history、固定 test block、purge 和 embargo 生成同一组时间折。
- 对 LightGBM、Transformer、CNN 统一计算 RankIC、IC、Top/Bottom 分组收益、多空收益、超额收益、
  换手和最大回撤。
- 输出 CSV/JSON/Markdown 比较报告；只允许样本外折指标进入模型选择，不能用单次训练 loss 决定生产模型。

基础评估器已落地于 `factor_engine/ml/walk_forward.py`，支持
`compare_walk_forward_predictions` 和 `write_walk_forward_report`；后续将接入每折重训和纸面
交易成熟 outcome，形成真正的 OOS 评估。

每折重训预测现已由 `factor_engine/ml/oos_predictions.py` 提供。建议执行顺序为：

```bash
uv run python scripts/run_cn_pipeline.py --stage oos_predictions
uv run python scripts/run_cn_pipeline.py --stage model_comparison
```

两个阶段默认关闭，避免日常数据刷新意外触发全量折训练。`oos_predictions` 成功后输出
`output/oos_predictions/cn_<model>_oos_predictions.csv` 及对应 manifest；比较报告只读取这些
带有 `forward_return_*` 标签的文件。

### P1.3：图时序网络

- 固定 GrifFinNet 或替代图时序模型的来源、版本、许可证和 manifest。
- 实现 PIT 行业图、按披露时间更新的机构共持图快照，以及边可得时间校验。
- 实现时序注意力、多关系图消息传递和自身/邻居自适应门控的交织层。
- 在时序 Transformer node embedding 上完成行业图、共持图和门控的同折 OOS 消融。

验收：行业和共持边不存在未来信息；行业图、共持图、门控和无图 Transformer 可重复比较；图模型未达 OOS 增量门槛时不进入生产候选。

### P1.4：A 股另类数据

- 建立 CN 股票名称/别名 registry、evidence schema 与事件 importer。
- 将新闻、公告、研报、搜索和主题事件 PIT 化后接入面板。
- 单独报告来源覆盖、发布时间滞后和证据质量。

验收：同一条 evidence 不会在 `available_at` 之前进入任何训练样本。

## 测试矩阵

| 风险 | 最小测试 |
|---|---|
| OHLCV 异常 | 倒挂价格、负成交量、重复日期、不同复权混入 |
| PIT 泄漏 | 晚披露财报、重述报表、未来行业映射、未来新闻发布时间 |
| 缺失 | 全空列、局部缺失、停牌窗口、分时缺 bar |
| 标准化泄漏 | 测试期极值不能改变训练期 scaler |
| 模型输入 | LightGBM missing indicator；Transformer/CNN window 和 mask 形状 |
| 可复现 | 相同原始数据和 cleaning version 生成相同 manifest/hash |

## 决策记录

- 训练门禁按“最小可训练样本 + PIT 合法性 + 核心特征完整性”判断，不按全市场字段覆盖或下载命令是否返回成功判断。
- 未接入的 A 股另类数据保持关闭并在报告中说明，不用 HK 特征替代。
- 清洗规则版本化；任何阈值变化都需要新的 `cleaning_version` 和回测复验。

## 从 Alpha 到执行的研究闭环

《打开量化投资的黑箱》所描述的链路可概括为：

```text
数据 -> Alpha 模型 / 风险模型 / 交易成本模型
     -> 组合构建模型 <-> 执行模型
```

这比“模型打分后直接取 TopN”更适合作为生产研究架构，但不应替换本项目已有的数据、因子和预测模型。正确的落地方式是把现有能力分别接到清晰的输入输出契约上：

```text
raw / PIT data
  -> clean_feature_panel
  -> Alpha provider (LightGBM, Transformer, graph-temporal ...)
  -> alpha panel
  -> risk model + expected-cost model + current holdings
  -> constrained portfolio optimizer
  -> target weights / orders
  -> execution simulator or broker adapter
  -> fills + TCA -> cost/risk calibration and walk-forward evaluation
```

其中组合构建与执行之间的双向箭头表示离线回放、TCA 校准和滚动研究反馈；生产时必须是单向的 `target weights -> orders -> fills`，不能形成同步循环依赖。

### 当前实现的对应关系与缺口

| 黑箱模块 | 现有实现 | 状态与缺口 |
|---|---|---|
| 数据与 Alpha | `factor_engine/expressions/`、`factor_engine/ml/lightgbm_ranker.py`、`core/lightgbm_analysis.py` | 已有因子和 LightGBM 排名；待接入 `clean_feature_panel` 与统一预测输出。 |
| 风险模型 | `factor_engine/ml/neutralization.py` 的行业/市值中性化，以及行业集中度约束 | 这不是组合风险模型；尚无随时间更新的因子暴露、特异风险和协方差矩阵。 |
| 成本模型 | `factor_engine/portfolio/costs.py` | 有可解释的 ADV/波动/价差代理成本、模拟 TCA 与 Ridge 研究模型；当前主要在选股后标注，而非优化目标输入。 |
| 组合构建 | `backtest_engine/portfolio.py`、`backtest_engine/industry_selector.py` | 当前为行业筛选、TopN、启发式权重、单票流动性上限；成本调整发生在最终持仓已生成后。 |
| 执行 | `factor_engine/rl/execution_simulator.py`、`factor_engine/rl/portfolio_env.py` | 有 TWAP/VWAP/POV/IS/AC 模拟与离线 RL 环境；尚未消费优化器订单或真实成交回传。 |

因此，现有实现适合作为可运行的研究基线；目标架构更完整。优先补齐风险模型和约束优化器，而不是先用 RL 或图模型替换当前的 LightGBM。

### 统一契约

Alpha 模型必须输出同一张可追溯面板，允许 LightGBM、Transformer 和图模型互换：

```text
asof_date, stock_code, alpha_score, expected_return, uncertainty,
model_version, feature_version, cleaning_version, universe_version
```

风险与成本也以预测日为索引：

```text
risk: asof_date, stock_code, factor exposures, specific_variance, covariance_version
cost: asof_date, stock_code, buy_cost_bps, sell_cost_bps, adv_20d, max_participation
state: asof_date, stock_code, current_weight, tradable_flag, industry, available_at
```

所有字段按 `asof_date` 的可得时间截断；预测、风险、成本、持仓和成交使用同一股票池快照，避免各模块看到不同 universe 或未来数据。

### 最小可用组合优化器

第一版不引入端到端 RL。使用现有 `scipy` 实现长仓约束优化器，给定目标权重 `w`、当前权重 `w_prev`、预测收益 `alpha`、风险协方差 `Sigma` 和交易成本 `c`：

```text
maximize  alpha' w
          - lambda_risk * w' Sigma w
          - lambda_turnover * sum(abs(w - w_prev))
          - lambda_cost * sum(c * abs(w - w_prev))
```

第一版约束：`sum(w) <= gross_exposure`、`0 <= w_i <= max_weight_i`、行业主动权重上限、单日换手上限、订单金额不超过 `ADV_20d * max_participation`，以及停牌/涨跌停/缺失流动性股票不可买入。求解失败时显式回退到当前可复现的 `TopNPortfolioBuilder`，并在 manifest 中记录原因，不能静默改变策略。

风险模型从小而可审计的截面模型起步：市场、行业、规模、动量、波动率、流动性暴露，加滚动残差协方差与收缩估计。行业/市值中性化只作为 Alpha 预处理，不可代替该风险模型。第一版无需追求 Barra 复刻，但必须有 `asof_date`、暴露版本、协方差估计窗口和 universe 记录。

### 实施顺序与验收

1. 完成 P0.1--P0.3 的 `clean_feature_panel`、PIT 与预测面板；没有一致时间截面，后续优化无可靠输入。
2. 增加 `alpha_panel`、`risk_snapshot`、`cost_snapshot`、`portfolio_state` 的 schema 和 manifest，先把 `TopNPortfolioBuilder` 输出转换为这些契约的消费者/生产者。
3. 新建 `factor_engine/portfolio/risk.py` 与 `optimizer.py`：先实现风险快照、约束可行性检查和 `scipy` 长仓优化；复用 `costs.py`，不复制成本公式。
4. 让 `select` 支持 `portfolio_mode = topn | mean_variance_cost_aware`；保留 `topn` 为默认基线，配置文件仅覆盖参数。
5. 将优化器订单送入 `ExecutionSimulator`，输出 decision price、arrival price、fill、未完成量和 TCA；真实执行接口以后以同一订单契约替换模拟器。
6. 采用 walk-forward OOS 对照：`TopN`、仅风险、风险+换手、风险+成本、执行回放后净收益。固定相同 Alpha、股票池、调仓日、成本假设和约束。

生产候选的判定不只看预测 IC：同时比较 RankIC/ICIR、净收益、换手、容量、最大回撤、行业主动暴露、约束违例数、成交完成率和实际/预测 TCA 偏差。只有成本与风险调整后的 OOS 结果稳定优于 `TopN` 基线，优化器才替代默认路径。

## 纸面交易与结果反馈闭环

当前仓库已有回测和模拟交易的组成部分：`backtest_engine/broker.py` 的 `SimulatedBroker`、`backtest_engine/engine.py` 的单标的事件回测、`backtest_engine/portfolio.py` 的组合 replay、`factor_engine/rl/execution_simulator.py` 的拆单模拟，以及 `MarketDataService` 的 signals/trades 持久化。它们能够验证历史信号与模拟成交，但尚未实现一个跨日存续的纸面账户：每日接收一次真实时点建议，保存当时输入和订单，随后按实际新增行情逐日估值，并在各目标期限成熟时统一评估。

### 目标与边界

“最大收益”和“最小回撤”不能同时作为无约束的单一目标。纸面交易以净收益最大化为主，同时把最大回撤、换手、集中度、容量和成本误差作为硬约束或独立比较指标；候选策略按 Pareto 前沿和预设风险预算选出，而不是事后只选择收益最高的回测。

纸面账户必须与历史回测分离：回测可以访问当时完整历史，纸面账户只能使用运行时已落库、`available_at <= decision_time` 的数据。任何策略参数升级创建新的 `strategy_version`；已有建议和成交不得被新版本改写。

### 生命周期

```text
select / optimizer
  -> signal snapshot (decision_time, feature/model/risk/cost versions)
  -> paper order
  -> next tradable session simulated fill
  -> daily position/NAV mark
  -> N-day outcome and benchmark attribution
  -> periodic strategy evaluation
  -> candidate promotion / parameter research
```

1. **发布建议**：保存完整 ranking，不只保存 TopN；入选目标权重生成 `paper_order`。快照包括 `run_id`、`strategy_version`、`model_version`、`feature/cleaning/risk/cost` 版本、预测分数、目标持仓、当时价格与可得时间。
2. **模拟成交**：默认在下一可交易日开盘，以配置的费用、滑点、涨跌停、停牌、最小交易单位和 ADV 参与率约束撮合。分钟数据完整时复用 `ExecutionSimulator`；否则明确记录日线撮合假设和未成交量。
3. **逐日记账**：每个账户维护现金、持仓、冻结订单、公司行为调整后数量、每日 NAV、日收益、峰值 NAV 与实时回撤。调仓按目标权重和当前持仓产生差额订单，不能把每次推荐都当作独立、可重复使用的全额本金。
4. **到期结果**：对每个建议在 `N = 1, 5, 20, 60` 个交易日分别记录毛/净收益、超额收益、最大不利波动（MAE）、最大有利波动（MFE）、是否可成交、实际持有期、换手、实际与预测 TCA 偏差；未满期限状态为 `pending`，不纳入成熟期评价。
5. **评估与反馈**：按策略版本、市场状态、行业、流动性桶、信号分位和持有期聚合 outcome，生成月度/滚动报告。只有冻结的 walk-forward 纸面样本达到最小观察数并同时满足收益、回撤、成本和容量门槛，才成为下一轮训练或参数搜索的候选数据；不得用尚未成熟或同一评估窗口的结果直接改写并宣称有效。

### 最小数据模型

现有 `signals` 和 `trades` 数据集保留为兼容层；纸面账户新增独立实体或等价带版本字段的表，不能复用历史 backtest 的无状态 CSV：

```text
paper_accounts(account_id, base_currency, initial_capital, config_version, status)
paper_orders(order_id, account_id, run_id, stock_code, side, target_weight,
             decision_time, executable_from, status, constraints_snapshot)
paper_fills(fill_id, order_id, fill_time, quantity, price, commission,
            slippage_bps, participation_rate, provenance)
paper_positions(account_id, asof_date, stock_code, quantity, cost_basis, market_value)
paper_nav(account_id, asof_date, cash, market_value, nav, daily_return, drawdown)
signal_outcomes(run_id, stock_code, horizon, status, gross_return, net_return,
                benchmark_return, excess_return, mae, mfe, tca_error_bps)
strategy_evaluations(strategy_version, start_date, end_date, sample_count,
                     annualized_return, max_drawdown, sharpe, turnover, capacity, gate_status)
```

所有行都要携带 `market`、`strategy_version`、`source`、`created_at`，并通过 `run_id/order_id` 追溯到原始建议和 manifest。

### 配置与验收

纸面交易只通过 TOML 配置驱动，例如 `config/paper_trading.toml` 提供账户初始资金、市场、调仓频率、执行价规则、费用、滑点、最大参与率、持有期、基准、收益/回撤/换手门槛和报告路径；命令只指定配置路径和可选 profile。

第一版验收：

- 同一建议快照和行情输入可重复得到相同订单、成交、持仓和 NAV。
- T+1、停牌、涨跌停、涨停无法买入、除权与部分成交均有明确事件和测试。
- 一个 20 日建议在第 20 个有效交易日才转为 `matured`，净收益与基准、成本和回撤可重算并可追溯。
- 同一 Alpha 下，`TopN`、成本感知优化器和纸面账户的持仓、净值和 TCA 可以逐笔对账。
- 策略升级不会改变旧版本的建议、成交、结果或评价区间。

## 牛市、熊市与震荡市的状态自适应

LightGBM、CNN、时序 Transformer 和图时序网络都可以针对不同市场状态使用不同策略，但应由一个独立的 **market-regime router** 统一决定“使用哪个模型/因子权重/组合风险预算”，而不是在每个模型内部各自猜测市场状态。模型负责预测 Alpha，路由器负责选择候选策略，组合优化器负责把信号变成风险可控的权重。

### 状态识别

第一版使用基准指数和市场广度的可复现规则，按日计算且只使用当日收盘前可得数据：

```text
regime_features:
  benchmark_return_5/20/60/120d
  benchmark_above_ma20/60/120
  market_breadth_above_ma20
  cross_sectional_median_return_20d
  realized_volatility_20/60d
  limit_up/down_ratio, turnover_breadth
```

建议的初始标签只是配置化的研究起点，不是固定真理：

```text
bull:     long trend + positive breadth + acceptable volatility
bear:     negative trend/breadth or stress volatility
sideways: neither sustained trend nor sustained breadth
```

状态需要滞后确认、最小持续天数和 hysteresis，避免在临界点每天切换。模型训练或回测只能读取 `regime(asof_date)`；不能用事后完整牛熊区间标签训练历史日期。HMM、变点检测或分类模型可作为后续候选，但必须与规则标签做同一 OOS 比较。

### 状态到策略的路由

每个模型都提供同一 Alpha 契约，因此可以独立训练、组合或退化到 LightGBM 基线：

| 状态 | Alpha/因子倾向 | 组合与执行倾向 |
|---|---|---|
| 牛市 | 动量、趋势突破、行业强度；可提高 Transformer/图模型对行业联动的权重 | 提高净暴露但受波动、行业和单票上限约束；执行可使用 VWAP/POV。 |
| 熊市 | 质量、低波、流动性、盈利稳定性；降低高 Beta 和纯动量权重 | 降低总仓位、提高现金、收紧回撤和参与率预算；禁止不可交易标的。 |
| 震荡市 | 反转、质量、估值、短周期均值回归；降低追涨信号 | 限制换手和集中度；优先低成本、稳定流动性的订单。 |

上表只是候选策略集合。最终权重必须通过历史 walk-forward、交易成本和基准相对收益验证。可以采用三种实现层级：

1. **规则路由**：状态对应固定因子/模型权重，最容易审计，作为第一版。
2. **软路由/集成**：用状态概率 `p(bull), p(bear), p(sideways)` 加权多个模型的 Alpha，再交给同一个组合优化器。
3. **状态条件模型**：给模型加入 PIT regime features 或分别训练模型；只有样本量足够且 OOS 增量稳定时采用，避免三套模型造成数据稀疏和过拟合。

图模型仍然只负责额外的股票关系信息；不能让图结构直接决定牛熊状态。行业图和机构共持图应在相同状态下做消融，确认增益来自关系信息而不是状态切分。

### 跑赢大盘的正确判定

“跑赢大盘”必须定义基准和比较口径。CN 默认至少保存 `CSI300`、`CSI500` 或配置指定基准的 point-in-time 收盘和可交易收益，HK 使用相应市场基准。纸面账户和每个 regime 分段都报告：

```text
net_return, benchmark_return, active_return
annualized_return, max_drawdown, downside_volatility
beta, tracking_error, information_ratio, turnover, capacity
```

生产候选的最低门槛应为：全样本 `active_return > 0`，滚动窗口中大多数窗口不落后于基准，且最大回撤、换手、容量和成本误差不超过配置阈值。每个牛/熊/震荡状态还要单独报告样本数、状态占比、净收益、超额收益和最大回撤；不能只靠某一轮牛市的收益宣布成功。

### 落地顺序

1. 在 `clean_feature_panel` 增加基准行情、广度和 `regime_version`，生成每日 `market_regime` 快照。
2. 新增配置化 `regime_router`，先支持 `rule` 和 `soft_ensemble`，输出 `strategy_id`、状态概率、因子/模型权重和风险预算。
3. 让 LightGBM 先接入路由；CNN/Transformer/图模型通过同一 Alpha provider 接口接入，不复制路由和清洗逻辑。
4. 纸面账户按每次建议记录 regime、路由决策、模型版本和目标权重，N 日成熟后按状态归因。
5. 固定相同股票池、标签、成本、调仓规则和 purged walk-forward folds，比较：单一 LightGBM、状态路由 LightGBM、Transformer、图时序模型和软集成。
6. 只有状态分段和全样本均通过净超额、回撤、容量与稳定性门槛，才允许状态路由替换默认策略；状态不确定或数据不足时回退到防御性基线。

## 训练窗口与 20/60/120 日的选择

这里必须区分两个参数：

- **输入 lookback**：一个样本向过去看多少交易日，例如 `[20, 60, 120]`；主要影响模型捕捉的时间尺度。
- **训练历史窗口**：模型拟合使用多少历史交易日，例如 expanding history 或最近 `504/756` 天；主要影响样本量、旧制度影响和适应速度。

`select --days 365` 是当前分析/预测覆盖周期，不应直接理解成模型只使用最近 365 天训练。当前 LightGBM 管线实际采用 expanding-window rolling，默认 `label_horizon=20`、`min_train_days=120`，但生产前仍应通过 OOS 试验确认窗口。

### 建议的初始配置

不要在 20、60、120 中只选一个。第一版使用多尺度特征或多分支输入：

| 预测目标 | 推荐输入 lookback | 解释 |
|---|---|---|
| 未来 1--5 日 | 20、60 | 短期形态、成交量冲击和近期波动。 |
| 未来 20 日 | 20、60、120 | 同时覆盖短期反转、中期动量和趋势状态。 |
| 未来 60 日 | 60、120、252 | 更需要中长期趋势、回撤和行业周期。 |

LightGBM 不需要把 20/60/120 天原始 OHLCV 平铺输入；应从这些窗口生成收益、波动、量比、回撤、均线偏离、流动性等截面特征。CNN/Transformer 则可以使用同一清洗面板的 `[20]`、`[60]`、`[120]` 序列分支，或先以 `lookback=60` 作为基线。图时序模型也先固定单一 lookback 做公平比较，再测试多尺度扩展。

训练历史窗口建议先使用 expanding history；如果需要快速适应制度变化，再比较最近 `504` 天（约 2 年）和 `756` 天（约 3 年）的 rolling history。只有约一年历史通常不足以覆盖牛、熊、震荡三种状态，也容易让一个极端行情主导参数。训练窗口的选择应在训练折内部用 walk-forward 验证，不能根据最终测试区间挑选。

### 连续一年涨 10 倍的股票如何处理

这种股票不应因为涨幅大就自动删除，也不应让它单独决定模型：

1. 价格序列使用复权一致的对数收益、横截面排名和滚动波动/回撤，不直接使用原始价格水平。
2. 对标签和极端收益执行训练折内 winsorize 或稳健变换；当前 LightGBM 已有按交易日 `0.5%/99.5%` 的标签截尾逻辑，阈值仍需写入 cleaning manifest。
3. 增加 `recent_return_20/60/120`、距滚动高点、波动率、流动性和 `extreme_move_flag`，让模型学习“强趋势”和“过热风险”是不同状态。
4. 保留该股票的有效样本，但限制单日截面影响、单票组合权重和 ADV 参与率；涨停/无法成交时标记为不可交易，不能按理想收盘价成交。
5. 按股票、行业和市场状态分别报告收益贡献，检查结果是否只由少数十倍股驱动。

### 正式选择方法

建立固定候选集，例如：

```text
lookback: [20, 60, 120]
training_history: [expanding, 504, 756]
label_horizon: [5, 20, 60]
```

在每个 walk-forward 验证折、每个市场状态内记录净 RankIC、超额收益、最大回撤、换手、容量和样本数。选择跨多个折都稳定的配置；若多个窗口接近，使用 OOS 加权 ensemble，权重只由过去成熟的纸面 outcome 更新。不要每天根据最近一个窗口的收益切换模型，否则会形成二次过拟合。

## 策略形态：反弹、首板/二板与趋势跟踪

这不是简单的“两种策略”：底部反弹、首板/二板和上行趋势跟踪依赖不同的市场机制、成交约束和持有周期，应该作为三个 `strategy_id` 共享数据清洗和 Alpha 接口，但使用各自的入场标签、风险规则和 outcome horizon。模型不需要各自复制一套清洗逻辑；LightGBM 可以把形态特征作为截面输入，CNN/Transformer 建模形态序列，图模型补充行业/资金关系，最后统一交给组合优化器。

### 底部反弹：识别“反转确认”，不预测绝对底点

实时系统无法知道最低价是否已经出现，因此“底部”应定义为概率性的 **候选区间 + 确认触发 + 失效条件**，而不是一个最低点标签。推荐拆成三层：

```text
context:     个股/行业相对基准经历显著回撤，且非持续基本面恶化禁入
exhaustion:  下跌动能衰竭、波动/成交量压缩或卖压减弱、形成可识别支撑区
confirmation: higher-low 或收复短期均线/箱体，反弹日量价配合且有后续跟随
invalidation: 再创新低、跌破支撑、成交不可执行或市场进入压力状态
```

一组可配置的研究起点（不是固定阈值）是：60/120 日回撤达到历史横截面较低分位；随后 10--30 日形成窄幅底部区间；最近 5 日收益转正、至少 2/3 日上涨、收盘站回 MA10/MA20，且反弹成交量相对底部放大。入场价使用下一可交易时点，止损用 ATR/支撑位，持有期先测试 5/20/40 日。必须同时记录 `drawdown_from_peak`、`base_range`、`volume_dryup`、`higher_low`、`reclaim_ma20` 和 `confirmation_strength`。

当前 `low_price_setup` 中的绝对价格条件 `0.20 <= price <= 8.0` 不能作为 A 股通用“底部”定义，应改成相对回撤、波动和流动性条件；现有 `bottom_rebound_score` 可保留为候选特征，但不能单独宣称已经见底。

### 首板/二板：这是 A 股专用的事件与执行策略

首板/二板不是普通的趋势突破标签，必须基于 point-in-time 的涨跌停规则和可成交性计算：

- 依据前收、板块（主板/创业板/科创板/北交所）、ST 状态和最小价位单位计算当日理论涨停价，并允许交易所规则版本变化。
- **首板**：当日达到涨停且前 `K` 个交易日没有满足涨停事件；**二板**：连续两个有效交易日达到涨停。`K`、是否允许一字板、开板回封和炸板都应配置化。
- 保存 `limit_price`、`is_limit_up`、`board_count`、封单/成交量代理、开板次数、次日可交易标志和信号发布时间。
- 回测不能按涨停收盘价无条件买入；封死涨停通常不可成交，必须用下一交易日开盘/首个可成交价、部分成交或未成交处理，并纳入 T+1、停牌、涨跌停和流动性约束。

首板/二板标签应分别预测次日、3 日和 5 日的可实现净收益与最大不利波动，不与底部反弹共用 20/60 日持有标签。若当前日线数据没有可靠涨跌停、开板和成交明细，该策略只能输出研究信号，不能进入纸面账户。

### 上行趋势跟踪

趋势策略应定义“持续趋势”而不是一次性涨幅，例如：收盘位于 MA20/60/120 之上且均线斜率为正；突破 60/120 日高点后没有快速跌回；相对基准强度、行业广度和成交额满足最低阈值。入场可采用突破或回踩确认，退出采用 MA/ATR 跟踪止损、趋势破坏或 20/60/120 日时间上限。初始 outcome horizon 可设 20/60/120 日，不能沿用反弹或首板的短周期标签。

### 策略确定与比较

每种形态都生成独立的 `strategy_id`、`entry_reason`、`invalidation_reason`、`holding_horizon` 和特征快照。训练标签示例：

```text
bottom_rebound:  future excess return 5/20/40d + forward MAE
first/second_board: executable next-day return 1/3/5d + fill probability
trend_following: future excess return 20/60/120d + trailing-stop path
```

先用规则配方定义候选，再用 LightGBM/时序模型估计每个候选的成功概率和净收益；通过相同股票池、成本、调仓规则和 walk-forward OOS 比较 RankIC、净收益、最大回撤、换手、容量和成交完成率。没有稳定增量的策略保持关闭，不因少数历史十倍股或极高历史胜率上线。
