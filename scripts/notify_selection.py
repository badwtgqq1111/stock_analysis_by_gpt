#!/usr/bin/env python
"""把当日选股结果推送到多个渠道（飞书 / 企业微信群机器人 / 钉钉 / 通用 webhook）。

    uv run python scripts/notify_selection.py [--trade-date YYYY-MM-DD] [--replay-dir DIR]
                                              [--channels email] [--dry-run] [--print-payload]

渠道与凭据全部走环境变量（未配置的渠道自动跳过）：

    MAIL_TO             邮件收件人（默认 badwtg2222@qq.com）
    SMTP_HOST/PORT/TLS  SMTP 服务（默认 smtp.qq.com:465 ssl）
    SMTP_USER            发信账号（默认与收件人相同）
    SMTP_PASSWORD        SMTP 授权码（QQ 邮箱"授权码"，不是登录密码）
    FEISHU_USER_ID      飞书 open_id（默认当前账号），身份用 lark-cli --as bot 发送
    WECOM_WEBHOOK_URL   企业微信群机器人 webhook（https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...）
    DINGTALK_WEBHOOK_URL 钉钉群机器人 webhook（可选，可加 DINGTALK_SECRET 做加签）
    GENERIC_WEBHOOK_URL 任意 HTTP 端点（POST JSON {"text": ...}）

关于"微信群"：个人微信没有官方群机器人接口。可自动化的等价物是**企业微信内部群的群机器人**
（获得 webhook 立即生效），企业微信外部客户群不支持机器人自动发消息（只能群发/人工确认）。
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_FEISHU_USER_ID = "ou_53cf03fd06b21154a774194516dc2e95"


def load_env_file(path: Path) -> None:
    """读取 config/notify.env（存在则加载，不覆盖已设置的环境变量）。

    这样手动运行与 launchd/systemd/cron 运行的行为一致——凭据只维护一份。
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def build_report(trade_date: str | None, replay_dir: str | None, top: int) -> tuple[str, Path]:
    cmd = [sys.executable, "scripts/build_feishu_report.py", "--top", str(top)]
    if trade_date:
        cmd += ["--trade-date", trade_date]
    if replay_dir:
        cmd += ["--replay-dir", replay_dir]
    built = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True)
    if built.returncode != 0:
        raise RuntimeError(f"report build failed: {built.stderr[-400:]}")
    resolved_date = trade_date or str(built.stdout.split("CN 选股 · ")[-1].split("\n")[0]).strip()
    directory = Path(replay_dir) if replay_dir else REPO / "output" / "results_cn"
    path = directory / f"feishu_{resolved_date}.md"
    if not path.is_file():
        raise RuntimeError(f"report not found: {path}")
    return path.read_text(encoding="utf-8"), path


def post_json(url: str, payload: dict, *, timeout: float = 10.0) -> tuple[bool, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
        return True, text[:300]
    except Exception as exc:                     # network / HTTP errors are reported, not raised
        return False, f"{type(exc).__name__}: {exc}"


def parse_cli_json(*streams: str) -> dict:
    """从 lark-cli 的输出里挑出**结果对象**。

    为什么不能直接 `json.loads(stdout)`（2026-10-12 踩的坑）：
      ① lark-cli 把结果写到 **stderr**（stdout 常常是空的）；
      ② stderr 里还混着"有新版本，跑 lark-cli update"的提示 JSON —— 直接解析会拿到那个提示，
         于是报错显示成 `None: "update": {…}`，看不出真正的原因（实际是 code=230013
         「Bot has NO availability to this user」）。
    做法：把所有 `{…}` 平衡片段都试一遍，优先返回带 `ok` 字段的那个。
    """
    import re as _re
    candidates = []
    for stream in streams:
        text = stream or ""
        try:
            candidates.append(json.loads(text))
        except json.JSONDecodeError:
            pass
        for match in _re.finditer(r"\{", text):
            start, depth = match.start(), 0
            for index in range(start, len(text)):
                if text[index] == "{":
                    depth += 1
                elif text[index] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            candidates.append(json.loads(text[start:index + 1]))
                        except json.JSONDecodeError:
                            pass
                        break
    for item in candidates:
        if isinstance(item, dict) and "ok" in item:
            return item
    return next((item for item in candidates if isinstance(item, dict)), {})


def send_feishu(content: str, *, identity: str, user_id: str, dry_run: bool) -> tuple[bool, str]:
    cmd = ["lark-cli", "im", "+messages-send", "--as", identity,
           "--user-id", user_id, "--markdown", content, "--format", "json"]
    if dry_run:
        return True, f"[dry-run] lark-cli im +messages-send --as {identity} --user-id {user_id}（{len(content)} 字）"
    sent = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True)
    payload = parse_cli_json(sent.stdout, sent.stderr)
    if payload.get("ok") is True:
        return True, f"message_id={(payload.get('data') or {}).get('message_id')}"
    error = payload.get("error") or {}
    code = error.get("code")
    message = error.get("message") or sent.stderr[-200:]
    hints = {
        230013: "机器人在该用户不可用（开放平台 → 应用 → 可用范围/发布状态；或改用群 chat_id / webhook 渠道）",
        99991663: "机器人不在该会话里（先把机器人拉进群，或用 chat_id 发送）",
        99991672: "应用缺少 im:message 权限（补 scope 后重新发布应用）",
    }
    hint = f"  ← {hints[code]}" if code in hints else ""
    return False, f"{error.get('subtype') or error.get('type')}(code={code}): {message}{hint}"


