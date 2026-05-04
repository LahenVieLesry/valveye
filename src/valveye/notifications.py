from __future__ import annotations

import asyncio
import json
import smtplib
import ssl
from email.message import EmailMessage

import aiohttp
import certifi

from valveye.config import settings


class NotificationError(RuntimeError):
    pass


class Notifier:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            connector = aiohttp.TCPConnector(ssl=ssl_ctx)
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=12), connector=connector,
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def send(self, channel: dict, message: str) -> None:
        channel_type = channel.get("type")
        if channel_type == "email":
            await self._send_email(channel=channel, message=message)
            return
        if channel_type == "telegram":
            await self._send_telegram(channel=channel, message=message)
            return
        if channel_type == "discord":
            await self._send_discord(message=message)
            return
        if channel_type in {"wecom", "lark", "dingtalk"}:
            await self._send_webhook(channel_type=channel_type, message=message)
            return
        if channel_type == "qq":
            await self._send_qq(channel=channel, message=message)
            return
        raise NotificationError(f"unsupported channel type: {channel_type}")

    async def _send_email(self, channel: dict, message: str) -> None:
        to_addr = channel.get("to")
        if not to_addr:
            raise NotificationError("email channel requires 'to'")
        if not settings.smtp_host or not settings.email_from:
            raise NotificationError("smtp not configured")

        msg = EmailMessage()
        msg["Subject"] = "[Valveye] 游戏价格提醒"
        msg["From"] = settings.email_from
        msg["To"] = to_addr
        msg.set_content(message)

        use_ssl = settings.smtp_use_ssl or (settings.smtp_port == 465 and not settings.smtp_use_tls)
        smtp_factory = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP

        def _do_send() -> None:
            with smtp_factory(settings.smtp_host, settings.smtp_port, timeout=60) as smtp:
                smtp.ehlo()
                if not use_ssl and settings.smtp_use_tls:
                    smtp.starttls()
                    smtp.ehlo()
                if settings.smtp_user:
                    smtp.login(settings.smtp_user, settings.smtp_password)
                smtp.send_message(msg)

        await asyncio.to_thread(_do_send)

    async def _send_telegram(self, channel: dict, message: str) -> None:
        chat_id = channel.get("chat_id")
        if not chat_id:
            raise NotificationError("telegram channel requires 'chat_id'")
        if not settings.telegram_bot_token:
            raise NotificationError("telegram bot token not configured")

        session = await self._get_session()
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message}
        async with session.post(url, json=payload) as resp:
            if resp.status >= 400:
                raise NotificationError(f"telegram failed: {resp.status}")

    async def _send_discord(self, message: str) -> None:
        if not settings.discord_webhook_url:
            raise NotificationError("discord webhook not configured")
        session = await self._get_session()
        async with session.post(settings.discord_webhook_url, json={"content": message}) as resp:
            if resp.status >= 400:
                raise NotificationError(f"discord failed: {resp.status}")

    async def _send_webhook(self, channel_type: str, message: str) -> None:
        url = {
            "wecom": settings.wecom_webhook_url,
            "lark": settings.lark_webhook_url,
            "dingtalk": settings.dingtalk_webhook_url,
        }[channel_type]
        if not url:
            raise NotificationError(f"{channel_type} webhook not configured")

        payload = {
            "msgtype": "text",
            "text": {"content": message},
        }
        if channel_type == "lark":
            payload = {"msg_type": "text", "content": {"text": message}}

        session = await self._get_session()
        async with session.post(url, json=payload) as resp:
            if resp.status >= 400:
                raise NotificationError(f"{channel_type} failed: {resp.status}")

    async def _send_qq(self, channel: dict, message: str) -> None:
        if not settings.qq_onebot_url:
            raise NotificationError("qq onebot url not configured")
        qq_id = channel.get("qq_id")
        if not qq_id:
            raise NotificationError("qq channel requires 'qq_id'")

        headers = {"Content-Type": "application/json"}
        if settings.qq_onebot_access_token:
            headers["Authorization"] = f"Bearer {settings.qq_onebot_access_token}"

        session = await self._get_session()
        payload = {"user_id": int(qq_id), "message": message}
        async with session.post(
            f"{settings.qq_onebot_url.rstrip('/')}/send_private_msg",
            data=json.dumps(payload),
            headers=headers,
        ) as resp:
            if resp.status >= 400:
                raise NotificationError(f"qq failed: {resp.status}")
