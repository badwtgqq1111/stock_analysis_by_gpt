# SearXNG 本地搜索部署与股票标签接入方案

本文档用于把股票标签 evidence 搜索链路从 `Playwright` / `Tavily` 迁移到本地免费的 `SearXNG` 聚合搜索。

目标：

- 全量港股 evidence 搜索优先走本地 SearXNG。
- Tavily 作为可选付费兜底。
- Playwright 只保留为小样本诊断和特殊页面兜底。
- 搜索结果仍然写入现有 `company_research_evidence` schema，后续 DeepSeek 抽取、tag 合并和导入流程不变。

## 为什么改成 SearXNG

当前三种搜索方式的定位：

| 方案 | 成本 | 速度 | 稳定性 | 推荐用途 |
|---|---:|---:|---:|---|
| SearXNG | 本地免费 | 中高 | 取决于上游搜索引擎 | 全量 evidence 主链路 |
| Tavily | 按额度/套餐 | 高 | 较好 | 小批量验证、兜底 |
| Playwright | 免费 | 低 | 易触发搜索站风控 | 小样本 smoke、疑难补查 |

SearXNG 不是“真正离线搜索引擎”，它是本地部署的元搜索服务，会请求外部搜索引擎；优势是不用每只股票打开浏览器，不需要 Tavily 额度，并且可以统一缓存、限速、切换 engines。

## 部署方案

推荐把 SearXNG 放在项目的 `deploy/searxng` 下。`assets/` 保持为数据资产和数据卷目录，例如 `assets/clickhouse`；SearXNG 属于基础设施服务配置，放在 `deploy/` 下更清晰。

### 目录结构

```text
deploy/searxng/
  docker-compose.yml
  searxng/
    settings.yml
  cache/
```

### docker run 启动

如果本机没有 Docker Compose v2 插件，或者老版 `docker-compose` 因 Python 3.12 缺少 `distutils` 报错，直接用 `docker run`：

```bash
mkdir -p deploy/searxng/searxng deploy/searxng/cache

docker run -d \
  --name quant-searxng \
  --restart unless-stopped \
  -p 127.0.0.1:8888:8080 \
  -v "$(pwd)/deploy/searxng/searxng:/etc/searxng:rw" \
  -v "$(pwd)/deploy/searxng/cache:/var/cache/searxng:rw" \
  -e SEARXNG_SETTINGS_PATH=/etc/searxng/settings.yml \
  docker.io/searxng/searxng:latest

docker ps --filter name=quant-searxng
```

说明：

- 只绑定 `127.0.0.1:8888`，不要暴露公网。
- `restart: unless-stopped` 保证 Docker 服务重启后自动拉起。
- `/etc/searxng` 持久化配置，`/var/cache/searxng` 持久化缓存。

如果容器已存在，需要重建：

```bash
docker stop quant-searxng
docker rm quant-searxng
```

### 可选：docker-compose.yml

仅当 `docker compose version` 可用时使用：

```yaml
services:
  searxng:
    image: docker.io/searxng/searxng:latest
    container_name: quant-searxng
    restart: unless-stopped
    ports:
      - "127.0.0.1:8888:8080"
    volumes:
      - ./searxng:/etc/searxng:rw
      - ./cache:/var/cache/searxng:rw
    environment:
      - SEARXNG_SETTINGS_PATH=/etc/searxng/settings.yml
```

### settings.yml

```yaml
use_default_settings: true

server:
  secret_key: "replace-with-random-secret"
  bind_address: "0.0.0.0"
  port: 8080
  limiter: false
  public_instance: false

search:
  formats:
    - html
    - json
  safe_search: 0
  default_lang: "zh-CN"

engines:
  - name: bing
    disabled: false
  - name: duckduckgo
    disabled: false
  - name: google
    disabled: true
```

说明：

- `search.formats` 必须包含 `json`，否则 `/search?format=json` 会返回 403。
- 本项目内部使用时 `limiter: false`，避免本地批处理被限流。仅当暴露公网时再开启 limiter / Valkey / 反代鉴权。
- 先禁用 Google，减少风控和验证码；优先用 Bing / DuckDuckGo 这类相对稳定的 engine。

### Compose 启动

```bash
mkdir -p deploy/searxng/searxng deploy/searxng/cache
cd deploy/searxng
docker compose up -d
docker compose ps
```

如果 `docker compose` 报 `unknown command` 或 `unknown shorthand flag: 'd' in -d`，说明 Compose v2 插件不可用，改用上面的 `docker run`。

### 验证 JSON API

```bash
curl 'http://127.0.0.1:8888/search?q=00700%20腾讯控股%20主营业务%20年报&format=json&language=zh-CN&categories=general'
```

返回 JSON 且包含 `results` 数组即为可用。

常见问题：

| 现象 | 原因 | 处理 |
|---|---|---|
| 403 Forbidden | `settings.yml` 没有启用 `json` format | 检查 `search.formats`，重启容器 |
| 结果为空 | engine 被禁用或上游搜索被限流 | 切换 engines，降低并发 |
| 429/验证码 | 上游搜索或 SearXNG bot detection | 降低 `--max-workers`，禁用易风控 engine |
| 搜到非港股同代码 | 查询词过泛 | 加 `HK`、`港股`、公司名、`site:hkexnews.hk` 等约束 |

## 代码实现方案

### 新增 provider

新增文件：

```text
data/ingest/providers/searxng_company_search.py
```

职责：

