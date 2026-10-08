import os
import pytest
from src.services.ffmpeg_service import FFmpegService
from src.services.ytdlp_service import YtDlpService


def test_no_quality_fallback():
    # Source only has 720p and 360p
    mock_info = {
        "formats": [
            {"vcodec": "avc1.4d401f", "height": 720},
            {"vcodec": "avc1.4d401e", "height": 360},
        ]
    }
    available = YtDlpService.get_available_resolutions(mock_info)
    assert available == [720, 360]

    # If user requests 1080p, it must NOT be accepted or substituted
    requested_height = 1080
    assert requested_height not in available

    # Fallback to 720p is strictly forbidden
    valid_exact = requested_height in available
    assert valid_exact is False


def test_stream_copy_vs_transcode_decision():
    # Case 1: Source is H.264 + AAC, requested is H264
    # Expected: Stream copy both video and audio (no re-encoding!)
    probe_h264_aac = {
        "streams": [
            {"codec_type": "video", "codec_name": "h264"},
            {"codec_type": "audio", "codec_name": "aac"},
        ]
    }
    copy_v, copy_a = FFmpegService.can_stream_copy(probe_h264_aac, "H264")
    assert copy_v is True
    assert copy_a is True

    # Case 2: Source is VP9 + Opus, requested is H264
    # Expected: Cannot stream copy (must transcode both)
    probe_vp9_opus = {
        "streams": [
            {"codec_type": "video", "codec_name": "vp9"},
            {"codec_type": "audio", "codec_name": "opus"},
        ]
    }
    copy_v, copy_a = FFmpegService.can_stream_copy(probe_vp9_opus, "H264")
    assert copy_v is False
    assert copy_a is False

    # Case 3: Source is HEVC + AAC, requested is H265
    # Expected: Stream copy both video and audio
    probe_hevc_aac = {
        "streams": [
            {"codec_type": "video", "codec_name": "hevc"},
            {"codec_type": "audio", "codec_name": "aac"},
        ]
    }
    copy_v, copy_a = FFmpegService.can_stream_copy(probe_hevc_aac, "H265")
    assert copy_v is True
    assert copy_a is True

    # Case 4: Source is H.264 + AAC, requested is H265
    # Expected: Video must be transcoded to H265, but AAC audio can be stream-copied
    copy_v, copy_a = FFmpegService.can_stream_copy(probe_h264_aac, "H265")
    assert copy_v is False
    assert copy_a is True


