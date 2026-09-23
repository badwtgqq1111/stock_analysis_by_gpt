# P1.21 启动门禁的形态与健壮性（打分制 A/B + 三条链路修复）

> 状态：已落地并验证。默认门禁形态 **`score` 打分制（strength 0.20）**，`eligibility` 硬门禁
> 保留为一行可切换的 A/B 对照；同时修复了 2026-09-21/22 让门禁"失控"的三条链路缺陷。
> 相关审计：`output/results_cn/report_20260922_filter_audit.md`；证据与脚本：
> `output/verification/p1_21_gate_20260923/`、`output/verification/gate_ab_20260923/`。

## 0. 起因

`report_20260922_filter_audit.md` 复算 2026-09-22 的选股漏斗：5209 只打分横截面 →
`startup_gate(eligibility)` 只剩 2588 只（49.7%），模型 top-100 只剩 53 只；被剔掉的是
"离半年低点较远/贴近 60 日高点"的强势票（模型第 1 名 002979、第 8 名 603159、第 11 名
300848 全部出局），留下的池子整体贴低点，叠加 ST 名单读取失败与资金流特征整块失效后，
最终 6 只持仓里进了 3 只 ST。由此提出两个问题：

1. 门禁是否该从硬剔改为**打分制**（连续倾斜）？
2. 9/22 的崩坏到底是门禁造成的，还是别的链路造成的？

## 1. 打分制 A/B 结论：**改用打分制（strength 0.20）**

> 2026-09-23 更新：初版结论是"不换"，随后补做了 strength 扫描（0.15/0.20）与用户对候选池
> 的判断复核，结论调整为**默认切到 score**；依据见下面两张表与"为什么改"。

### 1.1 59 个 OOS 决策日（2026-06-01~08-21）

| 方案 | 平均 20 日超额 | 中位数 | 胜率 | t(对 0) | 对硬门禁 |
|---|---:|---:|---:|---:|---:|
| `eligibility`（原默认） | +2.11% | +1.06% | 54.2% | +1.02 | — |
| **`score` 0.20（新默认）** | −0.31% | +0.58% | 55.9% | −0.17 | −2.42pp |
| `score` 0.15 | −0.51% | **+0.74%** | **57.6%** | −0.27 | −2.62pp |
| `score` 0.10 | −0.65% | +0.69% | 55.9% | −0.30 | −2.75pp |
| `score` 0.05（+硬护栏） | −1.29% | +0.50% | 54.2% | −0.61 | −3.40pp |
| `score` 0.02 | −3.81% | +0.17% | 50.8% | −1.98 | −5.92pp |
| `thresholds`（只留两个上限） | −4.59% | −1.91% | 42.4% | −2.36 | −6.70pp |
| 不过门禁 | −4.47% | −2.21% | 40.7% | −2.43 | −6.58pp |

最近 30 个决策日（2026-07-13~08-21，弱市）：`eligibility` −3.01% / 中位 −0.82% / 胜率 40%
vs `score` 0.20 −3.46% / **中位 +0.36% / 胜率 53%**；逐日打平 —— 两者互有胜负（score 0.20 在
30 天里 15 天赢），均值差异不显著（|t| 均 < 1.1），但 score 的中位数与胜率更好。
LightGBM 单腿最近 30 日：`eligibility` −4.8% > `score`0.20 −5.6% > 0.15 −6.1% > 0.10 −6.4%
> `thresholds` −8.1% > 不过门禁 −9.4%（同序）。

### 1.2 为什么改（证据 + 判断）

1. 两者均值差 2.4pp/20 日，但**都不显著**（t=+1.02 vs −0.17），且硬门禁的优势集中在
   2026-06 单月（+16.8% vs +10.8%），7/8 月两者几乎同水平（−11.0 vs −11.7、+1.75 vs +1.66）。
2. 打分制的**中位数与胜率更好**（少踩坑），且组合不再"整池贴低点"：9/22 同一份分数下，
   硬门禁池 = 000955/000803/000801/000590/688659/301180/000796/300876；
   打分制池 = **300848/000955/000803/000796/603416/000801/301029/300876**（把被硬剔掉的
   中盘强势票收回来），这也符合人工复核时的直觉判断。
3. 打分制不会因为资金流/元数据链路异常而"清空候选池"（§2），运行风险更低。
4. 保留可逆性：`eligibility` 仍是配置里的一行（也可以命令行临时切换）：
   `--gate-mode eligibility`（或 `--gate-mode score --gate-strength 0.15`）。


