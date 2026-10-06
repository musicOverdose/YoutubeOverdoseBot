import os
import shutil
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from src.core.config import settings
from src.services.system_service import SystemService
from src.worker.processor import JobProcessor


@pytest.fixture
def telemetry_env(monkeypatch):
    tmp = tempfile.mkdtemp()
    temp_dir = os.path.join(tmp, "ytdl")
    transfer_dir = os.path.join(tmp, "transfer")
    os.makedirs(temp_dir, exist_ok=True)
    os.makedirs(transfer_dir, exist_ok=True)

    monkeypatch.setattr(settings, "TEMP_DIR", temp_dir)
    monkeypatch.setattr(settings, "TRANSFER_DIR", transfer_dir)

    yield {"dir": tmp, "temp": temp_dir, "transfer": transfer_dir}
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.mark.asyncio
async def test_worker_publishes_disk_telemetry_to_redis(telemetry_env):
    processor = JobProcessor()

    # Create dummy files
    f1 = os.path.join(telemetry_env["temp"], "dummy1.bin")
    with open(f1, "wb") as f:
        f.write(b"0" * 1024 * 1024)  # 1 MB

    f2 = os.path.join(telemetry_env["transfer"], "dummy2.bin")
    with open(f2, "wb") as f:
        f.write(b"0" * (2 * 1024 * 1024))  # 2 MB

    # Publish telemetry
    await processor.report_telemetry()

    # Verify Redis keys
    from src.core.redis import get_redis_client
    r = get_redis_client()

    worker_temp = await r.get("telemetry:worker:temp_ytdl_bytes")
    worker_transfer = await r.get("telemetry:worker:transfer_bytes")
    host_free = await r.get("telemetry:host:filesystem_free_bytes")

    assert worker_temp is not None
    assert int(worker_temp) >= 1024 * 1024
    assert worker_transfer is not None
    assert int(worker_transfer) >= 2 * 1024 * 1024
    assert host_free is not None
    assert int(host_free) > 0


@pytest.mark.asyncio
async def test_system_service_aggregates_service_reported_telemetry():
    from src.core.redis import get_redis_client
    r = get_redis_client()

    # Simulate telemetry published by Worker, Local Bot API, and Host
    await r.set("telemetry:worker:temp_ytdl_bytes", str(100 * 1024 * 1024))  # 100 MB
    await r.set("telemetry:worker:transfer_bytes", str(200 * 1024 * 1024))   # 200 MB
    await r.set("telemetry:local_bot_api:data_bytes", str(500 * 1024 * 1024))  # 500 MB
    await r.set("telemetry:local_bot_api:temp_bytes", str(50 * 1024 * 1024))   # 50 MB
    await r.set("telemetry:host:filesystem_free_bytes", str(50 * 1024**3))     # 50 GB
    await r.set("telemetry:host:filesystem_total_bytes", str(80 * 1024**3))    # 80 GB
    await r.set("telemetry:host:filesystem_used_bytes", str(30 * 1024**3))     # 30 GB

    stats = await SystemService.get_system_stats()

    assert stats["worker_temp_gb"] == 0.098  # ~100MB
    assert stats["worker_transfer_gb"] == 0.195  # ~200MB
    assert stats["bot_api_data_gb"] == 0.488  # ~500MB
    assert stats["disk_free_gb"] == 50.0
    assert stats["disk_total_gb"] == 80.0

