# 港股行业与主题标签知识图谱设计

> 日期：2026-06-03  
> 目标：把当前 `stock_info_registry` 的主行业字段升级为可复用的多标签知识图谱，支持行业选股、热点识别、新闻/社媒舆情关联、龙头识别和多行业公司归因。

---

## 1. 背景与问题

当前 `docs/hk_industry_registry.csv` 已能提供 `industry_l1/industry_l2/theme_tags`，适合作为行业底座，但仍有几个限制：

1. `theme_tags` 是单字段派生摘要，不适合承载多来源、多置信度、多业务线标签。
2. 泛化标签如 `科技`、`消费`、`资源材料` 区分度不足，难以识别 `AI`、`token`、`铜矿`、`铁矿`、`创新药`、`算力` 等热点。
3. 多行业公司无法只用一个 `industry_l2` 描述，例如地产+物业、矿业+新能源材料、互联网+游戏+云服务。
4. 新闻和社媒事件天然是标签化的，若没有标准 tag 字典，很难把事件可靠映射回股票池。

设计方向：保留主行业表，同时新增“标签字典 + 股票标签长表 + 事件标签表”，让行业、主题、资源品、产业链位置、业务线都能独立表达和打分。

---

## 2. 设计原则

1. **主行业稳定，主题标签灵活**  
   `industry_l1/l2/l3` 用于行业内比较和组合风险预算；主题标签用于热点、事件和舆情映射。

2. **高精度标签不和低置信标签混放**  
   高置信标签进入正式表；低置信标签进入候选表或以较低 `confidence` 标记，避免污染选股逻辑。

3. **标签必须有来源和置信度**  
   每个 tag 都要知道来自公司简介、年报、新闻、规则、LLM 抽取还是人工确认。

4. **CSV 只做导入/人工编辑入口，不做唯一模型**  
   `theme_tags` 继续用英文分号 `;` 分隔，方便兼容和人工查看；真正查询使用长表。

5. **多行业公司允许多个标签并存**  
   一家公司可以同时有 `地产`、`物业管理`、`数据中心`、`高股息` 等标签，靠 `tag_type/confidence/source` 区分用途。

---

## 3. 数据表设计

### 3.1 `stock_info_registry`：主画像表

继续作为股票基础信息和主行业入口。

| 字段 | 说明 |
|---|---|
| `stock_code` | 港股 5 位代码 |
| `market` | HK |
| `name` | 股票名称 |
| `industry_l1` | 一级主行业 |
| `industry_l2` | 二级主行业 |
| `industry_l3` | 三级主行业，可选 |
| `theme_tags` | 派生摘要，英文分号分隔 |
| `industry_source` | 主行业来源 |
| `industry_updated_at` | 更新时间 |
| `instrument_type` | `common_stock` / `fund_like` |
| `is_fund_like` | 是否 ETF/基金/REIT/杠杆反向产品 |

使用方式：

- 行业内选股、行业中性、行业风险预算优先使用 `industry_l1/l2`。
- `theme_tags` 仅作为兼容字段和快速查看字段，不作为最终知识图谱。

### 3.2 `tag_dictionary`：标准标签字典

统一 tag 的名称、类型、别名和说明。

| 字段 | 说明 |
|---|---|
| `tag` | 标准标签，如 `AI`、`铜`、`算力` |
| `tag_type` | `theme` / `resource` / `value_chain` / `business` / `risk` / `geo` / `style` |
| `canonical_tag` | 标准名，通常等于 `tag` |
| `aliases` | 英文分号分隔别名，如 `人工智能;AIGC;大模型` |
| `description` | 标签解释 |
| `parent_tag` | 上级标签，可选，如 `AI` 的上级为 `科技` |
| `active` | 是否启用 |
| `updated_at` | 更新时间 |

示例：

| tag | tag_type | aliases | parent_tag |
|---|---|---|---|
| `AI` | `theme` | `人工智能;AIGC;大模型;Agent` | `科技` |
| `算力` | `value_chain` | `数据中心;IDC;GPU服务器` | `AI` |
| `铜` | `resource` | `铜矿;铜金属;铜资源` | `资源材料` |
| `铁矿` | `resource` | `铁矿石;铁矿资源` | `资源材料` |
| `token` | `theme` | `代币;稳定币;RWA;Web3` | `数字资产` |

### 3.3 `stock_tag_registry`：股票-标签关系表

核心多对多表，一只股票可以对应多个 tag。

