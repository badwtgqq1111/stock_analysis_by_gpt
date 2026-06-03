# Stock Tag Enrichment With Playwright And DeepSeek Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reproducible evidence-first pipeline that uses browser search plus DeepSeek structured analysis to enrich HK stock tags with higher-precision business, resource, value-chain, theme, risk, geo, and style tags.

**Architecture:** Keep the already implemented tag registry tables as the storage backbone. Add a browser-search evidence collector that caches source pages/snippets, then add an LLM extractor that outputs strict JSON tied to existing tag dictionary entries or candidate tags. High-confidence, multi-source tags go to `stock_tag_registry`; weaker or newly invented tags go to `stock_tag_candidate` for review.

**Tech Stack:** Python 3.12, pandas, Playwright Chromium, existing DeepSeek-compatible `core.llm.client.LLMClient`, Parquet/ClickHouse warehouse, pytest, existing `run.py` command router.

---

## Why This Is The Industry-Grade Shape

Professional tagging systems usually avoid direct `search result -> final tag` workflows. They separate four concerns:

1. **Taxonomy control**: maintain a curated tag dictionary so the model does not freely invent noisy labels.
2. **Evidence capture**: save search results, page snippets, URLs, timestamps, and source types before any LLM judgement.
3. **Structured extraction**: force LLM output into JSON with tags, confidence, evidence references, and rejection reasons.
4. **Review and versioning**: keep model/rule/search versions so stale or wrong tags can be rebuilt.

This plan follows that shape. It intentionally makes search and LLM extraction separately runnable so the expensive browser step can be cached and the prompt/rules can be improved without scraping again.

---

## Current State To Preserve

Already implemented:

- `docs/hk_industry_registry.csv`
- `docs/hk_tag_dictionary.csv`
- `docs/hk_stock_tag_registry.csv`
- `docs/hk_stock_tag_candidate.csv`
- `docs/hk_company_research_evidence.csv`
- `data/model/tag_schemas.py`
- `data/ingest/stock_tags.py`
- `data/ingest/providers/hk_company_research.py`
- warehouse support for:
  - `tag_dictionary`
  - `stock_tag_registry`
  - `stock_tag_candidate`
  - `company_research_evidence`
- CLI commands:
  - `research-stock-tags`
  - `build-stock-tags`
  - `import-stock-tags`
  - `tag-coverage`

Do not replace these tables. Extend the evidence and extraction stages around them.

---

## File Structure

- Create: `data/ingest/tag_taxonomy.py`
  - Builds a richer default tag dictionary.
  - Defines taxonomy groups, aliases, negative keywords, and parent-child relationships.
- Modify: `data/ingest/stock_tags.py`
  - Use the richer taxonomy instead of the current small `DEFAULT_TAGS`.
  - Merge LLM extracted tags while preserving formal/candidate split.
- Create: `data/ingest/providers/browser_company_search.py`
  - Uses Playwright Chromium to search one HK stock.
  - Returns normalized evidence rows with source type, rank, query, URL, title, snippet, and raw text.
- Create: `data/ingest/llm_tag_extractor.py`
  - Builds DeepSeek prompts.
  - Parses strict JSON.
  - Converts output into formal/candidate tag frames.
- Modify: `data/model/tag_schemas.py`
  - Extend `COMPANY_RESEARCH_EVIDENCE_FIELDS` with optional browser-search fields if needed.
  - Keep backward compatibility for existing CSV.
- Modify: `data/store/clickhouse_store.py`
  - Add new evidence columns to ClickHouse schema if schema is extended.
- Modify: `data/store/warehouse.py`
  - Keep evidence upsert/read compatible with the extended evidence schema.
- Modify: `data/ingest/service.py`
  - Add browser evidence collection service method.
  - Add DeepSeek extraction service method.
  - Add review/coverage helpers for LLM-extracted candidate tags.
- Modify: `run.py`
  - Add commands:
    - `browser-research-stock-tags`
    - `extract-stock-tags-llm`
    - `review-stock-tag-candidates`
- Modify: `pyproject.toml`
  - Add `playwright` dependency.
- Modify: `README.md`
  - Document search/extraction/import workflow and manual commands.
- Create/Modify: `test/test_stock_tag_registry.py`
  - Add tests for taxonomy expansion, browser evidence normalization, LLM JSON parsing, and candidate split.
- Create output/cache files:
  - `docs/hk_company_browser_evidence.csv`
  - `docs/hk_llm_tag_extraction.csv`
  - `docs/hk_stock_tag_candidate_llm.csv`

---

## Data Model Decisions

### Evidence Rows

Keep `company_research_evidence` as the main evidence table. Add these optional fields if implementation chooses to extend the schema:

| Field | Meaning |
|---|---|
| `query` | Search query used, e.g. `00700 Tencent annual report business AI cloud` |
| `result_rank` | Search result rank |
| `source_type` | `search_result` / `company_site` / `annual_report` / `hkex_announcement` / `news` |
| `retrieval_method` | `playwright_chrome` / `akshare` / `manual` |
| `content_hash` | Hash for dedupe |

If schema extension causes too much blast radius, encode these values into `source/title/summary/raw_text` first and defer physical columns. The first implementation should favor low schema risk.

### LLM Extraction Rows

LLM extraction should be a transient CSV first, not a mandatory warehouse table:

| Field | Meaning |
|---|---|
| `stock_code` | HK code |
| `tag` | Suggested tag |
| `tag_type` | Standard tag type |
| `confidence` | 0-1 |
| `is_primary` | Main business/theme |
| `source` | `deepseek_browser_evidence` |
| `evidence` | Short cited evidence |
| `evidence_url` | Best URL |
| `decision` | `formal` / `candidate` / `reject` |
| `reason` | Why this tag was chosen or rejected |
| `model` | DeepSeek model |
| `prompt_version` | Prompt version |

The final converter writes `formal` decisions to `stock_tag_registry` and `candidate` decisions to `stock_tag_candidate`.

---

## Tag Taxonomy Expansion

The current dictionary has 14 rows. Expand it to roughly 150-300 rows in the first pass, grouped by practical investing use:

### Theme Tags

Examples:

- `AI`
- `大模型`
- `AI应用`
- `AI医疗`
- `机器人`
- `自动驾驶`
- `token`
- `稳定币`
- `RWA`
- `Web3`
- `创新药`
- `CXO`
- `医疗器械`
- `半导体`
- `光模块`
- `低空经济`
- `新能源车`
- `储能`
- `中特估`

### Resource Tags

Examples:

- `铜`
- `铁矿`
- `黄金`
- `煤炭`
- `原油`
- `天然气`
- `锂`
- `铝`
- `镍`
- `稀土`
- `铀`
- `水泥`

### Value Chain Tags

Examples:

- `上游资源`
- `设备`
- `分销`
- `平台`
- `SaaS`
- `IDC`
- `算力`
- `液冷`
- `云服务`
- `支付网络`
- `物流网络`
- `供应链金融`

### Business Tags

Examples:

- `游戏`
- `广告`
- `电商`
- `外卖`
- `本地生活`
- `物业管理`
- `商业地产`
- `住宅开发`
- `保险`
- `银行`
- `券商`
- `交易所`
- `航运`
- `港口`
- `航空`
- `公路`
- `教育`
- `博彩`
- `餐饮`
- `运动服饰`

### Risk Tags

