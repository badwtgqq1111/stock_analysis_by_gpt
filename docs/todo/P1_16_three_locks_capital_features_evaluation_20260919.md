# P1.16 三把锁 / 主力资金 / 敢死队资金特征：训练与选股验证结论

> 状态：FEATURE ENGINE BUILT / LIGHTGBM ABLATION DONE / NO OOS GAIN CONFIRMED
> 日期：2026-09-19
> 关联：`P1_16_three_locks_capital_features_plan.md`、`P1_15_moneyflow_evaluation_20260918.md`
> 结论一句话：**`capital_flow_locks.v1` 特征已按方案落地并通过数据门禁，但在两个独立 OOS 窗口中没有可复现的选股增益；2026-09-11 之后一周的结果在噪声范围内，不能作为开启 signal boost 的依据。**

## 1. 本次交付物

| 类型 | 路径 |
|---|---|
| 特征引擎（代码） | `factor_engine/expressions/capital_flow_locks.py` |
| 宽表面板 | `assets/data/derived/cn_capital_flow_locks_features.parquet`（2,640,338 行 × 213 列，2024-08-23 ~ 2026-09-18） |
| 特征清单 | `output/research/capital_flow_locks_feature_manifest.json` |
| 数据门禁报告 | `output/research/capital_flow_locks_quality_report.json`（6/6 通过） |
| 消融报告（窗口 A/B） | `output/research/capital_flow_locks_ablation_report{,_window2}.{json,md}` |
| 跨窗口验收总结 | `output/research/capital_flow_locks_ablation_summary.{json,md}` |
| 9/11 截面影子分数 | `output/research/cn_capital_flow_shadow_scores.csv` |
| 资金确认分归因 | `output/results_cn/capital_flow_confirmation_attribution.csv` |
| 单元测试 | `test/test_cn_capital_flow_locks.py`（7 项，含前视、缺失、事件编码） |

## 2. 特征与数据门禁

- 特征版本 `capital_flow_locks.v1`，共 205 个特征 + 31 个缺失掩码：
  `pvx` 24（量价上下文，进入所有变体基线）、`mf` 31、`cyc` 38、`main` 35、`daredevil` 51、`locks` 10、`consensus` 16。
- 数据源：本地 `ohlcv`、`moneyflow`、`moneyflow_dc`、`moneyflow_ths`、`daily_basic`、`top_list`、`top_inst`、`hm_detail`、`cyq_perf`（`cyq_chips` 仍未全量，未纳入）。
- 门禁结果：

| 检查 | 结果 |
|---|---|
| 主键重复 | 0 |
| 资金流 × 日 K 匹配率（覆盖股票池口径） | 99.80%（门槛 98%） |
| 全市场口径匹配率（含 128 只 B 股等无资金流标的） | 97.40% |
| CYC/close 量纲一致性 | 中位数 1.015 |
| PIT 无前视（截断后重算 105,812 行 × 211 列） | 0 处不一致（2 行为阈值浮点边界翻转，已单独标注） |
| 缺失 / 未上榜 / 源未覆盖可区分 | `top_list_flag=0` 2,609,338 行；`mf_net_1d` 缺失 68,751 行带掩码 |
| 特征版本与来源 | 全部特征已分组、无未登记列 |

## 3. 训练与验证配置

- 基线特征：生产 clean panel（`alpha_zoo_hk`，`p0.2.v1`）剔除历史 `moneyflow_*` 列后的 1,236 个特征，另加量价上下文 `pvx`。
- 标签：`forward_return_5d`（9/11 决策日对应 9/14–9/18 五个交易日）。
- 切分：purged time split，验证 60 个交易日 + 5 日 embargo，每个变体同一随机种子。
- 模型：LightGBM（400 棵、`num_leaves=64`、`learning_rate=0.05`），逐变体独立训练。
- 两个独立 OOS 窗口：
  - 窗口 A：训练 ≤ 2026-06-11，验证 2026-06-22 ~ 2026-09-16；
  - 窗口 B：训练 ≤ 2026-03-10，验证 2026-03-18 ~ 2026-06-15。

## 4. 结果

### 4.1 OOS 验证期（RankIC / 逐日配对检验）

| 变体 | 窗口 A RankIC | Δ vs E0 (t) | 窗口 B RankIC | Δ vs E0 (t) | 两窗口同向改善 |
|---|---:|---:|---:|---:|---|
| E0 基线 | -0.0267 | — | +0.0678 | — | — |
| E1 + 标准资金流 | -0.0300 | -0.0033 (-1.30) | +0.0709 | +0.0031 (+2.22) | 否 |
| E2 + CYC | -0.0316 | -0.0050 (-1.56) | +0.0705 | +0.0027 (+1.47) | 否 |
| E3 + 主力资金 | -0.0337 | -0.0071 (**-3.10**) | +0.0711 | +0.0033 (+2.27) | 否 |
| E4 + 敢死队/龙虎榜 | -0.0374 | -0.0108 (**-5.12**) | +0.0600 | -0.0078 (-1.19) | 否（两窗口皆负） |
| E5 + 三把锁 | -0.0356 | -0.0090 (**-2.10**) | +0.0704 | +0.0026 (+0.43) | 否 |
| E6 + 来源共识 | -0.0314 | -0.0047 (-1.30) | +0.0699 | +0.0021 (+0.33) | 否 |

