import io
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


@pytest.mark.asyncio
async def test_prepare_preview_photo_falls_back_when_maxres_404(tmp_path):
    """
    Simulates the exact scenario for older/low-res videos (e.g. 2005 'Me at the zoo' jNQXAC9IVRw):
    maxresdefault and other HD endpoints return 404, but hqdefault returns 200.
    Verifies that prepare_preview_photo falls back and generates a valid JPEG preview.
    """
    out_preview = str(tmp_path / "preview.jpg")
    sample_img = tmp_path / "hqdefault.jpg"
    Image.new("RGB", (480, 360), color=(100, 150, 200)).save(str(sample_img), "JPEG")
    sample_bytes = sample_img.read_bytes()

    metadata = {
        "id": "jNQXAC9IVRw",
        "thumbnail": "https://i.ytimg.com/vi/jNQXAC9IVRw/hqdefault.jpg",
        "thumbnails": [
            {"id": "maxresdefault", "url": "https://i.ytimg.com/vi/jNQXAC9IVRw/maxresdefault.jpg"},
            {"id": "hqdefault", "url": "https://i.ytimg.com/vi/jNQXAC9IVRw/hqdefault.jpg"},
        ],
    }

    async def fake_get(url):
        mock_resp = MagicMock()
        if "maxresdefault" in str(url) or "hq720" in str(url) or "sddefault" in str(url):
            mock_resp.status_code = 404
            mock_resp.content = b""
        elif "hqdefault" in str(url):
            mock_resp.status_code = 200
            mock_resp.content = sample_bytes
        else:
            mock_resp.status_code = 404
            mock_resp.content = b""
        return mock_resp

    with patch("httpx.AsyncClient.get", side_effect=fake_get):
        ok = await ThumbnailService.prepare_preview_photo(
            info=metadata,
            source_id="jNQXAC9IVRw",
            output_path=out_preview,
            is_vertical=False,
        )
        assert ok is True
        assert os.path.exists(out_preview)
        with Image.open(out_preview) as img:
            assert img.format == "JPEG"
            assert img.size == (480, 360)


@pytest.mark.asyncio
async def test_prepare_preview_photo_vertical_shorts_cropping(tmp_path):
    """
    Verifies that for vertical Shorts, prepare_preview_photo automatically crops
    16:9 pillarbox margins to 9:16 vertical orientation.
    """
    out_preview = str(tmp_path / "preview_vertical.jpg")
    sample_img = tmp_path / "horizontal_short.jpg"
    Image.new("RGB", (1280, 720), color=(50, 100, 150)).save(str(sample_img), "JPEG")
    sample_bytes = sample_img.read_bytes()

    async def fake_get(url):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = sample_bytes
        return mock_resp

    with patch("httpx.AsyncClient.get", side_effect=fake_get):
        ok = await ThumbnailService.prepare_preview_photo(
            info=None,
            source_id="short123456",
            output_path=out_preview,
            is_vertical=True,
        )
        assert ok is True
        assert os.path.exists(out_preview)
        with Image.open(out_preview) as img:
            assert img.format == "JPEG"
            # 9:16 aspect ratio: width must be smaller than height
            assert img.width < img.height
            assert abs((img.width / float(img.height)) - (9 / 16)) < 0.05


@pytest.mark.asyncio
async def test_prepare_preview_photo_returns_false_when_all_fail(tmp_path):
    out_preview = str(tmp_path / "preview_fail.jpg")

    async def fake_get(url):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.content = b""
        return mock_resp

    with patch("httpx.AsyncClient.get", side_effect=fake_get):
        ok = await ThumbnailService.prepare_preview_photo(
            info=None,
            source_id="nonexistent1",
            output_path=out_preview,
            is_vertical=False,
        )
        assert ok is False
        assert not os.path.exists(out_preview)


@pytest.mark.asyncio
async def test_download_full_thumbnail_file(tmp_path):
    """
    Verifies download_full_thumbnail_file retrieves the uncompressed full resolution image.
    """
    out_full = str(tmp_path / "full_art.jpg")
    sample_img = tmp_path / "sample_1080p.jpg"
    Image.new("RGB", (1920, 1080), color=(80, 180, 240)).save(str(sample_img), "JPEG")
    sample_bytes = sample_img.read_bytes()

    async def fake_get(url):
        mock_resp = MagicMock()
        if "maxresdefault" in str(url):
            mock_resp.status_code = 200
            mock_resp.content = sample_bytes
        else:
            mock_resp.status_code = 404
            mock_resp.content = b""
        return mock_resp

    with patch("httpx.AsyncClient.get", side_effect=fake_get):
        res = await ThumbnailService.download_full_thumbnail_file(
            info=None,
            source_id="abc12345678",
            output_path=out_full,
        )
        assert res is not None
        path, w, h = res
        assert path == out_full
        assert (w, h) == (1920, 1080)
        assert os.path.exists(out_full)