Examples:

- `地产债`
- `监管风险`
- `商品价格敏感`
- `汇率敏感`
- `利率敏感`
- `单一客户风险`
- `海外制裁风险`

### Geo Tags

Examples:

- `中国内地`
- `香港本地`
- `澳门`
- `东南亚`
- `欧洲`
- `北美`
- `全球业务`

### Style Tags

Examples:

- `高股息`
- `央国企`
- `小市值`
- `高波动`
- `低估值`
- `成长股`
- `周期股`

---

## Prompt Contract

DeepSeek must return only JSON. The model should not decide final acceptance alone; it assigns confidence and cites evidence.

Expected response shape:

```json
{
  "stock_code": "00700",
  "company_name": "腾讯控股",
  "tags": [
    {
      "tag": "游戏",
      "tag_type": "business",
      "confidence": 0.94,
      "is_primary": true,
      "evidence": "公司资料显示主营业务包含网络游戏",
      "evidence_url": "https://example.com",
      "decision": "formal",
      "reason": "主营业务明确"
    },
    {
      "tag": "AI",
      "tag_type": "theme",
      "confidence": 0.68,
      "is_primary": false,
      "evidence": "新闻提及 AI 产品，但不是主营收入来源",
      "evidence_url": "https://example.com",
      "decision": "candidate",
      "reason": "主题相关但证据弱"
    }
  ],
  "rejected": [
    {
      "tag": "铜",
      "reason": "仅客户行业提及，不代表公司资源暴露"
    }
  ]
}
```

Acceptance rules after parsing:

- `business/resource/value_chain >= 0.80` -> formal
- `theme >= 0.75` and at least one reliable source -> formal
- `risk/geo/style >= 0.70` -> formal
- unknown tag not in dictionary -> candidate, unless auto-add mode is explicitly enabled
- single news mention with no company evidence -> candidate
- negative evidence or customer-only exposure -> reject

---

## Search Strategy

For each stock, run 3-5 targeted queries instead of broad web crawling:

1. `{code} {name} 主营业务 年报`
2. `{code} {name} company profile business segments`
3. `{code} {name} 港交所 公告 年报`
4. `{code} {name} AI 云服务 游戏 铜矿 铁矿 稳定币`
5. `{code} {name} 业务 风险 地区 收入`

Limits:

- default `--max-results-per-query 5`
- default `--max-pages-per-stock 8`
- default `--per-page-timeout 12`
- default `--stock-limit` supported for smoke runs
- cache by `content_hash`

Source weighting:

| Source | Weight |
|---|---:|
| annual report / HKEX announcement | 1.00 |
| company official site | 0.95 |
| exchange/company profile provider | 0.90 |
| established financial media | 0.75 |
| generic search snippets | 0.45 |
| social media/forum | 0.35 |

---

## Task 1: Expand Tag Taxonomy

**Files:**
- Create: `data/ingest/tag_taxonomy.py`
- Modify: `data/ingest/stock_tags.py`
- Test: `test/test_stock_tag_registry.py`

- [ ] **Step 1: Add failing taxonomy test**

Add:

```python
from data.ingest.tag_taxonomy import build_expanded_tag_dictionary


def test_expanded_tag_dictionary_contains_investable_specific_tags():
    dictionary = build_expanded_tag_dictionary()
    tags = set(dictionary["tag"])
    assert {"AI", "大模型", "稳定币", "铜", "锂", "光模块", "创新药", "博彩", "航运"}.issubset(tags)
    assert len(dictionary) >= 120
    assert dictionary["aliases"].astype(str).str.contains(",").sum() == 0
```

- [ ] **Step 2: Run failing test**

```bash
uv run pytest test/test_stock_tag_registry.py::test_expanded_tag_dictionary_contains_investable_specific_tags -q
```

Expected: import error for `data.ingest.tag_taxonomy`.

- [ ] **Step 3: Implement taxonomy module**

Create `data/ingest/tag_taxonomy.py` with:

