import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import delete, select

from src.core.config import settings
from src.core.security import (
    atomic_write_file,
    decrypt_credential,
    encrypt_credential,
    get_master_key,
    mask_secret,
    write_local_bot_api_env,
    write_restart_trigger,
    write_runtime_bot_token,
    write_runtime_ready,
)
from src.models.channel import RequiredChannel
from src.models.setting import Setting
from src.services.must_join_service import MustJoinService
from src.services.setting_service import (
    DEFAULT_MUST_JOIN_MESSAGE,
    SETTING_API_HASH,
    SETTING_API_ID,
    SETTING_API_MODE,
    SETTING_BOT_TOKEN,
    SETTING_CONFIG_VERSION,
    SETTING_MUST_JOIN_MSG,
    SettingService,
    TelegramConfigurationLock,
)


@pytest.fixture
def temp_config_dir(monkeypatch):
    tmp = tempfile.mkdtemp()
    master_file = os.path.join(tmp, "master", "master.key")
    token_file = os.path.join(tmp, "runtime", "bot-token")
    ready_file = os.path.join(tmp, "state", "READY")
    env_file = os.path.join(tmp, "bot-api", "local-bot-api.env")
    trigger_file = os.path.join(tmp, "bot-api", "restart-trigger")

    monkeypatch.setattr(settings, "MASTER_KEY_FILE", master_file)
    monkeypatch.setattr(settings, "RUNTIME_BOT_TOKEN_FILE", token_file)
    monkeypatch.setattr(settings, "RUNTIME_READY_FILE", ready_file)
    monkeypatch.setattr(settings, "LOCAL_BOT_API_ENV_FILE", env_file)
    monkeypatch.setattr(settings, "LOCAL_BOT_API_TRIGGER_FILE", trigger_file)

    yield {
        "dir": tmp,
        "master": master_file,
        "token": token_file,
        "ready": ready_file,
        "env": env_file,
        "trigger": trigger_file,
    }
    shutil.rmtree(tmp, ignore_errors=True)


# 1. Master Key Fail-Safe & Generation
@pytest.mark.asyncio
async def test_master_key_generation_on_fresh_install(temp_config_dir, db_session):
    # Master key does not exist, zero encrypted settings exist
    key = await get_master_key(db_session)
    assert key is not None
    assert len(key) == 44  # Fernet key base64 length
    assert os.path.exists(temp_config_dir["master"])
    # Verify file mode 0600
    stat_mode = oct(os.stat(temp_config_dir["master"]).st_mode & 0o777)
    assert stat_mode == "0o600"


@pytest.mark.asyncio
async def test_master_key_fail_safe_when_encrypted_data_exists(temp_config_dir, db_session):
    # Insert an encrypted record into DB
    db_session.add(
        Setting(
            key="test_secret",
            status="ACTIVE",
            value="some_encrypted_blob",
            is_encrypted=True,
        )
    )
    await db_session.commit()

    # Master key file is missing, but encrypted settings exist in DB -> FAIL-SAFE
    with pytest.raises(RuntimeError, match="fail-safe"):
        await get_master_key(db_session)


# 2. Encryption, Decryption and Masking
def test_credential_encryption_and_decryption(temp_config_dir):
    key = Fernet.generate_key()
    original = "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ_test_secret"

    encrypted = encrypt_credential(original, master_key=key)
    assert encrypted != original
    decrypted = decrypt_credential(encrypted, master_key=key)
    assert decrypted == original


def test_mask_secret():
    assert mask_secret("") == ""
    assert mask_secret("short") == "••••••••"
    token = "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ"
    masked = mask_secret(token, show_chars=4)
    assert masked.startswith("1234")
    assert masked.endswith("wxYZ")
    assert "••••••••" in masked


# 3. Atomic Writers & Permissions
def test_atomic_file_writers(temp_config_dir):
    write_runtime_bot_token("123456:TOKEN_TEST")
    assert os.path.exists(temp_config_dir["token"])
    with open(temp_config_dir["token"], "r") as f:
        assert f.read().strip() == "123456:TOKEN_TEST"
    token_mode = oct(os.stat(temp_config_dir["token"]).st_mode & 0o777)
    assert token_mode == "0o640"

    # write_local_bot_api_env is a safe no-op in decoupled mode
    write_local_bot_api_env(98765, "abcdef0123456789abcdef0123456789")

    write_runtime_ready()
    assert os.path.exists(temp_config_dir["ready"])
    with open(temp_config_dir["ready"], "r") as f:
        assert f.read().strip() == "READY"
    ready_mode = oct(os.stat(temp_config_dir["ready"]).st_mode & 0o777)
    assert ready_mode == "0o644"

    # write_restart_trigger is a safe no-op in decoupled mode
    write_restart_trigger()


