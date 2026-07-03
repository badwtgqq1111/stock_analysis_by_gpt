# 股票画像、产业链图谱与推荐式标签引擎设计

本文档把现有 `SearXNG + DeepSeek` 标签流水线升级为“证据驱动的股票画像系统”。目标不是简单给股票贴几个行业标签，而是为每只股票构建可解释、可更新、可排序的完整画像，并用类似短视频推荐系统的思想，把“股票-主题-产业链-事件-热度-用户偏好”连成图谱。

## 目标

1. 为每只股票生成完整画像：
   - 基础身份：公司名、别名、行业、标的类型、可交易性。
   - 业务画像：主营业务、产品、客户、区域、收入分部。
   - 技术画像：大模型、芯片、云、数据中心、医药管线等细分技术标签。
   - 产业链画像：上游、下游、供应商、客户、瓶颈环节、替代品。
   - 热度画像：新闻、社媒、GitHub、论文、模型榜单、公众号等 attention velocity。
   - 交易画像：流动性、估值、波动、趋势、拥挤度、机构覆盖。
2. 把标签从静态 CSV 升级成动态图谱：
   - `stock -> product -> technology -> supply_chain_node -> event -> source`
   - 每条边有证据、置信度、时间戳和来源质量。
3. 借鉴推荐系统思想：
   - 短视频推荐给“用户”画像和“内容”画像打分。
   - 本系统给“投资者/策略偏好”画像和“股票/主题”画像打分。
   - 输出可解释的 watchlist、主题篮子和产业链机会。

## 为什么现有方案不够

现有主流程：

```text
SearXNG evidence -> DeepSeek 抽 tag -> build-stock-tags -> import-stock-tags
```

优点是已经具备证据缓存、断点续跑、LLM 抽取、candidate 分离和仓库导入能力。但它仍有几个限制：

1. 搜索 query 过于通用。只搜 `02513` 容易命中时间、商品、无关页面；必须使用公司别名、英文名、产品名和官网域名。
2. 证据源没有分层。普通搜索结果、年报、论文、GitHub、模型榜单、新闻、社媒被混在一起，导致置信度不可比。
3. 标签粒度不足。`科技/软件服务` 不足以表达 `GLM-5.1`、`AI Coding`、`Agentic AI`、`模型榜单前沿`。
4. 缺少热度时间序列。社媒/新闻/GitHub 的价值不在“出现过”，而在“近期加速”。
5. 缺少图谱结构。找产业链卡脖子点，需要知道公司处在链条哪个环节，而不是只知道它属于哪个行业。

## 抖音推荐算法思想如何迁移

短视频推荐系统通常会构建：

| 推荐系统概念 | 股票画像系统映射 |
|---|---|
| 用户画像 | 投资者/策略画像，例如成长、AI、资源、低估值、高波动承受 |
| 内容画像 | 股票画像，例如业务、技术、产业链、估值、热度 |
| 用户行为 | 策略反馈，例如 watchlist、买入、卖出、回测收益、人工标注 |
| 内容互动 | 事件反馈，例如新闻热度、社媒转发、GitHub star、论文引用 |
| 召回 | 主题/产业链/事件候选股票召回 |
| 粗排 | 基础质量、流动性、估值、热度评分 |
| 精排 | 多因子模型 + 图谱扩散 + LLM 解释 |
| 探索 | 小仓位关注新主题、新链条、新技术突破 |
| 负反馈 | 证伪事件、过度拥挤、估值透支、弱证据降权 |

类比后，本系统可以形成两类画像：

1. **股票画像 Stock Profile**
   - “这家公司是什么、做什么、和谁有关、处于什么链条、近期发生什么”。
2. **策略画像 Strategy Profile**
   - “当前策略想找什么样的机会，例如 AI 基础模型、卡脖子供应链、低估值资源股、政策催化、出海应用”。

最终输出不是单一 tag，而是：

```text
score(stock, strategy, time)
```

并且每个分数都能追溯到证据和图谱路径。

## 数据源分层

### L0 基础数据

| 数据 | 用途 |
|---|---|
| `stock_info_registry` | 公司名、行业、可交易性、标的类型 |
| OHLCV / features | 流动性、趋势、波动、因子 |
| `hk_industry_registry.csv` | 行业分层和基础主题 |

### L1 强证据

