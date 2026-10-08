import os
import subprocess
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from PIL import Image

from src.services.thumbnail_service import ThumbnailService
from src.services.ffmpeg_service import FFmpegService


def test_highest_quality_available_youtube_thumbnail_selected():
    """
    Verifies that get_best_thumbnail_url correctly selects the highest resolution
    available thumbnail from yt-dlp metadata using width/height and tier heuristics.
    """
    metadata = {
        "thumbnail": "https://i.ytimg.com/vi/abc/default.jpg",
        "thumbnails": [
            {"id": "0", "url": "https://i.ytimg.com/vi/abc/default.jpg", "width": 120, "height": 90},
            {"id": "mqdefault", "url": "https://i.ytimg.com/vi/abc/mqdefault.jpg", "width": 320, "height": 180},
            {"id": "hqdefault", "url": "https://i.ytimg.com/vi/abc/hqdefault.jpg", "width": 480, "height": 360},
            {"id": "sddefault", "url": "https://i.ytimg.com/vi/abc/sddefault.jpg", "width": 640, "height": 480},
            {"id": "hq720", "url": "https://i.ytimg.com/vi/abc/hq720.jpg", "width": 1280, "height": 720},
            {"id": "maxresdefault", "url": "https://i.ytimg.com/vi/abc/maxresdefault.jpg", "width": 1920, "height": 1080},
        ],
    }
    best_url = ThumbnailService.get_best_thumbnail_url(metadata)
    assert best_url == "https://i.ytimg.com/vi/abc/maxresdefault.jpg"


def test_fallback_when_maxresdefault_does_not_exist():
    """
    Verifies that when maxresdefault is not provided by YouTube, the selector
    gracefully picks the next best available thumbnail (e.g. hq720 or sddefault).
    """
    metadata = {
        "thumbnail": "https://i.ytimg.com/vi/abc/default.jpg",
        "thumbnails": [
            {"id": "0", "url": "https://i.ytimg.com/vi/abc/default.jpg", "width": 120, "height": 90},
            {"id": "mqdefault", "url": "https://i.ytimg.com/vi/abc/mqdefault.jpg", "width": 320, "height": 180},
            {"id": "hqdefault", "url": "https://i.ytimg.com/vi/abc/hqdefault.jpg", "width": 480, "height": 360},
            {"id": "sddefault", "url": "https://i.ytimg.com/vi/abc/sddefault.jpg", "width": 640, "height": 480},
        ],
    }
    best_url = ThumbnailService.get_best_thumbnail_url(metadata)
    assert best_url == "https://i.ytimg.com/vi/abc/sddefault.jpg"


def test_thumbnail_selection_without_explicit_dimensions():
    """
    Verifies that if yt-dlp metadata omits width and height, the heuristic
    correctly ranks based on standard YouTube thumbnail URL identifiers.
    """
    metadata = {
        "thumbnails": [
            {"id": "0", "url": "https://i.ytimg.com/vi/abc/0.jpg"},
            {"id": "1", "url": "https://i.ytimg.com/vi/abc/hqdefault.jpg"},
            {"id": "2", "url": "https://i.ytimg.com/vi/abc/maxresdefault.jpg?sqp=abc"},
        ]
    }
    best_url = ThumbnailService.get_best_thumbnail_url(metadata)
    assert "maxresdefault" in best_url


def test_fit_dimensions_preserves_aspect_ratio():
    """
    Verifies fit_dimensions_inside_bounds calculates dimensions that:
    - Never exceed 320x320
    - Preserve exact display aspect ratio across landscape, portrait, and square
    - Do not distort, stretch, or squeeze
    """
    # 1. Landscape 16:9 (1920x1080) -> 320x180
    w, h = ThumbnailService.fit_dimensions_inside_bounds(1920, 1080, 320, 320)
    assert w <= 320 and h <= 320
    assert w == 320
    assert h == 180
    assert abs((w / h) - (16 / 9)) < 0.01

    # 2. Portrait 9:16 (1080x1920) -> 180x320
    w, h = ThumbnailService.fit_dimensions_inside_bounds(1080, 1920, 320, 320)
    assert w <= 320 and h <= 320
    assert w == 180
    assert h == 320
    assert abs((w / h) - (9 / 16)) < 0.01

    # 3. Square 1:1 (1000x1000) -> 320x320
    w, h = ThumbnailService.fit_dimensions_inside_bounds(1000, 1000, 320, 320)
    assert (w, h) == (320, 320)

    # 4. Ultrawide 21:9 (2560x1080) -> 320x135
    w, h = ThumbnailService.fit_dimensions_inside_bounds(2560, 1080, 320, 320)
    assert w == 320
    assert h == 135
    assert abs((w / h) - (2560 / 1080)) < 0.01

    # 5. Standard 4:3 (1440x1080) -> 320x240
    w, h = ThumbnailService.fit_dimensions_inside_bounds(1440, 1080, 320, 320)
    assert (w, h) == (320, 240)


