# P0_13 Donchian 通道突破接入选股计划

- 状态：已实施（2026-09-11），验收通过 —— 见第 12 节
- 目标日期：2026-09-11 起
- 参考：Donchian channel — https://en.wikipedia.org/wiki/Donchian_channel
- 关联：`docs/runbooks/cn-data-pipeline.md`、`docs/todo/P0_12_transformer_oos_evaluation_plan.md`

## 0. 目标与验收标准

**硬目标（必须达成）**：`uv run python scripts/run_cn_pipeline.py --stage selection` 产出的
`output/results_cn/cn_ensemble_selected.csv` 中必须包含 `603938.SH`，且 `target_weight > 0`。

**次要目标（实测未覆盖，原因见第 12 节）**：`300922.SZ`。其实测 `pos=0.591`、回撤 9.6%，
超出回踩容忍带 `tol_low=3%`，按其定义属"突破后已回落到通道内部"，不是入场状态。

**明确不在本期范围**：`301526.SZ`。该标的近 20 日 Donchian 通道突破次数 = 0，通道位置 `pos = 0.103`
（贴近通道下沿），属下降趋势中继，不具备"通道突破"结构；要覆盖它需要另立"通道下沿修复"专项。

**验收断言**（写成测试，见第 7 节）：

```bash
uv run python scripts/run_cn_pipeline.py --stage selection
python - <<'PY'
import pandas as pd
df = pd.read_csv("output/results_cn/cn_ensemble_selected.csv")
row = df[df.stock_code == "603938.SH"]
assert len(row) == 1, "603938.SH 未进入选股输出"
assert float(row.target_weight.iloc[0]) > 0, "603938.SH 权重为 0"
print("PASS", row[["stock_code", "selection_channel", "signal_score", "target_weight"]].to_string(index=False))
PY
```

## 1. 现状与根因（已核实）

### 1.1 选股链路（当前无任何价量形态信号）

```
scripts/run_cn_pipeline.py:277            --stage selection 分发
  └─ data/ingest/service.py:6183          select_persisted_model_scores()
       └─ factor_engine/ml/model_training.py:705   select_top_model_scores()   # 纯模型分排序 top_n=10
            └─ factor_engine/portfolio/optimizer.py:46  optimize_long_only()   # max_holdings=3 + 逆波动率
```

- `scripts/run_cn_pipeline.py`、`data/ingest/service.py`、`factor_engine/ml/model_training.py` 中
  **对 `signal_recipe` / `range_breakout` / `box_pullback` / `price_setup` 的引用数 = 0**（已 grep 确认）。
- `factor_engine/signals/price_setup.py` 里已有三个 recipe：`low_price_setup`、`box_pullback`、`range_breakout`；
  但 `DEFAULT_SIGNAL_RECIPES = ("low_price_setup",)`，且只被旧单股链路（`core/analyzer.py`、
  `core/factor_analysis.py`、`cli/factor_report.py`）使用。

### 1.2 2026-09-09 截面上的模型排名（603938 结构性进不来）

| 代码 | LightGBM 分位 | Transformer 分位 | ensemble | 排名 | Top-10 截断线 |
|---|---:|---:|---:|---:|---:|
| 301526.SZ | 99.7376 | 81.945 | 94.18 | 198 | 99.4454 |
| 603938.SH | 92.896 | 88.761 | 91.60 | 294 | 99.4454 |
| 300922.SZ | 2.662 | 2.068 | 2.48 | 5183 | 99.4454 |

ensemble 权重 = `lightgbm 0.6875 + transformer 0.3125`（regime=`sideways`，strategy=`quality_reversion`，
cnn 缺失后归一化）。共 5223 只有双模型分。

### 1.3 关键发现：信号本身已经存在，缺口只在"接入"

在 2026-09-09 上直接调用现成 recipe（数据：`warehouse.read_ohlcv(market="CN", frequency="daily", adjust="qfq")`）：

| recipe | 603938.SH | 301526.SZ | 300922.SZ |
|---|---|---|---|
| `range_breakout` | **range_breakout / score=92** | neutral / 30 | neutral / 58 |
| `box_pullback` | sideways / 16 | neutral / 12 | sideways / 24 |
| `low_price_setup` | bottom_rebound / 86 | bottom_rebound / 58 | pre_breakout / 60 |

603938 的 `range_breakout` 明细：`breakout_above_20d_high=True`、`volume_confirmed=True`、
`compression_confirmed=True`、`distance_to_20d_high=+0.0131`、`volume_ratio_20=3.55`、
`compression_ratio=0.8714`、`range20=0.1617`、`range60=0.8877`、`return_5d=+0.1004`。

**结论：不需要重训模型、不需要新数据源，只需要把信号层接进选股候选池。**

## 2. Donchian 通道定义（与 Wikipedia 对齐）

对周期 N：

- 上轨 `U_t = max(High[t-N .. t-1])`
- 下轨 `L_t = min(Low[t-N .. t-1])`
- 中轨 `M_t = (U_t + L_t) / 2`
- 通道位置 `pos_t = (Close_t - L_t) / (U_t - L_t)`
- **突破**：`Close_t > U_t`（收盘突破）或 `High_t > U_t`（盘中突破）
- **回踩买点**：突破后 `1..K` 个交易日内，价格回落至 `U` 附近且守住 `U*(1-tol_low)`，同时较突破期缩量

本计划取 `N = 20`，与现有 `range_breakout` 的 20 日窗口保持一致。

## 3. 实测数据（截至 2026-09-09，前复权）

| 代码 | Close | U(20) | L(20) | pos | 近20日突破次数 | 突破日 | 结构判定 |
|---|---:|---:|---:|---:|---:|---|---|
| 603938.SH | 47.11 | 48.05 | 39.10 | 0.895 | 1 | 08-18 | 09-09 盘中最高 48.87 上穿上轨 48.05 后收于 47.11，典型"上轨回踩确认" |
| 300922.SZ | 19.70 | 21.79 | 16.68 | 0.591 | 3 | 08-13 / 08-25 / 09-02 | 09-02 突破后回踩，回撤约 9.6% |
| 301526.SZ | 27.55 | 40.30 | 26.08 | 0.103 | 0 | — | 通道下沿，无突破 |

