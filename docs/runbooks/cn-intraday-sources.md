# A 股分时数据源实测（近一年 5min 能不能拿到）

测试日期：2026-10-01。目的：为 `xiaomkt_rotation` 的冲击成本标定（`impact_coef`）与执行研究选源。
所有结论均为本机实测，命令与返回都在下面。

## 0. 结论速查

| 源 | 能否拿到**近一年 5min** | 历史深度（实测） | 速度 | 稳定性 | 用途判定 |
|---|---|---|---|---|---|
| **BaoStock** | ✅ 11,520~11,568 行/股（覆盖全部 241 个交易日） | 5/15/30/60min，可回溯数年 | 7~30 s/股 | ⚠️ **并发会卡死、连续大批量会被限流** | **唯一免费可用的整年 5min 源**；必须单线程 + 检查点 |
| 腾讯 `ifzq mkline` | ❌ 只有 320 根（≈7 个交易日） | 单次上限 320 根，`,,1000` 无效、日期分页参数无效 | 0.24 s/请求 | ✅ 稳定 | 仅适合**近端增量**（每日补最近 320 根） |
| 新浪 `getKLineData` | ❌ 1023 根上限（≈21 个交易日） | `datalen` ≤1023 | 0.28 s | 未做连续压测 | 近端备选 |
| 东财 `push2his` | △ 首测 1488 根（≈31 个交易日） | `klt=1/5/15/30/60`；`beg` 传 20250101 仍只回最近段 | 0.27 s | ❌ 连续请求后 `RemoteDisconnected`，冷却 90 s+ 未恢复 | **不可用于批量回补** |
| Tushare 中继（本仓库配置的两个网关） | ❌ | `stk-mins`、`a-share-mins` 均 HTTP 400 | — | — | 需官方 token 或换网关 |
| 本地 clean 分时层 | ❌（ragged） | 见下 | — | — | 不能当连续面板 |

## 1. 本地 clean 分时层现状：**拼图，不是面板**

```sql
-- 每只标的实际行数（assets/data/clean/ohlcv/market=CN/**/frequency=*min）
5min   代码数=5215  每只行数 中位=594  p90=24   最大=2610  不同首日数=595
30min  代码数=5215  每只行数 中位=100  p90=19   最大=2078  不同首日数=100
60min  代码数=5215  每只行数 中位=63   p90=19   最大=2053  不同首日数=63
```

`不同首日数` 与代码数同量级说明：每天只按"最近 320 根"增量抓一批，**不同标的的起点各不相同**。
因此 60min 虽然横跨 2024-07 ~ 2026-09，但每只标的中位数只有 63 根 —— 不能当作连续一年使用。

## 2. 实测明细

### 2.1 腾讯 mkline（仓库现有 `intraday_bars` 阶段的数据源）

```bash
curl "https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=sh600519,m5,,1000"
```

| 频率 | 返回 | 覆盖区间 | 耗时 |
|---|---|---|---|
| m5 | **320 根**（请求 1000 无效） | 2026-09-21 ~ 2026-09-30 | 0.31 s |
| m15 | 320 根 | 2026-09-02 ~ 2026-09-30 | 0.21 s |
| m30 | 320 根 | 2026-08-05 ~ 2026-09-30 | 0.22 s |
| m60 | 320 根 | 2026-06-09 ~ 2026-09-30 | 0.23 s |

第 5 个参数传日期（`param=sh600519,m60,,1000,20250601`）**不生效**，返回与不传时完全一致 → 无分页能力。

### 2.2 东财 push2his

首次请求成功：`klt=5&beg=20250101` 返回 **1488 根**，覆盖 2026-08-18 ~ 2026-09-30（即 `beg` 未生效，服务端只给最近段）。
随后连续 4 次请求全部 `RemoteDisconnected`，等待 90 s 冷却后连续 3 次仍失败 → 被限流/封禁。

### 2.3 BaoStock（`uv run python`，需 `pip/uv` 安装 `baostock`，无需 key）