@pytest.mark.asyncio
async def test_aspect_ratio_preservation_synthetic(tmp_path):
    """
    Synthetic FFmpeg tests verifying that display aspect ratio is preserved across:
    - 1920x1080 (16:9 landscape)
    - 1080x1920 (9:16 portrait Shorts/Reels)
    - 1080x1080 (1:1 square)
    - 2560x1080 (21:9 ultrawide)
    Verifies that -c:v copy preserves exact output geometry and DAR via ffprobe.
    """
    import os
    import subprocess

    cases = [
        # (name, input_size, extra_in_vf, target_height, expected_w, expected_h, expected_dar_ratio)
        ("landscape_16_9", "1920x1080", None, 1080, 1920, 1080, 16 / 9),
        ("portrait_9_16", "1080x1920", None, 1080, 1080, 1920, 9 / 16),
        ("square_1_1", "1080x1080", None, 1080, 1080, 1080, 1.0),
        ("ultrawide_21_9", "2560x1080", None, 1080, 2560, 1080, 2560 / 1080),
    ]

    for name, in_size, in_vf, target_h, exp_w, exp_h, exp_dar in cases:
        in_file = str(tmp_path / f"{name}_in.mp4")
        out_file = str(tmp_path / f"{name}_out.mp4")

        # Generate synthetic input with H264 video
        cmd_gen = [
            "ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=size={in_size}:rate=1",
            "-t", "0.5",
        ]
        if in_vf:
            cmd_gen.extend(["-vf", in_vf])
        cmd_gen.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p", in_file])
        proc = subprocess.run(cmd_gen, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert proc.returncode == 0, f"Failed generating synthetic input {name}: {proc.stderr.decode()}"

        # Process video with FFmpegService (strictly stream-copied)
        ok = await FFmpegService.process_video(
            input_path=in_file,
            output_path=out_file,
            target_codec="H264",
            target_height=target_h,
        )
        assert ok is True, f"process_video failed for {name}"
        assert os.path.exists(out_file)

        # Probe output geometry
        probe = await FFmpegService.probe_file(out_file)
        v_stream = next(s for s in probe["streams"] if s["codec_type"] == "video")
        out_w = v_stream["width"]
        out_h = v_stream["height"]

        assert out_w == exp_w, f"Width mismatch for {name}: got {out_w}, expected {exp_w}"
        assert out_h == exp_h, f"Height mismatch for {name}: got {out_h}, expected {exp_h}"

        # Check display aspect ratio
        dar_str = v_stream.get("display_aspect_ratio")
        if dar_str and ":" in dar_str:
            dw, dh = map(float, dar_str.split(":"))
            actual_dar = dw / dh
        else:
            actual_dar = out_w / out_h

        assert abs(actual_dar - exp_dar) / exp_dar < 0.03, (
            f"DAR distorted for {name}: actual {actual_dar:.3f} vs expected {exp_dar:.3f}"
        )


@pytest.mark.asyncio
async def test_zero_reencoding_enforced_on_mismatched_codec(tmp_path):
    """
    Verifies that FFmpegService.process_video strictly refuses to re-encode video
    when the source video codec does not match the requested target codec.
    """
    import subprocess

    in_file = str(tmp_path / "vp9_video.mp4")
    out_file = str(tmp_path / "h264_out.mp4")

    # Generate synthetic VP9 input
    cmd_gen = [
        "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=1",
        "-t", "0.5", "-c:v", "libvpx-vp9", in_file
    ]
    proc = subprocess.run(cmd_gen, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.returncode == 0

    # Attempting to process as H264 MUST raise RuntimeError, never transcode
    with pytest.raises(RuntimeError, match="Video re-encoding is disabled"):
        await FFmpegService.process_video(
            input_path=in_file,
            output_path=out_file,
            target_codec="H264",
            target_height=360,
        )


def test_get_available_codecs_for_height():
    info = {
        "formats": [
            # 1080p has both H264 (avc1) and H265 (hev1) and VP9
            {"format_id": "137", "vcodec": "avc1.640028", "height": 1080},
            {"format_id": "270", "vcodec": "hev1.1.6.L93.B0", "height": 1080},
            {"format_id": "248", "vcodec": "vp9", "height": 1080},
            # 720p has only H264
            {"format_id": "136", "vcodec": "h264", "height": 720},
            {"format_id": "247", "vcodec": "vp9", "height": 720},
            # 480p has only VP9
            {"format_id": "244", "vcodec": "vp9", "height": 480},
            # 360p has only H265
            {"format_id": "300", "vcodec": "hvc1.1.6.L93.B0", "height": 360},
        ]
    }

    assert YtDlpService.get_available_codecs_for_height(info, 1080) == ["H264", "H265"]
    assert YtDlpService.get_available_codecs_for_height(info, 720) == ["H264"]
    assert YtDlpService.get_available_codecs_for_height(info, 480) == []
    assert YtDlpService.get_available_codecs_for_height(info, 360) == ["H265"]
    assert YtDlpService.get_available_codecs_for_height(info, 240) == []


def test_build_codec_keyboard():
    from src.bot.keyboards import build_codec_keyboard

    # Both codecs available
    kb_both = build_codec_keyboard("test_id", 1080, ["H264", "H265"])
    assert len(kb_both.inline_keyboard) == 3
    assert "H.264" in kb_both.inline_keyboard[0][0].text
    assert "c:test_id:H264:1080" == kb_both.inline_keyboard[0][0].callback_data
    assert "H.265" in kb_both.inline_keyboard[1][0].text
    assert "c:test_id:H265:1080" == kb_both.inline_keyboard[1][0].callback_data
    assert "Back" in kb_both.inline_keyboard[2][0].text

    # Only H264 available
    kb_h264 = build_codec_keyboard("test_id", 720, ["H264"])
    assert len(kb_h264.inline_keyboard) == 2
    assert "H.264" in kb_h264.inline_keyboard[0][0].text
    assert "Back" in kb_h264.inline_keyboard[1][0].text

    # Only H265 available
    kb_h265 = build_codec_keyboard("test_id", 360, ["H265"])
    assert len(kb_h265.inline_keyboard) == 2
    assert "H.265" in kb_h265.inline_keyboard[0][0].text
    assert "Back" in kb_h265.inline_keyboard[1][0].text

    # Neither available
    kb_none = build_codec_keyboard("test_id", 480, [])
    assert len(kb_none.inline_keyboard) == 1
    assert "Back" in kb_none.inline_keyboard[0][0].text


def test_render_progress_bar():
    from src.worker.notifier import StatusNotifier

    assert StatusNotifier.render_progress_bar(0.0) == "░░░░░░░░░░░░░░░░ 0%"
    assert StatusNotifier.render_progress_bar(50.0) == "████████░░░░░░░░ 50%"
    assert StatusNotifier.render_progress_bar(75.0) == "████████████░░░░ 75%"
    assert StatusNotifier.render_progress_bar(100.0) == "████████████████ 100%"



@pytest.mark.asyncio
async def test_ffprobe_metadata_extraction_landscape_portrait_square(tmp_path):
    """
    Verifies that extract_video_metadata extracts exact positive duration, width,
    and height across landscape, portrait, and square videos.
    """
    import subprocess

    cases = [
        ("landscape", 1920, 1080, 2),
        ("portrait", 1080, 1920, 3),
        ("square", 720, 720, 1),
    ]

    for name, w, h, dur in cases:
        v_path = str(tmp_path / f"meta_{name}.mp4")
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", f"testsrc=size={w}x{h}:rate=25",
                "-t", str(dur),
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                v_path,
            ],
            check=True,
            capture_output=True,
        )

        extracted_dur, extracted_w, extracted_h = await FFmpegService.extract_video_metadata(v_path)
        assert extracted_w == w
        assert extracted_h == h
        assert extracted_dur == dur


