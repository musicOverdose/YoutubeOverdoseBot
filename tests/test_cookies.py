import os
import pytest
from src.services.cookie_service import CookieService


def test_cookie_validation():
    valid_netscape = (
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t1780000000\tVISITOR_INFO1_LIVE\tsome_value\n"
        ".youtube.com\tTRUE\t/\tTRUE\t1780000000\tLOGIN_INFO\tanother_value\n"
        "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1780000000\tSID\tsecure_sid\n"
    )

    is_valid, count, err = CookieService.validate_netscape_format(valid_netscape)
    assert is_valid is True
    assert count == 3
    assert err is None

    # Corrupted / invalid cookies (not enough fields)
    invalid_content = "this is just a random text string not a netscape cookie"
    is_valid, count, err = CookieService.validate_netscape_format(invalid_content)
    assert is_valid is False
    assert count == 0
    assert err is not None


def test_atomic_cookie_saving(tmp_path, monkeypatch):
    test_cookies_file = str(tmp_path / "secrets" / "youtube-cookies.txt")
    monkeypatch.setattr("src.core.config.settings.YTDLP_COOKIES_FILE", test_cookies_file)

    valid_netscape = (
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t1780000000\tTEST_COOKIE\tsecret_123\n"
    )

    ok, count, err = CookieService.save_cookies_atomically(valid_netscape)
    assert ok is True
    assert count == 1
    assert os.path.exists(test_cookies_file)

    # Verify status masking (does NOT return secret values)
    status = CookieService.get_status()
    assert status["configured"] is True
    assert status["cookie_count"] == 1
    assert "secret_123" not in str(status)

    # Delete
    deleted = CookieService.delete_cookies()
    assert deleted is True
    assert not os.path.exists(test_cookies_file)


