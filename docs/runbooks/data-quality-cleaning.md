# 数据质量与清洗面板

P0.1/P0.2 的基础实现位于 `data/model/quality.py` 和
`data/model/feature_panel.py`。规则只审计、不覆盖 raw/clean 原始行情。

## 质量审计

`MarketDataService.bulk_sync_cn_history` 在每只股票、每个频率写入前审计
OHLCV。日线检查交易日唯一性；分时检查完整 timestamp 唯一性和 A 股交易时段
`09:30-11:30`、`13:00-15:00`。同步结果包含质量计数，并默认生成：

```text
output/data_quality/cn_ohlcv_<timestamp>_cn.json
output/data_quality/cn_ohlcv_<timestamp>_cn.csv
output/data_quality/cn_ohlcv_<timestamp>_cn.md
```

基本面、估值和行业数据使用 `validate_pit_frame` 检查
`available_at <= trade_date` 与 PIT 主键重复；该检查不会自动填充或删除记录。

## 清洗面板

```python
from data.model import clean_feature_panel, feature_panel_to_long

cleaned, manifest = clean_feature_panel(
    frame,
    feature_columns=["ret_20d", "pe_ratio", "alpha158_001"],
    fit_frame=train_fold,
)
long_panel = feature_panel_to_long(cleaned)
```

小规模审计 API 为每个数值特征保留 `*_raw`、`*_clean`、`*_is_missing`、`*_is_imputed` 和
`*_is_outlier`。生产 `clean_panel` 不再把每个单元格展开成审计长表；它保留紧凑的
`*_clean` float32 值和 `*_is_missing` 布尔掩码，raw 值由原始 `features` 数据集审计。
PIT 违规行标记为 `quality_status=invalid`，不会被伪装成可训练值。
`manifest["features"]` 保存训练折 winsor 阈值、中心值和尺度；验证/测试折通过
`scaler_manifest=manifest` 复用同一套参数，避免未来数据改变 scaler。

训练门禁由 pipeline 和训练入口共同执行：核心 OHLCV、标签、lookback 缺失时剔除样本；
PIT 无效行直接拒绝训练；财务、行业、另类数据缺失保留样本并使用缺失标记。训练入口
读取已经物化的 `clean_feature_panel`，不会重新执行因子公式。

### 模型特征质量门槛

`[model_features]` 在每次训练、每个 OOS 训练折独立执行，不修改原始因子或清洗面板：

```toml
[model_features]
min_feature_coverage = 0.05
drop_constant_features = true
```

- `*_clean` 的有效值覆盖率低于阈值时，该值列与对应 `*_is_missing` 列一并排除；不能只留下描述数据源故障的 mask。
- 全缺失、全常量的值列和掩码都会排除。有效但稀疏的特征保留原始缺失 mask，让模型学习其可用性。
- 过滤只使用该训练折数据；模型 manifest 的 `extra.feature_quality` 记录输入列数、输出列数、阈值及剔除列表。推理严格读取 manifest 中保留的列。

默认 `5%` 是启动阈值，不是数据补全目标。对于财务报表等低频 PIT 字段，应先修复来源和披露日期，再依据 OOS 指标将阈值提高到 `20%` 或按特征族设定不同规则。

### A 股缺失数据处理优先级

当前覆盖报告中的行业 `0%` 和财务 `19.6%` 是数据质量问题，不应使用横截面均值、零值或未来报告回填来伪造完整样本。处理顺序如下：

1. 行业：先运行 `fundamental`，检查 `backfill_cn_industry` 的 `updated_count` 和报告中的失败原因；只有拿到带生效日期或 `available_at` 的行业快照，才能用于行业中性化与图模型。
2. 财务：启用 `pipeline.financial_metrics_enabled = true`，用季度报表的实际披露日作为 `available_at`，以最近已披露值前向持有到下一次披露，不能用报告期末日期提前使用数据。
3. 历史估值与换手：优先落入按交易日的历史面板；当前快照字段只能代表当日，不能倒灌到历史日期。缺失无法补齐时留为缺失并让训练过滤器决定是否使用。
4. 另类数据：只导入含 `published_at` 或 `available_at` 的事件/搜索聚合数据；缺失代表无可用观测，不等同于负面信号。
5. 每次数据刷新后依次运行 `features -> clean_panel -> lightgbm`，再检查新的模型 manifest 和质量报告。因子与清洗宽表会使用更新后的 PIT 数据重建；日常 `model_scores` 不会重算因子。

### Parquet 与 ClickHouse 的职责

