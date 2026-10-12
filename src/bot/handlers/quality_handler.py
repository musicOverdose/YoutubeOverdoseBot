import asyncio
import html
import json
import os
import uuid
from typing import Optional, Set
from aiogram import Bot, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, LinkPreviewOptions, Message
from src.bot.handlers.audio_handler import enqueue_audio_job
from src.bot.handlers.codec_handler import enqueue_video_job, safe_callback_answer
from src.bot.keyboards import (
    build_advanced_options_keyboard,
    build_advanced_quality_keyboard,
    build_must_join_keyboard,
    build_quality_keyboard,
)
from src.core.config import settings
from src.core.database import AsyncSessionLocal
from src.core.logger import setup_logger
from src.core.redis import get_redis_client
from src.services.must_join_service import MustJoinService
from src.services.setting_service import SettingService
from src.services.thumbnail_service import ThumbnailService
from src.services.ytdlp_service import YtDlpService, get_canonical_url
from src.utils.timestamp_parser import format_timestamp, parse_timestamp_range

logger = setup_logger("quality_handler")
quality_router = Router()
_background_tasks: Set[asyncio.Task] = set()


class AdvancedClipStates(StatesGroup):
    waiting_for_timestamp = State()


ADV_STATE_TTL = 1800


async def get_adv_options(user_id: int, chat_id: int, source_id: str) -> dict:
    r = get_redis_client()
    key = f"advanced_options:{user_id}:{chat_id}:{source_id}"
    data = await r.get(key)
    if data:
        try:
            return json.loads(data if isinstance(data, str) else data.decode("utf-8"))
        except Exception:
            pass
    return {
        "clip_start": None,
        "clip_end": None,
        "is_muted": False,
        "mode": "video",
    }


async def set_adv_options(user_id: int, chat_id: int, source_id: str, data: dict) -> None:
    r = get_redis_client()
    key = f"advanced_options:{user_id}:{chat_id}:{source_id}"
    await r.set(key, json.dumps(data), ex=ADV_STATE_TTL)


async def clear_adv_options(user_id: int, chat_id: int, source_id: str) -> None:
    r = get_redis_client()
    key = f"advanced_options:{user_id}:{chat_id}:{source_id}"
    await r.delete(key)


def render_advanced_options_card(
    title: str,
    duration: int,
    clip_start: Optional[int],
    clip_end: Optional[int],
    is_muted: bool,
    mode: str,
) -> str:
    total_dur_str = format_timestamp(duration) if duration else "00:00"
    if clip_start is not None and clip_end is not None:
        clip_len = max(0, clip_end - clip_start)
        clip_range_str = f"{format_timestamp(clip_start)} - {format_timestamp(clip_end)} ({format_timestamp(clip_len)}) ✂️"
    else:
        clip_range_str = f"Full ({total_dur_str})"

    if mode == "mp3":
        audio_str = "N/A"
        mode_str = "MP3 💿"
    else:
        audio_str = "Muted 🔇" if is_muted else "Sound ON 🔊"
        mode_str = "Video 🎬"

    clean_title = html.escape(title[:55])
    return (
        f"🎛 <b>Advanced Options</b>\n"
        f"<b>{clean_title}</b> ({total_dur_str})\n\n"
        f"• <b>Clip:</b> {clip_range_str}\n"
        f"• <b>Audio:</b> {audio_str}\n"
        f"• <b>Mode:</b> {mode_str}"
    )