```python
#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Expanded investable tag taxonomy for HK stocks."""

import pandas as pd

from data.model import TAG_DICTIONARY_FIELDS, normalize_tag_dictionary_entry


TAG_GROUPS = {
    "theme": [
        ("AI", "科技", "人工智能;AIGC;AI"),
        ("大模型", "AI", "LLM;基础模型;生成式AI"),
        ("AI应用", "AI", "AI软件;智能应用"),
        ("AI医疗", "AI", "医疗AI;智能诊断"),
        ("机器人", "先进制造", "人形机器人;工业机器人"),
        ("自动驾驶", "汽车科技", "智能驾驶;ADAS"),
        ("token", "数字资产", "代币;Web3"),
        ("稳定币", "token", "stablecoin;支付型代币"),
        ("RWA", "token", "现实资产代币化;资产上链"),
        ("Web3", "数字资产", "区块链;去中心化"),
        ("创新药", "医药", "生物药;新药研发"),
        ("CXO", "医药", "CRO;CDMO;CMO"),
        ("医疗器械", "医药", "高值耗材;诊断设备"),
        ("半导体", "科技", "芯片;集成电路"),
        ("光模块", "算力", "光通信模块;高速光模块"),
        ("低空经济", "先进制造", "eVTOL;无人机"),
        ("新能源车", "汽车", "电动车;智能电动车"),
        ("储能", "新能源", "电化学储能;储能系统"),
        ("中特估", "风格", "央国企重估;中国特色估值"),
        ("国企改革", "风格", "央企改革;国资改革"),
        ("一带一路", "政策", "海外基建;出海工程"),
        ("消费复苏", "消费", "可选消费复苏;线下消费"),
        ("银发经济", "消费", "养老服务;老龄化"),
        ("体育产业", "消费", "运动消费;赛事经济"),
        ("教育科技", "教育", "在线教育;职业教育"),
        ("云计算", "科技", "云平台;云基础设施"),
        ("SaaS", "软件", "企业软件;订阅软件"),
        ("网络安全", "软件", "信息安全;数据安全"),
        ("数据要素", "数字经济", "数据资产;数据交易"),
        ("智能制造", "先进制造", "工业互联网;数字工厂"),
        ("绿色电力", "新能源", "绿电;可再生能源"),
        ("碳中和", "新能源", "减碳;双碳"),
        ("核电", "能源", "核能;核电运营"),
        ("氢能", "新能源", "燃料电池;绿氢"),
        ("出海", "全球化", "海外扩张;国际化"),
    ],
    "resource": [
        ("铜", "资源材料", "铜矿;铜资源;铜金属"),
        ("铁矿", "资源材料", "铁矿石;铁矿资源"),
        ("黄金", "资源材料", "金矿;贵金属"),
        ("煤炭", "能源", "动力煤;焦煤"),
        ("原油", "能源", "石油;油气"),
        ("天然气", "能源", "燃气;LNG"),
        ("锂", "新能源材料", "锂矿;碳酸锂;氢氧化锂"),
        ("铝", "资源材料", "电解铝;氧化铝"),
        ("镍", "资源材料", "镍矿;电池镍"),
        ("钴", "资源材料", "钴矿;电池钴"),
        ("稀土", "资源材料", "稀土矿;磁材"),
        ("铀", "能源", "铀矿;核燃料"),
        ("水泥", "建材", "熟料;建筑材料"),
        ("钢铁", "资源材料", "钢材;粗钢"),
        ("玻璃", "建材", "浮法玻璃;光伏玻璃"),
        ("化肥", "农业", "磷肥;钾肥;氮肥"),
        ("农产品", "农业", "粮食;大豆;玉米"),
        ("纸浆", "造纸", "木浆;纸品"),
    ],
    "value_chain": [
        ("上游资源", "产业链", "资源端;原料端"),
        ("设备", "产业链", "设备商;制造设备"),
        ("分销", "产业链", "渠道;经销"),
        ("平台", "产业链", "互联网平台;交易平台"),
        ("IDC", "算力", "数据中心;机房"),
        ("算力", "AI", "数据中心;GPU服务器;AI服务器"),
        ("液冷", "算力", "液冷服务器;冷却系统"),
        ("云服务", "科技", "云计算;云平台"),
        ("支付网络", "金融科技", "支付;收单"),
        ("物流网络", "物流", "仓配;快递网络"),
        ("供应链金融", "金融科技", "保理;贸易融资"),
        ("芯片设计", "半导体", "IC设计;Fabless"),
        ("晶圆制造", "半导体", "晶圆代工;Foundry"),
        ("半导体设备", "半导体", "刻蚀;光刻;检测设备"),
        ("半导体材料", "半导体", "硅片;光刻胶;电子气体"),
        ("封测", "半导体", "封装;测试"),
        ("电池材料", "新能源车", "正极;负极;隔膜;电解液"),
        ("动力电池", "新能源车", "电池包;电芯"),
        ("充电桩", "新能源车", "充电设备;补能"),
        ("整车", "汽车", "乘用车;商用车"),
        ("零部件", "汽车", "汽车零部件;供应商"),
        ("创新药研发", "创新药", "临床;管线"),
        ("药品分销", "医药", "医药流通;药品批发"),
        ("医院服务", "医药", "医疗服务;专科医院"),
        ("港口物流", "交通运输", "码头;集装箱港口"),
        ("航运运力", "交通运输", "船队;运价"),
        ("航空运力", "交通运输", "客运;货运"),
        ("广告投放", "互联网服务", "效果广告;品牌广告"),
        ("内容分发", "媒体", "流媒体;短视频;长视频"),
        ("物业运营", "地产", "商业运营;物管"),
    ],
    "business": [
        ("游戏", "互联网服务", "网络游戏;手游"),
        ("广告", "互联网服务", "广告营销;数字广告"),
        ("电商", "互联网服务", "电子商务;线上零售"),
        ("外卖", "本地生活", "即时配送;餐饮配送"),
        ("本地生活", "互联网服务", "到店;酒旅;外卖"),
        ("物业管理", "地产", "物管;物业服务"),
        ("商业地产", "地产", "购物中心;写字楼"),
        ("住宅开发", "地产", "房地产开发;住宅销售"),
        ("保险", "金融", "寿险;财险;再保险"),
        ("银行", "金融", "商业银行;零售银行"),
        ("券商", "金融", "证券;投行;经纪"),
        ("交易所", "金融", "证券交易所;清算"),
        ("航运", "交通运输", "集运;干散货;油运"),
        ("港口", "交通运输", "码头;港口运营"),
        ("航空", "交通运输", "航空公司;机场"),
        ("公路", "交通运输", "收费公路;高速公路"),
        ("铁路", "交通运输", "轨交;铁路运营"),
        ("教育", "消费服务", "学校;培训;职业教育"),
        ("博彩", "旅游消费", "赌场;博彩运营;澳门博彩"),
        ("餐饮", "消费", "餐厅;连锁餐饮"),
        ("运动服饰", "消费", "运动鞋服;体育用品"),
        ("啤酒", "消费", "啤酒生产;酒类"),
        ("乳制品", "消费", "牛奶;奶粉"),
        ("调味品", "消费", "酱油;复合调味料"),
        ("珠宝", "消费", "黄金珠宝;首饰"),
        ("美妆", "消费", "化妆品;护肤"),
        ("旅游", "消费服务", "景区;旅行服务"),
        ("酒店", "消费服务", "酒店运营;住宿"),
        ("电影", "媒体", "院线;影视制作"),
        ("音乐", "媒体", "在线音乐;版权"),
        ("视频平台", "媒体", "长视频;短视频"),
        ("通信运营", "电信", "移动通信;宽带"),
        ("通信设备", "电信", "基站;网络设备"),
        ("软件服务", "科技", "软件开发;IT服务"),
        ("企业服务", "科技", "B端服务;企业软件"),
        ("数据中心", "科技", "IDC;机房运营"),
        ("消费电子", "科技", "手机;智能硬件"),
        ("家电", "消费", "白电;小家电"),
        ("汽车经销", "汽车", "4S店;汽车销售"),
        ("汽车制造", "汽车", "整车制造;汽车品牌"),
        ("煤电", "公用事业", "火电;燃煤发电"),
        ("水电", "公用事业", "水力发电"),
        ("风电", "新能源", "风力发电;风电场"),
        ("光伏", "新能源", "太阳能;光伏电站"),
        ("燃气", "公用事业", "城市燃气;天然气分销"),
        ("供水", "公用事业", "水务;自来水"),
        ("环保", "公用事业", "污水处理;固废"),
        ("工程建设", "建筑", "基建;工程承包"),
        ("建筑材料", "建材", "水泥;玻璃;管材"),
        ("机械设备", "工业", "工程机械;通用设备"),
        ("电子制造", "工业", "EMS;代工"),
        ("纺织服装", "消费", "服装;纺织品"),
        ("医药制造", "医药", "药品生产;制药"),
        ("医疗服务", "医药", "医院;诊所"),
        ("农业", "农业", "种植;养殖"),
    ],
    "risk": [
        ("地产债", "地产", "房企债务;违约风险"),
        ("监管风险", "风险", "政策监管;合规"),
        ("商品价格敏感", "风险", "大宗商品波动;原料价格"),
        ("汇率敏感", "风险", "外汇;美元"),
        ("利率敏感", "风险", "利率上行;融资成本"),
        ("单一客户风险", "风险", "客户集中;大客户"),
        ("海外制裁风险", "风险", "出口管制;制裁"),
        ("流动性风险", "风险", "成交稀疏;停牌"),
        ("高负债", "风险", "杠杆;资产负债率"),
        ("商誉风险", "风险", "商誉减值;并购"),
        ("原材料成本风险", "风险", "成本上涨;毛利率压力"),
        ("牌照风险", "风险", "牌照续期;特许经营"),
    ],
    "geo": [
        ("中国内地", "地域", "大陆;内地业务"),
        ("香港本地", "地域", "香港业务;本地市场"),
        ("澳门", "地域", "澳门业务;澳门博彩"),
        ("东南亚", "地域", "ASEAN;新加坡;印尼;泰国"),
        ("欧洲", "地域", "欧盟;英国;德国"),
        ("北美", "地域", "美国;加拿大"),
        ("全球业务", "地域", "全球化;国际业务"),
        ("非洲", "地域", "非洲业务;矿业非洲"),
    ],
    "style": [
        ("高股息", "风格", "收息资产;红利"),
        ("央国企", "风格", "央企;国企"),
        ("小市值", "风格", "小盘;微盘"),
        ("高波动", "风格", "波动率高;弹性"),
        ("低估值", "风格", "低PE;低PB"),
        ("成长股", "风格", "高增长;成长"),
        ("周期股", "风格", "周期;价格弹性"),
        ("防御", "风格", "防守;稳定现金流"),
        ("高杠杆", "风格", "负债高;财务杠杆"),
        ("高研发", "风格", "研发投入;技术驱动"),
        ("蓝筹", "风格", "大市值;恒指成分"),
        ("红筹", "风格", "红筹股;中资企业"),
    ],
    "instrument": [
        ("ETF", "基金", "交易所买卖基金;交易所买卖产品"),
        ("REIT", "地产", "房地产基金;地产投资信托基金"),
        ("杠杆反向", "基金", "杠杆产品;反向产品"),
        ("债券基金", "基金", "债券ETF;固收基金"),
        ("货币基金", "基金", "现金管理;货币市场基金"),
        ("商品ETF", "基金", "黄金ETF;原油ETF"),
        ("期货ETF", "基金", "期货型ETF"),
        ("封闭基金", "基金", "封闭式基金"),
    ],
}


def _iter_tag_rows():
    for tag_type, items in TAG_GROUPS.items():
        for tag, parent, aliases in items:
            yield {
                "tag": tag,
                "tag_type": tag_type,
                "aliases": aliases,
                "parent_tag": parent,
                "description": f"{tag} related exposure",
            }


EXPANDED_TAGS = list(_iter_tag_rows())


def build_expanded_tag_dictionary():
    rows = [normalize_tag_dictionary_entry(row) for row in EXPANDED_TAGS]
    return pd.DataFrame(rows, columns=TAG_DICTIONARY_FIELDS)
```

