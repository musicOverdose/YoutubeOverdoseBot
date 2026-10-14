import asyncio
import json
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.constants import JobStatus
from src.models.job import Job
from src.models.job_request import JobRequest
from src.services.queue_service import QueueService
from src.services.ytdlp_service import YtDlpService
from src.worker.notifier import StatusNotifier
from src.bot.keyboards import (
    build_queue_status_keyboard,
)
from src.bot.handlers.url_handler import process_youtube_url
from src.bot.handlers.queue_handler import on_cancel_job


# ==============================================================================
# 1. METADATA PERFORMANCE & CONFIGURATION TESTS
# ==============================================================================

def test_metadata_options_exclude_download_only_settings():
    """Verifies that metadata options do NOT contain heavy download settings."""
    meta_opts = YtDlpService.get_metadata_opts()
    
    assert "concurrent_fragments" not in meta_opts
    assert "buffersize" not in meta_opts
    assert "trim_file_name" not in meta_opts
    assert "fragment_retries" not in meta_opts
    assert "skip_unavailable_fragments" not in meta_opts
    assert "retry_sleep_functions" not in meta_opts


def test_download_options_retain_required_settings():
    """Verifies that download options retain fragment concurrency and large buffers."""
    dl_opts = YtDlpService.get_download_opts()
    
    assert dl_opts["concurrent_fragments"] == 16
    assert dl_opts["buffersize"] == 4194304
    assert dl_opts["trim_file_name"] == 220
    assert dl_opts["retries"] == 10
    assert dl_opts["fragment_retries"] == 10
    assert dl_opts["skip_unavailable_fragments"] is True
    assert "retry_sleep_functions" in dl_opts
    assert dl_opts["socket_timeout"] == 30


def test_socket_timeout_and_retries_configured_correctly():
    """Verifies metadata options have tight timeout and small retries."""
    meta_opts = YtDlpService.get_metadata_opts()
    
    assert meta_opts["socket_timeout"] == 10
    assert meta_opts["retries"] == 2
    assert meta_opts["extractor_retries"] == 2


def test_dash_manifest_discovery_remains_enabled():
    """Ensures DASH manifest extraction is NOT globally disabled in yt-dlp options."""
    meta_opts = YtDlpService.get_metadata_opts()
    assert meta_opts.get("youtube_include_dash_manifest") is not False
    
    dl_opts = YtDlpService.get_download_opts()
    assert dl_opts.get("youtube_include_dash_manifest") is not False


def test_ipv4_configuration_applied():
    """Confirms IPv4 source_address binding is present in common options."""
    common_opts = YtDlpService.get_common_base_opts()
    assert common_opts["source_address"] == "0.0.0.0"
    
    meta_opts = YtDlpService.get_metadata_opts()
    assert meta_opts["source_address"] == "0.0.0.0"


@pytest.mark.asyncio
async def test_cache_hits_avoid_repeated_extraction():
    """Verifies that subsequent extraction calls for the same URL hit the cache without calling yt-dlp."""
    from src.services.ytdlp_service import _metadata_cache
    _metadata_cache.clear()
    
    test_id = "cachevid999"
    test_url = f"https://www.youtube.com/watch?v={test_id}"
    fake_info = {
        "id": test_id,
        "title": "Cached Song",
        "formats": [{"format_id": "18", "height": 360}],
    }
    
    try:
        with patch("src.services.ytdlp_service.yt_dlp.YoutubeDL") as mock_ydl, \
             patch("src.services.ytdlp_service.get_redis_client") as mock_redis_func:
            mock_r = AsyncMock()
            mock_r.get.return_value = json.dumps(fake_info)
            mock_redis_func.return_value = mock_r
            
            info = await YtDlpService.extract_metadata(test_url, use_cache=True)
            assert info["title"] == "Cached Song"
            mock_ydl.assert_not_called()
    finally:
        _metadata_cache.clear()


# ==============================================================================
# 2. EXTRACTION UX TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_extracting_message_does_not_attach_cancel_button():
    """Verifies that the initial extracting message is sent without reply markup (no cancel button)."""
    bot_mock = AsyncMock()
    status_msg = AsyncMock()
    bot_mock.send_message.return_value = status_msg
    
    simulated_info = {
        "id": "dQw4w9WgXcQ",
        "title": "Fast Video",
        "duration": 120,
        "formats": [{"format_id": "18", "height": 360}],
    }
    
    with patch("src.services.ytdlp_service.YtDlpService.extract_metadata", new=AsyncMock(return_value=simulated_info)), \
         patch("src.services.thumbnail_service.ThumbnailService.prepare_preview_photo", new=AsyncMock(return_value=False)):
        
        await process_youtube_url(
            bot=bot_mock,
            chat_id=222,
            canonical_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            source_id="dQw4w9WgXcQ",
            user_id=111,
        )
        
        assert bot_mock.send_message.call_count >= 1
        first_call = bot_mock.send_message.call_args_list[0]
        assert "Extracting video information" in first_call.kwargs.get("text", "")
        assert first_call.kwargs.get("reply_markup") is None