| 来源 | 用途 | 置信度 |
|---|---|---:|
| HKEX 公告、年报、招股书 | 主营业务、收入分部、风险、客户 | 高 |
| 公司官网、IR、产品页 | 产品、技术路线、客户案例 | 高 |
| 官方 API / docs / release notes | 技术能力、模型版本、开发者生态 | 高 |
| arXiv / 论文 / technical report | 技术先进性、模型架构、benchmark | 中高 |

### L2 市场证据

| 来源 | 用途 | 置信度 |
|---|---|---:|
| 主流财经新闻 | 催化事件、业绩、合作、融资 | 中 |
| 行业媒体 | 产业链变化、订单、价格、供需 | 中 |
| 模型榜单 / OpenRouter / HuggingFace | 模型热度、性能、开发者采用 | 中 |
| GitHub | 开源生态、开发者活跃度、issue/PR velocity | 中 |

### L3 热度和弱信号

| 来源 | 用途 | 置信度 |
|---|---|---:|
| X/Twitter | KOL 讨论、海外热度、叙事扩散 |
| 雪球 / 股吧 | 散户热度、主题扩散、情绪 |
| 微信公众号 | 中文产业链深度文章、专家观点 |
| Reddit / Hacker News | 开发者和海外社区注意力 |

弱信号不能直接生成正式 tag，只能形成 attention / candidate / alert。

## 核心数据模型

### 1. `entity_alias_registry`

解决 `02513` 搜不到真实资料的问题。

| 字段 | 说明 |
|---|---|
| `stock_code` | 02513 |
| `market` | HK |
| `alias` | 智谱 / 智谱AI / Zhipu AI / Z.ai / GLM / ChatGLM |
| `alias_type` | company / english_name / product / model / brand |
| `source` | stock_info / official / llm / manual |
| `confidence` | 0-1 |
| `updated_at` | 更新时间 |

### 2. `stock_profile`

每只股票的一页画像摘要。

| 字段 | 说明 |
|---|---|
| `stock_code` | 股票代码 |
| `profile_json` | 结构化画像 |
| `summary` | LLM 生成的短摘要 |
| `strengths` | 优势 |
| `risks` | 风险 |
| `open_questions` | 未验证问题 |
| `evidence_count` | 证据数量 |
| `confidence` | 总体置信度 |
| `updated_at` | 更新时间 |

### 3. `stock_deep_tag_registry`

比现有 `stock_tag_registry` 更细。

| 字段 | 说明 |
|---|---|
| `stock_code` | 股票代码 |
| `tag` | GLM-5.1 / AI Coding / Agentic AI |
| `tag_type` | product / technology / theme / bottleneck / catalyst / risk |
| `confidence` | 置信度 |
| `evidence_count` | 证据数量 |
| `source_count` | 来源数量 |
| `freshness_days` | 证据新鲜度 |
| `attention_velocity_7d` | 7 日热度变化 |
| `is_primary` | 是否核心标签 |
| `evidence_refs` | evidence id 列表 |
| `updated_at` | 更新时间 |

### 4. `stock_graph_edges`

产业链和实体关系。

| 字段 | 说明 |
|---|---|
| `src_type` | stock / company / product / technology / event |
| `src_id` | 来源节点 |
| `edge_type` | produces / supplies / uses / competes_with / exposed_to / bottleneck |
| `dst_type` | product / technology / customer / supplier / theme |
| `dst_id` | 目标节点 |
| `confidence` | 置信度 |
| `evidence_refs` | 证据 |
| `updated_at` | 更新时间 |

示例：

```text
02513 -> produces -> GLM-5.1
GLM-5.1 -> belongs_to -> 大模型
GLM-5.1 -> capability -> AI Coding
大模型 -> bottleneck -> 推理算力
推理算力 -> upstream -> GPU/HBM/光模块/IDC电力
```

### 5. `attention_signal`

热度时间序列。

| 字段 | 说明 |
|---|---|
| `entity_type` | stock / product / theme / person |
| `entity_id` | 02513 / GLM-5.1 / AI Coding |
| `source` | twitter / github / news / wechat / arxiv |
| `metric` | mentions / stars / downloads / citations |
| `value` | 数值 |
| `window` | 1d / 7d / 30d |
| `velocity` | 环比变化 |
| `quality_score` | 来源质量 |
| `asof_date` | 日期 |

## 股票画像生成流程

### Step 1. Entity Resolution

输入股票代码，先生成别名：

