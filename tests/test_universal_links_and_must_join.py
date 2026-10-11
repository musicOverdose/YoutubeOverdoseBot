import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.services.ytdlp_service import contains_url, extract_youtube_id
from src.services.setting_service import (
    DEFAULT_UNSUPPORTED_LINK_MESSAGE,
    SETTING_UNSUPPORTED_LINK_MSG,
    SettingService,
)
from src.web.routes.must_join_routes import AddChannelRequest, add_channel, sync_channel_info


def test_universal_youtube_link_extraction():
    # User's exact examples from prompt:
    user_google_link_1 = (
        "https://www.google.com/url?sa=t&source=web&rct=j&opi=89978449&"
        "url=https://www.youtube.com/watch%3Fv%3DjNQXAC9IVRw%26vl%3Den&"
        "ved=2ahUKEwj-hITmrrGXAxWbyAIHHbRtDD0QwqsBegQIHhAB&usg=AOvVaw0eeF4NOkl-4toVirRhsJZx"
    )
    user_google_link_2 = (
        "https://www.google.com/url?sa=t&source=web&rct=j&opi=89978449&"
        "url=https://www.youtube.com/watch%3Fv%3DjNQXAC9IVRw%26vl%3Den"
    )

    assert extract_youtube_id(user_google_link_1) == "jNQXAC9IVRw"
    assert extract_youtube_id(user_google_link_2) == "jNQXAC9IVRw"

    # Google with q= parameter
    google_q_link = "https://www.google.com/url?q=https://youtu.be/jNQXAC9IVRw&sa=U"
    assert extract_youtube_id(google_q_link) == "jNQXAC9IVRw"

    # Other YouTube formats:
    assert extract_youtube_id("https://youtu.be/jNQXAC9IVRw?si=12345") == "jNQXAC9IVRw"
    assert extract_youtube_id("https://www.youtube.com/shorts/jNQXAC9IVRw") == "jNQXAC9IVRw"
    assert extract_youtube_id("https://m.youtube.com/watch?feature=share&v=jNQXAC9IVRw") == "jNQXAC9IVRw"
    assert extract_youtube_id("https://music.youtube.com/watch?v=jNQXAC9IVRw") == "jNQXAC9IVRw"
    assert extract_youtube_id("https://www.youtube.com/live/jNQXAC9IVRw?feature=shared") == "jNQXAC9IVRw"
    assert extract_youtube_id("https://www.youtube.com/embed/jNQXAC9IVRw") == "jNQXAC9IVRw"
    assert extract_youtube_id("https://www.youtube.com/v/jNQXAC9IVRw") == "jNQXAC9IVRw"
    assert extract_youtube_id("https://www.youtube.com/attribution_link?a=xyz&u=/watch%3Fv%3DjNQXAC9IVRw") == "jNQXAC9IVRw"

    # Share text from mobile apps
    share_text = "Check out this video on YouTube: https://youtu.be/jNQXAC9IVRw ! It's awesome."
    assert extract_youtube_id(share_text) == "jNQXAC9IVRw"

    # URL without scheme
    assert extract_youtube_id("youtube.com/watch?v=jNQXAC9IVRw") == "jNQXAC9IVRw"


def test_contains_url_and_unsupported_detection():
    # Plain text without links:
    assert contains_url("hello how are you") is False
    assert contains_url("salam khobi") is False
    assert contains_url("can you download this video please") is False
    assert contains_url("") is False
    assert contains_url(None) is False

    # Messages with links:
    assert contains_url("https://instagram.com/reel/123456") is True
    assert contains_url("check out www.tiktok.com/@user/video/123") is True
    assert contains_url("look at example.com/page") is True

    # Telegram entity detection:
    mock_entity = MagicMock(type="url")
    assert contains_url("look here", entities=[mock_entity]) is True


def test_unsupported_link_filter_condition():
    # Helper replicating the handler filter
    def is_unsupported_link(text: str, entities: list = None) -> bool:
        return (
            bool(text)
            and not text.startswith("/")
            and contains_url(text, entities)
            and extract_youtube_id(text) is None
        )

    # Plain text should NOT trigger unsupported link error
    assert is_unsupported_link("hello world") is False
    assert is_unsupported_link("thanks for downloading") is False
    assert is_unsupported_link("/help") is False
    assert is_unsupported_link("/start") is False

    # Valid YouTube link should NOT trigger unsupported link error
    assert is_unsupported_link("https://youtu.be/jNQXAC9IVRw") is False
    assert is_unsupported_link("https://www.youtube.com/watch?v=jNQXAC9IVRw") is False
    assert is_unsupported_link("https://www.google.com/url?url=https://www.youtube.com/watch%3Fv%3DjNQXAC9IVRw") is False

    # Unsupported URLs SHOULD trigger unsupported link error
    assert is_unsupported_link("https://instagram.com/reel/Cxyz123") is True
    assert is_unsupported_link("https://tiktok.com/@test/video/1234") is True
    assert is_unsupported_link("https://vimeo.com/1234567") is True
    assert is_unsupported_link("www.example.com") is True


@pytest.mark.asyncio
async def test_unsupported_link_setting_service():
    session = AsyncMock()

    with patch.object(SettingService, "get_active_setting", return_value=None):
        default_msg = await SettingService.get_unsupported_link_message(session)
        assert default_msg == DEFAULT_UNSUPPORTED_LINK_MESSAGE

    with patch.object(SettingService, "get_active_setting", return_value="<b>Custom error</b>"):
        custom_msg = await SettingService.get_unsupported_link_message(session)
        assert custom_msg == "<b>Custom error</b>"


@pytest.mark.asyncio
async def test_must_join_auto_fetch_by_uid():
    mock_chat = MagicMock()
    mock_chat.id = -1004324324324
    mock_chat.title = "Music Overdose Channel"
    mock_chat.username = "musicoverdose"
    mock_chat.invite_link = "https://t.me/musicoverdose"

    mock_bot = AsyncMock()
    mock_bot.get_chat.return_value = mock_chat

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None

    session = AsyncMock()
    session.execute.return_value = mock_result

    req = AddChannelRequest(
        chat_id="-1004324324324",
        # Notice title, username, invite_url are omitted!
    )

    with patch("src.web.routes.must_join_routes.get_bot", return_value=mock_bot):
        with patch("src.services.must_join_service.MustJoinService.verify_bot_is_admin", return_value=(True, "Bot is administrator")):
            with patch("src.services.audit_service.AuditService.log_action", return_value=None):
                res = await add_channel(req, session, admin={"sub": "admin"})

    assert res["status"] == "added"
    assert res["chat_id"] == -1004324324324
    assert res["title"] == "Music Overdose Channel"
    assert res["username"] == "musicoverdose"
    assert res["invite_url"] == "https://t.me/musicoverdose"
