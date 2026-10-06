import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from mutagen.id3 import ID3
from PIL import Image
from sqlalchemy import select

from src.core.constants import JobStatus, OperationType, REDIS_KEY_QUEUE, REDIS_KEY_ACTIVE_JOBS
from src.models.job import Job
from src.models.job_request import JobRequest
from src.models.user import User
from src.services.ffmpeg_service import FFmpegService
from src.services.queue_service import QueueService


# ==============================================================================
# 1. ID3v2.3 Tags & Audio Filename Generation
# ==============================================================================

def test_resolve_audio_metadata_with_rich_dict():
    info = {
        "title": "Song Title (Official Music Video)",
        "artist": "Famous Artist",
        "album": "Greatest Hits",
        "release_date": "20230501",
        "duration": 215,
    }
    title, artist, album, year, duration = FFmpegService.resolve_audio_metadata(info)
    assert title == "Song Title"
    assert artist == "Famous Artist"
    assert album == "Greatest Hits"
    assert year == "2023"
    assert duration == 215


def test_resolve_audio_metadata_fallbacks():
    info = {"title": "Unknown Song"}
    title, artist, album, year, duration = FFmpegService.resolve_audio_metadata(info)
    assert title == "Unknown Song"
    assert artist == "Unknown Artist"
    assert album == "Unknown Song - Single"
    assert len(year) == 4
    assert duration == 0


def test_generate_safe_audio_filename():
    assert FFmpegService.generate_safe_audio_filename("Queen", "Bohemian Rhapsody") == "Queen - Bohemian Rhapsody.mp3"
    assert FFmpegService.generate_safe_audio_filename("Unknown Artist", "Track 01") == "Track 01.mp3"
    # Invalid characters sanitized
    assert FFmpegService.generate_safe_audio_filename("AC/DC", "Who Made Who?") == "ACDC - Who Made Who.mp3"


def test_prepare_cover_image_and_id3_tags():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a dummy image
        img_path = os.path.join(tmpdir, "raw_cover.png")
        img = Image.new("RGB", (600, 400), color=(100, 150, 200))
        img.save(img_path)

        # Prepare square cover
        cover_out = os.path.join(tmpdir, "cover.jpg")
        FFmpegService.prepare_cover_image(img_path, cover_out)
        assert os.path.exists(cover_out)
        with Image.open(cover_out) as c_img:
            assert c_img.size == (500, 500)
            assert c_img.format == "JPEG"

        # Create a dummy mp3 file with valid MP3 frame header
        dummy_mp3 = os.path.join(tmpdir, "test.mp3")
        with open(dummy_mp3, "wb") as f:
            f.write(b"\xff\xfb\x90\x00" + b"\x00" * 1024)

        # Apply and verify ID3 tags
        success = FFmpegService.apply_and_verify_id3_tags(
            mp3_path=dummy_mp3,
            title="Tested Title",
            artist="Tested Artist",
            album="Tested Album",
            year="2024",
            cover_image_path=cover_out,
        )
        assert success is True

        # Read back tags with Mutagen
        tags = ID3(dummy_mp3)
        assert tags.get("TIT2").text[0] == "Tested Title"
        assert tags.get("TPE1").text[0] == "Tested Artist"
        assert tags.get("TALB").text[0] == "Tested Album"
        assert str(tags.get("TDRC").text[0]) == "2024"
        assert any(k.startswith("APIC") for k in tags.keys())


# ==============================================================================
# 2. Queue Clearing (Transaction-Safe, Active Preserved)
# ==============================================================================

