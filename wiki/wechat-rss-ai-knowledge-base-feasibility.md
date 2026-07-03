# 微信公众号 RSS 订阅 + AI 知识库可行性方案

## 结论

方案可行，但不建议一开始就做成重平台。对本项目最合适的落地路径是：

```text
公众号 RSS 源
  -> n8n 定时抓取
  -> 本地原文归档 + 元数据表
  -> 去重、清洗、摘要、实体/主题抽取
  -> LightRAG 索引
  -> 股票画像 / 主题机会 / 问答检索
```

第一版优先复用仓库已经在推进的 `SearXNG + LightRAG + ClickHouse/Parquet` 能力，不急着上 Pinecone、Weaviate 这类云向量库。云向量库适合多用户、高并发、跨团队共享；当前更关键的是把公众号内容稳定抓下来、去重、保留证据链，并能被股票画像流程消费。

## 目标

1. 自动订阅多个公众号的文章更新，减少人工复制链接和手动整理。
2. 将文章沉淀为可检索知识库，用于主题研究、产业链跟踪、公司画像和事件复盘。
3. 支持 AI 问答和证据回溯，回答必须能返回文章标题、发布时间、公众号、原文链接和摘要。
4. 与现有股票画像系统连接，抽取 `stock / company / theme / product / supply_chain / catalyst / risk` 等结构化标签。

## 适用场景

适合：

- 跟踪产业链深度号、卖方研究摘要号、政策解读号、AI/半导体/医药等垂直行业号。
- 将公众号文章作为弱信号或研究素材进入股票画像。
- 做“最近 7/30 天某主题有哪些新变化”的检索。
- 对重点股票做专题问答，例如“最近有哪些文章提到光模块出货、价格或海外订单”。

不适合：

- 依赖公众号作为唯一事实来源。
- 抓取付费、会员、登录后或未授权内容。
- 需要强实时交易信号。RSS 转换通常有延迟，也可能突然失效。

## 推荐架构

```text
┌──────────────────────┐
│ WeRSS / feeddd / RSSHub│
└──────────┬───────────┘
           │ RSS/Atom
┌──────────▼───────────┐
│ n8n 定时任务           │
│ - RSS Trigger          │
│ - HTTP Request fallback│
│ - Retry / alert        │
└──────────┬───────────┘
           │ article item
┌──────────▼───────────┐
│ 清洗与归档层           │
│ - raw markdown/html    │
│ - content hash 去重    │
│ - metadata CSV/DB      │
└──────────┬───────────┘
           │ clean text
┌──────────▼───────────┐
│ AI 处理层              │
│ - 摘要                 │
│ - 实体识别             │
│ - 主题/股票映射         │
│ - 质量评分             │
└──────────┬───────────┘
           │ chunks + metadata
┌──────────▼───────────┐
│ LightRAG / 向量库       │
└──────────┬───────────┘
           │ retrieval
┌──────────▼───────────┐
│ 问答 / 股票画像 / 特征层 │
└──────────────────────┘
```

## 组件选型

| 层级 | 推荐 | 备选 | 取舍 |
|---|---|---|---|
| 公众号转 RSS | RSSHub 自托管优先 | WeRSS、feeddd.org | 自托管可控，但维护成本更高；第三方省事但稳定性不可控 |
| 调度编排 | n8n | Make、Dify workflow、cron + Python | n8n 自托管、可视化、重试和告警较方便 |
| 原文归档 | 本地文件 + metadata CSV/Parquet | Notion、Obsidian | 本项目更适合文件化和可批处理；Notion/Obsidian 适合人工阅读 |
| 结构化存储 | ClickHouse / Parquet | SQLite | 与现有数据仓库一致 |
| 知识库检索 | LightRAG | Dify Knowledge、Weaviate、Pinecone | 本项目已有 LightRAG，先复用；云向量库后置 |
| AI API | DeepSeek / OpenAI 兼容接口 | 本地模型 | 先用 API 做高质量抽取，本地模型可用于降本 |

## 推荐第一版实现

### 1. RSS 源管理

维护一个订阅清单：

```csv
source_id,source_name,rss_url,category,priority,enabled,notes
wechat_ai_001,某AI产业链公众号,https://example.com/feed.xml,AI,1,true,重点跟踪推理算力
wechat_chip_001,某半导体公众号,https://example.com/feed.xml,Semiconductor,1,true,关注设备材料
```