```python
import baostock as bs
bs.login()
rs = bs.query_history_k_data_plus("sh.600519", "date,time,open,high,low,close,volume,amount",
                                  start_date="2025-10-01", end_date="2026-09-30",
                                  frequency="5", adjustflag="2")   # 2 = 前复权
```

| 请求 | 返回 | 耗时 |
|---|---|---|
| 5min 2025-10-01 ~ 2026-09-30（近一年） | **11,568 行 / 241 个交易日** | 7.2 s |
| 5min 2024 全年 | 11,616 行 | 31.4 s |
| 5min 2025-09 ~ 2025-12（4 个月） | 3,936 行 | 4.5 s |
| 15min 2025-06 ~ 2026-09（16 个月） | 5,232 行 | 7.7 s |
| 60min 2024-01 ~ 2026-09（2.75 年） | 2,664 行 | 7.4 s |

**限制（实测）**：3 线程并发时进程卡死（>5 分钟无返回）；连续多标的请求后，连 `bs.login()` 都会挂起。
结论：**单线程 + 每标的检查点 + 请求间 sleep(0.5~1s) + 失败退避**，允许跑到一半中断后继续。

### 2.4 Tushare 中继

```
stk-mins      -> HTTP 400
a-share-mins  -> HTTP 400
```

即仓库运行手册里标的 P2 「`a_share_mins` / `stk_mins` 未接入」仍然成立。

## 3. 推荐方案

```bash
# 推荐：只补策略真正交易过的标的（冲击成本标定的最小充分集）
uv run python scripts/backfill_cn_intraday_baostock.py \
    --codes-from-trades output/results_cn/xiaomkt_rotation_optimized/trades.csv \
    --freq 5 --start 2025-10-01

# 全市场 60min（行数只有 5min 的 1/12，适合夜间跑）
uv run python scripts/backfill_cn_intraday_baostock.py --freq 60 --start 2024-10-01

# 每日近端增量（腾讯，快且稳）
uv run python scripts/run_cn_pipeline.py --stage intraday_bars
```

时间估算（BaoStock 单线程，实测 7.2 s/股起）：

| 任务 | 规模 | 预估 |
|---|---|---|
| 近一年 5min，策略交易过的标的 | 约 150~250 只 | **20~40 分钟** |
| 近一年 5min，全市场 | 5,215 只 | ≈11 小时（需分夜跑，检查点可续） |
| 近两年 60min，全市场 | 5,215 只 | ≈3~4 小时 |
| 每日增量（腾讯 320 根） | 5,215 只 | ≈20 分钟（现有阶段） |

产物：`assets/data/raw/intraday_baostock/frequency=5min/<code>.parquet` + `manifest.json`（成功/跳过/失败清单），
字段 `date, time, open, high, low, close, volume, amount, stock_code, frequency, adjust, source`（前复权、数值型）。

## 4. 与冲击成本标定的衔接

拿到分时后可以：

1. 用 5min 的 `amount/volume` 算**日内 VWAP**，与日线 `vwap` 对比，检验日线近似的偏差；
2. 用"策略实际委托额 / 该 5min 成交额"算真实参与率，回归 `impact_coef`（当前取 1.0 的中性假设施行）；
3. 用 5min 波动率替换 20 日日波动，检验平方根律的拟合优度。

相关 TODO（`xiaomkt-rotation-todo.md`）：P0-4 容量上限验证与降容方案、P0-3 流动性/可成交性约束。

## 5. 复现命令

```bash
# 腾讯
curl -s "https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=sh600519,m60,,1000" | head -c 300
# 东财（连续请求会被拒，谨慎）
curl -s "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600519&klt=5&fqt=1&beg=20250101&end=20500101&lmt=100000&fields1=f1,f2&fields2=f51,f52,f53,f54,f55,f56,f57" | head -c 300
# BaoStock
uv run python scripts/backfill_cn_intraday_baostock.py --codes 600519.SH --freq 5 --start 2025-10-01
```

