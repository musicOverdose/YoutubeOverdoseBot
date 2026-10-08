from typing import List, Optional
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from src.models.channel import RequiredChannel


def build_quality_keyboard(
    source_id: str,
    available_heights: List[int],
    resolution_labels: Optional[dict] = None,
) -> InlineKeyboardMarkup:
    """
    QUALITY SELECTION MENU:
    Displays available video resolutions + MP3 + Subtitle + Cancel button.
    Supports friendly resolution labels (e.g. 1080p for 1080x1920 vertical videos).
    """
    keyboard: List[List[InlineKeyboardButton]] = []
    labels = resolution_labels or {}

    # Pair video resolutions two per row
    row: List[InlineKeyboardButton] = []
    for height in available_heights:
        label = labels.get(height, f"{height}p")
        btn = InlineKeyboardButton(
            text=f"🎬 {label}",
            callback_data=f"q:{source_id}:{height}",
        )
        row.append(btn)
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    # Audio & Subtitle utility row
    utility_row = [
        InlineKeyboardButton(text="🎵 MP3", callback_data=f"aud:{source_id}:MP3"),
        InlineKeyboardButton(text="💬 Subtitle", callback_data=f"sub_menu:{source_id}"),
    ]
    keyboard.append(utility_row)

    # Cancel request button
    keyboard.append([
        InlineKeyboardButton(text="❌ Cancel", callback_data="req_cancel"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def build_codec_keyboard(
    source_id: str, height: int, available_codecs: Optional[List[str]] = None
) -> InlineKeyboardMarkup:
    """
    SECOND STEP: CODEC SELECTION
    STRICT SOURCE-CODEC-ONLY:
    - If H.264 is available -> show H.264
    - If H.265 is available -> show H.265
    - If both available -> show both
    - If neither available -> show neither (only 'Back')
    If available_codecs is None, defaults to ["H264", "H265"] for backward compatibility.
    """
    codecs = available_codecs if available_codecs is not None else ["H264", "H265"]
    keyboard: List[List[InlineKeyboardButton]] = []

    if "H264" in codecs:
        keyboard.append([
            InlineKeyboardButton(
                text="🎬 H.264 / AAC",
                callback_data=f"c:{source_id}:H264:{height}",
            )
        ])
    if "H265" in codecs:
        keyboard.append([
            InlineKeyboardButton(
                text="📦 H.265 / AAC",
                callback_data=f"c:{source_id}:H265:{height}",
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            text="⬅️ Back",
            callback_data=f"back_q:{source_id}",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def build_subtitle_keyboard(
    source_id: str,
    has_english: bool,
    ai_available: bool,
    has_persian: bool = False,
) -> InlineKeyboardMarkup:
    """
    SUBTITLE MENU:
    - 🇬🇧 English appears when English subtitles are available.
    - 🇮🇷 Persian appears if native Persian subtitles exist OR (English exists AND AI is available).
    """
    keyboard: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []

    if has_english:
        row.append(InlineKeyboardButton(text="🇬🇧 English", callback_data=f"sub:{source_id}:EN"))

    if has_persian or (has_english and ai_available):
        row.append(InlineKeyboardButton(text="🇮🇷 Persian", callback_data=f"sub:{source_id}:FA"))

    if row:
        keyboard.append(row)

    keyboard.append([
        InlineKeyboardButton(text="⬅️ Back", callback_data=f"back_q:{source_id}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def build_must_join_keyboard(
    channels: List[RequiredChannel], resume_action: Optional[str] = None
) -> InlineKeyboardMarkup:
    """
    MUST-JOIN ENFORCEMENT UI:
    Shows one inline URL button per configured channel pointing to its public/invite URL.
    Channels without a usable URL are handled explicitly without creating broken buttons.
    Final callback button is strictly 'must_join:check'.
    """
    keyboard: List[List[InlineKeyboardButton]] = []

    for ch in channels:
        url = None
        if ch.invite_url and (ch.invite_url.strip().startswith("http://") or ch.invite_url.strip().startswith("https://")):
            url = ch.invite_url.strip()
        elif ch.username:
            clean_username = ch.username.strip().lstrip("@")
            if clean_username:
                url = f"https://t.me/{clean_username}"

        if url:
            keyboard.append([InlineKeyboardButton(text=ch.title, url=url)])
        else:
            keyboard.append([
                InlineKeyboardButton(
                    text=f"{ch.title} (No Link Configured)",
                    callback_data="must_join:no_url",
                )
            ])

    keyboard.append([
        InlineKeyboardButton(text="I Joined — Check Again", callback_data="must_join:check")
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def build_queue_status_keyboard(job_id: str) -> InlineKeyboardMarkup:
    """
    Interactive buttons on active/queued jobs:
    [📊 Queue Status]
    (No cancel button for active downloads per user constraint)
    """
    keyboard = [
        [
            InlineKeyboardButton(text="📊 Queue Status", callback_data=f"q_stat:{job_id}"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)
