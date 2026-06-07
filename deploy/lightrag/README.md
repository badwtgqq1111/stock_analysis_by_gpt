# Local LightRAG Runtime

This directory contains the local database/model services used by the LightRAG API server.

## Services

- `stock-lightrag-postgres`: PostgreSQL with pgvector and Apache AGE for LightRAG `PG*Storage`.
- `stock-lightrag-ollama`: Ollama for local `bge-m3` embeddings.

## Start

```bash
cp deploy/lightrag/.env.example deploy/lightrag/.env
docker compose --env-file deploy/lightrag/.env -f deploy/lightrag/docker-compose.yml up -d
docker exec stock-lightrag-ollama ollama pull bge-m3
```

Install and start the LightRAG API server:

```bash
cd /Users/ccs/code/quant/LightRAG
uv sync --extra api --extra offline-storage --extra offline-llm

cd /Users/ccs/code/quant/stock_analysis_by_gpt
export DEEPSEEK_API_KEY=...
bash deploy/lightrag/start-server.sh
```

API health: `http://127.0.0.1:9621/health`
API docs: `http://127.0.0.1:9621/docs`

The server script reads `DEEPSEEK_API_KEY` from the shell and does not write secrets to disk. Override defaults by exporting variables from `deploy/lightrag/server.env.example`.

Notes:

- The LightRAG WebUI route is only available after building the upstream `lightrag_webui` frontend.
- The PostgreSQL 18 image mounts `./data/postgres` to `/var/lib/postgresql`, not `/var/lib/postgresql/data`.
- The server uses `assets/lightrag/tiktoken_cache` to avoid first-start tokenizer downloads.