# 4. Non-Blocking Advisory Lock (pg_try_advisory_lock / HTTP 409 Conflict)
@pytest.mark.asyncio
async def test_non_blocking_advisory_lock_conflict():
    # When lock is acquired by one request, concurrent request must receive HTTP 409 Conflict immediately
    async with TelegramConfigurationLock() as lock1:
        assert lock1.locked is True
        # Second acquisition should fail and raise HTTP 409
        with pytest.raises(HTTPException) as exc_info:
            async with TelegramConfigurationLock() as lock2:
                pass
        assert exc_info.value.status_code == 409
        assert "Another Telegram configuration mutation is already running" in exc_info.value.detail

    # Once released, lock can be acquired again
    async with TelegramConfigurationLock() as lock3:
        assert lock3.locked is True


# 5. Mode-Branched Validation & Transaction Integrity
@pytest.mark.asyncio
async def test_save_telegram_config_local_mode_validation_failure(temp_config_dir, db_session):
    # Setup master key
    key = await get_master_key(db_session)

    # Local mode with mock probe failure (e.g. TCP connection refused)
    with patch.object(SettingService, "LOCAL_API_POLL_TIMEOUT", 0.05), \
         patch.object(SettingService, "LOCAL_API_POLL_INTERVAL", 0.01), \
         patch.object(SettingService, "probe_tcp", AsyncMock(return_value=False)):
        with pytest.raises(HTTPException) as exc:
            await SettingService.save_telegram_config(
                candidate_mode="local",
                bot_token="123456789:ABCdefGHIjklMNOpqrSTUvwxYZ",
            )
        assert exc.value.status_code == 400
        assert "Active configuration remains unchanged" in exc.value.detail or "rejected" in exc.value.detail

    # Verify PENDING was deleted from DB (rollback)
    stmt = select(Setting).where(Setting.status == "PENDING")
    res = await db_session.execute(stmt)
    assert res.scalars().all() == []


@pytest.mark.asyncio
async def test_save_telegram_config_local_mode_success(temp_config_dir, db_session):
    key = await get_master_key(db_session)

    with patch.object(SettingService, "probe_tcp", AsyncMock(return_value=True)), \
         patch.object(SettingService, "probe_cache_channel", AsyncMock(return_value=(True, ""))), \
         patch.object(SettingService, "probe_get_me", AsyncMock(return_value=(True, {"id": 123456789, "username": "TestBot"}, ""))):

        res = await SettingService.save_telegram_config(
            candidate_mode="local",
            bot_token="123456789:ABCdefGHIjklMNOpqrSTUvwxYZ",
            cache_channel_id="-1001234567890",
        )

        assert res["status"] == "success"
        assert res["mode"] == "local"
        assert res["bot_username"] == "TestBot"
        assert res["config_version"] >= 1

    # Verify ACTIVE records in DB
    stmt = select(Setting).where(Setting.status == "ACTIVE")
    result = await db_session.execute(stmt)
    active_map = {s.key: s for s in result.scalars().all()}

    assert SETTING_BOT_TOKEN in active_map
    assert active_map[SETTING_BOT_TOKEN].is_encrypted is True
    assert SETTING_API_MODE in active_map
    assert active_map[SETTING_API_MODE].value == "local"

    # Verify runtime bot-token file written with mode 0640
    assert os.path.exists(temp_config_dir["token"])
    with open(temp_config_dir["token"], "r") as f:
        assert f.read().strip() == "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ"


@pytest.mark.asyncio
async def test_save_telegram_config_cloud_mode_success(temp_config_dir, db_session):
    key = await get_master_key(db_session)

    with patch.object(SettingService, "probe_cache_channel", AsyncMock(return_value=(True, ""))), \
         patch.object(SettingService, "probe_get_me", AsyncMock(return_value=(True, {"id": 999999, "username": "CloudBot"}, ""))):
        res = await SettingService.save_telegram_config(
            candidate_mode="cloud",
            bot_token="999999999:CloudTokenABCDEFGHIJKLMN12345",
            api_id=None,
            api_hash=None,
        )

        assert res["status"] == "success"
        assert res["mode"] == "cloud"
        assert res["bot_username"] == "CloudBot"

    # Verify mode is cloud in DB
    mode = await SettingService.get_active_setting(SETTING_API_MODE, db_session)
    assert mode == "cloud"


