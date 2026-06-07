# LightRAG 部署与股票画像集成指南

本文档用于单独说明 LightRAG 在本项目里的部署方式、数据库后端选择和 DeepSeek LLM 配置。

本地 LightRAG 源码路径：

```text
/Users/ccs/code/quant/LightRAG
```

## 最优部署方案

当前项目的最优方案是：

```text
LightRAG API Server
+ PostgreSQL all-in-one
+ DeepSeek LLM
+ Ollama bge-m3 embedding
+ stock_analysis_by_gpt 强 schema 画像表
```

具体后端：

| 层 | 选择 | 原因 |
|---|---|---|
| KV | `PGKVStorage` | 持久化 LLM cache、chunk、抽取中间结果，便于备份 |
| Doc status | `PGDocStatusStorage` | 管理文档状态和后台处理队列 |
| Vector | `PGVectorStorage` | 第一版足够，避免额外部署 Qdrant/Milvus |
| Graph | `PGGraphStorage` | 第一版够用，路径查询重了以后再迁移 Neo4j/Memgraph |
| LLM | DeepSeek OpenAI-compatible | 成本和中文金融语料友好 |
| Embedding | Ollama `bge-m3` | 本地多语种 embedding，避免索引阶段消耗外部 API |
| Rerank | 暂不开启 | 先保证 evidence/graph 质量，后续再加 |

一句话：**先用 PostgreSQL all-in-one 把 LightRAG 跑成稳定服务，OpenSearch/Neo4j/Qdrant 暂时不要上。**

为什么不是 OpenSearch：

- 当前最难的是 `evidence -> entity/relation -> typed stock graph` 的质量，而不是全文检索吞吐。
- OpenSearch 适合大规模全文检索平台，但部署、调参、排障成本更高。
- 本项目已经有 ClickHouse/Parquet 做结构化数据，第一版再引入 OpenSearch 会让基础设施过重。
- 如果后续新闻/公告/网页全文检索成为核心，再迁移到 OpenSearch all-in-one。

为什么不是 Qdrant/Milvus + Neo4j：

- 这套组合能力最强，但服务数量多。
- 当前图谱规模还没证明需要专用图数据库和专用向量库。
- 等 `stock_graph_edges` 的质量和数量稳定后，再拆分更稳。

LightRAG 负责：

```text
evidence/document RAG
通用实体关系抽取
结构化 retrieval data
带引用问答
WebUI 图谱浏览
```

本项目继续负责：

```text
stock_profile
stock_deep_tag_registry
stock_graph_nodes
stock_graph_edges
attention_signal
选股排序和回测
```

不要把 LightRAG 的通用 graph 直接当成最终股票图谱。LightRAG 是研究语料和 RAG 引擎，本项目自己的强 schema 表才是选股和回测依据。

## 推荐 `.env`

以下是当前项目建议直接采用的 `.env` 核心配置：

```bash
HOST=0.0.0.0
PORT=9621
WORKSPACE=hk_stock_profile
WEBUI_TITLE='HK Stock Profile LightRAG'
WEBUI_DESCRIPTION='Stock profile, industry chain and evidence RAG'

SUMMARY_LANGUAGE=Chinese
ENTITY_EXTRACTION_USE_JSON=true
ENABLE_CONTENT_HEADINGS=true

LIGHTRAG_KV_STORAGE=PGKVStorage
LIGHTRAG_DOC_STATUS_STORAGE=PGDocStatusStorage
LIGHTRAG_VECTOR_STORAGE=PGVectorStorage
LIGHTRAG_GRAPH_STORAGE=PGGraphStorage

POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=lightrag
POSTGRES_PASSWORD='change_me'
POSTGRES_DATABASE=lightrag_hk_stock
POSTGRES_MAX_CONNECTIONS=25
POSTGRES_VECTOR_INDEX_TYPE=HNSW
POSTGRES_HNSW_M=16
POSTGRES_HNSW_EF=200

LLM_BINDING=openai
LLM_BINDING_HOST=https://api.deepseek.com
LLM_BINDING_API_KEY=${DEEPSEEK_API_KEY}
LLM_MODEL=deepseek-v4-pro
MAX_ASYNC_LLM=4

EMBEDDING_BINDING=ollama
EMBEDDING_BINDING_HOST=http://localhost:11434
EMBEDDING_MODEL=bge-m3:latest
EMBEDDING_DIM=1024
EMBEDDING_TOKEN_LIMIT=8192
EMBEDDING_FUNC_MAX_ASYNC=8
EMBEDDING_BATCH_NUM=32

RERANK_BINDING=null
```

