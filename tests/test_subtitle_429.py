import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import pytest
from yt_dlp.utils import DownloadError

from src.core.config import settings
from src.models.channel import RequiredChannel
from src.services.ai_service import AIService
from src.services.must_join_service import MustJoinService
from src.services.ytdlp_service import (
    YtDlpService,
    YouTubeSubtitleRateLimitError,
    is_http_429_error,
    set_subtitle_cooldown,
    get_subtitle_cooldown_remaining,
)


import src.services.ytdlp_service as ytdlp_mod
import src.core.redis as redis_mod


@pytest.fixture(autouse=True)
async def reset_cooldown():
    ytdlp_mod._in_memory_subtitle_cooldown_until = 0.0
    try:
        r = redis_mod.get_redis_client()
        await r.delete(ytdlp_mod.SUBTITLE_COOLDOWN_KEY)
    except Exception:
        pass
    yield
    ytdlp_mod._in_memory_subtitle_cooldown_until = 0.0
    try:
        r = redis_mod.get_redis_client()
        await r.delete(ytdlp_mod.SUBTITLE_COOLDOWN_KEY)
    except Exception:
        pass


# ==============================================================================
# 1. YOUTUBE SUBTITLE 429 RETRY & COOLDOWN TESTS
# ==============================================================================

def test_is_http_429_error():
    assert is_http_429_error(DownloadError("ERROR: Unable to download video subtitles: HTTP Error 429: Too Many Requests")) is True
    assert is_http_429_error(DownloadError("HTTP Error 429")) is True
    assert is_http_429_error(DownloadError("status 429")) is True
    assert is_http_429_error(DownloadError("HTTP Error 404: Not Found")) is False
    assert is_http_429_error(DownloadError("Private video")) is False
    assert is_http_429_error(ValueError("Invalid format")) is False


@pytest.mark.asyncio
async def test_subtitle_429_single_retry_success(monkeypatch):
    """
    429 -> retry -> success:
    Attempt 1 encounters HTTP 429, attempt 2 succeeds.
    """
    call_count = 0
    sleep_calls = []

    def mock_download(self, urls):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise DownloadError("HTTP Error 429: Too Many Requests")
        # Attempt 2 succeeds

    async def mock_sleep(duration):
        sleep_calls.append(duration)

    monkeypatch.setattr("yt_dlp.YoutubeDL.download", mock_download)
    monkeypatch.setattr("asyncio.sleep", mock_sleep)

    retry_callbacks = []
    async def on_retry(attempt, wait_sec, err):
        retry_callbacks.append((attempt, wait_sec, str(err)))

    await YtDlpService.download_subtitles(
        url="https://www.youtube.com/watch?v=test429_1",
        output_template="/tmp/subs.%(ext)s",
        on_retry=on_retry,
    )

    assert call_count == 2
    assert len(retry_callbacks) == 1
    assert retry_callbacks[0][0] == 1
    # Attempt 0 backoff: 10s + jitter [0.5, 3.0]
    assert 10.5 <= retry_callbacks[0][1] <= 13.0
    assert len(sleep_calls) == 1


@pytest.mark.asyncio
async def test_subtitle_429_double_retry_success(monkeypatch):
    """
    429 -> 429 -> success:
    Attempts 1 and 2 encounter HTTP 429, attempt 3 succeeds.
    """
    call_count = 0
    sleep_calls = []

    def mock_download(self, urls):
        nonlocal call_count
        call_count += 1
        if call_count in (1, 2):
            raise DownloadError("HTTP Error 429: Too Many Requests")
        # Attempt 3 succeeds

    async def mock_sleep(duration):
        sleep_calls.append(duration)

    monkeypatch.setattr("yt_dlp.YoutubeDL.download", mock_download)
    monkeypatch.setattr("asyncio.sleep", mock_sleep)

    retry_records = []
    async def on_retry(attempt, wait_sec, err):
        retry_records.append((attempt, wait_sec))

    await YtDlpService.download_subtitles(
        url="https://www.youtube.com/watch?v=test429_2",
        output_template="/tmp/subs.%(ext)s",
        on_retry=on_retry,
    )

    assert call_count == 3
    assert len(retry_records) == 2
    assert retry_records[0][0] == 1
    assert 10.5 <= retry_records[0][1] <= 13.0  # Attempt 1: ~10s
    assert retry_records[1][0] == 2
    assert 30.5 <= retry_records[1][1] <= 33.0  # Attempt 2: ~30s