| 字段 | 说明 |
|---|---|
| `stock_code` | 港股代码 |
| `market` | HK |
| `tag` | 标准标签 |
| `tag_type` | 与 `tag_dictionary.tag_type` 对齐 |
| `confidence` | 0-1，标签置信度 |
| `is_primary` | 是否核心业务/核心主题 |
| `source` | `manual` / `company_profile` / `annual_report` / `news` / `rule` / `llm` |
| `evidence` | 简短证据，如主营业务描述或新闻摘要 |
| `evidence_url` | 来源 URL，可选 |
| `updated_at` | 更新时间 |

示例：

| stock_code | tag | tag_type | confidence | is_primary | source |
|---|---|---|---:|---|---|
| `00700` | `游戏` | `business` | 0.95 | true | `company_profile` |
| `00700` | `AI` | `theme` | 0.70 | false | `news/llm` |
| `01208` | `铜` | `resource` | 0.90 | true | `company_profile` |
| `01024` | `AI医疗` | `theme` | 0.80 | false | `news` |

### 3.4 `stock_tag_candidate`：候选标签表

低置信、待审核、LLM 初筛标签先进入候选表。

| 字段 | 说明 |
|---|---|
| `stock_code` | 港股代码 |
| `tag` | 候选标签 |
| `tag_type` | 标签类型 |
| `confidence` | 0-1 |
| `source` | 来源 |
| `evidence` | 证据 |
| `review_status` | `pending` / `accepted` / `rejected` |
| `review_note` | 人工审核备注 |
| `updated_at` | 更新时间 |

候选表的作用是提高召回，但不直接污染正式选股逻辑。

### 3.5 `news_tag_events`：新闻/社媒事件标签表

用于热点识别和事件驱动信号。

| 字段 | 说明 |
|---|---|
| `event_time` | 事件时间 |
| `source` | 新闻、RSS、雪球、股吧、X、Reddit 等 |
| `title` | 标题 |
| `content_hash` | 内容去重 |
| `tag` | 事件标签 |
| `sentiment` | 情绪分数 |
| `heat_score` | 热度分数 |
| `evidence` | 摘要或命中片段 |

关联方式：

```
news_tag_events.tag
    -> stock_tag_registry.tag
    -> stock_code
    -> 因子/流动性/行业内排名/组合约束
```

---

## 4. 标签类型与建议粒度

| tag_type | 用途 | 示例 |
|---|---|---|
| `theme` | 热点、叙事、政策、市场主题 | `AI`、`token`、`稳定币`、`中特估`、`创新药` |
| `resource` | 大宗商品和资源品映射 | `铜`、`铁矿`、`煤炭`、`黄金`、`锂`、`原油` |
| `value_chain` | 产业链位置 | `上游`、`设备`、`平台`、`分销`、`算力`、`IDC` |
| `business` | 主营业务 | `游戏`、`云服务`、`物业管理`、`航运`、`保险` |
| `risk` | 风险暴露 | `地产债`、`监管风险`、`商品价格敏感`、`汇率敏感` |
| `geo` | 地域暴露 | `中国内地`、`香港本地`、`东南亚`、`全球业务` |
| `style` | 投资风格辅助 | `高股息`、`央国企`、`小市值`、`高波动` |

粒度规则：

- 主行业标签不应过细，避免行业内样本过少。
- 主题和资源标签可以较细，因为它们用于事件关联和热点识别。
- `theme_tags` 派生摘要最多保留 6-10 个高价值 tag，避免过长。
- `stock_tag_registry` 不限制标签数量，但低置信标签必须保留 confidence。

---

## 5. 置信度规则

| confidence | 含义 | 允许用途 |
|---:|---|---|
| `0.90-1.00` | 人工确认、公司简介明确、主营业务明确 | 正式选股、龙头识别 |
| `0.75-0.89` | 多来源一致、新闻频繁共现、业务线明确 | 热点关联、候选增强 |
| `0.50-0.74` | LLM/规则推断，有证据但不稳定 | 候选标签、待审核 |
| `<0.50` | 弱相关或单次新闻噪声 | 不进入正式表 |

默认入正式表阈值：

- `business/resource/value_chain`：`confidence >= 0.80`
- `theme`：`confidence >= 0.75`
- `risk/style/geo`：`confidence >= 0.70`

---

## 6. 数据来源优先级

1. **人工维护/复核**：最高置信，用于核心龙头和高频交易标的。
2. **公司简介/F10/年报主营业务**：主业务标签和资源品标签的基础。
3. **行业分类源**：主行业字段来源，如现有东方财富行业。
4. **新闻/社媒共现**：主题标签和热点标签来源。
5. **规则推断**：ETF/REIT/杠杆反向产品、代码段、名称关键词。
6. **LLM 抽取**：提高召回，但默认进入候选表，除非证据足够。

---

## 7. 生成流程

### 7.1 初始离线构建

输入：