def test_read_only_cookie_file_loading_and_saving(tmp_path):
    """
    Verify yt-dlp successfully loads cookies from a read-only file (chmod 444)
    and exits the YoutubeDL context manager cleanly without raising PermissionError or OSError.
    """
    import yt_dlp
    from src.services.ytdlp_service import YtDlpService

    ro_cookie_path = str(tmp_path / "readonly_cookies.txt")
    with open(ro_cookie_path, "w") as f:
        f.write("# Netscape HTTP Cookie File\n")
        f.write(".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSESSION_TOKEN\tread_only_secret\n")

    os.chmod(ro_cookie_path, 0o444)
    try:
        opts = {"cookiefile": ro_cookie_path, "quiet": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            cookies = list(ydl.cookiejar)
            assert len(cookies) == 1
            assert cookies[0].name == "SESSION_TOKEN"
            assert cookies[0].value == "read_only_secret"
    finally:
        os.chmod(ro_cookie_path, 0o666)


def test_read_only_filesystem_erofs_suppression(tmp_path):
    """
    Verify yt-dlp suppresses [Errno 30] Read-only file system (errno.EROFS)
    when cookie secrets volume is mounted :ro in Docker containers.
    """
    import errno
    from unittest.mock import patch
    import yt_dlp
    from yt_dlp.cookies import YoutubeDLCookieJar
    from src.services.ytdlp_service import _is_readonly_or_permission_error

    # Verify helper correctly flags EROFS (30)
    erofs_exc = OSError(errno.EROFS, "Read-only file system: '/config/secrets/youtube-cookies.txt'")
    assert _is_readonly_or_permission_error(erofs_exc) is True

    cookie_path = str(tmp_path / "cookies.txt")
    with open(cookie_path, "w") as f:
        f.write("# Netscape HTTP Cookie File\n")
        f.write(".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSESSION\ttest_val\n")

    with patch.object(
        YoutubeDLCookieJar,
        "save",
        side_effect=OSError(errno.EROFS, "Read-only file system: '/config/secrets/youtube-cookies.txt'"),
    ):
        # Should not raise OSError [Errno 30]
        with yt_dlp.YoutubeDL({"cookiefile": cookie_path, "quiet": True}) as ydl:
            assert len(list(ydl.cookiejar)) == 1


@pytest.mark.asyncio
async def test_ytdlp_service_extract_metadata_with_readonly_cookiefile(tmp_path, monkeypatch):
    """
    Verify YtDlpService.extract_metadata succeeds cleanly when YTDLP_COOKIES_FILE
    points to a read-only file, verifying full lifecycle without [Errno 30] or [Errno 13].
    """
    from unittest.mock import patch
    import yt_dlp
    from src.core.config import settings
    from src.services.ytdlp_service import YtDlpService

    ro_cookie_path = str(tmp_path / "youtube-cookies.txt")
    with open(ro_cookie_path, "w") as f:
        f.write("# Netscape HTTP Cookie File\n")
        f.write(".youtube.com\tTRUE\t/\tTRUE\t2147483647\tLOGIN_INFO\tauth_value\n")

    os.chmod(ro_cookie_path, 0o444)

    monkeypatch.setattr(settings, "YTDLP_COOKIES_ENABLED", True)
    monkeypatch.setattr(settings, "YTDLP_COOKIES_FILE", ro_cookie_path)

    fake_info = {
        "id": "dQw4w9WgXcQ",
        "title": "Test Title",
        "duration": 212,
        "formats": [{"vcodec": "avc1", "height": 1080}],
    }

    try:
        with patch.object(yt_dlp.YoutubeDL, "extract_info", return_value=fake_info):
            info = await YtDlpService.extract_metadata("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
            assert info["id"] == "dQw4w9WgXcQ"
            assert info["title"] == "Test Title"
    finally:
        os.chmod(ro_cookie_path, 0o666)


def test_worker_download_with_readonly_secrets_mount(tmp_path, monkeypatch):
    """
    Simulate worker container environment where /config/secrets is mounted :ro.
    Verifies that YoutubeDL(ydl_opts) context manager completes cleanly without
    failing on save_cookies.
    """
    from unittest.mock import patch
    import yt_dlp
    from src.core.config import settings
    from src.services.ytdlp_service import YtDlpService

    ro_cookie_path = str(tmp_path / "youtube-cookies.txt")
    with open(ro_cookie_path, "w") as f:
        f.write("# Netscape HTTP Cookie File\n")
        f.write(".youtube.com\tTRUE\t/\tTRUE\t2147483647\tLOGIN_INFO\tauth_value\n")

    os.chmod(ro_cookie_path, 0o444)
    monkeypatch.setattr(settings, "YTDLP_COOKIES_ENABLED", True)
    monkeypatch.setattr(settings, "YTDLP_COOKIES_FILE", ro_cookie_path)

    ydl_opts = YtDlpService.get_base_opts()
    assert ydl_opts.get("cookiefile") == ro_cookie_path

    try:
        with patch.object(yt_dlp.YoutubeDL, "download", return_value=0):
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download(["https://www.youtube.com/watch?v=dQw4w9WgXcQ"])
    finally:
        os.chmod(ro_cookie_path, 0o666)


@pytest.mark.asyncio
async def test_extract_metadata_automated_fallback_on_cookie_error(tmp_path, monkeypatch):
    """
    Verify that when global cookies are enabled but fail with a 'Sign in to confirm you’re not a bot'
    or invalid cookie error, extract_metadata automatically logs a warning and retries without
    cookies, returning valid metadata instead of crashing.
    """
    from unittest.mock import MagicMock
    import yt_dlp
    from src.core.config import settings
    from src.services.ytdlp_service import YtDlpService

    bad_cookie_path = str(tmp_path / "expired_cookies.txt")
    with open(bad_cookie_path, "w") as f:
        f.write("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2147483647\tOLD\tval\n")

    monkeypatch.setattr(settings, "YTDLP_COOKIES_ENABLED", True)
    monkeypatch.setattr(settings, "YTDLP_COOKIES_FILE", bad_cookie_path)

    calls = []

    def fake_extract_info(url, download=False):
        # Examine the current YoutubeDL options
        current_opts = fake_ydl_instance.params
        calls.append(current_opts.get("cookiefile"))
        if current_opts.get("cookiefile"):
            raise yt_dlp.utils.DownloadError("ERROR: [youtube] dQw4w9WgXcQ: Sign in to confirm you’re not a bot. Use --cookies")
        return {"id": "dQw4w9WgXcQ", "title": "Fallback Success"}

    fake_ydl_instance = MagicMock()
    fake_ydl_instance.extract_info.side_effect = fake_extract_info

    class FakeYoutubeDL:
        def __init__(self, params):
            self.params = params
            fake_ydl_instance.params = params
        def __enter__(self):
            return fake_ydl_instance
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYoutubeDL)

    info = await YtDlpService.extract_metadata("https://www.youtube.com/watch?v=dQw4w9WgXcQ", use_cache=False)
    assert info["title"] == "Fallback Success"
    assert len(calls) == 2
    assert calls[0] == bad_cookie_path  # First attempt with cookiefile
    assert calls[1] is None             # Fallback attempt without cookiefile


def test_explicit_cookie_file_respected_when_disabled(tmp_path, monkeypatch):
    """
    Verify that passing cookies_file explicitly attaches cookiefile even when
    YTDLP_COOKIES_ENABLED is False (essential for Admin UI Test Cookies endpoint).
    """
    from src.core.config import settings
    from src.services.ytdlp_service import YtDlpService

    test_file = str(tmp_path / "test_cookies.txt")
    with open(test_file, "w") as f:
        f.write("# Netscape\n")

    monkeypatch.setattr(settings, "YTDLP_COOKIES_ENABLED", False)

    # When cookies_file is None and YTDLP_COOKIES_ENABLED is False -> no cookiefile
    opts_default = YtDlpService.get_metadata_opts(cookies_file=None)
    assert "cookiefile" not in opts_default

    # When cookies_file is explicitly provided -> cookiefile is present
    opts_explicit = YtDlpService.get_metadata_opts(cookies_file=test_file)
    assert opts_explicit.get("cookiefile") == test_file

