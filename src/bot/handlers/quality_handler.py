import os
import uuid
from aiogram import Bot, Router
from aiogram.types import CallbackQuery, FSInputFile
from src.bot.handlers.codec_handler import enqueue_video_job, safe_callback_answer
from src.bot.keyboards import build_must_join_keyboard, build_quality_keyboard
from src.core.config import settings
from src.core.database import AsyncSessionLocal
from src.core.logger import setup_logger
from src.services.must_join_service import MustJoinService
from src.services.thumbnail_service import ThumbnailService
from src.services.ytdlp_service import YtDlpService, get_canonical_url

logger = setup_logger("quality_handler")
quality_router = Router()


@quality_router.callback_query(lambda c: c.data and c.data.startswith("q:"))
async def on_quality_selected(callback: CallbackQuery, bot: Bot):
    """
    1-TAP DOWNLOAD EXPERIENCE:
    Automatically selects the best available video codec for the chosen resolution
    and immediately queues or delivers the video without prompting for codec.
    """
    user_id = callback.from_user.id
    parts = callback.data.split(":")
    if len(parts) != 3:
        await safe_callback_answer(callback, "Invalid request.", show_alert=True)
        return

    _, source_id, height_str = parts
    try:
        height = int(height_str)
    except ValueError:
        await safe_callback_answer(callback, "Invalid resolution.", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        if not await MustJoinService.enforce_must_join_callback(callback, bot, session):
            return

    canonical_url = get_canonical_url(source_id)
    try:
        info = await YtDlpService.extract_metadata(canonical_url)
        codec = YtDlpService.auto_select_codec(info, height)
    except Exception as e:
        logger.error(f"Error inspecting metadata for {source_id} at {height}p: {e}")
        codec = "H264"
        info = None

    # Directly enqueue with auto-selected codec (1-tap flow)
    await enqueue_video_job(
        bot=bot,
        callback=callback,
        source_id=source_id,
        height=height,
        codec=codec,
        info=info,
    )


@quality_router.callback_query(lambda c: c.data == "req_cancel")
async def on_cancel_request(callback: CallbackQuery, bot: Bot):
    """
    Dismisses/cancels the unconfirmed request message preview.
    (Note: Active/queued downloads cannot be cancelled by users per design).
    """
    try:
        await callback.message.delete()
    except Exception as e:
        logger.debug(f"Could not delete request message on cancel: {e}. Editing caption.")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
            if callback.message.caption:
                await callback.message.edit_caption(caption="❌ <i>Request cancelled.</i>")
            elif callback.message.text:
                await callback.message.edit_text(text="❌ <i>Request cancelled.</i>")
        except Exception:
            pass

    await safe_callback_answer(callback, "Request cancelled.")


@quality_router.callback_query(lambda c: c.data and c.data.startswith("back_q:"))
async def on_back_to_quality(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    source_id = callback.data.split(":")[1]
    canonical_url = get_canonical_url(source_id)

    async with AsyncSessionLocal() as session:
        if not await MustJoinService.enforce_must_join_callback(callback, bot, session):
            return

    try:
        info = await YtDlpService.extract_metadata(canonical_url)
        available_heights = YtDlpService.get_available_resolutions(info)
        resolution_labels = YtDlpService.get_resolution_labels(info)
        keyboard = build_quality_keyboard(
            source_id, available_heights, resolution_labels=resolution_labels
        )
        await callback.message.edit_reply_markup(reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Error going back to quality menu: {e}")
        await safe_callback_answer(callback, "Could not refresh quality options.", show_alert=True)
        return

    await safe_callback_answer(callback)


@quality_router.callback_query(lambda c: c.data and c.data.startswith("thumb:"))
async def on_download_thumbnail(callback: CallbackQuery, bot: Bot):
    """
    Delivers the full-quality uncompressed YouTube thumbnail as a file/document,
    allowing users to save the exact original creator artwork without Telegram photo compression.
    """
    chat_id = callback.message.chat.id
    parts = callback.data.split(":")
    if len(parts) != 2:
        await safe_callback_answer(callback, "Invalid request.", show_alert=True)
        return

    source_id = parts[1]
    async with AsyncSessionLocal() as session:
        if not await MustJoinService.enforce_must_join_callback(callback, bot, session):
            return

    await safe_callback_answer(callback, "Downloading full quality thumbnail...")

    canonical_url = get_canonical_url(source_id)
    try:
        info = await YtDlpService.extract_metadata(canonical_url, use_cache=True)
    except Exception as e:
        logger.warning("Error fetching metadata for thumbnail download (%s): %s", source_id, e)
        info = None

    title = (info.get("title") if info else None) or f"YouTube_{source_id}"
    safe_title = "".join(c for c in title if c.isalnum() or c in (" ", "_", "-")).strip() or source_id
    filename = f"{safe_title[:60]}_thumbnail.jpg"

    os.makedirs(settings.TEMP_DIR, exist_ok=True)
    temp_file = os.path.join(settings.TEMP_DIR, f"full_thumb_{source_id}_{uuid.uuid4().hex[:8]}.jpg")

    try:
        res = await ThumbnailService.download_full_thumbnail_file(info, source_id, temp_file)
        if res and os.path.exists(temp_file) and os.path.getsize(temp_file) > 0:
            w, h = res[1], res[2]
            caption = (
                f"🖼 <b>{title}</b>\n"
                f"📐 Resolution: <code>{w}x{h}</code>\n"
                f"📦 <i>Full Quality (Uncompressed)</i>"
            )
            await bot.send_document(
                chat_id=chat_id,
                document=FSInputFile(temp_file, filename=filename),
                caption=caption,
            )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text="❌ Could not retrieve high-resolution thumbnail for this video.",
            )
    except Exception as e:
        logger.error("Error sending full thumbnail: %s", e)
        await bot.send_message(
            chat_id=chat_id,
            text="❌ Error sending thumbnail file. Please try again.",
        )
    finally:
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except OSError:
                pass

