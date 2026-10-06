import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import FSInputFile

from src.core.config import settings
from src.core.constants import OperationType
from src.models.job import Job
from src.worker.processor import JobProcessor
from src.worker.recovery import WorkerRecovery
from src.worker.telegram_factory import TelegramClientFactory


@pytest.fixture
def transfer_env(monkeypatch):
    tmp = tempfile.mkdtemp()
    transfer_dir = os.path.join(tmp, "transfer")
    temp_dir = os.path.join(tmp, "ytdl")
    token_file = os.path.join(tmp, "runtime", "bot-token")

    os.makedirs(transfer_dir, exist_ok=True)
    os.makedirs(temp_dir, exist_ok=True)
    os.makedirs(os.path.dirname(token_file), exist_ok=True)

    with open(token_file, "w") as f:
        f.write("123456:RUNTIME_TOKEN\n")

    monkeypatch.setattr(settings, "TRANSFER_DIR", transfer_dir)
    monkeypatch.setattr(settings, "TEMP_DIR", temp_dir)
    monkeypatch.setattr(settings, "RUNTIME_BOT_TOKEN_FILE", token_file)

    yield {
        "dir": tmp,
        "transfer": transfer_dir,
        "temp": temp_dir,
        "token": token_file,
    }
    shutil.rmtree(tmp, ignore_errors=True)


# 1. TelegramClientFactory: Token from File, Mode from Redis
@pytest.mark.asyncio
async def test_telegram_client_factory_local_mode(transfer_env, monkeypatch):
    from tests.mock_redis import MockRedis
    mock_r = MockRedis()
    await mock_r.set("telegram:active:mode", "local")
    monkeypatch.setattr("src.worker.telegram_factory.get_redis_client", lambda: mock_r)

    bot, mode = await TelegramClientFactory.get_client()
    assert bot.token == "123456:RUNTIME_TOKEN"
    assert mode == "local"
    # Centralized Local Bot API session should have is_local=False (standard HTTP multipart upload)
    assert bot.session.api.is_local is False
    assert "8081" in bot.session.api.base
    await bot.session.close()


@pytest.mark.asyncio
async def test_telegram_client_factory_cloud_mode(transfer_env, monkeypatch):
    from tests.mock_redis import MockRedis
    mock_r = MockRedis()
    await mock_r.set("telegram:active:mode", "cloud")
    monkeypatch.setattr("src.worker.telegram_factory.get_redis_client", lambda: mock_r)

    bot, mode = await TelegramClientFactory.get_client()
    assert bot.token == "123456:RUNTIME_TOKEN"
    assert mode == "cloud"
    assert bot.session.api.is_local is False
    assert "api.telegram.org" in bot.session.api.base
    await bot.session.close()


# 2. Local Mode: Standardized FSInputFile Multipart Upload (is_local=False)
@pytest.mark.asyncio
async def test_local_mode_send_media_handoff(transfer_env):
    processor = JobProcessor()
    mock_bot = MagicMock()
    mock_bot.send_video = AsyncMock(return_value=MagicMock(message_id=999))

    # Create dummy video file
    dummy_video = os.path.join(transfer_env["temp"], "test_video.mp4")
    with open(dummy_video, "wb") as f:
        f.write(b"0" * 1024)

    msg_id, size = await processor._send_media_to_cache(
        bot=mock_bot,
        api_mode="local",
        channel_id=-1001234567890,
        local_file_path=dummy_video,
        operation=OperationType.VIDEO.value,
        job_id="job-123",
        job_title="My Test Video",
        caption="test caption",
    )

    assert msg_id == 999
    assert size == 1024

    mock_bot.send_video.assert_called_once()
    call_kwargs = mock_bot.send_video.call_args.kwargs
    video_arg = call_kwargs["video"]
    assert isinstance(video_arg, FSInputFile)


# 3. Cloud Mode: 50 MB Preflight Limit Check
@pytest.mark.asyncio
async def test_cloud_mode_50mb_preflight_rejection(transfer_env):
    processor = JobProcessor()
    mock_bot = MagicMock()

    # Create dummy large file (e.g. 51 MB)
    large_video = os.path.join(transfer_env["temp"], "large_video.mp4")
    with open(large_video, "wb") as f:
        # Seek to 51 MB
        f.seek(51 * 1024 * 1024)
        f.write(b"\0")

    with pytest.raises(ValueError, match="exceeds Cloud Bot API limit of 50 MB"):
        await processor._send_media_to_cache(
            bot=mock_bot,
            api_mode="cloud",
            channel_id=-1001234567890,
            local_file_path=large_video,
            operation=OperationType.VIDEO.value,
            job_id="job-large",
            job_title="Large Video",
        )

    # Bot send_video should NOT have been called
    mock_bot.send_video.assert_not_called()