字段建议：

- `source_id`：稳定 ID，不依赖公众号中文名。
- `source_name`：公众号或来源名称。
- `rss_url`：RSS/Atom 地址。
- `category`：AI、Semiconductor、Healthcare、Policy 等。
- `priority`：抓取和处理优先级。
- `enabled`：失效时先禁用，不删除。
- `notes`：来源质量、覆盖范围、特殊注意事项。

### 2. n8n 工作流

最小工作流：

```text
Cron
  -> Read subscription list
  -> RSS Read / HTTP Request
  -> Normalize item
  -> Deduplicate by url + title + content_hash
  -> Save raw article
  -> Call AI extraction API
  -> Save metadata and extracted entities
  -> Insert into LightRAG
  -> Send failure summary
```

重试策略：

- 单个 RSS 源失败不影响其他源。
- HTTP 429/403/5xx 进入短重试，仍失败则记录 `last_error`。
- 连续失败 3 次自动标记为 `degraded`，但不删除订阅。

### 3. 数据落盘结构

建议不要一开始只放进向量库。向量库是索引，不是唯一事实存储。原文和元数据要单独保留。

```text
assets/rss_knowledge/
  subscriptions.csv
  raw/
    2026/
      07/
        <article_id>.html
        <article_id>.md
  metadata/
    articles.parquet
    article_entities.parquet
    article_topics.parquet
  failures/
    rss_fetch_failures.csv
```

`articles` 建议字段：

| 字段 | 说明 |
|---|---|
| `article_id` | URL/title/content hash 生成 |
| `source_id` | 来源 ID |
| `source_name` | 来源名称 |
| `title` | 文章标题 |
| `url` | 原文 URL |
| `published_at` | 发布时间 |
| `fetched_at` | 抓取时间 |
| `content_hash` | 去重 hash |
| `raw_path` | 原文路径 |
| `clean_text_path` | 清洗文本路径 |
| `summary` | AI 摘要 |
| `quality_score` | 来源和内容质量分 |
| `status` | fetched / parsed / indexed / failed |

`article_entities` 建议字段：

| 字段 | 说明 |
|---|---|
| `article_id` | 文章 ID |
| `entity_type` | stock / company / product / theme / person / policy |
| `entity_name` | 实体名 |
| `stock_code` | 能映射时填股票代码 |
| `confidence` | 置信度 |
| `evidence_text` | 命中的短证据 |

## AI 抽取任务

每篇文章进入知识库前，至少做四类处理：

1. 摘要：生成 200-400 字中文摘要。
2. 实体识别：公司、股票、产品、产业链节点、政策、人物。
3. 主题分类：AI、算力、半导体、医药、出海、稳定币、机器人等。
4. 研究价值评分：是否值得进入股票画像或主题跟踪。

抽取输出建议保持 JSON：

```json
{
  "summary": "...",
  "themes": ["推理算力", "光模块"],
  "companies": [
    {
      "name": "示例公司",
      "stock_code": "00000.HK",
      "relation": "supplier",
      "confidence": 0.72,
      "evidence": "..."
    }
  ],
  "catalysts": ["订单增长", "价格上涨"],
  "risks": ["需求不确定", "估值透支"],
  "quality_score": 0.81
}
```

## 与本项目的结合方式

### 进入股票画像

公众号文章不直接成为强标签，而是进入 evidence 层：

```text
rss_article
  -> article_entities
  -> stock_profile evidence
  -> LightRAG index
  -> theme_opportunity_score / stock_deep_tag_registry
```

置信度建议：

- 官方公告、年报、公司官网：高。
- 主流财经媒体：中。
- 行业公众号：中低到中，取决于来源质量。
- 观点类公众号：弱信号，只能做 candidate，不直接进入强标签。

### 问答样例

可支持的问题：

- “最近 30 天哪些公众号提到推理算力供需变化？”
- “有哪些文章把某港股和 AI Coding 主题联系起来？证据是什么？”
- “过去一周光模块、液冷、IDC 电力分别有哪些新催化？”
- “某个主题下，哪些股票只被弱信号提到，还没有强证据？”

回答要求：

- 必须附引用来源。
- 必须区分事实、观点、推断。
- 必须给出时间范围。
- 对低置信度结论标注“待验证”。

