#!/usr/bin/env bash

set -euo pipefail

IMA_CONFIG_DIR="${HOME}/.config/ima"
CLIENT_ID_FILE="${IMA_CONFIG_DIR}/client_id"
API_KEY_FILE="${IMA_CONFIG_DIR}/api_key"

if [[ -z "${IMA_OPENAPI_CLIENTID:-}" && -f "${CLIENT_ID_FILE}" ]]; then
  export IMA_OPENAPI_CLIENTID="$(<"${CLIENT_ID_FILE}")"
fi

if [[ -z "${IMA_OPENAPI_APIKEY:-}" && -f "${API_KEY_FILE}" ]]; then
  export IMA_OPENAPI_APIKEY="$(<"${API_KEY_FILE}")"
fi

if [[ -z "${IMA_OPENAPI_CLIENTID:-}" || -z "${IMA_OPENAPI_APIKEY:-}" ]]; then
  echo "IMA credentials are not configured. Set ~/.config/ima/client_id and ~/.config/ima/api_key or export IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY." >&2
  exit 1
fi

export IMA_CLIENT_ID="${IMA_OPENAPI_CLIENTID}"
export IMA_API_KEY="${IMA_OPENAPI_APIKEY}"

ima_api() {
  local path="$1"
  local body="${2:-{}}"
  curl -sS -X POST "https://ima.qq.com/${path}" \
    -H "ima-openapi-clientid: ${IMA_CLIENT_ID}" \
    -H "ima-openapi-apikey: ${IMA_API_KEY}" \
    -H "Content-Type: application/json" \
    -d "${body}"
}