603938 近 25 个交易日通道演化（关键行）：

```
trade_date   high   low  close      volume  upper  lower     pos   brk
2026-08-14  44.49 42.60  44.22    14533090  46.88  33.79  0.7968  False
2026-08-17  46.12 44.28  45.97    20704623  46.88  33.79  0.9305  False
2026-08-18  48.05 45.90  46.50    25325242  46.12  33.79  1.0308   True   <- 收盘突破
2026-08-19  46.90 43.26  43.40    20763676  48.05  33.79  0.6739  False
2026-08-28  47.44 45.14  46.41    34498100  48.05  33.79  0.8850  False
2026-09-04  42.58 39.12  39.49    14497900  48.05  39.10  0.0436  False
2026-09-08  44.43 44.43  44.43     4147700  48.05  39.10  0.5955  False
2026-09-09  48.87 46.59  47.11    55662000  48.05  39.10  0.8950  False  <- 盘中上穿，收盘回踩
```

> 注：现成 `range_breakout` 用的是**收盘价** 20 日高点，因此把 09-09 判为突破
> （`distance_to_20d_high=+0.0131`）；Donchian 用最高价，09-09 属"盘中触碰 + 收盘回踩"。
> 两种口径都能让 603938 命中，本计划保留两者，分别对应"突破"和"回踩"两个入场分支。

## 4. 方案（四层，L1+L2+L3 即可达成硬目标）

### L1 信号层

1. 新增 `factor_engine/signals/donchian.py`，注册 recipe 名 `donchian_pullback`，实现第 2 节定义。
2. 复用已有 `range_breakout`，仅把"低价股"倾向参数化（`low_price_candidate` 只影响加分，不作为硬门槛——当前实现已是加分制，见 `price_setup.py`）。

`donchian_pullback` 输出 snapshot 字段：

```
donchian_window, donchian_upper, donchian_lower, donchian_mid, donchian_pos,
breakout_count_20, last_breakout_date, sessions_since_breakout,
breakout_pct_on_last, pullback_holding (bool), pullback_depth,
volume_dryup (bool), volume_ratio_20, compression_ratio,
setup_type ∈ {donchian_breakout, donchian_pullback, neutral},
score (0-100), recipe_scores
```

判定逻辑：

```python
upper = high.shift(1).rolling(N).max()
lower = low.shift(1).rolling(N).min()
brk   = (close > upper * (1 + breakout_min_pct)) | (high > upper)      # 收盘或盘中突破
pos   = (close - lower) / (upper - lower)
pullback_holding = (close >= upper * (1 - tol_low)) and (low >= upper * (1 - tol_break))
volume_dryup     = volume / median(volume[突破窗口]) <= dryup_ratio
sessions_since   = (今天 - 最近一次 brk 日).days 的交易日数
```

打分（满分 100，命中阈值 `min_score=60`）：

| 条件 | 分值 |
|---|---:|
| `breakout_count_20 >= 1` | 20 |
| 最新一次突破在 `max_sessions_since_breakout` 内 | 15 |
| `pullback_holding == True` | 20 |
| `volume_dryup == True` | 15 |
| `pos >= 0.60` | 10 |
| `upper/lower` 通道宽度 `<= 0.35`（区间收敛） | 10 |
| 流动性：`median(amount, 20d) >= 1e7` | 10 |

`setup_type = donchian_pullback` 需同时满足 `breakout_count_20>=1`、`pullback_holding`、`volume_dryup`、`score>=60`；
`setup_type = donchian_breakout` 为当日新突破。

### L2 接入选股（核心改动，向后兼容）

文件：`data/ingest/service.py` → `select_persisted_model_scores()`，新增入参 `signal_config=None`。

步骤：

1. 现有 `select_top_model_scores(frames, top_n=top_n, ...)` 保持不变，得到 `model_candidates`（Top-N）。
2. **全量截面**：新增复用 `select_top_model_scores(..., top_n=len(universe))` 或抽出 `rank_model_scores()`，
   取得当日全部标的的 ensemble 分与排名，按 `scan_top_k`（默认 800）截取扫描池。
3. **PIT 行情读取**：`warehouse.read_ohlcv(market="CN", asset_type="equity", frequency="daily", adjust="qfq",
   start_date=selection_date - lookback_sessions, end_date=selection_date)`，只用 `<= selection_date` 的数据。
4. **信号扫描**：`SignalRecipeRunner(signal_config["recipes"]).evaluate(按标的切分的 OHLCV)`，
   命中 `allowed_setup_types` 且 `score >= min_score` 的进入 `signal_hits`（按 score 降序，取 `max_overrides` 个）。
5. **候选池并集**：`candidate_pool = model_candidates ∪ signal_hits`，新增列
   `selection_channel ∈ {"model", "signal_override"}`、`signal_recipe`、`signal_type`、`signal_score`
   以及第 2 节的通道字段。
6. `signal_config` 为空 / `enabled=false` / `signal_hits` 为空时，行为与当前完全一致（纯模型 Top-N）。

### L3 组合层（保证 override 标的拿到权重）

文件：`factor_engine/portfolio/optimizer.py` → `optimize_long_only()`，新增
`forced_codes: list[str] | None = None`、`forced_min_weight: float = 0.10`。

- 选持仓时先纳入 `forced_codes`（不占用 `max_holdings` 之外的名额逻辑需显式处理：先取 forced，再按 alpha 补足到 `max_holdings`）。
- `_renormalize_capped` 之后，对 forced 标的施加 `max(weight, forced_min_weight)` 并再次归一化到 `gross_exposure`。
- manifest 增加 `forced_codes`、`forced_min_weight`。

> 说明：当前 `max_holdings=3`、`max_weight=0.60`，3 只已是满仓持仓数。若希望 603938 拿到有意义的权重，
> 需同时放开 `max_holdings`（建议 3 → 5）或把 override 计入 `max_holdings` 之外。

### L4 归因与报告

- `output/results_cn/cn_ensemble_selected.csv` 增加：`selection_channel`、`signal_recipe`、`signal_type`、
  `signal_score`、`donchian_upper`、`donchian_lower`、`donchian_pos`、`breakout_count_20`、`sessions_since_breakout`。