## 6. 实测标定结果（2026-10-01，45 只 / 502,992 行 / 2025-10-09 ~ 2026-09-30）

数据：优化配置近一年买入的 45 只标的、135 笔买入的 5min 分时（`assets/data/raw/intraday_baostock/frequency=5min/`），
脚本：`uv run python scripts/calibrate_impact_xiaomkt.py --freq 5 --windows 1,3,6,12`。

### 6.1 参与率（真实订单；回测初始 10 万，2025-10 时账户已复利到百万级，单笔委托额中位 25.2 万）

| 订单倍数 | 单笔委托额中位 | 参与率中位(首根 5min) | 参与率 p90(首根) | 参与率中位(前 6 根=30min) | 日参与率中位 | 首根占比>10% 的笔数 |
|---|---|---|---|---|---|---|
| ×1（回测实际） | 25.2 万 | 5.79% | 28.90% | 1.79% | 0.58% | 28% |
| ×5 | 126 万 | 28.94% | 100% | 8.93% | 2.89% | 84% |
| ×20 | 504 万 | 100% | 100% | 35.70% | 11.58% | 94% |
| ×100 | 2,520 万 | 100% | 100% | 100% | 57.90% | 100% |

### 6.2 "铺单成本"（把订单摊到前 N 根 5min，VWAP 相对开盘价的变化）

| 订单倍数 | 1 根 | 3 根 | 6 根 | 12 根 |
|---|---|---|---|---|
| ×1 | -0.0106% | 0.0000% | 0.0000% | 0.0000% |
| ×100 | -0.0106% | 0.0000% | 0.0000% | 0.0000% |

中位数为 0，过原点回归的 `impact_coef` 落在 0.17~1.23 之间但 **R²≈0** →
**用公开 5min 数据无法识别自身冲击系数**（没有自己的成交流水）。因此：

### 6.3 结论：用"参与率上限"替代拍脑袋的冲击系数

1. 已把 `max_participation = 0.01`（单笔 ≤ 1% × 20 日均成交额）写进 `config/cn_pipeline_xmkt_optimized.toml`。
2. 引擎实测（2015-01-05 ~ 2026-09-30，含 `impact_coef = 1.0`）：

   | 容量约束 | 年化 | 最大回撤 | 终值 |
   |---|---|---|---|
   | 无上限 | 24.28% | -51.51% | 128.1 万 |
   | ≤2% ADV20 | 24.28% | -51.24% | 128.1 万 |
   | **≤1% ADV20（现配置）** | **24.58%** | **-47.05%** | **131.9 万** |
   | ≤0.5% ADV20 | 22.12% | -42.81% | 104.4 万 |

   1% 上限**免费**换来 4.5pp 回撤改善（只卖掉几笔最大的委托）；0.5% 才开始伤收益。
3. **容量换算**：5 只等权 × X/5 ≤ 1%×ADV20 → **X ≤ 5% × ADV20**。
   微盘 ADV 2000~5000 万 → 账户上限约 **100~250 万**，与平方根冲击模型（coef=1 时 100 万约 12.6%、500 万 1.1%）**独立吻合**。

### 6.4 建仓可行性（当天 48 根 5min 内能否吃完目标金额）

对同样的 135 笔买入，按"每根 5min 最多吃 X% 成交额"，模拟从开盘起铺单：

| 订单倍数 | 每根参与率上限 | 成交金额比例中位 | 完全成交的笔数占比 | 用掉的 bar 中位 |
|---|---|---|---|---|
| ×1（≈25 万/笔） | 10% | 100% | 98% | 1 |
| ×1 | 5% | 100% | 96% | 2 |
| ×5（≈126 万/笔） | 10% | 100% | 96% | 5 |
| ×5 | 5% | 100% | 81% | 21 |
| ×20（≈504 万/笔） | 10% | 86.2% | 45% | 48 |
| ×20 | 5% | 43.1% | 14% | 48 |
| ×100（≈2520 万/笔） | 10% | 17.2% | 3% | 48 |