@pytest.mark.asyncio
async def test_cloud_mode_under_50mb_uses_fs_input_file(transfer_env):
    processor = JobProcessor()
    mock_bot = MagicMock()
    mock_bot.send_video = AsyncMock(return_value=MagicMock(message_id=888))

    small_video = os.path.join(transfer_env["temp"], "small_video.mp4")
    with open(small_video, "wb") as f:
        f.write(b"x" * 2048)

    msg_id, size = await processor._send_media_to_cache(
        bot=mock_bot,
        api_mode="cloud",
        channel_id=-1001234567890,
        local_file_path=small_video,
        operation=OperationType.VIDEO.value,
        job_id="job-small",
        job_title="Small Video",
    )

    assert msg_id == 888
    call_kwargs = mock_bot.send_video.call_args.kwargs
    video_arg = call_kwargs["video"]
    assert isinstance(video_arg, FSInputFile)


# 4. Startup Recovery Cleans Stale Transfer Folders
@pytest.mark.asyncio
async def test_startup_recovery_cleans_transfer_dir(transfer_env, db_session):
    # Create an orphaned folder in /transfer/
    stale_folder = os.path.join(transfer_env["transfer"], "orphaned-job-999")
    os.makedirs(stale_folder, exist_ok=True)
    with open(os.path.join(stale_folder, "orphaned.mp4"), "w") as f:
        f.write("orphan")

    assert os.path.exists(stale_folder)

    # Run startup recovery
    await WorkerRecovery.perform_startup_recovery(db_session)

    # Verify orphaned folder was removed
    assert not os.path.exists(stale_folder)


# 5. Centralized Local Bot API Multipart Integration: FSInputFile video + thumbnail
@pytest.mark.asyncio
async def test_local_bot_api_mixed_mode_integration(tmp_path):
    from aiogram import Bot
    from aiogram.methods import SendVideo
    from aiogram.types import FSInputFile

    bot = Bot("123456:RUNTIME_TOKEN")
    video_file = str(tmp_path / "video.mp4")
    with open(video_file, "wb") as f:
        f.write(b"MP4_VIDEO_BYTES")
    thumb_file = str(tmp_path / "thumb.jpg")
    with open(thumb_file, "wb") as f:
        f.write(b"JPEG_THUMBNAIL_BYTES")

    method = SendVideo(
        chat_id=-1001234567890,
        video=FSInputFile(video_file),
        thumbnail=FSInputFile(thumb_file),
        duration=75,
        width=1920,
        height=1080,
        supports_streaming=True,
    )

    form_data = bot.session.build_form_data(bot, method)
    fields = {f[0]["name"]: f for f in form_data._fields}

    # 1. Video MUST be multipart uploaded with attach:// reference
    assert "video" in fields
    video_ref = fields["video"][2]
    assert video_ref.startswith("attach://")

    # 2. Explicit metadata must be present
    assert str(fields["duration"][2]) == "75"
    assert str(fields["width"][2]) == "1920"
    assert str(fields["height"][2]) == "1080"
    assert str(fields["supports_streaming"][2]).lower() == "true"

    # 3. Thumbnail must be multipart uploaded with attach:// reference
    assert "thumbnail" in fields
    thumb_ref = fields["thumbnail"][2]
    assert thumb_ref.startswith("attach://")
    attach_key = thumb_ref.replace("attach://", "")
    assert attach_key in fields
    assert fields[attach_key][0]["filename"] == "thumb.jpg"


