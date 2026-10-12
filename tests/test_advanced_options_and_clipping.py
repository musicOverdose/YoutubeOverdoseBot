import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.services.cache_service import CacheService
from src.services.ytdlp_service import YtDlpService
from src.utils.timestamp_parser import (
    format_clip_filename_tag,
    format_timestamp,
    parse_time_token,
    parse_timestamp_range,
)
from src.bot.keyboards import (
    build_advanced_options_keyboard,
    build_advanced_quality_keyboard,
    build_quality_keyboard,
)
from src.worker.processor import format_video_caption_resolution


class TestTimestampParser:
    def test_parse_time_token(self):
        assert parse_time_token("0") == 0
        assert parse_time_token("45") == 45
        assert parse_time_token("1:23") == 83
        assert parse_time_token("01:23") == 83
        assert parse_time_token("1:00:00") == 3600
        assert parse_time_token("01:30:15") == 5415
        assert parse_time_token("45s") == 45
        assert parse_time_token("2m") == 120
        assert parse_time_token("1h30m") == 5400
        assert parse_time_token("invalid") is None
        assert parse_time_token("-5") is None

    def test_parse_timestamp_range_valid(self):
        # Delimiter ' - '
        ok, start, end, err = parse_timestamp_range("1:23 - 3:30", source_duration=600)
        assert ok is True
        assert (start, end) == (83, 210)
        assert err is None

        # Delimiter '-' without spaces
        ok, start, end, err = parse_timestamp_range("01:23-03:30", source_duration=600)
        assert ok is True
        assert (start, end) == (83, 210)

        # En-dash ' – '
        ok, start, end, err = parse_timestamp_range("1:23 – 3:30", source_duration=600)
        assert ok is True
        assert (start, end) == (83, 210)

        # Em-dash ' — '
        ok, start, end, err = parse_timestamp_range("1:23 — 3:30", source_duration=600)
        assert ok is True
        assert (start, end) == (83, 210)

        # Units with '-'
        ok, start, end, err = parse_timestamp_range("45s - 2m", source_duration=600)
        assert ok is True
        assert (start, end) == (45, 120)

        # Raw seconds with '-'
        ok, start, end, err = parse_timestamp_range("75 - 210", source_duration=600)
        assert ok is True
        assert (start, end) == (75, 210)

    def test_parse_timestamp_range_errors(self):
        # Missing hyphen delimiter (whitespace or 'to' must be rejected)
        ok, _, _, err = parse_timestamp_range("01:23 03:30", source_duration=600)
        assert ok is False
        assert "start - end" in err

        ok, _, _, err = parse_timestamp_range("45s to 2m", source_duration=600)
        assert ok is False
        assert "start - end" in err

        # Start >= End
        ok, start, end, err = parse_timestamp_range("3:30 - 1:23", source_duration=600)
        assert ok is False
        assert "greater than start" in err.lower()

        # Equal start and end
        ok, start, end, err = parse_timestamp_range("1:00 - 1:00", source_duration=600)
        assert ok is False

        # Exceeds max clip duration (20 min = 1200 sec)
        ok, start, end, err = parse_timestamp_range("0:00 - 25:00", source_duration=3600)
        assert ok is False
        assert "exceeds max limit" in err.lower()

        # Exceeds video duration
        ok, start, end, err = parse_timestamp_range("1:00 - 5:00", source_duration=200)
        assert ok is False
        assert "exceeds video length" in err.lower()

    def test_format_timestamp(self):
        assert format_timestamp(0) == "00:00"
        assert format_timestamp(83) == "01:23"
        assert format_timestamp(3600) == "01:00:00"
        assert format_timestamp(3665) == "01:01:05"

    def test_format_clip_filename_tag(self):
        assert format_clip_filename_tag(83) == "01.23"
        assert format_clip_filename_tag(3665) == "01.01.05"


class TestResolutionLabelSimplification:
    def test_get_resolution_labels(self):
        info = {
            "formats": [
                {"height": 2160, "width": 3840, "vcodec": "avc1.640033", "format_id": "1"},
                {"height": 1440, "width": 2560, "vcodec": "avc1.640032", "format_id": "2"},
                {"height": 1080, "width": 1920, "vcodec": "avc1.640028", "format_id": "3"},
                {"height": 720, "width": 1280, "vcodec": "avc1.4d401f", "format_id": "4"},
            ]
        }
        labels = YtDlpService.get_resolution_labels(info)
        assert labels[2160] == "4K"
        assert labels[1440] == "2K"
        assert labels[1080] == "1080p"
        assert labels[720] == "720p"

    def test_format_video_caption_resolution(self):
        assert format_video_caption_resolution("1080p", 1080) == "1080p"
        assert format_video_caption_resolution("720p", 720) == "720p"
        assert format_video_caption_resolution("1440p", 1440) == "2K"
        assert format_video_caption_resolution("2160p", 2160) == "4K"
        assert format_video_caption_resolution("1440p (Vertical)", 1440) == "2K (Vertical)"
        assert format_video_caption_resolution("2160p (Vertical)", 2160) == "4K (Vertical)"


