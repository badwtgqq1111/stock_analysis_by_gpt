#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}" || exit 1

ACTION="up"
COMPONENTS="clickhouse,searxng,lightrag"
DEPLOY_RETRIES="${DEPLOY_RETRIES:-2}"
WITH_OLLAMA=0
SKIP_OLLAMA_PULL=0

RESULT_COMPONENTS=()
RESULT_STATUS=()
RESULT_DETAILS=()
HAS_FAILURE=0
RUN_OUTPUT=""

usage() {
  cat <<'EOF'
Usage:
  bash scripts/deploy_local_services.sh [up|check|down|restart|pull] [options]

Options:
  --components LIST      Comma-separated components: clickhouse,searxng,lightrag
  --retries N           Retry docker pull/up operations N times (default: 2)
  --with-ollama         Also start the optional Ollama profile for local embeddings
  --skip-ollama-pull    With --with-ollama, skip `docker exec stock-lightrag-ollama ollama pull bge-m3`
  --help                Show this help

Examples:
  bash scripts/deploy_local_services.sh
  bash scripts/deploy_local_services.sh up --components clickhouse,searxng
  bash scripts/deploy_local_services.sh check
  bash scripts/deploy_local_services.sh up --components lightrag
  bash scripts/deploy_local_services.sh up --components lightrag --with-ollama
EOF
}

record_result() {
  local component="$1"
  local status="$2"
  local detail="$3"

  RESULT_COMPONENTS+=("${component}")
  RESULT_STATUS+=("${status}")
  RESULT_DETAILS+=("${detail}")

  if [[ "${status}" == "FAIL" ]]; then
    HAS_FAILURE=1
  fi
}

print_summary() {
  printf '\nDeployment summary:\n'
  printf '%-28s %-6s %s\n' "Component" "Status" "Detail"
  printf '%-28s %-6s %s\n' "---------" "------" "------"

  local i
  for i in "${!RESULT_COMPONENTS[@]}"; do
    printf '%-28s %-6s %s\n' "${RESULT_COMPONENTS[$i]}" "${RESULT_STATUS[$i]}" "${RESULT_DETAILS[$i]}"
  done
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

docker_compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  elif command_exists docker-compose; then
    docker-compose "$@"
  else
    return 127
  fi
}

run_with_retries() {
  local label="$1"
  shift
  local attempt=1
  local tmp_output
  tmp_output="$(mktemp)"

  while (( attempt <= DEPLOY_RETRIES )); do
    : > "${tmp_output}"
    if "$@" >"${tmp_output}" 2>&1; then
      RUN_OUTPUT="$(cat "${tmp_output}")"
      rm -f "${tmp_output}"
      return 0
    fi

    RUN_OUTPUT="$(cat "${tmp_output}")"
    if (( attempt < DEPLOY_RETRIES )); then
      printf '[retry] %s failed, retrying (%s/%s)...\n' "${label}" "${attempt}" "${DEPLOY_RETRIES}" >&2
      sleep 2
    fi
    attempt=$((attempt + 1))
  done

  rm -f "${tmp_output}"
  return 1
}

contains_component() {
  local target="$1"
  local item
  IFS=',' read -ra items <<< "${COMPONENTS}"
  for item in "${items[@]}"; do
    item="${item// /}"
    if [[ "${item}" == "${target}" ]]; then
      return 0
    fi
  done
  return 1
}

parse_args() {
  if [[ $# -gt 0 ]]; then
    case "$1" in
      up|check|down|restart|pull)
        ACTION="$1"
        shift
        ;;
      -h|--help|help)
        usage
        exit 0
        ;;
    esac
  fi

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --components)
        COMPONENTS="${2:-}"
        shift 2
        ;;
      --components=*)
        COMPONENTS="${1#*=}"
        shift
        ;;
      --retries)
        DEPLOY_RETRIES="${2:-}"
        shift 2
        ;;
      --retries=*)
        DEPLOY_RETRIES="${1#*=}"
        shift
        ;;
      --skip-ollama-pull|--no-ollama-pull)
        SKIP_OLLAMA_PULL=1
        shift
        ;;
      --with-ollama)
        WITH_OLLAMA=1
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        printf 'Unknown argument: %s\n\n' "$1" >&2
        usage >&2
        exit 2
        ;;
    esac
  done

  if [[ -z "${COMPONENTS}" ]]; then
    printf 'No components selected.\n' >&2
    exit 2
  fi
  local item
  IFS=',' read -ra items <<< "${COMPONENTS}"
  for item in "${items[@]}"; do
    item="${item// /}"
    case "${item}" in
      clickhouse|searxng|lightrag) ;;
      *)
        printf 'Unknown component: %s\n' "${item}" >&2
        exit 2
        ;;
    esac
  done
  if ! [[ "${DEPLOY_RETRIES}" =~ ^[0-9]+$ ]] || (( DEPLOY_RETRIES < 1 )); then
    printf '--retries must be a positive integer.\n' >&2
    exit 2
  fi
}

