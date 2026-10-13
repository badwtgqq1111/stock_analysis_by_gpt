# P2.54 每日自动化审计（2026-10-12）

触发（用户原话）：**"另外看一下每日自动化有什么问题？"**。
方法：读最近三次真实运行的 `output/pipeline_reports/daily/*/run.log` + 各阶段日志 + `agent_runs/*-daily/run.json`，
并对可疑点**单跑复现**（不是只看日志结论）。

## 0. 结论先行

自动化**在正常出结果**（10-10、10-11 都跑到 `done:`），但有 **6 个真问题**，其中 4 个本轮已修（有测试锁），
2 个是机制/环境层面的（1 个已做成护栏但**因正在运行而暂缓接入**，1 个需要你在飞书开放平台点一下）。

| # | 问题 | 证据 | 状态 |
|---|---|---|---|
| 1 | **飞书通知连续两天失败**，但邮件其实发出去了，日志却写 `notify FAILED`（全或无语义） | `run.log`：`notify FAILED`（10-10、10-11 各一次）；单跑复现：`FAIL feishu: …` + `ok email: sent` | ✅ 已修（PARTIAL 语义 + 错误码提示） |
| 2 | 飞书失败的**真因被吞掉**：lark-cli 把结果写 **stderr**，还把"有新版本"的提示 JSON 混在里面 ⇒ 报错显示成 `None: "update": {…}` | 单跑 `lark-cli im +messages-send --as bot --user-id …` → `{"ok": false, "error": {"code": 230013, "message": "Bot has NO availability to this user."}}` | ✅ 已修（解析器 + 230013/99991663/99991672 的可执行提示）；⚠️ **需要你在开放平台把机器人可用范围加上该用户**，或给个群 `chat_id`/webhook |
| 3 | **attribution 连续两天失败**：`explain_selection.py` 硬取 `panel["trade_date"]`，而某些读取路径只回 `trade_date_clean` ⇒ `KeyError: 'trade_date'` | `attribution.log` 栈：`panel["trade_date"] → KeyError` | ✅ 已修（两个列名都认 + 取不到直说） |
| 4 | **分时体检 1/4523 只落后就判"未达标"** | `data_completeness.md`：`已到 2026-10-09: 4522/4523 只 / 真实落后 1 只`，仍列 `未达标 1/11` | ✅ 已修（阈值 = max(3 只, 0.1%)，阈值写进 detail） |
| 5 | **fundamental 阶段因 17/5224 行失败而整体 FAILED**（中继 429/503/504） | `data_fundamental.log`：`success=5207 rows=11780574 failed=17` → `[ERROR] stage=fundamental: 网络接收错误` | ⏳ 已定位，**修法已定**（部分失败容忍 ≤0.5% 记 warn + 缺口写进 data-completeness）；需要一次 A/B 验证后再改 `run_cn_pipeline.py` 的判定 |
| 6 | **不能改运行中的脚本**（运行中的 bash 脚本按字节偏移续读）（bash 按字节偏移续读；今晚这次运行期间我确实改过它）；且**这次跑的是哪版代码**没有锚点 | 20:54 时 79193 号进程仍在跑；`run.log` 只有时间戳，没有 `git head`/脏文件数 | ✅ 护栏已写（`scripts/daily_runner_guard.sh`：自身快照+exec、代码锚点、进度行过滤）；**因运行中暂缓接入**，`--install` 会拒绝并在 done 后可一键接入 |
| 7 | 日志噪声：tqdm 进度条按 `\r` 刷屏，单阶段日志到 1.6MB | `daily_bars.log` 1.69MB、`data_fundamental.log` 1.03MB | ✅ 已修（`runner_filter_progress`，有测试） |

顺带记录两个观察（不是 bug，但要知道）：
* **整趟要 4h50m~5h04m**，从 20:30/22:00 跑到次日 01:35/02:54 → 意味着"当天"的产物凌晨才齐；
* **锁文件**：`taking over stale lock (holder pid=… alive=0 age=1812206s)` —— 21 天前的锁才被接管（判定逻辑本身是对的：alive=0）。

## 1. 本轮改了什么（可复算）

```bash
# ① 通知语义 + 飞书错误码（scripts/notify_selection.py）
uv run python scripts/notify_selection.py --trade-date 2026-10-11
# ok   email: sent to … via smtp.qq.com:465 (ssl)
# FAIL feishu: unknown(code=230013): Bot has NO availability to this user.  ← 机器人在该用户不可用（开放平台 → 应用 → 可用范围；或改用群 chat_id / webhook）
# PARTIAL: 已送达 ['email']；失败 ['feishu']（至少一个渠道成功 ⇒ 视为通知成功，退出码 0）
# ② 归因列名（scripts/explain_selection.py）：trade_date / trade_date_clean 都认
# ③ 分时阈值（scripts/data_completeness_report.py）：ok = lag <= max(3, 0.1%)
# ④ 运行期护栏（新增 scripts/daily_runner_guard.sh）：
bash scripts/daily_runner_guard.sh --check      # 谁在跑 / 锁 / 代码锚点 / 快照
bash scripts/daily_runner_guard.sh --install    # 幂等接入；有任务在跑时**拒绝**（exit 2）
```

护栏做三件事：**自身快照 + exec**（编辑源文件不再影响正在跑的那趟）、
**代码锚点**（`code_anchor.json`：git head + 脏文件数 + 脚本 sha256）、
**进度行过滤**（`runner_filter_progress`）。

## 2. 待办（按优先级）

1. **接入护栏**：等今晚这次 `done:` 之后跑 `bash scripts/daily_runner_guard.sh --install`（幂等，会插到 `log()` 之前）；
   之后每次运行都会留 `code_anchor.json`，且"跑的是哪版代码"可核对；
2. **飞书**：开放平台把机器人可用范围加上目标用户（或改用群机器人 webhook：`WECOM_WEBHOOK_URL` 现在也没配）；
3. **fundamental 容忍**：先跑一次带 `--tolerance` 的 A/B（或直接看 `failed/total ≤ 0.5%` 时把 stage 判成 warn），
   并把缺口行写进 `data-completeness`；
4. **把整趟时间压下来**：`daily_bars` 40 分钟 + `adj_factor` 14 分钟是两头；可考虑把这两步**提前到收盘后立刻跑**（与其余阶段解耦），
   让人在下班前就能看到当日名单；
5. 锁的 stale 接管加一行 `notify_alert`（现在是静默 log）。
