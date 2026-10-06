from typing import Any, Dict, Optional, Union
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.database import get_db
from src.services.setting_service import SettingService
from src.web.auth import get_current_admin

router = APIRouter(prefix="/api/telegram", tags=["Telegram"])


class TelegramConfigRequest(BaseModel):
    mode: str = "cloud"
    bot_token: Optional[str] = None
    api_id: Optional[Union[int, str]] = None  # Deprecated / ignored
    api_hash: Optional[str] = None  # Deprecated / ignored
    cache_channel_id: Optional[Union[int, str]] = None


class TestTokenRequest(BaseModel):
    bot_token: Optional[str] = None
    mode: str = "cloud"


class TestChannelRequest(BaseModel):
    channel_id: Union[int, str]
    bot_token: Optional[str] = None
    mode: str = "cloud"


class MigrationRequest(BaseModel):
    target_mode: str


class WelcomeMessageRequest(BaseModel):
    message: str


@router.get("/config")
async def get_telegram_config(
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    """Retrieve sanitized Telegram configuration for Web Admin."""
    return await SettingService.get_telegram_config(session)


@router.post("/test-token")
async def test_bot_token(
    req: TestTokenRequest,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    """Probe Telegram getMe against derived endpoint."""
    token = req.bot_token
    if not token or not token.strip():
        # Fall back to active setting
        active_config = await SettingService.get_all_active(session)
        token = active_config.get("telegram_bot_token")

    if not token:
        raise HTTPException(status_code=400, detail="Bot token is required to test connectivity.")

    endpoint = SettingService.derive_endpoint(req.mode)
    ok, bot_info, err = await SettingService.probe_get_me(token, endpoint)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Token verification failed: {err}")

    return {
        "valid": True,
        "endpoint": endpoint,
        "bot_id": bot_info.get("id"),
        "username": bot_info.get("username"),
        "first_name": bot_info.get("first_name"),
    }


@router.post("/test-local-api")
async def test_local_bot_api(admin: dict = Depends(get_current_admin)):
    """Probe TCP connectivity to Local Bot API container on port 8081."""
    endpoint = SettingService.derive_endpoint("local")
    host = "telegram-bot-api"
    port = 8081
    try:
        base_clean = endpoint.split("://")[-1]
        parts = base_clean.split(":")
        host = parts[0]
        if len(parts) > 1:
            port = int(parts[1].split("/")[0])
    except Exception:
        pass

    connected = await SettingService.probe_tcp(host, port, timeout_sec=3.0)
    if not connected:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot reach Local Bot API at {host}:{port}. Ensure the centralized Local Bot API server is running on the 'telegram-bots' network.",
        )
    return {"connected": True, "host": host, "port": port}


@router.post("/test-channel")
async def test_cache_channel(
    req: TestChannelRequest,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    """Probe access to the specified cache channel."""
    try:
        cid = int(req.channel_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid cache channel ID format.")

    token = req.bot_token
    if not token or not token.strip():
        active_config = await SettingService.get_all_active(session)
        token = active_config.get("telegram_bot_token")

    if not token:
        raise HTTPException(status_code=400, detail="Bot token is required to test cache channel.")

    endpoint = SettingService.derive_endpoint(req.mode)
    ok, err = await SettingService.probe_cache_channel(token, endpoint, cid)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Cache channel verification failed: {err}")

    return {"valid": True, "channel_id": cid}


@router.post("/save")
async def save_telegram_config(
    req: TelegramConfigRequest,
    request: Request,
    admin: dict = Depends(get_current_admin),
):
    """
    Save Telegram configuration with non-blocking advisory locking.
    Concurrent mutation requests immediately receive HTTP 409 Conflict.
    """
    ip = request.client.host if request.client else None
    return await SettingService.save_telegram_config(
        candidate_mode=req.mode,
        bot_token=req.bot_token,
        api_id=req.api_id,
        api_hash=req.api_hash,
        cache_channel_id=req.cache_channel_id,
        admin_username=admin.get("sub", "admin"),
        ip_address=ip,
    )


@router.post("/migrate")
async def migrate_telegram_mode(
    req: MigrationRequest,
    request: Request,
    admin: dict = Depends(get_current_admin),
):
    """Explicitly migrate between Local and Cloud Bot API modes."""
    ip = request.client.host if request.client else None
    return await SettingService.migrate_mode(
        target_mode=req.target_mode,
        admin_username=admin.get("sub", "admin"),
        ip_address=ip,
    )


@router.get("/welcome-message")
async def get_welcome_message(
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    """Retrieve the current active /start welcome message template."""
    msg = await SettingService.get_welcome_message(session)
    return {"message": msg}


@router.post("/welcome-message")
async def save_welcome_message(
    req: WelcomeMessageRequest,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    """Save a validated custom /start welcome message template."""
    msg = await SettingService.save_welcome_message(
        message=req.message,
        session=session,
        admin_username=admin.get("sub", "admin"),
    )
    return {"message": msg, "status": "saved"}


@router.post("/welcome-message/reset")
async def reset_welcome_message(
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    """Reset the /start welcome message template to default."""
    msg = await SettingService.reset_welcome_message(
        session=session,
        admin_username=admin.get("sub", "admin"),
    )
    return {"message": msg, "status": "reset"}