@pytest.mark.asyncio
async def test_subtitle_429_all_retries_exhausted(monkeypatch):
    """
    429 -> all retries exhausted:
    All 4 attempts fail with HTTP 429.
    Must raise YouTubeSubtitleRateLimitError after 3 retries (total 4 attempts).
    """
    call_count = 0
    sleep_calls = []

    def mock_download(self, urls):
        nonlocal call_count
        call_count += 1
        raise DownloadError("HTTP Error 429: Too Many Requests")

    async def mock_sleep(duration):
        sleep_calls.append(duration)

    monkeypatch.setattr("yt_dlp.YoutubeDL.download", mock_download)
    monkeypatch.setattr("asyncio.sleep", mock_sleep)

    with pytest.raises(YouTubeSubtitleRateLimitError) as exc_info:
        await YtDlpService.download_subtitles(
            url="https://www.youtube.com/watch?v=test429_exhausted",
            output_template="/tmp/subs.%(ext)s",
        )

    assert call_count == 4  # Initial + 3 retries
    assert "YouTube rate-limited subtitle extraction (HTTP 429: Too Many Requests) across all retries" in str(exc_info.value)
    assert "configure YouTube Cookies or a Proxy" in str(exc_info.value)
    assert len(sleep_calls) == 3


@pytest.mark.asyncio
async def test_subtitle_non_429_no_retry(monkeypatch):
    """
    non-429 DownloadError -> no retry:
    An unrelated error (e.g., HTTP 404, Private Video) must fail immediately without retrying.
    """
    call_count = 0
    sleep_calls = []

    def mock_download(self, urls):
        nonlocal call_count
        call_count += 1
        raise DownloadError("ERROR: Video unavailable. This video is private.")

    async def mock_sleep(duration):
        sleep_calls.append(duration)

    monkeypatch.setattr("yt_dlp.YoutubeDL.download", mock_download)
    monkeypatch.setattr("asyncio.sleep", mock_sleep)

    on_retry = AsyncMock()

    with pytest.raises(DownloadError) as exc_info:
        await YtDlpService.download_subtitles(
            url="https://www.youtube.com/watch?v=private_video",
            output_template="/tmp/subs.%(ext)s",
            on_retry=on_retry,
        )

    assert call_count == 1  # No retry!
    assert "video is private" in str(exc_info.value)
    on_retry.assert_not_called()
    assert len(sleep_calls) == 0


@pytest.mark.asyncio
async def test_subtitle_backoff_timing_and_jitter(monkeypatch):
    """
    Verifies that backoff timing uses conservative schedule with random jitter:
    attempt 0: 10s + jitter [0.5, 3.0]
    attempt 1: 30s + jitter [0.5, 3.0]
    attempt 2: 60s + jitter [0.5, 3.0]
    """
    call_count = 0
    observed_waits = []

    def mock_download(self, urls):
        nonlocal call_count
        call_count += 1
        raise DownloadError("HTTP Error 429: Too Many Requests")

    async def mock_sleep(duration):
        observed_waits.append(duration)

    monkeypatch.setattr("yt_dlp.YoutubeDL.download", mock_download)
    monkeypatch.setattr("asyncio.sleep", mock_sleep)

    with pytest.raises(YouTubeSubtitleRateLimitError):
        await YtDlpService.download_subtitles(
            url="https://www.youtube.com/watch?v=test_jitter",
            output_template="/tmp/subs.%(ext)s",
        )

    assert len(observed_waits) == 3
    # Check bounds
    assert 10.5 <= observed_waits[0] <= 13.0
    assert 30.5 <= observed_waits[1] <= 33.0
    assert 60.5 <= observed_waits[2] <= 63.0


