from unittest.mock import AsyncMock, MagicMock
import json
import pytest
from aiogram import Bot
from aiogram.types import Message
from src.core.config import settings
from src.models.channel import RequiredChannel
from src.services.must_join_service import MustJoinService, is_active_member


@pytest.fixture(autouse=True)
def reset_channels_cache():
    MustJoinService.clear_local_channels_cache()
    yield
    MustJoinService.clear_local_channels_cache()


def test_membership_state_evaluation():
    # 1. Active states
    m_creator = MagicMock(status="creator")
    m_admin = MagicMock(status="administrator")
    m_member = MagicMock(status="member")

    assert is_active_member(m_creator) is True
    assert is_active_member(m_admin) is True
    assert is_active_member(m_member) is True

    # 2. Restricted with is_member=True
    m_restricted_member = MagicMock(status="restricted", is_member=True)
    assert is_active_member(m_restricted_member) is True

    # 3. Restricted with is_member=False (e.g. muted non-member)
    m_restricted_non_member = MagicMock(status="restricted", is_member=False)
    assert is_active_member(m_restricted_non_member) is False

    # 4. Unauthorized states
    m_left = MagicMock(status="left")
    m_kicked = MagicMock(status="kicked")

    assert is_active_member(m_left) is False
    assert is_active_member(m_kicked) is False


@pytest.mark.asyncio
async def test_must_join_disabled_allows_all(db_session):
    settings.MUST_JOIN_ENABLED = False
    bot = MagicMock()

    is_auth, missing = await MustJoinService.require_must_join(bot, db_session, user_id=123)
    assert is_auth is True
    assert missing == []


@pytest.mark.asyncio
async def test_must_join_multiple_channels_logic(db_session):
    settings.MUST_JOIN_ENABLED = True

    # Add 2 channels to DB
    ch1 = RequiredChannel(chat_id=-1001, title="Channel A", enabled=True, bot_status="administrator")
    ch2 = RequiredChannel(chat_id=-1002, title="Channel B", enabled=True, bot_status="administrator")
    db_session.add_all([ch1, ch2])
    await db_session.commit()

    bot = MagicMock()

    # Case 1: User is member of ch1, but NOT ch2
    async def mock_get_chat_member(chat_id, user_id):
        if chat_id == -1001:
            return MagicMock(status="member")
        else:
            return MagicMock(status="left")

    bot.get_chat_member = AsyncMock(side_effect=mock_get_chat_member)

    is_auth, missing = await MustJoinService.require_must_join(
        bot, db_session, user_id=123, force_authoritative=True
    )
    # Must fail because user is missing Channel B
    assert is_auth is False
    assert len(missing) == 1
    assert missing[0].chat_id == -1002

    # Case 2: User joins Channel B and presses "Check Again"
    async def mock_get_chat_member_joined(chat_id, user_id):
        return MagicMock(status="member")

    bot.get_chat_member = AsyncMock(side_effect=mock_get_chat_member_joined)

    is_auth2, missing2 = await MustJoinService.require_must_join(
        bot, db_session, user_id=123, force_authoritative=True
    )
    assert is_auth2 is True
    assert len(missing2) == 0

    # Case 3: User later leaves Channel A
    async def mock_get_chat_member_left(chat_id, user_id):
        if chat_id == -1001:
            return MagicMock(status="left")
        return MagicMock(status="member")

    bot.get_chat_member = AsyncMock(side_effect=mock_get_chat_member_left)

    is_auth3, missing3 = await MustJoinService.require_must_join(
        bot, db_session, user_id=123, force_authoritative=True
    )
    # Revoked immediately!
    assert is_auth3 is False
    assert len(missing3) == 1
    assert missing3[0].chat_id == -1001


