from datetime import datetime, timezone
import html
from aiogram import Bot, Router
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery
from sqlalchemy import select, update
from src.bot.keyboards import build_must_join_keyboard
from src.core.constants import DeliveryStatus
from src.core.database import AsyncSessionLocal
from src.core.logger import setup_logger
from src.models.cache import CacheEntry
from src.models.job import Job
from src.models.job_request import JobRequest
from src.services.cache_service import CacheService
from src.services.must_join_service import MustJoinService

logger = setup_logger("must_join_handler")
must_join_router = Router()


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

    async with AsyncSessionLocal() as session:
        # ALWAYS FRESH AUTHORITATIVE TELEGRAM API CHECK!
        is_auth, missing_channels = await MustJoinService.require_must_join(
            bot, session, user_id, username=username, force_authoritative=True
        )

        if not is_auth:
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
            return

        # User is AUTHORIZED!
        await callback.answer("✅ Verified!", show_alert=False)
        if callback.message:
            try:
                await callback.message.edit_text(
                    "✅ <b>Joined!</b>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        # 1. CHECK FOR PENDING 'WAITING_FOR_AUTHORIZATION' DELIVERIES
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

        for req, job, cache_entry in pending_items:
            delivered, err = await CacheService.deliver_cached_media(
                bot, session, cache_entry, chat_id
            )
            if delivered:
                await session.execute(
                    update(JobRequest)
                    .where(JobRequest.id == req.id)
                    .values(
                        delivery_status=DeliveryStatus.DELIVERED.value,
                        delivered_at=datetime.now(timezone.utc),
                    )
                )
                await session.commit()
                logger.info(f"Delivered previously pending file to rejoined user {user_id}")

        # 2. CHECK FOR SERVER-SIDE PENDING ACTION
        pending = await MustJoinService.consume_pending_action(user_id=user_id, chat_id=chat_id)
        if pending:
            action_type = pending.get("action_type")
            payload = pending.get("payload", {})
            if action_type == "url":
                url = payload.get("url")
                source_id = payload.get("source_id")
                if url and source_id:
                    from src.bot.handlers.url_handler import process_youtube_url
                    if callback.message:
                        await callback.message.answer(
                            f"🔗 Resuming your request for:\n<b>{html.escape(url)}</b>",
                            parse_mode=ParseMode.HTML,
                        )
                    await process_youtube_url(bot, chat_id, url, source_id, user_id=user_id)
                    return

        if callback.message:
            await callback.message.answer(
                "🎉 You are all set! Send any YouTube link to start downloading."
            )