@pytest.mark.asyncio
async def test_subtitle_global_cooldown():
    """
    Verifies that set_subtitle_cooldown sets a cooldown that get_subtitle_cooldown_remaining respects.
    """
    await set_subtitle_cooldown(cooldown_seconds=10)
    rem = await get_subtitle_cooldown_remaining()
    assert 0 < rem <= 10.0


# ==============================================================================
# 2. AI TIMEOUT FORMATTING & CUE LIMIT TESTS
# ==============================================================================

@pytest.mark.asyncio
async def test_ai_read_timeout_formatting(monkeypatch):
    """
    Verifies that httpx.ReadTimeout produces a descriptive error containing:
    - Provider timeout type (ReadTimeout)
    - Affected chunk number
    - Total chunks
    - Configured timeout (120s)
    Without relying on str(exception) which is empty.
    """
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "AI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setattr(settings, "AI_MODEL", "gpt-4o-mini")
    monkeypatch.setattr(settings, "AI_API_KEY", "sk-test-key")
    monkeypatch.setattr(settings, "AI_TIMEOUT", 120.0)
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    async def mock_post(url, headers, json):
        raise httpx.ReadTimeout("")

    sample_srt = "1\n00:00:01,000 --> 00:00:03,000\nHello timeout\n"

    with patch("httpx.AsyncClient.post", side_effect=mock_post):
        ok, res, err = await AIService.translate_english_to_persian(sample_srt)

    assert ok is False
    assert res is None
    assert "ReadTimeout" in err
    assert "chunk 1/1" in err
    assert "provider read timed out after 120s" in err


@pytest.mark.asyncio
async def test_ai_connect_timeout_formatting(monkeypatch):
    """
    Verifies that httpx.ConnectTimeout produces a descriptive error with connect timeout details.
    """
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "AI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setattr(settings, "AI_MODEL", "gpt-4o-mini")
    monkeypatch.setattr(settings, "AI_API_KEY", "sk-test-key")
    monkeypatch.setattr(settings, "AI_TIMEOUT", 120.0)
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    async def mock_post(url, headers, json):
        raise httpx.ConnectTimeout("")

    sample_srt = "1\n00:00:01,000 --> 00:00:03,000\nHello connect timeout\n"

    with patch("httpx.AsyncClient.post", side_effect=mock_post):
        ok, res, err = await AIService.translate_english_to_persian(sample_srt)

    assert ok is False
    assert res is None
    assert "ConnectTimeout" in err
    assert "failed to connect to provider within 20s" in err
    assert "chunk 1/1" in err


@pytest.mark.asyncio
async def test_ai_500_cues_limit_behavior(monkeypatch):
    """
    Verifies the 500-cue limit behavior (50 chunks × 10 cues = 500 cues maximum):
    - Subtitle with exactly 500 cues is allowed (50 chunks).
    - Subtitle with 501 cues is rejected before API calls (51 chunks > 50 chunks).
    """
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "AI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setattr(settings, "AI_MODEL", "gpt-4o-mini")
    monkeypatch.setattr(settings, "AI_API_KEY", "sk-test-key")
    monkeypatch.setattr(settings, "AI_CHUNK_SIZE", 10)
    monkeypatch.setattr(settings, "AI_MAX_CHUNKS", 50)  # 50 chunks = 500 cues max

    # 1. Test 501 cues: must be rejected
    segments_501 = []
    for i in range(1, 502):
        m, s = divmod(i * 2, 60)
        h, m = divmod(m, 60)
        segments_501.append(f"{i}\n{h:02d}:{m:02d}:{s:02d},000 --> {h:02d}:{m:02d}:{s+1:02d},000\nCue {i}\n")
    srt_501 = "\n".join(segments_501)

    ok, res, err = await AIService.translate_english_to_persian(srt_501)
    assert ok is False
    assert res is None
    assert "51 chunks / 501 cues exceeds maximum allowed limit of 50 chunks / 500 cues" in err
    assert "Please download English subtitles instead." in err


