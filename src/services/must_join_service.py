import asyncio
from datetime import datetime, timezone
import html
import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramAPIError
from aiogram.types import CallbackQuery, ChatMember, ChatMemberUpdated, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.bot.keyboards import build_must_join_keyboard
from src.core.config import settings
from src.core.constants import (
    MEMBERSHIP_CACHE_TTL,
    MEMBERSHIP_CACHE_TTL_MEMBER,
    MEMBERSHIP_CACHE_TTL_NON_MEMBER,
    REDIS_KEY_KNOWN_USERS,
    REDIS_KEY_MEMBERSHIP_PREFIX,
    REDIS_KEY_MUST_JOIN_MSG_PREFIX,
)
from src.core.database import AsyncSessionLocal
from src.core.logger import setup_logger
from src.core.redis import get_redis_client
from src.models.channel import RequiredChannel

logger = setup_logger("must_join_service")


def serialize_membership_record(
    status: str,
    ts: float,
    update_id: Optional[int] = None,
    source: str = "api_check",
) -> str:
    """Serializes structured membership state into consistent JSON."""
    return json.dumps({
        "status": status,
        "ts": ts,
        "update_id": update_id,
        "source": source,
    })


def parse_membership_record(raw: Any) -> Optional[dict]:
    """Parses membership record, gracefully handling structured JSON and legacy '1'/'0' strings."""
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        raw_str = raw.strip()
        if raw_str == "1":
            return {"status": "MEMBER", "ts": 0.0, "update_id": None, "source": "legacy"}
        if raw_str == "0":
            return {"status": "NOT_MEMBER", "ts": 0.0, "update_id": None, "source": "legacy"}
        try:
            data = json.loads(raw_str)
            if isinstance(data, dict) and "status" in data:
                return data
        except Exception:
            pass
    return None


def is_active_member(chat_member: ChatMember) -> bool:
    """
    Authoritative evaluation of Telegram membership state:
    - Authorized: 'creator', 'administrator', 'member', and 'restricted' with is_member=True.
    - Unauthorized: 'left', 'kicked', and 'restricted' with is_member=False.
    """
    status = getattr(chat_member, "status", None)
    if status in ("creator", "administrator", "member"):
        return True
    if status == "restricted":
        # Check explicit is_member attribute on ChatMemberRestricted
        return getattr(chat_member, "is_member", False) is True
    # 'left', 'kicked', None, etc.
    return False


