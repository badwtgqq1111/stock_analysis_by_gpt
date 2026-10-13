"""每日生产脚本的**解析安全**（2026-10-09 事故回归）。

事故：19:30 与 20:30 两次自动运行都是"启动后静默退出"，主阶段一步没跑。
原因不是数据源挂了，而是脚本里 5 处 `$code（中文）`/`$_step（中文）` ——
字符编码/地区设置（launchd 环境下没有 LANG/LC_ALL）让变量名被多字节字符污染，
`set -u` 于是直接终止脚本；而这几行**只在"数据步骤失败"的分支**里，
所以平时看不出来，一旦数据源抖动（当晚 relay 502/503）就会把整个每日生产打死。

本文件盯四件事：
  1. 脚本必须是合法 UTF-8（坏字节会让 bash 的变量名解析出错）；
  2. 不允许出现 `$var` 紧跟非 ASCII 字符（必须写 `${var}`）；
  3. `LC_ALL=C bash -n` 语法检查必须通过（launchd 的默认地区）；
  4. "数据步骤失败"分支必须继续执行（用抽出的小 harness 在两种地区下各跑一次）。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/run_daily_production.sh"


def _function_body(source: str, name: str) -> str:
    """取出一个 shell 函数的**完整**定义（从 `name() {` 到第一个顶格的 `}`）。

    事故（2026-10-13，我自己踩的）：这段原来写的是
        `source[idx("run_data_step() {"): idx('if [ "${DAILY_DATA_REFRESH:-1}" = "1" ]; then')]`
    —— 用**下一个 if 的文本**当结束标记。P2.56 在"函数"与那个 if 之间插了「基准指数同步」和
    「当日数据就绪闸门」两段，切片于是把闸门也吞进 harness：
      * 14:00 之后跑测试，闸门会**阻塞到 45 分钟**（等当日中继数据）→ 预检直接卡住；
      * 若闸门判定未就绪，harness 会走 `notify_alert`（harness 里没定义）→ 退出非 0 → 测试失败
        ⇒ **18:30 的每日生产会在预检阶段被自己的测试挡住**（比卡住更糟：它看起来像"代码坏了"）。
    现在按**大括号匹配**取函数体，并在下面断言它不包含其它段落的关键词。
    """
    start = source.index(f"{name}() {{")
    end = source.index("\n}\n", start) + 3
    body = source[start:end]
    for banned in ("daily_ready_gate", "DAILY_BENCHMARK", "DAILY_DATA_REFRESH", "run_data_step benchmark"):
        assert banned not in body, f"函数体切片吞进了 {banned}（切片边界又漂了）"
    return body


def test_function_body_extraction_is_scoped():
    """`_function_body` 只返回这一个函数（防止将来再漂）。"""
    body = _function_body(SCRIPT.read_text(encoding="utf-8"), "run_data_step")
    assert body.startswith("run_data_step() {") and body.rstrip().endswith("}")
    assert body.count("\n}") == 1, f"函数体切片吞进了别的段落：\n{body[-300:]}"


def test_script_is_valid_utf8():
    raw = SCRIPT.read_bytes()
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:                              # pragma: no cover
        pytest.fail(f"脚本含非法 UTF-8 字节（会让 bash 变量名解析出错）：{exc}")


def test_no_variable_immediately_followed_by_multibyte():
    text = SCRIPT.read_text(encoding="utf-8")
    offenders = []
    for number, line in enumerate(text.splitlines(), start=1):
        code = line.split("#", 1)[0]                               # 注释里的示例不算
        if re.search(r"\$[A-Za-z_][A-Za-z0-9_]*(?=[^\x00-\x7F])", code):
            offenders.append(f"{number}: {line.strip()[:90]}")
    assert not offenders, "`$var` 必须写成 `${var}`（紧跟中文会被解析成未定义变量）：\n" + "\n".join(offenders)


def test_syntax_ok_under_c_locale():
    env = dict(os.environ, LC_ALL="C", LANG="C")
    proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("locale", ["C", "en_US.UTF-8"])
def test_data_step_failure_does_not_abort(tmp_path, locale):
    """数据步骤失败必须"只记日志"，不能终止整个每日生产（事故的核心）。"""
    source = SCRIPT.read_text(encoding="utf-8")
    body = _function_body(source, "run_data_step")
    harness = tmp_path / "harness.sh"
    harness.write_text(textwrap.dedent(f'''
        set -uo pipefail
        LOG_DIR=$(mktemp -d)
        DRY_RUN=0
        log() {{ printf '%s\\n' "$*"; }}
        {body}
        run_data_step broken_step bash -c 'echo "模拟数据源 502"; exit 3'
        echo "CONTINUED_AFTER_FAILURE"
    '''), encoding="utf-8")
    env = dict(os.environ, LC_ALL=locale, LANG=locale)
    proc = subprocess.run(["bash", str(harness)], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    assert "CONTINUED_AFTER_FAILURE" in proc.stdout


def test_script_exports_utf8_locale():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "export LC_ALL=" in text and "export LANG=" in text, "launchd 环境没有 locale，脚本要自己兜底"
