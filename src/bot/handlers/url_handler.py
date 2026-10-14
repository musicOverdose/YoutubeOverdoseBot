import json
import os
import uuid
from typing import Optional
from aiogram import Bot, Router
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, FSInputFile, Message
from src.bot.handlers.base import get_or_create_user
from src.bot.keyboards import build_extraction_keyboard, build_quality_keyboard
from src.core.config import settings
from src.core.database import AsyncSessionLocal
from src.core.logger import setup_logger
from src.core.redis import get_redis_client
from src.services.must_join_service import MustJoinService
from src.services.setting_service import DEFAULT_UNSUPPORTED_LINK_MESSAGE, SettingService
from src.services.thumbnail_service import ThumbnailService
from src.services.ytdlp_service import YtDlpService, contains_url, extract_youtube_id, get_canonical_url

logger = setup_logger("url_handler")
url_router = Router()


async def process_youtube_url(
    bot: Bot, chat_id: int, canonical_url: str, source_id: str, user_id: Optional[int] = None
) -> None:
    """
    Extracts video metadata, validates duration, and sends the quality selection menu.
    Associates the extraction request with user_id and chat_id to allow cancelling.
    Can be called directly or resumed after Must-Join verification.
    """
    request_id = uuid.uuid4().hex[:12]
    ext_key = f"ytdl:ext_req:{request_id}"
    r = get_redis_client()
    try:
        await r.set(
            ext_key,
            json.dumps({"user_id": user_id, "chat_id": chat_id, "status": "active"}),
            ex=300,
        )
    except Exception as e:
        logger.debug("Failed to set extraction request in redis: %s", e)

    status_msg = await bot.send_message(
        chat_id=chat_id,
        text="🔎 <i>Extracting video information...</i>",
        reply_markup=build_extraction_keyboard(request_id),
    )
    try:
        info = await YtDlpService.extract_metadata(canonical_url)
    except Exception as e:
        logger.error(f"Failed to extract metadata for {canonical_url}: {e}")
        try:
            await status_msg.edit_text("❌ Failed to retrieve video details. Please ensure the link is valid and public.", reply_markup=None)
        except Exception:
            pass
        return

    # Check if request was abandoned during extraction
    try:
        raw_req = await r.get(ext_key)
        if raw_req:
            req_data = json.loads(raw_req)
            if req_data.get("status") == "cancelled":
                logger.info("Extraction request %s was cancelled by user. Aborting.", request_id)
                return
    except Exception:
        pass

    # Reject livestreams early with friendly notification
    if info.get("is_live") or info.get("live_status") == "is_live":
        try:
            await status_msg.edit_text("❌ Livestreams are not supported. Please provide a link to a recorded video.", reply_markup=None)
        except Exception:
            pass
        return

    # Usable resolutions and friendly display labels
    max_height = getattr(settings, "DEFAULT_MAX_HEIGHT", 2160)
    try:
        app_settings = await SettingService.get_application_settings()
        max_height = app_settings.get("default_max_height") or max_height
    except Exception:
        pass

    available_heights = YtDlpService.get_available_resolutions(info, max_height=max_height)
    resolution_labels = YtDlpService.get_resolution_labels(info)
    title = info.get("title", "YouTube Video")
    thumbnail_url = ThumbnailService.get_best_thumbnail_url(info) or info.get("thumbnail")

    keyboard = build_quality_keyboard(source_id, available_heights, resolution_labels=resolution_labels)

    is_vertical = (
        (info.get("height") or 0) > (info.get("width") or 0)
        or (info.get("aspect_ratio") or 1.0) < 0.95
        or "/shorts/" in canonical_url
    )

    temp_preview = os.path.join(settings.TEMP_DIR, f"prev_{uuid.uuid4().hex[:12]}.jpg")
    os.makedirs(settings.TEMP_DIR, exist_ok=True)
    try:
        preview_ready = await ThumbnailService.prepare_preview_photo(
            info=info,
            source_id=source_id,
            output_path=temp_preview,
            is_vertical=is_vertical,
        )

        # Check again if cancelled before sending photo
        try:
            raw_req = await r.get(ext_key)
            if raw_req:
                req_data = json.loads(raw_req)
                if req_data.get("status") == "cancelled":
                    logger.info("Extraction request %s was cancelled by user. Aborting before sending.", request_id)
                    return
        except Exception:
            pass

        if preview_ready and os.path.exists(temp_preview) and os.path.getsize(temp_preview) > 0:
            await bot.send_photo(
                chat_id=chat_id,
                photo=FSInputFile(temp_preview),
                caption=f"<b>{title}</b>",
                reply_markup=keyboard,
            )
            try:
                await status_msg.delete()
            except Exception:
                pass
            try:
                await r.delete(ext_key)
            except Exception:
                pass
            return
    except Exception as e:
        logger.warning(f"Could not send thumbnail photo: {e}. Falling back to text.")
    finally:
        if os.path.exists(temp_preview):
            try:
                os.remove(temp_preview)
            except OSError:
                pass

    # Final check if cancelled before text message
    try:
        raw_req = await r.get(ext_key)
        if raw_req:
            req_data = json.loads(raw_req)
            if req_data.get("status") == "cancelled":
                return
    except Exception:
        pass

    await bot.send_message(
        chat_id=chat_id,
        text=f"🎬 <b>{title}</b>",
        reply_markup=keyboard,
    )
    try:
        await status_msg.delete()
    except Exception:
        pass
    try:
        await r.delete(ext_key)
    except Exception:
        pass


