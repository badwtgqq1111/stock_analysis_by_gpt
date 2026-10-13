# P2.58 K 线页左侧自选「名称看不见」——一行 flex 里的列优先级

> 用户（2026-10-13）："k 线图左侧自选股票名称看不见"

## 0. 现场（同一构建、同一视口 1500×1100，Playwright 量 DOM）

```
【修复前】83 行里 35 行名称宽度 <12px（其中不少是 0px，连省略号都没有）；最小宽度 5px
   300229.SZ name='拓尔思'   w=11   tag='实盘镜像（真实持仓）'
   605499.SH name='东鹏饮料' w= 5   tag='实盘镜像（真实持仓） +2'
   ...
【修复后】83 行里 0 行；最小宽度 55px（w == scrollWidth ⇒ 没有被省略）
```

（最初的实测是 **42/83 行**：当时一只票命中多条策略时，标签会把每个策略名都用 ` · ` 拼起来，
标签更长 ⇒ 名称被压得更狠。现在标签只留"首个策略 +N"，所以是 35/83。）

## 1. 根因：`.name` 写 `flex: 1`，它成了**第一个被压到 0px** 的列

```css
.rows li  { display: flex; gap: 6px; }          /* code | name | tag | live价 | ★ */
.rows .name { flex: 1; ... }                    /* = flex: 1 1 0%  ← 罪魁 */
.rows .tag  { max-width: 40%; }                 /* 不可压缩，且可能很长 */
.live       { margin-left: auto; flex 默认 0 1 auto; }  /* 价格+涨跌+"实时" 也不缩 */
```

`flex: 1` 等价于 `flex: 1 1 0%`（基准 0）：当 `code + tag + live` 之和接近行宽（346px）时，
剩余空间为 0，名称列被压成 0 宽 —— `text-overflow: ellipsis` 在 0 宽下什么都不显示。
所以"有的行看得见、有的看不见"完全取决于**来源标签有多长、价格数字有多宽**，
而不是这只票有没有名称（后端 `/api/selection` 一直返回正确的 `stock_name`，实测 4/4 行齐全）。

## 2. 修法（3 处，都在前端）

1. `StockWatchlist.vue` 样式：名称列给下限，**让标签去收缩**：
   ```css
   .rows .code { flex: 0 0 auto; }
   .rows .name { flex: 1 1 auto; min-width: 4.6em; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
   .rows .tag  { flex: 0 1 auto; min-width: 0; max-width: 34%; overflow: hidden; text-overflow: ellipsis; }
   .live       { flex: 0 0 auto; }  .live .src { max-width: 3.4em; ... }
   ```
2. `watchlistStrategies.ts`：一只票命中多条策略时，标签由 `策略A · 策略B · 策略C` 改成 `策略A +2`
   （完整信息在 title 里）—— 这既是可读性，也是把名称挤没的第二个原因。
3. `StockWatchlist.vue` 模板：行上挂 `:title="代码 名称 · 标签"`，被省略时鼠标悬停能看到全称/来源，
   名称缺失时显示"（缺名称）"而不是空白。

## 3. 回归锁

`test/test_kline_watchlist_layout.py`（4 条，进每日预检）：名称列必须有 `min-width` 且不得再用 `flex: 1;`；
标签列必须可收缩；行上必须有 title；标签拼接必须被 `+N` 收敛。

## 4. 边界

* 面板宽度只有 ~346px，名称列的下限是 4.6em（≈55px，2~3 个汉字）：极长的名称（如"中国石油化工"）
  仍会省略号截断 + title 兜底，这是设计（不换行、不推挤价格列）。
* 手机上（更窄）同样受益，但没有单独实测断点。