def send_wecom(content: str, url: str, *, dry_run: bool) -> tuple[bool, str]:
    # 企业微信群机器人：markdown 消息（content 上限 4096 字节）
    payload = {"msgtype": "markdown", "markdown": {"content": content[:4000]}}
    if dry_run:
        return True, f"[dry-run] POST {url.split('key=')[0]}key=*** msgtype=markdown {len(payload['markdown']['content'])} 字"
    return post_json(url, payload)


def send_dingtalk(content: str, url: str, secret: str | None, *, dry_run: bool) -> tuple[bool, str]:
    target = url
    if secret:
        import base64
        import hashlib
        import hmac
        import time
        import urllib.parse

        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{secret}"
        digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        signature = urllib.parse.quote_plus(base64.b64encode(digest))
        target = f"{url}&timestamp={timestamp}&sign={signature}"
    payload = {"msgtype": "markdown", "markdown": {"title": "CN 选股", "text": content[:4000]}}
    if dry_run:
        return True, f"[dry-run] POST {target.split('access_token=')[0]}access_token=*** msgtype=markdown"
    return post_json(target, payload)


def send_generic(content: str, url: str, *, dry_run: bool) -> tuple[bool, str]:
    if dry_run:
        return True, f"[dry-run] POST {url} json={{'text': ...}}（{len(content)} 字）"
    return post_json(url, {"text": content})