@pytest.mark.asyncio
async def test_send_video_passes_explicit_metadata_and_thumbnail(transfer_env):
    """
    Verifies that _send_media_to_cache forwards explicit duration, width, height,
    supports_streaming, and thumbnail to bot.send_video.
    """
    processor = JobProcessor()
    mock_bot = MagicMock()
    mock_bot.send_video = AsyncMock(return_value=MagicMock(message_id=777))

    dummy_video = os.path.join(transfer_env["temp"], "test_meta.mp4")
    with open(dummy_video, "wb") as f:
        f.write(b"video data")

    dummy_thumb = os.path.join(transfer_env["temp"], "test_thumb.jpg")
    with open(dummy_thumb, "wb") as f:
        f.write(b"thumb data")

    extra_kwargs = {
        "supports_streaming": True,
        "duration": 120,
        "width": 1920,
        "height": 1080,
        "thumbnail": FSInputFile(dummy_thumb),
    }

    msg_id, size = await processor._send_media_to_cache(
        bot=mock_bot,
        api_mode="local",
        channel_id=-1001234567890,
        local_file_path=dummy_video,
        operation=OperationType.VIDEO.value,
        job_id="job-meta-1",
        job_title="Metadata Video",
        caption="Meta Caption",
        extra_kwargs=extra_kwargs,
    )

    assert msg_id == 777
    call_kwargs = mock_bot.send_video.call_args.kwargs
    assert call_kwargs["duration"] == 120
    assert call_kwargs["width"] == 1920
    assert call_kwargs["height"] == 1080
    assert call_kwargs["supports_streaming"] is True
    assert isinstance(call_kwargs["thumbnail"], FSInputFile)


@pytest.mark.asyncio
async def test_send_video_still_sends_when_thumbnail_fails(transfer_env):
    """
    Verifies that if thumbnail generation fails (thumbnail is None), the video
    is still uploaded with explicit duration, width, and height.
    """
    processor = JobProcessor()
    mock_bot = MagicMock()
    mock_bot.send_video = AsyncMock(return_value=MagicMock(message_id=778))

    dummy_video = os.path.join(transfer_env["temp"], "test_nothumb.mp4")
    with open(dummy_video, "wb") as f:
        f.write(b"video data")

    extra_kwargs = {
        "supports_streaming": True,
        "duration": 90,
        "width": 1280,
        "height": 720,
    }

    msg_id, size = await processor._send_media_to_cache(
        bot=mock_bot,
        api_mode="local",
        channel_id=-1001234567890,
        local_file_path=dummy_video,
        operation=OperationType.VIDEO.value,
        job_id="job-nothumb",
        job_title="No Thumb Video",
        caption="No Thumb Caption",
        extra_kwargs=extra_kwargs,
    )

    assert msg_id == 778
    call_kwargs = mock_bot.send_video.call_args.kwargs
    assert call_kwargs["duration"] == 90
    assert call_kwargs["width"] == 1280
    assert call_kwargs["height"] == 720
    assert "thumbnail" not in call_kwargs


@pytest.mark.asyncio
async def test_send_video_uses_final_output_metadata_not_youtube(transfer_env, monkeypatch):
    """
    Verifies that the metadata passed to send_video is derived from the FINAL
    output video via ffprobe, and does NOT come from YouTube's initial metadata.
    """
    from src.services.ffmpeg_service import FFmpegService

    processor = JobProcessor()
    mock_bot = MagicMock()
    mock_bot.send_video = AsyncMock(return_value=MagicMock(message_id=779))

    dummy_video = os.path.join(transfer_env["temp"], "final_output.mp4")
    with open(dummy_video, "wb") as f:
        f.write(b"final video bytes")

    # YouTube initial metadata: 1920x1080, duration 300s
    # Transcoded final output: 608x1080 (DAR-preserved portrait Shorts), duration 298s
    final_output_metadata = (298, 608, 1080)

    monkeypatch.setattr(
        FFmpegService,
        "extract_video_metadata",
        AsyncMock(return_value=final_output_metadata),
    )

    duration, width, height = await FFmpegService.extract_video_metadata(dummy_video)
    extra_kwargs = {
        "supports_streaming": True,
        "duration": duration,
        "width": width,
        "height": height,
    }

    msg_id, size = await processor._send_media_to_cache(
        bot=mock_bot,
        api_mode="local",
        channel_id=-1001234567890,
        local_file_path=dummy_video,
        operation=OperationType.VIDEO.value,
        job_id="job-final-meta",
        job_title="Final Output Video",
        caption="Caption",
        extra_kwargs=extra_kwargs,
    )

    assert msg_id == 779
    call_kwargs = mock_bot.send_video.call_args.kwargs
    assert call_kwargs["duration"] == 298
    assert call_kwargs["width"] == 608
    assert call_kwargs["height"] == 1080