```text
02513
智谱
智谱AI
北京智谱华章
Zhipu AI
Z.ai
GLM
ChatGLM
GLM-5.1
```

来源优先级：

```text
stock_info -> HKEX/招股书 -> 公司官网 -> LLM 辅助 -> 人工确认
```

### Step 2. Source-Aware Search

不同来源使用不同 query：

```text
site:hkexnews.hk 02513 智谱 年报 招股书
site:z.ai GLM-5.1 Zhipu
site:arxiv.org Zhipu GLM
site:github.com zhipuai glm
site:huggingface.co Zhipu GLM
site:openrouter.ai GLM-5.1
智谱 GLM-5.1 大模型 benchmark 排名
智谱 AI Coding Agentic AI
```

不再使用纯 `02513` 作为主 query。

### Step 3. Evidence Cleaning

每条 evidence 先判断：

| 检查 | 处理 |
|---|---|
| 是否包含公司别名或产品别名 | 不包含则降权或丢弃 |
| 是否只是搜索 query echo | 不作为 tag 证据 |
| 是否来自签名 URL / 临时 URL | 清洗 query 参数 |
| 是否是无关页面 | 标记 `irrelevant` |
| 是否重复 | 按 URL/hash 去重 |

### Step 4. LLM Structured Extraction

每只股票输出：

```json
{
  "stock_code": "02513",
  "company": "智谱",
  "products": ["GLM-5.1", "ChatGLM"],
  "technologies": ["大模型", "AI Coding", "Agentic AI"],
  "industry_chain": [
    {"node": "基础模型", "role": "producer"},
    {"node": "推理算力", "role": "demand_driver"}
  ],
  "deep_tags": [
    {
      "tag": "GLM-5.1",
      "tag_type": "product",
      "confidence": 0.92,
      "evidence_refs": ["..."]
    }
  ],
  "risks": ["模型商业化不确定", "算力成本", "竞争激烈"],
  "open_questions": ["收入中 API/企业客户占比", "毛利率变化", "推理成本下降路径"]
}
```

### Step 5. Graph Construction

把抽取结果变成节点和边：

```text
stock:02513
company:智谱
product:GLM-5.1
technology:大模型
capability:AI Coding
bottleneck:推理算力
supplier_class:GPU/HBM/光模块/IDC
```

### Step 6. Ranking / Recommendation

借鉴推荐系统的多阶段排序：

#### 召回

```text
主题召回: 大模型 -> 所有关联股票
产业链召回: 推理算力 -> GPU/HBM/光模块/IDC
事件召回: GLM-5.1 发布 -> 智谱及关联链条
热度召回: Twitter/GitHub/新闻热度上升 -> 相关股票
```

#### 粗排

```text
base_score =
  tag_confidence
+ source_quality
+ evidence_count
+ freshness
+ liquidity_ok
- valuation_crowding
```

#### 精排

```text
final_score =
  fundamental_quality
+ thematic_relevance
+ bottleneck_score
+ attention_velocity
+ catalyst_strength
+ technical_trend
- risk_score
- crowding_score
```

## 卡脖子/产业链瓶颈评分

找“产业链卡脖子点”可以使用：

```text
bottleneck_score =
  demand_growth
* supply_constraint
* pricing_power
* substitution_difficulty
* evidence_quality
* freshness
```

字段解释：

| 因子 | 例子 |
|---|---|
| `demand_growth` | 大模型推理调用增长 |
| `supply_constraint` | GPU/HBM/光模块产能限制 |
| `pricing_power` | 价格上涨、毛利率提升 |
| `substitution_difficulty` | 替代技术不成熟 |
| `evidence_quality` | 订单、财报、供应链新闻 |
| `freshness` | 近期是否发生变化 |

## 02513 智谱示例画像

当前系统只能给出：

```text
industry: 资讯科技业 / 软件服务
theme: 科技 / 资讯科技业 / 软件服务
```

理想画像应补充：

```text
product: GLM-5.1, ChatGLM
technology: 大模型, Agentic AI, AI Coding
theme: 国产大模型, 企业 AI 平台, 开源模型
value_chain: 基础模型厂商, AI API, 企业应用赋能
bottleneck_exposure: 推理算力, GPU/HBM, 模型训练成本
catalyst: 新模型发布, benchmark 排名提升, API 调价, 企业客户增长
risk: 竞争激烈, 商业化不确定, 算力成本, 模型迭代风险
```

