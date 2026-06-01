from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv(override=True)


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    openai_user_agent: str = os.getenv("OPENAI_USER_AGENT", "")

    itad_api_key: str = os.getenv("ITAD_API_KEY", "")
    itad_base_url: str = os.getenv("ITAD_BASE_URL", "https://api.isthereanydeal.com")
    steamdb_api_base: str = os.getenv("STEAMDB_API_BASE", "")
    cheapshark_base_url: str = os.getenv("CHEAPSHARK_BASE_URL", "https://www.cheapshark.com/api/1.0")
    steam_store_base_url: str = os.getenv("STEAM_STORE_BASE_URL", "https://store.steampowered.com")
    steam_web_api_base_url: str = os.getenv("STEAM_WEB_API_BASE_URL", "https://api.steampowered.com")
    steam_api_key: str = os.getenv("STEAM_API_KEY", "")
    steam_id: str = os.getenv("STEAM_ID", "")
    steamspy_api_base_url: str = os.getenv("STEAMSPY_API_BASE_URL", "https://steamspy.com")
    steam_recommend_timeout_sec: int = int(os.getenv("STEAM_RECOMMEND_TIMEOUT_SEC", "15"))
    steam_recommend_candidate_pool: int = int(os.getenv("STEAM_RECOMMEND_CANDIDATE_POOL", "60"))
    steam_recommend_negative_review_count: int = int(os.getenv("STEAM_RECOMMEND_NEGATIVE_REVIEW_COUNT", "2"))

    check_local_hour: int = int(os.getenv("CHECK_LOCAL_HOUR", "9"))
    check_local_minute: int = int(os.getenv("CHECK_LOCAL_MINUTE", "0"))

    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_use_tls: bool = _as_bool(os.getenv("SMTP_USE_TLS"), True)
    smtp_use_ssl: bool = _as_bool(os.getenv("SMTP_USE_SSL"), False)
    email_from: str = os.getenv("EMAIL_FROM", "")

    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    discord_webhook_url: str = os.getenv("DISCORD_WEBHOOK_URL", "")
    wecom_webhook_url: str = os.getenv("WECOM_WEBHOOK_URL", "")
    lark_webhook_url: str = os.getenv("LARK_WEBHOOK_URL", "")
    dingtalk_webhook_url: str = os.getenv("DINGTALK_WEBHOOK_URL", "")
    qq_onebot_url: str = os.getenv("QQ_ONEBOT_URL", "")
    qq_onebot_access_token: str = os.getenv("QQ_ONEBOT_ACCESS_TOKEN", "")

    openviking_url: str = os.getenv("OPENVIKING_URL", "http://localhost:1933")
    openviking_api_key: str = os.getenv("OPENVIKING_API_KEY", "")
    openviking_enabled: bool = _as_bool(os.getenv("OPENVIKING_ENABLED"), False)
    recall_token_budget: int = int(os.getenv("RECALL_TOKEN_BUDGET", "2000"))
    commit_interval: int = int(os.getenv("COMMIT_INTERVAL", "5"))

    chat_db_path: str = os.getenv("CHAT_DB_PATH", "chat.db")
    subscription_db_path: str = os.getenv("SUBSCRIPTION_DB_PATH", "subscriptions.db")

    use_structured_routing: bool = _as_bool(os.getenv("USE_STRUCTURED_ROUTING"), True)
    embeddings_enabled: bool = _as_bool(os.getenv("EMBEDDINGS_ENABLED"), False)

    def validate(self) -> list[str]:
        """检查关键配置项，返回缺失或异常的警告列表。"""
        warnings: list[str] = []
        if not self.openai_api_key:
            warnings.append("OPENAI_API_KEY 未设置，AI 对话功能将不可用")
        if not self.itad_api_key:
            warnings.append("ITAD_API_KEY 未设置，价格查询将降级为 CheapShark 单源")
        if not self.steam_api_key:
            warnings.append("STEAM_API_KEY 未设置，游戏库查询功能将不可用（推荐仍正常工作）")
        return warnings


settings = Settings()
