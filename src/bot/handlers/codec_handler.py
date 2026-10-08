import uuid
from typing import Any, Dict, Optional
from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
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
from src.services.setting_service import SettingService
from src.services.ytdlp_service import YtDlpService, get_canonical_url

logger = setup_logger("codec_handler")
codec_router = Router()


async def safe_callback_answer(
    callback: CallbackQuery,
    text: Optional[str] = None,
    show_alert: bool = False,
) -> bool:
    """Safely answers callback query suppressing TelegramBadRequest (e.g. query is too old)."""
    try:
        if text is not None:
            if show_alert:
                await callback.answer(text, show_alert=True)
            else:
                await callback.answer(text)
        else:
            await callback.answer()
        return True
    except TelegramBadRequest as e:
        logger.debug("Suppressed TelegramBadRequest answering callback query: %s", e)
        return False
    except TelegramAPIError as e:
        logger.debug("Suppressed TelegramAPIError answering callback query: %s", e)
        return False
    except Exception as e:
        logger.warning("Unexpected error answering callback query: %s", e)
        return False


@codec_router.callback_query(lambda c: c.data and c.data.startswith("c:"))
async def on_codec_selected(callback: CallbackQuery, bot: Bot):
    parts = callback.data.split(":")
    if len(parts) != 4:
        await safe_callback_answer(callback, "Invalid parameters.", show_alert=True)
        return

    _, source_id, codec, height_str = parts
    try:
        height = int(height_str)
    except ValueError:
        await safe_callback_answer(callback, "Invalid height.", show_alert=True)
        return

    if codec not in ("H264", "H265"):
        await safe_callback_answer(callback, "Unsupported video codec.", show_alert=True)
        return

    await enqueue_video_job(bot, callback, source_id, height, codec)

