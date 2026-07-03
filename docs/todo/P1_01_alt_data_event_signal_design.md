# 另类数据事件驱动信号引擎 — 设计方案

## 1. 核心洞察

股价是果，信息是因。2026-02-06 的 02513（智谱 AI）7 倍涨幅复盘：

```
OpenRouter 匿名模型 "Pony Alpha" 上线
    → 社区盲测发现性能接近 GLM-4.5+
    → 推测是智谱 GLM-5 预发布
    → "全球大模型第一股" 叙事形成
    → 稀缺性定价: 203 → 725（14天 3.5x）
```

价格因子（RPS/ROC/均线）只能描述"涨了"，无法解释"**为什么涨**"。
另类数据的价值：**在信息传播链上游抓信号，趁价格还没反应完先进场。**

---

## 2. 架构总览

```
                        信号工厂
                    ==================

数据源层              实体识别层          映射层              因子输出
────────             ──────────         ──────             ──────────
Twitter/X    ──┐                        entity → ticker
Reddit       ──┤     spaCy NER     ──→  product → company  ──→  ClickHouse
GitHub       ──┤     关键词字典          keyword → sector      features 表
HuggingFace  ──┤                                          
arXiv        ──┤     事件分类引擎  
新闻 RSS     ──┘     情感打分
                     (FinBERT)
                         │
                    异常检测
                    (3σ / MAD)
                         │
                    ┌────┴────┐
                    │ 信号融合  │
                    │ 事件×情绪 │
                    └────┬────┘
                         ↓
                   BUY/SELL 信号
```

三层递进设计：

| 层 | 技术栈 | 说明 |
|---|---|---|
| L1: 关注度异常 | SQL + 统计 | **不需要 NLP**，关键词提及量环比 > 3σ 即信号 |
| L2: 实体映射 | spaCy NER + 知识图谱 | 自动关联"Pony Alpha"→ 智谱 → 02513 |
| L3: 情感+叙事 | FinBERT / LLM | 判断利好（模型突破）vs 利空（安全事故），每天跑一次 |

---

## 3. 数据源矩阵

### 3.1 技术社区监控（AI/科技股核心）

| 数据源 | 开源工具 | API 难度 | 免费额度 | 信号类型 |
|---|---|---|---|---|
| **GitHub** | `github-trending-api` | 简单 REST | 5000 req/h | repo stars/forks/issues 突增 |
| **HuggingFace** | `huggingface_hub` SDK | 简单 SDK | 无限制 | 模型下载量、Space 使用量、新模型上线 |
| **arXiv** | `arxiv` Python 包 | 简单 | 无限制 | AI 论文作者机构 → 映射上市公司 |
| **OpenRouter** | `openrouter-stats` (社区) | 中等 | 需 API Key | 新模型上线、调用量排名、token 消耗 |
| **LMSYS Arena** | `chatbot-arena-scraper` (社区) | 中等 | 无限制 | 匿名模型排名突升 |

### 3.2 舆情与新闻（全行业覆盖）

| 数据源 | 开源工具 | API 难度 | 免费额度 | 信号类型 |
|---|---|---|---|---|
| **Twitter/X** | `tweepy` + X API v2 | 简单 | 100 条/月 | 股票/产品提及量暴增 |
| **Reddit** | `praw` (Reddit API) | 简单 | 60 req/min | subreddit 热门话题、WBS 讨论量 |
| **新闻 RSS** | `feedparser` + Google News | 简单 | 无限制 | 公司名+事件关键词共现频率 |
| **SEC/港交所** | EDGAR / HKEX API | 简单 | 无限制 | 公告、持仓变动、大股东增减持 |

### 3.3 国内另类数据补充

| 数据源 | 开源工具 | 说明 |
|---|---|---|
| **雪球** | `Crawlee` 抓取 | 热门讨论、大 V 观点、个股页关注数变化 |
| **东方财富股吧** | `Crawlee` 抓取 | 帖子量突增、关键词频率 |
| **AKShare** | `akshare` (已有) | 龙虎榜、南向资金、行业资金流 |

---

## 4. 实体识别 + 映射层（核心投入，一次性建设）

### 4.1 知识图谱表结构（DuckDB 轻量方案）