`features` 原始长表目前同时支持 Parquet 与 ClickHouse 镜像；清洗面板的重建和训练读取固定使用
本地 Parquet。清洗阶段按股票批次和日期窗口流式扫描，把 `(trade_date, stock_code)` 一次性
向量化透视成 Qlib 风格宽表，并在全部批次成功后原子发布。Parquet 目录可
直接作为不可变版本工件，便于复现、校验和失败回滚。`clean_feature_panel` 只有存在
`_SUCCESS.json` 且 `status=completed` 时才允许训练读取；中断留下的部分 parquet 会被明确拒绝。

这不是 ClickHouse 性能不足，而是当前尚未为清洗长表定义完整的 ClickHouse 合约（PIT 版本、清洗
版本、去重键、原子发布和训练固定版本）。在这些约束补齐前，盲目把数亿至十亿行面板再写一份
ClickHouse 会增加存储和同步失败面。推荐职责划分：

- Parquet：原始因子、清洗面板和模型训练输入的主副本，按市场/频率/复权/因子集/年份分区。
- ClickHouse：在线查询、最新截面、信号/选股结果、回测与纸面交易结果；必要时保留原始
  `features` 镜像供快速聚合。

若未来需要 ClickHouse 直接承载清洗面板，必须新增专用 schema、`ReplacingMergeTree` 版本列、
分区策略、manifest/run_id 原子发布和按成功版本读取的训练适配器，再基准测试后切换，不能让
训练路径在两个后端之间隐式漂移。

当前 `clean_panel` 的物化过程按 10 股批次限制 Pandas 内存，训练读取不再执行长表 pivot。
模型训练仍会加载所选日期窗口的宽表；在更长窗口上应做内存基准，必要时启用按日期/股票分块的
Dataset/DataLoader。ClickHouse 不能自动消除模型矩阵的内存需求。

### 与 Qlib 的对应关系

实现参考 Qlib `DataHandlerLP`，但不增加 Qlib 运行时依赖：

| Qlib 概念 | 当前实现 |
|---|---|
| `DK_R` raw | `feature/features` 原始长表与 clean OHLCV |
| `DK_I` infer | `clean_feature_panel` 的 float32 宽特征和缺失掩码 |
| `DK_L` learn | 训练入口追加成熟标签并剔除 label 缺失样本 |
| `infer_processors` | 无穷值处理、按日截面鲁棒标准化、缺失处理 |
| `learn_processors` | 标签生成、purge/embargo、训练/验证时间切分 |
| Dataset segments | `train_dates` / `validation_dates` / OOS folds |

物化阶段不拟合全量 scaler。与 Qlib 的 `fit_start_time/fit_end_time` 约束一致，所有需要拟合的
中心值、尺度和截面变换只在训练折执行，并将配置写入模型 manifest。

## 因子物化与模型训练

因子生成和模型训练应当是两个阶段：

```text
OHLCV / 基本面 / 另类数据
    -> 因子计算 + 清洗
    -> feature/clean_feature_panel 持久化
    -> LightGBM / Transformer / CNN 读取统一面板
```

当前实现状态如下：

| 环节 | 当前行为 |
|---|---|
| `features` | `generate_factor_set` 按最近 `days + warmup_days` 窗口生成因子，写入 feature 层；已有完整覆盖时跳过。默认是 `365 + 180` 个自然日窗口。 |
| `lightgbm` | 读取清洗宽表，按交易日做截面预处理，保存 `model.txt` 和 manifest；支持 `warm_start_path`。 |
| `transformer` | 读取同一清洗宽表，按训练折拟合 scaler，生成固定 lookback 时序窗口，保存 `model.pt` 和 manifest。 |
| `cnn` | 使用同一清洗序列与缺失 mask，通过 1D 卷积提取局部时间模式，保存 `model.pt` 和 manifest。 |
| `model_scores` | 加载已保存模型，仅对 clean panel 最新交易日打分，输出 CSV，不重训、不重算因子。 |

改变因子公式、因子配置、复权方式、清洗版本、标签定义或
特征 schema 时，受影响的历史因子必须重新物化，不能直接混用旧版本。

## LightGBM 模型复用与增量训练

LightGBM 可以保存并复用已经训练的模型。当前项目导出 `model.txt` 和带特征 schema 指纹的
manifest；`model_scores` 阶段通过 `model_path` 加载并预测，不重新训练。

模型工件复用能显著降低日常选股耗时。日常运行不再需要重新读取完整训练窗口、构造标签、
执行 walk-forward 拟合和重复计算因子，而是只做：