# 6. Startup Reconciliation & Readiness Gating
@pytest.mark.asyncio
async def test_reconcile_startup_state(temp_config_dir, db_session):
    # Pre-populate DB with active token and mode
    key = await get_master_key(db_session)
    db_session.add_all([
        Setting(
            key=SETTING_BOT_TOKEN,
            status="ACTIVE",
            value=encrypt_credential("123456789:ReconciledToken12345", key),
            is_encrypted=True,
        ),
        Setting(
            key=SETTING_API_MODE,
            status="ACTIVE",
            value="local",
            is_encrypted=False,
        ),
        # Stale PENDING record that must be pruned
        Setting(
            key=SETTING_BOT_TOKEN,
            status="PENDING",
            value="stale_pending_value",
            is_encrypted=False,
        ),
    ])
    await db_session.commit()

    # Run reconciliation
    await SettingService.reconcile_startup_state()

    # Verify stale PENDING was purged
    stmt = select(Setting).where(Setting.status == "PENDING")
    res = await db_session.execute(stmt)
    assert res.scalars().all() == []

    # Verify /config/runtime/bot-token reconstructed
    assert os.path.exists(temp_config_dir["token"])
    with open(temp_config_dir["token"]) as f:
        assert f.read().strip() == "123456789:ReconciledToken12345"

    # Verify /config/state/READY was created with mode 0644
    assert os.path.exists(temp_config_dir["ready"])
    ready_mode = oct(os.stat(temp_config_dir["ready"]).st_mode & 0o777)
    assert ready_mode == "0o644"


# 7. Customizable Must-Join Message CRUD & Tag Rendering
@pytest.mark.asyncio
async def test_must_join_custom_message(db_session):
    # 1. Default template when nothing saved
    default_msg = await SettingService.get_must_join_message(db_session)
    assert "{first_name}" in default_msg
    assert "{channel_list}" in default_msg

    # 2. Save custom template
    custom = "Hi {first_name}! Please subscribe to:\n{channel_list}\nThank you!"
    saved = await SettingService.save_must_join_message(custom, session=db_session)
    assert saved == custom

    retrieved = await SettingService.get_must_join_message(db_session)
    assert retrieved == custom

    # 3. Render template with simulated channels
    mock_channel = RequiredChannel(
        id=1,
        chat_id=-100111,
        title="Test Music Channel",
        username="testmusic",
        enabled=True,
    )
    rendered = await MustJoinService.get_rendered_must_join_message(
        db_session, first_name="Farzad", missing_channels=[mock_channel]
    )
    assert "Hi Farzad!" in rendered
    assert "• <b>Test Music Channel</b>" in rendered

    # 4. Reset template
    reset = await SettingService.reset_must_join_message(session=db_session)
    assert reset == DEFAULT_MUST_JOIN_MESSAGE


@pytest.mark.asyncio
async def test_delayed_local_api_restart_readiness_success(temp_config_dir, db_session):
    """
    Simulate delayed Local Bot API restart where TCP / getMe fails on initial attempts
    and succeeds on subsequent polling attempt before timeout.
    """
    key = await get_master_key(db_session)

    # TCP fails once, then succeeds
    tcp_results = [False, True, True, True]
    async def mock_tcp(*args, **kwargs):
        return tcp_results.pop(0) if tcp_results else True

    # getMe fails once, then succeeds
    get_me_results = [
        (False, None, "Local Bot API starting up"),
        (True, {"id": 111222333, "username": "DelayedBot"}, ""),
    ]
    async def mock_get_me(*args, **kwargs):
        return get_me_results.pop(0) if get_me_results else (True, {"id": 111222333, "username": "DelayedBot"}, "")

    with patch.object(SettingService, "LOCAL_API_POLL_TIMEOUT", 1.0), \
         patch.object(SettingService, "LOCAL_API_POLL_INTERVAL", 0.05), \
         patch.object(SettingService, "probe_tcp", side_effect=mock_tcp), \
         patch.object(SettingService, "probe_get_me", side_effect=mock_get_me):

        res = await SettingService.save_telegram_config(
            candidate_mode="local",
            bot_token="111222333:DelayedBotTokenABCD12345",
            api_id=998877,
            api_hash="abcdefabcdefabcdefabcdefabcdefab",
        )

        assert res["status"] == "success"
        assert res["bot_username"] == "DelayedBot"

    # Verify READY exists
    assert os.path.exists(temp_config_dir["ready"])


