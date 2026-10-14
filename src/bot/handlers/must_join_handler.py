from datetime import datetime, timezone
import html
from typing import Optional
from aiogram import Bot, Router
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, ChatMemberUpdated, Update
from sqlalchemy import select, update
from src.bot.keyboards import build_must_join_keyboard
from src.core.constants import DeliveryStatus, REDIS_KEY_MUST_JOIN_MSG_PREFIX
from src.core.database import AsyncSessionLocal
from src.core.logger import setup_logger
from src.core.redis import get_redis_client
from src.models.cache import CacheEntry
from src.models.job import Job
from src.models.job_request import JobRequest
from src.services.cache_service import CacheService
from src.services.must_join_service import MustJoinService
from src.services.setting_service import SettingService

logger = setup_logger("must_join_handler")
must_join_router = Router()


async def check_and_auto_unblock_user(
    bot: Bot,
    user_id: int,
    username: Optional[str] = None,
    explicit_chat_id: Optional[int] = None,
    force_authoritative: bool = False,
) -> bool:
    """
    Checks if a user has satisfied all required channels, and if so:
    1. Cleanly deletes the Must-Join prompt message from chat.
    2. Delivers any media that was WAITING_FOR_AUTHORIZATION.
    3. Resumes any pending action (URL or /start).
    Returns True if unblocked, False if still missing channels.
    """
    async with AsyncSessionLocal() as session:
        is_auth, missing_channels = await MustJoinService.require_must_join(
            bot, session, user_id, username=username, force_authoritative=force_authoritative
        )
        if not is_auth:
            return False

        r = get_redis_client()
        prompt_info = None
        try:
            prompt_info = await r.get(f"{REDIS_KEY_MUST_JOIN_MSG_PREFIX}{user_id}")
        except Exception as e:
            logger.debug("Redis error fetching prompt msg: %s", e)

        target_chat_id = explicit_chat_id or user_id
        if prompt_info:
            try:
                if isinstance(prompt_info, bytes):
                    prompt_info = prompt_info.decode("utf-8")
                p_chat_id, p_msg_id = map(int, prompt_info.split(":"))
                target_chat_id = p_chat_id
                await bot.delete_message(chat_id=p_chat_id, message_id=p_msg_id)
            except Exception as e:
                logger.debug("Could not delete must-join prompt message %s: %s", prompt_info, e)
            try:
                await r.delete(f"{REDIS_KEY_MUST_JOIN_MSG_PREFIX}{user_id}")
            except Exception:
                pass

        # 1. Deliver pending WAITING_FOR_AUTHORIZATION media
        stmt = (
            select(JobRequest, Job, CacheEntry)
            .join(Job, Job.id == JobRequest.job_id)
            .join(CacheEntry, CacheEntry.cache_key == Job.cache_key)
            .where(
                JobRequest.user_id == user_id,
                JobRequest.delivery_status == DeliveryStatus.WAITING_FOR_AUTHORIZATION.value,
                CacheEntry.is_valid == True,
            )
        )
        res = await session.execute(stmt)
        pending_items = res.all()
        had_pending_delivery = False

        for req, job, cache_entry in pending_items:
            delivered, err = await CacheService.deliver_cached_media(
                bot, session, cache_entry, target_chat_id
            )
            if delivered:
                had_pending_delivery = True
                await session.execute(
                    update(JobRequest)
                    .where(JobRequest.id == req.id)
                    .values(
                        delivery_status=DeliveryStatus.DELIVERED.value,
                        delivered_at=datetime.now(timezone.utc),
                    )
                )
                await session.commit()
                logger.info(f"Delivered previously pending file to unblocked user {user_id}")

        # 2. Check for server-side pending action (e.g. YouTube URL or /start)
        pending = await MustJoinService.consume_pending_action(user_id=user_id, chat_id=target_chat_id)
        if pending:
            action_type = pending.get("action_type")
            payload = pending.get("payload", {})
            if action_type == "url":
                url = payload.get("url")
                source_id = payload.get("source_id")
                if url and source_id:
                    from src.bot.handlers.url_handler import process_youtube_url
                    try:
                        await bot.send_message(
                            chat_id=target_chat_id,
                            text=f"🔗 <b>Resuming your request for:</b>\n{html.escape(url)}",
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        pass
                    await process_youtube_url(bot, target_chat_id, url, source_id, user_id=user_id)
                    return True
            elif action_type == "start":
                welcome_text = await SettingService.get_welcome_message()
                try:
                    await bot.send_message(
                        chat_id=target_chat_id,
                        text=welcome_text,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    await bot.send_message(
                        chat_id=target_chat_id,
                        text=welcome_text,
                    )
                return True

        if not had_pending_delivery:
            welcome_text = await SettingService.get_welcome_message()
            try:
                await bot.send_message(
                    chat_id=target_chat_id,
                    text=f"🎉 <b>You are verified!</b>\n\n{welcome_text}",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                await bot.send_message(
                    chat_id=target_chat_id,
                    text="🎉 You are verified! Send any YouTube link to start downloading.",
                )

        return True


@must_join_router.chat_member()
async def on_channel_chat_member_updated(
    event: ChatMemberUpdated,
    bot: Bot,
    event_update: Optional[Update] = None,
):
    """Event-driven listener for user join/leave/kick updates in required channels."""
    update_id = getattr(event_update, "update_id", None)
    joined = await MustJoinService.handle_chat_member_event(bot, event, update_id=update_id)
    if joined:
        user = event.new_chat_member.user if event.new_chat_member else None
        if user and not user.is_bot:
            logger.info("Triggering auto-unblock check for user %d...", user.id)
            await check_and_auto_unblock_user(bot, user.id, username=user.username)


@must_join_router.my_chat_member()
async def on_bot_chat_member_updated(
    event: ChatMemberUpdated,
    bot: Bot,
):
    """Event-driven listener for bot's own administrative permission changes in required channels."""
    await MustJoinService.handle_my_chat_member_event(bot, event)


@must_join_router.callback_query(lambda c: c.data in ("must_join:no_url", "none"))
async def on_no_url_clicked(callback: CallbackQuery):
    await callback.answer(
        "This channel does not have a public invite link configured. Please contact the administrator.",
        show_alert=True,
    )


@must_join_router.callback_query(
    lambda c: c.data and (
        c.data == "must_join:check"
        or c.data.startswith("must_join:check:")
        or c.data.startswith("mj_chk")
    )
)
async def on_must_join_check_again(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    username = callback.from_user.username if callback.from_user else None
    chat_id = callback.message.chat.id if callback.message else user_id

    unblocked = await check_and_auto_unblock_user(
        bot, user_id, username=username, explicit_chat_id=chat_id, force_authoritative=True
    )
    if not unblocked:
        async with AsyncSessionLocal() as session:
            _, missing_channels = await MustJoinService.require_must_join(
                bot, session, user_id, username=username, force_authoritative=True
            )
            kb = build_must_join_keyboard(missing_channels)
            await callback.answer(
                "❌ You still haven't joined all required channels. Please join them first!",
                show_alert=True,
            )
            if callback.message:
                try:
                    await callback.message.edit_reply_markup(reply_markup=kb)
                except Exception:
                    pass
    else:
        await callback.answer("✅ Verified!", show_alert=False)
        if callback.message:
            try:
                await callback.message.edit_text("✅ <b>Joined!</b>", parse_mode=ParseMode.HTML)
            except Exception:
                pass