# ==============================================================================
# 3. MUST-JOIN EXEMPTION WHITELIST TESTS
# ==============================================================================

def test_parse_exemptions():
    raw = "123456789, @Owner, 987654321; admin\n@VIP_USER"
    ids, usernames = MustJoinService.parse_exemptions(raw)
    assert 123456789 in ids
    assert 987654321 in ids
    assert "owner" in usernames
    assert "admin" in usernames
    assert "vip_user" in usernames


def test_is_user_exempt_by_user_id():
    raw = "123456789, @admin"
    # Canonical numeric match
    assert MustJoinService.is_user_exempt(user_id=123456789, username="any_name", exemptions_str=raw) is True
    # Non-exempt ID
    assert MustJoinService.is_user_exempt(user_id=999999999, username="regular_user", exemptions_str=raw) is False


def test_is_user_exempt_by_username():
    raw = "123456789, @TestAdmin, vip"
    # Case-insensitive match with @
    assert MustJoinService.is_user_exempt(user_id=111, username="@testadmin", exemptions_str=raw) is True
    # Case-insensitive match without @
    assert MustJoinService.is_user_exempt(user_id=111, username="TestAdmin", exemptions_str=raw) is True
    # Another username
    assert MustJoinService.is_user_exempt(user_id=111, username="VIP", exemptions_str=raw) is True
    # Non-exempt username
    assert MustJoinService.is_user_exempt(user_id=111, username="other_user", exemptions_str=raw) is False


@pytest.mark.asyncio
async def test_must_join_exemption_bypasses_all_membership_api_calls(db_session, monkeypatch):
    """
    Exempt users must bypass ALL Must-Join membership API calls.
    bot.get_chat_member must NOT be called even once.
    """
    monkeypatch.setattr(settings, "MUST_JOIN_ENABLED", True)
    monkeypatch.setattr(settings, "MUST_JOIN_EXEMPT_USERS", "123456789, @whitelisted")

    # Add a required channel
    ch = RequiredChannel(chat_id=-1001999, title="Required Channel", enabled=True, bot_status="administrator")
    db_session.add(ch)
    await db_session.commit()

    bot = MagicMock()
    bot.get_chat_member = AsyncMock()

    # 1. Exempt by user_id
    is_auth, missing = await MustJoinService.require_must_join(
        bot, db_session, user_id=123456789, username="regular_nick", force_authoritative=True
    )
    assert is_auth is True
    assert missing == []
    bot.get_chat_member.assert_not_called()

    # 2. Exempt by username
    is_auth, missing = await MustJoinService.require_must_join(
        bot, db_session, user_id=555555, username="@whitelisted", force_authoritative=True
    )
    assert is_auth is True
    assert missing == []
    bot.get_chat_member.assert_not_called()


@pytest.mark.asyncio
async def test_must_join_non_exempt_user_checked(db_session, monkeypatch):
    """
    Non-exempt users must still undergo normal Telegram membership verification.
    """
    monkeypatch.setattr(settings, "MUST_JOIN_ENABLED", True)
    monkeypatch.setattr(settings, "MUST_JOIN_EXEMPT_USERS", "123456789, @whitelisted")

    ch = RequiredChannel(chat_id=-1001999, title="Required Channel", enabled=True, bot_status="administrator")
    db_session.add(ch)
    await db_session.commit()

    bot = MagicMock()
    # User is not a member
    bot.get_chat_member = AsyncMock(return_value=MagicMock(status="left"))

    # Non-exempt user
    is_auth, missing = await MustJoinService.require_must_join(
        bot, db_session, user_id=999999, username="@stranger", force_authoritative=True
    )
    assert is_auth is False
    assert len(missing) == 1
    assert missing[0].chat_id == -1001999
    bot.get_chat_member.assert_called_once_with(chat_id=-1001999, user_id=999999)