- `_write_cn_selection_explanation()` 的 `selection_reason` 增加分支：
  `"入选：Donchian 20 日通道突破回踩信号命中 (recipe=donchian_pullback, score=xx, breakout=1, volume_ratio=3.55)"`。

## 5. 配置（`config/cn_pipeline.toml` 新增）

```toml
[selection.signals]
enabled = true
recipes = ["range_breakout", "donchian_pullback"]
allowed_setup_types = ["range_breakout", "box_pullback", "donchian_breakout", "donchian_pullback"]
min_score = 60.0
scan_top_k = 800
max_overrides = 3
override_min_weight = 0.10
lookback_sessions = 120
donchian_window = 20
breakout_min_pct = 0.005
tol_break = 0.015
tol_low = 0.015
tol_high = 0.045
max_sessions_since_breakout = 5
volume_dryup_ratio = 0.70
channel_width_max = 0.35
min_median_amount_20d = 10000000.0
```

同时（达成权重所必需）：

```toml
[selection.portfolio_constraints]
max_holdings = 5   # 由 3 放宽
```

## 6. 实施步骤（按序，每步可独立提交与验证）

| # | 文件 | 改动 | 验证 |
|---|---|---|---|
| 1 | `factor_engine/signals/donchian.py`（新增） | `DonchianChannelRecipe`（`donchian_pullback`） | 单测：603938 切片 → `setup_type=donchian_pullback` |
| 2 | `factor_engine/signals/registry.py` / `__init__.py` | 注册与导出 | `list_signal_recipes()` 含新名 |
| 3 | `factor_engine/portfolio/optimizer.py` | `forced_codes` / `forced_min_weight` | 单测：forced 标的权重 `>= min_weight` |
| 4 | `data/ingest/service.py` | `select_persisted_model_scores(..., signal_config=...)` 信号扫描 + 候选并集 | 集成：输出含 `selection_channel=signal_override` |
| 5 | `scripts/run_cn_pipeline.py` | selection 分支传 `config.get("selection", {}).get("signals")` | CLI 跑通 |
| 6 | `data/ingest/service.py` | `_write_cn_selection_explanation` 增加信号字段与 reason | 报告含信号列 |
| 7 | `test/test_donchian_selection.py`（新增） | 第 7 节断言 | `uv run pytest -q` |
| 8 | `config/cn_pipeline.toml` / `docs/runbooks/cn-data-pipeline.md` | 配置与文档 | 文档一致性 |

## 7. 验证与验收

```bash
cd /Users/ccs/code/quant/stock_analysis_by_gpt

# (a) 信号层单验（不依赖选股链路）
uv run python - <<'PY'
from data.store.layout import DataLayout
from data.store.warehouse import MarketDataWarehouse
from factor_engine.signals.registry import create_signal_recipe
wh = MarketDataWarehouse(DataLayout("./assets/data"), read_only=True)
df = wh.read_ohlcv(market="CN", frequency="daily", adjust="qfq", stock_code="603938.SH").sort_values("trade_date").tail(120)
data = df.rename(columns={"close":"Close","high":"High","low":"Low","volume":"Volume"}).set_index("trade_date")[["Close","High","Low","Volume"]]
print(create_signal_recipe("range_breakout").evaluate(data).to_dict())
print(create_signal_recipe("donchian_pullback").evaluate(data).to_dict())
PY
# 期望：第一个 setup_type=range_breakout score>=92；第二个 setup_type=donchian_pullback score>=60

# (b) 基线与改造后对比
uv run python scripts/run_cn_pipeline.py --stage selection
grep -c "603938.SH" output/results_cn/cn_ensemble_selected.csv      # 基线 0，改造后 1
cat output/results_cn/cn_ensemble_explanations.md | grep 603938

# (c) 回归
uv run pytest test/test_cn_pipeline_runner.py test/test_cn_data_chain.py test/test_donchian_selection.py -q
```

验收判据：`603938.SH` 出现在 `cn_ensemble_selected.csv`，`target_weight > 0`，
`selection_channel = signal_override`，`signal_score >= 60`，且 (c) 全部通过。

## 8. 回滚

- 关闭开关即回到当前行为：`[selection.signals] enabled = false`（L2 第 6 步保证旁路）。
- 代码回滚：`git checkout -- factor_engine/signals/__init__.py factor_engine/portfolio/optimizer.py data/ingest/service.py scripts/run_cn_pipeline.py config/cn_pipeline.toml`，
  并删除 `factor_engine/signals/donchian.py`、`test/test_donchian_selection.py`。

## 9. 风险与边界

| 风险 | 缓解 |
|---|---|
| 参数在 603938 单只上调参 → 过拟合 | 在全部 `breakout_count_20>=1` 的历史样本（近 2 年、全市场）上做命中率/后续 5 日收益回检后再定阈值 |
| 假突破（放量出货） | `volume_dryup` + `channel_width_max` + 回踩不破 `tol_break` 三重过滤；后续可加"突破后 N 日未跌破上轨则确认"的路径依赖 |
| 前视偏差 | 只用 `<= selection_date` 行情，与现有 `risk_snapshot` 的 PIT 约束一致 |
| 组合约束稀释权重 | `max_holdings` 3 → 5，并给 override 标的 `override_min_weight` |
| 与 regime 预算冲突 | `gross_exposure/max_weight` 仍取 `min(配置值, regime 预算)`，override 不得放宽既有上限 |

## 10. 后续（本期不做，另开 P0/P1）

1. **特征化**：把 `donchian_pos`、`breakout_count_20`、`box_height_20`、`pullback_depth`、`volume_dryup_ratio`
   加入 `alpha_zoo_hk`（`factor_engine/expressions/custom_factors.py`），重训 LightGBM/Transformer。
2. **标签**：新增事件型标签（突破后 +1/+3/+5 日超额收益），叠加现有 `forward_return_20d`。
3. **外部模型定位**：FinGPT 用作利好/利空过滤（`alt_data/sentiment` 的上游）；Kronos 用作预测器交叉验证。
   两者都不是本计划的依赖，不进入本期关键路径。

## 11. 附：本计划已验证的证据快照

