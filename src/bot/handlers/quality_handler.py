from aiogram import Bot, Router
from aiogram.types import CallbackQuery
from src.bot.handlers.codec_handler import enqueue_video_job
from src.bot.keyboards import build_must_join_keyboard, build_quality_keyboard
from src.core.database import AsyncSessionLocal
from src.core.logger import setup_logger
from src.services.must_join_service import MustJoinService
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
        await callback.answer("Invalid request.", show_alert=True)
        return

    _, source_id, height_str = parts
    try:
        height = int(height_str)
    except ValueError:
        await callback.answer("Invalid resolution.", show_alert=True)
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

    await callback.answer("Request cancelled.")


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
        await callback.answer("Could not refresh quality options.", show_alert=True)
        return

    await callback.answer()
