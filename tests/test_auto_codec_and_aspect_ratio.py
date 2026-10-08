import os
import tempfile
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from PIL import Image

from src.bot.keyboards import build_quality_keyboard, build_queue_status_keyboard
from src.services.ytdlp_service import YtDlpService
from src.services.thumbnail_service import ThumbnailService
from src.services.ffmpeg_service import FFmpegService
from src.bot.handlers.quality_handler import on_cancel_request


def test_auto_select_codec_prefers_h264():
    mock_info = {
        "formats": [
            {"vcodec": "avc1.4d401f", "height": 1080},
            {"vcodec": "hev1.1.6.L93.B0", "height": 1080},
            {"vcodec": "vp9", "height": 1080},
        ]
    }
    codec = YtDlpService.auto_select_codec(mock_info, 1080)
    assert codec == "H264"


def test_auto_select_codec_falls_back_to_h265():
    mock_info = {
        "formats": [
            {"vcodec": "hev1.1.6.L93.B0", "height": 1080},
            {"vcodec": "vp9", "height": 1080},
        ]
    }
    codec = YtDlpService.auto_select_codec(mock_info, 1080)
    assert codec == "H265"


def test_auto_select_codec_defaults_to_h264():
    mock_info = {
        "formats": [
            {"vcodec": "vp9", "height": 1080},
        ]
    }
    codec = YtDlpService.auto_select_codec(mock_info, 1080)
    assert codec == "H264"


def test_get_resolution_labels_for_vertical_and_landscape():
    # Vertical Short: 1080x1920, 720x1280
    vertical_info = {
        "formats": [
            {"vcodec": "avc1.640", "width": 1080, "height": 1920},
            {"vcodec": "avc1.4d4", "width": 720, "height": 1280},
            {"vcodec": "avc1.4d4", "width": 480, "height": 854},
        ]
    }
    v_labels = YtDlpService.get_resolution_labels(vertical_info)
    assert v_labels[1920] == "1080p"
    assert v_labels[1280] == "720p"
    assert v_labels[854] == "480p"

    # Landscape video: 1920x1080, 1280x720
    landscape_info = {
        "formats": [
            {"vcodec": "avc1.640", "width": 1920, "height": 1080},
            {"vcodec": "avc1.4d4", "width": 1280, "height": 720},
        ]
    }
    l_labels = YtDlpService.get_resolution_labels(landscape_info)
    assert l_labels[1080] == "1080p"
    assert l_labels[720] == "720p"


def test_build_quality_keyboard_has_cancel_button():
    kb = build_quality_keyboard(
        source_id="abc12345678",
        available_heights=[1920, 1280],
        resolution_labels={1920: "1080p", 1280: "720p"},
    )
    all_buttons = [btn for row in kb.inline_keyboard for btn in row]
    callbacks = [btn.callback_data for btn in all_buttons]
    texts = [btn.text for btn in all_buttons]

    # Verify friendly button labels
    assert "🎬 1080p" in texts
    assert "🎬 720p" in texts

    # Verify cancel button is present
    assert "req_cancel" in callbacks
    cancel_btn = next(btn for btn in all_buttons if btn.callback_data == "req_cancel")
    assert "Cancel" in cancel_btn.text


def test_build_queue_status_keyboard_has_no_cancel_button():
    kb = build_queue_status_keyboard("job-12345")
    all_buttons = [btn for row in kb.inline_keyboard for btn in row]
    callbacks = [btn.callback_data for btn in all_buttons]

    assert "q_stat:job-12345" in callbacks
    assert not any("cancel" in c.lower() for c in callbacks)


@pytest.mark.asyncio
async def test_on_cancel_request_deletes_message():
    callback = MagicMock()
    callback.data = "req_cancel"
    callback.message = MagicMock()
    callback.message.delete = AsyncMock()
    callback.answer = AsyncMock()
    bot = MagicMock()

    await on_cancel_request(callback, bot)

    callback.message.delete.assert_awaited_once()
    callback.answer.assert_awaited_once_with("Request cancelled.")


def test_crop_vertical_pillarbox():
    # Create 16:9 canvas (1280x720)
    img = Image.new("RGB", (1280, 720), color=(0, 0, 0))
    # Draw center box
    cropped = ThumbnailService.crop_vertical_pillarbox(img, target_ratio=9 / 16)
    w, h = cropped.size
    assert h == 720
    assert w == int(round(720 * 9 / 16))
    assert abs((w / h) - (9 / 16)) < 0.01

    # Image that is already vertical or square should not be cropped
    portrait_img = Image.new("RGB", (720, 1280), color=(100, 100, 100))
    not_cropped = ThumbnailService.crop_vertical_pillarbox(portrait_img)
    assert not_cropped.size == (720, 1280)


@pytest.mark.asyncio
async def test_prepare_video_thumbnail_extracts_native_frame_for_vertical():
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_video = os.path.join(tmpdir, "video.mp4")
        with open(fake_video, "w") as f:
            f.write("fake video content")

        out_thumb = os.path.join(tmpdir, "thumb.jpg")

        with patch.object(
            ThumbnailService, "extract_frame_fallback", new_callable=AsyncMock
        ) as mock_extract:
            mock_extract.return_value = True

            ok = await ThumbnailService.prepare_video_thumbnail(
                video_path=fake_video,
                output_thumb_path=out_thumb,
                source_thumb_path=os.path.join(tmpdir, "youtube_16_9.jpg"),
                duration=15,
                is_vertical=True,
            )

            assert ok is True
            # For vertical videos, native frame extraction MUST be called to avoid black bars
            mock_extract.assert_awaited_once_with(fake_video, out_thumb, 15)


@pytest.mark.asyncio
async def test_extract_video_metadata_handles_rotation():
    with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
        file_path = f.name

        # Mock ffprobe output where video was recorded horizontally (1920x1080) but rotated 90 degrees
        mock_probe = {
            "streams": [
                {
                    "codec_type": "video",
                    "width": 1920,
                    "height": 1080,
                    "duration": "10.0",
                    "tags": {"rotate": "90"},
                }
            ]
        }

        with patch.object(FFmpegService, "probe_file", new_callable=AsyncMock) as mock_pf:
            mock_pf.return_value = mock_probe

            duration, width, height = await FFmpegService.extract_video_metadata(file_path)
            assert duration == 10
            # Width and height should be swapped due to 90 deg rotation!
            assert width == 1080
            assert height == 1920