def test_process_image_file_quality_and_limits(tmp_path):
    """
    Verifies that process_image_file:
    - Scales down high-res image using Lanczos
    - Outputs valid JPEG
    - Stays strictly under 1280x1280
    - File size is strictly < 195 KB (and therefore < 200 KB)
    """
    src_path = str(tmp_path / "creator_thumb_1080p.png")
    out_path = str(tmp_path / "telegram_thumb.jpg")

    # Create synthetic 1920x1080 high-res creator artwork
    img = Image.new("RGB", (1920, 1080), color=(220, 50, 50))
    img.save(src_path, "PNG")

    ok = ThumbnailService.process_image_file(src_path, out_path)
    assert ok is True
    assert os.path.exists(out_path)

    file_size = os.path.getsize(out_path)
    assert file_size < 195 * 1024, f"File size {file_size} exceeds 195 KB"

    with Image.open(out_path) as out_img:
        assert out_img.format == "JPEG"
        tw, th = out_img.size
        assert tw == 1280
        assert th == 720
        assert tw <= 1280 and th <= 1280


def test_adaptive_compression_when_initial_quality_too_large(tmp_path):
    """
    Verifies that save_image_adaptive_jpeg steps down quality when an image
    is large or high-entropy, ensuring output stays under the byte limit.
    """
    out_path = str(tmp_path / "adaptive_test.jpg")
    img = Image.new("RGB", (320, 320), color=(128, 128, 128))

    # Test with a very restrictive byte ceiling (e.g. 3 KB)
    # The adaptive loop must step down quality until it fits under the limit
    ok = ThumbnailService.save_image_adaptive_jpeg(img, out_path, max_bytes=3 * 1024)
    assert ok is True
    assert os.path.getsize(out_path) <= 3 * 1024


@pytest.mark.asyncio
async def test_official_thumbnail_preferred_over_frame_extraction(tmp_path):
    """
    Verifies that when an official YouTube thumbnail file exists, ThumbnailService
    processes the artwork and DOES NOT invoke FFmpeg frame extraction.
    """
    video_path = str(tmp_path / "video.mp4")
    official_thumb = str(tmp_path / "input.webp")
    out_thumb = str(tmp_path / "output_thumb.jpg")

    # Create dummy video and official webp thumbnail
    open(video_path, "w").write("dummy video")
    img = Image.new("RGB", (1280, 720), color=(0, 180, 90))
    img.save(official_thumb, "WEBP")

    with patch.object(ThumbnailService, "extract_frame_fallback", new_callable=AsyncMock) as mock_ffmpeg:
        ok = await ThumbnailService.prepare_video_thumbnail(
            video_path=video_path,
            output_thumb_path=out_thumb,
            source_thumb_path=official_thumb,
            source_thumb_url=None,
            duration=120,
        )
        assert ok is True
        # FFmpeg must NEVER be called when official artwork is available
        mock_ffmpeg.assert_not_called()

    assert os.path.exists(out_thumb)
    with Image.open(out_thumb) as result_img:
        assert result_img.format == "JPEG"
        assert result_img.size == (1280, 720)


@pytest.mark.asyncio
async def test_official_thumbnail_download_preferred_when_local_missing(tmp_path):
    """
    Verifies that if no local thumbnail file was written by yt-dlp, but an official
    URL is available in metadata, the URL is downloaded and processed without FFmpeg.
    """
    video_path = str(tmp_path / "video.mp4")
    out_thumb = str(tmp_path / "output_thumb.jpg")
    open(video_path, "w").write("dummy video")

    dummy_image_data = b""
    temp_img_path = str(tmp_path / "sample.jpg")
    Image.new("RGB", (640, 480), color=(50, 100, 200)).save(temp_img_path, "JPEG")
    with open(temp_img_path, "rb") as f:
        dummy_image_data = f.read()

    with patch.object(ThumbnailService, "download_thumbnail_image", new_callable=AsyncMock) as mock_dl, \
         patch.object(ThumbnailService, "extract_frame_fallback", new_callable=AsyncMock) as mock_ffmpeg:

        async def _fake_dl(url, path, timeout=15.0):
            with open(path, "wb") as f:
                f.write(dummy_image_data)
            return True

        mock_dl.side_effect = _fake_dl

        ok = await ThumbnailService.prepare_video_thumbnail(
            video_path=video_path,
            output_thumb_path=out_thumb,
            source_thumb_path=None,
            source_thumb_url="https://i.ytimg.com/vi/xyz/hqdefault.jpg",
            duration=60,
        )
        assert ok is True
        mock_dl.assert_called_once()
        mock_ffmpeg.assert_not_called()

    assert os.path.exists(out_thumb)
    with Image.open(out_thumb) as result_img:
        assert result_img.format == "JPEG"
        assert result_img.size == (640, 480)


