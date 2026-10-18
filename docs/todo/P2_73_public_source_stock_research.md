# P2_73 · 公开来源个股调研（Bynewlt 未收录票的兜底）：searxng + 免费直连 + Tushare

> 用户（2026-10-17）："如果用这个新的 skills 找不到对应股票信息，就使用 searxng /
> lightrag 实现类似个股研报分析能力。"
> 追加："可以同时看看 tushare 是不是也有相关数据。"

## 0. 已定决策

| 决定项 | 结论 |
| --- | --- |
| 首批范围 | Bynewlt `not_found` 的 **61 只**（P2.72 全量 103 只 − 42 只已收录） |
| 来源分层 | 管线证据一律 `source=public_web`，与 Bynewlt 库内证据**不混同**；面板加「公开来源」徽标 |
| 合成方式 | 管线取证据 → DeepSeek 起草 → **引文自动逐条核对** → agent 复核后 `promote` |
| LightRAG | **v1 不采用**（评估见 §2.4）：本地库 embedding 大面积失败、重建成本高；个体票用"直接证据 + LLM"已可达成，留作后续跨股票图谱/问答增强 |
| Tushare | 采用（token 已是高级积分）：`report_rc` 券商预测、机构调研、回购、解禁、两融、股东户数、分红、筹码分布、券商金股、业绩预告/快报 |

## 1. 架构（照 P2.72 三层模式，缺一票则回落一棵树）

```
scripts/cn_public_stock_research.py
  fetch  → evidence/<code>.json（证据档）+ state.json（台账）     ← 本脚本负责
  draft  → dossier_draft/<code>.json（DeepSeek 草稿 + 引文核对）  ← 自动
  promote→ dossier/<code>.json（agent 复核后的成品，source=public_web）
backend/api/stock_research.py   Bynewlt 稿优先 → 缺则回落公开来源稿/证据（response.source 标明）
frontend/src/components/StockResearchPanel.vue  同面板展示 + 公开来源徽标 + 引用原文链接
```

- 产物目录：`output/results_cn/cn_public_research/`
  （`state.json` / `evidence/` / `dossier_draft/` / `dossier/` / `pdf_cookies.json` / `logs/`）。
- 状态机：`queued → fetching → fetched / no_data / error`（→ `draft → composed`）。
- 证据编号：`R#` 研报、`A#` 公告、`N#` 新闻、`D#` Tushare 结构化事实、`Q` 行情快照。
- 命令：
  - `fetch [--codes ...] [--limit N] [--refetch] [--sleep S]`（默认目标 = Bynewlt not_found 全集）
  - `news --codes ...`（新闻层补采：引擎限流时事后单独补）
  - `status [--verbose]` / `show --code X [--json]`
  - `draft --code X` / `draft --codes ... --sleep S`（DeepSeek；引文自动核对随产出）
  - `promote --code X`（agent 复核后晋级为成品；置 `agent_checked`、状态 `composed`）
  - `mark --code ... --status ...`

## 2. 证据源与口径

### 2.1 五层证据（全部免费源，温和节流）

1. **东财券商研报**：`reportapi.eastmoney.com/report/list`（列表：机构/评级/EPS 预测/日期）
   → PDF 全文 `pdf.dfcfw.com/pdf/H3_{infoCode}_1.pdf`。PDF 首返 **JS 反爬挑战**，用 node
   执行挑战脚本解出并持久化 `EO_Bot_Ssid`/`__tst_status` cookie（`pdf_cookies.json`）后可下；
   `pypdf` 抽取逐页文本。
2. **巨潮公告**：`cninfo`（`topSearch` 现查 orgId → `hisAnnouncement/query` 列表 → 静态 PDF
   全文）；公告按重要性排序（回购/业绩/重组/股权激励等优先喂给 LLM）。
