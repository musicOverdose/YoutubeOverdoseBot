import asyncio
import json
import os
import re
import socket
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union
from urllib.parse import urlparse

import aiohttp
from fastapi import HTTPException
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.database import AsyncSessionLocal, engine
from src.core.logger import get_logger
from src.core.redis import get_redis_client
from src.core.security import (
    decrypt_credential,
    encrypt_credential,
    ensure_directory,
    get_master_key,
    mask_secret,
    remove_local_bot_api_artifacts,
    remove_runtime_bot_token,
    remove_runtime_ready,
    verify_runtime_artifacts,
    write_local_bot_api_env,
    write_restart_trigger,
    write_runtime_bot_token,
    write_runtime_ready,
)
from src.models.setting import Setting
from src.models.telegram_migration import TelegramMigration

logger = get_logger("setting_service")

# Setting Keys
SETTING_BOT_TOKEN = "telegram_bot_token"
SETTING_API_ID = "telegram_api_id"
SETTING_API_HASH = "telegram_api_hash"
SETTING_API_MODE = "telegram_api_mode"
SETTING_CACHE_CHANNEL_ID = "telegram_cache_channel_id"
SETTING_CONFIG_VERSION = "telegram_config_version"
SETTING_MUST_JOIN_MSG = "must_join_message"
SETTING_WELCOME_MSG = "welcome_message"
SETTING_HELP_MSG = "help_message"

# Application Setting Keys
SETTING_MAX_VIDEO_FILE_SIZE_MB_LOCAL = "max_video_file_size_mb_local"
SETTING_MAX_VIDEO_FILE_SIZE_MB_CLOUD = "max_video_file_size_mb_cloud"
SETTING_MAX_VIDEO_DURATION_SECONDS = "max_video_duration_seconds"
SETTING_ALLOW_UNKNOWN_DURATION = "allow_unknown_duration"
SETTING_CACHE_HIT_BYPASSES_DURATION_LIMIT = "cache_hit_bypasses_duration_limit"
SETTING_MAX_CONCURRENT_PER_USER = "max_concurrent_per_user"
SETTING_MAX_QUEUED_PER_USER = "max_queued_per_user"
SETTING_MAX_TEMP_STORAGE_GB = "max_temp_storage_gb"
SETTING_DEFAULT_MAX_HEIGHT = "default_max_height"
SETTING_PLAYLISTS_ENABLED = "playlists_enabled"
SETTING_H264_ENABLED = "h264_enabled"
SETTING_H265_ENABLED = "h265_enabled"
SETTING_MP3_ENABLED = "mp3_enabled"
SETTING_SUBTITLES_ENABLED = "subtitles_enabled"

# AI Setting Keys
SETTING_AI_ENABLED = "ai_enabled"
SETTING_AI_PROVIDER = "ai_provider"
SETTING_AI_BASE_URL = "ai_base_url"
SETTING_AI_MODEL = "ai_model"
SETTING_AI_API_KEY = "ai_api_key"
SETTING_AI_MAX_CHUNKS = "ai_max_chunks"
SETTING_AI_CHUNK_SIZE = "ai_chunk_size"

# Additional Audited Runtime Setting Keys
SETTING_YTDLP_COOKIES_ENABLED = "ytdlp_cookies_enabled"
SETTING_YTDLP_PROXY = "ytdlp_proxy"
SETTING_MUST_JOIN_ENABLED = "must_join_enabled"
SETTING_MUST_JOIN_EXEMPT_USERS = "must_join_exempt_users"
SETTING_MAX_ACTIVE_JOBS = "max_active_jobs"

APP_SETTINGS_SPEC: Dict[str, Dict[str, Any]] = {
    SETTING_MAX_VIDEO_FILE_SIZE_MB_LOCAL: {"attr": "MAX_VIDEO_FILE_SIZE_MB_LOCAL", "type": int, "encrypted": False, "desc": "Max video file size (MB) for Local Bot API"},
    SETTING_MAX_VIDEO_FILE_SIZE_MB_CLOUD: {"attr": "MAX_VIDEO_FILE_SIZE_MB_CLOUD", "type": int, "encrypted": False, "desc": "Max video file size (MB) for Cloud Bot API"},
    SETTING_MAX_VIDEO_DURATION_SECONDS: {"attr": "MAX_VIDEO_DURATION_SECONDS", "type": int, "encrypted": False, "desc": "Max video duration in seconds"},
    SETTING_ALLOW_UNKNOWN_DURATION: {"attr": "ALLOW_UNKNOWN_DURATION", "type": bool, "encrypted": False, "desc": "Allow unknown video duration"},
    SETTING_CACHE_HIT_BYPASSES_DURATION_LIMIT: {"attr": "CACHE_HIT_BYPASSES_DURATION_LIMIT", "type": bool, "encrypted": False, "desc": "Cache hit bypasses duration limit"},
    SETTING_MAX_CONCURRENT_PER_USER: {"attr": "MAX_CONCURRENT_PER_USER", "type": int, "encrypted": False, "desc": "Max concurrent processing jobs per user"},
    SETTING_MAX_QUEUED_PER_USER: {"attr": "MAX_QUEUED_PER_USER", "type": int, "encrypted": False, "desc": "Max queued jobs per user"},
    SETTING_MAX_TEMP_STORAGE_GB: {"attr": "MAX_TEMP_STORAGE_GB", "type": int, "encrypted": False, "desc": "Max temporary storage in GB"},
    SETTING_DEFAULT_MAX_HEIGHT: {"attr": "DEFAULT_MAX_HEIGHT", "type": int, "encrypted": False, "desc": "Default max video height"},
    SETTING_PLAYLISTS_ENABLED: {"attr": "PLAYLISTS_ENABLED", "type": bool, "encrypted": False, "desc": "Enable YouTube playlists"},
    SETTING_H264_ENABLED: {"attr": "H264_ENABLED", "type": bool, "encrypted": False, "desc": "H264 codec enabled"},
    SETTING_H265_ENABLED: {"attr": "H265_ENABLED", "type": bool, "encrypted": False, "desc": "H265 codec enabled"},
    SETTING_MP3_ENABLED: {"attr": "MP3_ENABLED", "type": bool, "encrypted": False, "desc": "MP3 audio extraction enabled"},
    SETTING_SUBTITLES_ENABLED: {"attr": "SUBTITLES_ENABLED", "type": bool, "encrypted": False, "desc": "Subtitle extraction enabled"},
    SETTING_AI_ENABLED: {"attr": "AI_ENABLED", "type": bool, "encrypted": False, "desc": "AI subtitle translation enabled"},
    SETTING_AI_PROVIDER: {"attr": "AI_PROVIDER", "type": str, "encrypted": False, "desc": "AI subtitle translation provider"},
    SETTING_AI_BASE_URL: {"attr": "AI_BASE_URL", "type": str, "encrypted": False, "desc": "AI subtitle translation API base URL"},
    SETTING_AI_MODEL: {"attr": "AI_MODEL", "type": str, "encrypted": False, "desc": "AI subtitle translation model name"},
    SETTING_AI_API_KEY: {"attr": "AI_API_KEY", "type": str, "encrypted": True, "desc": "AI subtitle translation API key (encrypted)"},
    SETTING_AI_MAX_CHUNKS: {"attr": "AI_MAX_CHUNKS", "type": int, "encrypted": False, "desc": "Max chunks for AI subtitle translation"},
    SETTING_AI_CHUNK_SIZE: {"attr": "AI_CHUNK_SIZE", "type": int, "encrypted": False, "desc": "Subtitle segment chunk size for AI translation"},
    SETTING_YTDLP_COOKIES_ENABLED: {"attr": "YTDLP_COOKIES_ENABLED", "type": bool, "encrypted": False, "desc": "YouTube cookies enabled"},
    SETTING_YTDLP_PROXY: {"attr": "YTDLP_PROXY", "type": str, "encrypted": False, "desc": "Proxy URL for yt-dlp"},
    SETTING_MUST_JOIN_ENABLED: {"attr": "MUST_JOIN_ENABLED", "type": bool, "encrypted": False, "desc": "Must-join channels enforcement enabled"},
    SETTING_MUST_JOIN_EXEMPT_USERS: {"attr": "MUST_JOIN_EXEMPT_USERS", "type": str, "encrypted": False, "desc": "Must-join exempt user IDs and usernames"},
    SETTING_MAX_ACTIVE_JOBS: {"attr": "MAX_ACTIVE_JOBS", "type": int, "encrypted": False, "desc": "Max active worker processing jobs"},
}


def _cast_setting_value(val: Any, target_type: type) -> Any:
    if target_type is bool:
        if isinstance(val, bool):
            return val
        return str(val).lower() in ("true", "1", "yes", "t")
    elif target_type is int:
        return int(val)
    elif target_type is str:
        return str(val) if val is not None else ""
    return val


