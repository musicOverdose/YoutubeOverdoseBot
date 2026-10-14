import asyncio
from datetime import datetime, timezone
import glob
import os
from pathlib import Path
import shutil
import threading
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
from src.models.cache import CacheEntry
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
from src.utils.timestamp_parser import format_caption_timestamp, format_clip_filename_tag, format_timestamp
from src.worker.notifier import StatusNotifier
from src.worker.telegram_factory import TelegramClientFactory

logger = setup_logger("worker_processor")


def format_video_caption_resolution(resolution: Optional[str], height: Optional[int] = None) -> str:
    """
    Formats the resolution text for video message captions on Telegram.
    Simplifies 2160p -> 4K, 1440p -> 2K, and preserves other resolutions (1080p, 720p, etc.).
    """
    res = (resolution or "").strip()
    is_4k = (height == 2160) or ("2160" in res) or (res.upper() == "4K")
    is_2k = (height == 1440) or ("1440" in res) or (res.upper() == "2K")

    if is_4k:
        return "4K (Vertical)" if "vertical" in res.lower() else "4K"
    if is_2k:
        return "2K (Vertical)" if "vertical" in res.lower() else "2K"
    return res or (f"{height}p" if height else "")


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
            notifier = StatusNotifier(bot, subscribers, job_id=job_id)

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
            await notifier.update_unified_progress(2.0, subtitle="Starting...", force=True)

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
                    clip_duration = (job.clip_end - job.clip_start) if (job.clip_start is not None and job.clip_end is not None) else None
                    is_muted = bool(job.is_muted)
                    is_clip = (job.clip_start is not None and job.clip_end is not None)

                    info = await YtDlpService.extract_metadata(job.canonical_url)
                    if info.get("is_live") or info.get("live_status") == "is_live":
                        logger.warning("Job %s rejected: stream is live", job_id)
                        await notifier.stop_ticker()
                        job.status = JobStatus.FAILED.value
                        job.error_code = "LIVESTREAM_NOT_SUPPORTED"
                        job.error_message = "Livestreams are not supported."
                        await session.commit()
                        await notifier.update("❌", "Failed", "Livestreams are not supported.", force=True)
                        await QueueService.release_active_job(job_id, queue_type=job.queue_type)
                        await asyncio.to_thread(shutil.rmtree, job_dir, True)
                        if own_bot and bot is not None:
                            await bot.session.close()
                        return

                    expected_bytes, size_source, size_details = YtDlpService.calculate_expected_video_size(
                        info, target_height, target_codec, clip_duration=clip_duration, is_muted=is_muted
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
                    clip_status = ""
                    if is_clip:
                        clip_status = f" • Clip ({format_timestamp(job.clip_start)} - {format_timestamp(job.clip_end)})"
                    if is_muted:
                        clip_status += " • Muted"
                    notifier.start_ticker(initial_pct=5.0)

                    logger.info(
                        "OPERATION: DOWNLOAD + REMUX for job %s (resolution=%dp, codec=%s, is_clip=%s, is_muted=%s)",
                        job_id,
                        target_height,
                        target_codec,
                        is_clip,
                        is_muted,
                    )

                    download_template = os.path.join(job_dir, "input.%(ext)s")
                    ydl_format = YtDlpService.build_video_format_spec(target_height, target_codec, is_muted=is_muted)
                    ydl_opts = YtDlpService.get_download_opts()
                    ydl_opts.update({
                        "outtmpl": download_template,
                        "format": ydl_format,
                        "writethumbnail": not is_clip,
                    })

                    clip_offset = None
                    clip_duration = None
                    if is_clip:
                        # 5-second cushion to capture the preceding keyframe, eliminating blurry start on iOS
                        cushion_sec = 5.0 if (job.clip_start and job.clip_start > 0) else 0.0
                        dl_start = max(0.0, float(job.clip_start or 0) - cushion_sec)
                        dl_end = float(job.clip_end)
                        ydl_opts["download_ranges"] = yt_dlp.utils.download_range_func(
                            None, [(dl_start, dl_end)]
                        )
                        clip_offset = float(job.clip_start or 0) - dl_start
                        clip_duration = float((job.clip_end or 0) - (job.clip_start or 0))

                    loop = asyncio.get_running_loop()
                    cancel_event = threading.Event()

                    async def _cancel_watcher():
                        while not cancel_event.is_set():
                            if await QueueService.is_cancelled(job_id):
                                cancel_event.set()
                                break
                            await asyncio.sleep(0.5)

                    def progress_hook(d):
                        if cancel_event.is_set():
                            raise yt_dlp.utils.DownloadCancelled("Job cancelled by user")
                        try:
                            if d.get("status") == "downloading":
                                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                                downloaded = d.get("downloaded_bytes", 0)
                                if total and total > 0:
                                    pct = (downloaded / total) * 100
                                elif d.get("fragment_count") and d.get("fragment_count") > 0:
                                    pct = (d.get("fragment_index", 0) / d.get("fragment_count")) * 100
                                else:
                                    pct = 50.0
                                speed = d.get("_speed_str", "")
                                eta = d.get("_eta_str", "")
                                notifier.on_download_progress(pct, speed=speed, eta=eta)
                        except yt_dlp.utils.DownloadCancelled:
                            raise
                        except Exception as pe:
                            logger.debug("Progress hook update error: %s", pe)

                    ydl_opts["progress_hooks"] = [progress_hook]

                    def _dl():
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            ydl.download([job.canonical_url])

                    watcher_task = asyncio.create_task(_cancel_watcher())
                    try:
                        await loop.run_in_executor(None, _dl)
                    except (yt_dlp.utils.DownloadCancelled, Exception) as dl_err:
                        if cancel_event.is_set() or isinstance(dl_err, yt_dlp.utils.DownloadCancelled):
                            logger.info("Video download aborted due to cancellation for job %s", job_id)
                            await notifier.stop_ticker()
                            job.status = JobStatus.CANCELLED.value
                            await session.commit()
                            await QueueService.release_active_job(job_id, queue_type=job.queue_type)
                            await asyncio.to_thread(shutil.rmtree, job_dir, True)
                            if own_bot and bot is not None:
                                await bot.session.close()
                            return
                        raise
                    finally:
                        watcher_task.cancel()

                    notifier.set_stage("process")

                    if await QueueService.is_cancelled(job_id):
                        await notifier.stop_ticker()
                        job.status = JobStatus.CANCELLED.value
                        await session.commit()
                        await QueueService.release_active_job(job_id, queue_type=job.queue_type)
                        await asyncio.to_thread(shutil.rmtree, job_dir, True)
                        if own_bot and bot is not None:
                            await bot.session.close()
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

                    # 1. Look up highest-quality YouTube thumbnail URLs & pre-download cover artwork (full videos only)
                    candidate_thumb_urls = []
                    meta_info = None
                    raw_cover_path = os.path.join(job_dir, "cover.jpg")
                    cover_downloaded = False

                    if not is_clip:
                        try:
                            meta_info = await YtDlpService.extract_metadata(job.canonical_url, use_cache=True)
                            candidate_thumb_urls = ThumbnailService.get_candidate_thumbnail_urls(
                                meta_info, source_id=job.source_id
                            )
                        except Exception as meta_e:
                            logger.debug("Could not extract metadata for thumbnail URLs: %s", meta_e)

                        # Determine video orientation and aspect ratio early so cover image can be cropped for vertical Shorts
                        is_vertical = False
                        target_ratio = None
                        early_w, early_h = 0, 0
                        _dur = 0
                        try:
                            _dur, early_w, early_h = await FFmpegService.extract_video_metadata(input_file)
                            if early_w > 0 and early_h > 0:
                                is_vertical = (early_h > early_w)
                                if is_vertical:
                                    target_ratio = early_w / float(early_h)
                        except Exception as probe_err:
                            logger.debug("Could not probe input_file early for orientation: %s", probe_err)
                            if "/shorts/" in (job.canonical_url or "").lower():
                                is_vertical = True
                                target_ratio = 9 / 16.0

                        try:
                            res = await ThumbnailService.download_full_thumbnail_file(
                                info=meta_info,
                                source_id=job.source_id,
                                output_path=raw_cover_path,
                                is_vertical=is_vertical,
                                target_ratio=target_ratio,
                            )
                            if res and os.path.exists(raw_cover_path) and os.path.getsize(raw_cover_path) > 0:
                                cover_downloaded = True
                        except Exception as ce:
                            logger.debug("Could not pre-download cover artwork for video: %s", ce)

                        if not cover_downloaded and found_thumb_file and os.path.exists(found_thumb_file):
                            try:
                                ok_conv = ThumbnailService.process_image_file(
                                    found_thumb_file,
                                    raw_cover_path,
                                    is_vertical=is_vertical,
                                    target_ratio=target_ratio,
                                )
                                if ok_conv and os.path.exists(raw_cover_path) and os.path.getsize(raw_cover_path) > 0:
                                    cover_downloaded = True
                            except Exception as fe:
                                logger.debug("Could not convert found_thumb_file to cover: %s", fe)

                        # Hybrid Thumbnail Selection for full videos:
                        # Check resolution fidelity of downloaded cover vs native video
                        use_frame_extraction = False
                        if early_w > 0 and early_h > 0 and max(early_w, early_h) >= 720:
                            # Video is HD (>= 720p). If cover art is low-res (e.g. YouTube auto-generated 480p pillarbox),
                            # extract crystal-clear native frame directly from the HD video stream!
                            if cover_downloaded and os.path.exists(raw_cover_path):
                                try:
                                    with Image.open(raw_cover_path) as cimg:
                                        cw, ch = cimg.size
                                        if (is_vertical and cw < 500) or (not is_vertical and max(cw, ch) < 640):
                                            logger.info(
                                                "Cover art resolution is low (%dx%d) for HD video (%dx%d). "
                                                "Extracting native HD frame directly from video stream...",
                                                cw, ch, early_w, early_h,
                                            )
                                            use_frame_extraction = True
                                except Exception as c_err:
                                    logger.debug("Error checking cover art dimensions: %s", c_err)
                            else:
                                use_frame_extraction = True

                        if use_frame_extraction:
                            try:
                                frame_ok = await ThumbnailService.extract_frame_fallback(
                                    video_path=input_file,
                                    output_thumb_path=raw_cover_path,
                                    duration=_dur,
                                    is_vertical=is_vertical,
                                    target_ratio=target_ratio,
                                )
                                if frame_ok and os.path.exists(raw_cover_path) and os.path.getsize(raw_cover_path) > 0:
                                    cover_downloaded = True
                            except Exception as frame_err:
                                logger.warning("Frame extraction error: %s", frame_err)

                    job.status = JobStatus.PROCESSING.value
                    await session.commit()
                    notifier.set_stage("process")

                    output_file = os.path.join(job_dir, "output.mp4")
                    process_ok = await FFmpegService.process_video(
                        input_path=input_file,
                        output_path=output_file,
                        target_codec=target_codec,
                        target_height=target_height,
                        cover_image_path=None if is_clip else (raw_cover_path if cover_downloaded else None),
                        prepend_thumbnail=False if is_clip else True,
                        is_muted=is_muted,
                        clip_offset=clip_offset,
                        clip_duration=clip_duration,
                    )
                    if not process_ok or not os.path.exists(output_file):
                        raise RuntimeError("FFmpeg processing failed")

                    # 2. Run ffprobe against FINAL output file
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

                    extra_kwargs = {
                        "supports_streaming": True,
                        "duration": duration,
                        "width": width,
                        "height": height,
                    }

                    # 3. For full videos, generate high-fidelity JPEG thumbnail for Telegram chat bubble.
                    # For clips, omit custom thumbnail so users receive pure trimmed stream with 0 extra frames.
                    if not is_clip:
                        thumb_path = os.path.join(job_dir, "thumbnail.jpg")
                        thumb_ok = False
                        target_ratio = (width / float(height)) if is_vertical and height > 0 else None

                        if cover_downloaded and os.path.exists(raw_cover_path) and os.path.getsize(raw_cover_path) > 0:
                            thumb_ok = ThumbnailService.create_telegram_chat_thumbnail(
                                source_image_path=raw_cover_path,
                                output_thumb_path=thumb_path,
                                is_vertical=is_vertical,
                                target_ratio=target_ratio,
                            )

                        if not thumb_ok:
                            try:
                                thumb_ok = await ThumbnailService.prepare_video_thumbnail(
                                    video_path=output_file,
                                    output_thumb_path=thumb_path,
                                    source_thumb_path=found_thumb_file,
                                    source_thumb_urls=candidate_thumb_urls,
                                    duration=duration,
                                    is_vertical=is_vertical,
                                    target_ratio=target_ratio,
                                )
                            except Exception as thumb_err:
                                logger.warning("Thumbnail generation error: %s", thumb_err)
                                thumb_ok = False

                        if thumb_ok and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
                            extra_kwargs["thumbnail"] = FSInputFile(thumb_path)

                    job.status = JobStatus.UPLOADING.value
                    await session.commit()
                    notifier.set_stage("upload")

                    disp_res = format_video_caption_resolution(job.resolution, target_height)
                    tag_parts = [disp_res] if disp_res else []
                    if is_clip:
                        tag_parts.append(f"{format_caption_timestamp(job.clip_start)} ➔ {format_caption_timestamp(job.clip_end)}")
                    if is_muted:
                        tag_parts.append("Muted")
                    meta_str = " • ".join(tag_parts)
                    caption_text = f"🎬 <b>{job.title}</b>\n({meta_str})" if meta_str else f"🎬 <b>{job.title}</b>"

                    # Custom filename for video
                    clean_title = "".join(c for c in job.title if c.isalnum() or c in (" ", "_", "-")).strip() or "video"
                    short_title = clean_title[:45].strip()
                    fn_parts = []
                    if disp_res:
                        fn_parts.append(disp_res)
                    if is_clip:
                        fn_parts.append(f"clip_{format_clip_filename_tag(job.clip_start)}-{format_clip_filename_tag(job.clip_end)}")
                    if is_muted:
                        fn_parts.append("muted")
                    fn_tag = f"_[{'_'.join(fn_parts)}]" if fn_parts else ""
                    custom_video_filename = f"{short_title}{fn_tag}.mp4"

                    uploaded_msg_id, file_size = await self._send_media_to_cache(
                        bot=bot,
                        api_mode=api_mode,
                        channel_id=cache_channel_id,
                        local_file_path=output_file,
                        operation=job.operation,
                        job_id=job_id,
                        job_title=job.title,
                        custom_filename=custom_video_filename,
                        caption=caption_text,
                        extra_kwargs=extra_kwargs,
                    )

                    if not is_clip:
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
                    else:
                        cache_entry = CacheEntry(
                            telegram_channel_id=cache_channel_id,
                            telegram_message_id=uploaded_msg_id,
                        )

                    await notifier.stop_ticker()

                # ==========================================
                # PIPELINE: AUDIO (MP3) PROCESSING
                # ==========================================
                elif job.operation == OperationType.AUDIO.value or job.operation == "AUDIO":
                    is_audio_clip = (job.clip_start is not None and job.clip_end is not None)
                    job.status = JobStatus.DOWNLOADING.value
                    await session.commit()
                    notifier.start_ticker(initial_pct=5.0)

                    # Extract video metadata to get rich audio tags (artist, title, upload_date, etc.)
                    meta_info = {}
                    try:
                        meta_info = await YtDlpService.extract_metadata(job.canonical_url)
                    except Exception as meta_err:
                        logger.warning("Could not extract rich metadata for audio job %s: %s", job_id, meta_err)

                    if meta_info.get("is_live") or meta_info.get("live_status") == "is_live":
                        logger.warning("Audio job %s rejected: stream is live", job_id)
                        await notifier.stop_ticker()
                        job.status = JobStatus.FAILED.value
                        job.error_code = "LIVESTREAM_NOT_SUPPORTED"
                        job.error_message = "Livestreams are not supported."
                        await session.commit()
                        await notifier.update("❌", "Failed", "Livestreams are not supported.", force=True)
                        await QueueService.release_active_job(job_id, queue_type=job.queue_type)
                        await asyncio.to_thread(shutil.rmtree, job_dir, True)
                        if own_bot and bot is not None:
                            await bot.session.close()
                        return

                    audio_title, audio_artist, audio_album, audio_year, audio_duration = FFmpegService.resolve_audio_metadata(
                        info_dict=meta_info, fallback_title=job.title
                    )
                    safe_audio_filename = FFmpegService.generate_safe_audio_filename(artist=audio_artist, title=audio_title)

                    if is_audio_clip:
                        audio_duration = max(1, int(job.clip_end - job.clip_start))
                        base_fn, ext_fn = os.path.splitext(safe_audio_filename)
                        safe_audio_filename = f"{base_fn}_[clip_{format_clip_filename_tag(job.clip_start)}-{format_clip_filename_tag(job.clip_end)}]{ext_fn}"

                    audio_template = os.path.join(job_dir, "audio.%(ext)s")
                    ydl_opts = YtDlpService.get_download_opts()
                    ydl_opts.update({
                        "outtmpl": audio_template,
                        "format": "bestaudio/best",
                        "writethumbnail": True,
                    })
                    if is_audio_clip:
                        ydl_opts["download_ranges"] = yt_dlp.utils.download_range_func(
                            None, [(job.clip_start, job.clip_end)]
                        )

                    loop = asyncio.get_running_loop()
                    cancel_event_audio = threading.Event()

                    async def _cancel_watcher_audio():
                        while not cancel_event_audio.is_set():
                            if await QueueService.is_cancelled(job_id):
                                cancel_event_audio.set()
                                break
                            await asyncio.sleep(0.5)

                    def progress_hook_audio(d):
                        if cancel_event_audio.is_set():
                            raise yt_dlp.utils.DownloadCancelled("Job cancelled by user")
                        try:
                            if d.get("status") == "downloading":
                                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                                downloaded = d.get("downloaded_bytes", 0)
                                if total and total > 0:
                                    pct = (downloaded / total) * 100
                                elif d.get("fragment_count") and d.get("fragment_count") > 0:
                                    pct = (d.get("fragment_index", 0) / d.get("fragment_count")) * 100
                                else:
                                    pct = 50.0
                                speed = d.get("_speed_str", "")
                                eta = d.get("_eta_str", "")
                                notifier.on_download_progress(pct, speed=speed, eta=eta)
                        except yt_dlp.utils.DownloadCancelled:
                            raise
                        except Exception as pe:
                            logger.debug("Audio progress hook error: %s", pe)

                    ydl_opts["progress_hooks"] = [progress_hook_audio]

                    def _dl_audio():
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            ydl.download([job.canonical_url])

                    watcher_task_audio = asyncio.create_task(_cancel_watcher_audio())
                    try:
                        await loop.run_in_executor(None, _dl_audio)
                    except (yt_dlp.utils.DownloadCancelled, Exception) as dl_err:
                        if cancel_event_audio.is_set() or isinstance(dl_err, yt_dlp.utils.DownloadCancelled):
                            logger.info("Audio download aborted due to cancellation for job %s", job_id)
                            await notifier.stop_ticker()
                            job.status = JobStatus.CANCELLED.value
                            await session.commit()
                            await QueueService.release_active_job(job_id, queue_type=job.queue_type)
                            await asyncio.to_thread(shutil.rmtree, job_dir, True)
                            if own_bot and bot is not None:
                                await bot.session.close()
                            return
                        raise
                    finally:
                        watcher_task_audio.cancel()

                    notifier.set_stage("process", label="🎵 Processing audio...")

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
                    if not raw_cover_path or not os.path.exists(raw_cover_path):
                        raw_dl = os.path.join(job_dir, "raw_audio_cover.jpg")
                        try:
                            res = await ThumbnailService.download_full_thumbnail_file(
                                info=meta_info,
                                source_id=job.source_id,
                                output_path=raw_dl,
                            )
                            if res and os.path.exists(raw_dl):
                                raw_cover_path = raw_dl
                        except Exception as ce:
                            logger.debug("Could not pre-download cover artwork for audio: %s", ce)

                    FFmpegService.prepare_cover_image(raw_cover_path, cover_jpg_path)

                    job.status = JobStatus.PROCESSING.value
                    await session.commit()
                    notifier.set_stage("process", label="🎵 Processing audio...")

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
                    notifier.set_stage("upload")

                    tg_thumbnail = FSInputFile(cover_jpg_path) if os.path.exists(cover_jpg_path) else None

                    if is_audio_clip:
                        audio_caption = f"(MP3 • {format_caption_timestamp(job.clip_start)} ➔ {format_caption_timestamp(job.clip_end)})"
                    else:
                        audio_caption = "(MP3)"

                    uploaded_msg_id, file_size = await self._send_media_to_cache(
                        bot=bot,
                        api_mode=api_mode,
                        channel_id=cache_channel_id,
                        local_file_path=output_mp3,
                        operation=job.operation,
                        job_id=job_id,
                        job_title=audio_title,
                        custom_filename=safe_audio_filename,
                        caption=audio_caption,
                        performer=audio_artist,
                        duration=audio_duration,
                        thumbnail=tg_thumbnail,
                    )

                    if not is_audio_clip:
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
                    else:
                        cache_entry = CacheEntry(
                            telegram_channel_id=cache_channel_id,
                            telegram_message_id=uploaded_msg_id,
                        )

                    await notifier.stop_ticker()

                # ==========================================
                # PIPELINE: SUBTITLE PROCESSING
                # ==========================================
                elif job.operation == OperationType.SUBTITLE.value or job.operation == "SUBTITLE":
                    target_lang = job.subtitle_lang or "EN"
                    sub_template = os.path.join(job_dir, "subs.%(ext)s")
                    r = get_redis_client()
                    final_srt_content = None
                    persian_handled = False
                    notifier.start_ticker(initial_pct=5.0)
                    notifier.set_stage("download")

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
                            notifier.set_stage("process", label="🌐 Translating subtitles...")

                            async def _on_ai_progress(c_idx: int, total_c: int, attempt: int):
                                if total_c > 0:
                                    target = 65.0 + (c_idx / total_c) * 23.0
                                    notifier.target_pct = max(notifier.target_pct, target)
                                    notifier.current_pct = max(notifier.current_pct, target - 2.0)

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
                    notifier.set_stage("upload")

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
                    await notifier.stop_ticker()

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
                        bot, session, req.user_id, force_authoritative=False
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
                                    "Please re-join the channels below to receive your file."
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

                            status_msg_id = getattr(req, "status_message_id", None)
                            if not status_msg_id:
                                try:
                                    r = get_redis_client()
                                    val = await r.get(f"job_request:status_msg:{job_id}:{req.user_id}")
                                    if val:
                                        status_msg_id = int(val)
                                except Exception:
                                    pass

                            if status_msg_id:
                                try:
                                    await bot.delete_message(
                                        chat_id=req.chat_id,
                                        message_id=int(status_msg_id),
                                    )
                                except Exception as del_stat_err:
                                    logger.debug("Could not delete queue status message %s for user %s: %s", status_msg_id, req.user_id, del_stat_err)

                            # Delete keyboard message after sending video / delivering media
                            menu_msg_id = getattr(req, "menu_message_id", None)
                            if not menu_msg_id:
                                try:
                                    r = get_redis_client()
                                    val = await r.get(f"job_request:menu_msg:{job_id}:{req.user_id}")
                                    if val:
                                        menu_msg_id = int(val)
                                except Exception:
                                    pass

                            if menu_msg_id:
                                try:
                                    await bot.delete_message(
                                        chat_id=req.chat_id,
                                        message_id=int(menu_msg_id),
                                    )
                                except Exception as del_err:
                                    logger.debug("Could not delete keyboard message %s for user %s: %s", menu_msg_id, req.user_id, del_err)
                                try:
                                    r = get_redis_client()
                                    await r.delete(f"job_request:menu_msg:{job_id}:{req.user_id}")
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
                if notifier is not None:
                    try:
                        await notifier.stop_ticker()
                    except Exception:
                        pass

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