@pytest.mark.asyncio
async def test_evaluate_channel_membership_error_distinctions():
    """
    Verifies that evaluate_channel_membership distinguishes user non-membership
    from bot configuration/permission and Telegram API errors.
    """
    from aiogram.exceptions import TelegramBadRequest, TelegramAPIError

    bot = MagicMock()

    # 1. User is member
    bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))
    is_m, err = await MustJoinService.evaluate_channel_membership(bot, -1001, 123)
    assert is_m is True
    assert err is None

    # 2. User left (normal non-member, NOT a bot error)
    bot.get_chat_member = AsyncMock(return_value=MagicMock(status="left"))
    is_m, err = await MustJoinService.evaluate_channel_membership(bot, -1001, 123)
    assert is_m is False
    assert err is None

    # 3. User not found (normal non-member, NOT a bot error)
    bot.get_chat_member = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: user not found")
    )
    is_m, err = await MustJoinService.evaluate_channel_membership(bot, -1001, 123)
    assert is_m is False
    assert err is None

    # 4. Chat not found (BOT/CHANNEL ERROR)
    bot.get_chat_member = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: chat not found")
    )
    is_m, err = await MustJoinService.evaluate_channel_membership(bot, -1001, 123)
    assert is_m is False
    assert err == "CHANNEL_NOT_FOUND"

    # 5. Not enough rights / admin required (BOT PERMISSION ERROR)
    bot.get_chat_member = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: not enough rights to get chat member")
    )
    is_m, err = await MustJoinService.evaluate_channel_membership(bot, -1001, 123)
    assert is_m is False
    assert err == "BOT_INSUFFICIENT_PERMISSIONS"

    # 6. Bot not a member (BOT PERMISSION ERROR)
    bot.get_chat_member = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="Bad Request: bot is not a member of the channel")
    )
    is_m, err = await MustJoinService.evaluate_channel_membership(bot, -1001, 123)
    assert is_m is False
    assert err == "BOT_NOT_MEMBER"

    # 7. Telegram API / Network error
    bot.get_chat_member = AsyncMock(
        side_effect=TelegramAPIError(method=MagicMock(), message="Network connection failed")
    )
    is_m, err = await MustJoinService.evaluate_channel_membership(bot, -1001, 123)
    assert is_m is False
    assert "TELEGRAM_API_ERROR" in err


@pytest.mark.asyncio
async def test_must_join_zero_channels_allows_all(db_session):
    """When Must-Join is enabled but no channels are configured, allow access safely."""
    settings.MUST_JOIN_ENABLED = True
    bot = MagicMock()
    is_auth, missing = await MustJoinService.require_must_join(bot, db_session, user_id=123)
    assert is_auth is True
    assert missing == []


def test_must_join_keyboard_structure():
    """
    Verifies that build_must_join_keyboard builds:
    - One URL button per channel pointing to configured invite/public URL
    - Explicit fallback button for channels missing URLs
    - NO 'I Joined' button (eliminated for event-driven automatic unblocking)
    """
    from src.bot.keyboards import build_must_join_keyboard

    ch1 = RequiredChannel(chat_id=-1001, title="Channel Public", username="public_chan", enabled=True)
    ch2 = RequiredChannel(chat_id=-1002, title="Channel Invite", invite_url="https://t.me/+AbCdEf", enabled=True)
    ch3 = RequiredChannel(chat_id=-1003, title="Channel No Link", enabled=True)

    kb = build_must_join_keyboard([ch1, ch2, ch3])
    buttons = kb.inline_keyboard

    # Exactly 3 buttons for 3 channels, zero extra buttons
    assert len(buttons) == 3

    # ch1: username -> https://t.me/public_chan
    assert buttons[0][0].text == "Channel Public"
    assert buttons[0][0].url == "https://t.me/public_chan"

    # ch2: invite_url -> https://t.me/+AbCdEf
    assert buttons[1][0].text == "Channel Invite"
    assert buttons[1][0].url == "https://t.me/+AbCdEf"

    # ch3: no link -> explicit fallback button
    assert buttons[2][0].text == "Channel No Link (No Link Configured)"
    assert buttons[2][0].callback_data == "must_join:no_url"



@pytest.mark.asyncio
async def test_pending_action_storage_binding_expiry_and_consumption(monkeypatch):
    """
    Verifies that pending user action:
    - Binds strictly to user_id and chat_id
    - Cannot be consumed twice (one-time consumption)
    - Cannot be consumed by a different user or chat
    """
    from tests.mock_redis import MockRedis
    mock_r = MockRedis()
    monkeypatch.setattr("src.services.must_join_service.get_redis_client", lambda: mock_r)

    user_id = 998877
    chat_id = 112233

    # Store pending action
    await MustJoinService.save_pending_action(
        user_id=user_id,
        chat_id=chat_id,
        action_type="url",
        payload={"url": "https://www.youtube.com/watch?v=abcdefghijk"},
    )

    # Attempt consume by wrong chat_id -> must fail
    wrong_chat = await MustJoinService.consume_pending_action(user_id=user_id, chat_id=999999)
    assert wrong_chat is None

    # Re-save for valid consumption
    await MustJoinService.save_pending_action(
        user_id=user_id,
        chat_id=chat_id,
        action_type="url",
        payload={"url": "https://www.youtube.com/watch?v=abcdefghijk"},
    )

    # Attempt consume by wrong user_id -> must fail
    wrong_user = await MustJoinService.consume_pending_action(user_id=123456, chat_id=chat_id)
    assert wrong_user is None

    # Valid consumption
    consumed = await MustJoinService.consume_pending_action(user_id=user_id, chat_id=chat_id)
    assert consumed is not None
    assert consumed["action_type"] == "url"
    assert consumed["payload"]["url"] == "https://www.youtube.com/watch?v=abcdefghijk"

    # Consume second time -> must return None (one-time only!)
    second_consume = await MustJoinService.consume_pending_action(user_id=user_id, chat_id=chat_id)
    assert second_consume is None