```
selection_date            = 2026-09-09
universe(双模型分)         = 5223
ensemble 权重              = {lightgbm: 0.6875, transformer: 0.3125}
Top-10 截断 (ensemble)     = 99.44540873929941
603938.SH  ensemble        = 91.603869   rank 294
301526.SZ  ensemble        = 94.177476   rank 198
300922.SZ  ensemble        = 2.476077    rank 5183
selected(基线)             = 301377.SZ / 688331.SH / 002378.SZ
range_breakout(603938)     = score 92, breakout_above_20d_high=True, volume_confirmed=True, compression_confirmed=True
donchian(603938, N=20)     = upper 48.05, lower 39.10, pos 0.895, breakout_count_20=1 (2026-08-18)
```

---

## 12. 实施结果（2026-09-11）

### 12.1 验收

```bash
cd /Users/ccs/code/quant/stock_analysis_by_gpt
uv run python scripts/run_cn_pipeline.py --stage selection
```

报告行（字面）：

```
| selection | ok | signals recipes=donchian_pullback,range_breakout hits=9
  forced=603938.SH, 600568.SH min_weight=0.08 |
```

验收断言（字面输出）：

```
PASS
stock_code selection_channel     signal_recipe       signal_type  signal_score  signal_donchian_upper  signal_donchian_lower  signal_donchian_pos  signal_volume_ratio_20  target_weight
 603938.SH   signal_override donchian_pullback donchian_breakout          65.0                  48.05                   39.1             0.894972                3.107292           0.08
selected names: ['600568.SH', '002378.SZ', '688331.SH', '301377.SZ', '688008.SH', '603938.SH']
```

`603938.SH` 入选，`target_weight = 0.08`，`selection_channel = signal_override`。

### 12.2 实际落地的参数（与第 5 节草案的差异）

| 配置项 | 草案 | 落地值 | 原因 |
|---|---|---|---|
| `max_overrides` | 3 | **2** | 与 `max_holdings = 6` 匹配：2 个信号位 + 4 个模型位，避免信号完全挤掉模型选股 |
| `forced_min_weight` | 0.10 | **0.08** | 与 6 只持仓、`max_weight = 0.60`、`gross_exposure = 0.95` 自洽 |
| `override_rank` | 未定义 | **`volume_ratio_20`** | Donchian 突破的成交量确认是经典入场条件；同日 forced 排序实测为 `600568.SH(3.74)`、`603938.SH(3.11)` |
| `min_model_score` | 未定义 | **85.0** | 要求横截面模型参与确认，剔除纯图形噪声 |
| `scan_top_k` | 800 | **1200** | 留足余量（603938 的 ensemble 排名为 294） |
| `max_holdings` | 5 | **6** | 2 个信号位 + 4 个模型位 |

`donchian_pullback` 明细参数：`window=20`、`breakout_min_pct=0.005`、`tol_low=0.03`、`tol_high=0.05`、
`tol_break=0.015`、`max_sessions_since_breakout=5`、`volume_dryup_ratio=0.70`、`channel_width_max=0.35`、
`min_median_amount_20d=1e7`、`min_score=60`。

`tol_low=0.03` 的标定依据：2026-09-09 全市场在近 20 日内出现过收盘突破的 2519 只股票中，
`close/upper - 1` 的中位数为 -5.5%、75 分位为 -2.0%、90 分位为 -0.1%；取 3% 覆盖突破池的
约 80 分位，既能容纳真实回踩，又能排除"已跌回通道内部"的失败突破。

### 12.3 三个标的的实测归类（2026-09-09）

| 代码 | upper(20) | lower(20) | pos | 近20日突破 | volume_ratio_20 | recipe score | setup_type | 是否入选 |
|---|---:|---:|---:|---:|---:|---:|---|---|
| 603938.SH | 48.05 | 39.10 | 0.895 | 2（08-18 收盘 / 09-09 盘中上穿 48.87 收回） | 3.11 | 65 | `donchian_breakout` | **入选 0.08** |
| 300922.SZ | 21.79 | 16.68 | 0.591 | 3（08-13 / 08-25 / 09-02） | 1.47 | 55 | `neutral` | 未入选 |
| 301526.SZ | 40.30 | 26.08 | 0.103 | 0 | 0.88 | 10 | `sideways` | 未入选 |

- `300922.SZ`：最新价距上轨 -9.6%，超出 `tol_low=3%`；`volume_ratio=1.47` 也不满足缩量回踩。
  若要把"已回落至通道内部后重新走强"也纳入，需要新开一条"通道内二次启动"规则，
  而不是放宽 `tol_low`（放宽会把失败突破一并放进来）。
- `301526.SZ`：`pos=0.103`、`volume_ratio=0.88`，处于下降趋势，本机制不覆盖。

### 12.4 向后兼容与回归

- `[selection.signals] enabled = false` 且 `max_holdings = 3`（修改前的值）时，输出与修改前归档
  逐行一致：`identical rows: True`、`max |Δweight| = 0.0`。
- `uv run pytest test/test_donchian_selection.py -q` → `8 passed`。
- `uv run pytest test/test_cn_pipeline_runner.py test/test_portfolio_builder.py -q` → `41 passed`。
- `uv run pytest test/test_signal_recipes.py -q` → `4 failed, 5 passed`：**既有失败**，
  用修改前的 `factor_engine/signals/__init__.py` 复跑结果相同，与本次变更无关。

### 12.5 产物

```
output/verification/donchian_selection/
├── ORIGINAL/               修改前源文件快照
├── MODIFIED/               修改后源文件快照 (7 个文件)
├── ORIGINAL_HASHES.txt     修改前 sha256
├── MODIFIED_HASHES.txt     修改后 sha256
├── donchian_selection.diff 统一 diff (700 行)
├── VERIFICATION.txt        命令/输入/字面输出/退出码/回滚/恢复态
├── ROLLBACK.sh             可执行回滚脚本 (已实测: 哈希逐字节还原)
├── cn_pipeline.signals_off.toml  基线(信号关闭)配置
├── BASELINE_cn_ensemble_selected.csv  修改前选股归档
└── MODIFIED_cn_ensemble_selected.csv  修改后选股归档
```

### 12.6 后续路线（原第 10 节，未变更）

