from unittest.mock import AsyncMock, MagicMock
import pytest
from src.core.config import settings
from src.models.channel import RequiredChannel
from src.services.must_join_service import MustJoinService, is_active_member


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
    - Final '✅ I Joined' callback button strictly using 'must_join:check'
    """
    from src.bot.keyboards import build_must_join_keyboard

    ch1 = RequiredChannel(chat_id=-1001, title="Channel Public", username="public_chan", enabled=True)
    ch2 = RequiredChannel(chat_id=-1002, title="Channel Invite", invite_url="https://t.me/+AbCdEf", enabled=True)
    ch3 = RequiredChannel(chat_id=-1003, title="Channel No Link", enabled=True)

    kb = build_must_join_keyboard([ch1, ch2, ch3])
    buttons = kb.inline_keyboard

    assert len(buttons) == 4

    # ch1: username -> https://t.me/public_chan
    assert buttons[0][0].text == "Channel Public"
    assert buttons[0][0].url == "https://t.me/public_chan"

    # ch2: invite_url -> https://t.me/+AbCdEf
    assert buttons[1][0].text == "Channel Invite"
    assert buttons[1][0].url == "https://t.me/+AbCdEf"

    # ch3: no link -> explicit fallback button
    assert buttons[2][0].text == "Channel No Link (No Link Configured)"
    assert buttons[2][0].callback_data == "must_join:no_url"

    # Final button: strictly fixed callback
    assert buttons[3][0].text == "✅ I Joined"
    assert buttons[3][0].callback_data == "must_join:check"


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
    # Check that channel buttons are present
    assert len(call_kwargs["reply_markup"].inline_keyboard) == 2

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