@pytest.mark.asyncio
async def test_start_handler_must_join_gate(db_session):
    """
    Verifies /start behavior:
    - If Must-Join enabled & user not joined: sends Must-Join prompt + buttons
    - If user joined: sends normal welcome message with {first_name} escaped
    """
    from src.bot.handlers.base import cmd_start

    settings.MUST_JOIN_ENABLED = True
    ch = RequiredChannel(chat_id=-1001, title="Official Channel", username="official", enabled=True)
    db_session.add(ch)
    await db_session.commit()

    bot = MagicMock()
    mock_msg = MagicMock()
    mock_msg.from_user = MagicMock(id=1234, first_name="<b>Farzad</b>", username="farzad")
    mock_msg.chat = MagicMock(id=1234)
    mock_msg.answer = AsyncMock()

    # Case 1: User not joined -> blocked with Must-Join message
    bot.get_chat_member = AsyncMock(return_value=MagicMock(status="left"))
    await cmd_start(mock_msg, bot)

    assert mock_msg.answer.called
    call_kwargs = mock_msg.answer.call_args.kwargs
    assert "reply_markup" in call_kwargs
    # Check that channel buttons are present (1 channel button, no I Joined button)
    assert len(call_kwargs["reply_markup"].inline_keyboard) == 1

    # Case 2: User joined -> normal welcome message with escaped name
    mock_msg.answer.reset_mock()
    bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))
    await cmd_start(mock_msg, bot)

    assert mock_msg.answer.called
    welcome_text = mock_msg.answer.call_args[0][0]
    # The literal <b>Farzad</b> must be escaped to &lt;b&gt;Farzad&lt;/b&gt;
    assert "&lt;b&gt;Farzad&lt;/b&gt;" in welcome_text


@pytest.mark.asyncio
async def test_must_join_message_html_validation(db_session):
    """
    Verifies that custom Must-Join message templates are validated against
    TelegramHTMLValidator, accepting valid HTML and rejecting invalid HTML.
    """
    from fastapi import HTTPException
    from src.services.setting_service import SettingService

    # Valid template with allowed tags
    valid_tpl = "Hello {first_name}!\nPlease join:\n{channel_list}\n<b>Important Notice</b>"
    saved = await SettingService.save_must_join_message(valid_tpl, session=db_session)
    assert saved == valid_tpl

    # Invalid template with disallowed or malformed tags
    invalid_tpl = "Hello {first_name}!<script>alert(1)</script>{channel_list}"
    with pytest.raises(HTTPException) as exc_info:
        await SettingService.save_must_join_message(invalid_tpl, session=db_session)
    assert exc_info.value.status_code == 400
    assert "Invalid Telegram HTML" in exc_info.value.detail