- [ ] **Step 4: Wire stock tag builder to expanded taxonomy**

In `data/ingest/stock_tags.py`, replace `build_default_tag_dictionary()` implementation:

```python
def build_default_tag_dictionary():
    from data.ingest.tag_taxonomy import build_expanded_tag_dictionary

    return build_expanded_tag_dictionary()
```

- [ ] **Step 5: Run taxonomy tests**

```bash
uv run pytest test/test_stock_tag_registry.py::test_expanded_tag_dictionary_contains_investable_specific_tags -q
```

Expected: PASS.

---

## Task 2: Browser Search Evidence Collector

**Files:**
- Modify: `pyproject.toml`
- Create: `data/ingest/providers/browser_company_search.py`
- Test: `test/test_stock_tag_registry.py`

- [ ] **Step 1: Add Playwright dependency**

In `pyproject.toml`, add:

```toml
"playwright>=1.45.0",
```

Run:

```bash
uv sync --dev
uv run playwright install chromium
```

Expected: dependency installed. Browser install may require network; if blocked, document and continue with unit tests using fakes.

- [ ] **Step 2: Add evidence normalization test**

Add:

```python
from data.ingest.providers.browser_company_search import normalize_browser_search_result


def test_normalize_browser_search_result_keeps_query_rank_and_hash():
    row = normalize_browser_search_result(
        stock_code="700",
        query="00700 腾讯 主营业务",
        result_rank=1,
        title="腾讯控股 年报",
        url="https://example.com/annual-report",
        snippet="主营业务包括网络游戏、金融科技、云服务。",
        raw_text="主营业务包括网络游戏、金融科技、云服务。",
    )
    assert row["stock_code"] == "00700"
    assert row["source"] == "playwright_search"
    assert "query=00700 腾讯 主营业务" in row["title"]
    assert "rank=1" in row["summary"]
    assert "网络游戏" in row["raw_text"]
```

- [ ] **Step 3: Implement browser provider**

Create `data/ingest/providers/browser_company_search.py`:

```python
#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Playwright browser search provider for company tag evidence."""

from __future__ import annotations

import hashlib
from datetime import datetime

from data.ingest.providers.hk_common import normalize_hk_stock_code


SEARCH_SOURCE = "playwright_search"


def _clean(value):
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def _content_hash(*parts):
    text = "\n".join(_clean(part) for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def normalize_browser_search_result(
    stock_code,
    query,
    result_rank,
    title,
    url,
    snippet,
    raw_text,
    fetched_at=None,
):
    code = normalize_hk_stock_code(stock_code)
    query = _clean(query)
    title = _clean(title)
    url = _clean(url)
    snippet = _clean(snippet)
    raw_text = _clean(raw_text)
    digest = _content_hash(code, query, url, title, snippet, raw_text)
    return {
        "stock_code": code,
        "market": "HK",
        "source": SEARCH_SOURCE,
        "title": f"query={query}; rank={int(result_rank)}; title={title}",
        "summary": f"rank={int(result_rank)}; url={url}; hash={digest}; snippet={snippet[:360]}",
        "url": url,
        "raw_text": raw_text or snippet,
        "fetched_at": fetched_at or datetime.utcnow().isoformat(),
    }
```

- [ ] **Step 4: Add async/sync search class**

In the same file add `BrowserCompanySearchFetcher`:

```python
class BrowserCompanySearchFetcher:
    def __init__(
        self,
        stock_code,
        company_name="",
        *,
        max_results_per_query=5,
        max_pages_per_stock=8,
        per_page_timeout=12,
    ):
        self.stock_code = normalize_hk_stock_code(stock_code)
        self.company_name = _clean(company_name)
        self.max_results_per_query = int(max_results_per_query)
        self.max_pages_per_stock = int(max_pages_per_stock)
        self.per_page_timeout = int(per_page_timeout)

    def build_queries(self):
        name = self.company_name or self.stock_code
        return [
            f"{self.stock_code} {name} 主营业务 年报",
            f"{self.stock_code} {name} company profile business segments",
            f"{self.stock_code} {name} 港交所 公告 年报",
            f"{self.stock_code} {name} AI 云服务 游戏 铜矿 铁矿 稳定币",
            f"{self.stock_code} {name} 业务 风险 地区 收入",
        ]

    def fetch(self):
        from playwright.sync_api import sync_playwright

        rows = []
        pages_seen = 0
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_default_timeout(self.per_page_timeout * 1000)
            try:
                for query in self.build_queries():
                    if pages_seen >= self.max_pages_per_stock:
                        break
                    page.goto(f"https://www.google.com/search?q={query}", wait_until="domcontentloaded")
                    results = page.locator("a").all()[: self.max_results_per_query * 3]
                    rank = 0
                    for link in results:
                        href = link.get_attribute("href") or ""
                        text = link.inner_text(timeout=1000) or ""
                        if not href.startswith("http"):
                            continue
                        rank += 1
                        rows.append(
                            normalize_browser_search_result(
                                stock_code=self.stock_code,
                                query=query,
                                result_rank=rank,
                                title=text[:160],
                                url=href,
                                snippet=text[:500],
                                raw_text=text[:2000],
                            )
                        )
                        pages_seen += 1
                        if rank >= self.max_results_per_query or pages_seen >= self.max_pages_per_stock:
                            break
            finally:
                browser.close()
        return rows
```

Note: Google may block automated scraping. If blocked, keep the provider interface but add fallback search URLs in Task 3.

- [ ] **Step 5: Run provider tests**

```bash
uv run pytest test/test_stock_tag_registry.py::test_normalize_browser_search_result_keeps_query_rank_and_hash -q
```

Expected: PASS without requiring browser/network.

---

## Task 3: Browser Research Service And CLI

**Files:**
- Modify: `data/ingest/service.py`
- Modify: `run.py`
- Test: `test/test_stock_tag_registry.py`

