# P1.19 CN 数据卫生与自动化稳定性修复计划

状态：§1 已执行并验证（2026-09-22），§2~§5 待执行
关联：`docs/runbooks/cn-data-pipeline.md`、P1.17 §0.12/§0.18、P1_18 龙虎榜计划
证据：`output/pipeline_reports/cn_pipeline_20260921_195432.json`、
`output/data_quality/cn_ohlcv_20260921_195430_cn.md`、本文件 §1~§4 的内联实测输出

---

## 0. 一句话结论

2026-09-21 的 `daily_bars` 本身**没有漏抓**（5221/5221 成功、0 失败、0 质量告警；可交易标的
5209 只，差的 12 只全部是停牌），但库里存在两类历史卫生问题（§1 指数代码、§2 停牌语义）和
两个稳定性问题（§3 预检告警口径、§4 自动化停摆），每一项都可能污染模型或让每日生产静默失败。

---

## 1. 问题 A：128 个上证指数代码被当成个股落库（历史污染，优先级最高）

### 实测证据（2026-09-21）

```
$ 读 assets/data/clean/ohlcv/**/frequency=daily/adjust=qfq/**/*.parquet
全库代码 5349 个，其中不符合个股交易所前缀的 128 个，全部是 000xxx.SH：
  000001.SH(上证指数 3949.91) 000002.SH(上证A股指数 4143.2) ... 000148.SH
  行数 395,008，日期范围 2014-01-02 → 2026-09-09
$ 读 clean_feature_panel（market=CN, feature_set=alpha_zoo_hk）
  128 个代码、47,232 行，日期 2025-03-10 → 2026-09-09，每天恰好 128 行
  训练窗口 2025-09-19~2026-04-27：窗口 749,475 行中有 18,176 行是它们 = 2.43%（128/日）
  价量类特征填充率 1.000（KMID/ROC5/ROC20/vol_cc_20/pv_return_20d），
  资金流与估值全 NaN（moneyflow_net_z_5d_clean=0.000、valuation_market_cap_log_clean=0.000）
$ 读 assets/data/meta/stock_info_registry
  5,347 行里仍包含这 128 个代码（000001.SH 等），即元数据层仍把它们当个股
```

### 影响

1. **横截面污染**：09-10 之前每个交易日有 128 行"非个股"参与 rank/z-score/regime 宽度/资金流
   横截面统计，且它们的 amount 是全市场成交额（例：000001.SH 单日 8.7e11），流动性类分位数会
   被严重拉偏。
2. **universe 断点**：抓取侧自 2026-09-10 起不再收这些代码，全库单日行数出现最大跳变
   **−129（5,335 → 5,206）**；任何跨 09-10 的 walk-forward 折都面对"前后 universe 不同"。
3. **训练集污染**：当前生产模型（lightgbm/transformer，训练窗口 2025-09-19~2026-04-27，
   1,267 / 500 列）**整段落在污染区间内**，每天 128 行带有效标签的指数行。

### 修复方案（建议按此顺序）

1. 在 CN universe 侧加**交易所前缀白名单**（沪 600/601/603/605/688/689，深 000/001/002/003/
   300/301/302，北 43x/83x/87x/920），抓取、`stock_info`、`features`、`clean_panel` 共用同一
   判定函数，避免各处各写一份正则。
2. 一次性清理：把这 128 个代码从 `stock_info_registry` 与历史 OHLCV/feature 分区中剔除
   （或打 `is_index=true` 标记并在建面板时强制排除，保留原始数据以便对照）。
3. 重跑 `clean_panel`（`features` 不必重算），确认 2026-09-09 与 09-10 的单日行数不再跳变。
4. 重训 lightgbm + transformer，并用 `--stage oos_predictions` + `model_comparison` 对比
   "剔除指数行"前后的 rank IC / Top-N 超额，确认污染影响量级。

### 验收

- [x] `daily_bars` 之后，库内代码全部通过交易所前缀校验（不再出现 `000xxx.SH`）；
- [x] `clean_feature_panel` 任一交易日的行数在 09-10 边界前后连续（无 ±129 跳变）；
- [ ] `model_manifest.json` 的训练窗口统计中不再包含指数代码（需重训，§1.4 待做）；
- [x] `test/` 新增"universe 前缀白名单"回归测试（`test/test_cn_equity_universe.py`，30 例）。

