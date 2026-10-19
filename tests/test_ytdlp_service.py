import pytest
from src.services.ytdlp_service import (
    YtDlpService,
    extract_youtube_id,
    get_canonical_url,
)


def test_youtube_url_extraction():
    # Standard watch URL
    assert extract_youtube_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    # Short youtu.be URL
    assert extract_youtube_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    # Shorts URL
    assert extract_youtube_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    # Embed URL
    assert extract_youtube_id("https://www.youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    # With additional parameters
    assert extract_youtube_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&feature=share&t=42s") == "dQw4w9WgXcQ"
    # Invalid / non-youtube
    assert extract_youtube_id("https://vimeo.com/123456") is None
    assert extract_youtube_id("hello world") is None

    # Canonical URL
    assert get_canonical_url("dQw4w9WgXcQ") == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_dynamic_quality_extraction_and_deduplication():
    # Mock yt-dlp format metadata
    mock_info = {
        "formats": [
            {"format_id": "137", "vcodec": "avc1.640028", "acodec": "none", "height": 1080},
            {"format_id": "248", "vcodec": "vp9", "acodec": "none", "height": 1080},
            {"format_id": "399", "vcodec": "av01", "acodec": "none", "height": 1080},
            {"format_id": "136", "vcodec": "avc1.4d401f", "acodec": "none", "height": 720},
            {"format_id": "135", "vcodec": "avc1.4d401e", "acodec": "none", "height": 480},
            {"format_id": "134", "vcodec": "avc1.4d401e", "acodec": "none", "height": 360},
            {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "height": None},  # Audio-only
            {"format_id": "251", "vcodec": "none", "acodec": "opus", "height": None},        # Audio-only
        ]
    }

    resolutions = YtDlpService.get_available_resolutions(mock_info)

    # 1. Must be sorted descending
    assert resolutions == [1080, 720, 480, 360]

    # 2. Must deduplicate (1080 appears only once despite 3 source codecs)
    assert resolutions.count(1080) == 1

    # 3. Unavailable resolutions must never appear
    assert 2160 not in resolutions
    assert 1440 not in resolutions
    assert 240 not in resolutions


def test_exact_quality_format_spec():
    # Primary format priority: must prioritize exact height
    spec_1080 = YtDlpService.build_video_format_spec(1080)
    assert "height=1080" in spec_1080
    assert spec_1080.startswith("bestvideo[height=1080]")

    spec_720_h264 = YtDlpService.build_video_format_spec(720, "H264")
    assert "height=720" in spec_720_h264
    assert "vcodec~='(?i)^(avc1|h264)'" in spec_720_h264

    spec_1080_h265 = YtDlpService.build_video_format_spec(1080, "H265")
    assert "height=1080" in spec_1080_h265
    assert "vcodec~='(?i)^(hev1|hvc1|hevc|h265)'" in spec_1080_h265


def test_ytdlp_player_client_configuration(monkeypatch):
    """
    1. The common options include both 'default' and 'android' in the YouTube player_client setting.
    2. A missing YTDLP_PO_TOKEN does not create a malformed token argument.
    """
    from src.core.config import settings

    monkeypatch.setattr(settings, "YTDLP_PO_TOKEN", None)
    opts = YtDlpService.get_common_base_opts()

    extractor_args = opts.get("extractor_args", {})
    assert "youtube" in extractor_args
    youtube_args = extractor_args["youtube"]

    # Verify default,android is in player_client
    assert any("player_client=default,android" in arg for arg in youtube_args)
    # Verify no po_token is present when YTDLP_PO_TOKEN is None/empty
    assert not any("po_token" in arg for arg in youtube_args)


def test_ytdlp_po_token_formats_and_integrity(monkeypatch):
    """
    Verifies that:
    1. A configured raw token is safely prefixed with web+ without corrupting player_client.
    2. A configured token already prefixed with web+ does not become web+web+.
    3. Empty or whitespace-only token does not inject a po_token argument.
    """
    from src.core.config import settings

    # Case 1: Raw token
    monkeypatch.setattr(settings, "YTDLP_PO_TOKEN", "raw_token_xyz")
    opts1 = YtDlpService.get_common_base_opts()
    args1 = opts1["extractor_args"]["youtube"]
    assert any("player_client=default,android" in a for a in args1)
    assert any("po_token=web+raw_token_xyz" in a for a in args1)

    # Case 2: Already prefixed token
    monkeypatch.setattr(settings, "YTDLP_PO_TOKEN", "web+prefixed_token_abc")
    opts2 = YtDlpService.get_common_base_opts()
    args2 = opts2["extractor_args"]["youtube"]
    assert any("player_client=default,android" in a for a in args2)
    assert any("po_token=web+prefixed_token_abc" in a for a in args2)
    assert not any("po_token=web+web+" in a for a in args2)

    # Case 3: Whitespace only
    monkeypatch.setattr(settings, "YTDLP_PO_TOKEN", "   ")
    opts3 = YtDlpService.get_common_base_opts()
    args3 = opts3["extractor_args"]["youtube"]
    assert any("player_client=default,android" in a for a in args3)
    assert not any("po_token" in a for a in args3)


def test_ytdlp_opts_construction_consistency():
    """
    Verifies that extractor_args with player_client=default,android are consistently
    present in both get_metadata_opts and get_download_opts.
    """
    meta_opts = YtDlpService.get_metadata_opts()
    assert "extractor_args" in meta_opts
    assert any("player_client=default,android" in a for a in meta_opts["extractor_args"]["youtube"])
    assert meta_opts.get("retries") == 2

    dl_opts = YtDlpService.get_download_opts()
    assert "extractor_args" in dl_opts
    assert any("player_client=default,android" in a for a in dl_opts["extractor_args"]["youtube"])
    assert dl_opts.get("concurrent_fragments") == 16