PostgreSQL 需要 `pgvector`。如果使用 `PGGraphStorage`，还需要 Apache AGE。LightRAG 的 setup 向导和 Docker 文档中有带 pgvector/AGE 的 PostgreSQL 镜像说明。

启动：

```bash
cd /Users/ccs/code/quant/LightRAG
source .venv/bin/activate

lightrag-server \
  --host 0.0.0.0 \
  --port 9621 \
  --working-dir /Users/ccs/code/quant/stock_analysis_by_gpt/assets/lightrag/rag_storage \
  --input-dir /Users/ccs/code/quant/stock_analysis_by_gpt/assets/lightrag/inputs \
  --workspace hk_stock_profile
```

WebUI：

```text
http://127.0.0.1:9621/webui
```

## 数据库后端选择

LightRAG 有四类存储：

| 存储 | 用途 |
|---|---|
| `KV_STORAGE` | LLM 缓存、chunk、抽取中间结果 |
| `DOC_STATUS_STORAGE` | 文档状态、处理队列 |
| `VECTOR_STORAGE` | chunks/entities/relationships 向量 |
| `GRAPH_STORAGE` | 知识图谱 |

### 方案 A：本地文件后端

配置：

```bash
LIGHTRAG_KV_STORAGE=JsonKVStorage
LIGHTRAG_DOC_STATUS_STORAGE=JsonDocStatusStorage
LIGHTRAG_GRAPH_STORAGE=NetworkXStorage
LIGHTRAG_VECTOR_STORAGE=NanoVectorDBStorage
```

优点：

- 启动最快。
- 无需额外数据库。
- 适合验证 `02513 -> 智谱 -> GLM-5.1 -> 大模型` 这类链路。

缺点：

- 不适合长时间服务化。
- 多进程/多实例扩展能力弱。
- 图谱和向量数据规模上来后维护成本高。

建议：只用于 PoC、小样本调试、离线实验。

### 方案 B：PostgreSQL all-in-one

配置：

```bash
LIGHTRAG_KV_STORAGE=PGKVStorage
LIGHTRAG_DOC_STATUS_STORAGE=PGDocStatusStorage
LIGHTRAG_VECTOR_STORAGE=PGVectorStorage
LIGHTRAG_GRAPH_STORAGE=PGGraphStorage

POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=lightrag
POSTGRES_PASSWORD='change_me'
POSTGRES_DATABASE=lightrag_hk_stock
POSTGRES_MAX_CONNECTIONS=25
POSTGRES_VECTOR_INDEX_TYPE=HNSW
POSTGRES_HNSW_M=16
POSTGRES_HNSW_EF=200
```

优点：

- 一个数据库承接 KV、doc status、vector、graph。
- 持久化和备份简单。
- 对本项目第一版足够：3,000 多只港股，几十万级 evidence/chunk 可以先扛住。
- 运维复杂度明显低于 OpenSearch/Milvus/Neo4j 多服务组合。

缺点：

- `PGGraphStorage` 依赖 PostgreSQL + Apache AGE，图遍历性能不如 Neo4j/Memgraph。
- 更换 embedding 维度后，需要清空相关向量数据并重新索引。
- 对超大规模向量检索不如 Qdrant/Milvus。

建议：作为第一版稳定运行的默认选择。

### 方案 C：OpenSearch all-in-one

配置：

```bash
LIGHTRAG_KV_STORAGE=OpenSearchKVStorage
LIGHTRAG_DOC_STATUS_STORAGE=OpenSearchDocStatusStorage
LIGHTRAG_VECTOR_STORAGE=OpenSearchVectorDBStorage
LIGHTRAG_GRAPH_STORAGE=OpenSearchGraphStorage

OPENSEARCH_HOSTS=localhost:9200
OPENSEARCH_USER=admin
OPENSEARCH_PASSWORD='change_me'
OPENSEARCH_USE_SSL=true
OPENSEARCH_VERIFY_CERTS=false
```

优点：

- 一个后端同时支持 KV、向量、图和全文检索。
- 横向扩展能力强。
- 对新闻/公告/网页全文检索友好。

缺点：

- 运维重。
- LightRAG 文档建议新部署使用 OpenSearch 3.3.0 或更高版本。
- 本项目已经有 ClickHouse/Parquet，第一版再引入 OpenSearch 容易过度复杂。

