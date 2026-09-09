#!/usr/bin/env python3
"""Start the upstream LightRAG API with this repository's local services."""

from __future__ import annotations

import os
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parents[1]


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def require_path(path: Path, message: str) -> None:
    if not path.exists():
        print(message, file=sys.stderr)
        raise SystemExit(1)


def main() -> None:
    load_env_file(SCRIPT_DIR / ".env")
    load_env_file(SCRIPT_DIR / "server.env")

    lightrag_path = Path(os.environ.get("LIGHTRAG_PATH", "/Users/ccs/code/quant/LightRAG"))
    require_path(lightrag_path, f"LightRAG source directory not found: {lightrag_path}")
    if not os.environ.get("DEEPSEEK_API_KEY") and not os.environ.get("LLM_BINDING_API_KEY"):
        print("DEEPSEEK_API_KEY or LLM_BINDING_API_KEY must be set before starting LightRAG.", file=sys.stderr)
        raise SystemExit(1)

    for directory in ("rag_storage", "inputs", "prompts", "tiktoken_cache"):
        (PROJECT_DIR / "assets" / "lightrag" / directory).mkdir(parents=True, exist_ok=True)
    runtime_env = PROJECT_DIR / "assets" / "lightrag" / ".env"
    if not runtime_env.exists():
        runtime_env.write_text("LIGHTRAG_RUNTIME_TARGET=host\n", encoding="utf-8")

    defaults = {
        "HOST": "0.0.0.0", "PORT": "9621", "WORKSPACE": "hk_stock_profile",
        "SUMMARY_LANGUAGE": "Chinese", "CORS_ORIGINS": "*", "WHITELIST_PATHS": "/health,/api/*,/webui,/webui/*",
        "LIGHTRAG_KV_STORAGE": "PGKVStorage", "LIGHTRAG_DOC_STATUS_STORAGE": "PGDocStatusStorage",
        "LIGHTRAG_VECTOR_STORAGE": "PGVectorStorage", "LIGHTRAG_GRAPH_STORAGE": "PGGraphStorage",
        "POSTGRES_HOST": "localhost", "POSTGRES_PORT": "15432", "POSTGRES_USER": "lightrag",
        "POSTGRES_PASSWORD": "lightrag_change_me", "POSTGRES_DATABASE": "lightrag_hk_stock",
        "POSTGRES_VECTOR_INDEX_TYPE": "HNSW", "LLM_BINDING": "openai",
        "LLM_BINDING_HOST": "https://api.deepseek.com", "LLM_MODEL": "deepseek-v4-pro",
        "MAX_ASYNC_LLM": "4", "OPENAI_LLM_TEMPERATURE": "0.2", "EMBEDDING_BINDING": "ollama",
        "EMBEDDING_BINDING_HOST": "http://localhost:11434", "EMBEDDING_MODEL": "bge-m3:latest",
        "EMBEDDING_DIM": "1024", "EMBEDDING_TOKEN_LIMIT": "8192", "EMBEDDING_FUNC_MAX_ASYNC": "8",
        "EMBEDDING_BATCH_NUM": "32", "RERANK_BINDING": "null",
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)
    os.environ.setdefault("LLM_BINDING_API_KEY", os.environ.get("DEEPSEEK_API_KEY", ""))
    os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(PROJECT_DIR / "assets" / "lightrag" / "tiktoken_cache"))

    executable = lightrag_path / ".venv" / "bin" / "lightrag-server"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        print(
            f"LightRAG virtualenv is missing. Run: cd {lightrag_path} && uv sync --extra api --extra offline-storage --extra offline-llm",
            file=sys.stderr,
        )
        raise SystemExit(1)
    command = [
        str(executable), "--host", os.environ["HOST"], "--port", os.environ["PORT"],
        "--working-dir", str(PROJECT_DIR / "assets" / "lightrag" / "rag_storage"),
        "--input-dir", str(PROJECT_DIR / "assets" / "lightrag" / "inputs"),
        "--workspace", os.environ["WORKSPACE"],
    ]
    os.chdir(PROJECT_DIR / "assets" / "lightrag")
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    main()
