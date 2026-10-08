import html
from datetime import datetime, timezone
from typing import Optional
from aiogram import Bot, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.database import AsyncSessionLocal
from src.core.logger import setup_logger
from src.models.user import User
from src.services.must_join_service import MustJoinService
from src.services.setting_service import DEFAULT_HELP_MESSAGE, DEFAULT_WELCOME_MESSAGE, SettingService

logger = setup_logger("bot_base")
base_router = Router()


async def get_or_create_user(session: AsyncSession, msg_user) -> User:
    stmt = select(User).where(User.id == msg_user.id)
    res = await session.execute(stmt)
    user = res.scalar_one_or_none()

    if not user:
        user = User(
            id=msg_user.id,
            username=msg_user.username,
            first_name=msg_user.first_name,
            first_seen_at=datetime.now(timezone.utc),
            last_seen_at=datetime.now(timezone.utc),
        )
        session.add(user)
    else:
        user.username = msg_user.username
        user.first_name = msg_user.first_name
        user.last_seen_at = datetime.now(timezone.utc)

    await session.commit()
    return user


@base_router.message(CommandStart())
async def cmd_start(message: Message, bot: Optional[Bot] = None):
    raw_name = message.from_user.first_name if (message.from_user and message.from_user.first_name) else "User"
    safe_name = html.escape(raw_name)
    template = DEFAULT_WELCOME_MESSAGE

    active_bot = bot or getattr(message, "bot", None)

    try:
        async with AsyncSessionLocal() as session:
            if message.from_user:
                try:
                    await get_or_create_user(session, message.from_user)
                except Exception as e:
                    logger.warning(
                        "Auxiliary user persistence in /start failed: %s. Proceeding with welcome message.", e
                    )
                    try:
                        await session.rollback()
                    except Exception:
                        pass

            # Centralized Must-Join Gate for /start
            if active_bot is not None:
                allowed = await MustJoinService.enforce_must_join_message(message, active_bot, session)
                if not allowed:
                    return

            try:
                template = await SettingService.get_welcome_message(session)
            except Exception as e:
                logger.warning("Failed to load welcome message from DB: %s. Using default template.", e)
    except Exception as e:
        logger.error("Database session error in /start: %s. Using default welcome template.", e)

    welcome_text = template.replace("{first_name}", safe_name)

    try:
        await message.answer(welcome_text, parse_mode=ParseMode.HTML)
    except TelegramBadRequest as e:
        err_msg = str(e).lower()
        if "can't parse entities" in err_msg or "entity" in err_msg:
            logger.warning(
                "Failed to render custom welcome message due to HTML entity error: %s. Falling back to default.", e
            )
            fallback_text = DEFAULT_WELCOME_MESSAGE.replace("{first_name}", safe_name)
            await message.answer(fallback_text, parse_mode=ParseMode.HTML)
        else:
            raise


@base_router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = DEFAULT_HELP_MESSAGE
    try:
        async with AsyncSessionLocal() as session:
            try:
                help_text = await SettingService.get_help_message(session)
            except Exception as e:
                logger.warning("Failed to load help message from DB: %s. Using default.", e)
    except Exception as e:
        logger.error("Database session error in /help: %s. Using default.", e)

    try:
        await message.answer(help_text, parse_mode=ParseMode.HTML)
    except TelegramBadRequest as e:
        err_msg = str(e).lower()
        if "can't parse entities" in err_msg or "entity" in err_msg:
            logger.warning("Custom help message failed HTML parsing: %s. Falling back to default.", e)
            await message.answer(DEFAULT_HELP_MESSAGE, parse_mode=ParseMode.HTML)
        else:
            await message.answer(help_text)
    except Exception:
        await message.answer(DEFAULT_HELP_MESSAGE, parse_mode=ParseMode.HTML)