- [ ] **Step 1: Add service test with fake fetcher**

Add:

```python
def test_service_browser_research_stock_tags_writes_evidence(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        industry_csv = base / "hk_industry_registry.csv"
        evidence_csv = base / "hk_company_browser_evidence.csv"
        pd.DataFrame([{"stock_code": "00700", "market": "HK", "name": "腾讯控股"}]).to_csv(
            industry_csv, index=False, encoding="utf-8-sig"
        )

        class FakeBrowserFetcher:
            def __init__(self, stock_code, company_name="", **kwargs):
                self.stock_code = stock_code
                self.company_name = company_name

            def fetch(self):
                return [{
                    "stock_code": "00700",
                    "market": "HK",
                    "source": "playwright_search",
                    "title": "query=00700 腾讯控股 主营业务; rank=1; title=公司资料",
                    "summary": "rank=1; url=https://example.com; snippet=网络游戏 云服务",
                    "url": "https://example.com",
                    "raw_text": "网络游戏 云服务",
                    "fetched_at": "2026-06-03T00:00:00",
                }]

        monkeypatch.setattr("data.ingest.service.BrowserCompanySearchFetcher", FakeBrowserFetcher, raising=False)
        service = MarketDataService(base_dir=str(base / "data"))
        try:
            summary = service.browser_research_stock_tags(
                industry_registry_csv=industry_csv,
                evidence_csv=evidence_csv,
                limit=1,
            )
        finally:
            service.close()

        assert summary["evidence_rows"] == 1
        assert evidence_csv.exists()
```

- [ ] **Step 2: Implement service method**

Add `MarketDataService.browser_research_stock_tags(...)`:

```python
def browser_research_stock_tags(
    self,
    industry_registry_csv="docs/hk_industry_registry.csv",
    evidence_csv="docs/hk_company_browser_evidence.csv",
    stock_codes=None,
    limit=None,
    skip_existing=True,
    max_results_per_query=5,
    max_pages_per_stock=8,
    per_page_timeout=12,
    show_progress=False,
):
    from data.model import COMPANY_RESEARCH_EVIDENCE_FIELDS

    industry = pd.read_csv(industry_registry_csv, dtype=str).fillna("")
    code_name_rows = []
    for _, row in industry.iterrows():
        code = normalize_stock_code(row.get("stock_code"), market="HK")
        name = str(row.get("name") or row.get("stock_name") or "").strip()
        code_name_rows.append((code, name))
    if stock_codes:
        allowed = {normalize_stock_code(code, market="HK") for code in stock_codes}
        code_name_rows = [(code, name) for code, name in code_name_rows if code in allowed]
    code_name_rows = list(dict.fromkeys(code_name_rows))
    if limit:
        code_name_rows = code_name_rows[: int(limit)]

    path = Path(evidence_csv)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = pd.read_csv(path, dtype=str).fillna("") if path.exists() else pd.DataFrame(columns=COMPANY_RESEARCH_EVIDENCE_FIELDS)
    existing_codes = set(existing.loc[existing["source"].astype(str) == "playwright_search", "stock_code"].astype(str)) if skip_existing and not existing.empty else set()

    fetcher_cls = globals().get("BrowserCompanySearchFetcher")
    if fetcher_cls is None:
        from data.ingest.providers.browser_company_search import BrowserCompanySearchFetcher as fetcher_cls

    rows = []
    errors = []
    iterator = code_name_rows
    if show_progress:
        iterator = tqdm(code_name_rows, desc="browser research", unit="stock")
    for code, name in iterator:
        if code in existing_codes:
            continue
        try:
            rows.extend(fetcher_cls(
                code,
                company_name=name,
                max_results_per_query=max_results_per_query,
                max_pages_per_stock=max_pages_per_stock,
                per_page_timeout=per_page_timeout,
            ).fetch())
        except Exception as exc:
            errors.append({"stock_code": code, "error": str(exc)})

    new_frame = pd.DataFrame(rows, columns=COMPANY_RESEARCH_EVIDENCE_FIELDS)
    combined = pd.concat([existing, new_frame], ignore_index=True) if not existing.empty else new_frame
    combined = combined.drop_duplicates(subset=["market", "stock_code", "source", "title"], keep="last")
    combined.to_csv(path, index=False, encoding="utf-8-sig")
    self.warehouse.upsert_company_research_evidence(combined)
    return {
        "status": "completed",
        "requested": len(code_name_rows),
        "fetched": len(rows),
        "evidence_rows": len(combined),
        "errors": len(errors),
        "error_samples": errors[:10],
        "evidence_csv": str(path),
    }
```

- [ ] **Step 3: Add CLI command**

In `run.py`, add `browser-research-stock-tags` with args:

```bash
--industry-registry-csv
--evidence-csv
--stock-codes
--limit
--max-results-per-query
--max-pages-per-stock
--per-page-timeout
--no-skip-existing
--show-progress
```

The handler calls `service.browser_research_stock_tags(...)`.

- [ ] **Step 4: Run service test**

```bash
uv run pytest test/test_stock_tag_registry.py::test_service_browser_research_stock_tags_writes_evidence -q
```

Expected: PASS.

---

## Task 4: DeepSeek JSON Tag Extractor

**Files:**
- Create: `data/ingest/llm_tag_extractor.py`
- Test: `test/test_stock_tag_registry.py`

- [ ] **Step 1: Add JSON parser tests**

Add:

```python
from data.ingest.llm_tag_extractor import parse_llm_tag_response, llm_extractions_to_tag_frames


def test_parse_llm_tag_response_strips_markdown_and_validates_tags():
    text = """```json
    {"stock_code":"00700","company_name":"腾讯控股","tags":[{"tag":"游戏","tag_type":"business","confidence":0.94,"is_primary":true,"evidence":"主营业务包含网络游戏","evidence_url":"https://example.com","decision":"formal","reason":"主营明确"}],"rejected":[]}
    ```"""
    parsed = parse_llm_tag_response(text)
    assert parsed["stock_code"] == "00700"
    assert parsed["tags"][0]["tag"] == "游戏"


def test_llm_extractions_to_tag_frames_splits_formal_and_candidate():
    extraction = {
        "stock_code": "00700",
        "tags": [
            {"tag": "游戏", "tag_type": "business", "confidence": 0.94, "is_primary": True, "evidence": "主营业务", "evidence_url": "", "decision": "formal"},
            {"tag": "AI", "tag_type": "theme", "confidence": 0.68, "is_primary": False, "evidence": "新闻提及", "evidence_url": "", "decision": "candidate"},
        ],
    }
    formal, candidates = llm_extractions_to_tag_frames([extraction], source="deepseek_browser_evidence")
    assert set(formal["tag"]) == {"游戏"}
    assert set(candidates["tag"]) == {"AI"}
```

- [ ] **Step 2: Implement extractor module**

Create `data/ingest/llm_tag_extractor.py`:

```python
#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""DeepSeek structured tag extraction from company evidence."""

from __future__ import annotations

import json
import re

import pandas as pd

from data.model import (
    STOCK_TAG_CANDIDATE_FIELDS,
    STOCK_TAG_FIELDS,
    normalize_stock_code,
    normalize_stock_tag_entry,
)


PROMPT_VERSION = "stock_tag_extract_v1"


def strip_json_markdown(text):
    text = str(text or "").strip()
    match = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    if match:
        return match.group(1).strip()
    return text


def parse_llm_tag_response(text):
    payload = json.loads(strip_json_markdown(text))
    if "stock_code" not in payload:
        raise ValueError("LLM response missing stock_code")
    if "tags" not in payload or not isinstance(payload["tags"], list):
        raise ValueError("LLM response missing tags list")
    payload["stock_code"] = normalize_stock_code(payload["stock_code"], market="HK")
    return payload


def llm_extractions_to_tag_frames(extractions, source="deepseek_browser_evidence"):
    formal_rows = []
    candidate_rows = []
    for extraction in extractions:
        stock_code = normalize_stock_code(extraction.get("stock_code"), market="HK")
        for tag in extraction.get("tags", []):
            confidence = float(tag.get("confidence") or 0)
            decision = str(tag.get("decision") or "").lower()
            payload = {
                "stock_code": stock_code,
                "market": "HK",
                "tag": tag.get("tag"),
                "tag_type": tag.get("tag_type"),
                "confidence": confidence,
                "is_primary": tag.get("is_primary"),
                "source": source,
                "evidence": tag.get("evidence") or tag.get("reason") or "",
                "evidence_url": tag.get("evidence_url") or "",
            }
            if decision == "formal":
                formal_rows.append(normalize_stock_tag_entry(payload))
            elif decision == "candidate":
                payload["review_status"] = "pending"
                payload["review_note"] = tag.get("reason") or "llm candidate"
                candidate_rows.append(normalize_stock_tag_entry(payload, candidate=True))
    return (
        pd.DataFrame(formal_rows, columns=STOCK_TAG_FIELDS),
        pd.DataFrame(candidate_rows, columns=STOCK_TAG_CANDIDATE_FIELDS),
    )
```

- [ ] **Step 3: Add prompt builder**

In the same module:

```python
def build_tag_extraction_prompt(stock_code, evidence_rows, tag_dictionary_frame):
    dictionary = tag_dictionary_frame[["tag", "tag_type", "aliases", "parent_tag", "description"]].fillna("").to_dict("records")
    evidence = evidence_rows[["source", "title", "summary", "url", "raw_text"]].fillna("").head(20).to_dict("records")
    return [
        {
            "role": "system",
            "content": (
                "你是港股公司研究标签抽取器。只输出 JSON，不要输出解释。"
                "标签必须基于证据，不能把客户行业误判成公司自身暴露。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "stock_code": stock_code,
                    "allowed_tags": dictionary,
                    "evidence": evidence,
                    "output_schema": {
                        "stock_code": "string",
                        "company_name": "string",
                        "tags": [
                            {
                                "tag": "string",
                                "tag_type": "theme|resource|value_chain|business|risk|geo|style|instrument|industry",
                                "confidence": "number 0-1",
                                "is_primary": "boolean",
                                "evidence": "string",
                                "evidence_url": "string",
                                "decision": "formal|candidate|reject",
                                "reason": "string",
                            }
                        ],
                        "rejected": [{"tag": "string", "reason": "string"}],
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]
```

- [ ] **Step 4: Run extractor tests**

```bash
uv run pytest test/test_stock_tag_registry.py::test_parse_llm_tag_response_strips_markdown_and_validates_tags test/test_stock_tag_registry.py::test_llm_extractions_to_tag_frames_splits_formal_and_candidate -q
```

Expected: PASS.

---

## Task 5: LLM Extraction Service And CLI

**Files:**
- Modify: `data/ingest/service.py`
- Modify: `run.py`
- Test: `test/test_stock_tag_registry.py`

- [ ] **Step 1: Add service test with fake LLM**

Add:

```python
from data.ingest.stock_tags import build_default_tag_dictionary


def test_service_extract_stock_tags_llm_uses_cached_evidence(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        evidence_csv = base / "evidence.csv"
        dictionary_csv = base / "dictionary.csv"
        output_csv = base / "llm_tags.csv"
        candidate_csv = base / "llm_candidates.csv"
        pd.DataFrame([{
            "stock_code": "00700",
            "market": "HK",
            "source": "playwright_search",
            "title": "company",
            "summary": "网络游戏 云服务",
            "url": "https://example.com",
            "raw_text": "网络游戏 云服务",
            "fetched_at": "2026-06-03T00:00:00",
        }]).to_csv(evidence_csv, index=False, encoding="utf-8-sig")
        build_default_tag_dictionary().to_csv(dictionary_csv, index=False, encoding="utf-8-sig")

        class FakeClient:
            def chat_with_retry(self, messages, **kwargs):
                return '{"stock_code":"00700","tags":[{"tag":"游戏","tag_type":"business","confidence":0.94,"is_primary":true,"evidence":"网络游戏","evidence_url":"https://example.com","decision":"formal","reason":"主营明确"}],"rejected":[]}'

        monkeypatch.setattr("data.ingest.service.LLMClient", lambda *args, **kwargs: FakeClient(), raising=False)
        service = MarketDataService(base_dir=str(base / "data"))
        try:
            summary = service.extract_stock_tags_llm(
                evidence_csv=evidence_csv,
                tag_dictionary_csv=dictionary_csv,
                output_csv=output_csv,
                candidate_output_csv=candidate_csv,
            )
        finally:
            service.close()

        assert summary["formal_rows"] == 1
        assert pd.read_csv(output_csv)["tag"].iloc[0] == "游戏"
```

- [ ] **Step 2: Implement service method**

Add `MarketDataService.extract_stock_tags_llm(...)`:

```python
def extract_stock_tags_llm(
    self,
    evidence_csv="docs/hk_company_browser_evidence.csv",
    tag_dictionary_csv="docs/hk_tag_dictionary.csv",
    output_csv="docs/hk_llm_tag_extraction.csv",
    candidate_output_csv="docs/hk_stock_tag_candidate_llm.csv",
    stock_codes=None,
    limit=None,
    model=None,
    temperature=0.1,
    max_tokens=4096,
    show_progress=False,
):
    from core.llm.client import LLMClient
    from data.ingest.llm_tag_extractor import (
        build_tag_extraction_prompt,
        llm_extractions_to_tag_frames,
        parse_llm_tag_response,
    )

    evidence = pd.read_csv(evidence_csv, dtype=str).fillna("")
    dictionary = pd.read_csv(tag_dictionary_csv, dtype=str).fillna("")
    codes = list(dict.fromkeys(evidence["stock_code"].astype(str)))
    if stock_codes:
        allowed = {normalize_stock_code(code, market="HK") for code in stock_codes}
        codes = [code for code in codes if code in allowed]
    if limit:
        codes = codes[: int(limit)]

    client_cls = globals().get("LLMClient", LLMClient)
    client = client_cls(model=model)
    extractions = []
    errors = []
    iterator = codes
    if show_progress:
        iterator = tqdm(codes, desc="llm tag extract", unit="stock")
    for code in iterator:
        rows = evidence.loc[evidence["stock_code"].astype(str) == code]
        try:
            messages = build_tag_extraction_prompt(code, rows, dictionary)
            text = client.chat_with_retry(messages, temperature=temperature, max_tokens=max_tokens, model=model)
            extractions.append(parse_llm_tag_response(text))
        except Exception as exc:
            errors.append({"stock_code": code, "error": str(exc)})

    formal, candidates = llm_extractions_to_tag_frames(extractions)
    Path(output_csv).parent.mkdir(parents=True, exist_ok=True)
    formal.to_csv(output_csv, index=False, encoding="utf-8-sig")
    candidates.to_csv(candidate_output_csv, index=False, encoding="utf-8-sig")
    return {
        "status": "completed",
        "requested": len(codes),
        "formal_rows": len(formal),
        "candidate_rows": len(candidates),
        "errors": len(errors),
        "error_samples": errors[:10],
        "output_csv": str(output_csv),
        "candidate_output_csv": str(candidate_output_csv),
    }
```

