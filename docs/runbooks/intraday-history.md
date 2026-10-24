# 5min 分时：每日自愈 + 历史回补

更新时间：2026-10-23。对应问题：**「每日分时同步总会有一部分没同步成功，要手动补」**、
**「历史分时能不能用 iFinD 补到 2016」**。

## 0. 结论速查

| 问题 | 结论 |
|---|---|
| iFinD 能补 2016 起的 5min 吗 | ❌ 试用 `high_frequency` 只给近 1 年（2019 及更早 `-4309`） |
| 谁能给多年 5min | **BaoStock**：2020-01-02 起可用（2019 全年为空），~500 行/秒/连接 |
| 谁能补「最近 1~2 个月」缺口 | **新浪 `getKLineData`**：`datalen` 上限 **1970 根 ≈ 41 个交易日**，带成交额，0.05s/只 |
| 现网覆盖率 | 5,228 只全部对齐到 2026-10-22（stale=0）；2020-2024 深度仅 0.4~0.5%（19 只） |
| 每日还会不会漏 | 不会静默漏：refresh 目标改为「全部在市 ∪ 全部已有文件」，并加了新浪兜底 + 状态快照 |

## 1. 每日链路（`scripts/run_daily_production.sh`）

```
daily_bars（日线）
  ├─ intraday_tencent_fill   新浪 1970 根 / 腾讯 320 根，补最近 ≤40 个交易日缺口（无需登录，秒级）
  ├─ intraday-refresh        BaoStock 增量：只抓「文件最后日期 < 该股最后日线」的票，--merge
  ├─ intraday-repair         BaoStock 小批量重试（20 只 / 600s）
  └─ intraday-status         写 output/pipeline_reports/daily/<date>/intraday_status.json
```

开关：`DAILY_INTRADAY_TENCENT_FILL=0`、`DAILY_INTRADAY=0`、`DAILY_INTRADAY_REPAIR=0`；
预算：`DAILY_INTRADAY_SECONDS`（默认 1800）、`DAILY_INTRADAY_TENCENT_LIMIT`（默认 400）。

### 1.1 之前为什么会「总有一部分没同步」

两个独立 bug，都在 2026-10-23 修掉：

1. **刷新目标集错了**：`--refresh` 只遍历 `_sync/codes.txt`，而这份清单是「首次回补时**还没有文件**的标的」。
   文件已存在的 695 只永远不在清单里 → 实测 **684 只停在 2026-09-30**，`--status` 却报 `remaining=0`。
   现在 `--refresh` 用 `full_universe()` = 全部在市 A 股 ∪ 全部已有 parquet。
2. **状态只看「文件在不在」**：`status()` 现在额外输出 `stale`（最后日期落后于该股自己最后一条日线的数量）。

```bash
uv run python scripts/sync_cn_intraday_full.py --status
# {"target_total": 4533, "done": 5228, "remaining": 0, "stale": 0, ...}
```

### 1.2 停牌股不再被误报

`refresh_pending()` 用**每只票自己的最后一条日线**当目标日期；停牌/退市股不会被当成「落后」。

### 1.3 BaoStock 黑名单（10001011）

并发会话会把 IP 拉黑（实测 6 个 worker 跑约 6 分钟后触发）。现在：

- 子进程输出出现 `10001011` → 立即中止会话、写 `_sync/cooldown.json`（6 小时），**不计入逐只 quarantine**；
- 每日链路检测到 cooldown 会跳过两个 BaoStock 步骤，只走新浪/腾讯兜底；
- 历史回补任务同样读这份 cooldown，冷却期内不登录。

## 2. 近端兜底：新浪 / 腾讯

```bash
uv run python scripts/fill_intraday_gap_recent.py --status          # 有多少 stale
uv run python scripts/fill_intraday_gap_recent.py --limit 700 --workers 6
```

规则（防止「看起来新、中间有洞」）：

- 只用**该股真实有日线**的交易日定义缺口（停牌日不算缺）；
- 新浪窗口的首根必须 ≤「最后一条日线的下一个交易日」，否则判定 `hole` 并交给深度回补；
- 15:05 之前不写当天（避免写进半天数据）；
- 写入 = 读旧 parquet → 按 `(date, time)` 去重合并 → 临时文件 + `os.replace` 原子替换。

实测：686 只 22 天缺口，**675 只 23 秒补完**（腾讯窗口不够的会落到 `hole`）。

| 源 | 深度 | 成交额 | 速度 | 备注 |
|---|---|---|---|---|
| 新浪 `getKLineData` | 1,970 根 ≈ 41 交易日 | ✅ 元 | 0.05 s/只 | 首选兜底 |
| 腾讯 mkline | 320 根 ≈ 7 交易日 | ❌（close×volume 估算，source 带 `_est`） | 0.2 s/只 | 二级兜底 |
| 东财 push2his | 1,512 根 ≈ 31 交易日 | ✅ | 0.1 s/只 | 连续请求会被 `RemoteDisconnected`，不作主路径 |
| BaoStock | 2020-01 起 | ✅ | ~500 行/秒/连接 | 深度回补唯一来源 |

## 3. 深度回补（2020-01 起）

```bash
uv run python scripts/backfill_cn_intraday_history.py --status
uv run python scripts/backfill_cn_intraday_history.py --workers 2 --limit 20   # 试跑
uv run python scripts/backfill_cn_intraday_history.py --workers 2 --session-seconds 10800
```

设计：

- **优先级**：实盘持仓 → 近 30 天策略选股 → 成交过的标的 → 其余按最近成交额降序（`--max-priority` 默认 1,500）；
- **断点**：每只完成写 `_hist/done/<code>.json`，重复启动自动跳过；
- **限流**：`cooldown.json` 冷却 + `PAUSE` 文件（15:00-19:59 自动暂停，给每日增量让路）；
- **卡死**：单只 900s SIGALRM + 父进程心跳看门狗（默认 1,200s 无心跳 → kill + 重排）。

常驻调度（可选）：

```bash
cp scripts/launchd/com.quant.intraday-history.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.quant.intraday-history.plist
# 连续 3 轮零新增会写 _hist/STOPPED 自动停（防止被永久拉黑后无限空转）；删掉该文件即恢复
```

容量：全市场 5,228 只 × 2020-2026 ≈ 4.1 亿行；实测 4 worker = 2.3 只/分钟，2 worker ≈ 1.2 只/分钟。
即：**2 worker 全量约 3 天**，优先池 1,500 只约 20 小时。

## 4. 核对

```bash
uv run python scripts/report_intraday_coverage.py     # 重新生成 INTRADAY_COVERAGE.md
uv run python scripts/sync_cn_intraday_full.py --status
uv run python scripts/fill_intraday_gap_recent.py --status
```

口径提醒：覆盖率按「股票-日」算（分子=有 5min 的股票-日，分母=当日有日线的股票-日）。
用「日期并集」会把 19 只有 2020 数据的票算成 100%，是错的。

## 5. 已知限制

- 2016-2019 的 5min：BaoStock 为空、iFinD 试用拒绝、腾讯 320 根、东财 1,512 根、新浪 1,970 根 —— **免费源拿不到**；
  需要正式 iFinD/交易所级历史文件或付费供应商。
- 复权：`adjustflag=2`（前复权）。不同日期抓取的前复权价会随最新因子变化，长历史重抓会重写重叠区间（`keep="last"`），
  跨除权日的旧片段与新片段可能不一致；做跨年回测时建议用「不复权 + 复权因子」重算。
