import uuid
from aiogram import Bot, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select
from src.bot.handlers.codec_handler import safe_callback_answer
from src.bot.keyboards import build_must_join_keyboard, build_queue_status_keyboard
from src.core.config import settings
from src.core.constants import DeliveryStatus, JobStatus, OperationType
from src.core.database import AsyncSessionLocal
from src.core.logger import setup_logger
from src.core.redis import get_redis_client
from src.models.job import Job
from src.models.job_request import JobRequest
from src.services.cache_service import CacheService
from src.services.must_join_service import MustJoinService
from src.services.queue_service import QueueService
from src.services.ytdlp_service import YtDlpService, get_canonical_url

logger = setup_logger("audio_handler")
audio_router = Router()


@audio_router.callback_query(lambda c: c.data and c.data.startswith("aud:"))
async def on_audio_selected(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    parts = callback.data.split(":")
    if len(parts) != 3:
        await safe_callback_answer(callback, "Invalid parameters.", show_alert=True)
        return

    _, source_id, format_type = parts
    canonical_url = get_canonical_url(source_id)

    menu_msg = callback.message
    menu_msg_id = menu_msg.message_id if menu_msg else None

    if menu_msg:
        try:
            await menu_msg.edit_reply_markup(reply_markup=None)
        except Exception as e:
            logger.debug("Could not clear reply markup on audio select: %s", e)

    async with AsyncSessionLocal() as session:
        # 1. MUST-JOIN AUTHORIZATION (AUTHORITATIVE)
        if not await MustJoinService.enforce_must_join_callback(callback, bot, session):
            return

        # 2. EXACT CACHE LOOKUP
        cache_key = CacheService.generate_cache_key(
            source_id=source_id,
            operation=OperationType.AUDIO.value,
            codec="MP3",
        )

        cached_entry = await CacheService.get_cached_entry(session, cache_key)
        if cached_entry:
            delivered, err = await CacheService.deliver_cached_media(
                bot, session, cached_entry, chat_id
            )
            if delivered:
                if menu_msg:
                    try:
                        await menu_msg.delete()
                    except Exception as del_err:
                        logger.debug("Could not delete menu message on audio cache delivery: %s", del_err)
                await safe_callback_answer(callback, "Delivered from cache! 🎵")
                return

        # 3. ENFORCE USER LIMITS
        allowed, limit_err = await QueueService.check_user_limits(session, user_id)
        if not allowed:
            await safe_callback_answer(callback, limit_err, show_alert=True)
            return

        # 4. GET TITLE
        try:
            info = await YtDlpService.extract_metadata(canonical_url)
            title = info.get("title", "Audio Track")
        except Exception:
            title = "Audio Track"

        # 5. DUPLICATE JOB COALESCING
        active_statuses = [
            JobStatus.QUEUED.value,
            JobStatus.PREPARING.value,
            JobStatus.DOWNLOADING.value,
            JobStatus.PROCESSING.value,
            JobStatus.UPLOADING.value,
        ]
        stmt = (
            select(Job)
            .where(Job.cache_key == cache_key, Job.status.in_(active_statuses))
            .order_by(Job.created_at.asc())
        )
        res = await session.execute(stmt)
        existing_job = res.scalars().first()

        if existing_job:
            req = JobRequest(
                job_id=existing_job.id,
                user_id=user_id,
                chat_id=chat_id,
                delivery_status=DeliveryStatus.PENDING.value,
                menu_message_id=menu_msg_id,
            )
            session.add(req)
            await session.commit()

            if menu_msg_id:
                try:
                    r = get_redis_client()
                    await r.set(f"job_request:menu_msg:{existing_job.id}:{user_id}", str(menu_msg_id), ex=86400)
                except Exception as r_err:
                    logger.debug("Failed saving menu_msg_id for audio: %s", r_err)

            pos = await QueueService.get_derived_position(existing_job.id, queue_type="VIDEO")
            pos_str = f"#{pos}" if pos else "In Progress"
            active_count = len(await QueueService.get_active_job_ids(queue_type="VIDEO"))
            limit = getattr(settings, "MAX_ACTIVE_VIDEO_JOBS", 1)

            status_msg = await callback.message.answer(
                f"⏳ <b>Attached to existing job</b>\n"
                f"🎵 <b>MP3 Audio</b>\n"
                f"Position: {pos_str}\n"
                f"Active jobs: {active_count} / {limit}",
                reply_markup=build_queue_status_keyboard(existing_job.id),
            )
            req.status_message_id = status_msg.message_id
            await session.commit()
            await safe_callback_answer(callback, "Added to queue!")
            return

        # 6. CREATE JOB & QUEUE
        job_id = str(uuid.uuid4())
        new_job = Job(
            id=job_id,
            source_url=canonical_url,
            canonical_url=canonical_url,
            source_id=source_id,
            title=title,
            operation=OperationType.AUDIO.value,
            output_codec="MP3",
            resolution=None,
            target_height=None,
            status=JobStatus.QUEUED.value,
            cache_key=cache_key,
        )
        session.add(new_job)

        req = JobRequest(
            job_id=job_id,
            user_id=user_id,
            chat_id=chat_id,
            delivery_status=DeliveryStatus.PENDING.value,
            menu_message_id=menu_msg_id,
        )
        session.add(req)
        await session.commit()

        if menu_msg_id:
            try:
                r = get_redis_client()
                await r.set(f"job_request:menu_msg:{job_id}:{user_id}", str(menu_msg_id), ex=86400)
            except Exception as r_err:
                logger.debug("Failed saving menu_msg_id for audio: %s", r_err)

        # Push to VIDEO (media) queue
        position = await QueueService.push_job(job_id, queue_type="VIDEO")
        active_count = len(await QueueService.get_active_job_ids(queue_type="VIDEO"))
        limit = getattr(settings, "MAX_ACTIVE_VIDEO_JOBS", 1)

        status_msg = await callback.message.answer(
            f"⏳ <b>Added to queue</b>\n"
            f"🎵 <b>MP3 Audio</b>\n"
            f"Position: #{position}\n"
            f"Active: {active_count} / {limit}",
            reply_markup=build_queue_status_keyboard(job_id),
        )
        req.status_message_id = status_msg.message_id
        await session.commit()

    await safe_callback_answer(callback, "Added to queue!")