要得到这些标签，需要 source-aware search，而不是纯 SearXNG 股票代码搜索。

## GitNexus / Graph Index 思路借鉴

GitNexus 这类代码知识图谱工具的核心思想不是“画图”，而是把原本松散的文本预先索引成结构图：

```text
repo -> file -> class/function -> dependency -> call chain -> execution flow -> cluster
```

股票研究可以借鉴同一套逻辑，把证据和公司关系索引成：

```text
stock -> company -> product -> technology -> supply_chain_node -> event -> source
```

### 可迁移设计

| GitNexus / 代码图谱 | 股票研究图谱 |
|---|---|
| repository | 市场 / 股票池 |
| file / module | 公司 / 子公司 / 产品线 |
| class / function | 产品 / 技术 / 模型 / 药物 / 矿山 |
| dependency graph | 产业链上下游依赖 |
| call chain | 订单流、技术扩散链、收入兑现路径 |
| execution flow | 事件从技术突破到业绩影响的路径 |
| functional cluster | 主题簇、产业链簇 |
| impact analysis | 事件影响哪些股票 |
| stale index detection | 新公告/新闻/社媒热度触发局部 reindex |
| Graph RAG | LLM 回答前先取相关子图和证据 |

### Graph Indexer

Graph Indexer 是离线/增量构建图谱的组件。它不直接做问答，而是把 evidence 结构化成节点和边。

输入：

```text
company_research_evidence
stock_profile evidence
HKEX documents
official website
arXiv / GitHub / model benchmark
news / social signals
```

输出：

```text
stock_graph_nodes
stock_graph_edges
stock_deep_tag_registry
attention_signal
```

示例：

```text
stock:02513 -> alias_of -> company:智谱
company:智谱 -> produces -> product:GLM-5.1
product:GLM-5.1 -> belongs_to -> technology:大模型
product:GLM-5.1 -> capability -> theme:AI Coding
technology:大模型 -> bottleneck -> supply_chain:推理算力
supply_chain:推理算力 -> upstream -> supplier_class:GPU/HBM/光模块/IDC电力
```

### Graph Retrieval

Graph Retrieval 不是普通语义搜索，而是先定位实体，再沿边取子图。

典型查询：

```text
explain_stock_profile(02513)
trace_supply_chain(大模型, depth=2)
find_related_stocks(GLM-5.1)
impact_analysis(推理算力瓶颈)
rank_bottleneck_opportunities(大模型)
```

对应检索策略：

| 查询类型 | 检索策略 |
|---|---|
| 单只股票画像 | `stock -> 1-hop/2-hop subgraph + evidence` |
| 主题全局研究 | `theme -> community/cluster summaries + top stocks` |
| 事件影响分析 | `event -> affected technologies/products/supply_chain -> stocks` |
| 卡脖子机会 | `bottleneck node -> upstream/downstream graph expansion + ranking` |

### Graph RAG Agent

LLM 不直接从网页回答，而是基于图谱上下文回答：

```text
question -> entity linking -> graph retrieval -> evidence retrieval -> LLM answer -> citation
```

对于 `02513 为什么是大模型股？`，上下文应来自：

```text
02513
-> 智谱 / Z.ai
-> GLM / ChatGLM / GLM-5.1
-> 大模型 / AI Coding / Agentic AI
-> 官网 / 论文 / 榜单 / 新闻 evidence
```

这样可以显著减少“搜索结果漂移”和 LLM 幻觉。

### Reindex Trigger

类似代码库变更后重建索引，股票图谱也应局部重建：

| 触发器 | 动作 |
|---|---|
| 新公告 / 财报 | 重建公司业务、收入分部、风险 |
| 新产品 / 新模型发布 | 重建 product/technology/catalyst 边 |
| GitHub / HuggingFace 热度异动 | 更新 attention_signal |
| 新闻热度突增 | 更新 catalyst 和 candidate tag |
| 人工修正 alias/tag | 重跑相关股票的 source-aware search |

## RAG 技术选型：基于 LightRAG 定制

本地已经下载 `LightRAG` 代码，路径为：

```text
/Users/ccs/code/quant/LightRAG
```

RAG 部分建议优先基于 LightRAG 二次定制，而不是从零实现通用 GraphRAG。原因是 LightRAG 已经具备：