preflight() {
  if ! command_exists docker; then
    record_result "docker" "FAIL" "docker CLI not found"
    return 1
  fi

  if ! docker info >/dev/null 2>&1; then
    record_result "docker" "FAIL" "docker daemon is not reachable"
    return 1
  fi

  record_result "docker" "OK" "daemon reachable"
  return 0
}

curl_health() {
  local url="$1"
  curl -fsS --connect-timeout 3 --max-time 15 "${url}" >/dev/null 2>&1
}

deploy_clickhouse() {
  local http_port="${CLICKHOUSE_HTTP_PORT:-8123}"
  local native_port="${CLICKHOUSE_NATIVE_PORT:-9000}"
  local password="${CLICKHOUSE_PASSWORD:-quant2024}"
  local database="${CLICKHOUSE_DATABASE:-quant}"

  mkdir -p assets/clickhouse

  if docker ps -a --format '{{.Names}}' | grep -qx 'clickhouse'; then
    if docker start clickhouse >/dev/null 2>&1; then
      record_result "clickhouse deploy" "OK" "existing container started"
    else
      record_result "clickhouse deploy" "FAIL" "failed to start existing container"
      return
    fi
  else
    if run_with_retries "clickhouse docker run" \
      docker run -d --name clickhouse \
        --restart unless-stopped \
        -p "${http_port}:8123" -p "${native_port}:9000" \
        -v "${ROOT_DIR}/assets/clickhouse:/var/lib/clickhouse" \
        -e CLICKHOUSE_USER=default \
        -e "CLICKHOUSE_PASSWORD=${password}" \
        -e "CLICKHOUSE_DB=${database}" \
        clickhouse/clickhouse-server; then
      record_result "clickhouse deploy" "OK" "container created on HTTP ${http_port}"
    else
      record_result "clickhouse deploy" "FAIL" "docker run failed: $(short_error "${RUN_OUTPUT}")"
      return
    fi
  fi

  check_clickhouse
}

check_clickhouse() {
  local http_port="${CLICKHOUSE_HTTP_PORT:-8123}"
  if curl_health "http://127.0.0.1:${http_port}/ping"; then
    record_result "clickhouse health" "OK" "http://127.0.0.1:${http_port}/ping"
  else
    record_result "clickhouse health" "FAIL" "ping failed on HTTP port ${http_port}"
  fi
}

down_clickhouse() {
  if docker ps -a --format '{{.Names}}' | grep -qx 'clickhouse'; then
    if docker stop clickhouse >/dev/null 2>&1; then
      record_result "clickhouse down" "OK" "container stopped"
    else
      record_result "clickhouse down" "FAIL" "failed to stop container"
    fi
  else
    record_result "clickhouse down" "SKIP" "container not found"
  fi
}

deploy_searxng_compose() {
  docker_compose -f deploy/searxng/docker-compose.yml up -d
}

deploy_searxng_docker_run() {
  local port="${SEARXNG_PORT:-8888}"

  if docker ps -a --format '{{.Names}}' | grep -qx 'quant-searxng'; then
    docker start quant-searxng >/dev/null
    return $?
  fi

  docker run -d --name quant-searxng \
    --restart unless-stopped \
    --add-host host.docker.internal:host-gateway \
    -p "127.0.0.1:${port}:8080" \
    -v "${ROOT_DIR}/deploy/searxng/searxng:/etc/searxng:rw" \
    -v "${ROOT_DIR}/deploy/searxng/cache:/var/cache/searxng:rw" \
    -e SEARXNG_SETTINGS_PATH=/etc/searxng/settings.yml \
    docker.io/searxng/searxng:latest
}

deploy_searxng() {
  mkdir -p deploy/searxng/searxng deploy/searxng/cache

  if run_with_retries "searxng compose up" deploy_searxng_compose; then
    record_result "searxng deploy" "OK" "docker compose up -d"
  else
    local compose_error
    compose_error="$(short_error "${RUN_OUTPUT}")"
    if run_with_retries "searxng docker run fallback" deploy_searxng_docker_run; then
      record_result "searxng deploy" "OK" "docker run fallback used after compose error: ${compose_error}"
    else
      record_result "searxng deploy" "FAIL" "compose and docker run failed: $(short_error "${RUN_OUTPUT}")"
      return
    fi
  fi

  check_searxng
}

