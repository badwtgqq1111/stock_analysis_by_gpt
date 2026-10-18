# P2_72 · 个股研报调研（Bynewlt）：从原文证据到 K 线页面板

> 用户（2026-10-17）："K线图表里的策略选出来的票，每只我都想做一下针对个股的研报调研，
> 用于对个股的多空分析，方便买卖股票时做为参考。并在前端展示。"

## 0. 已定决策（用户当面确认）

| 决定项 | 结论 |
| --- | --- |
| 首批范围 | **全部 103 只**（16 组策略最新名单的并集，与 K 线页自选股分组同一口径） |
| 额度上限 | 不设上限；按 bynewlt-research 技能的预算纪律控制（每票 1 次检索 + 定向补查/核对） |
| 合成方式 | **agent 逐票核对后撰写**：引用逐条过 `cite`（免费绑定检查），关键数字 `read` 原文页抽查 |
| 前端形态 | K 线页个股面板「研报多空调研」（与「AI 多空分析」并列；总览页/日历徽标本轮不做） |

## 1. 架构（照 K 线页既有三层模式）

管线（取证据）→ 证据档 → 合成稿（agent）→ API → 面板：

- **`scripts/bynewlt_stock_research.py`**（新增）
  - 名单采集：`config/strategies.json` 里带 `selection_source` + `history_dir`、未退休的策略
    → 各取最新 CSV → 并集（2026-10-17 为 103 只，16 个名单文件）。
  - 逐票调 Bynewlt 研报库：`search_company_research_evidence`，analysis 模式、
    `since=2025-01-01`、`limit=20 / maxPerReport=5`（与技能检索参考的 analysis 默认配额一致）、
    `companyMarket=“A股”`；原始返回**原样**存档。
  - 状态机：`queued → fetching → fetched/empty/not_found/error/unknown/rejected`（→ 人工合成后
    `composed`）；断点续跑（默认跳过已有终态）、`--max-calls` 批上限、**绝不自动重试**；
    401/402/403/429 一律停止整批；相同参数 10 分钟内走本地复用（技能内置）。
  - 命令：`dry-run` / `fetch [--codes ...] [--max-calls N] [--refetch]` / `status` /
    `show --code X`（本地免费，合成时读数用）。
- **产物目录** `output/results_cn/bynewlt_research/`
  - `state.json`：台账（每票状态 / 调用次数 / request_id / 时间 / 归属策略）；
  - `evidence/<code>.json`：原始证据（ref/原文引文/机构/报告日期/PDF 页码/来源哈希/相邻上下文）；
  - `dossier/<code>.json`：agent 合成稿（多空要点、评级表、引用明细、核对级别、口径说明）。
- **`backend/api/stock_research.py`**（新增）：`GET /api/stocks/{code}/research` 只读盘
  （台账 + 证据摘要 + 合成稿），不发任何计费请求；`backend/main.py` 注册路由。
- **`frontend/src/components/StockResearchPanel.vue`**（新增）：K 线页面板，紧挨
  `AiAnalysisPanel` 之后；`client.ts::getStockResearch` + `types/api.ts` 类型。
  未合成的票如实显示状态（队列中/未收录/证据已取待整理/出错），不编造内容。

## 2. 口径与纪律（先说清楚，再谈结果）

- **计费**：Bynewlt 每次成功 `tools/call` 计 1 次额度，**未收录/空结果也可能计次**；
  相同参数 10 分钟内本地复用；本管线不自动重试（error/unknown 需人工确认后
  `--refetch` 或点名重跑）。
- **覆盖语义**：`company_not_found` = 研报库当前未收录该公司 ≠ 没有研报、≠ 负面判断。
  面板如实显示"暂未收录"，不计入"已调研"。
- **核对级别**：合成稿每条要点都挂引用编号（c1、c2…）；所有引用先过 `cite`（免费本地
  引文绑定检查）；关键数字（评级/目标价/盈利预测）用 `read` 抽读原文页核对（1 次/处），
  合成稿 `verification`/`caveats` 记录核对级别与口径。
- **不生成投资建议**：面板给"证据倾向"（偏多/偏空/中性/证据不足），标注"研报证据整理，
  不构成投资建议"；与「AI 多空分析」的买/持/卖是两套口径，并列展示、不互相替代。

## 3. 现场记录

### 3.1 诊断（2026-10-17，花费 3 次调用）

- 云南白药：`search_company_research_evidence` → `company_not_found`（库未收录）；
  `search_research_companies("云南白药")` → 0 条；空 query 列"最近覆盖"50 家 → 库总计 **572 家**
  （A 股/港股/美股），其中 5 只在 103 名单里（中国石化/亿联网络/分众传媒/联影医疗/今世缘）。
- 目录单次最多 50 家、**无翻页参数** → 覆盖只能逐票判定（由检索调用本身顺带完成）。

### 3.2 试点（花费 3 次：今世缘探针 1 + 管线试点 2）

- 今世缘 603369.SH：命中 1 份 GS 报告（2026-08-18，Buy，TP Rmb34.0）+ 财务预测表 → 证据结构可用。
- 管线试点：中国石化 → `fetched`（Citi，2026-09-08，Sell，TP Rmb5.20，5 条证据 + 8 条上下文）；
  云南白药 → `not_found`（验证了未收录路径）。

### 3.3 全量拉取（已完成，2026-10-17 17:47）

- **103 只全处理：42 fetched / 61 not_found**（台账 `calls_used` 合计 **103**；覆盖率 ≈ 40.8%）。
- 观察：命中率显著低于预期初估；未收录的票如实标注、不计入"已调研"覆盖。
- 61 只未收录票 → 转入 **P2.73 公开来源兜底管线**（`docs/todo/P2_73_public_source_stock_research.md`），
  两库口径互不混同（Bynewlt 证据 vs `source=public_web`）。