def _serialize_setting_value(val: Any) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    return str(val)


DEFAULT_MUST_JOIN_MESSAGE = (
    "👋 Hello {first_name}!\n\n"
    "To use this bot, you must join our channel(s) first:\n"
    "{channel_list}\n\n"
    "After joining, please send your link again!"
)

DEFAULT_WELCOME_MESSAGE = (
    "👋 Hello, <b>{first_name}</b>!\n\n"
    "Welcome to <b>Youtube Overdose</b> — your lightning-fast YouTube & Shorts media downloader.\n\n"
    "✨ <b>Features:</b>\n"
    "• 🎬 <b>Crisp Video:</b> 144p up to 4K with auto-optimized codec delivery\n"
    "• 📱 <b>Shorts & Reels:</b> True vertical aspect ratio & high-res covers\n"
    "• 🎵 <b>Studio Audio:</b> High-bitrate MP3 with full ID3 tags & cover art\n"
    "• 💬 <b>Subtitles:</b> Dual English & Persian subtitle tracks\n"
    "• ⚡ <b>Clean Chat:</b> Fast downloads with automatic progress cleanup\n\n"
    "Just paste any YouTube link below to start!"
)

DEFAULT_HELP_MESSAGE = (
    "📖 <b>How to use Youtube Overdose:</b>\n\n"
    "1. 🔗 <b>Send a Link:</b> Paste any YouTube video or Shorts URL in the chat.\n"
    "2. 🎛 <b>Select Quality:</b> Choose your preferred resolution (144p–4K), 🎵 MP3 audio, or 💬 Subtitles.\n"
    "3. ⚡ <b>Instant Delivery:</b> The bot delivers the media with original aspect ratio and cleans up progress messages.\n"
    "4. ❌ <b>Cancel Anytime:</b> Use the Cancel button on the menu to dismiss unwanted requests.\n\n"
    "💡 <i>Tip: Send /start anytime to view the main welcome menu.</i>"
)


class TelegramHTMLValidator(HTMLParser):
    ALLOWED_TAGS = {
        "b", "strong",
        "i", "em",
        "u", "ins",
        "s", "strike", "del",
        "span", "tg-spoiler",
        "a",
        "tg-emoji",
        "tg-time",
        "code",
        "pre",
        "blockquote",
    }
    NO_ATTR_TAGS = {
        "b", "strong",
        "i", "em",
        "u", "ins",
        "s", "strike", "del",
        "tg-spoiler",
    }
    INLINE_TAGS = {
        "b", "strong",
        "i", "em",
        "u", "ins",
        "s", "strike", "del",
        "span", "tg-spoiler",
        "a",
        "tg-emoji",
        "tg-time",
        "code",
    }

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack = []
        self.errors = []
        self.in_pre = False
        self.code_in_pre = False

    def handle_starttag(self, tag, attrs):
        lower_tag = tag.lower()
        if lower_tag not in self.ALLOWED_TAGS:
            self.errors.append(f"Tag <{tag}> is not supported by Telegram HTML.")
            return

        # Pre nesting rules
        if self.in_pre:
            if self.code_in_pre:
                self.errors.append(f"Tags cannot be nested inside code block: <{tag}>.")
            elif lower_tag != "code":
                self.errors.append(f"Tag <{tag}> cannot be nested inside <pre>.")

        if lower_tag == "pre":
            if any(parent in self.INLINE_TAGS or parent == "pre" for parent in self.stack):
                self.errors.append(f"<pre> cannot be nested inside <{self.stack[-1]}>.")
            self.in_pre = True

        if lower_tag == "code" and self.in_pre:
            self.code_in_pre = True

        # Blockquote nesting rules
        if lower_tag == "blockquote":
            if any(parent in self.INLINE_TAGS or parent == "pre" for parent in self.stack):
                self.errors.append(f"<blockquote> cannot be nested inside inline tag <{self.stack[-1]}>.")

        attrs_dict = dict(attrs)

        # Attribute validation
        if lower_tag in self.NO_ATTR_TAGS:
            if attrs:
                self.errors.append(f"Tag <{tag}> does not allow attributes.")
        elif lower_tag == "span":
            if attrs_dict.get("class") != "tg-spoiler":
                self.errors.append("Tag <span> must have class=\"tg-spoiler\".")
            for attr_name in attrs_dict:
                if attr_name != "class":
                    self.errors.append(f"Tag <span> does not support attribute '{attr_name}'.")
        elif lower_tag == "a":
            if "href" not in attrs_dict or not attrs_dict["href"].strip():
                self.errors.append("Tag <a> requires a non-empty 'href' attribute.")
            for attr_name in attrs_dict:
                if attr_name != "href":
                    self.errors.append(f"Tag <a> does not support attribute '{attr_name}'.")
        elif lower_tag == "tg-emoji":
            if "emoji-id" not in attrs_dict or not attrs_dict["emoji-id"].strip():
                self.errors.append("Tag <tg-emoji> requires a non-empty 'emoji-id' attribute.")
            for attr_name in attrs_dict:
                if attr_name != "emoji-id":
                    self.errors.append(f"Tag <tg-emoji> does not support attribute '{attr_name}'.")
        elif lower_tag == "tg-time":
            if "unix" not in attrs_dict or not str(attrs_dict["unix"]).strip():
                self.errors.append("Tag <tg-time> requires a non-empty 'unix' attribute.")
            else:
                try:
                    int(str(attrs_dict["unix"]).strip())
                except ValueError:
                    self.errors.append("Tag <tg-time> requires a numeric 'unix' timestamp attribute.")
            for attr_name in attrs_dict:
                if attr_name not in ("unix", "format"):
                    self.errors.append(f"Tag <tg-time> does not support attribute '{attr_name}'.")
        elif lower_tag == "blockquote":
            for attr_name in attrs_dict:
                if attr_name != "expandable":
                    self.errors.append(f"Tag <blockquote> does not support attribute '{attr_name}'.")
        elif lower_tag == "pre":
            for attr_name in attrs_dict:
                if attr_name != "class":
                    self.errors.append(f"Tag <pre> does not support attribute '{attr_name}'.")
        elif lower_tag == "code":
            if self.in_pre:
                for attr_name in attrs_dict:
                    if attr_name != "class":
                        self.errors.append(f"Tag <code> does not support attribute '{attr_name}'.")
            else:
                if attrs:
                    self.errors.append("Inline <code> tag does not allow attributes.")

        self.stack.append(lower_tag)

    def handle_endtag(self, tag):
        lower_tag = tag.lower()
        if lower_tag not in self.ALLOWED_TAGS:
            self.errors.append(f"Closing tag </{tag}> is not supported by Telegram HTML.")
            return

        if not self.stack:
            self.errors.append(f"Unmatched closing tag </{tag}>.")
            return

        top = self.stack.pop()
        if top != lower_tag:
            self.errors.append(f"Mismatched closing tag: expected </{top}>, got </{tag}>.")

        if lower_tag == "code" and self.in_pre:
            self.code_in_pre = False
        elif lower_tag == "pre":
            self.in_pre = False
            self.code_in_pre = False


def validate_telegram_html(text: str) -> tuple[bool, str]:
    """
    Validate that text contains only Telegram-supported HTML tags,
    valid attributes, and balanced nesting.
    Returns (is_valid, error_message).
    """
    if not text or not text.strip():
        return False, "Message cannot be empty."

    # Temporarily substitute valid {first_name} placeholder before parsing
    test_text = text.replace("{first_name}", "User")
    validator = TelegramHTMLValidator()
    try:
        validator.feed(test_text)
        validator.close()
    except Exception as e:
        return False, f"Malformed HTML: {e}"

    if validator.errors:
        return False, "; ".join(validator.errors)

    if validator.stack:
        unclosed = ", ".join(f"<{t}>" for t in reversed(validator.stack))
        return False, f"Unclosed HTML tags: {unclosed}"

    return True, ""


