# Local LightRAG Runtime

This directory contains the local database/model services used by the LightRAG API server.

## Services

- `stock-lightrag-postgres`: PostgreSQL with pgvector and Apache AGE for LightRAG `PG*Storage`.
- `stock-lightrag-ollama`: optional Ollama profile for local `bge-m3` embeddings.

`stock-lightrag-postgres` is built locally from `../LightRAG/Dockerfile.postgres` by default. This avoids pulling `gzdaniel/postgres-for-rag:pg18-age-pgvector` from Docker Hub, which is unreliable behind some mirrors. The first build downloads the `pgvector/pgvector:pg18-trixie` base image, Debian build dependencies, and compiles Apache AGE, so it can take several minutes on a slow network.

Ollama is intentionally opt-in because the image is 3GB+. Default compose commands do not pull it.

## Start

```bash
cp deploy/lightrag/server.env.example deploy/lightrag/server.env
docker compose --env-file deploy/lightrag/server.env -f deploy/lightrag/docker-compose.yml up -d

# Optional local embedding service:
docker compose --profile ollama --env-file deploy/lightrag/server.env -f deploy/lightrag/docker-compose.yml up -d
docker exec stock-lightrag-ollama ollama pull bge-m3
```

Install and start the LightRAG API server:

```bash
cd /home/yuxun/quant/LightRAG
uv sync --extra api --extra offline-storage --extra offline-llm

cd /home/yuxun/quant/stock_analysis_by_gpt
export DEEPSEEK_API_KEY=...
bash deploy/lightrag/start-server.sh
```

Run it in the background with `tmux`:

```bash
cd /home/yuxun/quant/stock_analysis_by_gpt
tmux new -d -s lightrag 'LIGHTRAG_PATH=/home/yuxun/quant/LightRAG DEEPSEEK_API_KEY=... bash deploy/lightrag/start-server.sh'

tmux ls
tmux attach -t lightrag
tmux kill-session -t lightrag
```

API health: `http://127.0.0.1:9621/health`
API docs: `http://127.0.0.1:9621/docs`

The server script reads `DEEPSEEK_API_KEY` from the shell and does not write secrets to disk. Override defaults by copying `deploy/lightrag/server.env.example` to `deploy/lightrag/server.env` and editing that local file.

Notes:

- The LightRAG WebUI route is only available after building the upstream `lightrag_webui` frontend.
- The PostgreSQL 18 image mounts `./data/postgres` to `/var/lib/postgresql`, not `/var/lib/postgresql/data`.
- The server uses `assets/lightrag/tiktoken_cache` to avoid first-start tokenizer downloads.
