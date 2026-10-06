from pathlib import Path
from typing import Optional
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from src.core.config import settings

_bot: Optional[Bot] = None
_dp: Optional[Dispatcher] = None


def reset_bot() -> None:
    """Reset the cached bot instance (e.g. after configuration reload)."""
    global _bot
    _bot = None


def get_bot() -> Bot:
    """
    Get or initialize the aiogram Bot instance.
    Reads token strictly from /config/runtime/bot-token.
    Zero fallback to .env credentials or direct database decryption.
    """
    global _bot
    if _bot is None:
        token_path = Path(settings.RUNTIME_BOT_TOKEN_FILE)
        if not token_path.is_file():
            raise RuntimeError(
                f"Bot token not available: {settings.RUNTIME_BOT_TOKEN_FILE} does not exist. "
                "Telegram credentials must be configured via Admin Panel."
            )
        token = token_path.read_text(encoding="utf-8").strip()
        if not token:
            raise RuntimeError(
                f"Bot token in {settings.RUNTIME_BOT_TOKEN_FILE} is empty."
            )

        from src.services.setting_service import SettingService
        mode = getattr(settings, "TELEGRAM_API_MODE", "cloud")
        endpoint = SettingService.derive_endpoint(mode)
        session = AiohttpSession(
            api=TelegramAPIServer.from_base(endpoint, is_local=False)
        )
        _bot = Bot(
            token=token,
            session=session,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
    return _bot


def get_dispatcher() -> Dispatcher:
    global _dp
    if _dp is None:
        _dp = Dispatcher()
    return _dp
