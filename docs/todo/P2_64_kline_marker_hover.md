# P2.64 K 线「入选记号」可悬停 + 构建戳失真修复

> 用户（2026-10-15）："继续完善优化吧"（承接第九十六轮给出的三个方向，取 ① 图表上的入选记号做成可 hover，
> 并顺手修掉本轮实测撞见的构建戳问题）

## 0. 现场（同一构建、同一视口 1680×1200，Playwright 实测）

第九十六轮把"入选日历"搬进了 K 线页（迷你日历条 + 悬停明细 + 选股记录点行跳转），
但**图上的金色"入选"箭头依然是死的**：它是 lightweight-charts 的 canvas marker，
没有 DOM、没有 hover 事件、点它也不会告诉你"那天是谁选的"。用户只能把鼠标移到图**上面**的
迷你日历条里逐个格子找。本轮要解决的就是这条：

```
【修复前】图表区域可交互元素：0 个（marker 在 canvas 里，只有 tooltip 的 OHLC）
【修复后】.sel-hit 命中区 2 个（随区间变化，全部 K 线时 5 个），悬停浮层文本：
         2026-10-09 ｜ 入选 1 条 ｜ 当日全部 → ｜ 行业轮动（概念/板块手臂） ｜ rank 6 ｜ 权重 20.0% ｜ T+1 收益 +1.70%
```

## 1. 做法：不用"叠加竖线 series"，而是给 marker 铺一层 DOM 命中区

`lightweight-charts` 的 marker 本身不可交互（官方没有 marker hover API），三条可选路线：

| 路线 | 能悬停 | 代价 |
|---|---|---|
| A 叠加一条"入选竖线" series | ❌ 还是 canvas，仍然没有事件 | 还得再写一套命中判定 |
| B 用 crosshair 回调自己算最近 marker | ✅ | 只覆盖蜡烛窗格；marker 画在窗外（下方）时会闪断 |
| **C 在图底部铺透明 DOM 命中区（本轮采用）** | ✅ | 需要在时间轴变化时重算 x（`timeToCoordinate`） |

选 C 的理由：命中区是**真 DOM**，因此 `:hover`、`:focus-visible`、`title`、`aria-label`
全部免费得到；键盘 Tab 也能到（无障碍路径与鼠标路径同源）。

```ts
// KlineChart.vue
chart.timeScale().subscribeVisibleTimeRangeChange(() => { updatePriceRange(); updateHitboxes() })

function updateHitboxes() {
  for (const m of props.selectionMarks) {
    if (!dataMap[m.date]) continue                      // 这天的 K 线不在当前区间 ⇒ 不铺
    const x = chart.timeScale().timeToCoordinate(m.date as Time)
    ...
  }
  // 命中区半宽 = 相邻入选日像素间距 / 2（保底 6px、封顶 14px）⇒ 入选日密集时不会互相盖住
  hitHalfWidth = Math.max(6, Math.min(14, Number.isFinite(minGap) ? minGap / 2 : 14))
}
```

**踩过的两个真坑（都是实测出来的，不是推理）**：

1. **命中区的鼠标事件没有可靠坐标**：`mouseenter` 走 `crosshairMove` 时 `param.point` 是 `undefined`，
   直接用会把浮层算到页外（实测浮层出现在 `y=54`，看着像飘在导航栏上）。现在：命中区进入 →
   用命中区的 x/y 出卡；`crosshairMove` 带 point 时才重算；`mouseleave` 到没有入选的区域 → 收卡。
2. **【最近入选】按钮指着数组最后一项 ⇒ 指到了 2017-08-16**：台账接口按决策日**倒序**返回，
   `marks[marks.length-1]` 是最老的一条（而且它在当前 K 线区间之外，点了没反应）。
   改成**按日期取最大值**，并且只在"图上真有记号"的日期里挑。

## 2. 顺手修掉：`/health` 的构建戳是**进程启动期快照**（真事故，不是洁癖）

导航栏一直显示 `build 10-15 17:14 · B4_GoZm1`，而磁盘上 `index-B4_GoZm1.js` 早就不存在了、
index.html 引用的是 18:31 构建的 `index-cNKXPK90.js`：

```
$ curl -s localhost:8000/health | jq .frontend
{"refs":["/assets/index-B4_GoZm1.js", ...], "missing":[], "ok":true,
 "built_at":"2026-10-15 17:14:49", "entry":"/assets/index-B4_GoZm1.js"}   # 17:14 的进程快照
$ grep -o '/assets/index-[^"]*js' frontend/dist/index.html
/assets/index-cNKXPK90.js                                                 # 磁盘上的真身
```

