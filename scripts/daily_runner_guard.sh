#!/usr/bin/env bash
# 每日生产的运行期护栏（2026-10-12，审计后新增）。三种用法：
#
#   1) 被 run_daily_production.sh **source**（推荐）：
#        . "$ROOT/scripts/daily_runner_guard.sh"
#        runner_guard_boot "$0" "$LOG_DIR" "$@"      # 自身快照 + exec（免疫"改运行中的脚本"）+ 代码锚点
#        ... 之后用 run_data_step 时可选 runner_filter_progress
#   2) 手工检查：bash scripts/daily_runner_guard.sh --check      # 打印当前是否在跑 / 锁 / 锚点
#   3) 接入：bash scripts/daily_runner_guard.sh --install        # 只在**没有运行中的每日任务**时改脚本（幂等）
#
# 为什么需要（审计发现的问题）：
#   * **改运行中的 bash 脚本是不安全的**：bash 按字节偏移续读文件，边跑边改可能读到半行 → 直接跑挂。
#     今天（2026-10-12 19:30 起跑的那次）我在运行期间改过它；机制上必须防住：启动时把自己**快照**一份，
#     之后 exec 快照，源文件随你怎么改都不影响这一趟。
#   * **这次跑的是哪版代码**：run.json 里只有时间戳。现在把 `git HEAD` + 脏文件数 + 脚本 sha256 写进
#     `$LOG_DIR/code_anchor.json`，并在 run.log 留一行。
set -uo pipefail

guard_root() { cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd; }

runner_guard_sha8() { shasum -a 256 "$1" 2>/dev/null | cut -c1-8; }