读法（三个口径不要混用）：

- **可成交性**（机械口径）：当前规模 1 根 bar 就吃完；×5 也只需 5 根（25 分钟）；×20 起才出现大面积吃不完。
- **成本纪律**（1%×ADV20 口径）：仍给出 100~250 万的容量上限 —— 因为"能成交"不等于"不冲击"。
  若为了成交把每日参与率推到 ~8%（×20 × 10%/根），价格冲击会远超 `impact_coef=1.0` 的中性假设。
- 结论：**<100 万可做；100~250 万需要按 1% 参与率纪律执行；>500 万只能靠更长建仓窗口，且冲击未建模。**

### 6.5 用 5min 建仓替代"开盘一把成交"的实测

引擎已支持 `execution_bars`（把建仓摊到前 N 根 5min，按每根参与率上限吃单，成交价取实际加权价）。
但受数据限制（只有 45 只、近一年），全样本回测无法启用，只能做执行研究：

| 2025-10 ~ 2026-09（1 年） | 年化 | 最大回撤 | 成交笔数 |
|---|---|---|---|
| 开盘价一把成交 | -33.52% | -45.47% | 274 |
| 摊到 12 根（每根≤10%） | -46.26% | -53.70% | 259（分时成交 130） |

差异主要来自**路径依赖与未成交残额**（约一半买入没有分时覆盖、回退到开盘价），而不是执行价格本身：
前面的铺单成本表显示 12 根 VWAP 与开盘价的中位差异是 0.0000%。更重要的是——**这一年策略本身在亏钱**，
执行方式的几个 bp 不改变结论。执行模型的价值在容量判断，不在收益预测。

## 7. 全 A 股一年 5min 同步（2026-10-01 起在跑）

> **2026-10-06 事故与修复（必读）**：第一版同步器把"每只 45s 硬超时"做在 Python 进程内（SIGALRM），
> 但 baostock 阻塞在 socket 读时**信号打不断** —— 结果一只标的把整轮挂了 **4 天 10 小时**，
> 进度停在 **695/5,224（13.3%）**；同时代码清单/日志/台账放在 `/tmp`，被系统清理后外层循环直接失效。
>
> 现在的架构（`scripts/sync_cn_intraday_full.py`）把这两个坑都堵上：
> ① **子进程级硬超时**：每批 5 只交给一个子进程，`subprocess.run(timeout=180)` 到点 SIGKILL，
> 卡死最多损失一批（≈1 分钟）；② **状态入库**：清单/日志/台账/隔离名单全部写在
> `assets/data/raw/intraday_baostock/_sync/`，不依赖 `/tmp`；③ **失败隔离**：单独标的连续超时 3 次进 quarantine，
> 不阻塞其余标的；④ **LaunchAgent 常驻**（`~/Library/LaunchAgents/com.quant.intraday-sync.plist`，
> `KeepAlive=true` + `RunAtLoad=true` + `caffeinate -is`），重启机器后自动续跑；
> ⑤ 会话限时 1 小时后正常退出，由 KeepAlive 重新拉起，天然清掉任何泄漏状态。
>
> 修复后实测（2026-10-06 09:12）：速率 **0.234 只/秒（≈4.3 秒/只）**，比事故前快 2.7×，
> 剩余 4,533 只 **ETA ≈4.5 小时**。已落盘 740 只，其中 **718 只覆盖完整 241 个交易日**，
> 17 只不足 200 天（多为期间新上市/长期停牌，属正常）。