```text
增量 OHLCV / 基本面
  -> 增量因子和 clean_feature_panel
  -> 加载已批准的 model.txt
  -> predict 最新交易日截面
  -> 排名、组合约束和导出
```

这会把“全量研究训练”与“日常推理选股”分开：前者按计划消耗更多计算资源并产出候选模型，
后者只使用已批准模型对最新特征打分。模型文件保存的是训练后的树与分裂阈值，类似保存 GPT
的权重；推理不需要重新输入全部历史训练样本，但仍必须提供与训练期完全一致的特征 schema。

效率不等于无限期沿用旧模型。模型 manifest 必须锁定因子版本、特征列顺序、清洗版本、scaler
和训练数据截止日；日常推理若发现 schema 不匹配、最新数据质量门禁失败或特征漂移超过阈值，
应停止使用该模型并转入重训候选，而不是静默补零或更换列。

目标模型工件应至少包含：

```text
model.txt / model.joblib
model_manifest.json
  factor_set, feature_columns, cleaning_version, scaler_manifest_hash
  label_horizon, execution_delay, train_start, train_end
  universe_version, model_version, data_cutoff
```

增量训练建议：

1. 只加入已经成熟的标签样本；例如 `horizon=20` 时，最近 20 个交易日不能直接作为有标签训练数据。
2. 每日新增数据之外保留 replay buffer，例如最近 504/756 个交易日，避免只用一天样本造成灾难性遗忘。
3. 使用 LightGBM 的 `init_model`/继续 boosting 作为短周期 warm-start，但限制新增树数量并监控 OOS 指标、特征漂移和回撤。
4. 按固定周期（例如每周或每月）从 replay window 全量重训，防止树无限累积和旧市场状态污染。

以下情况必须触发全量重训，而不能只做增量：

- `cleaning_version`、因子公式、特征集合或特征顺序变化；
- 标签 horizon、执行延迟、股票池或市场状态定义变化；
- scaler manifest 或 PIT 对齐规则变化；
- 数据分布漂移、模型 OOS RankIC/收益显著下降或回撤超过门槛。

Transformer 通过 `warm_start_path` 和 `warm_start_manifest_path` 加载兼容 checkpoint 后继续
fine-tune；会严格校验因子集、清洗版本、特征 schema 和网络结构。CNN 当前先使用完整重训和同折
OOS 验证，后续增量训练也必须冻结训练折 scaler、保留历史 replay 窗口并重新做 walk-forward 验证。
增量训练不是永久替代定期全量重训。

## Transformer 数值时序基线与优化清单

当前 Transformer 是面向量价、因子和基本面数值列的 encoder-only 基线，而不是 NLP 文本模型。
每个交易日的数值特征和缺失掩码拼接为一个连续 token，经线性层映射到 `d_model`，再叠加可学习
位置编码。训练折 scaler、历史 lookback 和未来收益标签按时间划分；一个样本窗口只包含决策日及
以前的数据，因此 encoder 的双向注意力不会读取未来行情。

该设计适合作为首个可比较模型，不需要为连续数值特征引入离散词表或 BPE/WordPiece tokenizer。
后续优化按以下顺序实施：

1. 已实现：Transformer 对连续特征执行按交易日截面去极值和鲁棒标准化，与 LightGBM 的预处理
   一致；原始缺失掩码与 `*_is_missing` 二元特征单独保留，避免填补过程抹掉缺失语义。
2. 部分已实现：交易日历特征包含周内循环编码、月初和月末标记。市场状态仍待接入 PIT 可审计的
   宽基指数序列后，增加指数趋势、波动和财报披露窗口；不能使用事后市场统计量替代。
3. 所有基本面和另类字段依照 `available_at` 做披露延迟对齐，禁止在报表实际可得前进入样本。
4. 文本另类数据先通过独立 Tokenizer 和 NLP encoder 生成按日聚合的情绪/事件 embedding，再与
   数值时序 token 融合；不要将原始长文本直接拼入量价序列。
5. 用 walk-forward 的 RankIC、分组收益、换手、交易成本后收益和最大回撤评价模型，只有在这些
   样本外指标优于 LightGBM 时才扩大 Transformer 的组合权重。

LightGBM、Transformer 和 CNN 当前均使用 purged 时间切分：验证集前的 `embargo_days` 个决策日从
训练集移除，默认等于标签 horizon。该切分信息写入模型 manifest。统一 expanding walk-forward
评估器已提供相同折的 RankIC、分组收益、超额收益、换手和最大回撤比较；完整的“每折重新训练并
生成 OOS 预测”以及成熟纸面 outcome 仍是下一阶段。
