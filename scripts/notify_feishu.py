#!/usr/bin/env python
"""兼容入口：转发到 scripts/notify_selection.py（只发飞书）。"""
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
cmd = [sys.executable, "scripts/notify_selection.py", "--channels", "feishu"] + sys.argv[1:]
sys.exit(subprocess.run(cmd, cwd=str(REPO)).returncode)
