import asyncio
from datetime import datetime, timezone
import glob
import os
from pathlib import Path
import shutil
from typing import Dict, List, Optional, Tuple
from aiogram import Bot
from aiogram.types import FSInputFile
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
import yt_dlp
from src.bot.keyboards import build_must_join_keyboard
from src.core.config import settings
from src.core.constants import (
    REDIS_KEY_SUBTITLE_CACHE_PREFIX,
    REDIS_KEY_SUBTITLE_FA_CACHE_PREFIX,
    DeliveryStatus,
    JobStatus,
    OperationType,
)
from src.core.database import AsyncSessionLocal
from src.core.logger import setup_logger
from src.core.redis import get_redis_client
from src.models.job import Job
from src.models.job_request import JobRequest
from src.models.setting import Setting
from src.services.ai_service import AIService
from src.services.cache_service import CacheService
from src.services.ffmpeg_service import FFmpegService
from src.services.must_join_service import MustJoinService
from src.services.queue_service import QueueService
from src.services.setting_service import SettingService
from src.services.system_service import SystemService
from src.services.thumbnail_service import ThumbnailService
from src.services.ytdlp_service import YtDlpService, YouTubeSubtitleRateLimitError
from src.worker.notifier import StatusNotifier
from src.worker.telegram_factory import TelegramClientFactory

logger = setup_logger("worker_processor")


