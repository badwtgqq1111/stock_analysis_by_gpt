# P2-26 前端整体规划与优化（含 2026-10-08 白屏事故复盘）

> 触发：用户反馈「前端 web 打不开了」+「看看前端怎么重新整体规划并优化一下」。
> 配套证据：`artifacts/frontend_replan/`（VERIFICATION.txt / evidence/ui/*.png / evidence/ui_check_all_pages.json）。

---

## 0. 事故复盘：为什么会白屏

**现象**：浏览器打开 `http://localhost:8000/` 白屏。

**根因链条（逐环实测，不是推测）**：

1. `frontend/dist/index.html` **入库**，但它引用的 `frontend/dist/assets/*` **不入库**。
   为什么？`.gitignore` 第 2 行写的是 `assets/` —— 这个模式**没有前导 `/`，会在任意层级匹配**，
   本意是忽略仓库根的数据目录 `stock_analysis_by_gpt/assets/`，却连 `frontend/dist/assets/` 一起忽略了。
   ```
   $ git check-ignore -v frontend/dist/assets/index-BXtpGbFj.js
   .gitignore:2:assets/	frontend/dist/assets/index-BXtpGbFj.js
   $ git ls-files frontend/dist
   frontend/dist/index.html          ← 只有入口入库
   ```
2. Vite 出包时文件名带内容哈希，`emptyOutDir` 会**删掉上一轮的文件**。
   所以任何一次 `bun run build`，入口 html 里的 hash 都会变，旧 hash 文件立刻消失。
3. 静态响应**没有任何 `Cache-Control`**（只有 ETag / Last-Modified）。
   ```
   $ curl -D - -o /dev/null http://127.0.0.1:8000/          # 事故时
   HTTP/1.1 200 OK
   etag: "8e12a9624b218012f5e4db61cca8c9df"
   （没有 cache-control）
   ```
   浏览器对"无 Cache-Control 的 200"会做**启发式缓存**：缓存住旧的 `index.html`，不再回源。
4. 于是浏览器拿着旧入口 → 请求上一轮的 `/assets/index-<旧hash>.js` → **404** → 白屏。
   ```
   $ curl -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/assets/index-CKwBPznR.js
   404
   ```

**一句话**：*入库的入口 + 不入库的 bundle + 无缓存策略 = 只要重建一次就可能白屏。*

还有一个伴生缺陷：`app.mount("/", StaticFiles(...))` 是"挂载"，会把之后注册的路径吞掉
（仓库注释里记着 `/health` 一度变 404 就是踩了这个坑），而且它对"文件缺失"没有任何告警。

---

## 1. 现状盘点（改动前的量化证据）

| 指标 | 改动前 | 说明 |
|---|---|---|
| 页面 / 组件 | 7 页 + 8 组件 | 全部手写 `<style scoped>` |
| `main.css` | **37 行**，只有颜色变量 | 没有任何共享组件类 |
| 重复定义的选择器 | `.page` ×7、`.page-title` ×7、`.data-table` ×6、`.empty-state` ×4、柱状图样式 ×3 | 每次都近似但不完全相同（例如 `.data-table th` 有的左对齐有的右对齐） |
| 共享 UI 组件 | **0** | 空态/加载/错误三态在每页各写一遍 |
| API 层 | 241 行，`axios.create` 裸用 | **无拦截器**、无统一错误类型；每页各写 try/catch，写漏的地方就是未捕获 rejection |
| 导航 | 7 个平铺链接 | 品牌写"港股量化分析系统"，但主工作流早已是 A 股 |
| `index.html` 标题 | `K线图表 - 港股量化分析` | 与内容不符 |
| 构建产物缓存策略 | 无 | 见 §0 |
| 构建产物自检 | 无 | 白屏只能靠人眼发现 |
| 已知口径问题 | 「选股结果」页读的是港股 `output/lightgbm_top10_selected.csv` | 与 CN 流水线名单口径不一致（旧遗留 #5） |
| 顺带修掉的一个隐藏 bug | `FactorIC.vue` 用了 `var(--accent-green)`，而 `:root` 里**从未定义过** `--accent-green` | `.bar-ic` 的柱子一直是透明/默认色 |

---

## 2. 目标架构

```
frontend/src/
├── styles/main.css          ← 【单一来源】设计令牌 + 组件类 + 兼容别名
├── api/
│   ├── client.ts            ← axios 实例（拦截器统一错误）+ 各业务接口
│   └── health.ts            ← 探活，**不依赖 axios**（避免污染入口 chunk）
├── composables/
│   ├── useAsync.ts          ← loading / error / data 三态 + 重试
│   ├── useKlineData.ts      ← 已有
│   └── useStockList.ts      ← 已有
├── components/
│   ├── ui/                  ← 【新】通用件：PageHeader / StateBlock / StatTile / BarChart
│   └── *.vue                ← 业务组件（K 线、筹码、选股器…）
├── pages/                   ← 页面只留"本页特有布局"
├── router/
└── types/
```

三条硬规则：
1. **颜色/间距/圆角/字号只能来自 `--token`**，页面里不许写死。
2. **`.card/.panel/.data-table/.btn*/.tag/.pill/.tabs/.state-block/.bar-*` 是全局组件类**，
   页面不要再 scoped 覆写（否则又回到"7 份近似实现"）。
3. **取数一律走 `useAsync`**，页面不允许出现裸 `await api.xxx()` 而没有 catch。

---

## 3. 本轮已落地（P0 + P1 的第一刀）

### 3.1 事故修复（P0）

| 改动 | 文件 |
|---|---|
| `.gitignore` `assets/` → `/assets/`，并显式加 `frontend/dist/`；`git rm --cached frontend/dist/index.html` | `.gitignore` |
| 弃用 `app.mount`，改为显式路由 + **分流缓存策略**：`/assets/<hash>` → `public, max-age=31536000, immutable`；`index.html` → `no-cache, must-revalidate` | `backend/main.py` |
| **自己实现条件请求**（Starlette 1.0.1 的 `FileResponse` 只处理 Range，**不实现 304**）→ 现在 `If-None-Match` / `If-Modified-Since` 都返回 304 | `backend/main.py` |
| **启动自检**：解析入口 html 引用的 `/assets/*` 并逐个验证存在，缺失就打印修复命令 | `backend/main.py` |
| `/health` 暴露自检结果；前端导航右侧状态点 + 顶部红色告警条 | `backend/main.py`、`App.vue` |
| 未匹配的 `/api/*` 返回 404 JSON（以前会被 SPA 回退成一张 HTML 200）；静态路由防目录穿越 | `backend/main.py` |

### 3.2 设计系统（P1）

| 改动 | 结果 |
|---|---|
| `main.css` 37 → **415 行**：令牌（间距/圆角/字号/语义色/层级/阴影）+ 组件类 + 兼容别名 | 7 份重复 CSS 收敛为 1 份 |
| 新增 `components/ui/`：`PageHeader` / `StateBlock` / `StatTile` / `BarChart` | 空态、加载态、评分榜、SHAP 图首次有统一实现 |
| `api/client.ts`：加响应拦截器 + `ApiError`（超时/无响应/HTTP 状态/后端 detail 四类翻译成人话） | 页面拿到的永远是可读 message |
| 新增 `composables/useAsync.ts` | 三态 + 重试，消灭各页重复的 try/catch |
| `App.vue`：导航按工作流分组（选股/研究/数据/图表·回测）、品牌与标题改成"A 股量化研究系统"、`<Suspense>` 骨架、健康状态点 | 见 `evidence/ui/pipeline.png` |
| 6 个页面的 scoped CSS 大幅缩减（Pipeline 5103→1762 B、StockSelection 2403→414 B、KlinePage 1588→0 B …） | 见 `diffs/` |

### 3.3 本轮验收（Playwright，7 页全绿）

```
tabs/pages: selection / factor_ic / kline / portfolio / factor_mining / pipeline / calendar
doc.title  = "A 股量化研究系统"（7/7）
nav        = ['选股结果','选股日历','因子 IC','因子挖掘','数据流水线','K 线图表','组合回测']
console errors = 0（7 页合计）
HTTP >= 400    = 0（7 页合计）
```

### 3.4 构建体积（诚实记录：首屏 JS 小幅变大）

| 产物 | 改动前 | 改动后 | 说明 |
|---|---|---|---|
| 入口 `index-*.js` | 95.98 kB (gz 37.72) | **103.03 kB** (gz 40.50) | +7 kB = 新的应用骨架（分组导航 + 健康探活 + Suspense） |
| `client-*.js` | 44.23 kB (gz 17.16) | 44.87 kB (gz 17.55) | 独立 chunk 保住了（`api/health.ts` 用原生 fetch，不把 axios 拖进入口） |
| 共享 `index-*.css` | 1.32 kB | **16.21 kB** (gz 3.87) | 设计系统全量下发；各页 CSS 相应减少（7.6 → 4.5 kB） |
| CSS 合计 | ~8.9 kB | ~20.7 kB (gz ~6 kB) | **源码去重换来的体积增长，值得**；若在意可后续按路由拆分 |

> 一次小事故：中途把 `getHealth` 放在 `client.ts` 里，入口 chunk 直接从 96 kB 涨到 **148 kB**
> （axios 被拽进首屏）。拆到 `api/health.ts` 后回到 103 kB —— 这条经验已写进 `health.ts` 注释。

---

## 4. 后续分期（未做）

### P1（下一步，半天量级）

| # | 事项 | 状态 |
|---|---|---|
| ~~1~~ | ~~「选股结果」页口径统一~~ | ✅ **已完成**（见 `P2_27_selection_sources.md`）：改成**三个数据源可选**（CN 生产名单 / CN 研究手臂 / 港股），页面上摊开文件路径与生成时间；顺带修掉一个"SHAP 区块从来没显示过数据"的老 bug（工单 F4） |
| 2 | 其余页面接入 `useAsync`（当前 `Pipeline`/`SelectionCalendar`/`FactorMining` 仍是手写 try/catch） | 后端停掉时页面显示可读错误 + 重试按钮，而不是"加载中…"卡死 |
| 3 | 流水线控制台：运行历史落盘 + **取消按钮** + 阶段产物地图自动化（旧遗留 #4） | 能取消在跑的阶段；历史不因刷新丢失 |

### P2（结构性）

| # | 事项 |
|---|---|
| 4 | 表格虚拟滚动 / 服务端分页（`SelectionCalendar` 单日 78 行、`FactorIC` 158 行还行，K 线全市场搜索会顶不住） |
| 5 | 图表统一封装（`lightweight-charts` 目前 K 线页 162 kB 独立 chunk，但 IC/组合页各写一份初始化逻辑） |
| 6 | 主题化：令牌已抽好，可加浅色主题（`prefers-color-scheme`） |
| 7 | E2E 冒烟纳入流水线：`evidence/ui_check_all_pages.py` 已可复用，建议挂到 `run_cn_pipeline.py --stage frontend_smoke` |
| 8 | 组件单测（Vitest + @vue/test-utils）：目前前端 0 测试 |

### 工程债（低优先）

- `frontend/dist` 不再入库后，**新克隆必须 `bun install && bun run build`**；启动日志与 `/health` 已给出明确指引。
- `frontend/dist` 的 hash 变更会导致书签失效？不会 —— 用的是 hash 路由，路径不变。

---

## 5. 复现命令

```bash
# 构建
cd frontend && bun install && bun run build

# 起服务（启动日志会打印前端自检结果）
uv run uvicorn backend.main:app --port 8000

# 自检（应返回 ok:true 且 missing 为空）
curl -s http://127.0.0.1:8000/health | python3 -m json.tool

# 缓存策略复验
curl -sI http://127.0.0.1:8000/                      # cache-control: no-cache, must-revalidate
curl -sI http://127.0.0.1:8000/assets/index-*.js     # cache-control: public, max-age=31536000, immutable

# 7 页 E2E 冒烟
uv run python artifacts/frontend_replan/evidence/ui_check_all_pages.py
```


---

## 6. 第二批：选股结果多数据源（同日 第二十一轮）

详见 [P2_27_selection_sources.md](P2_27_selection_sources.md)。要点：

* `/api/selection` 从"只会读一个港股 CSV"改成**数据源注册表**（`backend/services/selection_sources.py`），
  新增 `?source=` 参数；响应带 `source`（文件/生成时间/交易日/是否支持 SHAP）与
  `available`（各源可用性）。未知 source 回退默认源并在 `note` 说明，不报 4xx。
* 三个源：`cn_ensemble`（CN 生产名单，默认）/ `cn_arm`（CN 研究手臂）/ `hk_lightgbm`（港股，遗留）。
* 页面按源切换，**数据源指纹写在页面上**；SHAP/重要性区块按 `supports_shap` 分支，
  非港股源显示解释 + "切到港股源"按钮，而不是假装空图。
* 顺带修掉两处叠加的老 bug（工单 F4）：`factor_explanation` 里夹了裸 `nan` 让
  `ast.literal_eval` 抛异常；且 `get_shap` 在已嵌套的 dict 上又取了一层 `stock_shap_values`
  ⇒ **SHAP 区块从来没显示过任何数据**。
* 还修了港股名称一直为空的问题（`stock_info_registry` 本机查不到，改用
  `docs/hk_entity_alias_registry.csv`）。

### 6.1 本轮追加的体积变化

| 产物 | 第二十轮 | 本轮 |
|---|---|---|
| `StockSelection-*.js` | 5.09 kB | **10.10 kB**（多源切换 + 统一表格/图表组件） |
| `index-*.js` | 103.03 kB | 103.56 kB |
| `client-*.js` | 44.87 kB | 44.90 kB |
| 共享 `index-*.css` | 16.21 kB | 16.21 kB（未变） |