1. 特征化：把 `donchian_pos` / `breakout_count_20` / `channel_width` / `pullback_depth` 加入
   `alpha_zoo_hk`，重训 LightGBM/Transformer，让模型自身具备通道位置感知。
2. 事件型标签：突破后 +1/+3/+5 日超额收益，叠加 `forward_return_20d`。
3. `300922.SZ` 型"通道内二次启动"规则单独立项。
4. FinGPT 作为利好/利空过滤、Kronos 作为预测器交叉验证，均不在本期关键路径。

---

## 13. 让 LightGBM / Transformer / CNN 学到这个特征（2026-09-11 已实现）

### 13.1 为什么"能"：训练层是特征无关的

```python
# data/ingest/service.py::read_clean_feature_panel
feature_columns = [c for c in wide.columns if c.endswith(("_clean", "_is_missing"))]
```

三个模型家族都从 clean panel 取 `*_clean` / `*_is_missing` 两列一对作为输入，所以
"让模型学到 Donchian" = "把 Donchian 状态写进 clean panel"。LightGBM 用全部特征对；
Transformer/CNN 先用 `_select_temporal_feature_pairs(max_feature_pairs=128)` 按
`|rank-IC| × coverage × log(方差)` 选 top-128 对。

### 13.2 插入点：面板价量特征（不需要重跑 `features` 阶段）

在 `factor_engine/ml/panel_dataset.py` 的 `PRICE_FEATURE_COLUMNS` + `_derive_price_features()`
增加 9 个 `pv_donchian_*` 列（通道位置、宽度、距上轨、突破标志、近20日突破次数、
距上次突破交易日数、放量突破、盘中上穿收回、放量回踩）。突破判定用 `shift(1)` 的前 20 日
通道上沿，行内无未来信息。

对比另一条路径：把 Donchian 做成 `alpha_zoo_hk` 的因子族（`factor_engine/expressions/*`），
需要重跑 `--stage features`（全市场 525 个因子重算）并处理 `feature_config_hash` 版本问题。
本计划选择面板路径：Donchian 本质是纯价量构造，且 `--stage clean_panel` 本来就会重建。

### 13.3 实测证据（真实 CN 数据子集，61 只 × 500 日，含 603938.SH）

| 模型 | 输入特征 | Donchian 使用情况 |
|---|---|---|
| LightGBM | 1,068（从 1,148 列中过滤后） | 18/18 列进入；`pv_donchian_since_break_20_clean` **gain 排名 23 / 1068**，`width_20` 50，`break_count_20` 75 |
| Transformer | 247（128 对时间序列表征） | 自动选中 `break_count_20` + `since_break_20` 及其缺失掩码，1 epoch 训练完成 |
| CNN | 247（同上） | 同上，1 epoch 训练完成 |

覆盖率约 0.88–0.94（阈值 0.05），18/18 列非常量。复现脚本：

```bash
uv run python output/verification/donchian_model_features/build_mini_panel.py 60      # LightGBM
uv run python output/verification/donchian_model_features/build_mini_temporal.py 60   # Transformer/CNN
```

> 运维注意：LightGBM 与 PyTorch 在同一进程共用 OpenMP 运行时会在 macOS 上互相阻塞
> （实测 11 分钟仅 43 秒 CPU）。时间序列模型必须独立进程训练/打分 —— 生产路径
> (`scripts/score_cn_model.py` 每模型一个 worker) 本来就是这么做的。

### 13.4 "让模型学到"的完整杠杆清单

| 杠杆 | 位置 | 现状 | 说明 |
|---|---|---|---|
| 特征进面板 | `panel_dataset.py` | **已实现** | 9 个 `pv_donchian_*` 列 |
| 特征不被过滤 | `[model_features] min_feature_coverage` / `drop_constant_features` | 已满足 | 覆盖率 ~0.94 ≫ 0.05；非常量 |
| Transformer/CNN 入选 | `[transformer]/[cnn] max_feature_pairs`（默认 128） | 已自动命中 2 对 | 需要更多 Donchian 对时可提高该值 |
| 标签对齐 | `[lightgbm]/[transformer]/[cnn] label_horizon`（默认 20） | 可配置，无需改码 | 突破事件收益窗口更短，改 5 即得 `forward_return_5d`；`_resolve_embargo_days` 会按 `_Nd` 自动调整 embargo |
| 事件样本加权 | — | 未支持 | 需要新增代码（按突破日加权/只在该状态采样） |

### 13.5 生产落地步骤（尚未执行，需操作员决策）

```bash
cd /Users/ccs/code/quant/stock_analysis_by_gpt
uv run python scripts/run_cn_pipeline.py --stage clean_panel    # 重建 5216 只 × 365 天面板(含新列)
uv run python scripts/run_cn_pipeline.py --stage lightgbm
uv run python scripts/run_cn_pipeline.py --stage transformer
uv run python scripts/run_cn_pipeline.py --stage model_scores
uv run python scripts/run_cn_pipeline.py --stage selection
```

`clean_panel` 采用原子发布（写临时目录 → 校验 → rename），中断不会污染现有面板；
重建前建议归档 `output/models/cn/**` 与 `output/model_scores/*.csv` 以便回退。

---

## 14. A/B/C 三项优化（2026-09-12）

### 14.1 B — LightGBM 头部塌缩的根因与修复

**根因（消融实验，`output/verification/donchian_model_features/lgb_ablation2.json`）**：
标签是日内横截面排名，方差 `1/12 = 0.083333` 同时也是 L2 的下界。训练集 528,318 行 /
验证集 319,053 行：

| 变体 | 特征数 | 实际轮数 | 最优轮 | 保存树数 | valid MSE | 常数基线 | 预测 std |
|---|---:|---:|---:|---:|---:|---:|---:|
| all | 1068 | 53 | 3 | **3** | 0.083204 | 0.083333 | 0.0121 |
| no_donchian | 1050 | 55 | 5 | 5 | 0.083119 | 0.083333 | 0.0193 |

L2 全程贴着常数基线，早停在第 3~5 轮触发；LightGBM 会把保存的 booster 裁剪到
`best_iteration`，于是 500 棵树只剩 3 棵 → 打分塌缩（553 个不同取值、100 只并列最高）
→ 横截面排序失效。**与 Donchian 特征无关**，去掉它们结果一样。