@pytest.mark.asyncio
async def test_must_join_handler_check_again_flow(db_session, monkeypatch):
    """
    Verifies on_must_join_check_again:
    - User missing channels: alerts user, edits reply markup, preserves keyboard.
    - User joined: verifies user, edits message text, resumes pending action.
    """
    from src.bot.handlers.must_join_handler import on_must_join_check_again
    from tests.mock_redis import MockRedis

    mock_r = MockRedis()
    monkeypatch.setattr("src.services.must_join_service.get_redis_client", lambda: mock_r)

    settings.MUST_JOIN_ENABLED = True
    ch = RequiredChannel(chat_id=-1001, title="Test Channel", username="test_chan", enabled=True)
    db_session.add(ch)
    await db_session.commit()

    bot = MagicMock()
    callback = MagicMock()
    callback.from_user = MagicMock(id=556677)
    callback.message = MagicMock()
    callback.message.chat = MagicMock(id=556677)
    callback.message.edit_reply_markup = AsyncMock()
    callback.message.edit_text = AsyncMock()
    callback.message.answer = AsyncMock()
    callback.answer = AsyncMock()
    callback.data = "must_join:check"

    # Case 1: Still not joined
    bot.get_chat_member = AsyncMock(return_value=MagicMock(status="left"))
    await on_must_join_check_again(callback, bot)

    assert callback.answer.called
    assert callback.answer.call_args.kwargs.get("show_alert") is True
    assert callback.message.edit_reply_markup.called

    # Save a server-side pending action
    await MustJoinService.save_pending_action(
        user_id=556677,
        chat_id=556677,
        action_type="url",
        payload={"url": "https://www.youtube.com/watch?v=12345678901", "source_id": "12345678901"},
    )

    # Case 2: Joined!
    callback.answer.reset_mock()
    callback.message.edit_reply_markup.reset_mock()
    callback.message.edit_text.reset_mock()
    callback.message.answer.reset_mock()

    bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))

    mock_process = AsyncMock()
    monkeypatch.setattr("src.bot.handlers.url_handler.process_youtube_url", mock_process)

    await on_must_join_check_again(callback, bot)

    assert callback.answer.called
    assert callback.answer.call_args.kwargs.get("show_alert") is False
    assert callback.message.edit_text.called
    # Verifies pending action was resumed
    assert mock_process.called
    assert mock_process.call_args[0][2] == "https://www.youtube.com/watch?v=12345678901"


@pytest.mark.asyncio
async def test_chat_member_join_and_leave_event_handling(db_session, monkeypatch):
    """
    Verifies that:
    - User join event updates Redis to MEMBER with 24h TTL.
    - User leave event updates Redis to NOT_MEMBER with 30s TTL without deleting.
    """
    from datetime import datetime, timezone
    from tests.mock_redis import MockRedis
    from src.core.constants import REDIS_KEY_KNOWN_USERS, REDIS_KEY_MEMBERSHIP_PREFIX

    mock_r = MockRedis()
    monkeypatch.setattr("src.services.must_join_service.get_redis_client", lambda: mock_r)

    ch = RequiredChannel(chat_id=-1001, title="Test Channel", enabled=True, bot_status="administrator")
    db_session.add(ch)
    await db_session.commit()

    user_id = 778899
    # User is known to bot
    await mock_r.sadd(REDIS_KEY_KNOWN_USERS, str(user_id))

    bot = MagicMock()
    # 1. Join Event
    mock_event_join = MagicMock()
    mock_event_join.chat.id = -1001
    mock_event_join.new_chat_member.user.id = user_id
    mock_event_join.new_chat_member.user.is_bot = False
    mock_event_join.new_chat_member.status = "member"
    mock_event_join.old_chat_member.status = "left"
    mock_event_join.date = datetime.now(timezone.utc)

    await MustJoinService.handle_chat_member_event(bot, mock_event_join, update_id=101, session=db_session)

    key = f"{REDIS_KEY_MEMBERSHIP_PREFIX}-1001:{user_id}"
    record_raw = await mock_r.get(key)
    assert record_raw is not None
    assert '"status": "MEMBER"' in record_raw
    assert await mock_r.ttl(key) == 86400

    # 2. Leave Event
    mock_event_leave = MagicMock()
    mock_event_leave.chat.id = -1001
    mock_event_leave.new_chat_member.user.id = user_id
    mock_event_leave.new_chat_member.user.is_bot = False
    mock_event_leave.new_chat_member.status = "left"
    mock_event_leave.old_chat_member.status = "member"
    mock_event_leave.date = datetime.now(timezone.utc)

    await MustJoinService.handle_chat_member_event(bot, mock_event_leave, update_id=102, session=db_session)

    record_raw2 = await mock_r.get(key)
    assert record_raw2 is not None
    assert '"status": "NOT_MEMBER"' in record_raw2
    assert await mock_r.ttl(key) == 30