class TelegramConfigurationLock:
    """
    Acquires PostgreSQL session-level advisory lock using SELECT pg_try_advisory_lock(73541629)
    on a dedicated connection. If the lock cannot be acquired immediately, raises HTTP 409 Conflict.
    The dedicated connection remains open until the context manager exits.
    Autobegin is explicitly ended via rollback() immediately after lock query to preserve
    clean transaction boundaries for subsequent Short Transactions.
    For non-PostgreSQL dialects (e.g. SQLite in test suite), an in-memory lock is simulated.
    """
    ADVISORY_LOCK_ID = 73541629
    _simulated_locks = set()

    def __init__(self):
        self.conn = None
        self.locked = False

    async def __aenter__(self):
        self.conn = await engine.connect()
        if self.conn.dialect.name == "postgresql":
            res = await self.conn.scalar(
                text(f"SELECT pg_try_advisory_lock({self.ADVISORY_LOCK_ID})")
            )
            self.locked = bool(res)
            # End implicit autobegin transaction while preserving the session-level advisory lock
            await self.conn.rollback()
        else:
            # Simulated advisory lock for test environments
            if self.ADVISORY_LOCK_ID in TelegramConfigurationLock._simulated_locks:
                self.locked = False
            else:
                TelegramConfigurationLock._simulated_locks.add(self.ADVISORY_LOCK_ID)
                self.locked = True

        if not self.locked:
            await self.conn.close()
            self.conn = None
            raise HTTPException(
                status_code=409,
                detail="Another Telegram configuration mutation is already running. Please try again shortly.",
            )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.conn is not None:
            try:
                if self.locked:
                    if self.conn.dialect.name == "postgresql":
                        await self.conn.scalar(
                            text(f"SELECT pg_advisory_unlock({self.ADVISORY_LOCK_ID})")
                        )
                        await self.conn.rollback()
                    else:
                        TelegramConfigurationLock._simulated_locks.discard(self.ADVISORY_LOCK_ID)
            except Exception as e:
                logger.warning("Error releasing advisory lock %s: %s", self.ADVISORY_LOCK_ID, e)
            finally:
                await self.conn.close()
                self.conn = None
                self.locked = False