建议：如果后续要做全文检索平台和大规模 RAG 服务，再考虑。

### 方案 D：PostgreSQL + Qdrant/Milvus + Neo4j/Memgraph

配置方向：

```bash
LIGHTRAG_KV_STORAGE=PGKVStorage
LIGHTRAG_DOC_STATUS_STORAGE=PGDocStatusStorage
LIGHTRAG_VECTOR_STORAGE=QdrantVectorDBStorage
LIGHTRAG_GRAPH_STORAGE=Neo4JStorage
```

或：

```bash
LIGHTRAG_VECTOR_STORAGE=MilvusVectorDBStorage
LIGHTRAG_GRAPH_STORAGE=MemgraphStorage
```

优点：

- 各服务负责自己最擅长的部分。
- Qdrant/Milvus 更适合大规模向量。
- Neo4j/Memgraph 更适合产业链路径查询、社区发现、图谱可视化。

缺点：

- 部署和排障复杂。
- 数据一致性、备份、迁移都要管理。
- 第一版容易把精力花在基础设施，而不是标签和证据质量。

建议：中长期升级路线，不作为当前默认方案。

## 推荐后端

当前项目推荐：

```text
直接采用 PostgreSQL all-in-one
PoC 本地文件后端只作为临时诊断方案
等图谱质量和规模稳定后，再拆 Neo4j/Qdrant
```

理由：

1. LightRAG 在本项目里不是交易/回测核心库，只是 RAG 服务。
2. 第一版最重要的是 evidence 质量、实体标准化和 typed edge 映射。
3. PostgreSQL all-in-one 足够稳，运维开销最低，能避免重复索引和二次迁移。
4. 真正需要路径性能时，再把 graph storage 迁移到 Neo4j/Memgraph。

## DeepSeek LLM 配置

LightRAG 支持 `openai` binding 和 OpenAI-compatible endpoint，因此支持 DeepSeek。

推荐配置：

```bash
LLM_BINDING=openai
LLM_BINDING_HOST=https://api.deepseek.com
LLM_BINDING_API_KEY=${DEEPSEEK_API_KEY}
LLM_MODEL=deepseek-v4-pro
MAX_ASYNC_LLM=4
```

如果当前 DeepSeek 账户只支持官方别名模型，可以临时改为：

```bash
LLM_MODEL=deepseek-chat
```

或推理模型：

```bash
LLM_MODEL=deepseek-reasoner
```

注意：

- DeepSeek 官方 OpenAI-format base URL 是 `https://api.deepseek.com`。
- 官方文档已提示 `deepseek-chat` / `deepseek-reasoner` 这些旧模型名会在 `2026-07-24` 停用；本项目默认使用 `deepseek-v4-pro`，实际可用模型以你的 DeepSeek 控制台和 API 返回为准。
- LightRAG 的图谱抽取阶段对 LLM 稳定性要求高，建议 `ENTITY_EXTRACTION_USE_JSON=true`。
- 如果 `deepseek-reasoner` 不支持某些参数或响应格式，抽取阶段优先用 `deepseek-v4-pro` 或 `deepseek-chat`，最终问答阶段再考虑推理模型。

### 角色化 LLM 配置

如果后续要降低成本，可使用 LightRAG 的 role-specific LLM：

```bash
LLM_BINDING=openai
LLM_BINDING_HOST=https://api.deepseek.com
LLM_BINDING_API_KEY=${DEEPSEEK_API_KEY}
LLM_MODEL=deepseek-v4-pro

EXTRACT_LLM_BINDING=openai
EXTRACT_LLM_BINDING_HOST=https://api.deepseek.com
EXTRACT_LLM_BINDING_API_KEY=${DEEPSEEK_API_KEY}
EXTRACT_LLM_MODEL=deepseek-v4-pro

KEYWORD_LLM_BINDING=openai
KEYWORD_LLM_BINDING_HOST=https://api.deepseek.com
KEYWORD_LLM_BINDING_API_KEY=${DEEPSEEK_API_KEY}
KEYWORD_LLM_MODEL=deepseek-v4-flash

QUERY_LLM_BINDING=openai
QUERY_LLM_BINDING_HOST=https://api.deepseek.com
QUERY_LLM_BINDING_API_KEY=${DEEPSEEK_API_KEY}
QUERY_LLM_MODEL=deepseek-v4-pro
```

