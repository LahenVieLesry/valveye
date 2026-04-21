from __future__ import annotations

import argparse
import os
import smtplib
import sys
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="读取 .env 中的 SMTP 配置，发送一封测试邮件以验证服务器是否可用。"
    )
    parser.add_argument(
        "--to",
        # default=os.getenv("SMTP_TEST_TO") or os.getenv("SMTP_USER") or os.getenv("EMAIL_FROM"),
        default='2130588669@qq.com',
        help="测试邮件接收地址；默认依次尝试 SMTP_TEST_TO、SMTP_USER、EMAIL_FROM",
    )
    parser.add_argument(
        "--subject",
        default="[Valveye] SMTP 测试邮件",
        help="邮件主题",
    )
    parser.add_argument(
        "--message",
        default="这是一封由 test_smtp.py 发送的 SMTP 连通性测试邮件。",
        help="邮件正文",
    )
    parser.add_argument(
        "--force-ssl",
        action="store_true",
        help="强制使用 SMTP_SSL（适合 465 端口）",
    )
    parser.add_argument(
        "--force-starttls",
        action="store_true",
        help="强制使用 STARTTLS（适合 587 端口）",
    )
    return parser


def _connect_smtp(host: str, port: int, use_tls: bool, force_ssl: bool, force_starttls: bool):
    if force_ssl and force_starttls:
        raise ValueError("--force-ssl 和 --force-starttls 不能同时使用")

    if force_ssl or (not force_starttls and port == 465 and not use_tls):
        return smtplib.SMTP_SSL(host, port, timeout=60), "SMTP_SSL"

    return smtplib.SMTP(host, port, timeout=60), "SMTP"


def main() -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()

    host = os.getenv("SMTP_HOST", "").strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    use_tls = _as_bool(os.getenv("SMTP_USE_TLS"), True)
    email_from = os.getenv("EMAIL_FROM", "").strip() or user
    use_ssl = _as_bool(os.getenv("SMTP_USE_SSL"), False)

    if not host:
        print("[ERROR] SMTP_HOST 为空，无法连接。", file=sys.stderr)
        return 2
    if not email_from:
        print("[ERROR] EMAIL_FROM 或 SMTP_USER 为空，无法设置发件人。", file=sys.stderr)
        return 2
    if not args.to:
        print("[ERROR] 未提供收件人地址，请使用 --to 或设置 SMTP_TEST_TO。", file=sys.stderr)
        return 2

    message = EmailMessage()
    message["Subject"] = args.subject
    message["From"] = email_from
    message["To"] = args.to
    message["Date"] = datetime.now().astimezone().strftime("%a, %d %b %Y %H:%M:%S %z")
    message.set_content(
        f"{args.message}\n\n"
        f"时间: {datetime.now().isoformat(timespec='seconds')}\n"
        f"SMTP_HOST: {host}\n"
        f"SMTP_PORT: {port}\n"
        f"SMTP_USER: {'已设置' if user else '未设置'}\n"
        f"SMTP_USE_TLS: {use_tls}\n"
        f"SMTP_USE_SSL: {use_ssl}\n"
    )

    try:
        smtp_conn, mode = _connect_smtp(
            host=host,
            port=port,
            use_tls=use_tls,
            force_ssl=args.force_ssl or use_ssl,
            force_starttls=args.force_starttls,
        )
        with smtp_conn as smtp:
            smtp.ehlo()
            if mode == "SMTP" and use_tls:
                smtp.starttls()
                smtp.ehlo()
            if user:
                smtp.login(user, password)
            smtp.send_message(message)

        print("[OK] SMTP 测试邮件已发送成功。")
        print(f"      连接模式: {mode}")
        print(f"      发件人: {email_from}")
        print(f"      收件人: {args.to}")
        return 0
    except Exception as exc:  # pragma: no cover - 运行时网络/认证错误需要直接展示
        print("[ERROR] SMTP 测试失败。", file=sys.stderr)
        print(f"        类型: {type(exc).__name__}", file=sys.stderr)
        print(f"        详情: {exc}", file=sys.stderr)
        print("        提示: 若你的 SMTP 端口是 465，通常需要 SMTP_SSL；若是 587，通常需要 STARTTLS。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())