class TestCacheKeyIsolation:
    def test_cache_keys_distinct(self):
        key_full = CacheService.generate_cache_key("vid123", "VIDEO", "H264", height=1080)
        key_clip = CacheService.generate_cache_key("vid123", "VIDEO", "H264", height=1080, clip_start=83, clip_end=210)
        key_muted = CacheService.generate_cache_key("vid123", "VIDEO", "H264", height=1080, is_muted=True)
        key_clip_muted = CacheService.generate_cache_key("vid123", "VIDEO", "H264", height=1080, clip_start=83, clip_end=210, is_muted=True)

        assert key_full != key_clip
        assert key_full != key_muted
        assert key_clip != key_clip_muted
        assert len(key_full) == 64
        assert len(key_clip) == 64
        assert len(key_muted) == 64
        assert len(key_clip_muted) == 64

    def test_audio_clip_cache_key(self):
        key_full_audio = CacheService.generate_cache_key("vid123", "AUDIO", "MP3")
        key_clip_audio = CacheService.generate_cache_key("vid123", "AUDIO", "MP3", clip_start=60, clip_end=120)
        assert key_full_audio != key_clip_audio
        assert len(key_full_audio) == 64
        assert len(key_clip_audio) == 64


class TestClipSizeEstimation:
    def test_size_scales_with_clip_duration(self):
        info = {
            "duration": 600,  # 10 minutes
            "formats": [
                {
                    "height": 1080,
                    "vcodec": "avc1.640028",
                    "acodec": "none",
                    "ext": "mp4",
                    "filesize": 100 * 1024 * 1024,  # 100 MB full video
                    "format_id": "137",
                },
                {
                    "vcodec": "none",
                    "acodec": "mp4a.40.2",
                    "ext": "m4a",
                    "filesize": 10 * 1024 * 1024,   # 10 MB audio
                    "format_id": "140",
                },
            ],
        }

        # Full duration estimate
        full_bytes, _, _ = YtDlpService.calculate_expected_video_size(info, 1080, "H264")

        # 60 second clip estimate (1/10 of duration)
        clip_bytes, _, _ = YtDlpService.calculate_expected_video_size(
            info, 1080, "H264", clip_duration=60
        )

        assert clip_bytes < full_bytes
        # Approx 10% of full size
        assert abs(clip_bytes - (full_bytes // 10)) < (2 * 1024 * 1024)

    def test_muted_size_excludes_audio(self):
        info = {
            "duration": 300,
            "formats": [
                {
                    "height": 1080,
                    "vcodec": "avc1.640028",
                    "acodec": "none",
                    "ext": "mp4",
                    "filesize": 50 * 1024 * 1024,
                    "format_id": "137",
                },
                {
                    "vcodec": "none",
                    "acodec": "mp4a.40.2",
                    "ext": "m4a",
                    "filesize": 5 * 1024 * 1024,
                    "format_id": "140",
                },
            ],
        }
        normal_bytes, _, _ = YtDlpService.calculate_expected_video_size(info, 1080, "H264", is_muted=False)
        muted_bytes, _, details = YtDlpService.calculate_expected_video_size(info, 1080, "H264", is_muted=True)

        assert muted_bytes < normal_bytes
        assert details.get("audio_final_size") == 0


class TestKeyboards:
    def test_advanced_button_above_cancel(self):
        kb = build_quality_keyboard("test12345", [1080, 720])
        buttons = [row[0].text for row in kb.inline_keyboard]
        cancel_idx = buttons.index("❌ Cancel")
        adv_idx = buttons.index("🎛 Advanced Options")
        assert adv_idx == cancel_idx - 1

    def test_advanced_options_keyboard_toggles(self):
        # Video mode, unmuted
        kb = build_advanced_options_keyboard("test12345", is_muted=False, mode="video")
        flat = [btn.text for row in kb.inline_keyboard for btn in row]
        assert "Mute: OFF🔊" in flat
        assert "Mode: Video🎬" in flat
        assert "✂️ Set Timestamp Range" in flat
        assert "🔄 Reset Options" in flat
        assert "✅ Finish" in flat
        assert "🔙 Back to Menu" in flat

        # Video mode, muted
        kb_muted = build_advanced_options_keyboard("test12345", is_muted=True, mode="video")
        flat_muted = [btn.text for row in kb_muted.inline_keyboard for btn in row]
        assert "Mute: ON🔇" in flat_muted
        assert "Mode: Video🎬" in flat_muted

        # MP3 mode
        kb_mp3 = build_advanced_options_keyboard("test12345", is_muted=True, mode="mp3")
        flat_mp3 = [btn.text for row in kb_mp3.inline_keyboard for btn in row]
        assert "Mute: N/A" in flat_mp3
        assert "Mode: MP3💿" in flat_mp3

    def test_render_advanced_options_card(self):
        from src.bot.handlers.quality_handler import render_advanced_options_card
        card = render_advanced_options_card(
            title="Sample YouTube Video",
            duration=300,
            clip_start=60,
            clip_end=120,
            is_muted=False,
            mode="video",
        )
        assert "━━━━━━━━━━━━━━━━━━━" not in card
        assert "🎛 <b>Advanced Options</b>" in card
        assert "• <b>Clip:</b> 01:00 - 02:00 (01:00) ✂️" in card
        assert "• <b>Audio:</b> Sound ON 🔊" in card
        assert "• <b>Mode:</b> Video 🎬" in card

        card_muted = render_advanced_options_card(
            title="Sample YouTube Video",
            duration=300,
            clip_start=None,
            clip_end=None,
            is_muted=True,
            mode="video",
        )
        assert "• <b>Clip:</b> Full (05:00)" in card_muted
        assert "• <b>Audio:</b> Muted 🔇" in card_muted
        assert "• <b>Mode:</b> Video 🎬" in card_muted

    def test_advanced_quality_keyboard_labels(self):
        kb = build_advanced_quality_keyboard(
            "test12345",
            [2160, 1440, 1080],
            resolution_labels={2160: "4K", 1440: "2K", 1080: "1080p"},
        )
        flat = [btn.text for row in kb.inline_keyboard for btn in row]
        assert "🎬 4K" in flat
        assert "🎬 2K" in flat
        assert "🎬 1080p" in flat
        assert "🔙 Back to Options" in flat


class TestClipPrecisionAndProgress:
    @pytest.mark.asyncio
    async def test_process_video_clip_encoding(self):
        from src.services.ffmpeg_service import FFmpegService
        with patch.object(FFmpegService, "probe_file", return_value={"streams": [{"codec_type": "video", "codec_name": "h264"}, {"codec_type": "audio", "codec_name": "aac"}]}), \
             patch.object(FFmpegService, "run_subprocess", return_value=True) as mock_run, \
             patch("os.path.exists", return_value=True), \
             patch("os.path.getsize", return_value=1024), \
             patch("shutil.copy2") as mock_copy:
            
            ok = await FFmpegService.process_video(
                input_path="/tmp/in.mp4",
                output_path="/tmp/out.mp4",
                target_codec="H264",
                target_height=720,
                clip_offset=5.0,
                clip_duration=60.0,
            )
            assert ok is True
            assert mock_run.called
            cmd = mock_run.call_args[0][0]
            # Must seek by clip_offset and specify clip_duration
            assert "-ss" in cmd and "5.0" in cmd
            assert "-t" in cmd and "60.0" in cmd
            # Must encode with libx264 for keyframe alignment
            assert "libx264" in cmd

    @pytest.mark.asyncio
    async def test_unified_progress_notifier(self):
        from src.worker.notifier import StatusNotifier
        bot_mock = AsyncMock()
        notifier = StatusNotifier(bot=bot_mock, requests=[{"chat_id": 12345, "message_id": 999}])
        
        await notifier.update_unified_progress(
            pct=65.0,
            speed="12.4 MB/s",
            eta="00:05",
            force=True,
        )
        assert bot_mock.edit_message_text.called
        call_kwargs = bot_mock.edit_message_text.call_args.kwargs
        text = call_kwargs.get("text", "")
        assert "⚡️ <b>Please wait...</b>" in text
        assert "65%" in text
        assert "12.4 MB/s • ETA 00:05" in text
        # Does NOT contain technical jargon
        assert "Remuxing" not in text
        assert "stream copy" not in text

    @pytest.mark.asyncio
    async def test_back_to_menu_resets_options(self):
        from src.bot.handlers.quality_handler import on_advanced_back_to_menu, set_adv_options, get_adv_options
        user_id = 1111
        chat_id = 2222
        source_id = "testvid123"

        # Pre-set some advanced options
        await set_adv_options(user_id, chat_id, source_id, {
            "clip_start": 30,
            "clip_end": 60,
            "is_muted": True,
            "mode": "mp3",
        })

        callback_mock = AsyncMock()
        callback_mock.from_user.id = user_id
        callback_mock.message.chat.id = chat_id
        callback_mock.message.caption = "Menu"
        callback_mock.data = f"adv_back:{source_id}"

        bot_mock = AsyncMock()

        with patch("src.bot.handlers.quality_handler.MustJoinService.enforce_must_join_callback", return_value=True), \
             patch("src.bot.handlers.quality_handler.YtDlpService.extract_metadata", return_value={"title": "Test Vid", "formats": []}), \
             patch("src.bot.handlers.quality_handler.safe_callback_answer"):
            await on_advanced_back_to_menu(callback_mock, bot_mock)

        # Verify options are reset to default
        opts = await get_adv_options(user_id, chat_id, source_id)
        assert opts.get("clip_start") is None
        assert opts.get("clip_end") is None
        assert opts.get("is_muted") is False
        assert opts.get("mode") == "video"