@pytest.mark.asyncio
async def test_low_res_local_thumbnail_upgraded_to_hd_candidate_url(tmp_path):
    """
    Verifies that when yt-dlp downloads a low-resolution thumbnail (e.g. 320x180 on a 360p video),
    prepare_video_thumbnail identifies that the local file is not HD and downloads the HD candidate URL
    (e.g. 1920x1080 maxresdefault) instead of attaching a blurry thumbnail.
    """
    video_path = str(tmp_path / "video.mp4")
    low_res_thumb = str(tmp_path / "input.jpg")
    out_thumb = str(tmp_path / "output_thumb.jpg")
    open(video_path, "w").write("dummy video")

    # Create low-res 320x180 local thumbnail
    Image.new("RGB", (320, 180), color=(100, 100, 100)).save(low_res_thumb, "JPEG")

    # Create high-res 1920x1080 candidate artwork to be downloaded
    hd_temp = str(tmp_path / "hd.jpg")
    Image.new("RGB", (1920, 1080), color=(200, 40, 40)).save(hd_temp, "JPEG")
    with open(hd_temp, "rb") as f:
        hd_bytes = f.read()

    with patch.object(ThumbnailService, "download_thumbnail_image", new_callable=AsyncMock) as mock_dl:
        async def _fake_dl(url, path, timeout=15.0):
            with open(path, "wb") as f:
                f.write(hd_bytes)
            return True

        mock_dl.side_effect = _fake_dl

        ok = await ThumbnailService.prepare_video_thumbnail(
            video_path=video_path,
            output_thumb_path=out_thumb,
            source_thumb_path=low_res_thumb,
            source_thumb_urls=["https://i.ytimg.com/vi/abc/maxresdefault.jpg"],
            duration=60,
        )
        assert ok is True
        mock_dl.assert_called_once()

    assert os.path.exists(out_thumb)
    with Image.open(out_thumb) as result_img:
        assert result_img.format == "JPEG"
        assert result_img.size == (1280, 720)


@pytest.mark.asyncio
async def test_fallback_frame_extraction_when_no_official_thumbnail(tmp_path):
    """
    Verifies that when no local or remote official thumbnail exists, FFmpeg
    extracts a native-resolution frame at ~15% duration and produces a valid JPEG.
    """
    video_path = str(tmp_path / "synth_video.mp4")
    out_thumb = str(tmp_path / "fallback_thumb.jpg")

    # Generate a 3-second test video
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "testsrc=size=1280x720:rate=25",
            "-t", "3",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            video_path,
        ],
        check=True,
        capture_output=True,
    )

    ok = await ThumbnailService.prepare_video_thumbnail(
        video_path=video_path,
        output_thumb_path=out_thumb,
        source_thumb_path=None,
        source_thumb_url=None,
        duration=3,
    )
    assert ok is True
    assert os.path.exists(out_thumb)
    assert os.path.getsize(out_thumb) < 195 * 1024

    with Image.open(out_thumb) as result_img:
        assert result_img.format == "JPEG"
        tw, th = result_img.size
        assert tw == 1280
        assert th == 720
        assert tw <= 1280 and th <= 1280


@pytest.mark.asyncio
async def test_dark_frame_fallback(tmp_path):
    """
    Verifies that if the initial candidate timestamp is black/dark, fallback
    extraction tests alternative candidate timestamps to find a non-black frame.
    """
    video_path = str(tmp_path / "dark_intro.mp4")
    out_thumb = str(tmp_path / "dark_fallback.jpg")

    # 1.5s black intro, then 4.5s color content (total 6 seconds)
    filter_complex = "color=c=black:s=1280x720:d=1.5[v0];testsrc=size=1280x720:rate=25:d=4.5[v1];[v0][v1]concat=n=2:v=1:a=0[v]"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-filter_complex", filter_complex,
            "-map", "[v]",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            video_path,
        ],
        check=True,
        capture_output=True,
    )

    ok = await ThumbnailService.extract_frame_fallback(
        video_path=video_path,
        output_thumb_path=out_thumb,
        duration=6,
    )
    assert ok is True
    assert os.path.exists(out_thumb)

    with Image.open(out_thumb) as img:
        grayscale = img.convert("L")
        extrema = grayscale.getextrema()
        assert extrema[1] > 50, f"Expected non-black thumbnail, got max luminance {extrema[1]}"


@pytest.mark.asyncio
async def test_ffmpeg_service_backward_compatibility(tmp_path):
    """
    Verifies FFmpegService.generate_thumbnail delegates cleanly to ThumbnailService
    maintaining complete backward compatibility with existing tests and callers.
    """
    video_path = str(tmp_path / "compat_vid.mp4")
    out_thumb = str(tmp_path / "compat_thumb.jpg")

    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "testsrc=size=640x360:rate=25",
            "-t", "1",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            video_path,
        ],
        check=True,
        capture_output=True,
    )

    ok = await FFmpegService.generate_thumbnail(
        video_path=video_path,
        output_thumb_path=out_thumb,
        duration=1,
    )
    assert ok is True
    assert os.path.exists(out_thumb)
    with Image.open(out_thumb) as img:
        assert img.format == "JPEG"
        assert img.size == (640, 360)