- 文档上传、文本插入、批量索引和后台处理队列。
- `local/global/hybrid/naive/mix` 五种查询模式。
- 同时返回 LLM answer 和结构化 retrieval data。
- WebUI 图谱查看、label 搜索、子图查询。
- REST API、Open WebUI/Ollama 兼容接口。
- role-specific LLM 配置：`EXTRACT`、`KEYWORD`、`QUERY`、`VLM`。
- 多存储后端：本地 JSON/NetworkX/NanoVector、PostgreSQL、MongoDB、OpenSearch、Neo4j、Milvus、Qdrant 等。

但 LightRAG 不应替代股票系统自己的强 schema 表。推荐架构是：

```text
LightRAG:
  文档级 RAG / 证据召回 / 通用实体关系图 / 长文档问答

stock_analysis_by_gpt:
  股票画像表 / deep tags / stock graph edges / attention_signal / 选股排序 / 回测
```

也就是说，LightRAG 做“研究语料和语义图谱引擎”，本项目做“金融结构化画像和可回测评分引擎”。

### LightRAG 在本项目里的职责

| 模块 | LightRAG 负责 | 本项目负责 |
|---|---|---|
| 文档解析 | 年报、招股书、研报、网页 evidence 文本入库 | source-aware search、URL 清洗、来源质量评分 |
| 图谱抽取 | 从文本抽通用 entity / relationship | 把实体映射成 `stock/product/technology/bottleneck` 强类型 |
| 检索 | `/query/data` 返回 entities、relationships、chunks、references | 过滤、重排、join 到股票画像表 |
| 问答 | `/query` 生成带引用的自然语言解释 | 控制投资语境、风险提示、选股解释格式 |
| 子图 | `/graphs?label=...` 看语义图谱 | `stock-subgraph` 看金融 schema 子图 |
| 增量 | 文档新增、删除、重处理 | 股票级 reindex trigger、打分更新、回测验证 |

### 推荐集成方式

不要把 LightRAG SDK 深度嵌入主项目第一版，优先使用 LightRAG API Server。LightRAG 官方 README 也建议项目集成优先使用 REST API，SDK 更适合实验和研究。

核心接口：

```text
POST /documents/texts
  批量写入 SearXNG evidence、公告摘要、论文摘要、新闻正文

POST /query/data
  获取结构化 retrieval data: entities / relationships / chunks / references

POST /query
  获取带引用的自然语言回答

GET /graph/label/search?q=...
  做 entity linking 辅助

GET /graphs?label=...&max_depth=2&max_nodes=200
  获取 LightRAG 子图
```

股票画像链路：

```text
source-aware evidence
-> 写入 LightRAG workspace
-> /query/data 召回实体、关系、chunks
-> LLM/规则映射成 stock_profile / stock_deep_tag_registry / stock_graph_edges
-> stock_analysis_by_gpt 仓库落表
-> stock-subgraph / rank-theme-opportunities / explain-stock-profile
```

### 需要二次定制的点

LightRAG 原生 entity 类型是通用语义图谱，不天然理解股票产业链。需要在本项目侧做一层适配：

1. **实体标准化**

```text
智谱 / Zhipu AI / Z.ai -> company:智谱
02513 / 02513.HK -> stock:02513
GLM / ChatGLM / GLM-5.1 -> product:GLM-5.1
大模型 / foundation model -> technology:大模型
推理算力 / inference compute -> bottleneck:推理算力
```

2. **关系映射**

```text
LightRAG relation text -> typed edge

"智谱发布 GLM-5.1" -> company:智谱 produces product:GLM-5.1
"GLM-5.1 属于大模型" -> product:GLM-5.1 belongs_to technology:大模型
"大模型推理依赖 GPU/HBM" -> technology:大模型 bottleneck supply_chain:推理算力
```

3. **证据质量重排**

LightRAG 召回结果要再按金融来源质量排序：

```text
HKEX/招股书/年报 > 公司官网/IR > 论文/技术报告 > GitHub/HuggingFace/榜单 > 新闻 > 社媒/论坛
```

4. **时间与热度字段**

LightRAG 更偏文档语义，不直接维护投资需要的时间序列。`attention_signal`、`freshness_days`、`attention_velocity_7d` 仍由本项目维护。

5. **强 schema 输出**

最终写入本项目表时，必须满足：

```text
stock_profile
stock_deep_tag_registry
stock_graph_nodes
stock_graph_edges
attention_signal
```