@pytest.mark.asyncio
async def test_chat_member_restricted_is_member_flag(db_session, monkeypatch):
    """
    Verifies that restricted status respects the is_member flag:
    - is_member=True -> MEMBER
    - is_member=False -> NOT_MEMBER
    """
    from datetime import datetime, timezone
    from tests.mock_redis import MockRedis
    from src.core.constants import REDIS_KEY_KNOWN_USERS, REDIS_KEY_MEMBERSHIP_PREFIX

    mock_r = MockRedis()
    monkeypatch.setattr("src.services.must_join_service.get_redis_client", lambda: mock_r)

    ch = RequiredChannel(chat_id=-1001, title="Test Channel", enabled=True, bot_status="administrator")
    db_session.add(ch)
    await db_session.commit()

    user_id = 445566
    await mock_r.sadd(REDIS_KEY_KNOWN_USERS, str(user_id))

    bot = MagicMock()
    # 1. Restricted but still a member
    event1 = MagicMock()
    event1.chat.id = -1001
    event1.new_chat_member.user.id = user_id
    event1.new_chat_member.user.is_bot = False
    event1.new_chat_member.status = "restricted"
    event1.new_chat_member.is_member = True
    event1.old_chat_member.status = "left"
    event1.date = datetime.now(timezone.utc)

    await MustJoinService.handle_chat_member_event(bot, event1, update_id=201, session=db_session)

    key = f"{REDIS_KEY_MEMBERSHIP_PREFIX}-1001:{user_id}"
    rec1 = await mock_r.get(key)
    assert '"status": "MEMBER"' in rec1

    # 2. Restricted with is_member=False (effectively removed)
    event2 = MagicMock()
    event2.chat.id = -1001
    event2.new_chat_member.user.id = user_id
    event2.new_chat_member.user.is_bot = False
    event2.new_chat_member.status = "restricted"
    event2.new_chat_member.is_member = False
    event2.old_chat_member.status = "restricted"
    event2.old_chat_member.is_member = True
    event2.date = datetime.now(timezone.utc)

    await MustJoinService.handle_chat_member_event(bot, event2, update_id=202, session=db_session)

    rec2 = await mock_r.get(key)
    assert '"status": "NOT_MEMBER"' in rec2


@pytest.mark.asyncio
async def test_out_of_order_membership_events_discarded(monkeypatch):
    """
    Verifies that an older event cannot overwrite a newer state in Redis:
    - By update_id
    - By timestamp fallback
    """
    from tests.mock_redis import MockRedis

    mock_r = MockRedis()
    monkeypatch.setattr("src.services.must_join_service.get_redis_client", lambda: mock_r)

    channel_id = -1001
    user_id = 999111

    # Save newer event (update_id=500, ts=1000)
    saved = await MustJoinService.save_membership_record(
        channel_id=channel_id,
        user_id=user_id,
        status="MEMBER",
        update_id=500,
        ts=1000.0,
        source="event",
    )
    assert saved is True

    # Attempt to save older event by update_id (update_id=450, ts=1100) -> must be rejected
    rejected_uid = await MustJoinService.save_membership_record(
        channel_id=channel_id,
        user_id=user_id,
        status="NOT_MEMBER",
        update_id=450,
        ts=1100.0,
        source="event",
    )
    assert rejected_uid is False

    # Attempt to save older event by timestamp when update_id missing (ts=900) -> must be rejected
    rejected_ts = await MustJoinService.save_membership_record(
        channel_id=channel_id,
        user_id=user_id,
        status="NOT_MEMBER",
        update_id=None,
        ts=900.0,
        source="event",
    )
    assert rejected_ts is False

    # Current state in Redis remains MEMBER
    key = f"ytdl:membership:cache:{channel_id}:{user_id}"
    rec = await mock_r.get(key)
    assert '"status": "MEMBER"' in rec


@pytest.mark.asyncio
async def test_unknown_user_join_event_not_cached(db_session, monkeypatch):
    """
    Verifies that join events for users not known to the bot (not in known_users, no prior cache)
    are discarded to prevent database/Redis subscriber write amplification.
    """
    from datetime import datetime, timezone
    from tests.mock_redis import MockRedis
    from src.core.constants import REDIS_KEY_MEMBERSHIP_PREFIX

    mock_r = MockRedis()
    monkeypatch.setattr("src.services.must_join_service.get_redis_client", lambda: mock_r)

    ch = RequiredChannel(chat_id=-1001, title="Test Channel", enabled=True, bot_status="administrator")
    db_session.add(ch)
    await db_session.commit()

    unknown_user_id = 888777666
    bot = MagicMock()

    event = MagicMock()
    event.chat.id = -1001
    event.new_chat_member.user.id = unknown_user_id
    event.new_chat_member.user.is_bot = False
    event.new_chat_member.status = "member"
    event.old_chat_member.status = "left"
    event.date = datetime.now(timezone.utc)

    await MustJoinService.handle_chat_member_event(bot, event, update_id=601, session=db_session)

    key = f"{REDIS_KEY_MEMBERSHIP_PREFIX}-1001:{unknown_user_id}"
    # Key must NOT exist in Redis
    assert await mock_r.get(key) is None