class JobProcessor:
    def __init__(self, bot: Optional[Bot] = None):
        self.bot = bot

    async def report_telemetry(self) -> None:
        """Periodically publishes Worker disk usage telemetry to Redis."""
        try:
            r = get_redis_client()
            temp_bytes = 0
            if os.path.exists(settings.TEMP_DIR):
                for root, _, files in os.walk(settings.TEMP_DIR):
                    for f in files:
                        fp = os.path.join(root, f)
                        if os.path.exists(fp) and not os.path.islink(fp):
                            temp_bytes += os.path.getsize(fp)

            transfer_bytes = 0
            transfer_dir = getattr(settings, "TRANSFER_DIR", None)
            if transfer_dir and os.path.exists(transfer_dir):
                for root, _, files in os.walk(transfer_dir):
                    for f in files:
                        fp = os.path.join(root, f)
                        if os.path.exists(fp) and not os.path.islink(fp):
                            transfer_bytes += os.path.getsize(fp)

            disk_usage = shutil.disk_usage(settings.TEMP_DIR)

            await r.set("telemetry:worker:temp_ytdl_bytes", str(temp_bytes))
            await r.set("telemetry:worker:transfer_bytes", str(transfer_bytes))
            await r.set("telemetry:host:filesystem_free_bytes", str(disk_usage.free))
            await r.set("telemetry:host:filesystem_total_bytes", str(disk_usage.total))
            await r.set("telemetry:host:filesystem_used_bytes", str(disk_usage.used))
        except Exception as e:
            logger.debug("Failed to report telemetry to Redis: %s", e)

    async def _send_media_to_cache(
        self,
        bot: Bot,
        api_mode: str,
        channel_id: int,
        local_file_path: str,
        operation: str,
        job_id: str,
        job_title: str,
        caption: str = "",
        custom_filename: Optional[str] = None,
        performer: Optional[str] = None,
        duration: Optional[int] = None,
        thumbnail: Optional[FSInputFile] = None,
        extra_kwargs: Optional[dict] = None,
    ) -> Tuple[int, int]:
        """
        Sends media to Telegram cache channel using standard HTTP multipart upload via FSInputFile (is_local=False).
        Supports Cloud Mode (<= 50 MB) and Centralized Local Mode (<= 2000 MB).
        """
        extra_kwargs = extra_kwargs or {}
        file_size = os.path.getsize(local_file_path)

        if api_mode == "cloud":
            max_bytes = 50 * 1024 * 1024
            if file_size > max_bytes:
                size_mb = round(file_size / (1024 * 1024), 1)
                raise ValueError(
                    f"File size ({size_mb} MB) exceeds Cloud Bot API limit of 50 MB. "
                    "Switch to Local Bot API in Web Admin for up to 2000 MB uploads."
                )
        else:
            max_bytes = 2000 * 1024 * 1024
            if file_size > max_bytes:
                size_mb = round(file_size / (1024 * 1024), 1)
                raise ValueError(
                    f"File size ({size_mb} MB) exceeds Local Bot API limit of 2000 MB."
                )

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                if operation == OperationType.VIDEO.value or operation == "VIDEO":
                    upload_name = custom_filename or f"{job_title[:60]}.mp4"
                    tg_file = FSInputFile(local_file_path, filename=upload_name)
                    msg = await bot.send_video(
                        chat_id=channel_id,
                        video=tg_file,
                        caption=caption,
                        **extra_kwargs,
                    )
                elif operation == OperationType.AUDIO.value or operation == "AUDIO":
                    upload_name = custom_filename or f"{job_title[:60]}.mp3"
                    tg_file = FSInputFile(local_file_path, filename=upload_name)
                    audio_kwargs = dict(extra_kwargs)
                    if performer:
                        audio_kwargs["performer"] = performer
                    if duration:
                        audio_kwargs["duration"] = int(duration)
                    if thumbnail:
                        audio_kwargs["thumbnail"] = thumbnail
                    msg = await bot.send_audio(
                        chat_id=channel_id,
                        audio=tg_file,
                        title=job_title,
                        caption=caption if caption else None,
                        **audio_kwargs,
                    )
                else:
                    upload_name = custom_filename or f"{job_title[:50]}.srt"
                    tg_file = FSInputFile(local_file_path, filename=upload_name)
                    msg = await bot.send_document(
                        chat_id=channel_id,
                        document=tg_file,
                        caption=caption,
                        **extra_kwargs,
                    )
                return msg.message_id, file_size
            except Exception as ex:
                if attempt < max_retries:
                    logger.warning(
                        "Upload attempt %d/%d failed: %s. Retrying in 1s...",
                        attempt, max_retries, ex
                    )
                    await asyncio.sleep(1.0)
                else:
                    raise

    async def process_job(self, job_id: str) -> None:
        """
        Executes a single media processing pipeline.
        Guarantees cleanup, concurrency release, and cache-consistency.
        """
        job_dir = os.path.join(settings.TEMP_DIR, job_id)
        os.makedirs(job_dir, exist_ok=True)

        # Resolve authoritative non-secret Telegram configuration directly from PostgreSQL
        api_mode = "local"
        cache_channel_id = None
        config_version = "1"

        async with AsyncSessionLocal() as session:
            # 1. Synchronize public runtime settings (cookies, proxy, AI limits, Must-Join) from PostgreSQL
            try:
                active_map = await SettingService.load_public_settings_to_runtime(session)
                api_mode = active_map.get("telegram_api_mode") or getattr(settings, "TELEGRAM_API_MODE", "local")
                chan_str = active_map.get("telegram_cache_channel_id")
                if chan_str:
                    cache_channel_id = int(chan_str)
                config_version = active_map.get("telegram_config_version") or "1"
            except Exception as e:
                logger.warning("Could not sync authoritative public settings from PostgreSQL: %s. Falling back to Redis mirror.", e)
                try:
                    r = get_redis_client()
                    m = await r.get("telegram:active:mode")
                    if m:
                        api_mode = m.decode("utf-8") if isinstance(m, bytes) else str(m)
                    c = await r.get("telegram:active:cache_channel_id")
                    if c:
                        cache_channel_id = int(c.decode("utf-8") if isinstance(c, bytes) else str(c))
                except Exception:
                    pass

            bot = None
            own_bot = False
            if self.bot is not None:
                bot = self.bot
            else:
                bot, api_mode = await TelegramClientFactory.get_client(mode=api_mode)
                own_bot = True

            # 2. Fetch Job and active requests
            stmt = select(Job).where(Job.id == job_id)
            res = await session.execute(stmt)
            job = res.scalar_one_or_none()

            if not job:
                logger.error(f"Job {job_id} not found in database")
                await QueueService.release_active_job(job_id)
                shutil.rmtree(job_dir, ignore_errors=True)
                if own_bot and bot is not None:
                    await bot.session.close()
                return

            req_stmt = select(JobRequest).where(JobRequest.job_id == job_id)
            req_res = await session.execute(req_stmt)
            job_requests = list(req_res.scalars().all())

            subscribers = [
                {"chat_id": r.chat_id, "message_id": r.status_message_id}
                for r in job_requests
            ]
            notifier = StatusNotifier(bot, subscribers)

            # Check pre-flight disk safety
            safe_disk, disk_err = SystemService.check_disk_safety()
            if not safe_disk:
                logger.error(f"Disk protection triggered: {disk_err}")
                job.status = JobStatus.FAILED.value
                job.error_code = "DISK_LIMIT"
                job.error_message = disk_err
                await session.commit()
                await notifier.update("❌", "Failed", disk_err, force=True)
                await QueueService.release_active_job(job_id, queue_type=job.queue_type)
                await asyncio.to_thread(shutil.rmtree, job_dir, True)
                if own_bot and bot is not None:
                    await bot.session.close()
                return

            # Update job status to PREPARING
            job.status = JobStatus.PREPARING.value
            job.started_at = datetime.now(timezone.utc)
            await session.commit()
            await notifier.update("🔎", "Preparing", "Initializing download parameters...", force=True)

            try:
                # 2. Check for cancellation
                if await QueueService.is_cancelled(job_id):
                    logger.info(f"Job {job_id} was cancelled before starting")
                    job.status = JobStatus.CANCELLED.value
                    await session.commit()
                    return

                uploaded_msg_id = None
                if not cache_channel_id:
                    raise ValueError("TELEGRAM_CACHE_CHANNEL_ID is not configured in PostgreSQL ACTIVE settings!")

                # ==========================================
                # PIPELINE: VIDEO PROCESSING
                # ==========================================
                if job.operation == OperationType.VIDEO.value or job.operation == "VIDEO":
                    target_height = job.target_height or 1080
                    target_codec = job.output_codec or "H264"

                    # 1. WORKER-SIDE SECONDARY SIZE & ROUTE SAFETY CHECK (Requirement 5, 6, 15)
                    info = await YtDlpService.extract_metadata(job.canonical_url)
                    expected_bytes, size_source, size_details = YtDlpService.calculate_expected_video_size(
                        info, target_height, target_codec
                    )
                    limit_mb = SettingService.get_max_video_file_size_mb(api_mode)
                    limit_bytes = limit_mb * 1024 * 1024
                    cloud_limit_bytes = getattr(settings, "MAX_VIDEO_FILE_SIZE_MB_CLOUD", 48) * 1024 * 1024
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

                    # Route safety: A job larger than Cloud limit must NOT fall back to Cloud Bot API
                    if expected_bytes > cloud_limit_bytes and api_mode != "local":
                        err_msg = (
                            f"Job requires Local Bot API delivery ({formatted_size} exceeds "
                            f"{SettingService.get_max_video_file_size_mb('cloud')} MB Cloud limit), "
                            f"but active delivery route is Cloud Bot API."
                        )
                        logger.error(err_msg)
                        job.status = JobStatus.FAILED.value
                        job.error_code = "UPLOAD_ROUTE_UNAVAILABLE"
                        job.error_message = err_msg
                        await session.commit()
                        await notifier.update("❌", "Upload Route Unavailable", err_msg, force=True)
                        return

                    if not allowed:
                        err_msg = (
                            f"Video size ({formatted_size}) exceeds maximum allowed upload limit of {formatted_limit}."
                        )
                        logger.warning("Worker pre-download check rejected video job %s: %s", job_id, err_msg)
                        job.status = JobStatus.FAILED.value
                        job.error_code = "FILE_TOO_LARGE"
                        job.error_message = err_msg
                        await session.commit()
                        await notifier.update("❌", "Video Too Large", err_msg, force=True)
                        return

                    job.status = JobStatus.DOWNLOADING.value
                    await session.commit()
                    await notifier.update("⬇️", "Downloading video", f"Resolution: {job.resolution} ({target_codec})", force=True)

                    logger.info(
                        "OPERATION: DOWNLOAD + REMUX (ZERO VIDEO RE-ENCODE) for job %s (resolution=%dp, codec=%s)",
                        job_id,
                        target_height,
                        target_codec,
                    )

                    download_template = os.path.join(job_dir, "input.%(ext)s")
                    ydl_opts = YtDlpService.get_base_opts()
                    ydl_opts.update({
                        "outtmpl": download_template,
                        "format": YtDlpService.build_video_format_spec(target_height, target_codec),
                        "writethumbnail": True,
                    })

                    loop = asyncio.get_running_loop()

                    def progress_hook(d):
                        try:
                            if d.get("status") == "downloading":
                                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 1
                                downloaded = d.get("downloaded_bytes", 0)
                                pct = (downloaded / total) * 100
                                speed = d.get("_speed_str", "")
                                eta = d.get("_eta_str", "")
                                asyncio.run_coroutine_threadsafe(
                                    notifier.update_download_progress(
                                        "⬇️", "Downloading video", pct, speed=speed, eta=eta
                                    ),
                                    loop,
                                )
                        except Exception as pe:
                            logger.debug("Progress hook update error: %s", pe)

                    ydl_opts["progress_hooks"] = [progress_hook]

                    def _dl():
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            ydl.download([job.canonical_url])
                    await loop.run_in_executor(None, _dl)

                    if await QueueService.is_cancelled(job_id):
                        job.status = JobStatus.CANCELLED.value
                        await session.commit()
                        return

                    video_exts = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".ts", ".m4v"}
                    thumb_exts = {".jpg", ".jpeg", ".webp", ".png"}

                    all_input_files = glob.glob(os.path.join(job_dir, "input.*"))
                    input_video_files = [f for f in all_input_files if os.path.splitext(f)[1].lower() in video_exts]
                    input_thumb_files = [f for f in all_input_files if os.path.splitext(f)[1].lower() in thumb_exts]

                    if not input_video_files:
                        raise FileNotFoundError("Downloaded video file not found")
                    input_file = input_video_files[0]
                    found_thumb_file = input_thumb_files[0] if input_thumb_files else None

                    job.status = JobStatus.PROCESSING.value
                    await session.commit()
                    await notifier.update("🔧", "Remuxing (stream copy)...", f"Muxing {target_codec} source into MP4 container...", force=True)

                    output_file = os.path.join(job_dir, "output.mp4")
                    process_ok = await FFmpegService.process_video(
                        input_path=input_file,
                        output_path=output_file,
                        target_codec=target_codec,
                        target_height=target_height,
                    )
                    if not process_ok or not os.path.exists(output_file):
                        raise RuntimeError("FFmpeg processing failed")

                    # 1. Run ffprobe against FINAL output file
                    # Must fail cleanly with clear error if metadata cannot be determined
                    try:
                        duration, width, height = await FFmpegService.extract_video_metadata(output_file)
                        is_vertical = (height > width)
                        logger.info(
                            "Extracted metadata for final video: duration=%ds, width=%d, height=%d (is_vertical=%s)",
                            duration, width, height, is_vertical
                        )
                    except Exception as meta_err:
                        logger.error("Failed to extract valid video metadata from final file: %s", meta_err)
                        raise ValueError(f"Could not extract valid video metadata: {meta_err}") from meta_err

                    # 2. Look up highest-quality YouTube thumbnail URLs (always extracted to ensure HD artwork even on low-res downloads)
                    candidate_thumb_urls = []
                    try:
                        meta_info = await YtDlpService.extract_metadata(job.canonical_url, use_cache=True)
                        candidate_thumb_urls = ThumbnailService.get_candidate_thumbnail_urls(
                            meta_info, source_id=job.source_id
                        )
                    except Exception as meta_e:
                        logger.debug("Could not extract metadata for thumbnail URLs: %s", meta_e)

                    # 3. Generate high-fidelity JPEG thumbnail (official artwork cropped to 9:16 for shorts, or landscape)
                    thumb_desc = "Processing official vertical cover..." if is_vertical else "Processing official creator artwork..."
                    await notifier.update("🖼️", "Preparing thumbnail...", thumb_desc, force=True)
                    thumb_path = os.path.join(job_dir, "thumbnail.jpg")
                    thumb_ok = False
                    try:
                        thumb_ok = await ThumbnailService.prepare_video_thumbnail(
                            video_path=output_file,
                            output_thumb_path=thumb_path,
                            source_thumb_path=found_thumb_file,
                            source_thumb_urls=candidate_thumb_urls,
                            duration=duration,
                            is_vertical=is_vertical,
                        )
                    except Exception as thumb_err:
                        logger.warning("Thumbnail generation error: %s", thumb_err)
                        thumb_ok = False

                    extra_kwargs = {
                        "supports_streaming": True,
                        "duration": duration,
                        "width": width,
                        "height": height,
                    }
                    if thumb_ok and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
                        extra_kwargs["thumbnail"] = FSInputFile(thumb_path)
                    else:
                        logger.warning("Sending video without thumbnail because thumbnail generation failed or returned empty")

                    job.status = JobStatus.UPLOADING.value
                    await session.commit()
                    await notifier.update("📤", "Uploading...", "Sending video to Telegram...", force=True)

                    caption_text = f"🎬 <b>{job.title}</b>\n({target_codec} {job.resolution})"
                    uploaded_msg_id, file_size = await self._send_media_to_cache(
                        bot=bot,
                        api_mode=api_mode,
                        channel_id=cache_channel_id,
                        local_file_path=output_file,
                        operation=job.operation,
                        job_id=job_id,
                        job_title=job.title,
                        caption=caption_text,
                        extra_kwargs=extra_kwargs,
                    )

                    cache_entry = await CacheService.save_cache_entry(
                        session=session,
                        cache_key=job.cache_key,
                        source_id=job.source_id,
                        title=job.title,
                        operation=job.operation,
                        codec=target_codec,
                        resolution=job.resolution,
                        height=target_height,
                        file_size=file_size,
                        telegram_channel_id=cache_channel_id,
                        telegram_message_id=uploaded_msg_id,
                    )

                # ==========================================
                # PIPELINE: AUDIO (MP3) PROCESSING
                # ==========================================
                elif job.operation == OperationType.AUDIO.value or job.operation == "AUDIO":
                    job.status = JobStatus.DOWNLOADING.value
                    await session.commit()
                    await notifier.update("⬇️", "Downloading", "Fetching high quality audio...", force=True)

                    # Extract video metadata to get rich audio tags (artist, title, upload_date, etc.)
                    meta_info = {}
                    try:
                        meta_info = await YtDlpService.extract_metadata(job.canonical_url)
                    except Exception as meta_err:
                        logger.warning("Could not extract rich metadata for audio job %s: %s", job_id, meta_err)

                    audio_title, audio_artist, audio_album, audio_year, audio_duration = FFmpegService.resolve_audio_metadata(
                        info_dict=meta_info, fallback_title=job.title
                    )
                    safe_audio_filename = FFmpegService.generate_safe_audio_filename(artist=audio_artist, title=audio_title)

                    audio_template = os.path.join(job_dir, "audio.%(ext)s")
                    ydl_opts = YtDlpService.get_base_opts()
                    ydl_opts.update({
                        "outtmpl": audio_template,
                        "format": "bestaudio/best",
                        "writethumbnail": True,
                    })

                    loop = asyncio.get_running_loop()

                    def progress_hook_audio(d):
                        try:
                            if d.get("status") == "downloading":
                                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 1
                                downloaded = d.get("downloaded_bytes", 0)
                                pct = (downloaded / total) * 100
                                speed = d.get("_speed_str", "")
                                eta = d.get("_eta_str", "")
                                asyncio.run_coroutine_threadsafe(
                                    notifier.update_download_progress(
                                        "🎵", "Downloading audio", pct, speed=speed, eta=eta
                                    ),
                                    loop,
                                )
                        except Exception as pe:
                            logger.debug("Audio progress hook error: %s", pe)

                    ydl_opts["progress_hooks"] = [progress_hook_audio]

                    def _dl_audio():
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            ydl.download([job.canonical_url])
                    await loop.run_in_executor(None, _dl_audio)

                    audio_files = [f for f in glob.glob(os.path.join(job_dir, "audio.*")) if not f.endswith((".jpg", ".webp", ".png"))]
                    if not audio_files:
                        raise FileNotFoundError("Downloaded audio file not found")
                    input_audio = audio_files[0]

                    # Prepare square JPEG cover art for both ID3 APIC embedding and Telegram thumbnail
                    thumb_files = (
                        glob.glob(os.path.join(job_dir, "audio.*.jpg"))
                        or glob.glob(os.path.join(job_dir, "audio.*.webp"))
                        or glob.glob(os.path.join(job_dir, "audio.*.png"))
                    )
                    raw_cover_path = thumb_files[0] if thumb_files else None
                    cover_jpg_path = os.path.join(job_dir, "cover.jpg")
                    FFmpegService.prepare_cover_image(raw_cover_path, cover_jpg_path)

                    job.status = JobStatus.PROCESSING.value
                    await session.commit()
                    await notifier.update("🎵", "Preparing MP3...", "Converting audio with ID3 tags...", force=True)

                    output_mp3 = os.path.join(job_dir, "output.mp3")
                    mp3_ok = await FFmpegService.extract_mp3(
                        input_audio_path=input_audio,
                        output_mp3_path=output_mp3,
                        title=audio_title,
                        artist=audio_artist,
                        album=audio_album,
                        year=audio_year,
                        cover_image_path=cover_jpg_path,
                    )
                    if not mp3_ok or not os.path.exists(output_mp3):
                        raise RuntimeError("MP3 conversion failed")

                    job.status = JobStatus.UPLOADING.value
                    await session.commit()
                    await notifier.update("📤", "Uploading...", "Sending MP3 to Telegram...", force=True)

                    tg_thumbnail = FSInputFile(cover_jpg_path) if os.path.exists(cover_jpg_path) else None

                    uploaded_msg_id, file_size = await self._send_media_to_cache(
                        bot=bot,
                        api_mode=api_mode,
                        channel_id=cache_channel_id,
                        local_file_path=output_mp3,
                        operation=job.operation,
                        job_id=job_id,
                        job_title=audio_title,
                        custom_filename=safe_audio_filename,
                        performer=audio_artist,
                        duration=audio_duration,
                        thumbnail=tg_thumbnail,
                    )

                    cache_entry = await CacheService.save_cache_entry(
                        session=session,
                        cache_key=job.cache_key,
                        source_id=job.source_id,
                        title=audio_title,
                        operation=job.operation,
                        codec="MP3",
                        file_size=file_size,
                        telegram_channel_id=cache_channel_id,
                        telegram_message_id=uploaded_msg_id,
                    )

                # ==========================================
                # PIPELINE: SUBTITLE PROCESSING
                # ==========================================
                elif job.operation == OperationType.SUBTITLE.value or job.operation == "SUBTITLE":
                    target_lang = job.subtitle_lang or "EN"
                    sub_template = os.path.join(job_dir, "subs.%(ext)s")
                    r = get_redis_client()
                    final_srt_content = None
                    persian_handled = False

                    # ----------------------------------------------------------
                    # PERSIAN SUBTITLE HIERARCHY (Requirements 9-13, 15)
                    # 1. Redis Persian Cache
                    # 2. YouTube manual Persian
                    # 3. YouTube automatic Persian
                    # 4. English download + AI translation fallback
                    # ----------------------------------------------------------
                    if target_lang == "FA":
                        fa_cache_key = f"{REDIS_KEY_SUBTITLE_FA_CACHE_PREFIX}{job.source_id}"
                        # 1. Check Redis Persian cache
                        try:
                            cached_fa = await r.get(fa_cache_key)
                            if cached_fa:
                                final_srt_content = cached_fa if isinstance(cached_fa, str) else cached_fa.decode("utf-8")
                                logger.info(
                                    "SUBTITLE SOURCE\n"
                                    "source=redis\n"
                                    "language=fa\n"
                                    "ai_used=false"
                                )
                                persian_handled = True
                        except Exception as ce:
                            logger.debug("Error checking Redis Persian subtitle cache: %s", ce)

                        # 2 & 3. Check YouTube for native Persian tracks (manual > auto)
                        if not persian_handled:
                            info = await YtDlpService.extract_metadata(job.canonical_url)
                            has_persian, track_key, is_auto = YtDlpService.find_persian_subtitles(info)
                            if has_persian and track_key:
                                source_type = "youtube_manual" if not is_auto else "youtube_auto"
                                logger.info(
                                    "Native Persian subtitle track detected (%s, key=%s). Initiating direct download (bypassing AI).",
                                    source_type,
                                    track_key,
                                )

                                # Progress UX for native Persian (Requirement 13)
                                job.status = JobStatus.DOWNLOADING.value
                                await session.commit()
                                await notifier.update("🇮🇷", "Persian subtitles found", "Native Persian subtitles found on YouTube.", force=True)
                                await notifier.update("📝", "Downloading Persian subtitles...", f"Downloading native {track_key} subtitle track...", force=True)

                                async def _on_fa_retry(attempt_num: int, wait_sec: float, err: Exception):
                                    await notifier.update(
                                        "⚠️",
                                        "YouTube Rate-Limited (Persian Subtitles)",
                                        f"YouTube temporarily rate-limited subtitles.\nRetrying in {int(wait_sec)}s (attempt {attempt_num}/3)...",
                                        force=True,
                                    )

                                try:
                                    # Download specific Persian subtitle track
                                    await YtDlpService.download_subtitles(
                                        url=job.canonical_url,
                                        output_template=sub_template,
                                        on_retry=_on_fa_retry,
                                        langs=[track_key, "fa.*", "fa", "per", "fas"],
                                    )

                                    fa_sub_files = glob.glob(os.path.join(job_dir, "subs*.*"))
                                    if fa_sub_files:
                                        with open(fa_sub_files[0], "r", encoding="utf-8", errors="ignore") as sf:
                                            raw_sub_content = sf.read()

                                        # Format normalization: VTT/SRT -> standard SRT locally without AI (Requirement 12)
                                        parsed_segs = AIService.parse_srt(raw_sub_content)
                                        if parsed_segs:
                                            final_srt_content = "\n".join(seg.to_srt() for seg in parsed_segs)
                                            # Cache in Redis with 24h TTL and <= 1 MB size limit (Requirement 11)
                                            try:
                                                sub_bytes = len(final_srt_content.encode("utf-8"))
                                                if sub_bytes <= 1024 * 1024:
                                                    await r.set(fa_cache_key, final_srt_content, ex=86400)
                                                    logger.info("Cached direct Persian subtitles in Redis for %s (%d bytes, TTL=24h)", job.source_id, sub_bytes)
                                            except Exception as se:
                                                logger.debug("Failed to cache Persian subtitles in Redis: %s", se)

                                            logger.info(
                                                "SUBTITLE SOURCE\n"
                                                "source=%s\n"
                                                "language=fa\n"
                                                "ai_used=false",
                                                source_type,
                                            )
                                            persian_handled = True
                                except Exception as fa_dl_err:
                                    logger.warning(
                                        "Direct Persian subtitle download failed after retries: %s. Falling back to English + AI translation.",
                                        fa_dl_err,
                                    )
                                    await notifier.update(
                                        "⚠️",
                                        "Persian Subtitle Download Failed",
                                        "Native Persian download unavailable. Falling back to English subtitles + AI translation...",
                                        force=True,
                                    )

                    # ----------------------------------------------------------
                    # 4. ENGLISH SUBTITLE DOWNLOAD + AI TRANSLATION (IF FA & NOT HANDLED)
                    # OR STANDARD ENGLISH SUBTITLE RETRIEVAL (IF EN)
                    # ----------------------------------------------------------
                    if not persian_handled:
                        job.status = JobStatus.DOWNLOADING.value
                        await session.commit()
                        await notifier.update("📝", "Downloading subtitles...", "Extracting English subtitle track...", force=True)

                        english_srt = None
                        try:
                            sub_cache_key = f"{REDIS_KEY_SUBTITLE_CACHE_PREFIX}{job.source_id}"
                            cached_sub = await r.get(sub_cache_key)
                            if cached_sub:
                                english_srt = cached_sub if isinstance(cached_sub, str) else cached_sub.decode("utf-8")
                                logger.info("Reusing cached English subtitles from Redis for source %s", job.source_id)
                        except Exception as ce:
                            logger.debug("Error checking Redis subtitle cache: %s", ce)

                        if not english_srt:
                            async def _on_sub_retry(attempt_num: int, wait_sec: float, err: Exception):
                                await notifier.update(
                                    "⚠️",
                                    "YouTube Temporarily Rate-Limited",
                                    f"YouTube temporarily rate-limited subtitles.\nRetrying in {int(wait_sec)}s (attempt {attempt_num}/3)...",
                                    force=True,
                                )

                            try:
                                await YtDlpService.download_subtitles(
                                    url=job.canonical_url,
                                    output_template=sub_template,
                                    on_retry=_on_sub_retry,
                                    langs=["en.*", "en"],
                                )
                            except YouTubeSubtitleRateLimitError as rl_err:
                                raise RuntimeError(str(rl_err)) from rl_err

                            sub_files = glob.glob(os.path.join(job_dir, "subs*.*"))
                            if not sub_files:
                                raise FileNotFoundError("English subtitles could not be extracted")

                            with open(sub_files[0], "r", encoding="utf-8", errors="ignore") as sf:
                                raw_sub_content = sf.read()

                            parsed_segs = AIService.parse_srt(raw_sub_content)
                            if not parsed_segs:
                                raise ValueError("Failed to parse subtitle segments")
                            english_srt = "\n".join(seg.to_srt() for seg in parsed_segs)

                            # Cache in Redis with 24-hour TTL (limit size to max 1 MB)
                            try:
                                sub_bytes = len(english_srt.encode("utf-8"))
                                if sub_bytes <= 1024 * 1024:
                                    await r.set(f"{REDIS_KEY_SUBTITLE_CACHE_PREFIX}{job.source_id}", english_srt, ex=86400)
                                    logger.info("Cached English subtitles in Redis for %s (%d bytes, TTL=24h)", job.source_id, sub_bytes)
                                else:
                                    logger.debug("English subtitles for %s exceed 1 MB (%d bytes), skipping Redis cache", job.source_id, sub_bytes)
                            except Exception as se:
                                logger.debug("Failed to cache English subtitles in Redis: %s", se)
                        else:
                            parsed_segs = AIService.parse_srt(english_srt)
                            if not parsed_segs:
                                raise ValueError("Failed to parse cached subtitle segments")

                        final_srt_content = english_srt

                        if target_lang == "FA":
                            # Check chunk limit before starting AI translation
                            chunk_size = getattr(settings, "AI_CHUNK_SIZE", 10)
                            total_chunks = (len(parsed_segs) + chunk_size - 1) // chunk_size
                            max_chunks = getattr(settings, "AI_MAX_CHUNKS", 50)
                            if max_chunks and max_chunks > 0 and total_chunks > max_chunks:
                                max_cues = max_chunks * chunk_size
                                raise RuntimeError(
                                    f"Video subtitle size is too large for AI translation ({total_chunks} chunks / {len(parsed_segs)} cues exceeds maximum allowed limit of {max_chunks} chunks / {max_cues} cues). Please download English subtitles instead."
                                )

                            job.status = JobStatus.PROCESSING.value
                            await session.commit()
                            await notifier.update("🌐", "Translating subtitles...", f"Translating subtitles to Persian with AI ({total_chunks} chunks)...", force=True)

                            async def _on_ai_progress(c_idx: int, total_c: int, attempt: int):
                                pct = (c_idx / total_c) * 100.0 if total_c > 0 else 0.0
                                bar = StatusNotifier.render_progress_bar(pct)
                                if attempt > 1:
                                    d_text = f"Chunk {c_idx}/{total_c} • Retry {attempt}/3\n{bar}"
                                else:
                                    d_text = f"Chunk {c_idx}/{total_c}\n{bar}"
                                await notifier.update("🌐", "Translating subtitles", d_text)

                            ok, persian_srt, ai_err = await AIService.translate_english_to_persian(
                                english_srt, on_progress=_on_ai_progress
                            )
                            if not ok or not persian_srt:
                                raise RuntimeError(f"Persian translation failed: {ai_err}")
                            final_srt_content = persian_srt

                            logger.info(
                                "SUBTITLE SOURCE\n"
                                "source=ai_translation\n"
                                "language=fa\n"
                                "ai_used=true"
                            )

                    out_srt_file = os.path.join(job_dir, f"{target_lang}_subtitles.srt")
                    with open(out_srt_file, "w", encoding="utf-8") as f:
                        f.write(final_srt_content)

                    job.status = JobStatus.UPLOADING.value
                    await session.commit()
                    await notifier.update("📤", "Uploading...", "Sending subtitles to Telegram...", force=True)

                    caption_text = f"💬 <b>{job.title}</b> ({target_lang} Subtitle)"
                    uploaded_msg_id, file_size = await self._send_media_to_cache(
                        bot=bot,
                        api_mode=api_mode,
                        channel_id=cache_channel_id,
                        local_file_path=out_srt_file,
                        operation=job.operation,
                        job_id=job_id,
                        job_title=f"{job.title[:50]}_{target_lang}",
                        caption=caption_text,
                    )

                    cache_entry = await CacheService.save_cache_entry(
                        session=session,
                        cache_key=job.cache_key,
                        source_id=job.source_id,
                        title=job.title,
                        operation=job.operation,
                        subtitle_lang=target_lang,
                        file_size=file_size,
                        telegram_channel_id=cache_channel_id,
                        telegram_message_id=uploaded_msg_id,
                    )

                # ==========================================
                # FINAL DELIVERY TO SUBSCRIBERS
                # STRICT MUST-JOIN RE-CHECK PER USER
                # ==========================================
                job.status = JobStatus.COMPLETED.value
                job.completed_at = datetime.now(timezone.utc)
                await session.commit()

                # Refresh request list
                fresh_req_res = await session.execute(
                    select(JobRequest).where(JobRequest.job_id == job_id)
                )
                current_requests = list(fresh_req_res.scalars().all())

                for req in current_requests:
                    is_auth, missing_channels = await MustJoinService.require_must_join(
                        bot, session, req.user_id, force_authoritative=True
                    )

                    if not is_auth:
                        logger.warning(
                            f"User {req.user_id} left channel before delivery. Setting WAITING_FOR_AUTHORIZATION."
                        )
                        req.delivery_status = DeliveryStatus.WAITING_FOR_AUTHORIZATION.value
                        await session.commit()

                        kb = build_must_join_keyboard(missing_channels)
                        try:
                            await bot.send_message(
                                chat_id=req.chat_id,
                                text=(
                                    "🔒 <b>Your file is ready!</b>\n"
                                    "However, you left one of the required channels. "
                                    "Please re-join the channels below and tap <b>Check Again</b> to receive your file."
                                ),
                                reply_markup=kb,
                            )
                        except Exception:
                            pass
                    else:
                        delivered, err = await CacheService.deliver_cached_media(
                            bot, session, cache_entry, req.chat_id
                        )
                        if delivered:
                            req.delivery_status = DeliveryStatus.DELIVERED.value
                            req.delivered_at = datetime.now(timezone.utc)
                            await session.commit()

                            if req.status_message_id:
                                try:
                                    await bot.edit_message_text(
                                        chat_id=req.chat_id,
                                        message_id=req.status_message_id,
                                        text="✅ <b>Download complete! Delivered above.</b>",
                                    )
                                except Exception:
                                    pass
                        else:
                            req.delivery_status = DeliveryStatus.FAILED.value
                            await session.commit()

            except Exception as e:
                logger.error(f"Error executing job {job_id}: {e}", exc_info=True)
                job.status = JobStatus.FAILED.value
                job.error_code = "PROCESSING_ERROR"
                job.error_message = str(e)[:500]
                await session.commit()
                err_str = str(e)
                if "too large for AI translation" in err_str:
                    await notifier.update("⚠️", "Subtitle Too Large", err_str, force=True)
                else:
                    await notifier.update("❌", "Failed", f"Error: {err_str[:300]}", force=True)

            finally:
                if own_bot and bot is not None:
                    try:
                        await bot.session.close()
                    except Exception as e:
                        logger.debug("Error closing worker bot session: %s", e)

                # GUARANTEED CLEANUP:
                await asyncio.to_thread(shutil.rmtree, job_dir, True)
                logger.info(f"Cleaned temp directory for job {job_id}")

                await QueueService.release_active_job(job_id, queue_type=job.queue_type)
                await QueueService.release_cache_lock(job.cache_key)

                # Report disk telemetry
                await self.report_telemetry()
