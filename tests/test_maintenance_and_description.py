import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from aiogram.types import CallbackQuery, Message, User

from src.bot.middlewares.maintenance_middleware import MaintenanceMiddleware
from src.bot.handlers.quality_handler import on_show_description
from src.bot.keyboards import build_quality_keyboard
from src.services.ytdlp_service import YtDlpService


@pytest.mark.asyncio
async def test_maintenance_middleware_allows_when_disabled():
    middleware = MaintenanceMiddleware()
    handler = AsyncMock(return_value="OK")
    event = MagicMock(spec=Message)
    data = {"event_from_user": User(id=111, is_bot=False, first_name="User")}

    with patch("src.services.setting_service.SettingService.is_maintenance_mode", new_callable=AsyncMock, return_value=False):
        res = await middleware(handler, event, data)
        assert res == "OK"
        handler.assert_awaited_once_with(event, data)


@pytest.mark.asyncio
async def test_maintenance_middleware_allows_admin():
    middleware = MaintenanceMiddleware()
    handler = AsyncMock(return_value="OK")
    event = MagicMock(spec=Message)
    data = {"event_from_user": User(id=999, is_bot=False, first_name="Admin")}

    with patch("src.services.setting_service.SettingService.is_maintenance_mode", new_callable=AsyncMock, return_value=True), \
         patch("src.services.setting_service.SettingService.is_user_maintenance_admin", new_callable=AsyncMock, return_value=True):
        res = await middleware(handler, event, data)
        assert res == "OK"
        handler.assert_awaited_once_with(event, data)


@pytest.mark.asyncio
async def test_maintenance_middleware_blocks_regular_user_message():
    middleware = MaintenanceMiddleware()
    handler = AsyncMock(return_value="OK")
    event = MagicMock(spec=Message)
    event.reply = AsyncMock()
    data = {"event_from_user": User(id=111, is_bot=False, first_name="User")}

    with patch("src.services.setting_service.SettingService.is_maintenance_mode", new_callable=AsyncMock, return_value=True), \
         patch("src.services.setting_service.SettingService.is_user_maintenance_admin", new_callable=AsyncMock, return_value=False), \
         patch("src.services.setting_service.SettingService.get_maintenance_message", new_callable=AsyncMock, return_value="🛠 Maintenance Active"):
        res = await middleware(handler, event, data)
        assert res is None
        handler.assert_not_awaited()
        event.reply.assert_awaited_once_with("🛠 Maintenance Active")


@pytest.mark.asyncio
async def test_maintenance_middleware_blocks_regular_user_callback():
    middleware = MaintenanceMiddleware()
    handler = AsyncMock(return_value="OK")
    event = MagicMock(spec=CallbackQuery)
    event.answer = AsyncMock()
    data = {"event_from_user": User(id=111, is_bot=False, first_name="User")}

    with patch("src.services.setting_service.SettingService.is_maintenance_mode", new_callable=AsyncMock, return_value=True), \
         patch("src.services.setting_service.SettingService.is_user_maintenance_admin", new_callable=AsyncMock, return_value=False):
        res = await middleware(handler, event, data)
        assert res is None
        handler.assert_not_awaited()
        event.answer.assert_awaited_once()


def test_keyboard_contains_thumbnail_and_description_buttons():
    kb = build_quality_keyboard("test12345", [1080, 720])
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    btn_texts = [b.text for b in buttons]
    btn_datas = [b.callback_data for b in buttons]

    assert "🖼 Thumbnail" in btn_texts
    assert "📝 Description" in btn_texts
    assert "thumb:test12345" in btn_datas
    assert "desc:test12345" in btn_datas


@pytest.mark.asyncio
async def test_on_show_description_expandable_blockquote():
    callback = MagicMock()
    callback.data = "desc:testvid1234"
    callback.message = MagicMock()
    callback.message.chat.id = 555
    callback.from_user = MagicMock()
    callback.from_user.id = 555
    callback.answer = AsyncMock()

    bot = MagicMock()
    bot.send_message = AsyncMock()

    sample_desc = "Awesome song <lyrics> & track info!"
    with patch("src.services.must_join_service.MustJoinService.enforce_must_join_callback", new_callable=AsyncMock, return_value=True), \
         patch("src.services.ytdlp_service.YtDlpService.extract_metadata", new_callable=AsyncMock, return_value={"description": sample_desc}):
        await on_show_description(callback, bot)

        bot.send_message.assert_awaited_once()
        args, kwargs = bot.send_message.call_args
        assert kwargs["chat_id"] == 555
        # Must be wrapped in expandable quote and HTML escaped
        assert "<blockquote expandable>" in kwargs["text"]
        assert "&lt;lyrics&gt; &amp; track info!" in kwargs["text"]
        assert "</blockquote>" in kwargs["text"]


@pytest.mark.asyncio
async def test_4k_resolution_allowed_when_max_height_2160():
    mock_info = {
        "formats": [
            {"vcodec": "av01.0.08m", "height": 2160},
            {"vcodec": "vp09.00.51", "height": 1440},
            {"vcodec": "avc1.640028", "height": 1080},
            {"vcodec": "avc1.4d401f", "height": 720},
        ]
    }
    # When max_height is 2160, 4K and 1440p should be present
    res_4k = YtDlpService.get_available_resolutions(mock_info, max_height=2160)
    assert res_4k == [2160, 1440, 1080, 720]

    # Codec auto-selection for 2160 should select VP9 or AV1
    codec = YtDlpService.auto_select_codec(mock_info, 2160)
    assert codec in ("VP9", "AV1")