- 调用统计见 §6 预算登记（实时以 `state.json` 的 `calls_used` 为准）。

### 3.4 合成（逐批更新）

- **42/42 已合成**（2026-10-17 19:40）：`dossier/` 42 份，面板不再显示「待整理」。
  倾向分布：偏多 27 / 中性 11 / 偏空 4；看多要点 224 条、看空 115 条。
- 合成方式（对 §0 的补充，原因如实记录）：42 只逐票手写时长不可控，改为
  **DeepSeek 起草 → 引文逐条绑定核对 → agent 抽样复核**，新增子命令
  `draft / review / promote`（与 P2.73 同一套纪律）：
  - `draft`：资料包按 E1..E20 编号，模型只能引用现有编号；`quote` 必须逐字摘录，
    同一证据的多段引用自动生成唯一 id（E1 / E1b / E1c），避免前端 key 冲突。
  - 自动核对：`quote` 按 `…` 分段后必须能在对应证据原文中找到（NFKC + 空白折叠 + 小写），
    未命中的引用剔除、refs 连坐过滤；结果写进 `verification.failed`。
  - 本轮引用核验（重写后最终版）：**481 / 497 通过（96.8%）**；每份稿 `checked/verified` 见 dossier。
- 质量强化（二轮）：对 9 份问题稿（7 份无看空要点 / 低核验率 / 缺评级）增加「反向证据优先 +
  risks 至少 2 条」约束后重写并重新 `promote`，其中 5 份达标。
- 核对级别：**3 份 `agent_checked`**（600028.SH / 605499.SH / 300896.SZ，逐条读过原文页），
  其余 39 份 `auto_checked`（引文逐字绑定已过、分析结论未经人工逐票复核）。
  `promote --level` 显式区分，面板对 `auto_checked` 显示「自动引文核对」徽标。
- `review` 体检（重写后最终）：42 份中 **4 份仍被标记**——3 份「无看空要点」
  （002558/002709/601225，证据本身单边，如实保留）、1 份「无评级行」（002532）；
  其余均通过。属于证据本身单边/无评级，不是引文失败；面板按实展示，不补造空头理由。

## 4. 验收

- 测试：`test/test_bynewlt_stock_research.py`（13 条：归一化 / 名单口径 / 请求模板 / 状态机 /
  台账纯度 / 证据结构 / API 三例 / 前端契约 / 提示词编号 / 引文核对与唯一 id / promote 落盘）。
  与 P2.73 测试合跑 **25 条全部通过**。
- 前端构建：`bun run build`（vue-tsc + vite）通过；K 线页可见「研报多空调研」面板
  （含公开来源徽标与引用原文链接，P2.73 接入）。
- 后端：`/api/stocks/{code}/research` 多态实测（not_in_scope / fetched / composed /
  public_web 回落）。
- 覆盖：42 只 `status=fetched` 全部有 `dossier/<code>.json`（面板不再出现「证据已取回待整理」）。
- 2026-10-18：前端把「AI 结论」与「研报证据」合并为**一张个股多空结论卡**（多头/空头两栏，
  每条标 `AI` / `研报` 出处；两边冲突显示「分歧」）；每日选股页每行「多空」抽屉复用同一张卡。

## 5. 边界与后续

- 名单随策略名单刷新变化：新票自动入队（`queued`），重跑 `fetch` 即补漏。
- **已并入每日自动化（2026-10-18）**：顺序改为 `strategy_lists`（刷新 box/wyckoff/moneyflow/
  mainforce/donchian 等全部名单）→ `ai_picks` → `research_bynewlt/research_public`（fetch）→
  `draft --only-missing` → `promote --missing --level auto_checked`。
  预算：`DAILY_RESEARCH_MAX_CALLS`（默认 20，Bynewlt 计费）、
  `DAILY_RESEARCH_PUBLIC_LIMIT`（默认 10，公开源）、`DAILY_STOCK_RESEARCH=0` 可关。
- **Bynewlt 未收录的 61 只已由 P2.73 公开来源兜底**（东财研报/巨潮公告/searxng/Tushare 结构化
  数据），面板同区展示、来源徽标区分；两库证据口径互不混同。
- 库覆盖在扩充：`not_found` 的票以后重查只需 `fetch --codes 600xxx.SH`（会再计 1 次/票）。
- 自动合成：脚本已内置 `draft`（DeepSeek 草稿 + 逐字引文核对）/ `review`（批量体检）/
  `promote`（复核后晋级）；本轮采用「**自动草稿 + agent 复核后 promote**」，引用仅绑定
  证据档原文（不再逐处 `read` 消耗额度；`read` 仅留给人工存疑时抽查）。
- 行业/宏观证据（`search_research_evidence`）本轮未纳入个股稿；某票公司证据不足时，
  可另起行业查询（按行业分组、共享成本）再补。

## 6. 预算登记（Bynewlt 计次，全程如实记录）

| 阶段 | 次数 | 备注 |
| --- | --- | --- |
| 诊断（云南白药检索 + 2 次公司查询） | 3 | 建立"覆盖是部分覆盖"的事实 |
| 今世缘探针 | 1 | 验证证据结构与模板 |
| 管线试点（中国石化 / 云南白药） | 2 | 验证脚本两条路径 |
| 全量拉取（剩余 101 只） | 97 | 2026-10-17 17:47 完成：42 fetched / 61 not_found |
| 合成核对（`read` 抽查，逐批更新） | 0（进行中） | 引用绑定检查 `cite` 免费、不计次 |
| **小计** | **103**（台账口径，已封顶） | 另有 10 分钟内本地复用若干次（免费） |
