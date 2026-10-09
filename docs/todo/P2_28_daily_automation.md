# P2-28 每日自动化审计：arm_selection / 分时 到底有没有在跑（2026-10-08）

> 触发：用户问「arm_select / 分时 有在每天的自动化里跑么？」
> 证据：`artifacts/daily_automation/`（VERIFICATION.txt / evidence/*.log / evidence/ui/*.png）。

---

## 1. 审计结论（都是实测，不是看代码猜的）

| 问题 | 结论 | 证据 |
|---|---|---|
| `arm_selection` 每天跑吗？ | ❌ **没有** | `run_daily_production.sh` 的 `STAGES` 数组是硬编码的 11 个阶段，**不含 arm_selection**；虽然 `config/cn_pipeline.toml [pipeline].stages` 里有它，但那个列表**不被这个脚本读取** |
| 分时每天跑吗？ | ❌ **没有** | `[stages] intraday_bars = false`；负责分时的 LaunchAgent 判据是"文件存在"，补齐 5,228 只后永远空转 |
| 每日生产本身在跑吗？ | ❌ **没有** | `launchctl list \| grep -c quant` = 1（只有 intraday-sync）；`com.quant.cn-pipeline` **未加载**；最后一次自动产出 `output/pipeline_reports/daily/` 是 **2026-09-27** |

### 1.1 arm_selection

```
$ grep -n '^STAGES=' -A 13 scripts/run_daily_production.sh
STAGES=(
  "daily_bars:false" "moneyflow:false" "regime:false"
  "features:$SKIP_RETRAIN" "clean_panel:$SKIP_RETRAIN" "model_scores:false"
  "preselection:false" "pk:false" "exits:false"
  "paper_account:false" "paper_outcomes:false"
)                                   ← 没有 arm_selection

$ ls -la output/results_cn/arm_latest_selection.csv
2026-10-08 08:57                     ← 这是本次会话**手动**跑出来的，不是自动化
```

### 1.2 分时

```
$ launchctl list | grep -i quant
-  0  com.quant.intraday-sync          ← 只有这一个还挂着

$ plistlib: com.quant.intraday-sync
  KeepAlive = True     RunAtLoad = True
  StartCalendarInterval = None         ← 没有任何时间计划
  ProgramArguments = caffeinate -is uv run python scripts/sync_cn_intraday_full.py

$ tail assets/data/raw/intraday_baostock/_sync/service.out.log
[2026-10-08 16:52:10] === 监督器启动 目标 4533 只 ===
[2026-10-08 16:52:10] 全部完成或有配额隔离：done=5228 quarantine=0
[2026-10-08 16:52:40] === 监督器启动 目标 4533 只 ===     ← 每 30s 重来一次，什么也不做
```

根因在监督器的判据上：

```python
# scripts/sync_cn_intraday_full.py（旧）
have = {p.name.replace("_", ".")[:-8] for p in freq_dir.glob("*.parquet")}
pending = [c for c in target if c not in have]     # ← "完成" = 文件存在
```

它是为**一次性历史回补**写的。5,228 个 parquet 都在了以后，它永远判"完成"，
**永远不会去补新的一天**。实测数据停在 **2026-09-30**（今天是 10-08）。

### 1.3 更根本的问题

`output/pipeline_reports/daily/` 最后一次是 **2026-09-27**；日线 `assets/data/clean/ohlcv` 最大
`trade_date` 也是 **2026-09-30**。也就是说：**不是"少跑两步"，是整条每日生产已经停了**。

---

## 2. 修法

### 2.1 分时：从"存在即完成"改成"覆盖到最新交易日"

| 文件 | 改动 |
|---|---|
| `scripts/backfill_cn_intraday_baostock.py` | 新增 `--merge`：读旧 parquet → 拼接新窗口 → 按 `(date, time)` **去重（新值优先）** → 排序后写回。旧文件损坏时告警并只用新窗口，不静默丢历史。**此前只有 `--force`（整文件覆盖），重拉一小段会把一年历史全冲掉**，所以根本没法做每日增量 |
| `scripts/sync_cn_intraday_full.py` | 新增 `--refresh`：判据换成"文件最后日期 ≥ 最新交易日"（`latest_trade_date()` 直接读日线最大 `trade_date`，不引入交易日历依赖）；只抓 `最后日期+1 → 今天` 这一小段（每只约 48 行）并 `--merge` 回去。另加 `--limit` 便于调试 |

### 2.2 每日生产：把两步接进去

`scripts/run_daily_production.sh` 在原 11 个阶段之后、两阶段报告之前，新增两个**不阻塞生产**的步骤：

```bash
if [ "$FAILED" -eq 0 ] && [ "${DAILY_ARM_SELECTION:-1}" = "1" ]; then
  uv run python scripts/run_cn_pipeline.py --stage arm_selection > "$LOG_DIR/arm_selection.log"
fi
if [ "$FAILED" -eq 0 ] && [ "${DAILY_INTRADAY:-1}" = "1" ]; then
  NO_PROXY='*' uv run python scripts/sync_cn_intraday_full.py --refresh \
      --session-seconds "${DAILY_INTRADAY_SECONDS:-1800}" > "$LOG_DIR/intraday_refresh.log"
fi
```

两个设计点：

* **不阻塞**：arm_selection 是研究手臂（不替代生产名单），分时刷新是数据补齐 —— 任一失败都只记日志、
  不影响当日选股结果与 `production.done` 标记（与既有的 `RUN_IC_PROTOCOL` 同一套非致命模式）。
* **顺序**：分时刷新必须在 `daily_bars` **之后** —— 它用日线的最大 `trade_date` 当"最新交易日"，
  日线没更新就没得补。所以放在主阶段循环之后。

`--dry-run` 也会把这两步列出来（此前 dry-run 只列 11 个阶段，会让人误判实际会跑什么）。

### 2.3 arm_selection 的历史归档

`select_latest_via_arm.py` 原来只写会被每天覆盖的 `arm_latest_selection.csv`，
"上周二选的是哪 5 只" 回顾不了。现在**同时**写一份
`output/results_cn/arm_selection_history/<trade_date>.csv`（同日重跑覆盖当天那份）。

`build_selection_ledger.py` 新增 `read_arm_history()` 把它接进选股台账，
于是 **选股日历的来源下拉里出现 `arm`**，可以按源回看手臂名单与后续收益。

### 2.4 LaunchAgent

| 任务 | 改动前 | 改动后 |
|---|---|---|
| `com.quant.cn-pipeline` | **未加载** | 已加载（每日 19:30 / 20:30 / 22:00，`RunAtLoad=true` 用于开机补跑） |
| `com.quant.intraday-sync` | `KeepAlive=true` + 无时间计划 → 每 30s 空转；判据"文件存在" | 改成 `--refresh --session-seconds 1800`，`StartCalendarInterval` **每日 17:10**，去掉 `KeepAlive`（不再空转） |

旧 plist 已备份为 `~/Library/LaunchAgents/com.quant.intraday-sync.plist.bak_<ts>`，
本轮副本另存 `artifacts/daily_automation/evidence/`。

---

## 3. 验收（实测）

```
$ bash scripts/run_daily_production.sh --dry-run | tail -3
stage=paper_outcomes would run: uv run python scripts/run_cn_pipeline.py --stage paper_outcomes --force-rebalance
step=arm_selection   would run: uv run python scripts/run_cn_pipeline.py --stage arm_selection
step=intraday-refresh would run: sync_cn_intraday_full.py --refresh --session-seconds 1800

$ launchctl list | grep -i quant
69616  0  com.quant.cn-pipeline          ← 已加载并在跑（见下）
-      0  com.quant.intraday-sync       ← 已加载，等 17:10

$ tail output/pipeline_reports/daily/2026-10-08/run.log
2026-10-08 16:56:17 preflight ok: uv run python -m pytest test/... -q
2026-10-08 16:56:17 start daily production trade_date=2026-10-08 force=0 dry_run=0 skip_retrain=0
$ ps aux | grep run_cn_pipeline
... scripts/run_cn_pipeline.py --stage daily_bars --force-rebalance     ← 正在跑第一个阶段

$ uv run python scripts/sync_cn_intraday_full.py --status        # 旧判据
{"target_total": 9066, "done": 5228, "remaining": 0, ...}        ← 永远"完成"
$ 离线调用 refresh_pending(...)                                   # 新判据
推断的最新交易日: 2026-09-30
落后/缺失的只数: 16        ← 确实能识别出 8/14、9/14、9/21 等没补齐的标的
```

`--merge` 的语义用**离线构造**验证（baostock 当日不可达，见 §4）：

```
旧帧 3 行；新窗口 3 行 → 合并 5 行
去重后 09-30 收盘价 = [9.9]     （同日同分钟取新值 ✅）
10-08 行数 = 2                  （新窗口已追加 ✅）
是否按 date,time 排序: True ✅
坏文件场景 → 返回 3 行 + 打印告警（不静默丢历史）
```

前端：`arm` 源已出现在选股日历的来源下拉
（`['全部来源','arm','notify','preselected','survey_gate','survey_raw']`，0 console error）。

---

## 4. 边界与遗留

1. **baostock 当日不可达**：`www.baostock.com:80` 可达（HTTP 301），但数据接口登录报
   `10002007 网络接收错误 / [Errno 32] Broken pipe`，清空所有代理后仍失败 ⇒ 判定为上游当日故障。
   **真实的每日增量抓取尚未端到端跑通**，`--merge` 只做了离线语义验证。
2. `latest_trade_date()` 取自**日线**。所以「日线先更新、再补分时」的顺序是硬约束；
   若某天 `daily_bars` 阶段失败，分时也不会补（这是有意的：宁可不补，也不要用过期基准）。
3. `--refresh` 每次都全量扫 4,533 个 parquet 的 `date` 列（约 10~15 秒）来判断谁落后。
   量更大时应改成维护一份 `code → last_date` 索引（未做）。
4. `com.quant.cn-pipeline` 的 `RunAtLoad=true` 意味着**加载那一刻若已过 16:00 且当天没跑过，会立即启动**
   （本次就是这样触发的）。不想要这个行为就把 `RunAtLoad` 改成 `false`。
5. 另外 4 个 repair 型 LaunchAgent（`cn-feature-realign` / `panel-rescore` / `cn-reselect` /
   `financial-backfill`）**仍未加载** —— 它们是 2026-09-26 那次数据修复的一次性任务，
   本轮的判断是**保持不加载**（跑完且产物已在），没有动它们。
6. `arm_selection` 进日历后台账里只有 **1 个决策日 / 10 行**（历史只有今天这一次归档）；
   要形成可回看的曲线需要累积若干天。