check_searxng() {
  local url="${SEARXNG_URL:-http://127.0.0.1:${SEARXNG_PORT:-8888}}"
  local query="${url}/search?q=00700%20Tencent%20annual%20report&format=json&language=zh-CN&categories=general"

  if curl_health "${query}"; then
    record_result "searxng health" "OK" "${url}/search?format=json"
  else
    record_result "searxng health" "FAIL" "JSON search failed; inspect: docker logs quant-searxng"
  fi
}

down_searxng() {
  if docker_compose -f deploy/searxng/docker-compose.yml down >/dev/null 2>&1; then
    record_result "searxng down" "OK" "compose stack stopped"
  elif docker ps -a --format '{{.Names}}' | grep -qx 'quant-searxng' && docker stop quant-searxng >/dev/null 2>&1; then
    record_result "searxng down" "OK" "container stopped"
  else
    record_result "searxng down" "SKIP" "container not found or already stopped"
  fi
}

ensure_lightrag_env() {
  if [[ -f deploy/lightrag/server.env ]]; then
    record_result "lightrag env" "OK" "deploy/lightrag/server.env exists"
    return 0
  fi

  if [[ ! -f deploy/lightrag/server.env.example ]]; then
    record_result "lightrag env" "FAIL" "deploy/lightrag/server.env.example missing"
    return 1
  fi

  cp deploy/lightrag/server.env.example deploy/lightrag/server.env
  record_result "lightrag env" "OK" "created deploy/lightrag/server.env from example"
}

load_lightrag_env() {
  if [[ -f deploy/lightrag/server.env ]]; then
    set -a
    # shellcheck disable=SC1091
    source deploy/lightrag/server.env
    set +a
  fi
}

deploy_lightrag_compose() {
  if (( WITH_OLLAMA == 1 )); then
    docker_compose --profile ollama --env-file deploy/lightrag/server.env -f deploy/lightrag/docker-compose.yml up -d
  else
    docker_compose --env-file deploy/lightrag/server.env -f deploy/lightrag/docker-compose.yml up -d
  fi
}

deploy_lightrag() {
  ensure_lightrag_env || return
  load_lightrag_env

  if run_with_retries "lightrag compose up" deploy_lightrag_compose; then
    if (( WITH_OLLAMA == 1 )); then
      record_result "lightrag deploy" "OK" "postgres and ollama containers requested"
    else
      record_result "lightrag deploy" "OK" "postgres requested; ollama profile skipped"
    fi
  else
    record_result "lightrag deploy" "FAIL" "$(lightrag_compose_failure_hint "${RUN_OUTPUT}")"
    return
  fi

  if (( WITH_OLLAMA == 0 )); then
    record_result "lightrag embedding model" "SKIP" "ollama profile not enabled"
  elif (( SKIP_OLLAMA_PULL == 1 )); then
    record_result "lightrag embedding model" "SKIP" "ollama pull skipped"
  elif run_with_retries "ollama pull bge-m3" docker exec stock-lightrag-ollama ollama pull bge-m3; then
    record_result "lightrag embedding model" "OK" "bge-m3 pulled or already present"
  else
    record_result "lightrag embedding model" "FAIL" "ollama pull failed: $(short_error "${RUN_OUTPUT}")"
  fi

  check_lightrag
}

check_lightrag() {
  load_lightrag_env
  local postgres_port="${POSTGRES_PORT:-15432}"
  local ollama_port="${OLLAMA_PORT:-11434}"
  local api_port="${PORT:-9621}"

  if docker exec stock-lightrag-postgres pg_isready >/dev/null 2>&1; then
    record_result "lightrag postgres" "OK" "pg_isready in stock-lightrag-postgres"
  else
    record_result "lightrag postgres" "FAIL" "pg_isready failed; host port ${postgres_port}"
  fi

  if ! docker ps --format '{{.Names}}' | grep -qx 'stock-lightrag-ollama'; then
    record_result "lightrag ollama" "SKIP" "optional ollama profile is not running"
  elif curl_health "http://127.0.0.1:${ollama_port}/api/tags"; then
    record_result "lightrag ollama" "OK" "http://127.0.0.1:${ollama_port}/api/tags"
  else
    record_result "lightrag ollama" "FAIL" "ollama API failed on port ${ollama_port}"
  fi

  if curl_health "http://127.0.0.1:${api_port}/health"; then
    record_result "lightrag api" "OK" "http://127.0.0.1:${api_port}/health"
  else
    record_result "lightrag api" "WARN" "API server not healthy; start with deploy/lightrag/start-server.sh"
  fi
}