不要直接把 LightRAG 的通用 graph 当作最终选股图谱。

### LightRAG 部署文档

LightRAG 的安装、数据库后端选择、DeepSeek LLM 配置和 evidence 写入流程单独维护在：

```text
docs/done/P0_02_lightrag_deployment.md
```

本设计文档只保留架构取舍：LightRAG 负责 RAG 和通用图谱召回，本项目负责股票强 schema、选股排序和回测。

### 存储方案选择

PoC：

```text
JsonKVStorage
JsonDocStatusStorage
NetworkXStorage
NanoVectorDBStorage
```

优点是部署轻、适合本地验证 `02513`、`00700` 等样本。

第一版稳定运行：

```text
PostgreSQL all-in-one
```

优点是一个后端承接 KV、doc status、vector、graph，运维简单，推荐作为当前默认选择。

长期：

```text
KV/DOC: PostgreSQL
VECTOR: Qdrant 或 Milvus
GRAPH: Neo4j 或 Memgraph
```

当需要高性能路径查询、图谱可视化、产业链社区发现时，再把 graph storage 切到 Neo4j/Memgraph。股票系统自己的 `stock_graph_edges` 仍保留，用于选股、排序和回测。

### 与其他 GraphRAG 框架的取舍

| 方案 | 结论 |
|---|---|
| LightRAG | 第一优先。用于 evidence/document RAG、通用图谱抽取、引用和 WebUI |
| Microsoft GraphRAG | 适合行业报告/长文档全局总结实验，不作为第一版核心 |
| Neo4j GraphRAG | 中长期接入图数据库时再考虑 |
| LlamaIndex PropertyGraphIndex | 适合 PoC，不建议控制核心 schema |
| LangGraph | 后续做研究 agent 编排时使用，不作为图谱存储 |

### 第一版 LightRAG 定制目标

先实现三个适配器：

```text
lightrag_index_evidence(evidence_csv, alias_csv)
lightrag_retrieve_context(stock_code_or_theme, mode=mix)
lightrag_context_to_stock_graph(context_json)
```

第一版 retrieval 规则：

```text
1. entity linking: 股票代码/公司名/产品名 -> LightRAG label + 本项目 node_id
2. LightRAG retrieval: /query/data 获取 entities / relationships / chunks
3. stock schema mapping: 通用关系 -> typed edge
4. evidence join: references/chunks -> evidence_refs
5. context ranking: source_quality * confidence * freshness
6. LLM answer: 只允许引用 context 中的证据
```

这样既能借 LightRAG 的图谱 RAG 能力，又保留股票画像系统真正需要的可解释、可排序、可回测结构。

## 与现有系统的集成

保留现有表：

```text
tag_dictionary
stock_tag_registry
stock_tag_candidate
company_research_evidence
```

新增表：

```text
entity_alias_registry
stock_profile
stock_deep_tag_registry
stock_graph_nodes
stock_graph_edges
attention_signal
theme_opportunity_score
```

现有 `stock_tag_registry` 继续服务基础选股；新表用于深度主题研究和推荐式 watchlist。

## 产物分层

`docs/` 只放设计文档、小型 registry 和可复现流程说明，不放单股批量生成报告。上千只股票的产物按下面分层：

```text
output/stock_profiles/HK/<stock_code>/
  lightrag_context.json
  lightrag_profile_contexts.json
  graph_nodes.csv
  graph_edges.csv
  stock_profile_report.md
  stock_profile_report.json

output/theme_opportunity_score.csv
```

`output/` 已在 `.gitignore` 中，适合保存可读报告、大 JSON、调试上下文和批量中间 CSV。结构化、可查询、会参与选股排序的数据进入 Parquet/ClickHouse：`stock_profile`、`stock_deep_tag_registry`、`stock_graph_nodes`、`stock_graph_edges`、`attention_signal`、`theme_opportunity_score`。LightRAG/PostgreSQL 负责 RAG 文档、向量和索引召回，不作为最终选股事实表。

## CLI 设计

### 生成别名

```bash
uv run python run.py build-stock-entity-aliases \
  --stock-codes 02513 \
  --output docs/hk_entity_alias_registry.csv
```

### 深度 evidence 搜索

