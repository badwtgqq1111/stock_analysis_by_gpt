# P2-30 选股日历同步 + 手机端适配 + 局域网访问（2026-10-09）

> 触发：用户问「昨天的自动化都完成了么？为什么选股日历没同步更新？另外 web 需要适配手机端，
> 手机先在局域网能查看」。
> 证据：`artifacts/mobile_and_calendar/`（VERIFICATION.txt / evidence/*）。

---

## 1. 昨天的自动化：完成了（17/17），但跑了**两遍**

```
$ ls -la output/pipeline_reports/daily/2026-10-08/production.done
Oct  9 02:31
$ tail -1 .../run.log
2026-10-09 02:31:17 done: 2026-10-08 生产完成
阶段：数据补齐(fundamental 1666s / financials 31s / merge 20s) → 主阶段 11 个
      → 收尾(arm-selection 89s / intraday-refresh 57s / 体检)
整体：19:30:11 → 02:31:17 = 7h01m
```

数据面结果（对比上一轮）：行业分类 09-24 → **10-08 23:43**；分时 428 → **4522/4533**。

**为什么跑了 7 小时？** run.log 里主阶段出现了两组（21:00 一轮、23:48 一轮）。原因是
**我在那轮运行中途编辑了 `run_daily_production.sh`** —— bash 边读边执行，文件偏移错位后
从新插入的「数据补齐」块继续执行，于是又跑了一遍全流程。这正是 P2_29 §5-6 记下的隐患，
这次真的发生了。结果没错（幂等、marker 正常写出），但代价约 3 小时。
**教训固化：绝不在运行中的 bash 脚本上改文件。**

---

## 2. 日历为什么没同步：每日流程里没有「重建台账」这一步

```
$ grep -n "build_selection_ledger" scripts/run_daily_production.sh
（无匹配）
$ ls -la output/results_cn/selection_ledger.parquet
Oct  8 16:57      ← 10-08 手工重建过一次，此后没人再动
```

而 10-08 的输入**早就齐了**：`feishu_2026-10-08.json`（02:29 由 notify 写）
+ `selection_snapshots/2026-10-08/`（两个 run）。

⇒ **不是数据没产出，是没人去合并。**

修法：在每日流程里加台账重建，**放在 notify 之后**（台账要读 notify 写的 feishu 文件），非致命。
手工验证：811 → 827 行、决策日 37 → 38、最新决策日 2026-09-30 → **2026-10-08**。

---

## 3. 手机端适配

### 3.1 修复前的实测问题

| 视口 | 溢出情况 |
|---|---|
| 平板竖屏 820×1180 | calendar **456px**、pipeline 402px、selection 349px |
| 桌面 1280×900 | calendar 8px、automation 15px（宽表把整页撑出横向滚动条） |
| 手机 390×844 | selection 被撑到 **1044px** 视口 |

结构原因：**7 个页面 25 张 `.data-table`，24 张外面没有滚动容器**。

### 3.2 三处修改

1. **表格容器横滚，全宽度生效**（不放在媒体查询里）
   ```css
   .panel, .card, div:has(> .data-table) { overflow-x: auto; }
   ```
   安全性：这些容器都是 `height: auto`，`overflow: auto` 不会产生纵向滚动条。

2. **`.two-col` 翻转方向时必须同时改对齐方式**（最隐蔽的一个）
   它原本是 `align-items: flex-start`（横排做顶对齐）。
   改成 `flex-direction: column` 后，flex-start 变成「交叉轴不拉伸」，
   子项宽度退化成 fit-content → `.col-main` 被宽表撑到 1032px。
   ```css
   .two-col { flex-direction: column; align-items: stretch; }
   ```

3. **手机断点 ≤720px**：页头竖排（横排时按钮把说明挤成每行 2~4 字）、
   导航单行横滚、触摸目标 ≥36px、`.stat-grid` 两列、条状图标签收窄。

### 3.3 验收：8 页 × 5 档视口全部无横向溢出

```
手机 iPhone14 390x844 / 小屏 360x740 / 平板 820x1180 / 桌面 1280x900 / 桌面 1600x1000
→ 全部 无横向溢出 ✅，console/page errors: none
```

---

## 4. 局域网访问

```
$ lsof -nP -iTCP:8000 -sTCP:LISTEN        # 修复前
TCP 127.0.0.1:8000 (LISTEN)               ← 手机连不上
# 修复后
TCP *:8000 (LISTEN)
$ curl -o /dev/null -w "%{http_code}" http://192.168.0.162:8000/   → 200
```

新增 `scripts/serve_web.sh`：默认 `HOST=0.0.0.0`，启动时打印局域网 URL。

**手机打开：http://192.168.0.162:8000**（同一个 Wi-Fi）。
macOS 防火墙当前关闭，不会拦；若开了，首次启动要允许 Python 接受传入连接。

---

## 5. 顺带修掉的 3 个显示 bug

1. **日历上的台账时间少 8 小时**：`pd.Timestamp(mtime, unit="s")` 给的是 UTC
   （本地 08:13 显示成 00:13）。
2. **自动化页把 `--dry-run` 建出的当天空目录当成「今天的运行」**，盖掉了昨天已完成的运行。
   第一次修按「有没有 run.log」过滤**没用**（dry-run 也写 run.log）；
   正确判据是 run.log 里出现过 `dry_run=0`。
3. **跑完耗时还在涨**（显示"已跑 12h51m"）；陈旧锁也被当成活跃锁展示。

---

## 6. 遗留

1. 「数据补齐」块首次跑 28 分钟（fundamental 1666s 是行业分类全量回补）；明天起应是增量。
   若长期 >10 分钟，建议 `DAILY_FUNDAMENTAL=0` 拆到专门的 LaunchAgent。
2. 复权因子未启用、筹码 cyq_chips 未进每日流程（同 P2_29）。
3. 手机端只做**自适应**，没做移动端专用布局（宽表仍是横向滚动查看）。
4. `serve_web.sh` 起的服务无鉴权，只在可信局域网开。
5. 服务没做成 LaunchAgent —— 重启后要手动起（是否开机自启待定）。