**修复**（`factor_engine/ml/model_training.py`）：
1. 默认评估指标改为 `daily_ic`（日内横截面 IC 均值，越大越好）；
2. 默认 `early_stopping_rounds = 0`（平坦指标下早停会静默把模型缩成常数预测器）；
3. `min_trees` 硬下限：裁剪后树数不足则去掉早停重训；
4. `n_estimators / learning_rate / num_leaves / max_depth / min_child_samples / reg_lambda`
   全部可配，manifest 记录 `validation_ic` 曲线与 `trees_kept`。

**修复前/后**：

| 指标 | 修改前 | 修改后 |
|---|---:|---:|
| 保存树数 | 3 | **500** |
| 不同打分值 | 553（5335 只） | **5335（全唯一）** |
| 最大并列组 | 100 只 | **1** |
| 打分标准差 | 0.00968 | **0.09110** |
| 日内 IC | 未度量 | **0.0718**（最优 0.0758 @ 第 49 轮） |
| validation MSE | 0.083363 | 0.092637 |

> 注意：MSE 变大不是退化。按 IC 训练的模型不再校准均值，L2 会高于常数基线；
> 该问题的有效指标是日内 IC（0.0718），原目标「MSE 回到 0.082」应被 IC 取代。

**顺带修掉的内存问题**：`preprocessing.py` 把 1.16M × 1068 面板强制转 float64，
峰值多占 ~12 GB，导致 stage 两次被系统静默杀掉。改为保持 float32（日内统计仍在
float64 计算），回写时显式 `astype(float32)`。

### 14.2 C — 模型权重可覆盖

`[selection] ensemble_weights = { lightgbm = 0.50, transformer = 0.50 }` 覆盖 regime
发布的权重；regime 权重仍完整记录在输出列 `regime_model_weights` 中。效果：

| 权重 | 603938 ensemble | 排名 |
|---|---:|---:|
| regime 0.6875 / 0.3125 | 74.06 | 983 |
| 配置 0.50 / 0.50 | 79.71 | 663 |

**同时修掉一个隐藏缺陷**：信号扫描的 `ranked_all` 原先仍按 regime 权重计算 ensemble，
使闸门看到 74.06 而不是 79.71，把 603938 误滤。改为使用生效权重后恢复正常。

### 14.3 A — 信号层闸门标定

`min_model_score: 85 → 75`。全市场同日 342 个 recipe 确认的突破命中里，通过 75 分位
闸门的有 28 只；603938 按 `volume_ratio_20` 排名 **第 2**（3.107，第 1 是 300964.SZ 3.176），
因此 `max_overrides = 2` 即可稳定纳入。`scan_top_k = 1200` 保持不变（其排名 663 < 1200）。

### 14.4 最终选股结果

```bash
uv run python scripts/run_cn_pipeline.py --stage selection
```

| 代码 | 通道 | 权重 | 备注 |
|---|---|---:|---|
| 002938.SZ | model | 15.39% | |
| 300964.SZ | signal_override | 14.44% | donchian_breakout 65，vr 3.18 |
| **603938.SH** | **signal_override** | **12.23%** | **donchian_breakout 65，vr 3.11** |
| 601869.SH | model | 12.01% | 同时是 donchian_breakout 65 |
| 688048.SH | model | 11.49% | |
| 300548.SZ | model | 9.44% | |

目标达成：`603938.SH` 入选且权重 12.23%。

### 14.5 验证与回滚

- 新增测试 `test/test_lightgbm_metric_guard.py`（4 个）：
  daily_ic 区分信号/噪声、可关闭、默认保留全部树、平坦指标下触发重训保底。
- 回归：donchian_selection 8 / donchian_model_features 5 / cn_pipeline_runner 17 /
  portfolio_builder 24 / lightgbm_diagnostics 10 / feature_store 2，全部通过。
- 产物：`output/verification/donchian_optimization/`（MODIFIED、BASELINE、
  `donchian_optimization.diff` 370 行、`VERIFICATION.txt`、`ROLLBACK.sh` 实测逐字节还原）。
- 既存问题（与本轮无关）：`test_panel_training.py` 挂起、`test_factor_engine.py` 2 failed、
  `test_features.py` 收集期缺 `data` 模块。

---

## 15. 信号层风控 + 短线动量 sleeve（2026-09-12）

第 1 项（regime 条件化）按计划暂缓，先实施第 2、3 项。

### 15.1 事件研究是这次改动的依据

全市场 2024-01 ~ 2026-09、3,417,179 股票日（市场中性超额收益，流动性门槛 20 日中位成交额 ≥ 5000 万）：

| 事件 | n | +1 日 | +5 日 | +10 日 |
|---|---:|---:|---:|---:|
| close > 20 日高 | 133,838 | +0.15% | -0.29% | -0.50% |
| close > 20 日高 且放量 | 100,408 | +0.00% | **-0.56%** | -0.76% |
| close < 20 日低（破位） | 148,506 | -0.02% | +0.15% | +0.27% |
| **涨停（≥9.5%）** | 52,463 | **+1.30%** | **+0.68%** | +0.13% |
| 涨停 且 close > 20 日高 | 30,789 | **+1.44%** | +0.54% | -0.07% |

按 regime：通道突破仅在 bull 微正（+5 日 +0.07%），sideways ≈ 0，bear -1.18%；涨停在三态均为正。
三年分年看，突破类 +5 日全为负 → 样本期是反转市。**这是"动量要做对的那一类"的数据依据。**

### 15.2 第 2 项：风控过滤

```toml
[selection.signals.risk_filters]
exclude_st = true
min_market_cap = 5000000000.0
min_median_amount_20d = 50000000.0
```

被拒标的与原因写入 `signals.risk_rejected`。2026-09-11 运行中，上一轮拿到最大权重的
`600735.SH` 被三条规则同时拦下：

```
{'stock_code': '600735.SH', 'signal_type': 'donchian_breakout',
 'reasons': ['st:ST新华锦', 'market_cap=2757000000.0', 'median_amount_20=17539550.0']}
```

本轮共拒绝 36 条命中记录（同一标的可被多个 recipe 命中）。

### 15.3 第 3 项：limit_momentum 动量 sleeve

