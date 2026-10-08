import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import ASGITransport, AsyncClient

from src.core.security import create_session_token
from src.services.setting_service import (
    DEFAULT_HELP_MESSAGE,
    SettingService,
)
from src.services.ytdlp_service import YtDlpService
from src.bot.handlers.base import cmd_help
from src.web.app import app


@pytest.mark.asyncio
async def test_help_message_lifecycle():
    # 1. Default help message
    default_msg = await SettingService.get_help_message()
    assert default_msg == DEFAULT_HELP_MESSAGE
    assert "Youtube Overdose" in default_msg

    # 2. Save custom help message
    custom_msg = "<b>Custom Help!</b>\n1. Send link\n2. Done!"
    saved = await SettingService.save_help_message(custom_msg)
    assert saved == custom_msg

    current = await SettingService.get_help_message()
    assert current == custom_msg

    # 3. Reset to default
    reset = await SettingService.reset_help_message()
    assert reset == DEFAULT_HELP_MESSAGE
    assert await SettingService.get_help_message() == DEFAULT_HELP_MESSAGE


@pytest.mark.asyncio
async def test_help_message_api():
    admin_token = create_session_token("admin", role="ADMIN")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set("session_token", admin_token)
        client.headers.update({"Authorization": f"Bearer {admin_token}"})

        # GET help-message
        res = await client.get("/api/telegram/help-message")
        assert res.status_code == 200
        data = res.json()
        assert "message" in data

        # POST help-message
        new_msg = "📖 <b>Updated Help</b>\nSend link to download!"
        res_post = await client.post(
            "/api/telegram/help-message",
            json={"message": new_msg},
        )
        assert res_post.status_code == 200
        assert res_post.json()["message"] == new_msg

        # POST reset
        res_reset = await client.post(
            "/api/telegram/help-message/reset",
        )
        assert res_reset.status_code == 200
        assert res_reset.json()["status"] == "reset"


@pytest.mark.asyncio
async def test_cmd_help_handler():
    message = MagicMock()
    message.answer = AsyncMock()

    await cmd_help(message)
    message.answer.assert_called_once()
    sent_text = message.answer.call_args[0][0]
    assert "Youtube Overdose" in sent_text or "Help" in sent_text


def test_ytdlp_144p_cutoff():
    # Mock video info with 1080p, 720p, 144p, and 90p formats
    info = {
        "formats": [
            {"format_id": "137", "vcodec": "avc1.640028", "height": 1080, "width": 1920},
            {"format_id": "136", "vcodec": "avc1.4d401f", "height": 720, "width": 1280},
            {"format_id": "160", "vcodec": "avc1.4d400c", "height": 144, "width": 256},
            {"format_id": "99", "vcodec": "avc1.4d4005", "height": 90, "width": 160}, # Lower than 144p!
            {"format_id": "sb0", "format_note": "storyboard", "height": 90, "width": 160},
        ]
    }
    resolutions = YtDlpService.get_available_resolutions(info)
    assert 1080 in resolutions
    assert 720 in resolutions
    assert 144 in resolutions
    assert 90 not in resolutions  # Must be filtered out!

    labels = YtDlpService.get_resolution_labels(info)
    assert 90 not in labels
    assert labels[144] == "144p"