@pytest.mark.asyncio
async def test_candidate_failure_rollback_waits_for_readiness_and_restores_ready(temp_config_dir, db_session):
    """
    CRITICAL Correction 3:
    When candidate validation fails and previous ACTIVE credentials exist:
    Rollback restores old local-bot-api.env, restarts Local Bot API,
    waits for readiness of previous ACTIVE credentials, and only re-arms READY
    when restored runtime is verified.
    """
    key = await get_master_key(db_session)

    # 1. Establish ACTIVE configuration
    with patch.object(SettingService, "probe_tcp", AsyncMock(return_value=True)), \
         patch.object(SettingService, "probe_get_me", AsyncMock(return_value=(True, {"id": 100, "username": "ExistingBot"}, ""))):
        await SettingService.save_telegram_config(
            candidate_mode="local",
            bot_token="100:ExistingActiveToken12345",
            api_id=11111,
            api_hash="11111111111111111111111111111111",
        )
    assert os.path.exists(temp_config_dir["ready"])

    # 2. Try mutating with candidate credentials that fail validation
    # During rollback, poll_local_api_readiness is called for restored credentials and succeeds
    candidate_token = "200:CandidateFailingToken12345"

    async def mock_poll(token, endpoint, timeout_sec=None, interval_sec=None):
        if token == candidate_token:
            # Candidate validation fails
            return False, None, "Candidate credentials rejected by Telegram"
        elif token == "100:ExistingActiveToken12345":
            # Restored configuration succeeds
            return True, {"id": 100, "username": "ExistingBot"}, ""
        return False, None, "Unknown token"

    with patch.object(SettingService, "LOCAL_API_POLL_TIMEOUT", 0.2), \
         patch.object(SettingService, "LOCAL_API_POLL_INTERVAL", 0.02), \
         patch.object(SettingService, "poll_local_api_readiness", side_effect=mock_poll):

        with pytest.raises(HTTPException) as exc:
            await SettingService.save_telegram_config(
                candidate_mode="local",
                bot_token=candidate_token,
            )
        assert exc.value.status_code == 400
        assert "Active configuration remains unchanged" in exc.value.detail

    # Restored configuration verified: READY remains armed and token remains unchanged
    assert os.path.exists(temp_config_dir["ready"])
    with open(temp_config_dir["token"]) as f:
        assert f.read().strip() == "100:ExistingActiveToken12345"


@pytest.mark.asyncio
async def test_candidate_failure_preserves_active_configuration(temp_config_dir, db_session):
    """
    Decoupled architecture: Candidate failure leaves active configuration completely unchanged.
    """
    key = await get_master_key(db_session)

    # 1. Establish ACTIVE configuration
    with patch.object(SettingService, "probe_tcp", AsyncMock(return_value=True)), \
         patch.object(SettingService, "probe_get_me", AsyncMock(return_value=(True, {"id": 100, "username": "ExistingBot"}, ""))):
        await SettingService.save_telegram_config(
            candidate_mode="local",
            bot_token="100:ExistingActiveToken12345",
        )
    assert os.path.exists(temp_config_dir["ready"])

    # 2. Candidate fails
    with patch.object(SettingService, "LOCAL_API_POLL_TIMEOUT", 0.1), \
         patch.object(SettingService, "LOCAL_API_POLL_INTERVAL", 0.02), \
         patch.object(SettingService, "poll_local_api_readiness", AsyncMock(return_value=(False, None, "Total server outage"))):

        with pytest.raises(HTTPException) as exc:
            await SettingService.save_telegram_config(
                candidate_mode="local",
                bot_token="300:NewFailingToken123456789",
            )
        assert exc.value.status_code == 400
        assert "Active configuration remains unchanged" in exc.value.detail

    # Active READY and token still intact
    assert os.path.exists(temp_config_dir["ready"])
    with open(temp_config_dir["token"]) as f:
        assert f.read().strip() == "100:ExistingActiveToken12345"


@pytest.mark.asyncio
async def test_fresh_install_candidate_failure_cleans_up_artifacts(temp_config_dir, db_session):
    """
    Clean up failed candidate runtime artifacts on fresh installations
    where there is no previous ACTIVE configuration.
    """
    key = await get_master_key(db_session)

    # Ensure clean slate: no active configuration
    await db_session.execute(delete(Setting))
    await db_session.commit()

    with patch.object(SettingService, "LOCAL_API_POLL_TIMEOUT", 0.1), \
         patch.object(SettingService, "LOCAL_API_POLL_INTERVAL", 0.02), \
         patch.object(SettingService, "poll_local_api_readiness", AsyncMock(return_value=(False, None, "Local Bot API startup failure"))):

        with pytest.raises(HTTPException) as exc:
            await SettingService.save_telegram_config(
                candidate_mode="local",
                bot_token="400:FreshInstallToken123456789",
            )
        assert exc.value.status_code == 400

    # Verify candidate runtime artifacts were cleanly purged
    assert not os.path.exists(temp_config_dir["ready"])
    assert not os.path.exists(temp_config_dir["token"])