async def safe_edit_menu(message, text: str, reply_markup=None) -> None:
    try:
        if message.caption is not None:
            await message.edit_caption(caption=text, reply_markup=reply_markup)
        else:
            await message.edit_text(text=text, reply_markup=reply_markup)
    except Exception as e:
        logger.debug("Failed editing menu message: %s", e)



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
        max_height = getattr(settings, "DEFAULT_MAX_HEIGHT", 2160)
        try:
            app_settings = await SettingService.get_application_settings()
            max_height = app_settings.get("default_max_height") or max_height
        except Exception:
            pass
        available_heights = YtDlpService.get_available_resolutions(info, max_height=max_height)
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

    is_vertical = False
    target_ratio = None
    if info:
        w_meta = info.get("width") or 0
        h_meta = info.get("height") or 0
        is_vertical = (
            h_meta > w_meta
            or (info.get("aspect_ratio") or 1.0) < 0.95
            or "/shorts/" in canonical_url.lower()
        )
        if is_vertical and w_meta > 0 and h_meta > 0:
            target_ratio = w_meta / float(h_meta)
        elif is_vertical:
            target_ratio = 9 / 16.0
    elif "/shorts/" in canonical_url.lower():
        is_vertical = True
        target_ratio = 9 / 16.0

    title = (info.get("title") if info else None) or source_id or "thumbnail"
    safe_title = "".join(c for c in title if c.isalnum() or c in (" ", "_", "-")).strip() or source_id
    # Keep filename concise (max ~20-22 chars) to avoid awkward overflow in Telegram UI
    short_title = safe_title[:22].strip().rstrip("-_ ")
    if len(safe_title) > 22 and " " in short_title:
        last_space = short_title.rfind(" ")
        if last_space >= 8:
            short_title = short_title[:last_space].strip().rstrip("-_ ")
    filename = f"{short_title or source_id}_thumb.jpg"

    os.makedirs(settings.TEMP_DIR, exist_ok=True)
    temp_file = os.path.join(settings.TEMP_DIR, f"full_thumb_{source_id}_{uuid.uuid4().hex[:8]}.jpg")

    try:
        res = await ThumbnailService.download_full_thumbnail_file(
            info=info,
            source_id=source_id,
            output_path=temp_file,
            is_vertical=is_vertical,
            target_ratio=target_ratio,
        )
        if res and os.path.exists(temp_file) and os.path.getsize(temp_file) > 0:
            await bot.send_document(
                chat_id=chat_id,
                document=FSInputFile(temp_file, filename=filename),
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


@quality_router.callback_query(lambda c: c.data and c.data.startswith("desc:"))
async def on_show_description(callback: CallbackQuery, bot: Bot):
    chat_id = callback.message.chat.id
    parts = callback.data.split(":")
    if len(parts) != 2:
        await safe_callback_answer(callback, "Invalid request.", show_alert=True)
        return

    source_id = parts[1]
    async with AsyncSessionLocal() as session:
        if not await MustJoinService.enforce_must_join_callback(callback, bot, session):
            return

    await safe_callback_answer(callback, "Fetching description...")

    canonical_url = get_canonical_url(source_id)
    try:
        info = await YtDlpService.extract_metadata(canonical_url, use_cache=True)
    except Exception as e:
        logger.warning("Error fetching metadata for description (%s): %s", source_id, e)
        info = None

    raw_description = (info.get("description") if info else "") or ""
    description = raw_description.strip()

    if not description:
        await bot.send_message(
            chat_id=chat_id,
            text="ℹ️ <i>This video does not have a description.</i>",
        )
        return

    escaped = html.escape(description)
    no_preview = LinkPreviewOptions(is_disabled=True)

    # Chunking: Telegram max message length is 4096 chars.
    MAX_CHUNK_LEN = 3800
    if len(escaped) <= MAX_CHUNK_LEN:
        await bot.send_message(
            chat_id=chat_id,
            text=f"<blockquote expandable>{escaped}</blockquote>",
            link_preview_options=no_preview,
        )
        return

    lines = escaped.split("\n")
    chunks = []
    current_chunk = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1
        if current_len + line_len > MAX_CHUNK_LEN and current_chunk:
            chunks.append("\n".join(current_chunk))
            current_chunk = [line]
            current_len = line_len
        else:
            current_chunk.append(line)
            current_len += line_len

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    for chunk in chunks:
        if len(chunk) > MAX_CHUNK_LEN:
            for sub_i in range(0, len(chunk), MAX_CHUNK_LEN):
                sub_chunk = chunk[sub_i : sub_i + MAX_CHUNK_LEN]
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"<blockquote expandable>{sub_chunk}</blockquote>",
                    link_preview_options=no_preview,
                )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=f"<blockquote expandable>{chunk}</blockquote>",
                link_preview_options=no_preview,
            )