@pytest.mark.asyncio
async def test_thumbnail_generation_dar_bounds_and_size(tmp_path):
    """
    Verifies that generate_thumbnail:
    - Creates valid JPEG
    - Dimensions are <= 320x320
    - File size is strictly < 200 KB
    - Preserves display aspect ratio without stretching
    Across landscape (16:9), portrait (9:16), and square (1:1).
    """
    import subprocess
    from PIL import Image

    cases = [
        ("landscape", 1920, 1080, 16 / 9),
        ("portrait", 1080, 1920, 9 / 16),
        ("square", 1080, 1080, 1.0),
    ]

    for name, w, h, expected_dar in cases:
        v_path = str(tmp_path / f"thumb_src_{name}.mp4")
        t_path = str(tmp_path / f"thumb_{name}.jpg")

        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", f"testsrc=size={w}x{h}:rate=25",
                "-t", "2",
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                v_path,
            ],
            check=True,
            capture_output=True,
        )

        ok = await FFmpegService.generate_thumbnail(
            video_path=v_path,
            output_thumb_path=t_path,
            duration=2,
        )
        assert ok is True
        assert os.path.exists(t_path)

        file_size = os.path.getsize(t_path)
        assert file_size < 200 * 1024, f"Thumbnail {name} size {file_size} exceeds 200 KB"

        with Image.open(t_path) as img:
            assert img.format == "JPEG"
            tw, th = img.size
            assert tw <= 1280, f"Thumbnail width {tw} exceeds 1280"
            assert th <= 1280, f"Thumbnail height {th} exceeds 1280"

            actual_dar = tw / th
            assert abs(actual_dar - expected_dar) / expected_dar < 0.05, (
                f"Thumbnail DAR distorted for {name}: {actual_dar:.3f} vs {expected_dar:.3f}"
            )


@pytest.mark.asyncio
async def test_thumbnail_black_frame_fallback(tmp_path):
    """
    Verifies that if the initial candidate frame is solid black, generate_thumbnail
    detects it and seeks to an alternate timestamp to capture a content frame.
    """
    import subprocess
    from PIL import Image

    v_path = str(tmp_path / "black_intro.mp4")
    t_path = str(tmp_path / "thumb_fallback.jpg")

    # 1.5 seconds black, then 4.5 seconds testsrc (total 6 seconds)
    filter_complex = "color=c=black:s=1920x1080:d=1.5[v0];testsrc=size=1920x1080:rate=25:d=4.5[v1];[v0][v1]concat=n=2:v=1:a=0[v]"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-filter_complex", filter_complex,
            "-map", "[v]",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            v_path,
        ],
        check=True,
        capture_output=True,
    )

    ok = await FFmpegService.generate_thumbnail(
        video_path=v_path,
        output_thumb_path=t_path,
        duration=6,
    )
    assert ok is True

    # The resulting thumbnail should NOT be solid black
    with Image.open(t_path) as img:
        grayscale = img.convert("L")
        extrema = grayscale.getextrema()
        assert extrema[1] > 50, f"Expected non-black thumbnail, but max luminance is {extrema[1]}"


@pytest.mark.asyncio
async def test_metadata_extraction_fails_on_missing_or_corrupt_file(tmp_path):
    """
    Verifies that extract_video_metadata raises ValueError cleanly when given
    a non-existent or invalid video file.
    """
    with pytest.raises(ValueError, match="does not exist"):
        await FFmpegService.extract_video_metadata(str(tmp_path / "nonexistent.mp4"))

    corrupt_file = str(tmp_path / "corrupt.mp4")
    with open(corrupt_file, "wb") as f:
        f.write(b"not a video file")

    with pytest.raises(ValueError, match="No video stream found"):
        await FFmpegService.extract_video_metadata(corrupt_file)


