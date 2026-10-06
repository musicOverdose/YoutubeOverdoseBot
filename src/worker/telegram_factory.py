from pathlib import Path
from typing import Optional, Tuple
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode

from src.core.config import settings
from src.core.logger import get_logger
from src.core.redis import get_redis_client

logger = get_logger("telegram_factory")


class TelegramClientFactory:
    @staticmethod
    async def get_client(mode: Optional[str] = None) -> Tuple[Bot, str]:
        """
        Creates a Bot instance dynamically according to runtime configuration.
        Worker MUST NEVER decrypt credentials directly from PostgreSQL.
        Reads token exclusively from /config/runtime/bot-token. Zero fallback to .env credentials.
        """
        token_path = Path(settings.RUNTIME_BOT_TOKEN_FILE)
        if not token_path.is_file():
            raise RuntimeError(
                f"Bot token not available: file {settings.RUNTIME_BOT_TOKEN_FILE} does not exist."
            )
        token = token_path.read_text(encoding="utf-8").strip()
        if not token:
            raise RuntimeError(
                f"Bot token in {settings.RUNTIME_BOT_TOKEN_FILE} is empty."
            )

        if not mode:
            try:
                r = get_redis_client()
                cached_mode = await r.get("telegram:active:mode")
                if cached_mode:
                    mode = (
                        cached_mode.decode("utf-8")
                        if isinstance(cached_mode, bytes)
                        else str(cached_mode)
                    )
                else:
                    mode = getattr(settings, "TELEGRAM_API_MODE", "cloud")
            except Exception as e:
                logger.debug("Could not read telegram mode from Redis: %s", e)
                mode = getattr(settings, "TELEGRAM_API_MODE", "cloud")

        if mode == "local":
            base_url = (getattr(settings, "TELEGRAM_API_BASE_URL", "") or "").rstrip("/")
            if not base_url or base_url == "https://api.telegram.org":
                base_url = "http://telegram-bot-api:8081"
            session = AiohttpSession(
                api=TelegramAPIServer.from_base(base_url, is_local=False)
            )
        else:
            session = AiohttpSession(
                api=TelegramAPIServer.from_base("https://api.telegram.org")
            )

        bot = Bot(
            token=token,
            session=session,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        return bot, mode