### 执行记录（2026-09-22 已落地）

代码（`data/ingest/service.py`）：

| 位置 | 改动 |
|---|---|
| 模块级 | 新增 `CN_EQUITY_PREFIXES`（沪 600/601/603/605/688/689、深 000/001/002/003/300/301/302）、`is_cn_equity_code()`、`filter_cn_equity_codes()` |
| `_cn_metadata_codes()` | 过滤（覆盖 moneyflow/估值/财务/stock_info 的 universe 来源） |
| `get_all_stock_codes()` | 仅 `market="CN"` 时过滤，HK 与其他市场不受影响 |
| `download_cn_market_data()` | 抓取列表二次过滤，新增 `excluded_non_equity_count` / `excluded_non_equity_codes` 报告字段（不再静默跳过） |
| `materialize_clean_feature_panel()` | 面板构建前过滤 `stock_codes` 并同步裁剪 OHLCV 帧；manifest / 返回摘要记录被剔除代码 |
| `_clean_panel_training_data()` | 训练前兜底过滤 + `[WARN]` 提示需要重建面板（防旧面板复活污染） |

数据：

```bash
$ uv run python scripts/prune_cn_non_equity_codes.py --dataset stock_info            # dry-run
[would prune] .../part-b14615fa...parquet: 128/2446 rows
dataset=stock_info rows=5347 non_equity_rows=128 files_changed=1 mode=DRY-RUN
$ uv run python scripts/prune_cn_non_equity_codes.py --dataset stock_info --apply
[prune] ...: 128/2446 rows ; backup -> ...parquet.bak-20260922_094607
# 复核：registry rows 5347 -> 5219，000xxx.SH = 0

$ uv run python scripts/run_cn_pipeline.py --stage clean_panel        # exit 0
  clean_panel ok: stocks=5221 rows=1,934,773 excluded_non_equity_count=128
  start_date=2025-03-11 end_date=2026-09-22 feature_count=683
$ uv run python scripts/run_cn_pipeline.py --stage model_scores       # exit 0
  cn_lightgbm_scores.csv    5209 行 / 5209 只 / index_rows 0（此前会含 128 行指数）
  cn_transformer_scores.csv 5108 行 / 5108 只 / index_rows 0
```

面板前后对比（同一脚本口径，`artifacts/p1_19_cn_equity_whitelist/PANEL_{BEFORE,AFTER}.json`）：

| 指标 | 前 | 后 |
|---|---:|---:|
| 代码数 | 5,349 | **5,221** |
| 指数代码 / 行数 | 128 / 47,232 | **0 / 0** |
| 每日行数最大跳变 | 2026-09-10 **−129** | 2026-05-06 +29（正常上市） |
| 2026-09-10 跳变 | −129 | **−1** |
| 总行数 | 1,987,079 | 1,934,773 |

测试：`test/test_cn_equity_universe.py` 30 passed；相关 8 文件回归 172 passed / 3 failed，
3 个失败经 `git stash` 对照确认为**改动前既有**失败（pandas 3 时区 `astype` 问题，
`test_cn_data_chain.py`），与本次改动无关。

### §1.4 重训与 OOS 量化（2026-09-22 已完成）

重训（生产配置，全部 exit 0）：

| 模型 | 训练窗口 | 训练行数 | 特征列 | 校验指标 | 对比（污染面板，09-19） |
|---|---|---:|---:|---|---|
| LightGBM | 2025-09-22~2026-04-28 | 731,342 | 1,249 | daily_ic −0.00002 | 1,267 列 / 749,475 行 / daily_ic −0.0152 |
| Transformer | 2025-09-22~2026-04-28 | 426,014 | 492 | huber 0.05473 | 500 列 / 19,691 行 / huber 0.05241 |

OOS A/B（**同配置、同 5 折、同 50 个决策日 2025-09-02~2026-08-20**，唯一差异是面板里有没有
那 128 行/日指数）：把代码切回改动前重跑一次 `clean_panel` + `--stage oos_predictions`（只跑
lightgbm），生成"污染臂"，与"干净臂"用同一脚本（`evaluate_cn_model_comparison`）评估：

