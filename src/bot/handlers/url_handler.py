import os
import uuid
from aiogram import Bot, Router
from aiogram.types import FSInputFile, Message
from src.bot.handlers.base import get_or_create_user
from src.bot.keyboards import build_quality_keyboard
from src.core.config import settings
from src.core.database import AsyncSessionLocal
from src.core.logger import setup_logger
from src.services.must_join_service import MustJoinService
from src.services.thumbnail_service import ThumbnailService
from src.services.ytdlp_service import YtDlpService, extract_youtube_id, get_canonical_url

logger = setup_logger("url_handler")
url_router = Router()


async def process_youtube_url(bot: Bot, chat_id: int, canonical_url: str, source_id: str) -> None:
    """
    Extracts video metadata, validates duration, and sends the quality selection menu.
    Can be called directly or resumed after Must-Join verification.
    """
    status_msg = await bot.send_message(chat_id=chat_id, text="🔎 <i>Extracting video information...</i>")
    try:
        info = await YtDlpService.extract_metadata(canonical_url)
    except Exception as e:
        logger.error(f"Failed to extract metadata for {canonical_url}: {e}")
        try:
            await status_msg.edit_text("❌ Failed to retrieve video details. Please ensure the link is valid and public.")
        except Exception:
            pass
        return

    # Usable resolutions and friendly display labels
    available_heights = YtDlpService.get_available_resolutions(info)
    resolution_labels = YtDlpService.get_resolution_labels(info)
    title = info.get("title", "YouTube Video")
    thumbnail_url = ThumbnailService.get_best_thumbnail_url(info) or info.get("thumbnail")

    try:
        await status_msg.delete()
    except Exception:
        pass

    keyboard = build_quality_keyboard(source_id, available_heights, resolution_labels=resolution_labels)

    is_vertical = (
        (info.get("height") or 0) > (info.get("width") or 0)
        or (info.get("aspect_ratio") or 1.0) < 0.95
        or "/shorts/" in canonical_url
    )

    if thumbnail_url:
        temp_preview = None
        try:
            if is_vertical:
                os.makedirs(settings.TEMP_DIR, exist_ok=True)
                temp_preview = os.path.join(settings.TEMP_DIR, f"prev_{uuid.uuid4().hex[:12]}.jpg")
                cropped_ok = await ThumbnailService.create_vertical_preview_photo(thumbnail_url, temp_preview)
                if cropped_ok and os.path.exists(temp_preview):
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=FSInputFile(temp_preview),
                        caption=f"<b>{title}</b>",
                        reply_markup=keyboard,
                    )
                    return

            await bot.send_photo(
                chat_id=chat_id,
                photo=thumbnail_url,
                caption=f"<b>{title}</b>",
                reply_markup=keyboard,
            )
            return
        except Exception as e:
            logger.warning(f"Could not send thumbnail photo: {e}. Falling back to text.")
        finally:
            if temp_preview and os.path.exists(temp_preview):
                try:
                    os.remove(temp_preview)
                except OSError:
                    pass

    await bot.send_message(
        chat_id=chat_id,
        text=f"🎬 <b>{title}</b>",
        reply_markup=keyboard,
    )


@url_router.message(lambda msg: msg.text and extract_youtube_id(msg.text) is not None)
async def handle_youtube_url(message: Message, bot: Bot):
    user_id = message.from_user.id
    raw_text = message.text.strip()
    source_id = extract_youtube_id(raw_text)
    if not source_id:
        return

    canonical_url = get_canonical_url(source_id)

    async with AsyncSessionLocal() as session:
        await get_or_create_user(session, message.from_user)

        # 1. CENTRALIZED MUST-JOIN CHECK (AUTHORITATIVE)
        allowed = await MustJoinService.enforce_must_join_message(
            message=message,
            bot=bot,
            session=session,
            pending_action={
                "type": "url",
                "payload": {"url": canonical_url, "source_id": source_id},
            },
        )
        if not allowed:
            return

    # 2. PROCEED WITH URL PROCESSING
    await process_youtube_url(bot, message.chat.id, canonical_url, source_id)