3. **网络线索（searxng）**：本地容器 `quant-searxng`（127.0.0.1:8888），中文引擎 +
   **相关性过滤**（只留命中股票名/代码的结果）+ 静态详情页正文抓取；`NEWS_KEEP_MAX=24`；
   引擎限流时标 `degraded` 并记录 `engines_down`（可用 `news` 子命令事后补采）。
4. **行情与资金面**：腾讯行情（价格/PE/PB/市值）、东财资金流 120 日、龙虎榜、两融、
   解禁、股东户数、分红（datacenter / push2his）。
5. **Tushare 结构化数据**（`api.tushare.pro`，环境变量 `TUSHARE_TOKEN`，token 不落盘不打印）：
   - `report_rc` 券商预测（评级/EPS/各年预测；注意 `tp` 字段是**利润总额**而非目标价，
     目标价在 `max_price/min_price`）——城门研报之外可拿全量预测行的结构化补充；
   - `stk_surv` 机构调研、`repurchase` 回购、`share_float` 解禁、`margin_detail` 两融、
     `stk_holdernumber` 股东户数、`dividend` 分红、`cyq_perf` 筹码分布（获利盘/成本分位）、
     `broker_recommend` 券商金股（按月缓存一次）、`forecast`/`express` 业绩预告/快报。
   - 子接口失败只记账（`errors`）不拖垮整票；`_tushare_call` 直连 HTTP、字段显式截断。
   - `news` 接口无权限（跳过；新闻走 searxng）。

### 2.2 引文核对与合成（决不编造）

- 草稿提示词铁律：只准用资料包内容；`citations[].quote` 必须逐字摘录（允许 `…` 省略）；
  每个要点 refs 只能引用存在的编号；数字必须与资料包一致。
- 产出后自动逐条比对：quote 拆段后必须能在对应证据原文（NFKC 归一、全角折叠）中找到；
  未命中的引用剔除、refs 连坐过滤（空要点整条丢弃）；`Q` 语料 = quote JSON + 提示词中
  同一行情快照行（模型引快照行也可核对）。
- `D` 编号事实行由 `build_tushare_facts()` 单一来源生成：提示词与核对语料共用同一份编号。
- `promote` 置 `verification.level=agent_checked`、写 `caveats`（公开来源、非专业研报库、
  不构成投资建议）。

### 2.3 面板/API 口径

- `GET /api/stocks/{code}/research`：Bynewlt 稿优先；缺则回落 `cn_public_research/dossier`，
  再缺回落其 evidence 摘要；`source` 标明 `bynewlt|public_web`；证据摘要跟随稿的来源
  （避免"空档旧证据"遮挡公开来源全文证据）。
- 前端：`公开来源`徽标、来源措辞/页脚切换、引用弹层与明细里的原文链接（`url`）、
  采集期细分（研报全文/公告/新闻/结构化数据计数）。
- hints 按来源分话术：公开兜底队列/采集中/已取回/证据不足/出错，如实显示，不编造内容。

### 2.4 LightRAG 评估（v1 不采用，记录备查）

- 本地 `lightrag` server 未运行（无 9621 监听）；库 `lightrag_hk_stock` 有 15721 篇文档，
  其中 **15573 篇 failed**（embedding worker 60s 超时，Ollama bge-m3），有效内容极少。
- 重建（重新切分+embedding 全量 A 股公告/研报）成本高、且对"单票多空要点"任务，
  直接证据（研报 PDF+公告+结构化数字）+ LLM 起草已能达成同样口径；LightRAG 的价值在
  **跨股票图谱检索/问答**，留作后续增强项（届时建议独立库 + 换 embedding 后端）。

## 3. 现场记录

### 3.1 源验证（2026-10-17）

- 东财研报列表/PDF、巨潮公告/PDF、腾讯行情、东财 datacenter 各接口、searxng JSON API
  逐项验证通过；修复了：`http_get` 不支持 POST（公告全 0）、假 response 对象 lambda 自绑定、
  NFKC 归一缺失、引文分段正则不认 `…`。