```sql
-- 公司实体表
CREATE TABLE entity_company (
    entity_name TEXT PRIMARY KEY,     -- "智谱" / "Zhipu AI" / "Zhipu"
    aliases TEXT,                      -- ["智谱华章","清华智谱","THUDM"]
    ticker TEXT,                       -- "02513"
    market TEXT,                       -- "HK"
    sector TEXT,                       -- "人工智能"
    competitors TEXT                   -- ["百度","商汤","科大讯飞"]
);

-- 产品映射表
CREATE TABLE entity_product (
    product_name TEXT,                 -- "GLM-5" / "Pony Alpha" / "ChatGLM"
    company_entity TEXT,               -- → entity_company.entity_name
    product_type TEXT,                 -- "LLM" / "chip" / "drug" / "software"
    is_confirmed BOOLEAN              -- True=官方确认, False=社区推测
);

-- 关键词表
CREATE TABLE entity_keyword (
    keyword TEXT PRIMARY KEY,          -- "大模型" / "Agent" / "MoE"
    sector TEXT,                       -- "人工智能"
    relevance_weight FLOAT            -- 0.0-1.0
);

-- 事件分类规则表
CREATE TABLE event_rules (
    rule_id INTEGER PRIMARY KEY,
    rule_name TEXT,                    -- "model_launch" / "funding_round" / "security_incident"
    keyword_pattern TEXT,              -- "launch|release|上线|发布"
    sentiment_default TEXT,           -- "positive" / "negative" / "neutral"
    impact_window_days INTEGER        -- 事件有效期
);
```

初期数据量：100 家港股科技公司 × 3-5 个产品 × 10-20 个关键词。几百行数据，手动维护即可，不需要外部数据源。

### 4.2 NER 流水线

```python
# L1: 关键词字典匹配（不需要 NLP）
keywords = load_entity_dictionary()  # 从 DuckDB 加载
for text in stream:
    matches = {k for k in keywords if k in text}
    if matches:
        tickers = resolve_to_tickers(matches)
        emit_signal(tickers, source=text.source, ts=text.timestamp)

# L2: spaCy NER 发现新实体
nlp = spacy.load("en_core_web_sm")
doc = nlp(text)
new_orgs = [ent.text for ent in doc.ents if ent.label_ == "ORG"]
# 不在字典里的新实体 → 人工审核 → 加入字典

# L3: LLM 语义理解（每天批量跑，不是实时）
# 输入: 当日所有触发关键词的文本
# 输出: {entity, event_type, sentiment, confidence}
summary = llm.summarize(daily_texts)
update_event_table(summary)
```

---

## 5. 信号生成工厂

### 5.1 关注度异常信号（L1，通用性最强）

```python
def compute_attention_anomaly(entity_id, window_hours=24):
    """
    某实体在最近 24h 的提及量 vs 过去 30 天均值。
    对所有实体通用，不针对特定股票。
    """
    recent = count_mentions(entity_id, hours=window_hours)
    baseline_mean, baseline_std = get_baseline(entity_id, days=30)
    
    z_score = (recent - baseline_mean) / max(baseline_std, 1)
    return {
        "entity": entity_id,
        "z_score": z_score,
        "signal": "attention_anomaly",
        "strength": min(z_score / 5.0, 1.0),  # 归一化到 0-1
        "tickers": resolve_tickers(entity_id),
    }
```

### 5.2 事件驱动信号（L2）

```python
def compute_event_signal(entity_id):
    """
    事件分类 + 情感 + 关注度的加权信号。
    """
    events = get_recent_events(entity_id, hours=48)
    sentiment = get_sentiment(entity_id, hours=48)
    attention = compute_attention_anomaly(entity_id)
    
    # 利好事件 + 正面情绪 + 高关注度 = 强买入
    positive_events = [e for e in events if e.sentiment == "positive"]
    if not positive_events:
        return None
    
    top_event = max(positive_events, key=lambda e: e.confidence)
    
    return {
        "entity": entity_id,
        "event_type": top_event.type,          # "model_launch" / "funding" / ...
        "event_sentiment": sentiment.score,     # -1.0 to 1.0
        "attention_z": attention["z_score"],
        "composite_score": top_event.confidence * sentiment.score * min(attention["z_score"] / 5, 1),
        "tickers": resolve_tickers(entity_id),
        "narrative": top_event.summary,
    }
```