@pytest.mark.asyncio
async def test_cache_hit_does_not_extend_positive_ttl(db_session, monkeypatch):
    """
    Verifies that cache hits allow access without calling Telegram API,
    and DO NOT extend or refresh the positive record's fixed 24h TTL.
    """
    from tests.mock_redis import MockRedis
    from src.core.constants import REDIS_KEY_MEMBERSHIP_PREFIX

    mock_r = MockRedis()
    monkeypatch.setattr("src.services.must_join_service.get_redis_client", lambda: mock_r)

    ch = RequiredChannel(chat_id=-1001, title="Test Channel", enabled=True, bot_status="administrator")
    db_session.add(ch)
    await db_session.commit()

    user_id = 333222
    key = f"{REDIS_KEY_MEMBERSHIP_PREFIX}-1001:{user_id}"

    # Manually store record with TTL
    await MustJoinService.save_membership_record(
        channel_id=-1001,
        user_id=user_id,
        status="MEMBER",
        update_id=1,
        source="event",
    )
    initial_ttl = await mock_r.ttl(key)
    assert initial_ttl == 86400

    # Simulate TTL ticking down to 50000
    mock_r.ttls[key] = 50000

    bot = MagicMock()
    bot.get_chat_member = AsyncMock()

    # Perform Must-Join check with force_authoritative=False
    is_auth, missing = await MustJoinService.require_must_join(
        bot, db_session, user_id=user_id, force_authoritative=False
    )
    assert is_auth is True
    assert missing == []
    # Zero Telegram API calls!
    assert not bot.get_chat_member.called
    # TTL MUST NOT BE REFRESHED BACK TO 86400!
    assert await mock_r.ttl(key) == 50000


@pytest.mark.asyncio
async def test_telegram_api_error_not_cached_as_not_member_and_warns_user(db_session, monkeypatch):
    """
    Verifies that TelegramAPIError (e.g. 502/network failure):
    - Is NOT saved into Redis as NOT_MEMBER.
    - Prompts user with retryable temporary unavailable notice, NOT 'you left the channel'.
    """
    from aiogram.exceptions import TelegramAPIError
    from tests.mock_redis import MockRedis
    from src.core.constants import REDIS_KEY_MEMBERSHIP_PREFIX

    mock_r = MockRedis()
    monkeypatch.setattr("src.services.must_join_service.get_redis_client", lambda: mock_r)

    ch = RequiredChannel(chat_id=-1001, title="Test Channel", enabled=True, bot_status="administrator")
    db_session.add(ch)
    await db_session.commit()

    user_id = 112244
    key = f"{REDIS_KEY_MEMBERSHIP_PREFIX}-1001:{user_id}"

    bot = MagicMock()
    bot.get_chat_member = AsyncMock(side_effect=TelegramAPIError(method=MagicMock(), message="502 Bad Gateway"))

    mock_msg = MagicMock()
    mock_msg.from_user = MagicMock(id=user_id, first_name="User", username="user")
    mock_msg.chat = MagicMock(id=user_id)
    mock_msg.answer = AsyncMock()

    allowed = await MustJoinService.enforce_must_join_message(mock_msg, bot, db_session)
    assert allowed is False

    # Verify Redis does NOT contain a NOT_MEMBER record
    assert await mock_r.get(key) is None

    # Verify message sent to user explains temporary outage, not missing channels
    assert mock_msg.answer.called
    sent_text = mock_msg.answer.call_args[0][0]
    assert "Temporarily Unavailable" in sent_text


@pytest.mark.asyncio
async def test_my_chat_member_bot_permission_loss(db_session):
    """
    Verifies that when my_chat_member reports the bot lost administrator status:
    - RequiredChannel.bot_status is set to 'error'
    - last_error records the permission loss
    """
    ch = RequiredChannel(chat_id=-1001, title="Test Channel", enabled=True, bot_status="administrator")
    db_session.add(ch)
    await db_session.commit()

    bot = MagicMock()
    event = MagicMock()
    event.chat.id = -1001
    event.new_chat_member.status = "member"  # Demoted to regular member!

    await MustJoinService.handle_my_chat_member_event(bot, event, session=db_session)

    # Re-fetch channel from DB
    await db_session.refresh(ch)
    assert ch.bot_status == "error"
    assert "lost administrator rights" in ch.last_error