- searxng 引擎态：quark 可用（主力）；360search 曾可用后被限流；baidu/sogou/DDG 处于
  suspend/captcha；bing 中文查询返回垃圾（已被相关性过滤挡住）。曾死路径（勿再试）：
  东财 search-api-web 新闻 JSONP、腾讯 news API、新浪搜索/个股新闻页、caifuhao、股吧
  （散户噪声，不作证据）。

### 3.2 试点 3 只（002294 信立泰 / 601579 会稽山 / 600036 招商银行）

- 两轮 fetch 均成功：每只 7-10 篇研报（4 份 PDF 全文）、30 条公告（5 份全文）、
  Tushare 结构化 40+ 条；`quant errors=0`。
- 全链路演练（002294）：`draft` → 引文核对 **14/14 通过**（首轮 14/15，唯一失败为行情快照行，
  已把快照行并入 `Q` 语料修复）→ `promote` → API `source=public_web`、`state=composed`、
  evidence 42 条（8 研报/30 公告/4 新闻 + 21 Tushare 行）→ 前端构建通过。
- 新闻层：`news` 补采子命令实测可用（002294 5 条/3 正文；600036 6 条/4 正文；601579 0 条）。

### 3.3 全量批次（61 只，已完成）

- 2026-10-17 18:03–18:51 完成：**58 只新采 + 3 只试点 = 61/61 fetched，0 error、0 no_data**；
  日志 `logs/fetch_20261017_full.log`。单票 ~25–46s（含 PDF 下载与节流）。
- 证据覆盖（61 只合计）：**38 只（62%）有券商研报**、108 份研报 PDF 全文；
  公告 1830 条（305 份全文）；21 只（34%）采到相关新闻；**61/61 都有 Tushare 结构化数据**；
  quant 接口 errors=0。
- 新闻层覆盖率偏低与引擎限流有关（quark 为主，360search 时好时坏）；新闻只是补充层，
  缺新闻的稿子以研报/公告/结构化数据为准，`no_news` 在复核清单中标注。

### 3.4 草稿与复核（两轮）

- 第一轮 `draft`（60 只）：全部生成；自动核对多为 10/10–26/26，少数票有 1–7 条引用被剔除。
- 复核清单工具：`scripts/cn_public_dossier_review.py`（见 §4），第一轮结果
  **28 PASS / 33 FLAG**（FLAG 基本都是"有引用未通过逐字核对"；仅 1 例多空结构偏薄）。
- 针对失败引用的两类系统性问题做了管线修复（第二轮起生效）：
  1) 模型会引用提示词里的派生展示行（龙虎榜/两融/股东户数等）——现在 `Q` 语料纳入
     `quant_display_lines()` 的**全部展示行**，提示词与核对共用同一份文本；
  2) 模型引用 `D#` 事实行时会带上 `D8｜` 前缀——`D` 语料同时收录带前缀与不带前缀两种形态；
  3) 核对归一新增句读容忍（`_norm_for_verify`：忽略 `。；：，、` 等句读差异，但数字/汉字/单位
     仍须逐字一致），修复"跨行拼接 + 改标点"导致的误杀。
- 第二轮重写 FLAG 的 33 只（备份第一轮于 `dossier_draft_v1/`），按
  **核对通过数 + 多空结构完整度** 择优保留：最终 **35 PASS / 26 FLAG**（余下 FLAG 均为
  "个别引用未通过逐字核对已剔除"，稿件保留内容全部可核）。
- 透明度：有剔除的稿子自动在 `caveats` 注明"剔除 N 条未通过核对的引用"；
  **61 只全部 `promote` → `state=composed`**（`source=public_web`），API 逐票实测通过。

### 3.5 工程修复

- `save_state_merged()`（fetch 循环按票合并写台账）：修复后台 fetch 与其他进程
  （promote/news 等）并发写 `state.json` 互相覆盖的竞态；旧进程尾部残留的覆盖需在批次
  结束后重跑一次 `promote`（dossier 文件本身不受影响，面板展示以 dossier 文件为准）。