runner_guard_boot() {
  # 用法：runner_guard_boot <脚本路径> <日志目录> [原参数...]
  # 参数容错：被 source 后由调用方传；递归进快照时可能没参数（`set -u` 下不许炸）
  local self="${1:-}"; local log_dir="${2:-}"; shift 2 || true
  if [ -z "$self" ] || [ -z "$log_dir" ]; then
    echo "runner_guard_boot: 需要 <脚本路径> <日志目录>" >&2
    return 1
  fi
  # 快照重入时**立刻返回**：锚点与快照只由外层那一次写。重入时 $0 指向 $LOG_DIR/runner.<sha>.sh，
  # 按它推根目录会推成日志目录（里面没有 .git），再写一次锚点就把外层写好的 git_head/dirty_files
  # 覆盖成 unknown（2026-10-14 一并修）。
  # 判据必须是"**当前这个脚本就是那次快照**"（$self == RUNNER_SNAPSHOT_PATH），不能只看
  # RUNNER_SNAPSHOT=1：那个变量随环境继承下去了 —— 预检（跑在快照进程里）拉起的任何脚本都会
  # 带着它，被误判成"我自己是快照"就会连锚点都不写（实测 test_daily_automation_audit.py::
  # test_runner_guard_snapshots_and_anchors 就是这么红的）。
  if [ "${RUNNER_SNAPSHOT:-0}" = "1" ] && [ -n "${RUNNER_SNAPSHOT_PATH:-}" ]; then
    local self_abs
    self_abs="$(cd "$(dirname "$self")" 2>/dev/null && pwd)/$(basename "$self")"
    if [ "$self_abs" = "$RUNNER_SNAPSHOT_PATH" ]; then
      return 0
    fi
  fi
  # 根目录：优先用源脚本路径（外层 export 的 RUNNER_SOURCE_PATH），否则按传入的脚本路径推。
  local root
  if [ -n "${RUNNER_SOURCE_PATH:-}" ] && [ -f "${RUNNER_SOURCE_PATH:-}" ]; then
    root="$(cd "$(dirname "$RUNNER_SOURCE_PATH")/.." && pwd)"
  else
    root="$(cd "$(dirname "$self")/.." && pwd)"
  fi
  mkdir -p "$log_dir"
  local digest; digest="$(runner_guard_sha8 "$self")"
  local snapshot="$log_dir/runner.${digest}.sh"

  # ① 代码锚点（**无条件**写：git 信息拿不到就填 unknown —— 锚点本身不能因此缺席）
  local head="unknown" dirty="unknown"
  if command -v git >/dev/null 2>&1 && [ -d "$root/.git" ]; then
    head="$(git -C "$root" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    dirty="$(git -C "$root" status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
  fi
  printf '{"git_head":"%s","dirty_files":"%s","runner_sha256":"%s","script":"%s","recorded_at":"%s"}\n' \
    "$head" "$dirty" "$(shasum -a 256 "$self" | cut -d' ' -f1)" \
    "$(basename "$self")" "$(date '+%Y-%m-%dT%H:%M:%S%z')" > "$log_dir/code_anchor.json"
  printf '%s code-anchor: head=%s dirty_files=%s runner_sha256=%s…\n' \
    "$(date '+%Y-%m-%d %H:%M:%S')" "$head" "$dirty" "$digest" >> "$log_dir/run.log"

  # ② 自身快照 + exec（幂等：已经在快照里就不再递归 —— 见函数开头的早返回）
  cp "$self" "$snapshot"
  export RUNNER_SNAPSHOT=1 RUNNER_SNAPSHOT_PATH="$snapshot" RUNNER_SOURCE_PATH="$self"
  printf '%s runner-guard: 已快照自身 → %s（源文件之后被编辑也不影响本次运行）\n' \
    "$(date '+%Y-%m-%d %H:%M:%S')" "$(basename "$snapshot")" >> "$log_dir/run.log"
  # 把**全部**参数原样传给快照：快照里的那行 `runner_guard_boot "$0" "$LOG_DIR" "$@"` 与源脚本同形，
  # 只传 `"${@:2}"` 会让快照里的 `$LOG_DIR` 落空（实测：`$1: unbound variable`，set -u 直接炸）。
  exec bash "$snapshot" "$@"
}

runner_filter_progress() {
  # 把 tqdm 进度行（含 \r 与 `%|`）从 stdin 过滤掉再写 stdout：
  # 实测单个阶段（adj_factor）能写 1.6MB 全是进度条，run.log 读起来全是噪声。
  # 进度行的形状：`… 94%|█████████▍| 4900/5224 [13:44<01:08, 4.76stock/s]`
  # （条是 Unicode 方块，不是 ASCII 的 `=`，所以不能只匹配 `[=>. ]` —— 实测第一版没滤掉）。
  tr '\r' '\n' | grep -v -E '%\|.*\|[^|]*\[[0-9]+:[0-9]+<[0-9]+:[0-9]+' || true
}

runner_guard_check() {
  local root; root="$(guard_root)"
  echo "== 运行中的每日任务 =="
  pgrep -fl "run_daily_production.sh" || echo "  （没有）"
  echo "== 锁 =="
  ls -la "$root/output/pipeline_reports/daily/.lock" 2>/dev/null || echo "  （没有锁文件）"
  echo "== 代码锚点（最近一次运行）=="
  for f in $(ls -t "$root/output/pipeline_reports/daily"/*/code_anchor.json 2>/dev/null | head -1); do cat "$f"; done
  echo "== 源脚本与它最近的快照 =="
  shasum -a 256 "$root/scripts/run_daily_production.sh" | cut -c1-16
  ls -t "$root/output/pipeline_reports/daily"/*/runner.*.sh 2>/dev/null | head -3
}

runner_guard_install() {
  # 幂等接入：把 source + boot 两行插到 run_daily_production.sh 的固定位置。
  # **只在没有运行中的每日任务时才改**（否则就是在改一个正在被 bash 读取的文件）。
  local root; root="$(guard_root)"
  local target="$root/scripts/run_daily_production.sh"
  if [ "${GUARD_FORCE_INSTALL:-0}" != "1" ] && pgrep -f "run_daily_production.sh" >/dev/null 2>&1; then
    echo "[guard] 检测到每日任务正在运行 → 拒绝改脚本（改运行中的 bash 脚本不安全）。"
    echo "[guard] 等这次 done 之后再跑：bash scripts/daily_runner_guard.sh --install"
    return 2
  fi
  if grep -q "runner_guard_boot" "$target"; then
    echo "[guard] 已接入（幂等）"
    return 0
  fi
  if [ "${GUARD_FORCE_INSTALL:-0}" != "1" ] && pgrep -f "run_daily_production.sh" >/dev/null 2>&1; then
    echo "[guard] （二次确认）仍在跑 → 拒绝"
    return 2
  fi
  python3 - "$target" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
text = path.read_text(encoding="utf-8")

guard_anchor = 'log() { printf'
assert guard_anchor in text, "找不到 log() 定义位置"
guard_insert = ('# ── 运行期护栏（2026-10-12）：自身快照 + 代码锚点 ──\n'
                '# 放在 log() 之前：它自己会往 $LOG_DIR/run.log 追加两行。\n'
                '. "$ROOT/scripts/daily_runner_guard.sh"\n'
                'runner_guard_boot "$0" "$LOG_DIR" "$@"\n\n')
if 'runner_guard_boot' not in text:
    text = text.replace(guard_anchor, guard_insert + guard_anchor, 1)

gate_anchor = 'if [ "${DAILY_DATA_REFRESH:-1}" = "1" ]; then'
assert gate_anchor in text, "找不到数据面入口"
gate_insert = ('# ── 当日数据就绪闸门（P2.55）：18:00 起跑时先确认"今天的日线到位" ──\n'
               '# 否则下游会安静地按**昨天**取数（连 web-freshness 都会全绿）。未就绪 ⇒ 中止本次，\n'
               '# 由 20:00 / 21:30 的重试触发接管；失败会走 notify_alert 发邮件。\n'
               'if [ "${DAILY_READY_GATE:-1}" = "1" ] && [ "$DRY_RUN" -eq 0 ]; then\n'
               '  if ! uv run python scripts/daily_ready_gate.py --print >> "$LOG_DIR/ready_gate.log" 2>&1; then\n'
               '    log "ready-gate FAILED：当日日线未就绪（见 $LOG_DIR/ready_gate.log）——本次中止"\n'
               '    notify_alert "CN 自动化未就绪 ${TRADE_DATE}" "$(tail -3 "$LOG_DIR/ready_gate.log")"\n'
               '    exit 1\n'
               '  fi\n'
               'fi\n\n')
if 'daily_ready_gate.py' not in text:
    text = text.replace(gate_anchor, gate_insert + gate_anchor, 1)

path.write_text(text, encoding="utf-8")
print("[guard] 已接入 run_daily_production.sh（runner_guard_boot + daily_ready_gate）")
PY
}

case "${1:-}" in
  --check) runner_guard_check ;;
  --install) runner_guard_install ;;
  ""|--*) : ;;   # 被 source 时静默；带其它参数也静默，避免误报
esac