@pytest.mark.asyncio
async def test_worker_delivery_uses_event_updated_cache(db_session, monkeypatch):
    """
    Verifies that background worker delivery uses the event-updated Redis cache
    instead of issuing a blocking Telegram API request on every finished job.
    """
    from tests.mock_redis import MockRedis
    from src.core.constants import REDIS_KEY_MEMBERSHIP_PREFIX

    mock_r = MockRedis()
    monkeypatch.setattr("src.services.must_join_service.get_redis_client", lambda: mock_r)

    ch = RequiredChannel(chat_id=-1001, title="Test Channel", enabled=True, bot_status="administrator")
    db_session.add(ch)
    await db_session.commit()

    user_id = 998811
    key = f"{REDIS_KEY_MEMBERSHIP_PREFIX}-1001:{user_id}"

    # Store MEMBER in Redis
    await MustJoinService.save_membership_record(
        channel_id=-1001,
        user_id=user_id,
        status="MEMBER",
        source="event",
    )

    bot = MagicMock()
    bot.get_chat_member = AsyncMock()

    # Worker check with force_authoritative=False
    is_auth, missing = await MustJoinService.require_must_join(
        bot, db_session, user_id=user_id, force_authoritative=False
    )
    assert is_auth is True
    assert missing == []
    # Confirms worker avoided Telegram API round-trip
    assert not bot.get_chat_member.called

    # If user leaves and cache becomes NOT_MEMBER
    await MustJoinService.save_membership_record(
        channel_id=-1001,
        user_id=user_id,
        status="NOT_MEMBER",
        source="event",
    )
    is_auth2, missing2 = await MustJoinService.require_must_join(
        bot, db_session, user_id=user_id, force_authoritative=False
    )
    assert is_auth2 is False
    assert len(missing2) == 1
    assert not bot.get_chat_member.called


@pytest.mark.asyncio
async def test_channel_cache_invalidation_broadcast(monkeypatch):
    """
    Verifies that MustJoinService.invalidate_channels_cache clears the local cache
    and publishes the reload notification to Redis.
    """
    from tests.mock_redis import MockRedis

    mock_r = MockRedis()
    published_events = []

    async def mock_publish(channel, msg):
        published_events.append((channel, msg))
        return 1

    mock_r.publish = mock_publish
    monkeypatch.setattr("src.services.must_join_service.get_redis_client", lambda: mock_r)

    # Set up some dummy cache
    MustJoinService._channels_cache = [MagicMock()]
    MustJoinService._channels_cache_ts = 12345.0

    await MustJoinService.invalidate_channels_cache()

    assert MustJoinService._channels_cache is None
    assert MustJoinService._channels_cache_ts == 0.0
    assert ("must_join:channels:reload", "1") in published_events


@pytest.mark.asyncio
async def test_enforce_must_join_message_saves_prompt_msg_id_and_start_action(monkeypatch, db_session):
    """
    Verifies that enforce_must_join_message:
    - Stores prompt message ID in Redis under ytdl:must_join_msg:{user_id}
    - Stores default action_type='start' when no pending_action provided
    """
    from tests.mock_redis import MockRedis
    from src.bot.keyboards import build_must_join_keyboard
    from src.core.constants import REDIS_KEY_MUST_JOIN_MSG_PREFIX

    mock_r = MockRedis()
    monkeypatch.setattr("src.services.must_join_service.get_redis_client", lambda: mock_r)

    # Add required channel
    ch = RequiredChannel(chat_id=-1001, title="Test Channel", invite_url="https://t.me/+join_test", enabled=True)
    db_session.add(ch)
    await db_session.commit()

    bot = AsyncMock(spec=Bot)
    msg = AsyncMock(spec=Message)
    msg.from_user = MagicMock(id=555444, username="testuser", first_name="Test")
    msg.chat = MagicMock(id=555444)

    sent_msg = MagicMock()
    sent_msg.chat.id = 555444
    sent_msg.message_id = 9876
    msg.answer = AsyncMock(return_value=sent_msg)

    # User is not a member
    monkeypatch.setattr(
        MustJoinService,
        "require_must_join",
        AsyncMock(return_value=(False, [ch])),
    )

    allowed = await MustJoinService.enforce_must_join_message(msg, bot, db_session)
    assert allowed is False

    # Check that prompt message ID was saved in Redis
    prompt_record = await mock_r.get(f"{REDIS_KEY_MUST_JOIN_MSG_PREFIX}555444")
    assert prompt_record == "555444:9876"

    # Check that pending action was stored as 'start'
    pending_key = "must_join:pending:555444"
    pending_val = await mock_r.get(pending_key)
    assert pending_val is not None
    assert '"action_type": "start"' in pending_val