@url_router.callback_query(lambda c: c.data and c.data.startswith("ext_cancel:"))
async def on_cancel_extraction(callback: CallbackQuery, bot: Bot):
    """
    Cancels an in-flight metadata extraction request and marks it abandoned.
    Prevents subsequent quality selection menu or job creation.
    """
    request_id = callback.data.split(":")[1]
    ext_key = f"ytdl:ext_req:{request_id}"
    r = get_redis_client()

    raw_req = None
    try:
        raw_req = await r.get(ext_key)
    except Exception as e:
        logger.debug("Error reading extraction key %s: %s", ext_key, e)

    if raw_req:
        try:
            req_data = json.loads(raw_req)
            expected_user = req_data.get("user_id")
            if expected_user and callback.from_user.id != expected_user:
                await callback.answer("You cannot cancel someone else's request.", show_alert=True)
                return
            req_data["status"] = "cancelled"
            await r.set(ext_key, json.dumps(req_data), ex=300)
        except Exception:
            pass
    else:
        try:
            await r.set(ext_key, json.dumps({"status": "cancelled", "user_id": callback.from_user.id}), ex=300)
        except Exception:
            pass

    try:
        await callback.message.edit_text("❌ <i>Request cancelled.</i>", reply_markup=None)
    except Exception:
        try:
            await callback.message.delete()
        except Exception:
            pass

    await callback.answer("Request cancelled.")


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
    await process_youtube_url(bot, message.chat.id, canonical_url, source_id, user_id=user_id)


@url_router.message(
    lambda msg: msg.text
    and not msg.text.startswith("/")
    and contains_url(msg.text, msg.entities)
    and extract_youtube_id(msg.text) is None
)
async def handle_unsupported_link(message: Message, bot: Bot):
    """
    Replies with an unsupported link error when user sends any URL that is
    not a valid YouTube link (ignoring plain text without URLs).
    """
    async with AsyncSessionLocal() as session:
        await get_or_create_user(session, message.from_user)

        # Centralized Must-Join Check
        allowed = await MustJoinService.enforce_must_join_message(
            message=message,
            bot=bot,
            session=session,
        )
        if not allowed:
            return

        try:
            err_text = await SettingService.get_unsupported_link_message(session)
        except Exception as e:
            logger.warning("Failed to load unsupported link message from DB: %s. Using default.", e)
            err_text = DEFAULT_UNSUPPORTED_LINK_MESSAGE

    try:
        await message.reply(err_text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning("Custom unsupported link message failed HTML parsing: %s. Falling back to default.", e)
        await message.reply(DEFAULT_UNSUPPORTED_LINK_MESSAGE, parse_mode=ParseMode.HTML)

