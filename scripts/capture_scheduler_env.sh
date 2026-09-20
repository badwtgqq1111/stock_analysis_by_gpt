#!/usr/bin/env bash
# 把当前"交互式 shell 环境"（即 ~/.bashrc 里 export 的东西）固化成调度器可用的环境文件。
#
#   bash scripts/capture_scheduler_env.sh [--output config/scheduler_env.sh]
#
# 为什么要这么做：
#   launchd / systemd / cron 启动的是**非交互式、非登录** shell，bash 不会读取 ~/.bashrc；
#   而且 ~/.bashrc 开头通常有 `case $- in *i*) ;; *) return ;; esac`，即使手动 source 也会立刻返回。
#   所以调度任务必须显式加载一份"非交互式安全"的环境文件。
#
# 采集方式：用 `bash -ic env` 启动一个真正的交互式 shell（让 ~/.bashrc 完整执行），
# 再把其中的关键变量导出成 export 语句。文件权限 600，且已在 .gitignore 中。
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
OUT="config/scheduler_env.sh"
while [ $# -gt 0 ]; do
  case "$1" in
    --output) OUT="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# 交互式 shell 的完整环境（stderr 丢弃，避免 bash-it/主题输出污染）
captured="$(bash -ic 'env' 2>/dev/null)" || { echo "无法用 bash -i 采集环境" >&2; exit 1; }

# 需要固化到调度环境的变量：数据层凭据、模型/LLM key、网络代理、PATH 追加
KEEP_PATTERN='^(PATH|CLICKHOUSE_[A-Z_]+|TUSHARE_[A-Z_]*(TOKEN|KEY)|CN_INDUSTRY_RELAY[A-Z_]*|HF_TOKEN|DEEPSEEK_API_KEY|TAVILY_API_KEY|OPENAI_[A-Z_]+|ANTHROPIC_[A-Z_]+|HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|NO_PROXY|LANG|LC_ALL|TZ|PYTHONPATH|UV_[A-Z_]+)='

tmp="$(mktemp)"
{
  echo "# 由 scripts/capture_scheduler_env.sh 生成（$(date '+%Y-%m-%d %H:%M:%S')）"
  echo "# 供 launchd / systemd / cron 等非交互式调度使用；请勿提交到仓库（.gitignore 已包含）"
  echo "set -a"
  printf '%s\n' "$captured" | tr -d '\r' | grep -E "$KEEP_PATTERN" | while IFS= read -r line; do
    key="${line%%=*}"
    value="${line#*=}"
    # 单引号包裹并转义内部单引号，保证任意取值都能安全 source
    escaped="${value//\'/\'\\\'\'}"
    printf "export %s='%s'\n" "$key" "$escaped"
  done
  echo "set +a"
} > "$tmp"
mv "$tmp" "$OUT"
chmod 600 "$OUT"
echo "written: $OUT ($(grep -c '^export ' "$OUT") vars, mode 600)"