class MustJoinService:
    _channels_cache: Optional[List[RequiredChannel]] = None
    _channels_cache_ts: float = 0.0
    _CHANNELS_CACHE_TTL: float = 60.0

    @classmethod
    def clear_local_channels_cache(cls) -> None:
        """Clears process-local in-memory channel cache."""
        cls._channels_cache = None
        cls._channels_cache_ts = 0.0

    @classmethod
    async def invalidate_channels_cache(cls) -> None:
        """Invalidates local channel cache and broadcasts reload event to all processes via Redis."""
        cls.clear_local_channels_cache()
        try:
            r = get_redis_client()
            await r.publish("must_join:channels:reload", "1")
        except Exception as e:
            logger.debug("Failed to broadcast must_join:channels:reload: %s", e)

    @classmethod
    async def get_enabled_channels(cls, session: AsyncSession) -> List[RequiredChannel]:
        """Fetches active required channels from in-memory cache (< 60s) or database."""
        now = time.monotonic()
        if cls._channels_cache is not None and (now - cls._channels_cache_ts < cls._CHANNELS_CACHE_TTL):
            return cls._channels_cache

        stmt = select(RequiredChannel).where(RequiredChannel.enabled == True)
        res = await session.execute(stmt)
        raw_channels = list(res.scalars().all())
        channels = []
        for c in raw_channels:
            ch_copy = RequiredChannel(
                id=c.id,
                chat_id=c.chat_id,
                title=c.title,
                username=c.username,
                invite_url=c.invite_url,
                enabled=c.enabled,
                bot_status=c.bot_status,
                last_bot_check=c.last_bot_check,
                last_error=c.last_error,
            )
            channels.append(ch_copy)
        cls._channels_cache = channels
        cls._channels_cache_ts = now
        return channels

    @classmethod
    async def evaluate_channel_membership(
        cls, bot: Bot, chat_id: int, user_id: int
    ) -> Tuple[bool, Optional[str]]:
        """
        Performs authoritative Telegram getChatMember request.
        Returns:
            (is_member: bool, error_type: Optional[str])
            - If user is active member: (True, None)
            - If user is NOT a member (left, kicked, restricted with is_member=False, participant not found): (False, None)
            - If bot configuration/API problem: (False, "BOT_INSUFFICIENT_PERMISSIONS" | "CHANNEL_NOT_FOUND" | "BOT_NOT_MEMBER" | "TELEGRAM_API_ERROR")
        """
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if is_active_member(member):
                return True, None
            return False, None
        except TelegramBadRequest as e:
            err = str(e).lower()
            if "user not found" in err or "participant_id_invalid" in err:
                return False, None
            if "chat not found" in err:
                logger.error("Must-Join verification failed: channel %s not found: %s", chat_id, e)
                return False, "CHANNEL_NOT_FOUND"
            if "not enough rights" in err or "chat_admin_required" in err or "rights" in err:
                logger.error("Must-Join verification failed: bot lacks rights in channel %s: %s", chat_id, e)
                return False, "BOT_INSUFFICIENT_PERMISSIONS"
            if "bot is not a member" in err or "member of the channel" in err:
                logger.error("Must-Join verification failed: bot not a member of channel %s: %s", chat_id, e)
                return False, "BOT_NOT_MEMBER"

            logger.error("Must-Join BadRequest on channel %s: %s", chat_id, e)
            return False, f"CONFIG_ERROR: {e}"
        except TelegramAPIError as e:
            logger.error("Telegram API/Network error verifying channel %s for user %s: %s", chat_id, user_id, e)
            return False, f"TELEGRAM_API_ERROR: {e}"

    @classmethod
    async def check_channel_membership_authoritative(
        cls, bot: Bot, chat_id: int, user_id: int
    ) -> bool:
        """Performs a fresh, authoritative Telegram API getChatMember request."""
        is_member, _ = await cls.evaluate_channel_membership(bot, chat_id, user_id)
        return is_member

    @classmethod
    def parse_exemptions(cls, raw_exemptions: str) -> Tuple[set[int], set[str]]:
        """
        Parses comma-, newline-, semicolon-, or whitespace-separated exemption entries into:
        - exempt_user_ids: set of integer Telegram user IDs (canonical identity)
        - exempt_usernames: set of lowercase usernames without leading '@'
        """
        exempt_ids: set[int] = set()
        exempt_usernames: set[str] = set()

        if not raw_exemptions:
            return exempt_ids, exempt_usernames

        tokens = re.split(r"[,\n;\s]+", raw_exemptions.strip())
        for token in tokens:
            token = token.strip()
            if not token:
                continue
            if token.startswith("@"):
                clean_name = token.lstrip("@").strip().lower()
                if clean_name:
                    exempt_usernames.add(clean_name)
                continue

            try:
                user_id = int(token)
                exempt_ids.add(user_id)
            except ValueError:
                clean_name = token.lower()
                if clean_name:
                    exempt_usernames.add(clean_name)

        return exempt_ids, exempt_usernames

    @classmethod
    def is_user_exempt(
        cls,
        user_id: int,
        username: Optional[str] = None,
        exemptions_str: Optional[str] = None,
    ) -> bool:
        """
        Checks if a user is exempt from Must-Join channel checks.
        Canonical identity is numeric Telegram user_id.
        Username is matched case-insensitively as a secondary convenience.
        """
        raw = exemptions_str if exemptions_str is not None else getattr(settings, "MUST_JOIN_EXEMPT_USERS", "")
        if not raw:
            return False

        exempt_ids, exempt_usernames = cls.parse_exemptions(raw)

        # 1. Canonical check: numeric user_id
        if user_id in exempt_ids:
            logger.info("User %d is exempt from Must-Join by user_id", user_id)
            return True

        # 2. Secondary convenience check: username
        if username:
            norm_username = username.lstrip("@").strip().lower()
            if norm_username in exempt_usernames:
                logger.info("User %d (@%s) is exempt from Must-Join by username", user_id, username)
                return True

        return False

    @classmethod
    async def save_membership_record(
        cls,
        channel_id: int,
        user_id: int,
        status: str,  # "MEMBER" or "NOT_MEMBER"
        update_id: Optional[int] = None,
        ts: Optional[float] = None,
        source: str = "api_check",
    ) -> bool:
        """
        Saves structured membership record to Redis with idempotent ordering protection.
        Ensures an older update cannot overwrite a newer state.
        Returns True if saved, False if dropped as stale.
        """
        event_ts = ts if ts is not None else datetime.now(timezone.utc).timestamp()
        cache_key = f"{REDIS_KEY_MEMBERSHIP_PREFIX}{channel_id}:{user_id}"
        try:
            r = get_redis_client()
            existing_raw = await r.get(cache_key)
            existing = parse_membership_record(existing_raw)
            if existing:
                existing_uid = existing.get("update_id")
                existing_ts = existing.get("ts", 0.0)

                # 1. Update ID ordering check
                if update_id is not None and existing_uid is not None:
                    if update_id < existing_uid:
                        logger.debug(
                            "Dropping stale membership update: incoming update_id %s < existing %s (channel: %s, user: %s)",
                            update_id, existing_uid, channel_id, user_id,
                        )
                        return False
                # 2. Monotonic timestamp fallback check
                elif event_ts < existing_ts:
                    logger.debug(
                        "Dropping stale membership update: incoming ts %s < existing %s (channel: %s, user: %s)",
                        event_ts, existing_ts, channel_id, user_id,
                    )
                    return False

            payload = serialize_membership_record(
                status=status,
                ts=event_ts,
                update_id=update_id,
                source=source,
            )
            ttl = MEMBERSHIP_CACHE_TTL_MEMBER if status == "MEMBER" else MEMBERSHIP_CACHE_TTL_NON_MEMBER
            await r.set(cache_key, payload, ex=ttl)

            # Register user in known_users
            try:
                await r.sadd(REDIS_KEY_KNOWN_USERS, str(user_id))
            except Exception:
                pass

            return True
        except Exception as e:
            logger.warning("Redis error saving membership record: %s", e)
            return False

    @classmethod
    async def handle_chat_member_event(
        cls,
        bot: Bot,
        event: ChatMemberUpdated,
        update_id: Optional[int] = None,
        session: Optional[AsyncSession] = None,
    ) -> bool:
        """
        Handles real-time chat_member updates for required channels.
        Filters out non-required channels and users who have never used the bot.
        Updates Redis: MEMBER (24h TTL) on join/promote, NOT_MEMBER (30s TTL) on leave/kick.
        Returns True if the user successfully transitioned to MEMBER, False otherwise.
        """
        chat_id = event.chat.id
        if session is not None:
            channels = await cls.get_enabled_channels(session)
        else:
            async with AsyncSessionLocal() as sess:
                channels = await cls.get_enabled_channels(sess)

        req_chat_ids = {ch.chat_id for ch in channels}
        if chat_id not in req_chat_ids:
            return False

        user = event.new_chat_member.user if event.new_chat_member else (
            event.old_chat_member.user if event.old_chat_member else None
        )
        if not user or user.is_bot:
            return False

        user_id = user.id

        # Only track users known to the bot or with existing cache records
        r = get_redis_client()
        cache_key = f"{REDIS_KEY_MEMBERSHIP_PREFIX}{chat_id}:{user_id}"
        try:
            is_known = await r.sismember(REDIS_KEY_KNOWN_USERS, str(user_id))
            if not is_known:
                has_existing = await r.exists(cache_key)
                if not has_existing:
                    # Unknown channel subscriber who never used the bot -> do not record
                    return False
        except Exception as e:
            logger.debug("Redis known_users check error: %s", e)

        was_member = is_active_member(event.old_chat_member)
        is_member = is_active_member(event.new_chat_member)

        if was_member == is_member:
            return False  # No membership state change

        event_ts = event.date.timestamp() if event.date else datetime.now(timezone.utc).timestamp()

        if is_member:
            logger.info("User %d JOINED required channel %d. Updating Redis -> MEMBER.", user_id, chat_id)
            saved = await cls.save_membership_record(
                channel_id=chat_id,
                user_id=user_id,
                status="MEMBER",
                update_id=update_id,
                ts=event_ts,
                source="event",
            )
            return saved
        else:
            logger.info("User %d LEFT/KICKED from required channel %d. Updating Redis -> NOT_MEMBER.", user_id, chat_id)
            await cls.save_membership_record(
                channel_id=chat_id,
                user_id=user_id,
                status="NOT_MEMBER",
                update_id=update_id,
                ts=event_ts,
                source="event",
            )
            return False

    @classmethod
    async def _process_my_chat_member(
        cls,
        event: ChatMemberUpdated,
        chat_id: int,
        session: AsyncSession,
    ) -> None:
        stmt = select(RequiredChannel).where(RequiredChannel.chat_id == chat_id)
        res = await session.execute(stmt)
        ch = res.scalar_one_or_none()
        if not ch:
            return

        new_status = getattr(event.new_chat_member, "status", None)
        if new_status in ("administrator", "creator"):
            logger.info("Bot confirmed as administrator in required channel %d (%s)", chat_id, ch.title)
            ch.bot_status = "administrator"
            ch.last_error = None
            ch.last_bot_check = datetime.now(timezone.utc)
        else:
            err_msg = f"Bot lost administrator rights (status: {new_status})"
            logger.error("Bot permission revoked in channel %d (%s): %s", chat_id, ch.title, err_msg)
            ch.bot_status = "error"
            ch.last_error = err_msg
            ch.last_bot_check = datetime.now(timezone.utc)

        await session.commit()
        await cls.invalidate_channels_cache()

    @classmethod
    async def handle_my_chat_member_event(
        cls,
        bot: Bot,
        event: ChatMemberUpdated,
        session: Optional[AsyncSession] = None,
    ) -> None:
        """
        Handles my_chat_member updates to detect when the bot loses administrator rights
        or is removed from a required channel.
        """
        chat_id = event.chat.id
        if session is not None:
            await cls._process_my_chat_member(event, chat_id, session)
        else:
            async with AsyncSessionLocal() as sess:
                await cls._process_my_chat_member(event, chat_id, sess)

    @classmethod
    async def require_must_join(
        cls,
        bot: Bot,
        session: AsyncSession,
        user_id: int,
        username: Optional[str] = None,
        force_authoritative: bool = True,
    ) -> Tuple[bool, List[RequiredChannel]]:
        """
        Centralized server-side guard.
        Checks ALL enabled required channels.
        Returns: (is_authorized, missing_channels)
        """
        if not settings.MUST_JOIN_ENABLED:
            return True, []

        # Check exemption whitelist before making any Telegram API calls
        if cls.is_user_exempt(user_id, username):
            return True, []

        channels = await cls.get_enabled_channels(session)
        if not channels:
            return True, []

        async def _check_single(ch: RequiredChannel) -> Tuple[RequiredChannel, bool, Optional[str]]:
            if not force_authoritative:
                try:
                    r = get_redis_client()
                    cache_key = f"{REDIS_KEY_MEMBERSHIP_PREFIX}{ch.chat_id}:{user_id}"
                    cached_raw = await r.get(cache_key)
                    cached = parse_membership_record(cached_raw)
                    if cached:
                        # IMPORTANT: Cache hits MUST NOT extend positive TTL!
                        if cached.get("status") == "MEMBER":
                            return ch, True, None
                        elif cached.get("status") == "NOT_MEMBER":
                            return ch, False, None
                except Exception as e:
                    logger.warning("Redis error checking membership cache: %s", e)

            # Authoritative Telegram API check
            is_member, config_err = await cls.evaluate_channel_membership(
                bot, ch.chat_id, user_id
            )

            # Never cache Telegram API/network errors as NOT_MEMBER!
            if not config_err or not config_err.startswith("TELEGRAM_API_ERROR"):
                status_str = "MEMBER" if is_member else "NOT_MEMBER"
                await cls.save_membership_record(
                    channel_id=ch.chat_id,
                    user_id=user_id,
                    status=status_str,
                    source="api_check",
                )

            return ch, is_member, config_err

        results = await asyncio.gather(*(_check_single(ch) for ch in channels))
        missing_channels = []
        for ch, is_member, config_err in results:
            if config_err:
                ch.last_error = config_err
                ch.bot_status = "error"
                ch.last_bot_check = datetime.now(timezone.utc)

            if not is_member:
                missing_channels.append(ch)

        is_authorized = len(missing_channels) == 0
        return is_authorized, missing_channels

    @staticmethod
    async def verify_bot_is_admin(bot: Bot, chat_id: int) -> Tuple[bool, str]:
        """Verifies that the bot is an administrator in the given channel."""
        try:
            bot_info = await bot.get_me()
            chat_member = await bot.get_chat_member(chat_id=chat_id, user_id=bot_info.id)
            if chat_member.status in ("administrator", "creator"):
                return True, "Bot is administrator"
            return False, "Bot is not an administrator in this channel"
        except TelegramBadRequest as e:
            return False, f"Cannot access channel: {e}"
        except TelegramAPIError as e:
            return False, f"Telegram error: {e}"

    @classmethod
    async def get_rendered_must_join_message(
        cls, session: AsyncSession, first_name: Optional[str], missing_channels: List[RequiredChannel]
    ) -> str:
        """Render the customizable Must-Join template with dynamic tags."""
        from src.services.setting_service import SettingService
        template = await SettingService.get_must_join_message(session)
        channel_list_str = "\n".join([f"• <b>{html.escape(c.title)}</b>" for c in missing_channels])
        name = html.escape(first_name) if first_name else "User"
        return template.replace("{first_name}", name).replace("{channel_list}", channel_list_str)

    @classmethod
    async def save_pending_action(
        cls,
        user_id: int,
        chat_id: int,
        action_type: str,
        payload: dict,
        ttl_seconds: int = 900,
    ) -> None:
        """Stores pending user action server-side with strict TTL and user/chat binding."""
        try:
            r = get_redis_client()
            key = f"must_join:pending:{user_id}"
            data = {
                "user_id": user_id,
                "chat_id": chat_id,
                "action_type": action_type,
                "payload": payload,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            await r.set(key, json.dumps(data), ex=ttl_seconds)
        except Exception as e:
            logger.warning("Failed to store pending action for user %s: %s", user_id, e)

    @classmethod
    async def consume_pending_action(
        cls,
        user_id: int,
        chat_id: int,
    ) -> Optional[dict]:
        """Atomically retrieves and consumes a valid pending action."""
        try:
            r = get_redis_client()
            key = f"must_join:pending:{user_id}"
            val = await r.get(key)
            if not val:
                return None
            await r.delete(key)
            if isinstance(val, bytes):
                val = val.decode("utf-8")
            data = json.loads(val)
            if data.get("user_id") != user_id or data.get("chat_id") != chat_id:
                logger.warning(
                    "Pending action user/chat mismatch: expected user=%s chat=%s, got user=%s chat=%s",
                    user_id, chat_id, data.get("user_id"), data.get("chat_id")
                )
                return None
            return data
        except Exception as e:
            logger.warning("Failed to consume pending action for user %s: %s", user_id, e)
            return None

    @classmethod
    async def resolve_missing_invite_links(
        cls, bot: Bot, session: AsyncSession, channels: List[RequiredChannel]
    ) -> None:
        """
        Dynamically auto-resolves invite links for private or username-less channels from Telegram API.
        Persists newly resolved invite URLs to the database for future requests.
        """
        changed = False
        for ch in channels:
            has_valid_url = (
                (ch.invite_url and ch.invite_url.strip().startswith(("http://", "https://")))
                or (ch.username and ch.username.strip().lstrip("@"))
            )
            if not has_valid_url and ch.chat_id:
                try:
                    chat = await bot.get_chat(ch.chat_id)
                    link = chat.invite_link
                    if not link and chat.username:
                        link = f"https://t.me/{chat.username}"
                    if link:
                        ch.invite_url = link
                        changed = True
                        logger.info("Auto-resolved invite link for channel %s: %s", ch.chat_id, link)
                except Exception as e:
                    logger.debug("Failed to auto-resolve invite link for channel %s: %s", ch.chat_id, e)
        if changed:
            try:
                for ch in channels:
                    await session.merge(ch)
                await session.commit()
                cls.clear_local_channels_cache()
            except Exception as e:
                logger.debug("Failed to commit resolved invite URLs: %s", e)

    @classmethod
    async def enforce_must_join_message(
        cls,
        message: Message,
        bot: Bot,
        session: AsyncSession,
        pending_action: Optional[dict] = None,
    ) -> bool:
        """
        Centralized server-side gate for message handlers (/start, YouTube link, etc.).
        Returns True if authorized, False if blocked.
        If blocked, stores pending action server-side and sends Must-Join prompt with buttons.
        """
        user_id = message.from_user.id if message.from_user else message.chat.id
        username = message.from_user.username if message.from_user else None
        chat_id = message.chat.id

        is_auth, missing_channels = await cls.require_must_join(
            bot, session, user_id, username=username, force_authoritative=False
        )
        if is_auth:
            return True

        # Check if missing channels are due to temporary Telegram network outage
        api_errs = [c for c in missing_channels if c.last_error and c.last_error.startswith("TELEGRAM_API_ERROR")]
        if api_errs and len(api_errs) == len(missing_channels):
            try:
                await message.answer(
                    "⚠️ <b>Verification Service Temporarily Unavailable</b>\n"
                    "We are temporarily unable to verify channel membership with Telegram. "
                    "Please try again in a few moments.",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                await message.answer("⚠️ Verification service temporarily unavailable. Please try again shortly.")
            return False

        if pending_action:
            action_type = pending_action.get("type", "unknown")
            payload = pending_action.get("payload", {})
            await cls.save_pending_action(user_id=user_id, chat_id=chat_id, action_type=action_type, payload=payload)
        else:
            await cls.save_pending_action(user_id=user_id, chat_id=chat_id, action_type="start", payload={})

        await cls.resolve_missing_invite_links(bot, session, missing_channels)
        kb = build_must_join_keyboard(missing_channels)
        first_name = message.from_user.first_name if message.from_user else None
        text = await cls.get_rendered_must_join_message(session, first_name, missing_channels)

        config_errs = [c for c in missing_channels if c.last_error]
        if config_errs:
            err_details = ", ".join([f"{c.title} ({c.last_error})" for c in config_errs])
            logger.warning("Must-Join message has channel configuration errors: %s", err_details)

        sent_msg = None
        try:
            sent_msg = await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except TelegramBadRequest as e:
            logger.warning("Failed to send Must-Join message with HTML: %s. Falling back to default format.", e)
            try:
                sent_msg = await message.answer(text, reply_markup=kb)
            except Exception:
                pass
        except Exception as e:
            logger.warning("Failed to send Must-Join message: %s", e)

        if sent_msg:
            try:
                r = get_redis_client()
                await r.set(f"{REDIS_KEY_MUST_JOIN_MSG_PREFIX}{user_id}", f"{sent_msg.chat.id}:{sent_msg.message_id}", ex=86400)
            except Exception as e:
                logger.debug("Failed saving must-join prompt message id: %s", e)

        return False

    @classmethod
    async def enforce_must_join_callback(
        cls,
        callback: CallbackQuery,
        bot: Bot,
        session: AsyncSession,
        pending_action: Optional[dict] = None,
    ) -> bool:
        """
        Centralized server-side gate for callback handlers (quality, codec, audio, subtitle).
        Returns True if authorized, False if blocked.
        If blocked, stores pending action server-side and informs user via alert or prompt.
        """
        user_id = callback.from_user.id
        username = callback.from_user.username if callback.from_user else None
        chat_id = callback.message.chat.id if callback.message else user_id

        is_auth, missing_channels = await cls.require_must_join(
            bot, session, user_id, username=username, force_authoritative=False
        )
        if is_auth:
            return True

        # Check if missing channels are due to temporary Telegram network outage
        api_errs = [c for c in missing_channels if c.last_error and c.last_error.startswith("TELEGRAM_API_ERROR")]
        if api_errs and len(api_errs) == len(missing_channels):
            await callback.answer(
                "⚠️ Telegram verification is temporarily unavailable. Please try again shortly.",
                show_alert=True,
            )
            return False

        if pending_action:
            action_type = pending_action.get("type", "unknown")
            payload = pending_action.get("payload", {})
            await cls.save_pending_action(user_id=user_id, chat_id=chat_id, action_type=action_type, payload=payload)

        await cls.resolve_missing_invite_links(bot, session, missing_channels)
        kb = build_must_join_keyboard(missing_channels)
        await callback.answer("🔒 You must join the required channels to proceed.", show_alert=True)
        if callback.message:
            first_name = callback.from_user.first_name if callback.from_user else None
            text = await cls.get_rendered_must_join_message(session, first_name, missing_channels)
            sent_msg = None
            try:
                sent_msg = await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)
            except Exception:
                try:
                    sent_msg = await callback.message.answer(text, reply_markup=kb)
                except Exception:
                    pass
            if sent_msg:
                try:
                    r = get_redis_client()
                    await r.set(f"{REDIS_KEY_MUST_JOIN_MSG_PREFIX}{user_id}", f"{sent_msg.chat.id}:{sent_msg.message_id}", ex=86400)
                except Exception as e:
                    logger.debug("Failed saving must-join prompt message id: %s", e)

        return False
