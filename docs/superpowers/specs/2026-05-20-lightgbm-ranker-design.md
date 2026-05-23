# LightGBM Ranker Design

## Goal

为 `select_stocks` 增加一个新的 `lightgbm` 研究模式，直接替代当前 `factor` 打分模式，用全市场横截面特征训练 LightGBM Ranker，并继续复用现有的 TopN 排名、CSV 导出、signal recipe 叠加和 signal 落库链路。

## Scope

本次只实现最小可用闭环：

- CLI 支持 `--analysis-mode lightgbm`
- 自动从现有 `factor_set` 生成日频特征
- 以未来固定 horizon 收益构建横截面排序标签
- 在一次 `select_stocks` 命令内完成训练和打分
- 输出与当前 `factor` 模式同形状的分析结果，交给现有 `TopNPortfolioBuilder`
- README 补充安装和使用方式

本次不做：

- 模型持久化
- 独立 `train` / `predict` 两阶段命令
- 多模型集成
- 复杂 walk-forward 编排

## Integration Strategy

`lightgbm` 模式不沿用当前逐股票独立分析路径，而是走一条新的 market-level 分支：

1. 批量加载股票 OHLCV
2. 为每只股票生成 `factor_set` 特征
3. 拼成全市场 panel
4. 构造 future return 标签并按交易日分组
5. 训练 `LightGBMRanker`
6. 对 panel 逐行预测
7. 回填到每只股票的时间序列结果中
8. 产出与 `factor` 模式兼容的 result dict

这样可以保持 `TopNPortfolioBuilder`、`persist_signals` 和 `signal_recipes` 全部不动。

## Data And Labeling

- 特征来源：现有 `create_factor_set(factor_set)` 输出
- 频率：港股日线
- 标签：未来 `label_horizon` 交易日收益率
- 排序标签：按每个交易日的横截面 future return 分位数离散成 `0..num_quantiles-1`
- 训练/验证切分：按交易日时间顺序切分，后段日期作为验证集

## Runtime Contract

`lightgbm` 模式需要返回和现有 `factor` 模式一致的关键字段：

- `latest_expected_3m_score`
- `latest_matrix_score`
- `latest_regime_score`
- `latest_entry_type`
- `current_signal_score`
- `buy_signals`
- `backtest`
- `selection_source`
- `factor_explanation`

其中：

- `latest_expected_3m_score` 使用最新横截面模型分数的 0-100 百分位
- `latest_matrix_score` 先复用同一模型分数，避免引入额外语义
- `latest_regime_score` 第一版置为 `NaN`
- `latest_entry_type` 固定为 `lightgbm_rank`
- `selection_source` 固定为 `lightgbm_ranker`
- `factor_explanation` 使用全局特征重要性摘要

## Error Handling

- 当 `lightgbm` 未安装时，给出明确报错，提示使用 `uv sync`
- 当可训练样本不足、分组后标签无效、panel 为空时，返回空结果并打印可读错误
- 保持 `signal_recipes` 为可选增强层，不影响模型主链路

## Testing

优先覆盖：

1. CLI 接受 `--analysis-mode lightgbm`
2. `backtest_portfolio` 能走 `lightgbm` 分支并产出 TopN 结果
3. `lightgbm` 结果对象能被现有 portfolio builder 正常消费
4. README 用法与参数说明同步