建议：

- `EXTRACT` 用更强模型，保证 entity/relation 抽取质量。
- `KEYWORD` 可以用更快更便宜模型。
- `QUERY` 用回答质量更好的模型。
- `VLM` 只有开启多模态图片分析时才需要，普通网页/CSV evidence 暂时不需要。

## Embedding 配置

第一版推荐本地 embedding，避免每次索引 evidence 都消耗外部 API：

```bash
EMBEDDING_BINDING=ollama
EMBEDDING_BINDING_HOST=http://localhost:11434
EMBEDDING_MODEL=bge-m3:latest
EMBEDDING_DIM=1024
EMBEDDING_TOKEN_LIMIT=8192
EMBEDDING_FUNC_MAX_ASYNC=8
EMBEDDING_BATCH_NUM=32
```

准备：

```bash
ollama pull bge-m3
```

重要：

- embedding 模型和维度必须在索引前确定。
- 更换 `EMBEDDING_MODEL`、`EMBEDDING_DIM` 或 query/document 前缀后，需要清空 LightRAG workspace/vector 数据并重新索引。
- 中文/英文混合语料建议用多语种 embedding，例如 `bge-m3`。

## 本地部署流程

### 1. 初始化环境

```bash
cd /Users/ccs/code/quant/LightRAG
make dev
source .venv/bin/activate
```

如果不跑 `make dev`：

```bash
uv sync --extra test --extra offline
source .venv/bin/activate

cd lightrag_webui
bun install --frozen-lockfile
bun run build
cd ..
```

### 2. 生成 `.env`

```bash
make env-base
make env-storage
make env-server
make env-security-check
```

也可以手动：

```bash
cp env.example .env
```

### 3. PoC `.env`

小样本验证时：

```bash
HOST=0.0.0.0
PORT=9621
WORKSPACE=hk_stock_profile
WEBUI_TITLE='HK Stock Profile LightRAG'
WEBUI_DESCRIPTION='Stock profile, industry chain and evidence RAG'

SUMMARY_LANGUAGE=Chinese
ENTITY_EXTRACTION_USE_JSON=true
ENABLE_CONTENT_HEADINGS=true

LIGHTRAG_KV_STORAGE=JsonKVStorage
LIGHTRAG_DOC_STATUS_STORAGE=JsonDocStatusStorage
LIGHTRAG_GRAPH_STORAGE=NetworkXStorage
LIGHTRAG_VECTOR_STORAGE=NanoVectorDBStorage

LLM_BINDING=openai
LLM_BINDING_HOST=https://api.deepseek.com
LLM_BINDING_API_KEY=${DEEPSEEK_API_KEY}
LLM_MODEL=deepseek-v4-pro
MAX_ASYNC_LLM=4

EMBEDDING_BINDING=ollama
EMBEDDING_BINDING_HOST=http://localhost:11434
EMBEDDING_MODEL=bge-m3:latest
EMBEDDING_DIM=1024
EMBEDDING_TOKEN_LIMIT=8192
EMBEDDING_FUNC_MAX_ASYNC=8
EMBEDDING_BATCH_NUM=32

RERANK_BINDING=null
```

### 4. PostgreSQL all-in-one `.env`

稳定运行时：

```bash
HOST=0.0.0.0
PORT=9621
WORKSPACE=hk_stock_profile

SUMMARY_LANGUAGE=Chinese
ENTITY_EXTRACTION_USE_JSON=true
ENABLE_CONTENT_HEADINGS=true

LIGHTRAG_KV_STORAGE=PGKVStorage
LIGHTRAG_DOC_STATUS_STORAGE=PGDocStatusStorage
LIGHTRAG_VECTOR_STORAGE=PGVectorStorage
LIGHTRAG_GRAPH_STORAGE=PGGraphStorage

POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=lightrag
POSTGRES_PASSWORD='change_me'
POSTGRES_DATABASE=lightrag_hk_stock
POSTGRES_MAX_CONNECTIONS=25
POSTGRES_VECTOR_INDEX_TYPE=HNSW

LLM_BINDING=openai
LLM_BINDING_HOST=https://api.deepseek.com
LLM_BINDING_API_KEY=${DEEPSEEK_API_KEY}
LLM_MODEL=deepseek-v4-pro
MAX_ASYNC_LLM=4

EMBEDDING_BINDING=ollama
EMBEDDING_BINDING_HOST=http://localhost:11434
EMBEDDING_MODEL=bge-m3:latest
EMBEDDING_DIM=1024
EMBEDDING_TOKEN_LIMIT=8192
EMBEDDING_FUNC_MAX_ASYNC=8
EMBEDDING_BATCH_NUM=32
```