@pytest.mark.asyncio
async def test_auto_unblock_on_chat_member_joined_deletes_prompt_and_sends_welcome(monkeypatch, db_session):
    """
    Verifies that check_and_auto_unblock_user:
    - Calls bot.delete_message on the recorded prompt message
    - Deletes prompt record from Redis
    - Sends Welcome message when pending action is 'start'
    """
    from tests.mock_redis import MockRedis
    from src.bot.handlers.must_join_handler import check_and_auto_unblock_user
    from src.core.constants import REDIS_KEY_MUST_JOIN_MSG_PREFIX

    mock_r = MockRedis()
    monkeypatch.setattr("src.bot.handlers.must_join_handler.get_redis_client", lambda: mock_r)
    monkeypatch.setattr("src.services.must_join_service.get_redis_client", lambda: mock_r)
    monkeypatch.setattr("src.bot.handlers.must_join_handler.AsyncSessionLocal", lambda: db_session)

    user_id = 777888
    # Seed prompt message in Redis
    await mock_r.set(f"{REDIS_KEY_MUST_JOIN_MSG_PREFIX}{user_id}", f"{user_id}:12345")
    # Seed pending start action using service method
    await MustJoinService.save_pending_action(user_id=user_id, chat_id=user_id, action_type="start", payload={})

    bot = AsyncMock(spec=Bot)
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock()

    # User is authorized (joined all required channels)
    monkeypatch.setattr(
        MustJoinService,
        "require_must_join",
        AsyncMock(return_value=(True, [])),
    )

    unblocked = await check_and_auto_unblock_user(bot, user_id, first_name="Farzad")
    assert unblocked is True

    # Verified bot.delete_message was called with prompt msg id
    bot.delete_message.assert_called_once_with(chat_id=user_id, message_id=12345)

    # Prompt was deleted from Redis
    assert await mock_r.get(f"{REDIS_KEY_MUST_JOIN_MSG_PREFIX}{user_id}") is None

    # Welcome message was sent and {first_name} was replaced with Farzad
    assert bot.send_message.called
    sent_text = bot.send_message.call_args.kwargs["text"]
    assert "{first_name}" not in sent_text
    assert "Farzad" in sent_text


@pytest.mark.asyncio
async def test_auto_unblock_resumes_pending_url(monkeypatch, db_session):
    """
    Verifies that check_and_auto_unblock_user automatically resumes YouTube URL processing
    when pending action is 'url'.
    """
    from tests.mock_redis import MockRedis
    from src.bot.handlers.must_join_handler import check_and_auto_unblock_user
    from src.core.constants import REDIS_KEY_MUST_JOIN_MSG_PREFIX

    mock_r = MockRedis()
    monkeypatch.setattr("src.bot.handlers.must_join_handler.get_redis_client", lambda: mock_r)
    monkeypatch.setattr("src.services.must_join_service.get_redis_client", lambda: mock_r)
    monkeypatch.setattr("src.bot.handlers.must_join_handler.AsyncSessionLocal", lambda: db_session)

    user_id = 333444
    target_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    # Seed prompt message in Redis
    await mock_r.set(f"{REDIS_KEY_MUST_JOIN_MSG_PREFIX}{user_id}", f"{user_id}:999")
    # Seed pending action using service method
    await MustJoinService.save_pending_action(
        user_id=user_id,
        chat_id=user_id,
        action_type="url",
        payload={"url": target_url, "source_id": "test_src_123"},
    )

    bot = AsyncMock(spec=Bot)
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock()

    processed_calls = []

    async def mock_process(bot, chat_id, url, source_id, user_id=None):
        processed_calls.append((chat_id, url, source_id, user_id))

    monkeypatch.setattr("src.bot.handlers.url_handler.process_youtube_url", mock_process)
    monkeypatch.setattr(
        MustJoinService,
        "require_must_join",
        AsyncMock(return_value=(True, [])),
    )

    unblocked = await check_and_auto_unblock_user(bot, user_id)
    assert unblocked is True

    # Prompt message was deleted
    bot.delete_message.assert_called_once_with(chat_id=user_id, message_id=999)

    # URL process was resumed
    assert len(processed_calls) == 1
    assert processed_calls[0] == (user_id, target_url, "test_src_123", user_id)