- [ ] **Step 3: Add CLI command**

In `run.py`, add `extract-stock-tags-llm` with args:

```bash
--evidence-csv
--tag-dictionary-csv
--output
--candidate-output
--stock-codes
--limit
--llm-model
--temperature
--max-tokens
--show-progress
```

- [ ] **Step 4: Run service test**

```bash
uv run pytest test/test_stock_tag_registry.py::test_service_extract_stock_tags_llm_uses_cached_evidence -q
```

Expected: PASS.

---

## Task 6: Merge LLM Output Into Registry

**Files:**
- Modify: `data/ingest/stock_tags.py`
- Modify: `data/ingest/service.py`
- Test: `test/test_stock_tag_registry.py`

- [ ] **Step 1: Add merge test**

Add:

```python
def test_build_stock_tag_csvs_can_merge_llm_outputs():
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        industry_csv = base / "industry.csv"
        llm_csv = base / "llm.csv"
        llm_candidate_csv = base / "llm_candidate.csv"
        output_csv = base / "registry.csv"
        candidate_csv = base / "candidate.csv"
        dictionary_csv = base / "dictionary.csv"
        pd.DataFrame([{
            "stock_code": "00700",
            "market": "HK",
            "industry_l1": "资讯科技业",
            "industry_l2": "软件服务",
            "theme_tags": "港股;科技",
            "instrument_type": "common_stock",
            "is_fund_like": "False",
        }]).to_csv(industry_csv, index=False, encoding="utf-8-sig")
        pd.DataFrame([{
            "stock_code": "00700",
            "market": "HK",
            "tag": "游戏",
            "tag_type": "business",
            "confidence": 0.94,
            "is_primary": True,
            "source": "deepseek_browser_evidence",
            "evidence": "网络游戏",
            "evidence_url": "",
            "updated_at": "2026-06-03T00:00:00",
        }], columns=STOCK_TAG_FIELDS).to_csv(llm_csv, index=False, encoding="utf-8-sig")
        pd.DataFrame(columns=STOCK_TAG_CANDIDATE_FIELDS).to_csv(llm_candidate_csv, index=False, encoding="utf-8-sig")

        service = MarketDataService(base_dir=str(base / "data"))
        try:
            service.build_stock_tag_csvs(
                industry_registry_csv=industry_csv,
                tag_dictionary_csv=dictionary_csv,
                output_csv=output_csv,
                candidate_output_csv=candidate_csv,
                llm_tag_csv=llm_csv,
                llm_candidate_csv=llm_candidate_csv,
            )
        finally:
            service.close()
        tags = pd.read_csv(output_csv, dtype=str).fillna("")
        assert "游戏" in set(tags["tag"])
```

- [ ] **Step 2: Extend `build_stock_tag_csvs` signature**

In `data/ingest/service.py`, add optional args:

```python
llm_tag_csv=None,
llm_candidate_csv=None,
```

After evidence merge:

```python
if llm_tag_csv and Path(llm_tag_csv).exists():
    llm_formal = pd.read_csv(llm_tag_csv, dtype=str).fillna("")
    formal = pd.concat([formal, llm_formal], ignore_index=True)
if llm_candidate_csv and Path(llm_candidate_csv).exists():
    llm_candidates = pd.read_csv(llm_candidate_csv, dtype=str).fillna("")
    candidates = pd.concat([candidates, llm_candidates], ignore_index=True)
formal = formal.drop_duplicates(subset=["stock_code", "market", "tag", "tag_type"], keep="last")
candidates = candidates.drop_duplicates(subset=["stock_code", "market", "tag", "tag_type"], keep="last")
```

- [ ] **Step 3: Extend CLI**

Add to `build-stock-tags`:

```bash
--llm-tag-csv docs/hk_llm_tag_extraction.csv
--llm-candidate-csv docs/hk_stock_tag_candidate_llm.csv
```

- [ ] **Step 4: Run merge test**

```bash
uv run pytest test/test_stock_tag_registry.py::test_build_stock_tag_csvs_can_merge_llm_outputs -q
```

Expected: PASS.

---

## Task 7: Candidate Review CLI

**Files:**
- Modify: `data/ingest/service.py`
- Modify: `run.py`
- Test: `test/test_stock_tag_registry.py`

- [ ] **Step 1: Add review test**

Add:

```python
def test_review_stock_tag_candidates_accepts_selected_rows():
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        candidate_csv = base / "candidate.csv"
        accepted_csv = base / "accepted.csv"
        pd.DataFrame([{
            "stock_code": "00700",
            "market": "HK",
            "tag": "AI",
            "tag_type": "theme",
            "confidence": 0.68,
            "is_primary": False,
            "source": "deepseek_browser_evidence",
            "evidence": "AI news",
            "evidence_url": "",
            "updated_at": "2026-06-03T00:00:00",
            "review_status": "accepted",
            "review_note": "人工确认",
        }], columns=STOCK_TAG_CANDIDATE_FIELDS).to_csv(candidate_csv, index=False, encoding="utf-8-sig")

        service = MarketDataService(base_dir=str(base / "data"))
        try:
            summary = service.review_stock_tag_candidates(candidate_csv=candidate_csv, accepted_output_csv=accepted_csv)
        finally:
            service.close()
        assert summary["accepted_rows"] == 1
        assert pd.read_csv(accepted_csv)["tag"].iloc[0] == "AI"
```

- [ ] **Step 2: Implement review method**

Add:

```python
def review_stock_tag_candidates(
    self,
    candidate_csv="docs/hk_stock_tag_candidate.csv",
    accepted_output_csv="docs/hk_stock_tag_accepted_from_candidates.csv",
):
    candidates = pd.read_csv(candidate_csv, dtype=str).fillna("")
    accepted = candidates.loc[candidates["review_status"].astype(str).str.lower() == "accepted"].copy()
    if not accepted.empty:
        accepted = accepted[STOCK_TAG_FIELDS]
    else:
        accepted = pd.DataFrame(columns=STOCK_TAG_FIELDS)
    Path(accepted_output_csv).parent.mkdir(parents=True, exist_ok=True)
    accepted.to_csv(accepted_output_csv, index=False, encoding="utf-8-sig")
    return {
        "status": "completed",
        "candidate_rows": len(candidates),
        "accepted_rows": len(accepted),
        "accepted_output_csv": str(accepted_output_csv),
    }
```

- [ ] **Step 3: Add CLI**

Add `review-stock-tag-candidates`:

```bash
uv run python run.py review-stock-tag-candidates \
  --candidate-csv docs/hk_stock_tag_candidate.csv \
  --accepted-output-csv docs/hk_stock_tag_accepted_from_candidates.csv
```

- [ ] **Step 4: Run review test**