class SettingService:
    LOCAL_API_POLL_TIMEOUT: float = 20.0
    LOCAL_API_POLL_INTERVAL: float = 1.0

    @staticmethod
    def derive_endpoint(mode: str, base_url: Optional[str] = None) -> str:
        if mode == "cloud":
            return "https://api.telegram.org"
        base = (base_url or getattr(settings, "TELEGRAM_API_BASE_URL", "") or "").rstrip("/")
        if not base or base == "https://api.telegram.org":
            return "http://telegram-bot-api:8081"
        return base

    @classmethod
    def get_max_video_file_size_mb(cls, api_mode: str) -> int:
        """Returns safe maximum video file size limit in MB for the given API mode."""
        if api_mode == "local":
            return getattr(settings, "MAX_VIDEO_FILE_SIZE_MB_LOCAL", 1900)
        return getattr(settings, "MAX_VIDEO_FILE_SIZE_MB_CLOUD", 48)

    @classmethod
    async def get_active_api_mode(cls, session: Optional[AsyncSession] = None) -> str:
        """Resolves current active Telegram API mode ('local' or 'cloud')."""
        try:
            r = get_redis_client()
            cached = await r.get("telegram:active:mode")
            if cached:
                return cached.decode("utf-8") if isinstance(cached, bytes) else str(cached)
        except Exception:
            pass

        if session:
            try:
                stmt = select(Setting.value).where(Setting.key == SETTING_API_MODE, Setting.status == "ACTIVE")
                res = await session.execute(stmt)
                val = res.scalar_one_or_none()
                if val:
                    return str(val)
            except Exception:
                pass

        return getattr(settings, "TELEGRAM_API_MODE", "cloud")

    # --------------------------------------------------------------------------
    # Database Settings Retrieval
    # --------------------------------------------------------------------------

    @staticmethod
    async def get_active_setting(key: str, session: Optional[AsyncSession] = None) -> Optional[str]:
        """Fetch active setting value, decrypting if encrypted."""
        own_session = session is None
        sess = session or AsyncSessionLocal()
        try:
            stmt = select(Setting).where(Setting.key == key, Setting.status == "ACTIVE")
            result = await sess.execute(stmt)
            item = result.scalar_one_or_none()
            if not item:
                return None
            if item.is_encrypted:
                master_key = await get_master_key(sess)
                return decrypt_credential(item.value, master_key)
            return item.value
        finally:
            if own_session:
                await sess.close()

    @staticmethod
    async def get_all_active(session: Optional[AsyncSession] = None) -> Dict[str, str]:
        """Fetch all ACTIVE settings as a dictionary with decrypted values."""
        own_session = session is None
        sess = session or AsyncSessionLocal()
        try:
            stmt = select(Setting).where(Setting.status == "ACTIVE")
            result = await sess.execute(stmt)
            items = result.scalars().all()
            res = {}
            master_key = None
            for item in items:
                if item.is_encrypted:
                    if master_key is None:
                        master_key = await get_master_key(sess)
                    res[item.key] = decrypt_credential(item.value, master_key)
                else:
                    res[item.key] = item.value
            return res
        finally:
            if own_session:
                await sess.close()

    @staticmethod
    async def get_migration_state(session: AsyncSession) -> Tuple[str, Optional[datetime], Optional[str]]:
        """Retrieve the singleton migration state from telegram_migrations."""
        stmt = select(TelegramMigration).where(TelegramMigration.id == 1)
        res = await session.execute(stmt)
        row = res.scalar_one_or_none()
        if not row:
            row = TelegramMigration(id=1, state="IDLE")
            session.add(row)
            await session.flush()
        return row.state, row.logout_attempted_at, row.details

    @staticmethod
    async def set_migration_state(
        session: AsyncSession,
        state: str,
        logout_attempted_at: Optional[datetime] = None,
        details: Optional[str] = None,
    ) -> None:
        """Update the singleton migration state in telegram_migrations."""
        stmt = select(TelegramMigration).where(TelegramMigration.id == 1)
        res = await session.execute(stmt)
        row = res.scalar_one_or_none()
        if not row:
            row = TelegramMigration(
                id=1,
                state=state,
                logout_attempted_at=logout_attempted_at,
                details=details,
            )
            session.add(row)
        else:
            row.state = state
            row.logout_attempted_at = logout_attempted_at
            row.details = details
        await session.flush()

    # --------------------------------------------------------------------------
    # Telegram Configuration Read API
    # --------------------------------------------------------------------------

    @classmethod
    async def get_telegram_config(cls, session: Optional[AsyncSession] = None) -> Dict[str, Any]:
        """Return sanitized Telegram configuration for Web Admin."""
        all_active = await cls.get_all_active(session)
        bot_token = all_active.get(SETTING_BOT_TOKEN)
        mode = all_active.get(SETTING_API_MODE) or getattr(settings, "TELEGRAM_API_MODE", "cloud")
        cache_channel_id = all_active.get(SETTING_CACHE_CHANNEL_ID)
        version_str = all_active.get(SETTING_CONFIG_VERSION) or "1"

        return {
            "mode": mode,
            "derived_endpoint": cls.derive_endpoint(mode),
            "is_configured": bool(bot_token),
            "bot_token_masked": mask_secret(bot_token),
            "has_bot_token": bool(bot_token),
            "cache_channel_id": int(cache_channel_id) if (cache_channel_id and (cache_channel_id.isdigit() or cache_channel_id.startswith("-"))) else None,
            "config_version": int(version_str) if version_str.isdigit() else 1,
        }

    # --------------------------------------------------------------------------
    # Probes and Validation
    # --------------------------------------------------------------------------

    @staticmethod
    async def probe_tcp(host: str, port: int, timeout_sec: float = 3.0) -> bool:
        """Verify TCP socket connectivity."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=timeout_sec,
            )
            writer.close()
            await writer.wait_closed()
            return True
        except Exception as e:
            logger.debug("TCP probe to %s:%s failed: %s", host, port, e)
            return False

    @classmethod
    async def probe_get_me(cls, token: str, endpoint: str) -> Tuple[bool, Optional[Dict[str, Any]], str]:
        """
        Execute sanitized getMe probe against derived endpoint.
        Returns: (success, user_dict, error_message)
        """
        url = f"{endpoint.rstrip('/')}/bot{token}/getMe"
        sanitized_url = f"{endpoint.rstrip('/')}/bot<REDACTED>/getMe"
        logger.info("Executing getMe probe to %s", sanitized_url)

        try:
            async with aiohttp.ClientSession() as client:
                async with client.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    data = await resp.json()
                    if resp.status == 200 and data.get("ok"):
                        return True, data.get("result"), ""
                    err = data.get("description") or f"HTTP {resp.status}"
                    logger.warning("getMe probe to %s failed: %s", sanitized_url, err)
                    return False, None, err
        except asyncio.TimeoutError:
            return False, None, "Connection timed out"
        except Exception as e:
            logger.warning("getMe probe to %s encountered error: %s", sanitized_url, e)
            return False, None, str(e)

    @classmethod
    async def poll_local_api_readiness(
        cls,
        token: str,
        endpoint: str,
        timeout_sec: Optional[float] = None,
        interval_sec: Optional[float] = None,
    ) -> Tuple[bool, Optional[Dict[str, Any]], str]:
        """
        Poll Local Bot API readiness bounded by timeout_sec.
        Extracts host and port from endpoint, probes TCP connectivity,
        and executes sanitized getMe probe.
        Returns: (success, user_dict, error_message)
        """
        eff_timeout = timeout_sec if timeout_sec is not None else cls.LOCAL_API_POLL_TIMEOUT
        eff_interval = interval_sec if interval_sec is not None else cls.LOCAL_API_POLL_INTERVAL

        parsed = urlparse(endpoint)
        probe_host = parsed.hostname or "telegram-bot-api"
        probe_port = parsed.port or 8081

        start_time = asyncio.get_running_loop().time()
        deadline = start_time + eff_timeout
        last_error = ""

        logger.info(
            "Polling Local Bot API readiness on %s:%s (deadline %.1fs)...",
            probe_host,
            probe_port,
            eff_timeout,
        )

        while asyncio.get_running_loop().time() < deadline:
            tcp_ok = await cls.probe_tcp(probe_host, probe_port, timeout_sec=2.0)
            if tcp_ok:
                get_me_ok, bot_info, err = await cls.probe_get_me(token, endpoint)
                if get_me_ok:
                    logger.info("Local Bot API readiness confirmed via getMe.")
                    return True, bot_info, ""
                last_error = f"getMe probe failed: {err}"
            else:
                last_error = f"TCP connectivity probe to Local Bot API ({probe_host}:{probe_port}) failed"

            await asyncio.sleep(eff_interval)

        return False, None, f"Local Bot API readiness timeout after {eff_timeout:.0f}s: {last_error}"

    @classmethod
    async def probe_cache_channel(
        cls,
        token: str,
        endpoint: str,
        channel_id: int,
        bot_id: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """
        Strict 5-step cache channel validation:
        1. getChat(chat_id)
        2. verify result.type == "channel"
        3. getChatMember(chat_id, bot_id)
        4. verify status == creator OR administrator
        5. when administrator, require can_post_messages == true
        Do NOT use sendChatAction.
        """
        base_url = endpoint.rstrip('/')

        if bot_id is None:
            ok, bot_info, err = await cls.probe_get_me(token, endpoint)
            if not ok or not bot_info or "id" not in bot_info:
                return False, f"Cannot verify channel permissions without bot identity: {err}"
            bot_id = bot_info["id"]

        get_chat_url = f"{base_url}/bot{token}/getChat"
        get_member_url = f"{base_url}/bot{token}/getChatMember"

        try:
            async with aiohttp.ClientSession() as client:
                # 1. getChat
                async with client.post(get_chat_url, json={"chat_id": channel_id}, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    chat_data = await resp.json()
                    if resp.status != 200 or not chat_data.get("ok"):
                        err = chat_data.get("description") or f"HTTP {resp.status}"
                        return False, f"getChat failed: {err}"
                    chat_result = chat_data.get("result", {})

                # 2. verify result.type == "channel"
                chat_type = chat_result.get("type")
                if chat_type != "channel":
                    return False, f"Chat is not a channel (type is '{chat_type}')"

                # 3. getChatMember(bot_id)
                async with client.post(get_member_url, json={"chat_id": channel_id, "user_id": bot_id}, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    member_data = await resp.json()
                    if resp.status != 200 or not member_data.get("ok"):
                        err = member_data.get("description") or f"HTTP {resp.status}"
                        return False, f"getChatMember failed: {err}"
                    member_result = member_data.get("result", {})

                # 4. verify status == creator OR administrator
                status = member_result.get("status")
                if status not in ("creator", "administrator"):
                    return False, f"Bot is not creator or administrator of channel (status is '{status}')"

                # 5. when administrator, require can_post_messages == true
                if status == "administrator":
                    if not member_result.get("can_post_messages"):
                        return False, "Bot administrator does not have 'can_post_messages' permission in channel"

                return True, ""
        except Exception as e:
            return False, str(e)

    # --------------------------------------------------------------------------
    # Staged Telegram Configuration Update with Non-Blocking Advisory Lock
    # --------------------------------------------------------------------------

    @classmethod
    async def save_telegram_config(
        cls,
        candidate_mode: str,
        bot_token: Optional[str] = None,
        api_id: Optional[Union[int, str]] = None,
        api_hash: Optional[str] = None,
        cache_channel_id: Optional[Union[int, str]] = None,
        admin_username: str = "admin",
        ip_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Staged, atomic Telegram configuration mutation protected by pg_try_advisory_lock.
        The bot connects as a pure client to either Cloud Bot API or Centralized Local Bot API.
        """
        candidate_mode = candidate_mode.lower().strip()
        if candidate_mode not in ("local", "cloud"):
            raise HTTPException(status_code=400, detail="Mode must be either 'local' or 'cloud'.")

        async with TelegramConfigurationLock() as lock:
            conn = lock.conn
            master_key = await get_master_key()

            # Verify no migration workflow is active in PostgreSQL
            async with conn.begin():
                async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                    mig_state, _, _ = await cls.get_migration_state(session)
                    if mig_state != "IDLE":
                        raise HTTPException(
                            status_code=409,
                            detail=f"Cannot change Telegram configuration: migration in progress (state: {mig_state}).",
                        )

                    stmt = select(Setting).where(Setting.status == "ACTIVE")
                    res = await session.execute(stmt)
                    active_items = {s.key: s for s in res.scalars().all()}

            existing_token = (
                (decrypt_credential(active_items[SETTING_BOT_TOKEN].value, master_key) if active_items[SETTING_BOT_TOKEN].is_encrypted else active_items[SETTING_BOT_TOKEN].value)
                if SETTING_BOT_TOKEN in active_items
                else None
            )
            existing_channel_id = (
                active_items[SETTING_CACHE_CHANNEL_ID].value
                if SETTING_CACHE_CHANNEL_ID in active_items
                else None
            )

            # Resolve effective candidate values (retain existing if not provided)
            target_token = bot_token.strip() if bot_token and bot_token.strip() else existing_token
            if not target_token:
                raise HTTPException(status_code=400, detail="Bot API Token is required.")
            if not re.match(r"^\d+:[\w-]{20,}$", target_token):
                raise HTTPException(status_code=400, detail="Invalid Telegram Bot Token format.")

            target_channel = str(cache_channel_id).strip() if cache_channel_id is not None else existing_channel_id

            # ------------------------------------------------------------------
            # Short Transaction 1: Create or update PENDING records in PostgreSQL
            # ------------------------------------------------------------------
            async with conn.begin():
                async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                    await session.execute(delete(Setting).where(Setting.status == "PENDING"))

                    pending_records = [
                        Setting(
                            key=SETTING_BOT_TOKEN,
                            status="PENDING",
                            value=encrypt_credential(target_token, master_key),
                            is_encrypted=True,
                            description="Encrypted Telegram Bot Token",
                        ),
                        Setting(
                            key=SETTING_API_MODE,
                            status="PENDING",
                            value=candidate_mode,
                            is_encrypted=False,
                            description="Telegram Bot API Mode (local or cloud)",
                        ),
                    ]
                    if target_channel:
                        pending_records.append(
                            Setting(
                                key=SETTING_CACHE_CHANNEL_ID,
                                status="PENDING",
                                value=str(target_channel),
                                is_encrypted=False,
                                description="Telegram Cache Channel ID",
                            )
                        )

                    session.add_all(pending_records)
                    await session.flush()

            validation_passed = False
            validation_error = ""
            bot_info = None
            target_endpoint = cls.derive_endpoint(candidate_mode)

            # ------------------------------------------------------------------
            # Candidate Validation Execution
            # ------------------------------------------------------------------
            try:
                if candidate_mode == "local":
                    ready_ok, bot_info, err = await cls.poll_local_api_readiness(
                        target_token, target_endpoint
                    )
                    if not ready_ok:
                        raise ValueError(err)
                else:
                    get_me_ok, bot_info, err = await cls.probe_get_me(target_token, target_endpoint)
                    if not get_me_ok:
                        raise ValueError(f"Bot API getMe probe failed: {err}")

                if target_channel:
                    try:
                        cid = int(target_channel)
                        chan_ok, chan_err = await cls.probe_cache_channel(
                            target_token, target_endpoint, cid, bot_id=bot_info.get("id")
                        )
                        if not chan_ok:
                            raise ValueError(f"Cache channel validation failed: {chan_err}")
                    except ValueError as ve:
                        raise ValueError(f"Invalid cache channel: {ve}")

                validation_passed = True

            except Exception as ex:
                validation_passed = False
                validation_error = str(ex)
                logger.error("Telegram candidate validation failed: %s", validation_error)

                # Delete PENDING from PostgreSQL
                async with conn.begin():
                    async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                        await session.execute(delete(Setting).where(Setting.status == "PENDING"))

                raise HTTPException(
                    status_code=400,
                    detail=f"Candidate Telegram configuration rejected: {validation_error}. Active configuration remains unchanged.",
                )

            # Unlink READY before TX2
            remove_runtime_ready()

            # ------------------------------------------------------------------
            # Short Transaction 2: Promote PENDING to ACTIVE
            # ------------------------------------------------------------------
            new_version = 1
            async with conn.begin():
                async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                    v_stmt = select(Setting).where(Setting.key == SETTING_CONFIG_VERSION, Setting.status == "ACTIVE")
                    v_res = await session.execute(v_stmt)
                    v_item = v_res.scalar_one_or_none()
                    if v_item and v_item.value.isdigit():
                        new_version = int(v_item.value) + 1

                    p_stmt = select(Setting).where(Setting.status == "PENDING")
                    p_res = await session.execute(p_stmt)
                    pending_items = p_res.scalars().all()

                    keys_to_update = [item.key for item in pending_items]
                    if keys_to_update:
                        await session.execute(
                            delete(Setting).where(Setting.status == "ACTIVE", Setting.key.in_(keys_to_update))
                        )

                    await session.execute(
                        update(Setting).where(Setting.status == "PENDING").values(status="ACTIVE")
                    )

                    await session.execute(
                        delete(Setting).where(Setting.status == "ACTIVE", Setting.key == SETTING_CONFIG_VERSION)
                    )
                    session.add(
                        Setting(
                            key=SETTING_CONFIG_VERSION,
                            status="ACTIVE",
                            value=str(new_version),
                            is_encrypted=False,
                            description="Telegram Configuration Version",
                        )
                    )
                    await session.flush()

            # ------------------------------------------------------------------
            # Reconstruct Runtime Artifacts & Recreate READY
            # ------------------------------------------------------------------
            write_runtime_bot_token(target_token)

            if not verify_runtime_artifacts(candidate_mode):
                logger.critical("Runtime artifacts failed disk verification after TX2 promotion!")
                raise RuntimeError("Runtime artifacts failed verification on disk. READY will NOT be created.")

            # Update Redis mirror
            try:
                r = get_redis_client()
                await r.set("telegram:active:mode", candidate_mode)
                await r.set("telegram:active:config_version", str(new_version))
                if target_channel:
                    await r.set("telegram:active:cache_channel_id", str(target_channel))
                await r.publish(
                    "telegram:config:reload",
                    json.dumps({"version": new_version, "mode": candidate_mode}),
                )
            except Exception as e:
                logger.warning("Failed to publish config reload event to Redis: %s", e)

            # Atomically recreate READY
            write_runtime_ready()

            # Audit log
            try:
                from src.services.audit_service import AuditService
                async with AsyncSessionLocal() as audit_sess:
                    await AuditService.log_action(
                        audit_sess,
                        "TELEGRAM_CONFIG_UPDATE",
                        admin_username,
                        f"Updated Telegram config to mode={candidate_mode}, version={new_version}",
                        ip_address=ip_address,
                    )
            except Exception as e:
                logger.warning("Could not write audit log: %s", e)

            return {
                "status": "success",
                "message": f"Telegram configuration successfully validated and activated in {candidate_mode} mode.",
                "mode": candidate_mode,
                "config_version": new_version,
                "bot_username": bot_info.get("username") if bot_info else None,
            }

    # --------------------------------------------------------------------------
    # Startup State Reconciliation
    # --------------------------------------------------------------------------

    @classmethod
    async def reconcile_startup_state(cls) -> None:
        """
        Runs on Web Admin startup:
        1. Ensures directory permissions (2770 for runtime/bot-api, 0755 for transfer/tmp).
        2. Inspects telegram_migrations singleton workflow table.
        3. Discards stale PENDING records in PostgreSQL.
        4. Loads ACTIVE configuration strictly from PostgreSQL (NO .env fallback).
        5. Reconstructs /config/runtime/bot-token (0640, group ytdl-runtime GID 1001).
        6. Reconstructs /config/bot-api/local-bot-api.env (0640, group 101).
        7. Verifies runtime artifacts on disk.
        8. Updates Redis mirror (mode, config_version, cache_channel_id).
        9. Atomically creates /config/state/READY (0644).
        """
        logger.info("Starting startup state reconciliation...")
        try:
            ensure_directory(Path(settings.MASTER_KEY_FILE).parent, mode=0o700)
            ensure_directory(Path(settings.RUNTIME_BOT_TOKEN_FILE).parent, mode=0o2770, group="ytdl-runtime")
            if hasattr(settings, "LOCAL_BOT_API_ENV_FILE"):
                ensure_directory(Path(settings.LOCAL_BOT_API_ENV_FILE).parent, mode=0o2770, group=101)
            ensure_directory(Path(settings.RUNTIME_READY_FILE).parent, mode=0o755)
            if hasattr(settings, "TRANSFER_DIR"):
                ensure_directory(settings.TRANSFER_DIR, mode=0o755)
            ensure_directory(settings.TEMP_DIR, mode=0o755)
        except Exception as e:
            logger.warning("Directory initialization warning: %s", e)

        async with AsyncSessionLocal() as session:
            # Check migration workflow state
            try:
                mig_state, logout_time, details = await cls.get_migration_state(session)
                if mig_state in ("CLOUD_LOGOUT_UNKNOWN", "LOCAL_VALIDATION_FAILED"):
                    logger.critical(
                        "Migration state is %s (attempted: %s, details: %s). "
                        "System halted; operator resolution required. READY will NOT be created.",
                        mig_state, logout_time, details
                    )
                    remove_runtime_ready()
                    return

                if mig_state == "CLOUD_LOGOUT_IN_PROGRESS":
                    logger.critical(
                        "Startup recovery: Found migration in CLOUD_LOGOUT_IN_PROGRESS. "
                        "Transitioning to CLOUD_LOGOUT_UNKNOWN. READY will NOT be created."
                    )
                    await cls.set_migration_state(
                        session,
                        "CLOUD_LOGOUT_UNKNOWN",
                        logout_attempted_at=datetime.now(timezone.utc),
                        details="Process restarted while CLOUD_LOGOUT_IN_PROGRESS",
                    )
                    await session.commit()
                    remove_runtime_ready()
                    return

                if mig_state == "CLOUD_LOGOUT_SUCCEEDED":
                    active_settings = await cls.get_all_active(session)
                    token = active_settings.get(SETTING_BOT_TOKEN)
                    if token:
                        probe_ok, _, err = await cls.probe_get_me(token, cls.derive_endpoint("local"))
                        if probe_ok:
                            await cls.set_migration_state(session, "IDLE")
                            await session.commit()
                        else:
                            await cls.set_migration_state(session, "LOCAL_VALIDATION_FAILED", details=err)
                            await session.commit()
                            remove_runtime_ready()
                            return
            except Exception as e:
                logger.error("Error checking migration state during startup: %s", e)

            # Discard stale PENDING records
            try:
                await session.execute(delete(Setting).where(Setting.status == "PENDING"))
                await session.commit()
            except Exception as e:
                logger.warning("Could not purge stale PENDING records: %s", e)
                await session.rollback()

            # Load ACTIVE configuration strictly from PostgreSQL
            try:
                active_settings = await cls.get_all_active(session)
            except Exception as e:
                logger.error("Failed to load ACTIVE settings: %s", e)
                active_settings = {}

            # Load persistent application settings into runtime config
            try:
                await cls.load_all_settings_to_runtime(session)
            except Exception as e:
                logger.warning("Could not load application settings into runtime during reconciliation: %s", e)

        bot_token = active_settings.get(SETTING_BOT_TOKEN)
        api_mode = active_settings.get(SETTING_API_MODE) or "local"
        api_id = active_settings.get(SETTING_API_ID)
        api_hash = active_settings.get(SETTING_API_HASH)
        config_version = active_settings.get(SETTING_CONFIG_VERSION) or "1"
        cache_channel_id = active_settings.get(SETTING_CACHE_CHANNEL_ID)

        # If PostgreSQL has no ACTIVE bot token, do NOT synthesize from .env; keep READY absent
        if not bot_token:
            logger.warning(
                "No ACTIVE Telegram bot token in PostgreSQL database. "
                "Telegram service is unconfigured. /config/state/READY will NOT be created."
            )
            remove_runtime_ready()
            return

        # Reconstruct /config/runtime/bot-token (mode 0640, group ytdl-runtime GID 1001)
        try:
            write_runtime_bot_token(bot_token)
            logger.info("Reconstructed /config/runtime/bot-token (mode 0640, group ytdl-runtime)")
        except Exception as e:
            logger.error("Failed to write runtime bot token: %s", e)
            remove_runtime_ready()
            return

        # Reconstruct /config/bot-api/local-bot-api.env if local mode and credentials present
        if api_mode == "local" and api_id and api_hash:
            try:
                write_local_bot_api_env(api_id, api_hash)
                logger.info("Reconstructed /config/bot-api/local-bot-api.env (mode 0640, group 101)")
            except Exception as e:
                logger.error("Failed to write local-bot-api.env: %s", e)

        # Verify runtime artifacts on disk
        if not verify_runtime_artifacts(api_mode):
            logger.error("Runtime artifacts failed disk verification on startup. READY will NOT be created.")
            remove_runtime_ready()
            return

        # Populate Redis mirror
        try:
            r = get_redis_client()
            await r.set("telegram:active:mode", api_mode)
            await r.set("telegram:active:config_version", config_version)
            if cache_channel_id:
                await r.set("telegram:active:cache_channel_id", cache_channel_id)
        except Exception as e:
            logger.warning("Could not synchronize Redis cache during startup: %s", e)

        # Atomically create /config/state/READY (0644)
        try:
            write_runtime_ready()
            logger.info("Created /config/state/READY (mode 0644). Startup state reconciled.")
        except Exception as e:
            logger.error("Failed to write /config/state/READY: %s", e)

    # --------------------------------------------------------------------------
    # Explicit Mode Migrations
    # --------------------------------------------------------------------------

    @classmethod
    async def migrate_mode(
        cls,
        target_mode: str,
        admin_username: str = "admin",
        ip_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Execute explicit mode migration between Cloud and Local Bot API.
        Enforces consumer halting before Cloud logOut(), uses telegram_migrations table,
        and classifies outcomes with strict CLOUD_LOGOUT_UNKNOWN handling.
        """
        target_mode = target_mode.lower().strip()
        if target_mode not in ("local", "cloud"):
            raise HTTPException(status_code=400, detail="Invalid target mode. Must be 'local' or 'cloud'.")

        async with TelegramConfigurationLock() as lock:
            conn = lock.conn
            master_key = await get_master_key()

            async with conn.begin():
                async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                    mig_state, logout_time, details = await cls.get_migration_state(session)
                    if mig_state not in ("IDLE", "LOCAL_VALIDATION_FAILED"):
                        raise HTTPException(
                            status_code=409,
                            detail=f"Migration already in state '{mig_state}'. Operator resolution required.",
                        )

                    stmt = select(Setting).where(Setting.status == "ACTIVE")
                    res = await session.execute(stmt)
                    items = {s.key: s for s in res.scalars().all()}

            current_mode = items.get(SETTING_API_MODE).value if SETTING_API_MODE in items else "local"
            bot_token = (
                (decrypt_credential(items[SETTING_BOT_TOKEN].value, master_key) if items[SETTING_BOT_TOKEN].is_encrypted else items[SETTING_BOT_TOKEN].value)
                if SETTING_BOT_TOKEN in items
                else None
            )
            if not bot_token:
                raise HTTPException(status_code=400, detail="Cannot migrate without an active Bot Token in PostgreSQL.")

            if current_mode == target_mode and target_mode == "local":
                # Local -> Local reload
                logger.info("Executing Local -> Local session reload...")
                write_restart_trigger()
                await asyncio.sleep(1.0)
                probe_ok, _, err = await cls.probe_get_me(bot_token, cls.derive_endpoint("local"))
                if not probe_ok:
                    raise HTTPException(status_code=500, detail=f"Local Bot API reload verification failed: {err}")
                return {"status": "success", "message": "Local Bot API reloaded successfully.", "mode": "local"}

            if current_mode == target_mode:
                return {"status": "success", "message": f"Mode is already {target_mode}.", "mode": target_mode}

            target_endpoint = cls.derive_endpoint(target_mode)
            if target_mode == "local":
                probe_ok, bot_info, err = await cls.poll_local_api_readiness(bot_token, target_endpoint)
                if not probe_ok:
                    raise HTTPException(
                        status_code=502,
                        detail=f"Local Bot API verification failed: {err}. Ensure the centralized Local Bot API server is running.",
                    )
            else:
                probe_ok, bot_info, err = await cls.probe_get_me(bot_token, target_endpoint)
                if not probe_ok:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Cloud Bot API verification failed: {err}.",
                    )

            remove_runtime_ready()
            new_version = 1
            async with conn.begin():
                async with AsyncSession(bind=conn, expire_on_commit=False) as session:
                    v_stmt = select(Setting).where(Setting.key == SETTING_CONFIG_VERSION, Setting.status == "ACTIVE")
                    v_res = await session.execute(v_stmt)
                    v_item = v_res.scalar_one_or_none()
                    if v_item and v_item.value.isdigit():
                        new_version = int(v_item.value) + 1

                    await session.execute(
                        update(Setting)
                        .where(Setting.key == SETTING_API_MODE, Setting.status == "ACTIVE")
                        .values(value=target_mode)
                    )
                    await session.execute(
                        delete(Setting).where(Setting.key == SETTING_CONFIG_VERSION, Setting.status == "ACTIVE")
                    )
                    session.add(
                        Setting(
                            key=SETTING_CONFIG_VERSION,
                            status="ACTIVE",
                            value=str(new_version),
                            is_encrypted=False,
                            description="Telegram Configuration Version",
                        )
                    )
                    await session.execute(delete(Setting).where(Setting.status == "PENDING"))
                    await cls.set_migration_state(session, "IDLE")

            write_runtime_bot_token(bot_token)
            verify_runtime_artifacts(target_mode)

            try:
                r = get_redis_client()
                await r.set("telegram:active:mode", target_mode)
                await r.set("telegram:active:config_version", str(new_version))
                await r.publish("telegram:config:reload", json.dumps({"mode": target_mode, "version": new_version}))
            except Exception as re:
                logger.warning("Redis sync error: %s", re)

            write_runtime_ready()
            return {"status": "success", "message": f"Successfully switched mode to {target_mode}.", "mode": target_mode}

    # --------------------------------------------------------------------------
    # Must-Join Customizable Message Management
    # --------------------------------------------------------------------------

    @classmethod
    async def get_must_join_message(cls, session: Optional[AsyncSession] = None) -> str:
        """Get the active Must-Join template message."""
        custom_msg = await cls.get_active_setting(SETTING_MUST_JOIN_MSG, session)
        return custom_msg if custom_msg else DEFAULT_MUST_JOIN_MESSAGE

    @classmethod
    async def save_must_join_message(
        cls,
        message: str,
        session: Optional[AsyncSession] = None,
        admin_username: str = "admin",
    ) -> str:
        """Save a custom Must-Join message template."""
        clean_msg = message.strip()
        if not clean_msg:
            raise HTTPException(status_code=400, detail="Must-Join message cannot be empty.")

        is_valid, err = validate_telegram_html(clean_msg)
        if not is_valid:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid Telegram HTML formatting: {err}",
            )

        own_session = session is None
        sess = session or AsyncSessionLocal()
        try:
            stmt = select(Setting).where(Setting.key == SETTING_MUST_JOIN_MSG, Setting.status == "ACTIVE")
            res = await sess.execute(stmt)
            item = res.scalar_one_or_none()
            if item:
                item.value = clean_msg
            else:
                sess.add(
                    Setting(
                        key=SETTING_MUST_JOIN_MSG,
                        status="ACTIVE",
                        value=clean_msg,
                        is_encrypted=False,
                        description="Custom Must-Join Message Template",
                    )
                )
            await sess.commit()
            return clean_msg
        except Exception:
            await sess.rollback()
            raise
        finally:
            if own_session:
                await sess.close()

    @classmethod
    async def reset_must_join_message(
        cls,
        session: Optional[AsyncSession] = None,
        admin_username: str = "admin",
    ) -> str:
        """Reset the Must-Join message to system default."""
        own_session = session is None
        sess = session or AsyncSessionLocal()
        try:
            await sess.execute(
                delete(Setting).where(Setting.key == SETTING_MUST_JOIN_MSG, Setting.status == "ACTIVE")
            )
            await sess.commit()
            return DEFAULT_MUST_JOIN_MESSAGE
        except Exception:
            await sess.rollback()
            raise
        finally:
            if own_session:
                await sess.close()

    @classmethod
    async def get_must_join_exempt_users(cls, session: Optional[AsyncSession] = None) -> str:
        """Get the active Must-Join exempt user IDs and usernames list."""
        val = await cls.get_active_setting(SETTING_MUST_JOIN_EXEMPT_USERS, session)
        return val if val else getattr(settings, "MUST_JOIN_EXEMPT_USERS", "")

    @classmethod
    async def save_must_join_exempt_users(
        cls,
        exempt_users: str,
        session: Optional[AsyncSession] = None,
    ) -> str:
        """Save Must-Join exempt user IDs and usernames list."""
        clean_val = (exempt_users or "").strip()
        await cls.save_single_setting(
            SETTING_MUST_JOIN_EXEMPT_USERS,
            clean_val,
            description="Must-Join exempt user IDs and usernames",
            session=session,
        )
        settings.MUST_JOIN_EXEMPT_USERS = clean_val
        return clean_val

    @classmethod
    async def get_welcome_message(cls, session: Optional[AsyncSession] = None) -> str:
        """Get the active welcome message template for /start."""
        custom_msg = await cls.get_active_setting(SETTING_WELCOME_MSG, session)
        return custom_msg if custom_msg else DEFAULT_WELCOME_MESSAGE

    @classmethod
    async def save_welcome_message(
        cls,
        message: str,
        session: Optional[AsyncSession] = None,
        admin_username: str = "admin",
    ) -> str:
        """Validate and save a custom welcome message template for /start."""
        clean_msg = message.strip()
        is_valid, err = validate_telegram_html(clean_msg)
        if not is_valid:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid Telegram HTML formatting: {err}",
            )

        own_session = session is None
        sess = session or AsyncSessionLocal()
        try:
            stmt = select(Setting).where(Setting.key == SETTING_WELCOME_MSG, Setting.status == "ACTIVE")
            res = await sess.execute(stmt)
            item = res.scalar_one_or_none()
            if item:
                item.value = clean_msg
            else:
                sess.add(
                    Setting(
                        key=SETTING_WELCOME_MSG,
                        status="ACTIVE",
                        value=clean_msg,
                        is_encrypted=False,
                        description="Custom Telegram Bot Welcome Message Template",
                    )
                )
            await sess.commit()
            return clean_msg
        except Exception:
            await sess.rollback()
            raise
        finally:
            if own_session:
                await sess.close()

    @classmethod
    async def reset_welcome_message(
        cls,
        session: Optional[AsyncSession] = None,
        admin_username: str = "admin",
    ) -> str:
        """Reset the welcome message to system default."""
        own_session = session is None
        sess = session or AsyncSessionLocal()
        try:
            await sess.execute(
                delete(Setting).where(Setting.key == SETTING_WELCOME_MSG, Setting.status == "ACTIVE")
            )
            await sess.commit()
            return DEFAULT_WELCOME_MESSAGE
        except Exception:
            await sess.rollback()
            raise
        finally:
            if own_session:
                await sess.close()

    @classmethod
    async def get_help_message(cls, session: Optional[AsyncSession] = None) -> str:
        """Get the active help message template for /help."""
        custom_msg = await cls.get_active_setting(SETTING_HELP_MSG, session)
        return custom_msg if custom_msg else DEFAULT_HELP_MESSAGE

    @classmethod
    async def save_help_message(
        cls,
        message: str,
        session: Optional[AsyncSession] = None,
        admin_username: str = "admin",
    ) -> str:
        """Validate and save a custom help message template for /help."""
        clean_msg = message.strip()
        is_valid, err = validate_telegram_html(clean_msg)
        if not is_valid:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid Telegram HTML formatting: {err}",
            )

        own_session = session is None
        sess = session or AsyncSessionLocal()
        try:
            stmt = select(Setting).where(Setting.key == SETTING_HELP_MSG, Setting.status == "ACTIVE")
            res = await sess.execute(stmt)
            item = res.scalar_one_or_none()
            if item:
                item.value = clean_msg
            else:
                sess.add(
                    Setting(
                        key=SETTING_HELP_MSG,
                        status="ACTIVE",
                        value=clean_msg,
                        is_encrypted=False,
                        description="Custom Telegram Bot Help Message Template",
                    )
                )
            await sess.commit()
            return clean_msg
        except Exception:
            await sess.rollback()
            raise
        finally:
            if own_session:
                await sess.close()

    @classmethod
    async def reset_help_message(
        cls,
        session: Optional[AsyncSession] = None,
        admin_username: str = "admin",
    ) -> str:
        """Reset the /help message to system default."""
        own_session = session is None
        sess = session or AsyncSessionLocal()
        try:
            await sess.execute(
                delete(Setting).where(Setting.key == SETTING_HELP_MSG, Setting.status == "ACTIVE")
            )
            await sess.commit()
            return DEFAULT_HELP_MESSAGE
        except Exception:
            await sess.rollback()
            raise
        finally:
            if own_session:
                await sess.close()

    # --------------------------------------------------------------------------
    # Application & AI Settings (Persistent in PostgreSQL)
    # --------------------------------------------------------------------------

    @classmethod
    async def get_application_settings(cls, session: Optional[AsyncSession] = None) -> Dict[str, Any]:
        """Fetch all global application settings from DB, using config defaults for uninitialized values."""
        from src.services.ytdlp_service import YtDlpService
        all_active = await cls.get_all_active(session)

        res = {}
        for key, spec in APP_SETTINGS_SPEC.items():
            if key.startswith("ai_") or key in (
                SETTING_YTDLP_COOKIES_ENABLED,
                SETTING_MUST_JOIN_ENABLED,
                SETTING_MAX_ACTIVE_JOBS,
            ):
                continue
            attr = spec["attr"]
            default_val = getattr(settings, attr)
            if key in all_active:
                try:
                    res[key] = _cast_setting_value(all_active[key], spec["type"])
                except (ValueError, TypeError):
                    res[key] = default_val
            else:
                res[key] = default_val

        dur = res.get(SETTING_MAX_VIDEO_DURATION_SECONDS, settings.MAX_VIDEO_DURATION_SECONDS)
        res["max_video_duration_formatted"] = YtDlpService.format_duration(dur)
        return res

    @classmethod
    async def save_application_settings(
        cls,
        values: Dict[str, Any],
        session: Optional[AsyncSession] = None,
    ) -> Dict[str, Any]:
        """Persist global application settings to PostgreSQL and update runtime settings object."""
        own_session = session is None
        sess = session or AsyncSessionLocal()
        try:
            for key, val in values.items():
                if key not in APP_SETTINGS_SPEC:
                    continue
                spec = APP_SETTINGS_SPEC[key]
                clean_val = _cast_setting_value(val, spec["type"])
                serialized = _serialize_setting_value(clean_val)

                # Update in-memory settings
                setattr(settings, spec["attr"], clean_val)

                # Persist to Setting table
                stmt = select(Setting).where(Setting.key == key, Setting.status == "ACTIVE")
                result = await sess.execute(stmt)
                item = result.scalar_one_or_none()
                if item:
                    item.value = serialized
                    item.is_encrypted = False
                else:
                    sess.add(
                        Setting(
                            key=key,
                            status="ACTIVE",
                            value=serialized,
                            is_encrypted=False,
                            description=spec["desc"],
                        )
                    )

            await sess.commit()

            # Publish reload notification to Redis if connected
            try:
                r = get_redis_client()
                await r.publish("app:config:reload", json.dumps({"type": "application_settings"}))
            except Exception as e:
                logger.debug("Could not publish app:config:reload to Redis: %s", e)

            return await cls.get_application_settings(sess)
        except Exception:
            await sess.rollback()
            raise
        finally:
            if own_session:
                await sess.close()

    @classmethod
    async def get_ai_settings(cls, session: Optional[AsyncSession] = None) -> Dict[str, Any]:
        """Fetch AI translation settings, masking the API key."""
        from src.services.ai_service import AIService
        all_active = await cls.get_all_active(session)

        enabled = _cast_setting_value(
            all_active.get(SETTING_AI_ENABLED, settings.AI_ENABLED), bool
        )
        provider = str(all_active.get(SETTING_AI_PROVIDER, settings.AI_PROVIDER))
        base_url = str(all_active.get(SETTING_AI_BASE_URL, settings.AI_BASE_URL))
        model = str(all_active.get(SETTING_AI_MODEL, settings.AI_MODEL))
        max_chunks = _cast_setting_value(
            all_active.get(SETTING_AI_MAX_CHUNKS, getattr(settings, "AI_MAX_CHUNKS", 50)), int
        )
        api_key = all_active.get(SETTING_AI_API_KEY, settings.AI_API_KEY) or ""

        masked_key = ""
        if api_key:
            masked_key = f"{api_key[:4]}••••••••{api_key[-4:]}" if len(api_key) > 8 else "••••••••"

        return {
            "enabled": enabled,
            "provider": provider,
            "base_url": base_url,
            "model": model,
            "max_chunks": max_chunks,
            "api_key_masked": masked_key,
            "is_configured": AIService.is_configured(),
        }

    @classmethod
    async def save_ai_settings(
        cls,
        req: Dict[str, Any],
        session: Optional[AsyncSession] = None,
    ) -> Dict[str, Any]:
        """Validate and persist AI translation settings to PostgreSQL with encrypted API key."""
        from src.services.ai_service import AIService
        own_session = session is None
        sess = session or AsyncSessionLocal()
        try:
            enabled = bool(req.get("enabled", False))
            provider = str(req.get("provider", "openai")).strip()
            base_url = str(req.get("base_url", "https://api.openai.com/v1")).rstrip("/")
            model = str(req.get("model", "gpt-4o-mini")).strip()
            max_chunks = int(req.get("max_chunks", getattr(settings, "AI_MAX_CHUNKS", 50)))
            api_key = req.get("api_key")

            # Update in-memory
            settings.AI_ENABLED = enabled
            settings.AI_PROVIDER = provider
            settings.AI_BASE_URL = base_url
            settings.AI_MODEL = model
            settings.AI_MAX_CHUNKS = max_chunks

            async def _upsert(k: str, v: str, desc: str):
                s_stmt = select(Setting).where(Setting.key == k, Setting.status == "ACTIVE")
                s_res = await sess.execute(s_stmt)
                item = s_res.scalar_one_or_none()
                if item:
                    item.value = v
                    item.is_encrypted = False
                else:
                    sess.add(Setting(key=k, status="ACTIVE", value=v, is_encrypted=False, description=desc))

            await _upsert(SETTING_AI_ENABLED, "true" if enabled else "false", "AI Translation Enabled")
            await _upsert(SETTING_AI_PROVIDER, provider, "AI Translation Provider")
            await _upsert(SETTING_AI_BASE_URL, base_url, "AI Translation Base URL")
            await _upsert(SETTING_AI_MODEL, model, "AI Translation Model")
            await _upsert(SETTING_AI_MAX_CHUNKS, str(max_chunks), "AI Translation Max Chunks")

            if api_key and str(api_key).strip():
                clean_key = str(api_key).strip()
                settings.AI_API_KEY = clean_key
                master_key = await get_master_key(sess)
                encrypted_key = encrypt_credential(clean_key, master_key)

                k_stmt = select(Setting).where(Setting.key == SETTING_AI_API_KEY, Setting.status == "ACTIVE")
                k_res = await sess.execute(k_stmt)
                k_item = k_res.scalar_one_or_none()
                if k_item:
                    k_item.value = encrypted_key
                    k_item.is_encrypted = True
                else:
                    sess.add(
                        Setting(
                            key=SETTING_AI_API_KEY,
                            status="ACTIVE",
                            value=encrypted_key,
                            is_encrypted=True,
                            description="AI Translation API Key (encrypted)",
                        )
                    )

                # Write runtime key file for worker consumption if directory exists
                try:
                    runtime_dir = Path(settings.RUNTIME_BOT_TOKEN_FILE).parent
                    if runtime_dir.is_dir():
                        ai_key_file = runtime_dir / "ai-api-key"
                        ai_key_file.write_text(clean_key, encoding="utf-8")
                        try:
                            import grp
                            gid = grp.getgrnam("ytdl-runtime").gr_gid
                            os.chown(ai_key_file, -1, gid)
                        except Exception:
                            pass
                        ai_key_file.chmod(0o640)
                except Exception as e:
                    logger.debug("Could not write runtime ai-api-key file: %s", e)

            await sess.commit()

            # Publish reload notification
            try:
                r = get_redis_client()
                await r.publish("app:config:reload", json.dumps({"type": "ai_settings"}))
            except Exception as e:
                logger.debug("Could not publish reload to Redis: %s", e)

            return {
                "status": "updated",
                "is_configured": AIService.is_configured(),
            }
        except Exception:
            await sess.rollback()
            raise
        finally:
            if own_session:
                await sess.close()

    @classmethod
    async def save_single_setting(
        cls,
        key: str,
        value: Any,
        description: Optional[str] = None,
        session: Optional[AsyncSession] = None,
    ) -> None:
        """Persist a single application setting to PostgreSQL and update runtime settings."""
        own_session = session is None
        sess = session or AsyncSessionLocal()
        try:
            spec = APP_SETTINGS_SPEC.get(key)
            target_type = spec["type"] if spec else type(value)
            clean_val = _cast_setting_value(value, target_type)
            serialized = _serialize_setting_value(clean_val)

            if spec:
                setattr(settings, spec["attr"], clean_val)
            desc = description or (spec["desc"] if spec else f"Setting {key}")

            stmt = select(Setting).where(Setting.key == key, Setting.status == "ACTIVE")
            res = await sess.execute(stmt)
            item = res.scalar_one_or_none()
            if item:
                item.value = serialized
                item.is_encrypted = False
            else:
                sess.add(
                    Setting(
                        key=key,
                        status="ACTIVE",
                        value=serialized,
                        is_encrypted=False,
                        description=desc,
                    )
                )

            await sess.commit()

            try:
                r = get_redis_client()
                await r.publish("app:config:reload", json.dumps({"type": "single_setting", "key": key}))
            except Exception as e:
                logger.debug("Could not publish single_setting reload to Redis: %s", e)
        except Exception:
            await sess.rollback()
            raise
        finally:
            if own_session:
                await sess.close()

    @classmethod
    async def load_public_settings_to_runtime(cls, session: Optional[AsyncSession] = None) -> Dict[str, str]:
        """
        Load non-secret ACTIVE application settings from PostgreSQL into process settings.
        STRICT SECURITY BOUNDARY:
        Safe for Bot and Worker. Never attempts master key retrieval or credential decryption.
        Returns active_map of key->value for caller convenience.
        """
        own_session = session is None
        sess = session or AsyncSessionLocal()
        active_map: Dict[str, str] = {}
        try:
            stmt = select(Setting).where(Setting.status == "ACTIVE")
            res = await sess.execute(stmt)
            items = res.scalars().all()

            for item in items:
                active_map[item.key] = item.value
                if item.key not in APP_SETTINGS_SPEC:
                    continue
                spec = APP_SETTINGS_SPEC[item.key]
                if spec["encrypted"]:
                    # STRICT ISOLATION: Skip secret values completely
                    continue

                try:
                    clean_val = _cast_setting_value(item.value, spec["type"])
                    setattr(settings, spec["attr"], clean_val)
                except Exception as e:
                    logger.warning("Could not cast public setting %s (%s): %s", item.key, item.value, e)

            logger.info("Loaded non-secret application settings from PostgreSQL into runtime config.")
            return active_map
        except Exception as e:
            logger.warning("Could not load public application settings from PostgreSQL: %s. Using in-memory defaults.", e)
            return active_map
        finally:
            if own_session:
                await sess.close()

    @classmethod
    async def load_all_settings_to_runtime(cls, session: Optional[AsyncSession] = None) -> None:
        """
        Load all ACTIVE application settings from PostgreSQL into process settings,
        decrypting secrets using master key and writing /config/runtime/ai-api-key.
        STRICT SECURITY BOUNDARY:
        Used by Web Admin container ONLY.
        """
        # First load non-secret settings
        await cls.load_public_settings_to_runtime(session)

        own_session = session is None
        sess = session or AsyncSessionLocal()
        try:
            stmt = select(Setting).where(Setting.status == "ACTIVE")
            res = await sess.execute(stmt)
            items = res.scalars().all()

            master_key = None
            for item in items:
                if item.key not in APP_SETTINGS_SPEC:
                    continue
                spec = APP_SETTINGS_SPEC[item.key]
                if not spec["encrypted"]:
                    continue

                attr = spec["attr"]
                try:
                    if master_key is None:
                        master_key = await get_master_key(sess)
                    decrypted = decrypt_credential(item.value, master_key)
                    setattr(settings, attr, decrypted)

                    # Write runtime key file for Worker
                    try:
                        runtime_dir = Path(settings.RUNTIME_BOT_TOKEN_FILE).parent
                        if runtime_dir.is_dir():
                            ai_key_file = runtime_dir / "ai-api-key"
                            ai_key_file.write_text(decrypted, encoding="utf-8")
                            try:
                                import grp
                                gid = grp.getgrnam("ytdl-runtime").gr_gid
                                os.chown(ai_key_file, -1, gid)
                            except Exception:
                                pass
                            ai_key_file.chmod(0o640)
                    except Exception as w_err:
                        logger.warning("Could not write runtime ai-api-key file: %s", w_err)
                except Exception as dec_err:
                    logger.warning("Could not decrypt setting %s: %s", item.key, dec_err)

            logger.info("Decrypted secret application settings and synchronized runtime files in Web container.")
        except Exception as e:
            logger.warning("Error loading secret settings in Web: %s", e)
        finally:
            if own_session:
                await sess.close()


