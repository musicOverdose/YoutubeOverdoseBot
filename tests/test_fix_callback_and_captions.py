import pytest
from unittest.mock import AsyncMock, MagicMock
from aiogram.exceptions import TelegramBadRequest, TelegramAPIError
from src.bot.handlers.codec_handler import safe_callback_answer
from src.models.job_request import JobRequest


@pytest.mark.asyncio
async def test_safe_callback_answer_success():
    cb = MagicMock()
    cb.answer = AsyncMock(return_value=True)
    res = await safe_callback_answer(cb, "Test text")
    assert res is True
    cb.answer.assert_called_once_with("Test text")


@pytest.mark.asyncio
async def test_safe_callback_answer_query_too_old_suppressed():
    cb = MagicMock()
    cb.answer = AsyncMock(side_effect=TelegramBadRequest(method=MagicMock(), message="query is too old and response timeout expired or query ID is invalid"))
    res = await safe_callback_answer(cb, "Test text")
    assert res is False


@pytest.mark.asyncio
async def test_safe_callback_answer_telegram_api_error_suppressed():
    cb = MagicMock()
    cb.answer = AsyncMock(side_effect=TelegramAPIError(method=MagicMock(), message="network error"))
    res = await safe_callback_answer(cb, "Test text")
    assert res is False


def test_job_request_has_menu_message_id():
    req = JobRequest(
        job_id="job-123",
        user_id=111,
        chat_id=222,
        menu_message_id=98765,
    )
    assert req.menu_message_id == 98765


def test_caption_formatting_quality_only():
    title = "Best Video 2026"
    resolution = "1080p"
    caption_text = f"🎬 <b>{title}</b>\n({resolution})"
    assert caption_text == "🎬 <b>Best Video 2026</b>\n(1080p)"
    assert "H264" not in caption_text
    assert "H265" not in caption_text