根因：`frontend_audit = _audit_dist(frontend_dist)` 只在模块导入时算一次，`/health` 原样返回。
这个戳的**唯一用途**就是回答"我浏览器里跑的是不是最新构建"（2026-10-09 那条事故的守门人），
读快照等于把这个自证工具作废。修法：`/health` 每次请求重算（只做 index.html 的 stat +
目录内 `is_file`，几次 syscall），并补一条回归测试把"快照"钉死：

```python
# test/test_web_cache_and_build.py::test_health_build_stamp_recomputes_after_rebuild
os.utime(index, (original + 7.0, original + 7.0))     # 用"改 mtime"当重建的替身
second = client.get("/health").json()["frontend"]     # 快照实现 → built_at 不变 ⇒ 用例红
```

**反证**（本轮实测）：把 `health()` 换回快照实现，该用例 **FAILED**（`built_at 18:31:25 != 期望的 18:31:32`）；
换回修复版 **6 passed**。

## 3. 前端结构：明细卡抽成共用组件（两个悬停点必须说同一套话）

第九十六轮的浮层文案写在 `KlinePage.vue` 里；本轮图上也出同一张卡，如果各写一份，
迟早出现"图上写 T+5 超额、日历条写 T+5"的不一致。抽成 `SelectionDetailCard.vue`
（`<script>` 块导出 `SelectionRow` 类型 + `strategyLabel/pctText/toneOf` 纯函数，`<script setup>` 负责渲染），
迷你条与图表命中区共用；卡片里加两条纪律：

* **收益口径要自证**：有 `excess_r5` → `T+5 超额 +x%`；只有 `r5` → `T+5 收益 +x%`（并注明"未减基准"）；
  只有 `r1/r20` → 退到该窗口并注明；全空 → `T+5 待结算` + 一句"前瞻窗口未走完，台账里为空，不是 0"。
  （决策日 2026-10-08 之后的行 `r5/excess_r5` 全空 —— 5 个交易日还没走完，这是**正确**的空，不是缺数据。）
* **策略中文名要有兜底**：`/api/strategies` 只登记策略，台账里还有 `preselected/notify/survey_raw/arm`
  这些**口径来源**，只查策略池会退化显示 `preselected`（实测踩到）。新增
  `composables/selectionLabels.ts` 的兜底表（与「选股日历」页 `STRATEGY_LABEL` 同源），
  `mergeStrategyNames(pool)` 合并进来。

## 4. 判据与实测（Playwright，`check_ui_marker_hover.py`，五项全绿）

| # | 判据 | 实测 |
|---|---|---|
| ① | 命中区与入选日一一对应 | `.sel-hit` 2 个（`2026-10-09`、`2025-10-31`），title 带日期+条数；切「全部」区间后 5 个 |
| ② | 悬停出明细卡（中文策略名/rank/权重） | 卡片文本含「行业轮动（概念/板块手臂）｜rank 6｜权重 20.0%」 |
| ③ | 键盘 Tab 聚焦同样出卡 | `pop_on_focus == 1` |
| ④ | 离开入选日 → 卡片收、普通 tooltip 接管 | `pop_after_leave=0`、`tooltip_after_leave=1` |
| ⑤ | 第九十六轮功能不回归 | 翻月 `2026-10 → 2026-09`、「最新」回 `2026-10`、迷你条悬停明细仍在、「最近入选 10-09」一跳到位 |

## 5. 边界（如实）

* 命中区只覆盖**当前 K 线区间内**的入选日；台账里更早的记录（如 300229.SZ 的 2017-08-16）
  需要先把区间切到「全部」才会出现，迷你条的「最近入选」按钮也只指向区间内那一条。
* 入选日之间会画一条金色连线（叠加 series，只在入选日有点）——这是"记号可视化"的副产物，
  跨年时会形成一条长斜线，读作**连线**而非趋势线。
* 命中区压在时间轴那一条（高 28px）上，不与蜡烛/成交量抢事件。

## 6. 复现命令

```bash
cd frontend && npm run build                     # 刷新即生效（本轮改了前端 + backend/main.py）
.venv/bin/python output/verification/p1_112_kline_marker_hover_20261015/check_ui_marker_hover.py
.venv/bin/python -m pytest test/test_web_cache_and_build.py -q
```
