from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    itad_api_key: str = os.getenv("ITAD_API_KEY", "")
    itad_base_url: str = os.getenv("ITAD_BASE_URL", "https://api.isthereanydeal.com")
    steamdb_api_base: str = os.getenv("STEAMDB_API_BASE", "")
    cheapshark_base_url: str = os.getenv("CHEAPSHARK_BASE_URL", "https://www.cheapshark.com/api/1.0")

    check_local_hour: int = int(os.getenv("CHECK_LOCAL_HOUR", "9"))
    check_local_minute: int = int(os.getenv("CHECK_LOCAL_MINUTE", "0"))

    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_use_tls: bool = _as_bool(os.getenv("SMTP_USE_TLS"), True)
    email_from: str = os.getenv("EMAIL_FROM", "")

    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    discord_webhook_url: str = os.getenv("DISCORD_WEBHOOK_URL", "")
    wecom_webhook_url: str = os.getenv("WECOM_WEBHOOK_URL", "")
    lark_webhook_url: str = os.getenv("LARK_WEBHOOK_URL", "")
    dingtalk_webhook_url: str = os.getenv("DINGTALK_WEBHOOK_URL", "")
    qq_onebot_url: str = os.getenv("QQ_ONEBOT_URL", "")
    qq_onebot_access_token: str = os.getenv("QQ_ONEBOT_ACCESS_TOKEN", "")

    sqlite_path: str = os.getenv("SQLITE_PATH", "valveye.db")


settings = Settings()