- `docs/hk_industry_registry.csv`
- 现有 `stock_info_registry`
- 公司简介/名称/主营业务字段
- 可选新闻和社媒事件

流程：

1. 清洗 `industry_l1/l2/l3`。
2. 根据主行业生成基础 tag。
3. 根据 ETF/REIT/资源品/业务关键词规则生成高置信 tag。
4. 根据公司简介/业务描述抽取 `business/resource/value_chain`。
5. 根据新闻/社媒事件生成 `theme` 候选 tag。
6. 将高置信 tag 写入 `stock_tag_registry`。
7. 将低置信 tag 写入 `stock_tag_candidate`。
8. 从正式 tag 回填 `stock_info_registry.theme_tags` 派生摘要。

### 7.2 增量更新

按日或按周运行：

1. 新增/变更股票同步基础行业。
2. 新事件进入 `news_tag_events`。
3. 事件 tag 与股票 tag 图谱关联，计算热度。
4. 若某股票和新主题持续高频共现，则生成候选 tag。
5. 候选 tag 经规则或人工确认后进入正式表。

---

## 8. 行业选股与热点识别用法

### 8.1 行业内选股

使用 `industry_l1/l2`：

- 行业内估值分位
- 行业内质量分位
- 行业内 RPS
- 行业内成交额/流动性排名
- 行业内 TopN 候选

### 8.2 热点识别

使用 `stock_tag_registry + news_tag_events`：

1. 计算 tag 热度：`heat_score`、提及量、情绪、扩散速度。
2. 找出命中该 tag 的股票。
3. 按 `confidence`、流动性、行业内排名、成交额、近期 alpha 过滤。
4. 同 tag 内选龙头，而不是只看全市场涨幅。

示例：

```
热点 tag = AI
    -> stock_tag_registry 找到 AI 相关股票
    -> 过滤 confidence >= 0.75
    -> 行业内 RPS/成交额/市值排序
    -> 输出 AI 龙头候选与二线弹性候选
```

### 8.3 多行业公司归因

多行业公司不强行选一个主题，而是：

- `industry_l1/l2` 保留主行业。
- `stock_tag_registry` 保存多个业务和主题标签。
- 选股解释中输出“主行业 + 命中主题 + 证据”。

---

## 9. 文件与命令建议

建议新增文件：

| 文件 | 用途 |
|---|---|
| `docs/hk_tag_dictionary.csv` | 可人工维护的标签字典 |
| `docs/hk_stock_tag_registry.csv` | 股票-标签正式长表 |
| `docs/hk_stock_tag_candidate.csv` | 候选标签长表 |

建议新增命令：

```bash
uv run python run.py build-stock-tags \
  --industry-registry-csv docs/hk_industry_registry.csv \
  --tag-dictionary-csv docs/hk_tag_dictionary.csv \
  --output docs/hk_stock_tag_registry.csv \
  --candidate-output docs/hk_stock_tag_candidate.csv
```

```bash
uv run python run.py import-stock-tags \
  --stock-tag-csv docs/hk_stock_tag_registry.csv
```

后续可增加：

```bash
uv run python run.py enrich-stock-tags-from-news --days 7
uv run python run.py review-stock-tag-candidates
```

---

## 10. 验收标准

第一阶段：

- [ ] `stock_info_registry.theme_tags` 继续可用，使用英文分号分隔。
- [ ] `tag_dictionary` 至少覆盖 `theme/resource/value_chain/business/style` 五类。
- [ ] `stock_tag_registry` 覆盖 90%+ 普通股的至少一个高置信业务/行业标签。
- [ ] ETF/REIT/杠杆反向产品不混入普通股行业排名。
- [ ] 每个正式 tag 都有 `confidence/source/updated_at`。
- [ ] `未分类` 股票进入人工复核清单，而不是硬猜。

第二阶段：

- [ ] 新闻/社媒事件可打 tag 并关联股票。
- [ ] 能按热点 tag 输出股票池、龙头候选、二线弹性候选。
- [ ] 选股报告展示“主行业 + 命中主题 + 证据”。
- [ ] 回测比较：仅主行业 vs 主行业+热点 tag 的收益、回撤、换手。

---

## 11. 实施顺序建议

1. 新增 CSV 长表与 schema：`tag_dictionary`、`stock_tag_registry`、`stock_tag_candidate`。
2. 用当前 `hk_industry_registry.csv` 生成第一版高置信基础 tag。
3. 接入公司名称/简介/主营业务，补 `business/resource/value_chain`。
4. 增加 ClickHouse/Parquet 导入与读取 API。
5. 在选股和报告中读取 tag，输出行业/主题解释。
6. 接入新闻/社媒事件，做 tag 热度与龙头候选排序。