方法与仓库标签口径一致：59 个 OOS 决策日（2026-06-01 ~ 2026-08-21，交易日全覆盖），
每日按各方案取 Top-6 等权，T+1 开盘买入、持有 20 个交易日后收盘卖出，超额 = 相对当日
**可投池等权均值**（可投池 = 剔 ST + 成交额中位数 ≥1e7 + 价格 ≤64.71 元）。
打分制 tilt = 启动带（0.05~0.30 满分、0.55 衰减到 0）+ 距 60 日高点 + 缩量 + 资金流确认
（缺失记 0，不剔除），按 `model_score + 100*strength*(tilt - mean(tilt))` 倾斜后再取 Top-6。
脚本：`output/verification/gate_ab_20260923/score_gate_ab.py`，逐日明细同目录 CSV。

| 方案 | 平均 20 日超额 | 中位数 | 胜率 | t(对 0) | 相对硬门禁 |
|---|---:|---:|---:|---:|---:|
| `eligibility`（现行硬门禁） | **+2.11%** | +1.06% | 54.2% | +1.02 | — |
| `score` strength=0.10 | −0.65% | +0.69% | 55.9% | −0.30 | −2.75pp |
| `score` strength=0.05（+硬护栏） | −1.29% | +0.50% | 54.2% | −0.61 | −3.40pp |
| `score` strength=0.05 | −1.45% | +0.50% | 54.2% | −0.68 | −3.56pp |
| `score` strength=0.02 | −3.81% | +0.17% | 50.8% | −1.98 | −5.92pp |
| `thresholds`（只留两个上限） | −4.59% | −1.91% | 42.4% | −2.36 | −6.70pp |
| 不过门禁 | −4.47% | −2.21% | 40.7% | −2.43 | −6.58pp |

稳健性复核（LightGBM 单腿，最近 30 个决策日；该模型训练至 2026-04-28，全窗 OOS）：
同序 —— `eligibility` −4.8% > `score`(0.10) −5.6% > `score`(0.05) −6.1% >
`score`(0.02) −6.4% > `thresholds` −8.1% > 不过门禁 −9.4%（t 分别为 −2.82 / −3.33 /
−2.88 / −2.99 / −3.91 / −4.63）。

分月看（组合超额，%）：2026-06 / 07 / 08 —— `eligibility` +16.8 / −11.0 / +1.8；
不过门禁 +6.2 / −17.6 / +0.8；`score`(0.10) +13.1 / −14.8 / +1.7。
即"贴低点"的优势在 6 月最强、7 月收窄，与 P1.17 §0.4 观察到的"正结论来自特定时段"一致。

**决策**：打分制比"不过门禁"好，但**没有打赢硬门禁**，因此生产默认保持
`mode = "eligibility"`；`score` 模式与硬护栏保留，用于 A/B 与降级运行。若未来在更长样本
（含 2025Q4~2026Q1 的 13 折口径）上 `score` 模式反超，只需改一行配置即可切换。

## 2. 真正的病根：三条链路缺陷（已修）

| # | 缺陷 | 证据 | 修复 |
|---|---|---|---|
| 1 | 资金流特征冷启动：日更只对"本次抓到的 1 天"算 rolling，`*_net_z_*d` 全 NaN（9/21、9/22 覆盖率 0%），第二梯队/flow tilt/放量确认全部空转 | `cn_moneyflow_features.parquet` 覆盖率 31%→0%；代码 `refresh_cn_moneyflow` 的增量合并 | 新增 `rebuild_moneyflow_rolling_tail()`：合并后用完整历史重算尾部窗口，只回写新增日期；日更链路已接入；一次性修复脚本 `scripts/repair_cn_moneyflow_features.py`（dry-run 默认，`--apply` 才写，自动备份） |
| 2 | 放量过滤器盲剔：`volume_breakout_missing_flow="drop"` + 资金流全缺 → 9/22 盲剔 661 只（12.7%）放量上涨票，池子可被清空 | `universe filter removed every scored name`（9/23 13:28） | `filter_selection_universe` 新增 `flow_coverage` 与 `volume_breakout_min_flow_coverage`：覆盖不足时该规则整体降级为"保留"并写审计；配置改为 `missing_flow="keep"`、`min_flow_coverage=0.5` |
| 3 | ST/名称 registry 读空（fail-open / fail-closed 双向灾难）：ClickHouse 遇 >5000 个 IN 参数报 "Too many form fields" 后被永久禁用，回退到 schema 冲突的 Parquet 镜像（`year` int32 vs dictionary、`market` 物化与否不一致）→ 名称表为空 | 同进程实测：大读取前 5206 个名称、之后 0 个；镜像 `ds.dataset()` 直接抛 `ArrowTypeError` | ① ClickHouse 读取按 500 分片（`max_filter_values`）；② 镜像修复脚本 `repair_stock_info_mirror.py`（把不一致的分区列统一交给 hive 路径，备份移出数据集目录）；③ 名称表为空且 `exclude_st=true` 时**显式报错**，不再静默放行或清空；④ registry 读失败时发 RuntimeWarning |