@quality_router.callback_query(lambda c: c.data and c.data.startswith("adv_menu:"))
async def on_advanced_menu(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    source_id = callback.data.split(":")[1]

    async with AsyncSessionLocal() as session:
        if not await MustJoinService.enforce_must_join_callback(callback, bot, session):
            return

    canonical_url = get_canonical_url(source_id)
    try:
        info = await YtDlpService.extract_metadata(canonical_url, use_cache=True)
        title = info.get("title", "YouTube Video")
        duration = int(info.get("duration") or 0)
    except Exception as e:
        logger.warning("Could not extract metadata for adv_menu: %s", e)
        title = "YouTube Video"
        duration = 0

    adv_opts = await get_adv_options(user_id, chat_id, source_id)
    card_text = render_advanced_options_card(
        title=title,
        duration=duration,
        clip_start=adv_opts.get("clip_start"),
        clip_end=adv_opts.get("clip_end"),
        is_muted=adv_opts.get("is_muted", False),
        mode=adv_opts.get("mode", "video"),
    )
    kb = build_advanced_options_keyboard(
        source_id=source_id,
        clip_start=adv_opts.get("clip_start"),
        clip_end=adv_opts.get("clip_end"),
        is_muted=adv_opts.get("is_muted", False),
        mode=adv_opts.get("mode", "video"),
    )
    await safe_edit_menu(callback.message, card_text, reply_markup=kb)
    await safe_callback_answer(callback)


@quality_router.callback_query(lambda c: c.data and c.data.startswith("adv_mute:"))
async def on_toggle_mute(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    source_id = callback.data.split(":")[1]

    async with AsyncSessionLocal() as session:
        if not await MustJoinService.enforce_must_join_callback(callback, bot, session):
            return

    adv_opts = await get_adv_options(user_id, chat_id, source_id)
    if adv_opts.get("mode") == "mp3":
        await safe_callback_answer(callback, "Mute is not applicable in MP3 mode.", show_alert=True)
        return

    new_mute = not adv_opts.get("is_muted", False)
    adv_opts["is_muted"] = new_mute
    await set_adv_options(user_id, chat_id, source_id, adv_opts)

    canonical_url = get_canonical_url(source_id)
    try:
        info = await YtDlpService.extract_metadata(canonical_url, use_cache=True)
        title = info.get("title", "YouTube Video")
        duration = int(info.get("duration") or 0)
    except Exception:
        title = "YouTube Video"
        duration = 0

    card_text = render_advanced_options_card(
        title=title,
        duration=duration,
        clip_start=adv_opts.get("clip_start"),
        clip_end=adv_opts.get("clip_end"),
        is_muted=new_mute,
        mode=adv_opts.get("mode", "video"),
    )
    kb = build_advanced_options_keyboard(
        source_id=source_id,
        clip_start=adv_opts.get("clip_start"),
        clip_end=adv_opts.get("clip_end"),
        is_muted=new_mute,
        mode=adv_opts.get("mode", "video"),
    )
    await safe_edit_menu(callback.message, card_text, reply_markup=kb)
    await safe_callback_answer(callback, "Mute: ON" if new_mute else "Mute: OFF")


@quality_router.callback_query(lambda c: c.data and c.data.startswith("adv_mode:"))
async def on_toggle_mode(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    source_id = callback.data.split(":")[1]

    async with AsyncSessionLocal() as session:
        if not await MustJoinService.enforce_must_join_callback(callback, bot, session):
            return

    adv_opts = await get_adv_options(user_id, chat_id, source_id)
    current_mode = adv_opts.get("mode", "video")
    new_mode = "mp3" if current_mode == "video" else "video"
    adv_opts["mode"] = new_mode
    await set_adv_options(user_id, chat_id, source_id, adv_opts)

    canonical_url = get_canonical_url(source_id)
    try:
        info = await YtDlpService.extract_metadata(canonical_url, use_cache=True)
        title = info.get("title", "YouTube Video")
        duration = int(info.get("duration") or 0)
    except Exception:
        title = "YouTube Video"
        duration = 0

    card_text = render_advanced_options_card(
        title=title,
        duration=duration,
        clip_start=adv_opts.get("clip_start"),
        clip_end=adv_opts.get("clip_end"),
        is_muted=adv_opts.get("is_muted", False),
        mode=new_mode,
    )
    kb = build_advanced_options_keyboard(
        source_id=source_id,
        clip_start=adv_opts.get("clip_start"),
        clip_end=adv_opts.get("clip_end"),
        is_muted=adv_opts.get("is_muted", False),
        mode=new_mode,
    )
    await safe_edit_menu(callback.message, card_text, reply_markup=kb)
    await safe_callback_answer(callback, f"Mode: {'MP3' if new_mode == 'mp3' else 'Video'}")


@quality_router.callback_query(lambda c: c.data and c.data.startswith("adv_undo:"))
async def on_reset_options(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    source_id = callback.data.split(":")[1]

    async with AsyncSessionLocal() as session:
        if not await MustJoinService.enforce_must_join_callback(callback, bot, session):
            return

    adv_opts = {
        "clip_start": None,
        "clip_end": None,
        "is_muted": False,
        "mode": "video",
    }
    await set_adv_options(user_id, chat_id, source_id, adv_opts)

    canonical_url = get_canonical_url(source_id)
    try:
        info = await YtDlpService.extract_metadata(canonical_url, use_cache=True)
        title = info.get("title", "YouTube Video")
        duration = int(info.get("duration") or 0)
    except Exception:
        title = "YouTube Video"
        duration = 0

    card_text = render_advanced_options_card(
        title=title,
        duration=duration,
        clip_start=None,
        clip_end=None,
        is_muted=False,
        mode="video",
    )
    kb = build_advanced_options_keyboard(
        source_id=source_id,
        clip_start=None,
        clip_end=None,
        is_muted=False,
        mode="video",
    )
    await safe_edit_menu(callback.message, card_text, reply_markup=kb)
    await safe_callback_answer(callback, "Options reset to default.")


@quality_router.callback_query(lambda c: c.data and c.data.startswith("adv_time:"))
async def on_prompt_timestamp_range(callback: CallbackQuery, bot: Bot, state: FSMContext):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    source_id = callback.data.split(":")[1]

    max_clip_duration = getattr(settings, "MAX_CLIP_DURATION_SECONDS", 900)
    async with AsyncSessionLocal() as session:
        if not await MustJoinService.enforce_must_join_callback(callback, bot, session):
            return
        try:
            app_settings = await SettingService.get_application_settings(session)
            max_clip_duration = app_settings.get("max_clip_duration_seconds", max_clip_duration)
        except Exception:
            pass

    canonical_url = get_canonical_url(source_id)
    try:
        info = await YtDlpService.extract_metadata(canonical_url, use_cache=True)
        duration = int(info.get("duration") or 0)
    except Exception:
        duration = 0

    max_clip_min = max(1, max_clip_duration // 60)
    prompt_msg = await callback.message.answer(
        "✂️ <b>Enter Clip Range</b>\n\n"
        "Format: <code>start - end</code>\n\n"
        "Examples:\n"
        "• <code>1:23 - 3:30</code>\n"
        "• <code>start - max</code>\n"
        "• <code>1:00 - max</code>\n\n"
        f"Max length: {max_clip_min} min\n"
        "/cancel"
    )

    await state.set_state(AdvancedClipStates.waiting_for_timestamp)
    await state.update_data(
        source_id=source_id,
        chat_id=chat_id,
        menu_message_id=callback.message.message_id,
        duration=duration,
        max_clip_duration=max_clip_duration,
        prompt_msg_id=prompt_msg.message_id,
    )
    await safe_callback_answer(callback)


@quality_router.message(AdvancedClipStates.waiting_for_timestamp)
async def on_receive_timestamp_range(message: Message, bot: Bot, state: FSMContext):
    data = await state.get_data()
    chat_id = message.chat.id
    user_id = message.from_user.id
    prompt_msg_id = data.get("prompt_msg_id")
    source_id = data.get("source_id")
    menu_message_id = data.get("menu_message_id")
    duration = data.get("duration", 0)
    max_clip_duration = data.get("max_clip_duration", getattr(settings, "MAX_CLIP_DURATION_SECONDS", 900))

    # Cancel check
    if message.text and message.text.strip().lower() in ("/cancel", "cancel"):
        if prompt_msg_id:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=prompt_msg_id)
            except Exception:
                pass
        try:
            await message.delete()
        except Exception:
            pass
        await state.clear()
        return

    ok, start_sec, end_sec, err = parse_timestamp_range(
        message.text or "",
        source_duration=duration,
        max_clip_duration=max_clip_duration,
    )

    # Auto-cleanup: delete prompt and user reply
    if prompt_msg_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=prompt_msg_id)
        except Exception:
            pass
    try:
        await message.delete()
    except Exception:
        pass

    if not ok:
        err_prompt = await message.answer(
            f"⚠️ {err}\n\n"
            "Examples:\n"
            "• <code>1:23 - 3:30</code>\n"
            "• <code>start - max</code>\n"
            "• <code>1:00 - max</code>\n\n"
            "/cancel"
        )
        await state.update_data(prompt_msg_id=err_prompt.message_id)
        return
    adv_opts = await get_adv_options(user_id, chat_id, source_id)
    adv_opts["clip_start"] = start_sec
    adv_opts["clip_end"] = end_sec
    await set_adv_options(user_id, chat_id, source_id, adv_opts)
    await state.clear()

    canonical_url = get_canonical_url(source_id)
    try:
        info = await YtDlpService.extract_metadata(canonical_url, use_cache=True)
        title = info.get("title", "YouTube Video")
    except Exception:
        title = "YouTube Video"

    card_text = render_advanced_options_card(
        title=title,
        duration=duration,
        clip_start=start_sec,
        clip_end=end_sec,
        is_muted=adv_opts.get("is_muted", False),
        mode=adv_opts.get("mode", "video"),
    )
    kb = build_advanced_options_keyboard(
        source_id=source_id,
        clip_start=start_sec,
        clip_end=end_sec,
        is_muted=adv_opts.get("is_muted", False),
        mode=adv_opts.get("mode", "video"),
    )
    try:
        await bot.edit_message_caption(
            chat_id=chat_id,
            message_id=menu_message_id,
            caption=card_text,
            reply_markup=kb,
        )
    except Exception:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=menu_message_id,
                text=card_text,
                reply_markup=kb,
            )
        except Exception as e:
            logger.warning("Could not edit menu message with new timestamp range: %s", e)

    # Inform user that timestamp has been set
    clip_len_str = format_timestamp(end_sec - start_sec)
    confirm_msg = await message.answer(
        f"✅ <b>Clip range set:</b> {format_timestamp(start_sec)} ➔ {format_timestamp(end_sec)} "
        f"<i>({clip_len_str})</i>\n"
        f"<i>Updated in menu above ⬆️</i>"
    )

    async def _auto_delete_confirm(msg_id: int):
        await asyncio.sleep(5.0)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception as e:
            logger.debug("Auto-delete timestamp confirmation failed (message may have been dismissed): %s", e)

    t = asyncio.create_task(_auto_delete_confirm(confirm_msg.message_id))
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)


@quality_router.callback_query(lambda c: c.data and c.data.startswith("adv_finish:"))
async def on_advanced_finish(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    source_id = callback.data.split(":")[1]

    async with AsyncSessionLocal() as session:
        if not await MustJoinService.enforce_must_join_callback(callback, bot, session):
            return

    adv_opts = await get_adv_options(user_id, chat_id, source_id)
    clip_start = adv_opts.get("clip_start")
    clip_end = adv_opts.get("clip_end")

    if adv_opts.get("mode") == "mp3":
        await clear_adv_options(user_id, chat_id, source_id)
        await enqueue_audio_job(
            bot=bot,
            callback=callback,
            source_id=source_id,
            clip_start=clip_start,
            clip_end=clip_end,
        )
        return

    canonical_url = get_canonical_url(source_id)
    try:
        info = await YtDlpService.extract_metadata(canonical_url, use_cache=True)
    except Exception as e:
        logger.error("Error fetching metadata in adv_finish: %s", e)
        info = None

    max_height = getattr(settings, "DEFAULT_MAX_HEIGHT", 2160)
    try:
        app_settings = await SettingService.get_application_settings()
        max_height = app_settings.get("default_max_height") or max_height
    except Exception:
        pass

    available_heights = YtDlpService.get_available_resolutions(info, max_height=max_height)
    resolution_labels = YtDlpService.get_resolution_labels(info)
    kb = build_advanced_quality_keyboard(
        source_id=source_id,
        available_heights=available_heights,
        resolution_labels=resolution_labels,
    )

    if clip_start is not None and clip_end is not None:
        range_str = f"{format_timestamp(clip_start)} - {format_timestamp(clip_end)} ✂️"
    else:
        range_str = "Full"
    sound_str = "Muted 🔇" if adv_opts.get("is_muted") else "Sound ON 🔊"

    text = (
        f"🎬 <b>Select Quality</b>\n\n"
        f"• <b>Clip:</b> {range_str}\n"
        f"• <b>Audio:</b> {sound_str}\n\n"
        f"Choose resolution:"
    )
    await safe_edit_menu(callback.message, text, reply_markup=kb)
    await safe_callback_answer(callback)


@quality_router.callback_query(lambda c: c.data and c.data.startswith("adv_q:"))
async def on_advanced_quality_selected(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
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

    adv_opts = await get_adv_options(user_id, chat_id, source_id)
    clip_start = adv_opts.get("clip_start")
    clip_end = adv_opts.get("clip_end")
    is_muted = adv_opts.get("is_muted", False)

    await clear_adv_options(user_id, chat_id, source_id)

    await enqueue_video_job(
        bot=bot,
        callback=callback,
        source_id=source_id,
        height=height,
        codec=codec,
        info=info,
        clip_start=clip_start,
        clip_end=clip_end,
        is_muted=is_muted,
    )


@quality_router.callback_query(lambda c: c.data and c.data.startswith("adv_back:"))
async def on_advanced_back_to_menu(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    source_id = callback.data.split(":")[1]

    async with AsyncSessionLocal() as session:
        if not await MustJoinService.enforce_must_join_callback(callback, bot, session):
            return

    await clear_adv_options(user_id, chat_id, source_id)

    canonical_url = get_canonical_url(source_id)
    try:
        info = await YtDlpService.extract_metadata(canonical_url, use_cache=True)
        title = info.get("title", "YouTube Video")
        max_height = getattr(settings, "DEFAULT_MAX_HEIGHT", 2160)
        try:
            app_settings = await SettingService.get_application_settings()
            max_height = app_settings.get("default_max_height") or max_height
        except Exception:
            pass
        available_heights = YtDlpService.get_available_resolutions(info, max_height=max_height)
        resolution_labels = YtDlpService.get_resolution_labels(info)
        kb = build_quality_keyboard(source_id, available_heights, resolution_labels=resolution_labels)

        if callback.message.caption:
            await callback.message.edit_caption(caption=f"<b>{title}</b>", reply_markup=kb)
        else:
            await callback.message.edit_text(text=f"🎬 <b>{title}</b>", reply_markup=kb)
    except Exception as e:
        logger.error(f"Error restoring main quality menu: %s", e)
        await safe_callback_answer(callback, "Could not refresh quality options.", show_alert=True)
        return

    await safe_callback_answer(callback)