### 4.2 2026-09-11 之后一周（9/14–9/18，5 个交易日）

全市场截面均值 +2.32%，中位数 +0.85%（普涨周，绝对收益需扣除市场 beta 后再比较）。

| 变体 | Top20 | Top50 | Top50 超额 | Top50 胜率 |
|---|---:|---:|---:|---:|
| E0 基线 | +1.57% | +0.84% | -1.48% | 42% |
| E1 | +3.00% | +1.72% | -0.60% | 54% |
| E2 | -0.68% | +1.19% | -1.14% | 48% |
| E3 | -0.70% | -0.26% | -2.58% | 36% |
| E4 | -1.54% | -0.93% | -3.26% | 30% |
| E5 | +0.93% | +1.43% | -0.89% | 62% |
| E6（全量拼接） | +2.84% | +2.82% | **+0.49%** | 68% |
| E0 + 资金确认 boost（方案第 5 节） | +0.62% | +0.82% | -1.50% | 34% |
| 规则篮子 `three_lock_entry=1`（198 只） | — | +0.61% | -1.72% | 46% |

单特征 Top50 篮子（决策周超额）：`cyc_34` +5.44%、`cyc_13` +3.52%、`dc_net_5d` +1.62%、`main_net_5d` +1.37%。
其中 `cyc_*` 与 `close` 的截面 Spearman 相关分别为 +0.99 / +0.86 / +0.75，属于**价格水平风格**而非资金行为 alpha，不能计入资金流增益。

### 4.3 单特征层面

验证期 RankIC 为正且覆盖率 >95% 的资金特征（窗口 A）：`dc_net_5d` +0.034、`main_net_5d` +0.033、`dare_net_3d` +0.032、`mf_net_z_20d` +0.031、`main_net_3d` +0.020。
事件类特征（`top_inst_net_buy_rate` +0.078、`hm_net_rate` +0.056）覆盖率仅 1.2%，样本量不足以支撑独立信号，且其特征组 E4 在两个窗口都拖累模型。

## 5. 判定

按方案第 7 节门槛：

- **模型门槛：未通过**。没有任何变体在两个独立 OOS 时段同时改善 RankIC 或 Top-50 收益；E1/E3 在窗口 B 显著为正（t≈+2.2）而在窗口 A 为负（E3 t=-3.10），符号不稳定。
- **交易门槛：未通过**。决策周唯一跑赢基准的是 E6（+0.49pp），与单周噪声不可区分；方案第 5 节的 `capital_confirmation` boost 反而为 -1.50pp；`three_lock_entry` 规则篮子为 -1.72pp。
- **数据门槛：通过**（见第 2 节）。

因此：

1. `three_lock_entry` / `daredevil_overheat_flag` 继续作为**可解释状态**输出，不接入信号层；
2. E4（龙虎榜 + 游资）不建议进入模型：覆盖率 1.2%，两个窗口均负向；
3. 只有 `mf_*` / `main_*` / `cyc_*`（连续资金流与成本线）具备弱且不稳定的信息量，若继续投入应先在窗口 B 类型的环境（趋势市）单独验证；
4. `selection.signals.capital_flow_confirmation` 保持关闭，shadow 分数已落盘（`cn_capital_flow_shadow_scores.csv`）。

资金确认分在决策周内部同样是反向的（`capital_flow_confirmation_attribution.csv`）：7 个变体中有 5 个的
“高确认分半区”跑输“低确认分半区”（E6 为 +1.17% vs +4.47%，-3.30pp），平均 -1.20pp，
与方案第 5 节“资金确认分放大模型分数”的假设相反。

## 6. 未完成项与下一步

- Transformer 路径（方案 4.3 三种结构对比）未跑：项目 `.venv` 未安装 `torch`；补齐后命令为
  `uv run python scripts/run_cn_pipeline.py --stage transformer`（需先把 `capital_flow_locks.v1` 合入 clean panel）。
- `cyq_chips` 价格分布仅覆盖约 44% 股票，未纳入特征；补齐后可验证成本集中度增量。
- 20 日标签、扣成本（手续费/滑点/冲击）版本、行业与市值中性版本尚未跑。
- 特征未合入生产 `clean_feature_panel`（方案 P2 阶段），也尚未更新 LightGBM/Transformer manifest 的 feature allowlist。

## 7. 复现命令

```bash
uv run python scripts/build_capital_flow_locks_features.py --start 2024-08-23 --end 2026-09-18
uv run python scripts/check_capital_flow_locks.py
uv run python scripts/evaluate_capital_flow_locks_ablation.py --n-estimators 400 --workers 3 \
    --run-dir output/verification/capital_flow_locks_20260919/window_20260918
uv run python scripts/evaluate_capital_flow_locks_ablation.py --n-estimators 400 --workers 3 \
    --panel-end-date 2026-06-15 --report-suffix _window2 \
    --run-dir output/verification/capital_flow_locks_20260919/window_20260615
uv run python scripts/merge_capital_flow_locks_reports.py
uv run python -m pytest test/test_cn_capital_flow_locks.py -q
```