PostgreSQL 需要 pgvector；如果使用 `PGGraphStorage`，还需要 Apache AGE。LightRAG 的 Docker/setup 向导会倾向使用带 pgvector/AGE 的 PostgreSQL 镜像。

### 5. 启动服务

```bash
lightrag-server \
  --host 0.0.0.0 \
  --port 9621 \
  --working-dir /Users/ccs/code/quant/stock_analysis_by_gpt/assets/lightrag/rag_storage \
  --input-dir /Users/ccs/code/quant/stock_analysis_by_gpt/assets/lightrag/inputs \
  --workspace hk_stock_profile
```

WebUI：

```text
http://127.0.0.1:9621/webui
```

如果配置了 API key：

```bash
curl -H "X-API-Key: your-secure-api-key" http://127.0.0.1:9621/health
```

## 与股票画像系统集成

### 写入 evidence

计划新增命令：

```bash
uv run python run.py lightrag-index-evidence \
  --evidence-csv docs/hk_stock_deep_evidence.csv \
  --alias-csv docs/hk_entity_alias_registry.csv \
  --lightrag-url http://127.0.0.1:9621 \
  --workspace hk_stock_profile \
  --stock-codes 02513 \
  --show-progress
```

每条 evidence 写成 Markdown 文本，调用：

```text
POST /documents/texts
```

建议 `file_source`：

```text
stock/02513/evidence/<hash>.md
```

建议文本：

```text
# 02513 智谱 evidence

stock_code: 02513
aliases: 智谱;Zhipu AI;Z.ai;GLM;GLM-5.1
source: source_aware_search
url: https://...
fetched_at: ...

## title
...

## summary
...

## raw_text
...
```

### 查询上下文

计划新增命令：

```bash
uv run python run.py lightrag-query-stock-profile \
  --stock-code 02513 \
  --lightrag-url http://127.0.0.1:9621 \
  --mode mix \
  --output-json /private/tmp/02513_lightrag_context.json
```

内部调用：

```text
POST /query/data
```

请求：

```json
{
  "query": "02513 智谱 Zhipu AI GLM-5.1 的产品、技术、产业链瓶颈、AI Coding 能力和证据是什么？",
  "mode": "mix",
  "top_k": 20,
  "include_references": true
}
```

返回的 `entities / relationships / chunks / references` 再映射回：

```text
stock_profile
stock_deep_tag_registry
stock_graph_nodes
stock_graph_edges
```

## 迁移和清理注意事项

1. 更换 embedding 模型或维度后，清空 LightRAG workspace/vector 数据并重新索引。
2. 更换 storage backend 时，不建议直接迁移内部索引；更稳妥方式是从 evidence CSV 重新灌入。
3. LightRAG 的 `WORKSPACE` 用于隔离不同知识库，建议固定为 `hk_stock_profile`。
4. 不要提交 `.env`、API key、数据库密码。
5. RAG 召回结果只能作为证据上下文，正式选股仍以本项目结构化表和回测为准。

## 推荐下一步

1. 按 PostgreSQL all-in-one `.env` 启动 LightRAG。
2. 把 `02513` 的 `docs/hk_stock_deep_evidence.csv` 写入 LightRAG。
3. 调 `/query/data` 看能否稳定召回 `智谱 / Zhipu AI / GLM-5.1 / 大模型 / AI Coding`。
4. 再实现 `lightrag_context_to_stock_graph`，把 LightRAG 结果转成强 schema 图谱。

## 参考

- 本地 LightRAG 文档：`/Users/ccs/code/quant/LightRAG/README.md`
- 本地 LightRAG API Server 文档：`/Users/ccs/code/quant/LightRAG/docs/LightRAG-API-Server-zh.md`
- 本地 LightRAG 编程与存储文档：`/Users/ccs/code/quant/LightRAG/docs/ProgramingWithCore.md`
- DeepSeek API 快速开始：`https://api-docs.deepseek.com/`
- DeepSeek Chat Completion：`https://api-docs.deepseek.com/api/create-chat-completion`
- DeepSeek Models & Pricing：`https://api-docs.deepseek.com/quick_start/pricing`