```bash
uv run python run.py research-stock-deep-profile \
  --stock-codes 02513 \
  --alias-csv docs/hk_entity_alias_registry.csv \
  --output docs/hk_stock_deep_evidence.csv \
  --searxng-url http://127.0.0.1:8888 \
  --max-results-per-query 5 \
  --max-queries-per-stock 8 \
  --engines bing,duckduckgo \
  --max-workers 4 \
  --show-progress
```

### 画像抽取

```bash
uv run python run.py extract-stock-profile-llm \
  --evidence-csv docs/hk_stock_deep_evidence.csv \
  --profile-output docs/hk_stock_profile.csv \
  --deep-tag-output docs/hk_stock_deep_tag_registry.csv \
  --node-output docs/hk_stock_graph_nodes.csv \
  --edge-output docs/hk_stock_graph_edges.csv \
  --llm-model deepseek-v4-pro \
  --show-progress
```

### 导入画像/图谱

```bash
uv run python run.py import-stock-profile-graph \
  --alias-csv docs/hk_entity_alias_registry.csv \
  --profile-csv docs/hk_stock_profile.csv \
  --deep-tag-csv docs/hk_stock_deep_tag_registry.csv \
  --node-csv docs/hk_stock_graph_nodes.csv \
  --edge-csv docs/hk_stock_graph_edges.csv
```

### 查询子图

```bash
uv run python run.py stock-subgraph 02513 \
  --node-csv docs/hk_stock_graph_nodes.csv \
  --edge-csv docs/hk_stock_graph_edges.csv \
  --json
```

`rank-theme-opportunities` 属于 Phase 4 推荐排序命令，第一版先用 `stock-subgraph` 和 deep tag 表完成 GraphRAG-like 召回与解释。

### LightRAG evidence 索引

```bash
uv run python run.py lightrag-index-evidence \
  --evidence-csv docs/hk_stock_deep_evidence.csv \
  --alias-csv docs/hk_entity_alias_registry.csv \
  --lightrag-url http://127.0.0.1:9621 \
  --show-progress
```

小样本验证：

```bash
uv run python run.py lightrag-index-evidence \
  --evidence-csv docs/hk_stock_deep_evidence.csv \
  --alias-csv docs/hk_entity_alias_registry.csv \
  --stock-codes 02513 \
  --limit 10 \
  --lightrag-url http://127.0.0.1:9621 \
  --show-progress
```

### LightRAG context 检索

```bash
uv run python run.py lightrag-retrieve-context 02513 \
  --alias-csv docs/hk_entity_alias_registry.csv \
  --lightrag-url http://127.0.0.1:9621 \
  --mode mix \
  --top-k 20 \
  --chunk-top-k 10
```

默认输出到 `output/stock_profiles/HK/02513/lightrag_context.json`。

### LightRAG context 转本地图谱

```bash
uv run python run.py lightrag-context-to-stock-graph 02513 \
  --context-json output/stock_profiles/HK/02513/lightrag_context.json
```

默认输出到 `output/stock_profiles/HK/02513/graph_nodes.csv` 和 `output/stock_profiles/HK/02513/graph_edges.csv`。

生成后可复用已有导入命令：

```bash
uv run python run.py import-stock-profile-graph \
  --alias-csv docs/hk_entity_alias_registry.csv \
  --node-csv output/stock_profiles/HK/02513/graph_nodes.csv \
  --edge-csv output/stock_profiles/HK/02513/graph_edges.csv
```

注意：LightRAG 的 text insert 是异步后台处理，刚插入后立刻检索可能出现 `Query returned no results`。全量索引后应先等后台 pipeline 完成，再运行 `lightrag-retrieve-context`。

## 分阶段落地

### Phase 1：修正搜索和标签质量

1. 建 `entity_alias_registry`。
2. SearXNG query 强制使用公司名/别名/产品名。
3. 对搜索 evidence 做 relevance filter。
4. 禁止搜索 query echo 进入关键词 tag 规则。

验收：

```text
02513 能抓到智谱/Zhipu/Z.ai/GLM 相关 evidence
不再出现 AEST/truck steps 等无关 evidence 作为主证据
```

### Phase 2：深度画像

1. 新增 `stock_profile`。
2. 新增 `stock_deep_tag_registry`。
3. LLM 输出产品、技术、产业链、风险、催化。
4. 每个 deep tag 至少绑定 evidence。

验收：

```text
02513 出现 GLM/大模型/AI Coding/Agentic AI 等细分标签
每个标签能追溯到官网、论文、榜单或新闻
```

