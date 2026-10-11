from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from src.core.logger import setup_logger
from src.services.setting_service import SettingService

logger = setup_logger("maintenance_middleware")


class MaintenanceMiddleware(BaseMiddleware):
    """
    Global outer middleware that intercepts incoming Messages and CallbackQueries.
    When maintenance mode is active:
    - Whitelisted admin IDs (from maintenance_admin_ids & must_join_exempt_users) are permitted.
    - All other users receive the maintenance notification (or callback query alert)
      and execution is immediately halted.
    When maintenance mode is disabled:
    - All events pass through immediately with zero overhead.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        try:
            is_maintenance = await SettingService.is_maintenance_mode()
        except Exception as e:
            logger.debug("Error checking maintenance mode: %s", e)
            is_maintenance = False

        if not is_maintenance:
            return await handler(event, data)

        # Retrieve user details from event
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        user_id = user.id
        username = user.username or ""

        # Check if user is recognized as maintenance admin
        try:
            is_admin = await SettingService.is_user_maintenance_admin(user_id, username)
        except Exception as e:
            logger.error("Error verifying maintenance admin authorization for %s: %s", user_id, e)
            is_admin = False

        if is_admin:
            logger.debug("User %s is admin; bypassing maintenance lock.", user_id)
            return await handler(event, data)

        # Block non-admin user during maintenance
        logger.info("Blocked non-admin user %s (@%s) during active maintenance mode", user_id, username)

        try:
            maintenance_msg = await SettingService.get_maintenance_message()
        except Exception:
            maintenance_msg = (
                "🛠 <b>Bot Under Maintenance</b>\n\n"
                "The bot is currently undergoing scheduled maintenance. Please try again later!"
            )

        if isinstance(event, Message):
            try:
                await event.reply(maintenance_msg)
            except Exception as e:
                logger.warning("Failed to send maintenance reply to %s: %s", user_id, e)
            return None

        elif isinstance(event, CallbackQuery):
            try:
                await event.answer(
                    "🛠 Bot is currently undergoing maintenance. Please try again later.",
                    show_alert=True,
                )
            except Exception as e:
                logger.warning("Failed to answer callback query during maintenance to %s: %s", user_id, e)
            return None

        return None
