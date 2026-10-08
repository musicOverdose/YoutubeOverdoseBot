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
    cropped = ThumbnailService.crop_vertical_pillarbox(img, target_ratio=9 / 16)
    w, h = cropped.size
    assert h == 720
    # Safe inward inset produces clean cropped width (~400px), eliminating edge black borders
    assert 398 <= w <= 405
    assert abs((w / h) - (9 / 16)) < 0.02

    # Image that is already vertical or square should not be cropped
    portrait_img = Image.new("RGB", (720, 1280), color=(100, 100, 100))
    not_cropped = ThumbnailService.crop_vertical_pillarbox(portrait_img)
    assert not_cropped.size == (720, 1280)


@pytest.mark.asyncio
async def test_prepare_video_thumbnail_prioritizes_official_artwork_for_vertical():
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_video = os.path.join(tmpdir, "video.mp4")
        with open(fake_video, "w") as f:
            f.write("fake video content")

        # Create real mock thumbnail file
        local_thumb = os.path.join(tmpdir, "youtube_16_9.jpg")
        mock_img = Image.new("RGB", (1280, 720), color=(128, 64, 32))
        mock_img.save(local_thumb, "JPEG")

        out_thumb = os.path.join(tmpdir, "thumb.jpg")

        with patch.object(
            ThumbnailService, "extract_frame_fallback", new_callable=AsyncMock
        ) as mock_extract:
            ok = await ThumbnailService.prepare_video_thumbnail(
                video_path=fake_video,
                output_thumb_path=out_thumb,
                source_thumb_path=local_thumb,
                duration=15,
                is_vertical=True,
            )

            assert ok is True
            # Official artwork is prioritized; fallback must NOT be called
            mock_extract.assert_not_called()
            assert os.path.exists(out_thumb)


@pytest.mark.asyncio
async def test_prepare_video_thumbnail_falls_back_to_ffmpeg_when_no_official():
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
                source_thumb_path=None,
                source_thumb_urls=[],
                duration=15,
                is_vertical=True,
            )

            assert ok is True
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


def test_get_available_resolutions_filters_codecless_formats():
    # YouTube info where 1440p and 2160p are VP9 only, while 1080p, 720p have H264
    mock_info = {
        "formats": [
            {"vcodec": "av01.0.08m", "height": 2160},
            {"vcodec": "vp09.00.51", "height": 1440},
            {"vcodec": "avc1.640028", "height": 1080},
            {"vcodec": "vp09.00.51", "height": 1080},
            {"vcodec": "avc1.4d401f", "height": 720},
            {"vcodec": "none", "acodec": "mp4a.40.2"},  # Audio-only
            {"format_note": "storyboard", "height": 360, "vcodec": "avc1.4d401f"},  # Storyboard
        ]
    }
    resolutions = YtDlpService.get_available_resolutions(mock_info)
    # 2160 and 1440 have no H264/H265, so they must be filtered out to prevent 'format unavailable' errors
    assert resolutions == [1080, 720]


def test_build_video_format_spec_includes_fallback_chain():
    spec = YtDlpService.build_video_format_spec(1080, "H264")
    # Must prioritize exact codec & height with AAC audio
    assert "bestvideo[height=1080][vcodec~='(?i)^(avc1|h264)']+bestaudio[ext=m4a]" in spec
    # Must include fallback to bestvideo at height
    assert "bestvideo[height=1080]+bestaudio" in spec
    assert "best[height=1080]" in spec
    # Must include fallback to height<=1080
    assert "bestvideo[height<=1080]+bestaudio" in spec