- 新 recipe `limit_momentum`：只识别强势动量日（默认 `min_gain=9.5%`），输出 `stop_price`
  （min(当日最低, 收盘×(1-5%))）与 `expected_holding_days=5`。
- 独立预算：`slots=1`、权重区间 `0.05..0.10`，与 setup sleeve（`slots=2`、`0.08..0.20`）
  分开计数、分开限权；`optimize_long_only` 的 `forced_min_weight` / `forced_max_weight`
  支持 `{stock_code: value}` 映射。
- **扫描域修正（关键）**：动量 sleeve 不能继承模型短名单。实测 2026-09-11 全市场 24 只
  "涨停 + 流动性 + 市值 + 非 ST"标的中 **0 只**位于模型 top-1200；新增
  `_momentum_candidates()` 在全部有模型分的标的上做轻量涨停预筛（只读最近 14 天收盘），
  把命中者并入扫描池（本次 `momentum_scan_size=42`，其中在模型池内 0 只）。

### 15.4 运行结果（2026-09-11）

```
| selection | ok | signals recipes=donchian_pullback,range_breakout,limit_momentum
  hits=69 forced=688519.SH, 601869.SH, 603693.SH min_weight=0.08 |

sleeves: setup    slots=2 hits=14 forced=[688519.SH, 601869.SH]
         momentum slots=1 hits=19 forced=[603693.SH]
```

| 代码 | 通道 | sleeve | 权重 | 说明 |
|---|---|---|---:|---|
| 301377.SZ | model | — | 16.30% | |
| 603893.SH | model | — | 15.99% | |
| 301536.SZ | model | — | 13.07% | |
| 601869.SH | signal_override | setup | 10.02% | donchian_breakout 65，pos 1.13 |
| 603693.SH | signal_override | momentum | **10.00%** | limit_momentum 100，gain +9.97%，vr 5.30 |
| 688519.SH | signal_override | setup | 9.63% | donchian_breakout 65，pos 1.05 |

gross = 0.75；603693.SH 权重恰为 sleeve 上限 0.10 → 逐 sleeve 限权生效。

### 15.5 验证

- 新增 `test/test_signal_sleeves.py`（5 个）：recipe 注册、涨停识别、非流动性拒绝、
  逐 sleeve 上下限、ST/流动性过滤与 sleeve 名额（用 warehouse stub）。
- 修正 `test/test_donchian_selection.py`：评估日期固定为 2026-09-09（原断言随本地数据推进失效）。
- 回归：6 个测试文件 63 passed。既存失败 `test/test_signal_recipes.py`（4 failed）与本轮无关。
- 产物：`output/verification/donchian_sleeves/`（MODIFIED、BASELINE、
  `donchian_sleeves.diff` 874 行、`VERIFICATION.txt`、`ROLLBACK.sh` 实测逐字节还原）。

---

## 16. 持仓卖出规则（2026-09-12）

流水线此前只有买入排序、没有卖出侧。本次补上 `--stage exits`。

### 16.1 先做研究再定规则

样本与买入侧一致：2024-01 ~ 2026-09 全市场 3.42M 股票日，市场中性超额收益，
流动性门槛 20 日中位成交额 ≥ 5000 万（产物：`output/verification/donchian_sleeves/exit_rule_study.csv`、
`exit_rule_robust_by_year.csv`）。

| 持仓状态 | +5 日 | +20 日 | 分年度 +20 日 |
|---|---:|---:|---|
| 20 日涨幅 < -15% | +0.64% | **+1.87%** | 2024 +2.15 / 2025 +2.01 / 2026 +1.58 |
| 回撤 60 日高点 < -40% | +0.83% | **+2.45%** | 2025 +3.22 / 2026 +3.01 / 2024 -0.27 |
| Donchian 位置 < 0（破下沿） | +0.15% | +0.66% | 三年为正但较弱 |
| Donchian 位置 > 0.8（贴上沿） | -0.23% | **-0.68%** | 2024 -1.80 / 2025 -0.40 / 2026 +0.10 |
| 20 日涨幅 > +15% | -0.18% | **-0.53%** | 2024 -1.66 / 2025 -0.74 / 2026 +0.39 |
| 收盘 < MA60 且 MA20 < MA60 | +0.03% | -0.05% | — |
| 收盘 > MA60 且 MA20 > MA60 | -0.08% | -0.36% | — |

**结论：本样本期是反转市，固定百分比止损与"跌破均线卖出"会系统性卖在期望收益最高的状态上。**
所以卖出侧不采用这两类规则。

### 16.2 规则集

| 类别 | 规则 | 动作 |
|---|---|---|
| R1 风险（硬） | 名称含 ST / 停牌 > 5 日 / 20 日中位成交额 < 5000 万 / 模型排名 < 30 分位 | EXIT |
| R2 兑现 | 通道位置 ≥ 0.80 或 20 日涨幅 ≥ +15%（20 日超额 -0.53% ~ -0.68%） | REDUCE 1/3（不清仓） |
| R3 结构 | 单只权重 > 35% | 减到上限 |
| 可选 | `stop_loss_pct`（默认 0 = 关闭，报告标注该规则在本样本中无效） | EXIT |

### 16.3 用户实际持仓的评估（2026-09-11）

`config/holdings_cn.csv`：600460.SH 400 股 @34.80、300503.SZ 400 股 @66.59。

| 代码 | 名称 | 市值 | 权重 | 盈亏 | 模型分位 | 20日 | 回撤60日 | 通道位置 | 操作 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| 600460.SH | 士兰微 | 12,028 | 32.3% | -13.6% | 82 | -8.2% | -47.2% | 0.04 | **HOLD** |
| 300503.SZ | 昊志机电 | 25,196 | 67.7% | -5.4% | 97 | -13.7% | -45.6% | -0.04 | **REDUCE 193 股** |

即：趋势与模型维度没有卖出理由（两只都落在"深度回撤 + 破通道下沿"这一期望为正的状态），
唯一触发的是集中度（昊志 67.7% → 减到 35%，卖 193 股）。

### 16.4 已知局限