# ==============================================================================
# 3. QUEUED JOB CANCELLATION TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_queued_job_cancelled_atomically():
    """Verifies that an owner cancelling a QUEUED job removes it from Redis and marks it CANCELLED."""
    callback = MagicMock()
    callback.data = "q_cancel:job-abc"
    callback.from_user.id = 555
    callback.message = AsyncMock()
    callback.answer = AsyncMock()
    
    mock_job = MagicMock()
    mock_job.id = "job-abc"
    mock_job.status = JobStatus.QUEUED.value
    mock_job.queue_type = "VIDEO"
    mock_job.cache_key = "test-cache-key"
    
    mock_user_req = MagicMock()
    
    with patch("src.bot.handlers.queue_handler.AsyncSessionLocal") as mock_session_cls, \
         patch("src.bot.handlers.queue_handler.QueueService.remove_from_queue", new=AsyncMock(return_value=True)) as mock_rm, \
         patch("src.bot.handlers.queue_handler.QueueService.signal_cancel", new=AsyncMock()) as mock_sig, \
         patch("src.bot.handlers.queue_handler.QueueService.release_cache_lock", new=AsyncMock()) as mock_rel:
        
        session = AsyncMock()
        mock_session_cls.return_value.__aenter__.return_value = session
        
        res1 = MagicMock()
        res1.scalar_one_or_none.return_value = mock_user_req
        res2 = MagicMock()
        res2.scalar_one_or_none.return_value = mock_job
        res3 = MagicMock()
        res3.scalar.return_value = 0
        session.execute.side_effect = [res1, res2, res3]
        
        await on_cancel_job(callback, AsyncMock())
        
        mock_rm.assert_awaited_once_with("job-abc", queue_type="VIDEO")
        mock_sig.assert_awaited_once_with("job-abc")
        mock_rel.assert_awaited_once_with("test-cache-key", "job-abc")
        assert mock_job.status == JobStatus.CANCELLED.value
        assert callback.message.edit_text.called
        assert "removed from queue" in callback.message.edit_text.call_args[0][0].lower()


@pytest.mark.asyncio
async def test_cancellation_race_with_job_acquisition_falls_back_to_dismissal():
    """
    If the worker acquired the job right before user clicked cancel (job not removed from queue),
    it must NOT mark the active job cancelled or terminate it. It falls back to UI dismissal.
    """
    callback = MagicMock()
    callback.data = "q_cancel:job-xyz"
    callback.from_user.id = 555
    callback.message = AsyncMock()
    callback.message.chat.id = 1234
    callback.answer = AsyncMock()
    
    mock_job = MagicMock()
    mock_job.id = "job-xyz"
    mock_job.status = JobStatus.QUEUED.value
    mock_job.queue_type = "VIDEO"
    
    mock_user_req = MagicMock()
    
    with patch("src.bot.handlers.queue_handler.AsyncSessionLocal") as mock_session_cls, \
         patch("src.bot.handlers.queue_handler.QueueService.remove_from_queue", new=AsyncMock(return_value=False)) as mock_rm:
        
        session = AsyncMock()
        mock_session_cls.return_value.__aenter__.return_value = session
        
        res1 = MagicMock()
        res1.scalar_one_or_none.return_value = mock_user_req
        res2 = MagicMock()
        res2.scalar_one_or_none.return_value = mock_job
        session.execute.side_effect = [res1, res2]
        
        await on_cancel_job(callback, AsyncMock())
        
        assert mock_job.status == JobStatus.QUEUED.value
        callback.answer.assert_awaited_once_with(
            "Job has already started downloading and cannot be cancelled.", show_alert=True
        )
        callback.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)


# ==============================================================================
# 4. ACTIVE JOB PROGRESS CLEANLINESS TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_status_notifier_does_not_attach_dismiss_button_to_active_progress():
    """Verifies that StatusNotifier keeps active download/processing progress bar clean without buttons."""
    bot_mock = AsyncMock()
    requests = [
        {"chat_id": 100, "message_id": 1},
        {"chat_id": 200, "message_id": 2},
    ]
    notifier = StatusNotifier(bot=bot_mock, requests=requests, job_id="job-active-99")
    
    await notifier.update("⬇️", "Downloading", "50%", force=True)
    
    assert bot_mock.edit_message_text.call_count == 2
    for call in bot_mock.edit_message_text.call_args_list:
        assert call.kwargs.get("reply_markup") is None


