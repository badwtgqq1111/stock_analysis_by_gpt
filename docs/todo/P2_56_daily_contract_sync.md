# P2.56 每日名单刷新契约：为什么"不是所有策略的选股都更新"，以及怎么让它必然更新

> 用户（2026-10-13）："为什么每日自动化后，不是所有策略的选股都更新呢，包括股票日历 / 选股结果 /
> K 线自选 等，理论上所有的前端相关的都应该更新才对啊，为什么这么多不同步呢？能所有的都更新么？
> 数据太分散，还是该消费对象没有消费数据？"

## 0. 回答（先给结论）

**两层原因同时成立，而且第二层是第一层的放大器。**

1. **生产侧"数据太分散"**：每条策略各写各的产物、各在自己的步骤里跑，有的**压根不在每日流程里**；
   没有任何地方声明"这条策略**今天必须有**一份名单"，所以**缺失是静默的**（页面读到旧文件与"今天没更新"长得一样）。
2. **消费侧"该消费对象没有消费数据"**：选股结果 / 选股日历 / K 线自选各读自己的来源，
   没有任何一页以"**每条策略今天都到齐了吗**"为口径 —— 于是没人报错、没人点名。

**外加一次真实事故（本轮直接证据）**：2026-10-12 那次运行在 00:58 后被"编辑正在运行的 bash 脚本"打断
（`launchd.err.log`：`line 243: kfill_cn_financials.py: command not found` / `line 244: syntax error`），
`run.log` 停在 `paper_outcomes ok`、**没有 `production.done`** ⇒ 当天 arm/jq/rot/台账/日历/AI/导出/绩效/归因/龙虎榜
**全部没跑**。所以那天"不全更"不是错觉。

## 1. 逐条根因（都有证据）

| # | 现象 | 证据 | 处置 |
|---|---|---|---|
| 1 | 10-12 当天多数下游没跑 | `run.log` 无 `production.done`；只有 `preflight` 后的 `paper_outcomes ok` | 运行期护栏 `scripts/daily_runner_guard.sh`（`runner_guard_boot` **自身快照 + exec**：改源文件不再影响正在跑的那趟）+ `code_anchor.json` 记账 |
| 2 | 基准指数不在每日流程 | `assets/data/meta/cn_benchmark_daily.parquet` 停在 10-09 | 新增每日步骤 `run_data_step benchmark`（`scripts/backfill_cn_benchmark.py`），已回补 4 指数到 10-12 |
| 3 | sleeve v4 面板未刷新 ⇒ `arm_select` 选不出 10-12 | `cn_sleeve_features_long_v4.parquet` 停在 10-09 | 手工按顺序跑 5 步把面板推到 10-12，`arm_select` 随即更新 |
| 4 | **每条策略各写各的，没有契约** | 10 条策略各自的产物路径/history 目录都不一样 | `config/strategies.json` 每条新增 `daily_refresh`（`mode: run\|archive\|skip` + `max_minutes` + `empty_is_ok` + `reason`）；`scripts/refresh_strategy_lists.py` 统一执行 + 校验 + 归档 + 落 `status.json` |
| 5 | **没有统一消费端** | 页面各读各的文件 | 后端一处合成：`GET /api/strategies/lists-status`；`GET /api/strategies` 每条策略带 `refresh`、顶层带 `lists_status`；前端新增共享组件 `StrategyListsStatusBar.vue`，**策略池 / 选股结果 / 选股日历 / K 线自选** 四页都消费同一份 |
| 6 | 体检不点名名单刷新 | `audit_web_freshness.py` 没有这一行 | 新增「策略名单（每日刷新契约）」行（有 stale/failed/timeout 才算不达标）；`data_completeness_report.py` 新增「基准指数」行 |
| 7 | 同花顺导出默认取 `paper_selection_history` 的 max（落后就导旧日期） | `export_ths_watchlist.py` | 默认改为**最新交易日** |

## 2. 契约（这就是"能不能全部都更新"的答案）

**契约：每条策略每天都该有「顶层名单 + `history/<日期>.csv`」，或明确声明 `skip`。**

`scripts/refresh_strategy_lists.py` 逐条执行并给出**七态**（不是"成功/失败"两态，否则事件型策略每天都会被误报）：

| 状态 | 含义 | 是否算不达标 |
|---|---|---|
| `updated` | 命令跑完且名单日期前进了 | 否 |
| `already_fresh` | 命令跑完，名单已是最新交易日 | 否 |
| `no_signal` | 命令**跑成功**，但当日无信号（`empty_is_ok` 声明的事件/结构型：`first_ban_lowxi`、`box_arm`） | 否（正常） |
| `stale` | 名单日期**落后于**最新交易日 | **是** |
| `timeout` | 超过 `max_minutes` 未完成 | **是** |
| `failed` | 命令退出码非 0 | **是** |
| `skipped` | 声明为按需/无独立名单（影子对照 `cn_ensemble_pk_legacy`） | 否 |

产物：`output/results_cn/strategy_lists/{status.json,STATUS.md}`；每日流程新增非阻塞步骤 `run_data_step strategy_lists`（在台账之前）。

## 3. 前端：一处状态，四处消费

* 后端：`GET /api/strategies/lists-status`（`available/ok/attention/quiet/counts/rows`）+ `GET /api/strategies` 内联同一份。
* 组件：`frontend/src/components/StrategyListsStatusBar.vue`（按"需要关注 → 无信号 → 已更新 → 跳过"排序，落后的排最前）。
* 页面：
  * 策略池：顶部状态条 + 左侧每条策略的刷新徽标 + 详情「每日名单刷新」一行（名单日期 / 历史日期 / 条数 / 耗时 / 原因）；
  * 选股结果：页面第一屏状态条（这份名单属于哪条策略、更新到哪天）；
  * 选股日历：状态条（日历里每一天都是这些名单算出来的，名单停更一眼可见）；
  * K 线自选：侧栏状态条 + **分组 chip 上的 `✅/➖/⚠️/⏱️/❌` 标记**（`watchlistStrategies.refreshMark`），分组提示行显示 `刷新 <status>（<日期>）`。

## 4. 实测（本轮结束时）

```
[lists] counts={'updated': 0, 'already_fresh': 7, 'no_signal': 2, 'stale': 0, 'timeout': 0, 'failed': 0, 'skipped': 1}
```

* `already_fresh 7`：实盘镜像 / 模型策略 / 小市值手臂 / 行业轮动 / jq 多因子（低市值+高ROE）/ jq 动态诊断（ROE+0.25×ROA）/ rot_arm —— 名单日期均 **2026-10-12**；
* `no_signal 2`：`first_ban_lowxi`（最近名单 10-08）、`box_arm`（10-09）—— 命令成功、当日无票，**不是缺刷新**；
* `skipped 1`：`cn_ensemble_pk_legacy`（影子对照，无独立名单）；
* `audit_web_freshness.py --strict` **全绿**；全量预检 **42 个测试文件 / 329 passed**。

## 5. 边界（不写进"可信"的部分）

* 分时 5 分钟：`已到 2026-10-12: 2888/4519 只 / 真实落后 1631 只（阈值 ≤4）` —— 这是**真实数据缺口**，
  由 `com.quant.intraday-sync`（6 小时一次）慢慢补，与"名单刷新契约"无关；
* 复权因子：按日快照 9 天 + 逐股 1212 只，仍在后台补齐；
* `no_signal` 依赖 `empty_is_ok` 声明正确 —— 声明错会把"真故障"降级成"正常"。测试锁住了"未声明就判 `stale`"这条。