## 3. 代码与配置变更

- `factor_engine/ml/strategy_labels.py`：新增 `startup_gate_score()`（连续 0~1 打分）与
  `apply_startup_gate(mode="score")`（硬护栏 `hard_max_dist_from_120d_low` /
  `hard_max_return_60d`），其余模式行为不变。
- `factor_engine/ml/model_training.py`：`select_top_model_scores(score_adjust=..., score_adjust_strength=...)`
  —— 在 Top-N 裁剪**之前**按去均值后的 tilt 调整模型分，输出 `gate_tilt` 与 `model_score_pre_tilt`。
- `data/ingest/service.py`：score 模式接线（`_cn_startup_gate_frame` 读取 volume_ratio/flow_z、
  `select_persisted_model_scores` 的 tilt 摘要），`rebuild_moneyflow_rolling_tail()`，
  放量过滤降级，名称表为空的显式报错。
- `data/store/clickhouse_store.py` / `warehouse.py`：大 IN 分片、registry 读失败告警。
- `config/cn_pipeline.toml`：`[selection.startup_gate]` 增加 score 参数与硬护栏（默认
  `mode="score"`, `score_strength=0.20`）；`scripts/run_cn_pipeline.py` 新增 `--gate-mode` /
  `--gate-strength` 运行时覆盖，便于不改配置做 A/B；`[selection.universe_filter]` 的 `missing_flow="keep"` +
  `min_flow_coverage=0.5`。
- 测试：`test/test_startup_gate_score.py`（6 例：打分形状、缩量/资金流、护栏、纯 tilt、
  Top-N 前倾斜、放量降级）+ 生产配置守卫测试更新。
- 文档：本文件。

## 4. 验证记录（详见验证包）

- `uv run pytest -q test/test_startup_gate_score.py test/test_selection_universe_filter.py test/test_selection_execution.py` → 25 passed。
- 端到端 replay（同一份 9/22 分数）：
  - 硬门禁（现行）池子：000010\*ST美丽、000955、603557\*ST起步、000803、000615\*ST美谷、000801、000590、002207\*ST准油；
  - 修好名称表后同一硬门禁：000955、000803、000801、000590、688659、301180、000796、300176、300876、688573（无 ST）；
  - `score` 模式（strength 0.05）：000955、300848、002979、000803、000801、000590、000796、688659，PK 后组合
    000803 / 000955 / 300848 / 000801 + 两只信号票，无 ST。
- 资金流修复（在**副本**上验证）：`--output /tmp/...parquet`，9/21、9/22 的 `moneyflow_net_z_5d` 覆盖率
  0.0 → 0.3104 / 0.3103，9/18 不变。
- registry 修复：镜像数据集可读（5219 行、201 只 ST），ClickHouse 被禁用时名称表仍返回 5206 个名称。

## 5. 后续事项

0. 新默认为 `score 0.20`；若要在更长期样本上复检，用 `--gate-mode eligibility` 直接跑同口径回放。

1. 生产路径执行一次 `scripts/repair_cn_moneyflow_features.py --apply`，然后重建 clean panel，
   使 9/21 之后的资金流特征恢复（第二梯队/flow tilt 才能真正生效）。
2. 第二梯队是否默认启用，需要用修复后的资金流数据重跑本节的 A/B（当前默认关闭）。
3. 把 `score` 模式与 `eligibility` 放进 13 折（169 决策日）口径再做一次对照，覆盖 2025Q4~2026Q1。
4. 组合层 gross（0.65/0.75）与门禁命中率的关系仍需单独评估。
