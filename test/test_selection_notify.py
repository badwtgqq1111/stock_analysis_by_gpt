#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""选股通知渠道测试：邮件 MIME 结构、webhook 载荷、未配置渠道的跳过语义。"""

import importlib.util
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _load_notifier():
    spec = importlib.util.spec_from_file_location(
        "notify_selection_module", PROJECT_ROOT / "scripts" / "notify_selection.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["notify_selection_module"] = module
    spec.loader.exec_module(module)
    return module


class _Recorder:
    """替身 SMTP 客户端：记录 login / send_message。"""

    instances: list = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.logged_in = None
        self.message = None
        _Recorder.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, message):
        self.message = message


@pytest.fixture(autouse=True)
def _clear_recorder():
    _Recorder.instances.clear()
    yield
    _Recorder.instances.clear()


def test_email_channel_builds_multipart_mail(monkeypatch):
    import smtplib

    module = _load_notifier()
    monkeypatch.setattr(smtplib, "SMTP_SSL", _Recorder)
    monkeypatch.setenv("MAIL_TO", "badwtg2222@qq.com")
    monkeypatch.setenv("SMTP_HOST", "smtp.qq.com")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("SMTP_TLS", "ssl")
    monkeypatch.setenv("SMTP_USER", "badwtg2222@qq.com")
    monkeypatch.setenv("SMTP_PASSWORD", "unit-test-code")
    monkeypatch.setenv("MAIL_SUBJECT", "CN 选股结果 2026-09-16")

    ok, detail = module.send_email("**CN 选股 · 2026-09-16**\nPK 12.11%",
                                   "<html><body><table><tr><td>股票</td></tr></table></body></html>",
                                   dry_run=False)

    assert ok is True and "badwtg2222@qq.com" in detail
    recorder = _Recorder.instances[-1]
    assert recorder.host == "smtp.qq.com" and recorder.port == 465
    assert recorder.logged_in == ("badwtg2222@qq.com", "unit-test-code")
    message = recorder.message
    assert message["To"] == "badwtg2222@qq.com"
    assert message["Subject"] == "CN 选股结果 2026-09-16"
    assert message.is_multipart()
    body = "".join(part.get_payload(decode=True).decode("utf-8", errors="replace")
                   for part in message.walk() if part.get_content_maintype() == "text")
    assert "CN 选股" in body and "PK 12.11%" in body and "<table" in body


def test_email_channel_requires_an_authorisation_code(monkeypatch):
    module = _load_notifier()
    monkeypatch.setenv("SMTP_PASSWORD", "")
    ok, detail = module.send_email("body", "<html></html>", dry_run=False)
    assert ok is False and "授权码" in detail


def test_email_dry_run_reports_the_plan(monkeypatch):
    module = _load_notifier()
    monkeypatch.setenv("MAIL_TO", "badwtg2222@qq.com")
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    ok, detail = module.send_email("body", "<html></html>", dry_run=True)
    assert ok is True and "smtp.qq.com:465" in detail and "SMTP_PASSWORD" in detail


def test_wecom_payload_shape(monkeypatch):
    module = _load_notifier()
    captured = {}

    def fake_post(url, payload, **kwargs):
        captured["url"], captured["payload"] = url, payload
        return True, '{"errcode":0}'

    monkeypatch.setattr(module, "post_json", fake_post)
    ok, _ = module.send_wecom("**CN 选股**", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k", dry_run=False)

    assert ok is True
    assert captured["payload"]["msgtype"] == "markdown"
    assert captured["payload"]["markdown"]["content"].startswith("**CN 选股**")
    assert "key=k" in captured["url"]


def test_unconfigured_channels_are_skipped_not_failed(monkeypatch, capsys):
    module = _load_notifier()
    monkeypatch.setattr(module, "build_report", lambda *a, **k: ("content", Path("/tmp/report.md")))
    for name in ("WECOM_WEBHOOK_URL", "DINGTALK_WEBHOOK_URL", "GENERIC_WEBHOOK_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys, "argv", ["notify_selection.py", "--channels", "wecom,dingtalk,generic"])

    assert module.main() == 0
    output = capsys.readouterr().out
    assert output.count("skip") == 3 and "FAIL" not in output