- 从 `SEARXNG_URL` 读取本地服务地址，默认 `http://127.0.0.1:8888`。
- 请求 `/search`，参数包含 `q`、`format=json`、`language`、`categories`、`engines`、`pageno`。
- 把 SearXNG `results` 规范化成现有 evidence schema：
  - `stock_code`
  - `market`
  - `source=searxng_search`
  - `title`
  - `summary`
  - `url`
  - `raw_text`
  - `fetched_at`
- 请求失败或无结果时写 `search_error` / `no_results` rank=0 evidence，不让整只股票失败。

### 查询词策略

每只股票默认 3 个 query：

```text
{code} {name} 港股 主营业务 年报 收入分部
{code}.HK {name} company profile business segments annual report
{code} {name} AI 云服务 游戏 铜矿 铁矿 稳定币 业务
```

可选增强：

- 优先查交易所公告：`site:hkexnews.hk {code} {name} annual report`
- 对资源股加资源 query：`铜矿`、`铁矿`、`黄金`、`石油`、`天然气`
- 对科技股加主题 query：`AI`、`云服务`、`游戏`、`半导体`、`算力`

### 新增 service 方法

在 `MarketDataService` 中新增：

```python
searxng_research_stock_tags(
    industry_registry_csv="docs/hk_industry_registry.csv",
    evidence_csv="docs/hk_company_searxng_evidence.csv",
    stock_codes=None,
    limit=None,
    skip_existing=True,
    searxng_url=None,
    max_results_per_query=5,
    max_queries_per_stock=3,
    engines=None,
    language="zh-CN",
    categories="general",
    max_workers=4,
    show_progress=False,
)
```

并发建议：

- 本机 SearXNG：`--max-workers 4` 起步。
- 如果结果质量稳定，可升到 `6-8`。
- 出现 429、空结果明显增多或上游风控时降到 `2-4`。

### 新增 CLI

新增命令：

```bash
uv run python run.py searxng-research-stock-tags \
  --industry-registry-csv docs/hk_industry_registry.csv \
  --evidence-csv docs/hk_company_searxng_evidence.csv \
  --searxng-url http://127.0.0.1:8888 \
  --max-results-per-query 5 \
  --max-queries-per-stock 3 \
  --engines bing,duckduckgo \
  --max-workers 4 \
  --show-progress
```

### 搜索优先级

后续实现统一成三层：

```text
1. searxng-research-stock-tags    # 默认主力，本地免费
2. tavily-research-stock-tags     # 付费/额度兜底
3. browser-research-stock-tags    # 小样本人工诊断兜底
```

README 中增强模式应改为：

```bash
# 1. 推荐：SearXNG 本地免费搜索
uv run python run.py searxng-research-stock-tags ...

# 2. 可选：Tavily 兜底
uv run python run.py tavily-research-stock-tags ...

# 3. 可选：Playwright 小样本诊断
uv run python run.py browser-research-stock-tags --stock-codes 00700 ...
```

### 后续 DeepSeek 抽取不变

```bash
uv run python run.py extract-stock-tags-llm \
  --evidence-csv docs/hk_company_searxng_evidence.csv \
  --tag-dictionary-csv docs/hk_tag_dictionary.csv \
  --output docs/hk_llm_tag_extraction.csv \
  --candidate-output docs/hk_stock_tag_candidate_llm.csv \
  --llm-model deepseek-v4-pro \
  --max-workers 4 \
  --batch-size 10 \
  --checkpoint-every 25 \
  --show-progress
```

`extract-stock-tags-llm` 默认跳过输出 CSV 里已有正式/候选标签的股票，并按 checkpoint 定期写出结果。`--batch-size 10` 会把 10 只股票合并成一次 LLM 请求；全量港股抽取建议从 `--max-workers 4 --batch-size 10` 起步，遇到 DeepSeek 输出缺失、限流或错误率升高时先降 batch size，再降并发。

### 测试计划

新增或扩展 `test/test_stock_tag_registry.py`：

- `test_searxng_company_search_fetcher_normalizes_results`
- `test_searxng_company_search_fetcher_records_api_errors`
- `test_service_searxng_research_stock_tags_writes_evidence`
- `test_service_searxng_research_stock_tags_can_run_parallel`
- `test_searxng_cli_help_exposes_expected_options`

验证命令：

```bash
uv run pytest test/test_stock_tag_registry.py -q
uv run python run.py searxng-research-stock-tags --help
```

### 小样本验收

部署完成后先跑 5 只：

```bash
uv run python run.py searxng-research-stock-tags \
  --industry-registry-csv docs/hk_industry_registry.csv \
  --evidence-csv docs/hk_company_searxng_evidence.csv \
  --stock-codes 00700 03690 09988 01208 00883 \
  --searxng-url http://127.0.0.1:8888 \
  --max-results-per-query 5 \
  --max-queries-per-stock 3 \
  --engines bing,duckduckgo \
  --max-workers 4 \
  --show-progress
```

验收标准：

- `errors == 0` 或只有少量 `search_error`。
- `docs/hk_company_searxng_evidence.csv` 有有效 URL 和摘要。
- `rank=0` 占比低于 20%。
- DeepSeek 抽取后正式 tag 和 candidate tag 都有合理 evidence。

## 官方参考

- SearXNG Docker / Compose 安装：<https://docs.searxng.org/admin/installation-docker>
- SearXNG settings.yml：<https://docs.searxng.org/admin/settings/settings.html>
- SearXNG Search API：<https://docs.searxng.org/dev/search_api.html>