| 臂 | 预测行数 | 指数行 | RankIC | IC | Top10% 净 | 多空 | 链式净 | 最大回撤 | 换手 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 污染面板 | 264,197 | 6,144 | 0.0993 | 0.0699 | +1.84% | +3.43% | +4.25% | −1.17% | 0.619 |
| 干净面板 | 258,053 | 0 | 0.0996 | 0.0698 | +1.78% | +3.53% | +4.09% | −1.18% | 0.617 |

逐日配对检验（同一批 50 个决策日，剔除指数代码后的股票口径）：
`污染 mean RankIC 0.10182 vs 干净 0.09963`，差值 **−0.0022（t=−1.31, p=0.195, 胜 20/50）**。

**结论：指数行的存在既没帮也没害（差异不显著）**。§1 的价值是数据卫生——训练集里不再有
2.43% 的非个股行、universe 断点消失、报告口径自洽；**不是**性能杠杆。因此也不必为此重跑
历史结论：P1.17 的 13 折/169 日结论仍然有效。Transformer 臂的 A/B 未跑（同一机制，预计同向），
需要时可再用 `output/verification/p1_19_index_pollution_20260922/evaluate_oos_pair.py --arm polluted_lgbm|clean_lgbm` 复现。

另外，横截面值的污染也已量化（`probe_cross_sectional_shift.py`）：剔除 128 行后 RPS_5/20/60
的 mean|Δ| 只有 0.22~0.59 个百分点、max 1.2，**Spearman = 1.000000**，所以因子层不需要全量重算。

原始 OHLCV/feature 层仍保留历史指数行（读路径已全部过滤，属惰性数据）；物理清除留待需要时再做。

---

## 2. 问题 B：停牌股在库里的语义（现状正确，但要写进契约）

### 实测证据（2026-09-21）

```
今日无 bar 的 12 只（实时行情 vol=0）：
  中金公司 601995 / 东兴证券 601198 / 信达证券 601059（同日 09-14 起停牌）
  广汽集团 601238 / 园林股份 605303 / 华之杰 603400 / *ST清越 688496
  奥克股份 300082 / 奥联电子 300585 / *ST元道 301139 / *ST萃华 002731 / *ST康佳A 000016
09-21 与 09-18 代码集合差异只有 4 只：
  出：300082.SZ、300585.SZ（今日停牌）    入：600301.SH、600825.SH（今日复牌）
独立源核对（腾讯前复权日线，2026-09-01~09-21）：
  600825 新华传媒：09-01/02/03/04 → 09-21（09-07~09-18 无 K 线，复牌一字 5.84）
  600301：09-01..09-11 → 09-21（09-14~09-18 无 K 线）
  => 落库与行情源逐日一致，缺的交易日是停牌，不是抓漏
```

### 约定（写进 runbook，供 exits / 面板构造遵守）

- **停牌期间不写行**（现状），因此"某标的最近 bar 日期 < 最新交易日"是正常信号，不等于数据缺失；
- 依赖连续交易日的滚动量（`median_amount_20`、`ATR14`、`vol_*_20`）按**已有 bar** 计算，停牌会让
  窗口在日历上变长，这是可接受的；`exits` 已用 `max_suspend_sessions = 5` 处理长期停牌；
- 复牌当天只写当天（本次 600825/600301 均如此），**不做停牌期回填**：回填会伪造零成交行，
  比缺行更有害；
- 若未来需要"停牌中不许买入"的显式约束，应读行情源的状态字段，而不是用行缺失反推。

### 验收

- [ ] `daily_bars` 报告的 `success_count` 与库里当日行数差额，等于当日停牌名单长度；
- [ ] 每日产出附一份"当日停牌名单"（可从实时行情 `vol=0` 推导），避免每天人工核对。

---

## 3. 问题 C：预检告警 `cn_ohlcv_rows_below_threshold` 的构成