### 3.6 全量晋级（2026-10-17 19:45 完成）

- **61/61 草稿 → dossier**：面板 `source=public_web` 全部可读，不再有「已取证据待整理」。
- 倾向分布：偏多 15 / 中性 27 / 偏空 13 / 证据不足 6；看多要点 310 条、看空 296 条。
  6 只「证据不足」是公开源确实没有可用多空证据（小市值、研报/公告稀疏），不编造。
- 引用核验：**829 / 891 通过（93.0%）**；未通过的是模型改写/跨段拼接，已剔除且 refs 连坐过滤。
- 核对级别：**2 份 `agent_checked`**（002294.SZ 试点、000408.SZ 数字/口径复核），
  其余 59 份 `auto_checked`（逐字绑定已过、未逐票人工复核）。`promote --level auto_checked`
  是本轮批量晋级的显式档位；面板对 `auto_checked` 显示「自动引文核对」徽标。
- `scripts/cn_public_dossier_review.py` 终检：**PASS 39 / FLAG 22**。FLAG 原因集中在
  `no_reports`/`no_news`（公开源本身没采到研报或新闻）与 1–2 条引用被剔除，
  不是编造；22 份也以 `auto_checked` 晋级，面板保留证据细分让用户自行判断，
  不因"来源稀疏"把整票打回空白。

## 4. 验收

- 测试：`test/test_cn_public_stock_research.py` + `test/test_bynewlt_stock_research.py`
  合跑 **25 条通过**（纯函数归一/引文核对（省略号、句读容忍、D 前缀）/台账合并写/
  目标票推导/PDF 挑战空转等）。
- 复核工具：`scripts/cn_public_dossier_review.py`
  （打印核对与结构清单；`--promote` 只晋级 PASS；`--codes` 可只看指定票）。
- API 实测：`002294.SZ` / `000408.SZ`（composed/public_web）、`999999.SZ`（not_in_scope）、
  `600028.SH`（composed/bynewlt，优先级不回归）。
- 前端 `bun run build` 通过（公开来源徽标/来源措辞/引用原文链接/采集期细分）。

## 5. 边界与后续

- 免费源的"新闻/搜索"层受引擎限流影响最大：dossier 合成时如遇 `degraded`，先 `news` 补采；
  仍空则在 caveats 注明"新闻层不充分"。
- Tushare 额度：各接口按账号积分有频次限制；管线单票 ~11 次调用、批内无重试；
  如遇频次报错，错误记入该票 `sources.tushare.errors`，不阻塞。
- `report_rc` 的 `tp`（利润总额）与 `max_price/min_price`（目标价）口径已在文档与事实行
  中区分；合成时评级/目标价优先引用同报告的结构化行。
- LightRAG 留作后续（跨股票图谱检索）；如重启评估，先解决 embedding 后端（bge-m3 超时）。
- 名单刷新：新入队票重跑 `fetch`（自动追加），已 composed 的票不会被覆盖。
- **已并入每日自动化（2026-10-18）**：`run_daily_production.sh` 在 `strategy_lists` 之后、
  台账之前跑 `fetch --limit 10` → `draft --only-missing` → `promote --missing --level auto_checked`；
  已 composed 的票默认不再重采（此前 `fetch` 会白跑重采 61 只，已修）。

## 6. 预算

| 阶段 | 费用 | 备注 |
| --- | --- | --- |
| 全部证据源（东财/巨潮/腾讯/东财 datacenter/searxng/Tushare） | **0 元 / 0 计费调用** | 公开免费源；节日流 sleep=2s、Tushare 步进 0.4s |
| DeepSeek 起草 | 按量（深聊超低单价） | 仅 `draft` 用；重写不超过 2 次/票 |
| Bynewlt | 0（本管线不接触） | 未收录票不回查、不消耗额度 |
