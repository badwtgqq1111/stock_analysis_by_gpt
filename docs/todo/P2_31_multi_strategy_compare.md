# P2-31 多策略对比 · 名称/去重 · 面板链路 · 横竖屏 · 开机自启（2026-10-09）

> 触发：用户要求「要开机自启；选股日历 preselect 没有股票名称；preselect/notify 重复；
> 每日选股结果应该是多策略展示对比（比如没有 arm_select）；手机要支持横竖屏切换」。
> 证据：`artifacts/mobile_compare_and_autostart/`。

---

## 1. 先说最值钱的发现：arm_select 一直在选「上个月末」的名单

用户问"比如没有 arm_select"，顺着查下去挖到**两层根因**：

### 第一层：脚本按月末快照选股

`select_latest_via_arm.py` 用 `month_end_snapshot()` 取最后一个月末。
10 月没过完 ⇒ 选出来的永远是 **9 月末**名单（实测 `arm_latest_selection.csv`
的 `trade_date` = `2026-09-30`）。

修：加 `--snapshot {latest,month_end}`，默认 `latest`（面板最新交易日 = 每日口径）。
顺带修训练窗口：原来 `train = [m for m in months if m < months[-1]]`，
日频口径下 `months[-1]` 就是上个月末，再排除它等于**连上个月的数据都丢了**；
改成按**选股日所在月**切。

### 第二层（更根本）：面板链路的 `--end` 是硬编码的

```
$ grep -n 'default="2026-09-30"' scripts/build_sleeve_features_long.py
140:    parser.add_argument("--end", default="2026-09-30")
```

⇒ **无论什么时候重建**，v1..v4 五份产物都停在 2026-09-30。
实测 2026-10-09 重建一次，五份产物最新交易日全是 2026-09-30 —— 这一步骗过了一次排查。

修：`--end` 默认当天；并把 v1→v2→v3→v3i→v4 全链路接进每日流程
（放在 `arm_selection` **之前**，非致命）。**全链路只要 73 秒**
（v1 27s / v2 10s / v3 14s / v3i 7s / v4 15s），相对 7 小时主流程可忽略。

---

## 2. preselect 没有名称 + preselect/notify 重复

**名称**：两处都要修，只修一处不够。
- 源头：`read_snapshots()` 写死 `"name": None`（preselected.csv 里只有代码）→ 从 `cn_stock_basic` 补。
- API：`/api/calendar/day` 的回填有逻辑 bug —— 只拿**已经有名字的代码**去查注册表，
  而且用 `setdefault` 不覆盖 ⇒ 没名字的行**永远补不上**。改用"所有缺名字的代码"。

**重复**：同一只票在 `preselected`（候选池）与 `notify`（PK 后最终名单）里各占一行，
2026-10-08 就是 8 只票 = 16 行。

---

## 3. 多策略对比视图

`/api/calendar/day` 新增 `strategies` + `compare`：**一只票一行 × 每个策略一列**。

```
strategies: ['arm', 'notify', 'preselected']
平铺 26 行 → 去重后 18 只票
  003018.SZ 金富科技  {'notify': '#1', 'preselected': '#1'}
  603007.SH 顺景科技  {'arm': '#1'}
```

前端两个 tab：**多策略对比（默认）** / 明细列表。命中的格子给底纹，
一眼看出"哪只票被几个策略同时选中"。来源图例补 `A=arm（研究手臂）`。

> ⚠️ 口径提醒：`arm` 用的是 sleeve 面板 v4，生产名单用的是 clean panel，
> 两者**不是同一口径下的可比策略**，别把"手臂选中/生产没选"直接读成优劣。

---

## 4. 手机横竖屏

横屏宽 ~844px **超过 720px**，命中不了竖屏那套断点；而高度只有 390px ⇒
导航换行占 **81px（21% 屏高）**。

加 `@media (max-height: 500px) and (max-width: 1100px)`：导航压成一行横滚、
页头回横排、说明文字限 2 行、间距收紧 ⇒ 导航占屏 **21% → 9%**。

**踩坑**：第一版写进全局 `main.css` 完全没生效 —— `.app-nav { flex-wrap: wrap }`
声明在 `App.vue` 的 **scoped** 块里，选择器带 `[data-v-xxx]` 优先级更高，全局被压过去。
必须写进 App.vue 的 scoped 块。

---

## 5. 开机自启

新增 `~/Library/LaunchAgents/com.quant.web.plist`：
`RunAtLoad=true`（开机/登录即起）+ `KeepAlive=true`（崩了自动拉起），跑
`scripts/serve_web.sh`（HOST=0.0.0.0 / PORT=8000）。

---

## 6. 遗留

1. `arm` 与生产名单两套特征口径不同，对比视图是并列展示、不是同口径比较。
2. 面板链路只花 73 秒，但读的是日线；某天 `daily_bars` 失败则面板停在前一天（非致命，次日补）。
3. `--snapshot latest` 改变了研究默认口径；做月度研究要显式 `--snapshot month_end`。
4. 对比视图里 `arm` 只有 2 天历史（2026-09-30 / 2026-10-08）—— 更早的没归档。
5. 手机端仍是自适应而非专用布局；横屏下对比表列多，仍需横向滚动。
