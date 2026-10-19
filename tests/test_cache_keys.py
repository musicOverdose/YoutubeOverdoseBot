import pytest
from src.services.cache_service import CacheService


def test_cache_keys_determinism_and_uniqueness():
    source_id = "ABC123XYZ"

    # Determinism
    k1 = CacheService.generate_cache_key(source_id, "VIDEO", codec="H264", height=1080)
    k2 = CacheService.generate_cache_key(source_id, "VIDEO", codec="H264", height=1080)
    assert k1 == k2

    # Different Codecs must not collide
    k_h264 = CacheService.generate_cache_key(source_id, "VIDEO", codec="H264", height=1080)
    k_h265 = CacheService.generate_cache_key(source_id, "VIDEO", codec="H265", height=1080)
    assert k_h264 != k_h265

    # Different Resolutions must not collide
    k_1080 = CacheService.generate_cache_key(source_id, "VIDEO", codec="H264", height=1080)
    k_720 = CacheService.generate_cache_key(source_id, "VIDEO", codec="H264", height=720)
    assert k_1080 != k_720

    # Video vs MP3
    k_mp3 = CacheService.generate_cache_key(source_id, "AUDIO", codec="MP3")
    assert k_mp3 != k_1080

    # Subtitles: English vs Persian
    k_sub_en = CacheService.generate_cache_key(source_id, "SUBTITLE", subtitle_lang="EN")
    k_sub_fa = CacheService.generate_cache_key(source_id, "SUBTITLE", subtitle_lang="FA")
    assert k_sub_en != k_sub_fa


@pytest.mark.asyncio
async def test_save_cache_entry_upsert_on_invalidated_cache(db_session):
    """
    Verifies that calling save_cache_entry on a pre-existing cache_key (e.g. an invalidated entry)
    updates the record in-place instead of raising UniqueViolationError.
    """
    cache_key = "test_cache_key_upsert_123"
    source_id = "test_vid_1"

    # 1. Initial save
    entry1 = await CacheService.save_cache_entry(
        session=db_session,
        cache_key=cache_key,
        source_id=source_id,
        title="Test Title",
        operation="VIDEO",
        telegram_channel_id=-1001234567,
        telegram_message_id=10,
        codec="H264",
        resolution="720p",
        height=720,
        file_size=1000,
    )
    assert entry1.id is not None
    assert entry1.is_valid is True
    assert entry1.telegram_message_id == 10

    # 2. Invalidate cache (as happens when invalidating cache or deleted TG message)
    entry1.is_valid = False
    await db_session.commit()

    # 3. Save again with new message ID (re-download)
    entry2 = await CacheService.save_cache_entry(
        session=db_session,
        cache_key=cache_key,
        source_id=source_id,
        title="Test Title Updated",
        operation="VIDEO",
        telegram_channel_id=-1001234567,
        telegram_message_id=25,
        codec="H264",
        resolution="720p",
        height=720,
        file_size=1200,
    )

    # Must update the existing row, not create a duplicate
    assert entry2.id == entry1.id
    assert entry2.is_valid is True
    assert entry2.telegram_message_id == 25
    assert entry2.file_size == 1200
    assert entry2.title == "Test Title Updated"

