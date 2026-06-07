#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LIGHTRAG_PATH="${LIGHTRAG_PATH:-/Users/ccs/code/quant/LightRAG}"

load_env_file() {
  local env_file="$1"
  if [[ -f "${env_file}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${env_file}"
    set +a
  fi
}

load_env_file "${SCRIPT_DIR}/.env"
load_env_file "${SCRIPT_DIR}/server.env"

if [[ ! -d "${LIGHTRAG_PATH}" ]]; then
  echo "LightRAG source directory not found: ${LIGHTRAG_PATH}" >&2
  echo "Set LIGHTRAG_PATH=/path/to/LightRAG and retry." >&2
  exit 1
fi

if [[ -z "${DEEPSEEK_API_KEY:-}" && -z "${LLM_BINDING_API_KEY:-}" ]]; then
  echo "DEEPSEEK_API_KEY is not set. Export it before starting LightRAG." >&2
  exit 1
fi

mkdir -p "${PROJECT_DIR}/assets/lightrag/rag_storage"
mkdir -p "${PROJECT_DIR}/assets/lightrag/inputs"
mkdir -p "${PROJECT_DIR}/assets/lightrag/prompts"
mkdir -p "${PROJECT_DIR}/assets/lightrag/tiktoken_cache"

if [[ ! -x "${LIGHTRAG_PATH}/.venv/bin/lightrag-server" ]]; then
  echo "LightRAG virtualenv is missing. Run: cd ${LIGHTRAG_PATH} && uv sync --extra api --extra offline-storage --extra offline-llm" >&2
  exit 1
fi

cd "${PROJECT_DIR}/assets/lightrag"
if [[ ! -f ".env" ]]; then
  printf "LIGHTRAG_RUNTIME_TARGET=host\n" > ".env"
fi

export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-9621}"
export WORKSPACE="${WORKSPACE:-hk_stock_profile}"
export SUMMARY_LANGUAGE="${SUMMARY_LANGUAGE:-Chinese}"
export CORS_ORIGINS="${CORS_ORIGINS:-*}"
export WHITELIST_PATHS="${WHITELIST_PATHS:-/health,/api/*,/webui,/webui/*}"

export LIGHTRAG_KV_STORAGE="${LIGHTRAG_KV_STORAGE:-PGKVStorage}"
export LIGHTRAG_DOC_STATUS_STORAGE="${LIGHTRAG_DOC_STATUS_STORAGE:-PGDocStatusStorage}"
export LIGHTRAG_VECTOR_STORAGE="${LIGHTRAG_VECTOR_STORAGE:-PGVectorStorage}"
export LIGHTRAG_GRAPH_STORAGE="${LIGHTRAG_GRAPH_STORAGE:-PGGraphStorage}"

export POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
export POSTGRES_PORT="${POSTGRES_PORT:-15432}"
export POSTGRES_USER="${POSTGRES_USER:-lightrag}"
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-lightrag_change_me}"
export POSTGRES_DATABASE="${POSTGRES_DATABASE:-lightrag_hk_stock}"
export POSTGRES_VECTOR_INDEX_TYPE="${POSTGRES_VECTOR_INDEX_TYPE:-HNSW}"

export LLM_BINDING="${LLM_BINDING:-openai}"
export LLM_BINDING_HOST="${LLM_BINDING_HOST:-https://api.deepseek.com}"
export LLM_BINDING_API_KEY="${LLM_BINDING_API_KEY:-${DEEPSEEK_API_KEY}}"
export LLM_MODEL="${LLM_MODEL:-deepseek-v4-pro}"
export MAX_ASYNC_LLM="${MAX_ASYNC_LLM:-4}"
export OPENAI_LLM_TEMPERATURE="${OPENAI_LLM_TEMPERATURE:-0.2}"

export EMBEDDING_BINDING="${EMBEDDING_BINDING:-ollama}"
export EMBEDDING_BINDING_HOST="${EMBEDDING_BINDING_HOST:-http://localhost:11434}"
export EMBEDDING_MODEL="${EMBEDDING_MODEL:-bge-m3:latest}"
export EMBEDDING_DIM="${EMBEDDING_DIM:-1024}"
export EMBEDDING_TOKEN_LIMIT="${EMBEDDING_TOKEN_LIMIT:-8192}"
export EMBEDDING_FUNC_MAX_ASYNC="${EMBEDDING_FUNC_MAX_ASYNC:-8}"
export EMBEDDING_BATCH_NUM="${EMBEDDING_BATCH_NUM:-32}"
export TIKTOKEN_CACHE_DIR="${TIKTOKEN_CACHE_DIR:-${PROJECT_DIR}/assets/lightrag/tiktoken_cache}"

export RERANK_BINDING="${RERANK_BINDING:-null}"

exec "${LIGHTRAG_PATH}/.venv/bin/lightrag-server" \
  --host "${HOST}" \
  --port "${PORT}" \
  --working-dir "${PROJECT_DIR}/assets/lightrag/rag_storage" \
  --input-dir "${PROJECT_DIR}/assets/lightrag/inputs" \
  --workspace "${WORKSPACE}"