### 5.3 因子输出格式

所有信号统一写入 ClickHouse `features_feature` 表，与 Alpha158 的 198 个因子并列：

```sql
-- 每个股票每天一条记录
feature_name                        feature_value   source
───────────────────────────────────────────────────────────
alt_attention_anomaly_zscore        2.3             alt_data
alt_event_composite_score           0.72            alt_data
alt_sentiment_24h                   0.65            alt_data
alt_community_activity             192             alt_data     -- GitHub/HF 活跃度
alt_narrative_strength              0.88            alt_data     -- LLM 叙事置信度
```

LightGBM 训练时自动看到这些特征，模型自己决定给多少权重。

---

## 6. 开源工具选型

| 模块 | 推荐方案 | 替代方案 | 部署要求 |
|---|---|---|---|
| **NLP/NER** | spaCy (`en_core_web_sm`) | Flair, Stanza | 12MB 模型，笔记本 CPU |
| **金融情感分析** | FinBERT (`ProsusAI/finbert`) | FinGPT, FinBERT-Tone | 400MB 模型，可本地 CPU |
| **数据采集** | `tweepy` + `praw` + `feedparser` | Crawlee (反爬) | Python 纯代码 |
| **HF Hub 监控** | `huggingface_hub` SDK | — | `pip install` |
| **GitHub 监控** | `PyGithub` + `github-trending-api` | — | `pip install` |
| **LLM 精判** | DeepSeek API / GLM API | 本地部署 Qwen 等 | API Key，按量计费 |
| **数据编排** | Prefect (推荐) | Dagster, Airflow | 轻量 Python 任务调度 |
| **知识图谱** | DuckDB (已有) | Neo4j (大项目) | 零额外依赖 |

---

## 7. 分阶段实施路线

### Phase 1: 关注度异常检测（2-3 天）

```
□ 建 DuckDB 实体字典（100 家港股科技公司 + 500 个关键词）
□ 接入 Twitter/X API v2（免费 tier）
□ 接入 Reddit API（praw，免费）
□ 接入 HuggingFace Hub API（监控模型下载量）
□ 接入 GitHub API（监控 AI repo stars）
□ 实现 24h 提及量异常检测（z-score > 3σ）
□ 结果写入 alt_attention_anomaly feature → ClickHouse
```

### Phase 2: 事件识别 + 情感分析（3-5 天）

```
□ 部署 FinBERT 本地推理
□ 接入新闻 RSS（Google News + 财联社）
□ 实现事件分类规则引擎
□ 实现 entity → ticker 映射
□ 生成 alt_event_composite_score feature
```

### Phase 3: 信号融合 + 回测（3-5 天）

```
□ 事件信号 + 价格信号 融合逻辑
□ 回测框架验证历史事件（已知事件→ 验证信号是否提前触发）
□ 加入现有 LightGBM 训练 pipeline
□ 独立的 Alpha 信号 recipe
```

---

## 8. 与现有系统的集成点

```
现有系统                            另类数据模块（新增）
─────────                          ────────────────
generate_factors (Alpha158 + RPS)   ←  alt_features (新写入 ClickHouse)
validate_factors                    ←  自动看到 alt_* 特征
select_stocks (LightGBM)            ←  alt_* 特征参与训练
strategy_signals                    ←  alt_event_signal recipe (新)

数据流:
  Twitter/GitHub/HF/Reddit/News
      ↓ (Prefect 定时任务，每小时)
  实体识别 + 异常检测
      ↓
  ClickHouse features_feature 表
      ↓
  和 Alpha158 一起进 LightGBM
```

---

## 9. 注意事项

- **合规第一**：Twitter/Reddit 用官方 API，不用爬虫。GitHub/HF 都有公开 API。OpenRouter 如无官方 API，暂用社区统计项目。
- **噪音过滤**：另类数据噪声极大。必须用 z-score ≥ 3 做硬过滤，否则误报率太高。
- **延迟要求**：关注度异常需要小时级更新，情感/叙事分析每天跑一次即可。
- **成本控制**：小模型 (FinBERT) 本地跑，大模型 (GLM/DeepSeek) 仅做每日总结，API 调用量可控。
- **持续验证**：每季度用真实涨跌回测信号有效性，手动更新实体字典和事件规则。
