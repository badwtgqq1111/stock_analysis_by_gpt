# 环境部署与检查

## 一键入口

在项目根目录执行：

```bash
uv run python scripts/setup_environment.py
```

入口会安装/同步项目依赖，启动 ClickHouse、SearXNG、LightRAG PostgreSQL，并执行 Python、依赖、CLI、磁盘、ClickHouse、公开数据源和 A 股仓库覆盖率检查。只检查、不启动服务：

```bash
uv run python scripts/setup_environment.py --check-only
```

若本机还没有 `uv`，先安装它；项目解释器由 `uv` 管理，系统 `python3` 版本不影响运行时。

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync --dev
```

## 配置

统一配置在 [config/environment.toml](../../config/environment.toml)。默认部署全部本地服务：

```toml
[services]
components = ["clickhouse", "searxng", "lightrag"]
auto_deploy = true
retries = 2
with_ollama = false

[clickhouse]
http_port = 8123
native_port = 9000
```

复制该文件可建立一套独立的端口、密码或服务组合配置：

```bash
cp config/environment.toml config/environment.local.toml
uv run python scripts/setup_environment.py --config config/environment.local.toml
```

`with_ollama = true` 会启用 Ollama profile，并在未设置 `skip_ollama_pull` 时下载 `bge-m3`。这一步较大，只在本地 embedding 确有需要时开启。

## 监测结果

`OK` 表示本地组件或公开数据源实际响应。`WARN` 不会中断部署，常见于东方财富等公开接口的暂时限流或超时，也会用于提示 A 股数据尚未下载。`FAIL` 会使部署失败，例如依赖无法导入、Docker daemon 不可用或 ClickHouse 无法查询。

只查看 A 股链路状态：

```bash
uv run python scripts/check_cn_pipeline.py
```

该检查不会写入行情、财务或特征数据。

## 运行时存储

`pyproject.toml` 将 AkShare 指向同级 `../akshare` 目录；该目录必须存在。ClickHouse 可用时应用优先使用它，不可用时回退本地 Parquet。数据服务目录位于 `assets/`，不要删除 `assets/clickhouse` 或 `assets/data` 中正在使用的数据。

LightRAG API 需要单独安装上游 `../LightRAG`，并提供 `DEEPSEEK_API_KEY` 或 `LLM_BINDING_API_KEY`：

```bash
uv run python deploy/lightrag/start_server.py
```