def test_create_telegram_chat_thumbnail_fits_320_bounds(tmp_path):
    """
    Verifies that create_telegram_chat_thumbnail scales down a large 1080p image
    to fit strictly inside 320x320 while preserving aspect ratio, applying unsharp mask,
    and outputting a compliant JPEG.
    """
    src_img = tmp_path / "art_1080p.jpg"
    out_320 = str(tmp_path / "tg_thumb_320.jpg")
    Image.new("RGB", (1920, 1080), color=(100, 200, 150)).save(str(src_img), "JPEG")

    ok = ThumbnailService.create_telegram_chat_thumbnail(
        source_image_path=str(src_img),
        output_thumb_path=out_320,
    )
    assert ok is True
    assert os.path.exists(out_320)
    with Image.open(out_320) as img:
        assert img.format == "JPEG"
        assert img.width <= 320 and img.height <= 320
        assert img.size == (320, 180)


def test_create_telegram_chat_thumbnail_vertical_shorts(tmp_path):
    """
    Verifies vertical Shorts are cropped and scaled to fit within 320x320 (e.g. 180x320).
    """
    src_img = tmp_path / "short_horizontal.jpg"
    out_320 = str(tmp_path / "tg_thumb_short.jpg")
    Image.new("RGB", (1280, 720), color=(150, 100, 200)).save(str(src_img), "JPEG")

    ok = ThumbnailService.create_telegram_chat_thumbnail(
        source_image_path=str(src_img),
        output_thumb_path=out_320,
        is_vertical=True,
    )
    assert ok is True
    assert os.path.exists(out_320)
    with Image.open(out_320) as img:
        assert img.format == "JPEG"
        assert img.width <= 320 and img.height <= 320
        assert img.width < img.height


def test_candidate_urls_prioritize_live_tokens_over_deterministic_bare_urls():
    """
    Verifies that get_candidate_thumbnail_urls prioritizes live, tokenized URLs
    from yt-dlp metadata ahead of bare deterministic CDN URLs.
    """
    metadata = {
        "id": "R9B3DfUg_8Q",
        "thumbnail": "https://i.ytimg.com/vi/R9B3DfUg_8Q/hqdefault.jpg?sqp=live_token&rs=token2",
        "thumbnails": [
            {
                "id": "maxresdefault",
                "url": "https://i.ytimg.com/vi/R9B3DfUg_8Q/maxresdefault.jpg?sqp=live_token_hd&rs=token_hd",
                "width": 1920,
                "height": 1080,
            },
            {
                "id": "hq720",
                "url": "https://i.ytimg.com/vi/R9B3DfUg_8Q/hq720.jpg?sqp=live_token_720&rs=token_720",
                "width": 1280,
                "height": 720,
            },
        ],
    }
    candidates = ThumbnailService.get_candidate_thumbnail_urls(metadata, source_id="R9B3DfUg_8Q")
    assert len(candidates) > 2
    # First candidates MUST be the live tokenized ones
    assert "sqp=live_token_hd" in candidates[0]
    assert "sqp=live_token_720" in candidates[1]
    # The bare URLs must appear strictly after all metadata URLs
    bare_url = "https://i.ytimg.com/vi/R9B3DfUg_8Q/maxresdefault.jpg"
    assert bare_url in candidates
    assert candidates.index(bare_url) > candidates.index(candidates[0])


@pytest.mark.asyncio
async def test_download_full_thumbnail_file_crops_vertical_shorts(tmp_path):
    """
    Verifies download_full_thumbnail_file with is_vertical=True crops
    a 16:9 pillarboxed thumbnail into 9:16 vertical orientation.
    """
    out_cover = str(tmp_path / "cover_vertical.jpg")
    img_horizontal = tmp_path / "sample_16_9.jpg"
    Image.new("RGB", (1280, 720), color=(10, 50, 100)).save(str(img_horizontal), "JPEG")
    sample_bytes = img_horizontal.read_bytes()

    async def fake_get(url):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = sample_bytes
        return mock_resp

    with patch("httpx.AsyncClient.get", side_effect=fake_get):
        res = await ThumbnailService.download_full_thumbnail_file(
            info=None,
            source_id="short123456",
            output_path=out_cover,
            is_vertical=True,
            target_ratio=9 / 16.0,
        )
        assert res is not None
        path, w, h = res
        assert path == out_cover
        assert w < h
        assert abs((w / float(h)) - (9 / 16.0)) < 0.05
        with Image.open(out_cover) as img:
            assert img.width < img.height