async def enqueue_video_job(
    bot: Bot,
    callback: CallbackQuery,
    source_id: str,
    height: int,
    codec: str,
    info: Optional[dict] = None,
) -> None:
    """
    Core video queuing pipeline:
    1. Validates exact quality and codec against source metadata
    2. Calculates expected video size and enforces pre-download file size limit
    3. Checks cache for instant delivery
    4. Enforces per-user concurrency limits
    5. Coalesces duplicate requests or creates new job
    6. Enqueues job to VIDEO queue
    """
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    canonical_url = get_canonical_url(source_id)

    menu_msg = callback.message
    menu_msg_id = menu_msg.message_id if menu_msg else None

    # Clear inline keyboard on the preview message immediately so user cannot double-click
    if menu_msg:
        try:
            await menu_msg.edit_reply_markup(reply_markup=None)
        except Exception as e:
            logger.debug("Could not clear reply markup on quality click: %s", e)

    async with AsyncSessionLocal() as session:
        # 1. MUST-JOIN AUTHORIZATION (AUTHORITATIVE)
        if not await MustJoinService.enforce_must_join_callback(callback, bot, session):
            return

        # 2. EXACT QUALITY & CODEC VALIDATION & STALE BUTTON CHECK
        try:
            if info is None:
                info = await YtDlpService.extract_metadata(canonical_url)
            available_heights = YtDlpService.get_available_resolutions(info)
            if height not in available_heights:
                await safe_callback_answer(
                    callback,
                    f"❌ Resolution {height}p is no longer available. Please choose from current qualities.",
                    show_alert=True,
                )
                return

            available_codecs = YtDlpService.get_available_codecs_for_height(info, height)
            if codec not in available_codecs:
                # If auto-selected codec isn't in specific tag, fall back to any available or H264
                if available_codecs:
                    codec = available_codecs[0]
                else:
                    codec = "H264"

            title = info.get("title", "YouTube Video")
            res_labels = YtDlpService.get_resolution_labels(info)
            res_display = res_labels.get(height, f"{height}p")
        except Exception as e:
            logger.error(f"Error fetching metadata for verification: {e}")
            title = "YouTube Video"
            res_display = f"{height}p"

        # 3. PRE-DOWNLOAD FINAL FILE SIZE CHECK
        expected_bytes, size_source, size_details = YtDlpService.calculate_expected_video_size(
            info, height, codec
        )
        api_mode = await SettingService.get_active_api_mode(session)
        limit_mb = SettingService.get_max_video_file_size_mb(api_mode)
        limit_bytes = limit_mb * 1024 * 1024
        allowed = (expected_bytes <= limit_bytes)

        # Structured size check logging (Requirement 15)
        logger.info(
            "SIZE CHECK\n"
            "delivery_route=%s\n"
            "video_format_id=%s\n"
            "audio_format_id=%s\n"
            "video_codec=%s\n"
            "audio_codec=%s\n"
            "video_size=%d\n"
            "audio_final_size=%d\n"
            "expected_final_size=%d\n"
            "size_source=%s\n"
            "limit=%d\n"
            "allowed=%s",
            api_mode,
            size_details.get("video_format_id"),
            size_details.get("audio_format_id"),
            size_details.get("video_codec"),
            size_details.get("audio_codec"),
            size_details.get("video_size", 0),
            size_details.get("audio_final_size", 0),
            expected_bytes,
            size_source,
            limit_bytes,
            "true" if allowed else "false",
        )

        formatted_size = YtDlpService.format_file_size(expected_bytes)
        formatted_limit = YtDlpService.format_mb(limit_mb)
        size_prefix = "Size: " if size_source == "exact" else "Estimated size: ~"
        size_line = f"📦 {size_prefix}{formatted_size}"

        if not allowed:
            # Reject immediately BEFORE queuing or downloading (Requirement 6 & 7)
            rejection_alert = (
                f"❌ {size_prefix}{formatted_size}\n"
                f"Maximum allowed: {formatted_limit}\n\n"
                f"Please select a lower quality."
            )
            await safe_callback_answer(callback, rejection_alert, show_alert=True)
            try:
                await callback.message.answer(
                    f"❌ <b>Video too large for upload</b>\n\n"
                    f"🎬 <b>{height}p</b>\n"
                    f"📦 {size_prefix}<b>{formatted_size}</b>\n"
                    f"⚠️ Maximum allowed: <b>{formatted_limit}</b>\n\n"
                    f"Please select a lower quality.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="⬅️ Back to Qualities", callback_data=f"back_q:{source_id}")
                    ]]),
                )
            except Exception as e:
                logger.warning(f"Failed to send size rejection message: {e}")
            return

        # 4. EXACT CACHE KEY GENERATION & CACHE LOOKUP
        cache_key = CacheService.generate_cache_key(
            source_id=source_id,
            operation=OperationType.VIDEO.value,
            codec=codec,
            height=height,
        )

        cached_entry = await CacheService.get_cached_entry(session, cache_key)
        if cached_entry:
            # CACHE HIT: Instant delivery via copyMessage!
            # Bypasses queue, worker, yt-dlp, FFmpeg, and temp disk
            delivered, err = await CacheService.deliver_cached_media(
                bot, session, cached_entry, chat_id
            )
            if delivered:
                if menu_msg:
                    try:
                        await menu_msg.delete()
                    except Exception as del_err:
                        logger.debug("Could not delete menu message on cache delivery: %s", del_err)
                await safe_callback_answer(callback, "Delivered from cache! 🚀")
                return
            else:
                logger.warning(f"Cache delivery failed: {err}. Falling through to download.")

        # 5. ENFORCE PER-USER LIMITS
        allowed_user, limit_err = await QueueService.check_user_limits(session, user_id)
        if not allowed_user:
            await safe_callback_answer(callback, limit_err, show_alert=True)
            return

        # 6. DUPLICATE JOB COALESCING
        # Check if an active/queued job for this cache_key already exists
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
            # Coalesce: Attach this user request to existing job
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
                    logger.debug("Failed saving menu_msg_id to redis: %s", r_err)

            pos = await QueueService.get_derived_position(existing_job.id, queue_type="VIDEO")
            pos_str = f"#{pos}" if pos else "In Progress"
            active_count = len(await QueueService.get_active_job_ids(queue_type="VIDEO"))
            limit = getattr(settings, "MAX_ACTIVE_VIDEO_JOBS", 1)

            status_msg = await callback.message.answer(
                f"⏳ <b>Attached to existing job</b>\n"
                f"🎬 <b>{res_display}</b>\n"
                f"{size_line}\n"
                f"Position: {pos_str}\n"
                f"Active jobs: {active_count} / {limit}",
                reply_markup=build_queue_status_keyboard(existing_job.id),
            )
            req.status_message_id = status_msg.message_id
            await session.commit()
            await safe_callback_answer(callback, "Added to queue!")
            return

        # 7. CREATE NEW JOB & QUEUE
        job_id = str(uuid.uuid4())
        new_job = Job(
            id=job_id,
            source_url=canonical_url,
            canonical_url=canonical_url,
            source_id=source_id,
            title=title,
            operation=OperationType.VIDEO.value,
            output_codec=codec,
            resolution=res_display,
            target_height=height,
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
                logger.debug("Failed saving menu_msg_id to redis: %s", r_err)

        # Push to VIDEO queue
        position = await QueueService.push_job(job_id, queue_type="VIDEO")
        active_count = len(await QueueService.get_active_job_ids(queue_type="VIDEO"))
        limit = getattr(settings, "MAX_ACTIVE_VIDEO_JOBS", 1)

        status_msg = await callback.message.answer(
            f"⏳ <b>Added to queue</b>\n"
            f"🎬 <b>{res_display}</b>\n"
            f"{size_line}\n"
            f"Position: #{position}\n"
            f"Active: {active_count} / {limit}",
            reply_markup=build_queue_status_keyboard(job_id),
        )
        req.status_message_id = status_msg.message_id
        await session.commit()

    await safe_callback_answer(callback, "Added to queue!")