# ==============================================================================
# 5. POSITION 1 PROGRESS BAR & CLEAN QUEUE TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_position_1_with_idle_worker_shows_progress_bar_directly():
    """Verifies that if user is at position 1 and worker is idle, progress bar is shown directly without queue message."""
    from src.bot.handlers.codec_handler import on_codec_selected
    callback = MagicMock()
    callback.data = "c:vid123:H264:720"
    callback.from_user.id = 111
    callback.message = AsyncMock()
    callback.message.chat.id = 222
    callback.message.message_id = 333
    callback.message.answer = AsyncMock()
    callback.answer = AsyncMock()

    simulated_info = {
        "id": "vid123",
        "title": "Test Video",
        "duration": 60,
        "formats": [
            {"format_id": "137", "height": 720, "vcodec": "avc1.64001f", "acodec": "none", "filesize": 5000000},
            {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "filesize": 1000000},
        ],
    }

    with patch("src.bot.handlers.codec_handler.AsyncSessionLocal") as mock_session_cls, \
         patch("src.bot.handlers.codec_handler.MustJoinService.enforce_must_join_callback", new=AsyncMock(return_value=True)), \
         patch("src.bot.handlers.codec_handler.YtDlpService.extract_metadata", new=AsyncMock(return_value=simulated_info)), \
         patch("src.bot.handlers.codec_handler.CacheService.get_cached_entry", new=AsyncMock(return_value=None)), \
         patch("src.bot.handlers.codec_handler.QueueService.check_user_limits", new=AsyncMock(return_value=(True, None))), \
         patch("src.bot.handlers.codec_handler.QueueService.push_job", new=AsyncMock(return_value=1)), \
         patch("src.bot.handlers.codec_handler.QueueService.get_active_job_ids", new=AsyncMock(return_value=[])), \
         patch("src.bot.handlers.codec_handler.get_redis_client"):

        session = AsyncMock()
        mock_res = MagicMock()
        mock_res.scalars.return_value.first.return_value = None
        session.execute.return_value = mock_res
        mock_session_cls.return_value.__aenter__.return_value = session

        await on_codec_selected(callback, AsyncMock())

        callback.message.answer.assert_called_once()
        text = callback.message.answer.call_args[0][0]
        markup = callback.message.answer.call_args.kwargs.get("reply_markup")

        # Must show progress bar directly, without buttons
        assert "Please wait..." in text
        assert "Downloading..." in text
        assert markup is None
        callback.answer.assert_awaited_once_with("Starting download...")


@pytest.mark.asyncio
async def test_position_greater_than_1_shows_clean_queue_message_with_cancel_only():
    """Verifies that if user is at position > 1 or worker is busy, clean queue message with only cancel is shown."""
    from src.bot.handlers.codec_handler import on_codec_selected
    callback = MagicMock()
    callback.data = "c:vid123:H264:720"
    callback.from_user.id = 111
    callback.message = AsyncMock()
    callback.message.chat.id = 222
    callback.message.message_id = 333
    callback.message.answer = AsyncMock()
    callback.answer = AsyncMock()

    simulated_info = {
        "id": "vid123",
        "title": "Test Video",
        "duration": 60,
        "formats": [
            {"format_id": "137", "height": 720, "vcodec": "avc1.64001f", "acodec": "none", "filesize": 5000000},
            {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "filesize": 1000000},
        ],
    }

    with patch("src.bot.handlers.codec_handler.AsyncSessionLocal") as mock_session_cls, \
         patch("src.bot.handlers.codec_handler.MustJoinService.enforce_must_join_callback", new=AsyncMock(return_value=True)), \
         patch("src.bot.handlers.codec_handler.YtDlpService.extract_metadata", new=AsyncMock(return_value=simulated_info)), \
         patch("src.bot.handlers.codec_handler.CacheService.get_cached_entry", new=AsyncMock(return_value=None)), \
         patch("src.bot.handlers.codec_handler.QueueService.check_user_limits", new=AsyncMock(return_value=(True, None))), \
         patch("src.bot.handlers.codec_handler.QueueService.push_job", new=AsyncMock(return_value=2)), \
         patch("src.bot.handlers.codec_handler.QueueService.get_active_job_ids", new=AsyncMock(return_value=["other-job"])), \
         patch("src.bot.handlers.codec_handler.get_redis_client"):

        session = AsyncMock()
        mock_res = MagicMock()
        mock_res.scalars.return_value.first.return_value = None
        session.execute.return_value = mock_res
        mock_session_cls.return_value.__aenter__.return_value = session

        await on_codec_selected(callback, AsyncMock())

        callback.message.answer.assert_called_once()
        text = callback.message.answer.call_args[0][0]
        markup = callback.message.answer.call_args.kwargs.get("reply_markup")

        # Must show clean queue message
        assert "Queued" in text
        assert "Position #2" in text
        assert "Active:" not in text  # Bloat removed
        assert markup is not None
        all_buttons = [btn for row in markup.inline_keyboard for btn in row]
        assert len(all_buttons) == 1
        assert all_buttons[0].callback_data.startswith("q_cancel:")
        callback.answer.assert_awaited_once_with("Added to queue!")