### Phase 3：产业链图谱

1. 新增 `stock_graph_nodes` 和 `stock_graph_edges`。
2. 实现 Graph Indexer，把 evidence/profile 抽成 typed nodes/edges。
3. 支持 `stock -> product -> technology -> bottleneck -> supplier_class`。
4. 实现 `retrieve_stock_subgraph(stock_code, depth=2)`。
5. 支持图谱扩散召回相关股票。

验收：

```text
输入 大模型/推理算力 能召回模型厂商、算力、光模块、IDC、电力等链条
输入 02513 能返回 company/product/technology/bottleneck/evidence 子图
```

### Phase 4：热度和推荐

1. 新增 `attention_signal`。
2. 从现有 evidence/graph 派生本地热度弱信号。
3. 建 `theme_opportunity_score` 标准表，支持 Parquet/ClickHouse 落库。
4. 实现 `rank-theme-opportunities`，按主题召回并排序股票机会。
5. 外部新闻、GitHub、模型榜单、社媒、公众号 API 后续写入同一张 `attention_signal` 表。
6. 用回测或人工反馈更新策略画像。

闭环命令：

```bash
uv run python run.py derive-attention-signals \
  --evidence-csv docs/hk_stock_deep_evidence.csv \
  --alias-csv docs/hk_entity_alias_registry.csv \
  --node-csv output/stock_profiles/HK/02513/graph_nodes.csv \
  --edge-csv output/stock_profiles/HK/02513/graph_edges.csv \
  --output-csv output/attention_signal.csv \
  --import-to-warehouse

uv run python run.py rank-theme-opportunities 大模型 \
  --evidence-csv docs/hk_stock_deep_evidence.csv \
  --alias-csv docs/hk_entity_alias_registry.csv \
  --node-csv output/stock_profiles/HK/02513/graph_nodes.csv \
  --edge-csv output/stock_profiles/HK/02513/graph_edges.csv \
  --attention-csv output/attention_signal.csv \
  --output-csv output/theme_opportunities_ai.csv \
  --top-n 100 \
  --import-to-warehouse

uv run python run.py export-theme-score-features \
  --theme-score-csv output/theme_opportunities_ai.csv \
  --output-csv output/theme_opportunity_features_ai.csv \
  --import-to-warehouse
```

`theme_opportunity_score` 的组件包括：

```text
technology_score
commercialization_score
value_chain_score
bottleneck_score
catalyst_score
attention_score
evidence_quality_score
liquidity_score
technical_trend_score
risk_penalty
crowding_penalty
```

排序器的目标不是直接替代多因子选股，而是生成主题 overlay：主题召回、watchlist 优先级、产业链扩散候选和可解释打分。`export-theme-score-features` 会把主题分转成标准 features 长表，供 `factor-report`、LightGBM 或后续 `select` overlay 消费。

LightGBM 集成方式：

```text
theme_opportunity_score
-> export-theme-score-features
-> feature_set=theme_opportunity
-> LightGBM panel merge
-> theme_overlay_strength 小权重调最终 ranking_score
```

权重建议：

```text
模型内：作为普通 feature 交给 LightGBM 学，不手动放大。
模型外：theme_overlay_strength 从 0.05 起步，通常不超过 0.10-0.15。
用途：增强主题召回和排序，不替代价格趋势、质量、流动性、风险控制。
```

最终组合仍应经过流动性、风险、趋势、估值和回测验证。

验收：

```text
主题热度变化能进入 watchlist 排序
高热度但低证据质量的股票不会直接进入正式推荐
主题机会分可落库，并能作为 select/factor-report 的候选 overlay
```

## 风险和约束

1. 社媒数据容易噪声高、操纵强，只能作为弱信号。
2. 公众号和 Twitter 抓取存在平台限制，要注意合规和 API 规则。
3. LLM 容易过度联想，所有深度标签必须绑定 evidence。
4. 产业链图谱需要持续人工校验，否则会积累错误边。
5. 投资收益案例不能直接复制，必须转化为可验证的因子和流程。

## 推荐优先级

当前最值得先做：

```text
1. entity_alias_registry
2. source-aware search
3. relevance filter
4. stock_profile + deep_tag_registry
5. stock_graph_edges
```

Twitter、公众号、GitHub 热度可以作为第二阶段引入。先把“强证据画像”做准，再引入热度和推荐排序。
