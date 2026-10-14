from datetime import datetime, timezone
from typing import Optional, Union
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.bot.bot_instance import get_bot
from src.core.config import settings
from src.core.database import get_db
from src.core.logger import setup_logger
from src.models.channel import RequiredChannel
from src.services.audit_service import AuditService
from src.services.must_join_service import MustJoinService
from src.web.auth import get_current_admin

logger = setup_logger("must_join_routes")
router = APIRouter(prefix="/api/must-join", tags=["Must Join"])


class AddChannelRequest(BaseModel):
    chat_id: Union[int, str]
    title: Optional[str] = None
    username: Optional[str] = None
    invite_url: Optional[str] = None
    enabled: bool = True


class UpdateChannelRequest(BaseModel):
    title: str
    username: Optional[str] = None
    invite_url: Optional[str] = None
    enabled: bool = True


class ToggleRequest(BaseModel):
    enabled: bool


@router.get("")
async def get_must_join_info(
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    stmt = select(RequiredChannel).order_by(RequiredChannel.created_at.asc())
    res = await session.execute(stmt)
    channels = res.scalars().all()

    from src.services.setting_service import SettingService
    exempt_users = await SettingService.get_must_join_exempt_users(session)

    return {
        "enabled": settings.MUST_JOIN_ENABLED,
        "exempt_users": exempt_users,
        "channels": [
            {
                "id": c.id,
                "chat_id": c.chat_id,
                "title": c.title,
                "username": c.username,
                "invite_url": c.invite_url,
                "enabled": c.enabled,
                "bot_status": c.bot_status,
                "last_bot_check": c.last_bot_check.isoformat() if c.last_bot_check else None,
                "last_error": c.last_error,
            }
            for c in channels
        ],
    }


from src.services.setting_service import SETTING_MUST_JOIN_ENABLED, SettingService


@router.post("/toggle")
async def toggle_must_join(
    req: ToggleRequest,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    await SettingService.save_single_setting(
        SETTING_MUST_JOIN_ENABLED, req.enabled, description="Must Join System Enabled", session=session
    )
    await MustJoinService.invalidate_channels_cache()
    await AuditService.log_action(
        session, "SETTING_UPDATE", admin["sub"], f"Must Join system toggled to {req.enabled}"
    )
    return {"status": "ok", "must_join_enabled": settings.MUST_JOIN_ENABLED}


@router.post("/channels")
async def add_channel(
    req: AddChannelRequest,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    bot = get_bot()

    # Parse chat_id identifier (numeric ID or @username)
    raw_target = str(req.chat_id).strip()
    target_id: Union[int, str]
    if raw_target.startswith("@"):
        target_id = raw_target
    else:
        try:
            target_id = int(raw_target)
        except ValueError:
            target_id = raw_target

    chat_obj = None
    if bot:
        try:
            chat_obj = await bot.get_chat(chat_id=target_id)
        except Exception as e:
            logger.warning("Could not fetch chat info for %s: %s", target_id, e)

    numeric_chat_id = getattr(chat_obj, "id", None)
    if numeric_chat_id is None:
        if isinstance(target_id, int):
            numeric_chat_id = target_id
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Could not resolve Telegram chat for '{target_id}'. Make sure the bot is added to the channel as an administrator.",
            )

    is_admin = True
    status_detail = "Bot is administrator"
    if bot:
        is_admin, status_detail = await MustJoinService.verify_bot_is_admin(bot, numeric_chat_id)
        if not is_admin:
            raise HTTPException(
                status_code=400,
                detail=f"❌ Bot is not an administrator in this channel ({numeric_chat_id}): {status_detail}",
            )

    # Auto-resolve metadata
    title = (req.title.strip() if req.title and req.title.strip() else None)
    if not title:
        title = (getattr(chat_obj, "title", None) or str(numeric_chat_id))

    username = (req.username.lstrip("@").strip() if req.username and req.username.strip() else None)
    if not username and getattr(chat_obj, "username", None):
        username = chat_obj.username

    invite_url = (req.invite_url.strip() if req.invite_url and req.invite_url.strip() else None)
    if not invite_url:
        if getattr(chat_obj, "invite_link", None):
            invite_url = chat_obj.invite_link
        elif username:
            invite_url = f"https://t.me/{username}"
        elif bot:
            try:
                invite_url = await bot.export_chat_invite_link(numeric_chat_id)
            except Exception:
                try:
                    created_link = await bot.create_chat_invite_link(numeric_chat_id)
                    invite_url = created_link.invite_link
                except Exception:
                    pass

    # Check for existing
    stmt = select(RequiredChannel).where(RequiredChannel.chat_id == numeric_chat_id)
    res = await session.execute(stmt)
    existing = res.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail=f"Channel with Chat ID {numeric_chat_id} already exists")

    ch = RequiredChannel(
        chat_id=numeric_chat_id,
        title=title,
        username=username,
        invite_url=invite_url,
        enabled=req.enabled,
        bot_status="administrator" if is_admin else "not_admin",
        last_bot_check=datetime.now(timezone.utc),
    )
    session.add(ch)
    await session.commit()
    await session.refresh(ch)

    await MustJoinService.invalidate_channels_cache()

    await AuditService.log_action(
        session, "CHANNEL_ADD", admin["sub"], f"Added required channel {title} ({numeric_chat_id})"
    )
    return {
        "status": "added",
        "channel_id": ch.id,
        "title": ch.title,
        "chat_id": ch.chat_id,
        "username": ch.username,
        "invite_url": ch.invite_url,
    }


@router.put("/channels/{channel_id}")
async def update_channel(
    channel_id: int,
    req: UpdateChannelRequest,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    stmt = select(RequiredChannel).where(RequiredChannel.id == channel_id)
    res = await session.execute(stmt)
    ch = res.scalar_one_or_none()
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")

    ch.title = req.title
    ch.username = req.username.lstrip("@") if req.username else None
    ch.invite_url = req.invite_url
    ch.enabled = req.enabled
    await session.commit()

    await MustJoinService.invalidate_channels_cache()

    await AuditService.log_action(
        session, "CHANNEL_UPDATE", admin["sub"], f"Updated required channel {ch.id}"
    )
    return {"status": "updated"}


@router.delete("/channels/{channel_id}")
async def delete_channel(
    channel_id: int,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    stmt = select(RequiredChannel).where(RequiredChannel.id == channel_id)
    res = await session.execute(stmt)
    ch = res.scalar_one_or_none()
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")

    await session.delete(ch)
    await session.commit()

    await MustJoinService.invalidate_channels_cache()

    await AuditService.log_action(
        session, "CHANNEL_REMOVE", admin["sub"], f"Removed required channel {channel_id}"
    )
    return {"status": "deleted"}


@router.post("/channels/{channel_id}/test")
async def test_channel_bot_status(
    channel_id: int,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    stmt = select(RequiredChannel).where(RequiredChannel.id == channel_id)
    res = await session.execute(stmt)
    ch = res.scalar_one_or_none()
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")

    bot = get_bot()
    is_admin, status_detail = await MustJoinService.verify_bot_is_admin(bot, ch.chat_id)
    ch.bot_status = "administrator" if is_admin else "not_admin"
    ch.last_bot_check = datetime.now(timezone.utc)
    ch.last_error = None if is_admin else status_detail
    await session.commit()

    return {
        "is_admin": is_admin,
        "detail": status_detail,
        "bot_status": ch.bot_status,
    }


@router.post("/channels/{channel_id}/sync")
async def sync_channel_info(
    channel_id: int,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    stmt = select(RequiredChannel).where(RequiredChannel.id == channel_id)
    res = await session.execute(stmt)
    ch = res.scalar_one_or_none()
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")

    bot = get_bot()
    chat_obj = None
    if bot:
        try:
            chat_obj = await bot.get_chat(chat_id=ch.chat_id)
            if getattr(chat_obj, "title", None):
                ch.title = chat_obj.title
            if getattr(chat_obj, "username", None):
                ch.username = chat_obj.username
            if getattr(chat_obj, "invite_link", None):
                ch.invite_url = chat_obj.invite_link
            elif getattr(chat_obj, "username", None) and not ch.invite_url:
                ch.invite_url = f"https://t.me/{chat_obj.username}"
        except Exception as e:
            logger.warning("Could not sync chat info for %s: %s", ch.chat_id, e)

    is_admin = True
    status_detail = "Bot is administrator"
    if bot:
        is_admin, status_detail = await MustJoinService.verify_bot_is_admin(bot, ch.chat_id)
        ch.bot_status = "administrator" if is_admin else "not_admin"
        ch.last_bot_check = datetime.now(timezone.utc)
        ch.last_error = None if is_admin else status_detail

    await session.commit()
    await session.refresh(ch)

    await AuditService.log_action(
        session, "CHANNEL_SYNC", admin["sub"], f"Synchronized metadata for required channel {ch.title} ({ch.chat_id})"
    )

    return {
        "status": "synced",
        "title": ch.title,
        "username": ch.username,
        "invite_url": ch.invite_url,
        "is_admin": is_admin,
        "bot_status": ch.bot_status,
    }


class MustJoinMessageRequest(BaseModel):
    message: str


@router.get("/message")
async def get_must_join_message(
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    from src.services.setting_service import DEFAULT_MUST_JOIN_MESSAGE, SettingService
    msg = await SettingService.get_must_join_message(session)
    return {
        "message": msg,
        "is_default": (msg == DEFAULT_MUST_JOIN_MESSAGE),
        "default_message": DEFAULT_MUST_JOIN_MESSAGE,
    }


@router.post("/message")
async def update_must_join_message(
    req: MustJoinMessageRequest,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    from src.services.setting_service import SettingService
    saved_msg = await SettingService.save_must_join_message(
        req.message, session=session, admin_username=admin.get("sub", "admin")
    )
    await AuditService.log_action(
        session, "SETTING_UPDATE", admin["sub"], "Updated custom Must-Join message template"
    )
    return {"status": "saved", "message": saved_msg}


@router.post("/message/reset")
async def reset_must_join_message(
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    from src.services.setting_service import SettingService
    reset_msg = await SettingService.reset_must_join_message(
        session=session, admin_username=admin.get("sub", "admin")
    )
    await AuditService.log_action(
        session, "SETTING_UPDATE", admin["sub"], "Reset Must-Join message template to default"
    )
    return {"status": "reset", "message": reset_msg}


class ExemptUsersRequest(BaseModel):
    exempt_users: str


@router.get("/exempt-users")
async def get_must_join_exempt_users(
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    from src.services.setting_service import SettingService
    val = await SettingService.get_must_join_exempt_users(session)
    return {"exempt_users": val}


@router.post("/exempt-users")
async def save_must_join_exempt_users(
    req: ExemptUsersRequest,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    from src.services.setting_service import SettingService
    saved = await SettingService.save_must_join_exempt_users(req.exempt_users, session=session)
    await AuditService.log_action(
        session, "SETTING_UPDATE", admin["sub"], f"Updated Must-Join exempt users list: {saved[:100]}"
    )
    return {"status": "saved", "exempt_users": saved}