@pytest.mark.asyncio
async def test_clear_queued_jobs_transaction_safe(db_session, monkeypatch):
    from tests.mock_redis import MockRedis
    mock_r = MockRedis()
    monkeypatch.setattr("src.services.queue_service.get_redis_client", lambda: mock_r)

    # 1. Create an active job and two queued jobs
    active_job = Job(
        id="job-active-1",
        source_url="https://youtube.com/watch?v=1",
        canonical_url="https://youtube.com/watch?v=1",
        source_id="src1",
        cache_key="key1",
        title="Active Video",
        operation=OperationType.VIDEO.value,
        status=JobStatus.DOWNLOADING.value,
    )
    queued_job_1 = Job(
        id="job-queued-1",
        source_url="https://youtube.com/watch?v=2",
        canonical_url="https://youtube.com/watch?v=2",
        source_id="src2",
        cache_key="key2",
        title="Queued Video 1",
        operation=OperationType.VIDEO.value,
        status=JobStatus.QUEUED.value,
    )
    queued_job_2 = Job(
        id="job-queued-2",
        source_url="https://youtube.com/watch?v=3",
        canonical_url="https://youtube.com/watch?v=3",
        source_id="src3",
        cache_key="key3",
        title="Queued Video 2",
        operation=OperationType.VIDEO.value,
        status=JobStatus.QUEUED.value,
    )
    db_session.add_all([active_job, queued_job_1, queued_job_2])
    await db_session.commit()

    # Populate Redis queues
    await mock_r.rpush(REDIS_KEY_QUEUE, "job-queued-1", "job-queued-2")
    await mock_r.sadd(REDIS_KEY_ACTIVE_JOBS, "job-active-1")

    # Clear queued jobs
    res = await QueueService.clear_queued_jobs(session=db_session, admin_username="admin")
    assert res["status"] == "cleared"
    assert res["count"] == 2

    # Verify active job is UNTOUCHED
    await db_session.refresh(active_job)
    assert active_job.status == JobStatus.DOWNLOADING.value

    # Verify queued jobs are CANCELLED in DB
    await db_session.refresh(queued_job_1)
    await db_session.refresh(queued_job_2)
    assert queued_job_1.status == JobStatus.CANCELLED.value
    assert queued_job_2.status == JobStatus.CANCELLED.value

    # Verify Redis queues drained
    q_len = await mock_r.llen(REDIS_KEY_QUEUE)
    assert q_len == 0
    active_members = await mock_r.smembers(REDIS_KEY_ACTIVE_JOBS)
    assert "job-active-1" in [m.decode() if isinstance(m, bytes) else m for m in active_members]


# ==============================================================================
# 3. User Deletion with Cascade
# ==============================================================================

@pytest.mark.asyncio
async def test_delete_user_cascade(db_session):
    from src.web.routes.user_routes import delete_user

    # Create user and associated job & job request
    user = User(
        id=999888,
        username="tobedeleted",
        first_name="To Be",
        status="ACTIVE",
    )
    db_session.add(user)
    await db_session.flush()

    job = Job(
        id="job-user-cascade",
        source_url="https://youtube.com/watch?v=cascade",
        canonical_url="https://youtube.com/watch?v=cascade",
        source_id="src_cascade",
        cache_key="key_cascade",
        title="Cascade Video",
        operation="VIDEO",
        status=JobStatus.COMPLETED.value,
    )
    db_session.add(job)
    await db_session.flush()

    req = JobRequest(
        job_id="job-user-cascade",
        user_id=999888,
        chat_id=999888,
        status_message_id=1,
    )
    db_session.add(req)
    await db_session.commit()

    # Call delete route handler by user_id
    res = await delete_user(user_id=999888, admin={"username": "admin"}, session=db_session)
    assert res["status"] == "deleted"
    assert res["user_id"] == 999888
    assert "deleted from database" in res["message"]

    # Verify user is deleted
    stmt = select(User).where(User.id == 999888)
    user_res = await db_session.execute(stmt)
    assert user_res.scalar_one_or_none() is None

    # Test delete by @username
    user2 = User(
        id=777666,
        username="targetuser",
        first_name="Target",
        status="ACTIVE",
    )
    db_session.add(user2)
    await db_session.commit()

    res2 = await delete_user(identifier="@targetuser", admin={"username": "admin"}, session=db_session)
    assert res2["status"] == "deleted"
    assert res2["user_id"] == 777666
    assert res2["username"] == "targetuser"


@pytest.mark.asyncio
async def test_clear_jobs_response_fields(db_session):
    from src.web.routes.job_routes import clear_jobs

    res = await clear_jobs(scope="finished", session=db_session, admin={"sub": "admin"})
    assert res["status"] == "cleared"
    assert "count" in res
    assert "deleted_jobs_count" in res
    assert res["count"] == res["deleted_jobs_count"]
    assert "Successfully cleared" in res["message"]