```bash
# 后台常驻（LaunchAgent，重启后自动恢复），plist 已安装
cat ~/Library/LaunchAgents/com.quant.intraday-sync.plist
launchctl load -w ~/Library/LaunchAgents/com.quant.intraday-sync.plist      # 需要时重新加载
launchctl unload ~/Library/LaunchAgents/com.quant.intraday-sync.plist      # 需要时停止

# 监控
tail -f assets/data/raw/intraday_baostock/_sync/sync.log          # 进度与批次超时
cat assets/data/raw/intraday_baostock/_sync/progress.json         # 速率 / ETA / 隔离数
uv run python scripts/sync_cn_intraday_full.py --status           # 目标/已完成/剩余/ETA
launchctl list | grep intraday                                    # 任务是否存活

# 一次性查看隔离名单（连续超时的标的）
cat assets/data/raw/intraday_baostock/_sync/quarantine.json
```

设计（对应 BaoStock 的两个坑：**并发卡死** + **连续请求被限流**）：

| 机制 | 参数 | 作用 |
|---|---|---|
| 清单优先级 | `assets/data/raw/intraday_baostock/_sync/codes.txt` | 策略交易过的标的排最前，先补最需要的 |
| 逐股检查点 | `frequency=5min/<code>.parquet` | 已落盘即跳过；中断/关机/掉线都不丢进度 |
| **批次硬超时** | `--batch-size 5 --batch-timeout 180` | 子进程到点 SIGKILL，单只卡死最多损失一批 |
| 隔离名单 | `--max-failures-per-code 3` | 连续超时的标的归档到 `quarantine.json`，不阻塞其余标的 |
| 会话限时 | `--session-seconds 3600` | 每小时正常退出，由 KeepAlive 重新拉起 |
| 托管 | LaunchAgent（`KeepAlive`+`RunAtLoad`）+ `caffeinate -is` | 重启机器后自动续跑，且防止休眠 |

**实测进度（2026-10-06 09:12，修复后）**：目标 4,533 只待补（全量 5,224 只沪+深在市 A 股；
北交所未包含，BaoStock 不提供），已完成 740 只（其中 718 只覆盖完整一年），
速率 ≈0.234 只/秒（≈4.3 秒/只），**ETA ≈4.5 小时**。

**产物与后续**：

- `assets/data/raw/intraday_baostock/frequency=5min/<CODE>.parquet`（前复权，字段 date/time/open/high/low/close/volume/amount）
- **15/30/60min 不需要再抓**：可从 5min 直接聚合（open=首、high=max、low=min、close=末、volume/amount=sum）
- 完成后可把 `scripts/calibrate_impact_xiaomkt.py` 的匹配范围从 45 只扩到全市场，重跑容量标定

提速选项（未启用，按需选择）：① 只补需要的板块（主板小市值 ≈1,300 只，约 4 小时）；
② 只补策略交易过的 123 只（约 25 分钟，已排在最前）；③ 用付费源（Tushare 官方 token 的 `stk_mins`，本仓库中继不支持）。

## 8. 2026-10-23 更新：深度 / 兜底 / 每日自愈

| 结论 | 证据 |
|---|---|
| BaoStock 5min **从 2020-01-02 起**可用；2019 全年为空 | `query_history_k_data_plus(frequency="5")` 逐日探测 |
| 新浪 `getKLineData` 的 `datalen` 上限 **1,970 根 ≈ 41 个交易日**，且返回 `amount` | `datalen=1970` 正常、`1971` 返回 `null` |
| iFinD 试用 5min 仍只有近 1 年 | 2019/2020 请求 `-4309` |
| BaoStock 并发会拉黑 IP | 6 worker 约 6 分钟后 `10001011 黑名单用户`，持续 >90 分钟 |
| `--refresh` 曾漏刷 695 只 | `codes.txt` 只含"首次回补时没文件"的标的 → 684 只停在 09-30 |

对应改动与操作手册见 [`intraday-history.md`](intraday-history.md)：
`fill_intraday_gap_recent.py`（新浪 1970 根 / 腾讯 320 根兜底）、`backfill_cn_intraday_history.py`
（2020+ 深度回补，多进程 + 冷却 + 断点）、`sync_cn_intraday_full.py` 的 `full_universe()` / `stale` 指标。
