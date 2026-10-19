from datetime import datetime, timezone
import hashlib
from typing import Optional, Tuple
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramAPIError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.config import settings
from src.core.constants import OperationType
from src.core.logger import setup_logger
from src.models.cache import CacheEntry

logger = setup_logger("cache_service")


class CacheService:
    @staticmethod
    def generate_cache_key(
        source_id: str,
        operation: str,
        codec: Optional[str] = None,
        height: Optional[int] = None,
        subtitle_lang: Optional[str] = None,
        clip_start: Optional[int] = None,
        clip_end: Optional[int] = None,
        is_muted: bool = False,
    ) -> str:
        """
        Generates deterministic, unique cache keys:
        - Video: {source_id}:{codec}:{height}p (with optional :clip_{start}_{end} and :muted)
        - MP3: {source_id}:MP3 (with optional :clip_{start}_{end})
        - English Subtitle: {source_id}:SUBTITLE:EN
        - Persian Subtitle: {source_id}:SUBTITLE:EN_TO_FA
        """
        if operation == OperationType.VIDEO.value or operation == "VIDEO":
            codec_str = codec.upper() if codec else "ANY"
            raw_key = f"{source_id}:{codec_str}:{height}p"
            if clip_start is not None and clip_end is not None:
                raw_key += f":clip_{clip_start}_{clip_end}"
            if is_muted:
                raw_key += ":muted"
        elif operation == OperationType.AUDIO.value or operation == "AUDIO":
            raw_key = f"{source_id}:MP3"
            if clip_start is not None and clip_end is not None:
                raw_key += f":clip_{clip_start}_{clip_end}"
        elif operation == OperationType.SUBTITLE.value or operation == "SUBTITLE":
            if subtitle_lang and subtitle_lang.upper() == "FA":
                raw_key = f"{source_id}:SUBTITLE:EN_TO_FA"
            else:
                raw_key = f"{source_id}:SUBTITLE:EN"
        else:
            raw_key = f"{source_id}:{operation}:{codec}:{height}"
            if clip_start is not None and clip_end is not None:
                raw_key += f":clip_{clip_start}_{clip_end}"
            if is_muted:
                raw_key += ":muted"

        # Hash internally for fixed-length deterministic key
        hashed = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        return hashed

    @classmethod
    async def get_cached_entry(
        cls, session: AsyncSession, cache_key: str
    ) -> Optional[CacheEntry]:
        """Looks up a valid cache entry in the database."""
        stmt = select(CacheEntry).where(
            CacheEntry.cache_key == cache_key,
            CacheEntry.is_valid == True,
        )
        res = await session.execute(stmt)
        return res.scalar_one_or_none()

    @classmethod
    async def deliver_cached_media(
        cls,
        bot: Bot,
        session: AsyncSession,
        cache_entry: CacheEntry,
        target_chat_id: int,
    ) -> Tuple[bool, Optional[str]]:
        """
        Delivers cached media directly from the private Telegram cache channel using copyMessage.
        Validates cache consistency: if Telegram message was deleted, invalidates cache in DB.
        """
        try:
            await bot.copy_message(
                chat_id=target_chat_id,
                from_chat_id=cache_entry.telegram_channel_id,
                message_id=cache_entry.telegram_message_id,
            )

            # Update hit count and timestamp if entry is persisted in DB
            if getattr(cache_entry, "id", None) is not None:
                await session.execute(
                    update(CacheEntry)
                    .where(CacheEntry.id == cache_entry.id)
                    .values(
                        hit_count=CacheEntry.hit_count + 1,
                        last_used_at=datetime.now(timezone.utc),
                    )
                )
                await session.commit()
            return True, None

        except TelegramBadRequest as e:
            err_msg = str(e).lower()
            if "message to copy not found" in err_msg or "chat not found" in err_msg:
                # Invalidate stale cache record if persisted
                if getattr(cache_entry, "id", None) is not None:
                    logger.warning(
                        f"Cache message {cache_entry.telegram_message_id} in channel {cache_entry.telegram_channel_id} not found. Invalidating cache {cache_entry.cache_key}"
                    )
                    await session.execute(
                        update(CacheEntry)
                        .where(CacheEntry.id == cache_entry.id)
                        .values(is_valid=False)
                    )
                    await session.commit()
                return False, "Cached Telegram message was removed. Re-generating..."
            return False, f"Telegram delivery error: {e}"

        except TelegramAPIError as e:
            logger.error(f"Telegram API error delivering cached message: {e}")
            return False, f"Telegram API error: {e}"

    @classmethod
    async def save_cache_entry(
        cls,
        session: AsyncSession,
        cache_key: str,
        source_id: str,
        title: str,
        operation: str,
        telegram_channel_id: int,
        telegram_message_id: int,
        codec: Optional[str] = None,
        resolution: Optional[str] = None,
        height: Optional[int] = None,
        subtitle_lang: Optional[str] = None,
        file_size: Optional[int] = None,
    ) -> CacheEntry:
        """Stores or updates a newly uploaded file in the cache table."""
        stmt = select(CacheEntry).where(CacheEntry.cache_key == cache_key)
        res = await session.execute(stmt)
        entry = res.scalar_one_or_none()

        now = datetime.now(timezone.utc)
        if entry is not None:
            entry.source_id = source_id
            entry.title = title
            entry.operation = operation
            entry.codec = codec
            entry.resolution = resolution
            entry.height = height
            entry.subtitle_lang = subtitle_lang
            entry.file_size = file_size
            entry.telegram_channel_id = telegram_channel_id
            entry.telegram_message_id = telegram_message_id
            entry.hit_count = 1
            entry.is_valid = True
            entry.last_used_at = now
        else:
            entry = CacheEntry(
                cache_key=cache_key,
                source_id=source_id,
                title=title,
                operation=operation,
                codec=codec,
                resolution=resolution,
                height=height,
                subtitle_lang=subtitle_lang,
                file_size=file_size,
                telegram_channel_id=telegram_channel_id,
                telegram_message_id=telegram_message_id,
                hit_count=1,
                is_valid=True,
                created_at=now,
                last_used_at=now,
            )
            session.add(entry)

        await session.commit()
        await session.refresh(entry)
        return entry