down_lightrag() {
  if docker_compose --env-file deploy/lightrag/server.env -f deploy/lightrag/docker-compose.yml down >/dev/null 2>&1; then
    record_result "lightrag down" "OK" "compose stack stopped"
  else
    record_result "lightrag down" "SKIP" "compose stack not stopped; check docker manually"
  fi
}

pull_component() {
  local component="$1"
  case "${component}" in
    clickhouse)
      if run_with_retries "pull clickhouse" docker pull clickhouse/clickhouse-server; then
        record_result "clickhouse pull" "OK" "image available"
      else
        record_result "clickhouse pull" "FAIL" "$(docker_pull_failure_hint "${RUN_OUTPUT}")"
      fi
      ;;
    searxng)
      if run_with_retries "pull searxng" docker pull docker.io/searxng/searxng:latest; then
        record_result "searxng pull" "OK" "image available"
      else
        record_result "searxng pull" "FAIL" "$(docker_pull_failure_hint "${RUN_OUTPUT}")"
      fi
      ;;
    lightrag)
      ensure_lightrag_env || return
      if (( WITH_OLLAMA == 1 )); then
        if run_with_retries "pull lightrag compose images" docker_compose --profile ollama --env-file deploy/lightrag/server.env -f deploy/lightrag/docker-compose.yml pull; then
          record_result "lightrag pull" "OK" "compose images available"
        else
          record_result "lightrag pull" "FAIL" "$(docker_pull_failure_hint "${RUN_OUTPUT}")"
        fi
      elif run_with_retries "pull lightrag compose images" docker_compose --env-file deploy/lightrag/server.env -f deploy/lightrag/docker-compose.yml pull; then
        record_result "lightrag pull" "OK" "postgres image/build dependencies available; ollama profile skipped"
      else
        record_result "lightrag pull" "FAIL" "$(docker_pull_failure_hint "${RUN_OUTPUT}")"
      fi
      ;;
  esac
}

short_error() {
  local text="$1"
  text="$(printf '%s' "${text}" | tr '\n' ' ' | sed -E 's/[[:space:]]+/ /g')"
  printf '%s' "${text:0:220}"
}

docker_pull_failure_hint() {
  local output="$1"
  local short
  short="$(short_error "${output}")"
  if printf '%s' "${output}" | grep -Eqi 'Client.Timeout|request canceled|registry-1.docker.io|TLS handshake timeout|connection timed out'; then
    printf 'Docker Hub/network timeout: %s; configure Docker proxy/mirror or rerun with --retries N' "${short}"
  else
    printf '%s' "${short}"
  fi
}

lightrag_compose_failure_hint() {
  local output="$1"
  if printf '%s' "${output}" | grep -Eqi 'Client.Timeout|request canceled|registry-1.docker.io|TLS handshake timeout|connection timed out'; then
    docker_pull_failure_hint "${output}"
    printf '; manual retry: docker compose --env-file deploy/lightrag/server.env -f deploy/lightrag/docker-compose.yml pull'
  else
    short_error "${output}"
  fi
}

run_for_selected_components() {
  local component
  for component in clickhouse searxng lightrag; do
    if ! contains_component "${component}"; then
      continue
    fi

    case "${ACTION}:${component}" in
      up:clickhouse) deploy_clickhouse ;;
      up:searxng) deploy_searxng ;;
      up:lightrag) deploy_lightrag ;;
      check:clickhouse) check_clickhouse ;;
      check:searxng) check_searxng ;;
      check:lightrag) ensure_lightrag_env >/dev/null; check_lightrag ;;
      down:clickhouse) down_clickhouse ;;
      down:searxng) down_searxng ;;
      down:lightrag) down_lightrag ;;
      restart:clickhouse) down_clickhouse; deploy_clickhouse ;;
      restart:searxng) down_searxng; deploy_searxng ;;
      restart:lightrag) down_lightrag; deploy_lightrag ;;
      pull:*) pull_component "${component}" ;;
    esac
  done
}

parse_args "$@"

if ! preflight; then
  print_summary
  exit 1
fi

run_for_selected_components
print_summary

if (( HAS_FAILURE == 1 )); then
  exit 1
fi
exit 0