def send_email(
    content: str,
    html: str,
    *,
    attachments: list[Path] | None = None,
    dry_run: bool,
) -> tuple[bool, str]:
    """SMTP 发信（默认 QQ 邮箱：smtp.qq.com:465 SSL，密码用"授权码"而不是登录密码）。

    环境变量：MAIL_TO / MAIL_FROM / SMTP_HOST / SMTP_PORT / SMTP_TLS(ssl|starttls|none)
             SMTP_USER / SMTP_PASSWORD（QQ 授权码）
    """
    import smtplib
    from email.message import EmailMessage

    to_addr = os.environ.get("MAIL_TO", "badwtg2222@qq.com")
    host = os.environ.get("SMTP_HOST", "smtp.qq.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    tls_mode = os.environ.get("SMTP_TLS", "ssl").lower()
    user = os.environ.get("SMTP_USER", to_addr)
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("MAIL_FROM", user)
    subject = os.environ.get("MAIL_SUBJECT", f"CN 选股结果 {os.environ.get('MAIL_DATE', '')}".strip())

    attachment_list = attachments or []
    if dry_run:
        plan = f"[dry-run] SMTP {host}:{port} ({tls_mode}) {sender} -> {to_addr}"
        if attachment_list:
            plan += f"；附件 {', '.join(path.name for path in attachment_list)}"
        if not password:
            plan += "；SMTP_PASSWORD（QQ 授权码）未配置，真实发送会失败"
        return True, plan
    if not password:
        return False, "SMTP_PASSWORD（QQ 邮箱授权码）未配置：QQ 邮箱 → 设置 → 账户 → 开启 SMTP 服务并生成授权码"

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = to_addr
    message.set_content(content)
    message.add_alternative(html, subtype="html")
    for attachment in attachment_list:
        mime_type, _ = mimetypes.guess_type(attachment.name)
        maintype, subtype = (mime_type or "application/octet-stream").split("/", 1)
        message.add_attachment(
            attachment.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=attachment.name,
        )
    try:
        if port == 465 and tls_mode == "ssl":
            with smtplib.SMTP_SSL(host, port, timeout=20) as server:
                server.login(user, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=20) as server:
                if tls_mode != "none":
                    server.starttls()
                server.login(user, password)
                server.send_message(message)
        return True, f"sent to {to_addr} via {host}:{port} ({tls_mode})"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", default=None)
    parser.add_argument("--replay-dir", default=None)
    # 默认渠道（2026-10-12 用户要求）：**只发 email**。飞书那条渠道当前不可用
    # （lark-cli code=230013「Bot has NO availability to this user」），继续默认带着它只会让每天多一条 FAIL。
    # 想恢复飞书：NOTIFY_CHANNELS=feishu,email（或 --channels feishu,email），并先在开放平台把机器人可用范围加上该用户。
    parser.add_argument("--channels", default=os.environ.get("NOTIFY_CHANNELS", "email"))
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--print-payload", action="store_true", help="打印将发送的内容后退出")
    parser.add_argument(
        "--attach",
        action="append",
        default=[],
        help="附加文件（相对仓库路径，可重复）",
    )
    parser.add_argument("--text", default=None, help="直接发送这段文本（用于失败告警等短消息），跳过报告生成")
    parser.add_argument("--subject", default=None, help="邮件主题（配合 --text 使用）")
    args = parser.parse_args()

    load_env_file(REPO / "config" / "notify.env")
    attachments: list[Path] = []
    for raw_path in args.attach:
        candidate = Path(raw_path)
        resolved = (REPO / candidate).resolve()
        if candidate.is_absolute() or not resolved.is_relative_to(REPO) or not resolved.is_file():
            raise ValueError(f"attachment must be a repository-relative file: {raw_path}")
        attachments.append(resolved)
    if args.text:
        content, path = args.text, REPO / "output" / "results_cn" / "_notify_text.md"
        if args.subject:
            os.environ["MAIL_SUBJECT"] = args.subject
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        (path.with_suffix(".html")).write_text(
            "<html><body><pre style='font-family:-apple-system,PingFang SC,sans-serif'>"
            + content.replace("&", "&amp;").replace("<", "&lt;") + "</pre></body></html>", encoding="utf-8")
    else:
        content, path = build_report(args.trade_date, args.replay_dir, args.top)
    print(f"report: {path} ({len(content)} chars)")
    if args.print_payload:
        print(content)
        return 0

    requested = [item.strip().lower() for item in args.channels.split(",") if item.strip()]
    results: list[tuple[str, bool, str]] = []      # (channel, ok, detail)
    skipped: list[tuple[str, str]] = []
    for channel in requested:
        if channel == "feishu":
            user_id = os.environ.get("FEISHU_USER_ID", DEFAULT_FEISHU_USER_ID)
            identity = os.environ.get("FEISHU_IDENTITY", "bot")
            results.append(("feishu", *send_feishu(content, identity=identity, user_id=user_id, dry_run=args.dry_run)))
        elif channel == "wecom":
            url = os.environ.get("WECOM_WEBHOOK_URL", "")
            if not url:
                skipped.append(("wecom", "WECOM_WEBHOOK_URL 未配置（企业微信群机器人 webhook）"))
            else:
                results.append(("wecom", *send_wecom(content, url, dry_run=args.dry_run)))
        elif channel == "dingtalk":
            url = os.environ.get("DINGTALK_WEBHOOK_URL", "")
            if not url:
                skipped.append(("dingtalk", "DINGTALK_WEBHOOK_URL 未配置"))
            else:
                results.append(("dingtalk", *send_dingtalk(content, url, os.environ.get("DINGTALK_SECRET"), dry_run=args.dry_run)))
        elif channel == "generic":
            url = os.environ.get("GENERIC_WEBHOOK_URL", "")
            if not url:
                skipped.append(("generic", "GENERIC_WEBHOOK_URL 未配置"))
            else:
                results.append(("generic", *send_generic(content, url, dry_run=args.dry_run)))
        elif channel == "email":
            html_path = path.with_suffix(".html")
            html = html_path.read_text(encoding="utf-8") if html_path.is_file() else ""
            os.environ.setdefault("MAIL_DATE", path.stem.split("_")[-1])
            results.append(("email", *send_email(content, html, attachments=attachments, dry_run=args.dry_run)))
        else:
            results.append((channel, False, "未知渠道"))

    succeeded = [channel for channel, ok, _ in results if ok]
    failed_channels = [channel for channel, ok, _ in results if not ok]
    for channel, ok, detail in results:
        note = ""
        if not ok and "lark-cli" in detail:
            # 2026-10-12：飞书的失败原因是 lark-cli 把"有新版本"的提示混进了返回体，
            # 这不是"没发出去"就是"要升级 CLI"——把它标出来，别让人以为是网络问题。
            note = "  ← 疑似 lark-cli 版本提示混入返回体（跑 `lark-cli update` 后重试）"
        print(f"{'ok  ' if ok else 'FAIL'} {channel}: {detail}{note}")
    for channel, detail in skipped:
        print(f"skip {channel}: {detail}")
    if not results and skipped:
        print("（所有渠道都未配置：只生成报告，未发送）")
    if not failed_channels:
        if results:
            print(f"OK: 全部渠道送达 {succeeded}")
        return 0
    if succeeded:
        print(f"PARTIAL: 已送达 {succeeded}；失败 {failed_channels}"
              "（至少一个渠道成功 ⇒ 视为通知成功，退出码 0；失败渠道见上）")
        return 0
    print(f"ALL-FAILED: 所有已配置渠道都失败：{failed_channels}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