`data/ingest/service.py` 的健康检查把"universe 代码集 − 窗口内行数 ≥ `min_ohlcv_rows`(120) 的
代码集"非空即置该告警。当前触发它的是：128 个指数代码（历史有行但窗口内不足 120）+ 12 只停牌股
+ 次新股。它不阻塞（`backtest_ready=true`、`blocking_reasons=[]`），但告警信息量低。

改进：把告警拆成三个具名原因（`suspended_codes`、`new_listing_codes`、`short_history_codes`），
并各自附前 10 个代码，使告警能被直接判断而不用人工跑一遍 python。

注：§1 修完后，"universe 里的指数代码"这一项已消失，剩下的都是停牌/次新，属于正常业务状态。

---

## 4. 问题 D：自动化停摆（launchd 未加载 + 09-18 运行中被打断）

### 实测证据（2026-09-21 20:05）

```
$ launchctl print gui/501/com.quant.cn-pipeline
Bad request. Could not find service "com.quant.cn-pipeline" in domain for user gui: 501
$ last reboot | head -2        # 机器 09-09 起连续运行 12 天，排除重启导致未加载
reboot time  Wed Sep  9 10:08
$ tail output/pipeline_reports/daily/launchd.out.log
2026-09-18 20:53:17 stage=regime ok (28s)
（其后无任何行；09-19/20 为周末；09-21 全天没有任何触发记录）
$ tail output/pipeline_reports/daily/2026-09-18/features.log
factor gen: 96%|... 5140/5349 [52:16<...]   （21:47 停止）
resource_tracker: There appear to be 6 leaked semaphore objects to clean up at shutdown
$ ls output/pipeline_reports/daily/2026-09-18/
daily_bars.log moneyflow.log regime.log features.log preflight.log run.log   ← 没有 production.done
```

### 根因

1. launchd agent 在 09-18 之后处于**未加载**状态（无重启可归因，需现场确认是手动 bootout 还是
   plist 被改写）；没有 agent 就没有 19:30/20:30/22:00 的触发。
2. 09-18 的运行为 `features` 阶段被杀（52 分钟、96%），因此**没有写 `production.done`**，
   当日也没有 `preselection`/`pk`/`exits`（09-18 的生产选股是 12:03 的人工运行）。

### 处置（本次已完成）

```bash
bash deploy/daily-cn-pipeline/macos/install.sh     # bootout + bootstrap + enable
launchctl print gui/501/com.quant.cn-pipeline      # state = running（RunAtLoad 触发当日补跑）
```

### 后续加固建议

1. **被杀检测**：结束时若未写 marker，用飞书/邮件告警（现在只在"阶段返回非 0"时告警，
   "进程被杀"不会触发）；
2. **断点续跑**：把已完成阶段记进 `output/pipeline_reports/daily/<date>/stages.done`，重跑时跳过，
   避免 `features` 重算 50 分钟；
3. **plist 守护**：每天检查 `launchctl list | grep com.quant.cn-pipeline`，缺失即提醒
   （agent 未加载时告警链路本身也会失效，所以检查要放在独立脚本或其它调度器里）；
4. **features 进度持久化**：现在按股票批量写，被杀后需要重新 precheck；
   `flush_stocks=3` 可调大以提高被杀时的完成比例。

---

## 5. 问题 E：遗留测试失败（低优先级）

`test/test_signal_recipes.py` 4 个用例仍期望 `setup_type == "pre_breakout"`，实际为
`bottom_rebound`；`factor_engine/signals` 与测试文件都未改动（`git diff --stat` 为 0 行），
属既有遗留。12 文件回归集当前为 158 passed / 4 failed。

处置：把断言改为"分类结果落在允许集合内且 runner 与 direct 调用一致"，而不是钉死某个标签。

---

## 6. 执行顺序

1. §1 白名单 + 清理指数代码 + 重跑 `clean_panel`（收益最确定，先做）；
2. §4.1/§4.2 自动化告警与断点续跑（当天可完成）；
3. §1.4 重训 + OOS 对比（确认污染影响量级）；
4. §3 告警拆分、§5 测试修复（随手做）。

P1.18（龙虎榜 informed proxy）排在 §1 之后：它的横截面统计同样受指数行影响，先清干净再上研究。