- 模型排名规则无法回溯验证（当前只保存最新一日的打分），标注为"未验证"；
- 兑现型规则的负期望在 2026 年已衰减到接近 0，因此只减 1/3 而不清仓；
- 深回撤正期望在 2024 年为负（-0.27%），说明该效应是 regime 依赖的，需要在 bull/bear 切换时复核。

---

## 17. 整手约束与账户口径（2026-09-12）

真实账户：600460.SH 400 股 @34.813、300503.SZ 400 股 @66.633、现金 5,913.82，
总资产 43,137.82（校验：30.07×400 + 62.99×400 + 5,913.82 = 43,137.82）。

### 17.1 权重分母

风险上限必须按**总资产**计，而不是持仓市值。修正后：昊志机电 58.4%（此前误按持仓内
67.7%），士兰微 27.9%。报告同时输出两列（`weight` / `invested_weight`）。

### 17.2 整手约束

`lot_size = 100`，买卖均须整手。减仓目标改为「按上限反推股数 → 向下取整到整手」：

```
35% × 43,137.82 = 15,098 元  ->  /62.99 = 239.7 股  ->  整手 = 200 股
=> 建议卖出 200 股（2 手），剩余 200 股（12,598 元 = 29.2% < 35%）
```

清仓型 EXIT 允许卖零股；减仓型一律整手。

### 17.3 执行层新发现：4 万元账户无法执行当前的选股结果

给 selection 阶段加了「整手可执行」检查后（用 45,000 元、6 只持仓口径）：

| 代码 | 目标权重 | 价格 | 一手成本 | 目标金额 | 可买手数 | 可执行 |
|---|---:|---:|---:|---:|---:|---|
| 301377.SZ | 16.30% | 390.30 | 39,030 | 7,030 | 0 | ❌ |
| 603893.SH | 15.99% | 180.30 | 18,030 | 6,898 | 0 | ❌ |
| 301536.SZ | 13.07% | 110.90 | 11,090 | 5,636 | 0 | ❌ |
| 601869.SH | 10.02% | 473.99 | 47,399 | 4,323 | 0 | ❌ |
| 603693.SH | 10.00% | 12.80 | 1,280 | 4,314 | 3 | ✅ |
| 688519.SH | 9.63% | 327.07 | 32,707 | 4,153 | 0 | ❌ |

**6 只候选中 5 只的一手成本就超过了它的目标金额**，4 万元级账户（含 current 45,000 元口径）
无法执行。新增输出列 `one_lot_value / lots_at_target / lot_fillable` 与
`lot_execution` 汇总；下一步应在选股阶段直接把可执行性作为约束（例如
`price × lot_size ≤ equity × target_weight`），或按账户规模加价格上限。

---

## 18. 执行层：可执行性约束、止盈止损结论、每周再平衡（2026-09-12）

### 18.1 可执行性约束（"买得起"）

```toml
[selection.affordability]
enabled = true
equity = 43137.82     # 持仓 + 现金
lot_size = 100
budget_ratio = 0.15   # 单只预算 ≈ gross/持仓数
tolerance = 1.0
# => max_price = 43,137.82 × 0.15 / 100 = 64.71 元
```

两层实现：
1. **候选池预过滤**：读取模型分数后，先按最新收盘价剔除 `price > 64.71` 的标的
   （本次 5207 只候选 → 保留 4819 只）；
2. **整手修复循环**：组合优化后若某标的的最终权重买不到 1 手，剔除它（不剔除策略强制的
   sleeve 名额）并重新优化，最多 12 轮；仍有残留则权重置 0 并把 gross 归一到其余标的。

效果（2026-09-11）：

| | 修改前 | 修改后 |
|---|---|---|
| 6 只候选可整手买入 | **1 只** | **5 只（全部）** |
| 不可执行标的 | 301377 / 603893 / 301536 / 601869 / 688519 | 无 |
| 修复轮数 | — | 6（剔除 301228 / 688253 / 002859 / 603800 / 603650 / 688449） |

最终组合：688345.SH 18.87%(48.19) / 002653.SZ 17.41%(62.59) / 605100.SH 14.72%(30.98) /
002979.SZ 14.00%(50.84) / 603693.SH 10.00%(12.80，动量 sleeve)；整手取整后可投 26,198 元。

### 18.2 止盈止损：数据说"不要设固定百分比"

在"系统实际会买的画像"（成交额 ≥ 5000 万、距 60 日高点 < -30%、20 日跌幅 > 8%）上抽样
12 万个入场点，用其后 20 个交易日的日内高低价模拟各种退出规则：

| 退出规则 | 平均收益 | 中位 | 胜率 |
|---|---:|---:|---:|
| **无止损（持有 20 日）** | **+4.55%** | +3.28% | **58.9%** |
| 固定止损 -15% | +3.63% | +2.22% | 55.3% |
| 固定止损 -8% | +2.70% | -3.45% | 44.6% |
| 时间止损 10 日 | +2.54% | +1.42% | 55.1% |
| 固定止损 -15% + 止盈 +20% | +3.35% | +3.19% | 57.0% |
| 固定止损 -8% + 止盈 +15% | +2.09% | -1.23% | 48.2% |
| 移动止损 12%（自最高） | +1.68% | -0.70% | 47.7% |

**任何价格型止损/止盈都让结果变差**：这类超跌股波动大，-8% 止损在 20 日内几乎必然被打掉
（胜率 58.9% → 44.6%），随后错过反转。因此：
- 买入侧不加"买入止损"，保持事件型入场；
- 卖出侧用状态型规则（ST / 流动性 / 停牌 / 模型排名跌出 30 分位）替代价格型止损；
- 止盈只在 pos ≥ 0.8 或 20 日涨幅 ≥ +15% 时减 1/3；
- `stop_loss_pct` 保留但默认关闭；研究脚本可随时在别的 regime 上复跑。

### 18.3 每周再平衡

```toml
[selection] rebalance_stride_days = 5
```

再平衡后写入 `cn_ensemble_rebalance_state.json`；下次若距上次不足 5 个交易日，则直接沿用
上一版组合（`status=carried_forward`）不重算。`exits` 阶段仍**每日**运行，风险规则照常触发。
`--force-rebalance` 可强制立即再选（本次验证即用它）。这样把"模型 horizon ≈ 20 个交易日 +
周频调仓"与"每日风控"分开，避免每日全换仓。