## 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 微信封堵 RSS 转换 | RSS 源断流 | 多源备份；失败监控；订阅降级；人工补录重点文章 |
| 第三方 RSS 服务不稳定 | 延迟、丢文章 | 关键源自托管 RSSHub；保留抓取日志 |
| 版权和授权风险 | 不适合公开再分发 | 仅用于内部研究；保存必要引用；不抓取付费/登录后内容；不公开原文全文 |
| 内容质量参差 | 噪音进入知识库 | 来源质量分；主题白名单；AI 质量评分；人工抽查 |
| 重复转载 | 知识库污染 | URL + 标题 + 正文 hash 去重 |
| AI 幻觉 | 错误标签进入画像 | JSON schema 校验；证据片段必填；低置信度只入 candidate |
| 成本上升 | API 调用费用不可控 | 先摘要后深抽取；低优先级源批处理；缓存 content_hash |

## POC 验收标准

建议先做 2 周 POC：

1. 接入 5-10 个公众号 RSS 源。
2. 每天自动抓取不少于 1 次。
3. 文章去重准确率达到 95% 以上。
4. 每篇文章有摘要、主题、实体、来源引用。
5. 至少 3 类问答能返回可追溯证据。
6. 抽样 50 篇文章，股票/主题实体识别准确率达到 80% 以上。
7. RSS 失败能在日报中列出，不影响其他源继续处理。

## 分阶段落地计划

### Phase 0：手工验证

周期：1-2 天。

- 选 5 个目标公众号。
- 分别尝试 WeRSS、feeddd.org、RSSHub。
- 记录可用性、延迟、是否丢字段、是否能拿到正文。
- 选出第一批稳定 RSS 源。

### Phase 1：最小自动化

周期：3-5 天。

- 用 n8n 定时拉取 RSS。
- 原文和 metadata 落到 `assets/rss_knowledge/`。
- 做 content hash 去重。
- 生成失败清单。

### Phase 2：AI 清洗和抽取

周期：1 周。

- 对新文章生成摘要。
- 抽取主题、股票、公司、产品、催化和风险。
- 结构化输出写入 Parquet/CSV。
- 低置信度结果进入 candidate，不直接写强标签。

### Phase 3：接入 LightRAG

周期：1 周。

- 将清洗文本按文章切 chunk。
- 写入 LightRAG knowledge base。
- 问答返回 `article_id/title/source/url/published_at`。
- 与股票画像 evidence 关联。

### Phase 4：进入选股研究流程

周期：1-2 周。

- 将主题提及、来源质量、attention velocity 做成特征。
- 接入 `stock-intelligence-pipeline` 或独立的 rss evidence import。
- 回测验证公众号弱信号是否改善主题机会特征。

## 推荐优先级

第一版推荐：

```text
RSSHub/WeRSS/feeddd
  + n8n
  + 本地文件归档
  + Parquet metadata
  + LightRAG
  + DeepSeek/OpenAI-compatible API
```

暂缓：

- Pinecone / Weaviate：等数据量、并发或共享需求明确后再上。
- Dify 全流程：适合做 demo 和知识库 UI，但本项目更需要可控的数据管线和可回测特征。
- Notion/Obsidian 作为主存储：适合人工阅读，不适合作为量化流水线事实源。

## 最小可执行清单

1. 建 `assets/rss_knowledge/subscriptions.csv`。
2. 选 5 个公众号 RSS 源做连通性测试。
3. 用 n8n 建一个 daily RSS ingest workflow。
4. 保存 raw html/markdown 和 `articles.parquet`。
5. 加一段 AI 抽取 JSON schema。
6. 将抽取结果写入 `article_entities.parquet`。
7. 将 clean text 导入 LightRAG。
8. 做 10 个固定问题的问答验收。

## 最终判断

这个方案值得做，但要把它定位为“研究证据和弱信号采集层”，不是自动交易信号源。真正的价值在于：

- 提高产业链和主题研究覆盖率。
- 减少手工整理公众号文章的时间。
- 给股票画像系统补充中文行业深度内容。
- 形成可追溯、可复用、可回测的 evidence 数据资产。

最大风险是 RSS 源稳定性和内容版权边界。因此第一版要轻量、可替换、可降级：RSS 只是入口，原文归档、元数据、去重、证据链和 LightRAG 索引才是核心资产。
