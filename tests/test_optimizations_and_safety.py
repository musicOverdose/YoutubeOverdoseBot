import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import yt_dlp

from src.core.config import settings
from src.services.must_join_service import MustJoinService
from src.services.ytdlp_service import YtDlpService
from src.models.channel import RequiredChannel


def test_ytdlp_base_opts_performance_and_reliability():
    """
    Verifies that get_download_opts sets optimal throughput flags:
    concurrent_fragments=16, buffersize=4MB, trim_file_name=220, retries=10,
    and includes player_client=default,android in extractor_args.
    """
    opts = YtDlpService.get_download_opts()
    assert opts.get("concurrent_fragments") == 16
    assert opts.get("buffersize") == 4194304
    assert opts.get("trim_file_name") == 220
    assert opts.get("retries") == 10
    assert opts.get("fragment_retries") == 10
    assert opts.get("skip_unavailable_fragments") is True
    assert "retry_sleep_functions" in opts
    assert "extractor_args" in opts
    assert "youtube" in opts["extractor_args"]
    assert any("player_client=default,android" in arg for arg in opts["extractor_args"]["youtube"])


def test_ytdlp_po_token_injection(monkeypatch):
    """
    Verifies that PO-token is automatically injected into extractor_args when configured
    without removing or corrupting player_client.
    """
    monkeypatch.setattr(settings, "YTDLP_PO_TOKEN", "test_po_token_12345")
    opts = YtDlpService.get_base_opts()
    extractor_args = opts.get("extractor_args", {})
    assert "youtube" in extractor_args
    youtube_args = extractor_args["youtube"]
    assert any("player_client=default,android" in arg for arg in youtube_args)
    assert any("po_token=web+test_po_token_12345" in arg for arg in youtube_args)


@pytest.mark.asyncio
async def test_url_handler_rejects_livestream(monkeypatch):
    """
    Verifies that url_handler catches livestream videos early and displays a friendly notice.
    """
    from src.bot.handlers.url_handler import process_youtube_url

    bot_mock = AsyncMock()
    status_msg = AsyncMock()
    bot_mock.send_message.return_value = status_msg

    # Mock metadata returning is_live=True
    live_meta = {
        "title": "24/7 Lofi Stream",
        "is_live": True,
        "live_status": "is_live",
        "formats": [],
    }
    monkeypatch.setattr(
        "src.services.ytdlp_service.YtDlpService.extract_metadata",
        AsyncMock(return_value=live_meta),
    )

    await process_youtube_url(
        bot=bot_mock,
        chat_id=123456,
        canonical_url="https://www.youtube.com/watch?v=live_stream_id",
        source_id="live_stream_id",
    )

    # Status message must be edited with livestream rejection message
    status_msg.edit_text.assert_called_once()
    edited_text = status_msg.edit_text.call_args[0][0]
    assert "Livestreams are not supported" in edited_text


@pytest.mark.asyncio
async def test_must_join_auto_resolves_private_invite_link():
    """
    Verifies that resolve_missing_invite_links queries Telegram API for private channels
    without username or invite URL, and saves the resolved link.
    """
    bot_mock = AsyncMock()
    chat_mock = MagicMock()
    chat_mock.invite_link = "https://t.me/+joinPrivateChannelTest"
    chat_mock.username = None
    bot_mock.get_chat.return_value = chat_mock

    session_mock = AsyncMock()

    channel = RequiredChannel(
        id=1,
        chat_id=-100987654321,
        title="Private VIP Channel",
        username=None,
        invite_url=None,
        enabled=True,
    )

    await MustJoinService.resolve_missing_invite_links(
        bot=bot_mock,
        session=session_mock,
        channels=[channel],
    )

    bot_mock.get_chat.assert_called_once_with(-100987654321)
    assert channel.invite_url == "https://t.me/+joinPrivateChannelTest"
    session_mock.commit.assert_called_once()


def test_download_cancelled_exception_type():
    """
    Verifies yt-dlp DownloadCancelled exception can be raised and caught as expected.
    """
    import threading

    cancel_event = threading.Event()
    cancel_event.set()

    def dummy_hook(d):
        if cancel_event.is_set():
            raise yt_dlp.utils.DownloadCancelled("Job cancelled by user")

    with pytest.raises(yt_dlp.utils.DownloadCancelled, match="Job cancelled by user"):
        dummy_hook({"status": "downloading"})
