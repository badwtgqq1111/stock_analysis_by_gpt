# P2.55 每日自动化提前到 18:00 + 通知只发 email（2026-10-12）

触发（用户原话）：**"提前到每天 下午6 点跑吧，飞书那个通知不发了，只发 email 吧。"**

## 1. 调度改成 18:00

`~/Library/LaunchAgents/com.quant.cn-pipeline.plist`：

| | 旧 | 新 |
|---|---|---|
| 首触发 | 19:30 | **18:30**（先定 18:00，再按下面的理由挪到 18:30） |
| 重试 | 20:30 / 22:00 | **20:00 / 21:30** |

* 保留"重试"两档：当天成功后写 `production.done`，后续触发自动跳过；正在跑时由脚本自带的锁挡住重复触发。
* **为什么是 18:30 而不是 18:00**：中继通常 17:00~18:30 才把当日日线铺开（且偶发 429/503），
  18:00 起跑会经常撞上"还没铺完"；18:30 起跑把等待时间让给数据源，就绪闸门只在**真的异常**时才等/中止。
* `DAILY_EARLIEST_HOUR=16`（脚本内的最早时刻护栏）不变 —— 18:30 ≥ 16，不会误挡；
  开机后 `RunAtLoad` 在 16:00 之后也会补跑一次。
* **重载方式**：`bash scripts/apply_daily_schedule.sh`（**有任务在跑时拒绝**，因为 `launchctl bootout` 会杀掉那次运行）。
  今晚这次（19:30 起的那趟）仍在跑，所以 plist 已改好但**没有 reload**；`done:` 之后执行上面这条命令即可。

## 2. 18:00 起跑带来的新风险 → 就绪闸门（`scripts/daily_ready_gate.py`）

收盘 15:00 后，中继通常 17:00~18:30 才铺开当日日线，且偶发 429/503。
**若 18:00 就开跑而当日日线还没到位**，下游会安静地按**昨天**取数，连 `web-freshness --strict` 都会"全绿"
（它比的也是昨天）—— 于是你拿到的是昨天的名单却以为今天已经更新。

闸门逻辑（不自己抓数，只**等** + **判**）：
1. 今天不是交易日（或还没过 `--min-hour 15`）⇒ 放行；
2. 读本地日线最大日期（基准指数 + clean 日线取较大者），≥ 今天 ⇒ 放行；
3. 否则每 5 分钟重查，最多 45 分钟（`--wait-minutes 45 --sleep-seconds 300`）；
4. 超时仍落后 ⇒ **exit 1**（中止本次 + `notify_alert` 发邮件），状态写 `output/data_quality/daily_ready_gate.json`。

实测（21:12）：`clean_latest=2026-10-12 / bench_latest=2026-10-09 ⇒ ready`（今晚 `daily_bars` 阶段 20:54 已完成）。
闸门最长等待 45 分钟（18:30 + 45 = 19:15 必须有数据），之后再交给 20:00 / 21:30 的重试触发。

## 3. 通知只发 email

`scripts/notify_selection.py` 的默认渠道 `feishu,email,wecom,dingtalk,generic` → **`email`**：
* 飞书那条当前不可用（lark-cli `code=230013 Bot has NO availability to this user`），默认带着它只会每天多一条 FAIL；
* 想恢复：`NOTIFY_CHANNELS=feishu,email`（或 `--channels feishu,email`），并先在开放平台把机器人可用范围加上该用户；
* 其它渠道（wecom / dingtalk / generic）本来就没配 URL，保持"skip"。
* 本轮同时保留了上一步的 `PARTIAL` 语义与错误码提示（万一以后又开了多渠道路径）。

## 4. 接入步骤（护栏 + 就绪闸门 → 进 run_daily_production.sh）

两块都是**运行期**改动，必须等今晚那次跑完再进（改运行中的 bash 脚本不安全）：

```bash
bash scripts/daily_runner_guard.sh --install     # 幂等；插两块：runner_guard_boot + daily_ready_gate
bash scripts/apply_daily_schedule.sh             # reload 18:00 调度
```
`--install` 会拒绝在运行中执行（实测 exit 2、文件字节不变，有测试锁）。

## 5. 验收

`output/verification/p1_89_schedule_1800_email_only_20261012/verify_schedule.py`：
plist 里有 18:00、没有 19:30；notify 默认 email 且单跑只发邮件（exit 0）；
ready-gate 在"已就绪/非交易日/未就绪"三种情形下的判定；installer 在副本上幂等且只在无运行时生效。