@pytest.mark.asyncio
async def test_prepend_thumbnail_intro_zero_reencode(tmp_path):
    """
    Verifies FFmpegService.prepend_thumbnail_intro:
    - Generates 0.1s intro snippet from thumbnail
    - Concatenates with main video using stream-copy (-c copy)
    - Sets Frame 0 to the thumbnail image
    - Embeds attached_pic cover art stream
    """
    vid_in = str(tmp_path / "main.mp4")
    thumb_in = str(tmp_path / "thumb.jpg")
    vid_out = str(tmp_path / "out_intro.mp4")

    # Generate 1s test video
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=44100",
        "-t", "1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        vid_in
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Distinctive colored thumbnail (pure blue)
    Image.new("RGB", (320, 240), color=(0, 0, 255)).save(thumb_in, "JPEG")

    ok = await FFmpegService.prepend_thumbnail_intro(
        video_path=vid_in,
        output_path=vid_out,
        thumbnail_image_path=thumb_in,
        cover_image_path=thumb_in,
        duration_sec=0.1,
    )
    assert ok is True
    assert os.path.exists(vid_out)

    # Probe final output
    probe = await FFmpegService.probe_file(vid_out)
    v_streams = [s for s in probe.get("streams", []) if s.get("codec_type") == "video"]
    assert len(v_streams) == 2  # main video + attached_pic
    assert v_streams[1].get("disposition", {}).get("attached_pic") == 1

    # Extract frame 0 to verify it's the thumbnail (pure blue)
    f0_path = str(tmp_path / "frame0.jpg")
    subprocess.run(["ffmpeg", "-y", "-i", vid_out, "-vframes", "1", f0_path], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with Image.open(f0_path) as img:
        r, g, b = img.getpixel((50, 50))
        assert b > 200 and r < 50 and g < 50


def test_tokenized_thumbnails_beat_bare_urls_with_higher_preference():
    """
    Verifies that when yt-dlp assigns arbitrary higher preference to bare URLs
    (e.g. pref=-1 on bare maxresdefault vs pref=-39 on maxres2?sqp=...),
    _rank_thumbnails correctly prioritizes the live tokenized URL over the stale bare URL.
    """
    metadata = {
        "id": "Qpi_xEujan0",
        "thumbnail": "https://i.ytimg.com/vi/Qpi_xEujan0/hq720_2.jpg?sqp=token_720&rs=token_rs",
        "thumbnails": [
            {
                "id": "43",
                "url": "https://i.ytimg.com/vi/Qpi_xEujan0/maxresdefault.jpg",
                "width": None,
                "height": None,
                "preference": -1,
            },
            {
                "id": "0",
                "url": "https://i.ytimg.com/vi/Qpi_xEujan0/maxres2.jpg?sqp=token_1080&rs=token_rs",
                "width": 1920,
                "height": 1080,
                "preference": -39,
            },
        ],
    }
    ranked = ThumbnailService._rank_thumbnails(metadata)
    assert len(ranked) >= 2
    # The live tokenized thumbnail must be ranked at index 0 despite lower yt-dlp preference!
    assert "sqp=token_1080" in ranked[0]["url"]
    assert ranked[0]["url"] == "https://i.ytimg.com/vi/Qpi_xEujan0/maxres2.jpg?sqp=token_1080&rs=token_rs"


def test_tiny_tokenized_thumbnails_never_beat_hd_bare_urls():
    """
    Verifies that a tiny 168x94 tokenized thumbnail (from YouTube API notifications/search)
    never beats a 1080p bare URL (such as maxresdefault.jpg on Despacito).
    Resolution tier must always dominate over token presence.
    """
    metadata = {
        "id": "kJQP7kiw5Fk",
        "thumbnails": [
            {
                "id": "31",
                "url": "https://i.ytimg.com/vi/kJQP7kiw5Fk/hqdefault.jpg?sqp=token_tiny",
                "width": 168,
                "height": 94,
                "preference": -7,
            },
            {
                "id": "36",
                "url": "https://i.ytimg.com/vi/kJQP7kiw5Fk/hqdefault.jpg?sqp=token_med",
                "width": 480,
                "height": 360,
                "preference": -7,
            },
            {
                "id": "42",
                "url": "https://i.ytimg.com/vi/kJQP7kiw5Fk/maxresdefault.jpg",
                "width": None,
                "height": None,
                "preference": -1,
            },
        ],
    }
    cands = ThumbnailService.get_candidate_thumbnail_urls(metadata, source_id="kJQP7kiw5Fk")
    # Top candidate MUST be maxresdefault (1080p tier), NOT the tiny 168x94 tokenized image!
    assert len(cands) > 0
    assert "maxresdefault" in cands[0]
    # The 168x94 thumbnail must be at the very bottom
    assert "token_tiny" not in cands[0]


def test_b15ftNfMjK8_selects_sddefault_over_tiny_tokenized_urls():
    """
    For videos like b15ftNfMjK8 where maxres and hq720 return 404, but sddefault (640x480) exists,
    sddefault.jpg (Tier 2) must be prioritized over tiny 168x94 or 320x180 tokenized endpoints.
    """
    metadata = {
        "id": "b15ftNfMjK8",
        "thumbnails": [
            {
                "id": "32",
                "url": "https://i.ytimg.com/vi/b15ftNfMjK8/hqdefault.jpg?sqp=token_168",
                "width": 168,
                "height": 94,
                "preference": -7,
            },
            {
                "id": "27",
                "url": "https://i.ytimg.com/vi/b15ftNfMjK8/mqdefault.jpg?sqp=token_320",
                "width": 320,
                "height": 180,
                "preference": -11,
            },
            {
                "id": "36",
                "url": "https://i.ytimg.com/vi/b15ftNfMjK8/hqdefault.jpg?sqp=token_480",
                "width": 480,
                "height": 360,
                "preference": -7,
            },
            {
                "id": "38",
                "url": "https://i.ytimg.com/vi/b15ftNfMjK8/sddefault.jpg",
                "width": None,
                "height": None,
                "preference": -5,
            },
        ],
    }
    cands = ThumbnailService.get_candidate_thumbnail_urls(metadata, source_id="b15ftNfMjK8")
    # Candidates 0 and 1 will be speculative maxres and hq720 (which get 404 on b15)
    # The first metadata / sddefault URL must be sddefault.jpg (Tier 2, 640x480)
    # followed by 480x360, and tiny 168x94 must be last!
    sd_idx = next(i for i, u in enumerate(cands) if "sddefault" in u)
    tiny_idx = next(i for i, u in enumerate(cands) if "token_168" in u)
    assert sd_idx < tiny_idx


def test_auto_crop_black_borders_pillarbox():
    # 1280x720 canvas with 160px black pillarbox on left and right, inner 960x720 red
    img = Image.new("RGB", (1280, 720), (0, 0, 0))
    inner = Image.new("RGB", (960, 720), (220, 50, 50))
    img.paste(inner, (160, 0))

    cropped = ThumbnailService.auto_crop_black_borders(img)
    assert cropped.size == (960, 720)


def test_auto_crop_black_borders_letterbox():
    # 640x480 canvas with 60px black letterbox on top and bottom, inner 640x360 blue
    img = Image.new("RGB", (640, 480), (0, 0, 0))
    inner = Image.new("RGB", (640, 360), (50, 50, 220))
    img.paste(inner, (0, 60))

    cropped = ThumbnailService.auto_crop_black_borders(img)
    assert cropped.size == (640, 360)


def test_auto_crop_black_borders_clean_image_untouched():
    # Fully painted image with no black borders
    img = Image.new("RGB", (1280, 720), (100, 150, 200))
    cropped = ThumbnailService.auto_crop_black_borders(img)
    assert cropped.size == (1280, 720)


@pytest.mark.asyncio
async def test_prepare_preview_photo_crops_black_borders(tmp_path):
    """
    Verifies that prepare_preview_photo (used for sending the keyboard message's thumbnail image)
    auto-crops black pillarboxes/letterboxes instead of sending uncropped black bars.
    """
    out_preview = str(tmp_path / "keyboard_preview.jpg")

    # Create dummy 1280x720 image with 160px black pillarbox on sides (4:3 artwork)
    pillarboxed_img = Image.new("RGB", (1280, 720), (0, 0, 0))
    inner = Image.new("RGB", (960, 720), (220, 50, 50))
    pillarboxed_img.paste(inner, (160, 0))
    img_bytes_io = io.BytesIO()
    pillarboxed_img.save(img_bytes_io, format="JPEG", quality=95)
    img_bytes = img_bytes_io.getvalue()

    with patch.object(ThumbnailService, "download_thumbnail_image", new_callable=AsyncMock) as mock_dl:
        async def _fake_dl(url, path, client=None):
            with open(path, "wb") as f:
                f.write(img_bytes)
            return True

        mock_dl.side_effect = _fake_dl

        ok = await ThumbnailService.prepare_preview_photo(
            info={"id": "O2jkV4BsN6U", "thumbnails": [{"url": "https://i.ytimg.com/vi/O2jkV4BsN6U/maxresdefault.jpg", "width": 1280, "height": 720}]},
            source_id="O2jkV4BsN6U",
            output_path=out_preview,
            is_vertical=False,
        )

        assert ok is True
        assert os.path.exists(out_preview)

        with Image.open(out_preview) as res:
            # Must be cropped from 1280x720 (16:9) to ~960x720 (4:3)
            assert abs(res.size[0] - 960) <= 2
            assert res.size[1] == 720