```bash
uv run pytest test/test_stock_tag_registry.py::test_review_stock_tag_candidates_accepts_selected_rows -q
```

Expected: PASS.

---

## Task 8: README And Manual Commands

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add browser + LLM workflow**

Add under `股票标签 Registry`:

```bash
# 1. 安装 Playwright Chromium，首次需要
uv run playwright install chromium

# 2. 浏览器搜索抓证据，先小样本
uv run python run.py browser-research-stock-tags \
  --industry-registry-csv docs/hk_industry_registry.csv \
  --evidence-csv docs/hk_company_browser_evidence.csv \
  --limit 20 \
  --max-results-per-query 5 \
  --max-pages-per-stock 8 \
  --per-page-timeout 12 \
  --show-progress

# 3. DeepSeek 从证据中抽取结构化 tag
export DEEPSEEK_API_KEY=...
uv run python run.py extract-stock-tags-llm \
  --evidence-csv docs/hk_company_browser_evidence.csv \
  --tag-dictionary-csv docs/hk_tag_dictionary.csv \
  --output docs/hk_llm_tag_extraction.csv \
  --candidate-output docs/hk_stock_tag_candidate_llm.csv \
  --llm-model deepseek-chat \
  --limit 20 \
  --show-progress

# 4. 合并行业 registry、浏览器 evidence 和 LLM tags
uv run python run.py build-stock-tags \
  --industry-registry-csv docs/hk_industry_registry.csv \
  --evidence-csv docs/hk_company_research_evidence.csv \
  --llm-tag-csv docs/hk_llm_tag_extraction.csv \
  --llm-candidate-csv docs/hk_stock_tag_candidate_llm.csv \
  --tag-dictionary-csv docs/hk_tag_dictionary.csv \
  --output docs/hk_stock_tag_registry.csv \
  --candidate-output docs/hk_stock_tag_candidate.csv

# 5. 覆盖导入仓库，避免旧 tag 残留
uv run python run.py import-stock-tags \
  --tag-dictionary-csv docs/hk_tag_dictionary.csv \
  --stock-tag-csv docs/hk_stock_tag_registry.csv \
  --candidate-csv docs/hk_stock_tag_candidate.csv \
  --evidence-csv docs/hk_company_browser_evidence.csv \
  --replace
```

- [ ] **Step 2: Document operational guardrails**

Add:

- Use `--limit` for every first run.
- Use `--stock-codes 00700 01208 03690` for smoke tests.
- Browser scraping may be blocked by Google; evidence rows must show `source` and `url`.
- LLM output is never final unless it passes threshold and evidence rules.
- `candidate` tags are not used for official selection until reviewed or merged.

---

## Task 9: Verification

**Files:**
- No new files.

- [ ] **Step 1: Unit tests**

Run:

```bash
uv run pytest test/test_stock_tag_registry.py -q
```

Expected: all tag tests pass.

- [ ] **Step 2: Data layer tests**

Run:

```bash
uv run pytest test/test_data_layer_smoke.py -q
```

Expected: all smoke tests pass.

- [ ] **Step 3: CLI smoke without network**

Run:

```bash
uv run python run.py build-stock-tags \
  --industry-registry-csv docs/hk_industry_registry.csv \
  --tag-dictionary-csv docs/hk_tag_dictionary.csv \
  --output docs/hk_stock_tag_registry.csv \
  --candidate-output docs/hk_stock_tag_candidate.csv
```

Expected: command exits 0 and prints dictionary/formal/candidate row counts.

- [ ] **Step 4: Browser smoke with strict limit**

Run only if Playwright Chromium is installed:

```bash
uv run python run.py browser-research-stock-tags \
  --industry-registry-csv docs/hk_industry_registry.csv \
  --evidence-csv docs/hk_company_browser_evidence.csv \
  --stock-codes 00700 \
  --max-results-per-query 2 \
  --max-pages-per-stock 3 \
  --per-page-timeout 8 \
  --show-progress
```

Expected: either evidence rows are written or a clear browser/search error is printed. The command must not hang indefinitely.

- [ ] **Step 5: LLM smoke with fake or real key**

If `DEEPSEEK_API_KEY` is configured:

```bash
uv run python run.py extract-stock-tags-llm \
  --evidence-csv docs/hk_company_browser_evidence.csv \
  --tag-dictionary-csv docs/hk_tag_dictionary.csv \
  --output docs/hk_llm_tag_extraction.csv \
  --candidate-output docs/hk_stock_tag_candidate_llm.csv \
  --stock-codes 00700 \
  --show-progress
```

Expected: CSV outputs exist. If API key is absent, skip real LLM smoke and rely on fake-client unit test.

---

## Recommended Manual Rollout

Start with liquid, well-known stocks to calibrate noise:

```bash
uv run python run.py browser-research-stock-tags \
  --industry-registry-csv docs/hk_industry_registry.csv \
  --evidence-csv docs/hk_company_browser_evidence.csv \
  --stock-codes 00700 03690 09988 01208 00883 00005 01299 02318 01810 09618 \
  --max-results-per-query 5 \
  --max-pages-per-stock 8 \
  --per-page-timeout 12 \
  --show-progress

uv run python run.py extract-stock-tags-llm \
  --evidence-csv docs/hk_company_browser_evidence.csv \
  --tag-dictionary-csv docs/hk_tag_dictionary.csv \
  --output docs/hk_llm_tag_extraction.csv \
  --candidate-output docs/hk_stock_tag_candidate_llm.csv \
  --stock-codes 00700 03690 09988 01208 00883 00005 01299 02318 01810 09618 \
  --llm-model deepseek-chat \
  --show-progress
```

Review the first 10 stocks manually before full-market run.

Full-market run should be staged:

```bash
# 100-stock batch
uv run python run.py browser-research-stock-tags --limit 100 --show-progress
uv run python run.py extract-stock-tags-llm --limit 100 --show-progress

# then 500-stock batch
uv run python run.py browser-research-stock-tags --limit 500 --show-progress
uv run python run.py extract-stock-tags-llm --limit 500 --show-progress

# then full universe
uv run python run.py browser-research-stock-tags --show-progress
uv run python run.py extract-stock-tags-llm --show-progress
```

---

## Open Risks

1. Search engines may block Playwright automation. Keep provider interface isolated so Baidu/Bing/SerpAPI-like providers can be swapped later.
2. DeepSeek may output invalid JSON. Parser must reject invalid responses and record errors, not silently accept.
3. LLM may over-tag popular themes. Thresholds and source weights must keep weak single-news mentions in candidates.
4. Browser scraping all HK stocks is slow. Cache evidence and run staged batches.
5. Some tags are investable themes but not company exposures. The prompt and rules must reject customer-only or supplier-only mentions unless value-chain exposure is explicit.

---

## Completion Criteria

This plan is complete when:

- `hk_tag_dictionary.csv` expands from 14 rows to at least 120 rows.
- Browser evidence collection can run for a small stock list and write `docs/hk_company_browser_evidence.csv`.
- DeepSeek extraction can parse evidence into formal and candidate tag CSVs.
- `build-stock-tags` can merge LLM formal/candidate outputs into existing registry outputs.
- `import-stock-tags --replace` can rebuild warehouse tag tables without stale tag residue.
- Tests pass:
  - `uv run pytest test/test_stock_tag_registry.py -q`
  - `uv run pytest test/test_data_layer_smoke.py -q`
