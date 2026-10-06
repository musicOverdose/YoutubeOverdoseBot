from typing import Any, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.constants import UserRole, UserStatus
from src.core.database import get_db
from src.models.user import User
from src.services.audit_service import AuditService
from src.web.auth import get_current_admin

router = APIRouter(prefix="/api/users", tags=["Users"])


class StatusRequest(BaseModel):
    status: str  # ACTIVE or BANNED


class RoleRequest(BaseModel):
    role: str  # USER or ADMIN


@router.get("")
async def list_users(
    search: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(default=50, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    query = select(User)
    count_query = select(func.count(User.id))

    if search:
        f = User.username.ilike(f"%{search}%") | User.first_name.ilike(f"%{search}%")
        query = query.where(f)
        count_query = count_query.where(f)

    if status:
        query = query.where(User.status == status)
        count_query = count_query.where(User.status == status)

    total = await session.scalar(count_query) or 0
    banned_count = await session.scalar(select(func.count(User.id)).where(User.status == UserStatus.BANNED.value)) or 0

    query = query.order_by(desc(User.last_seen_at)).offset(offset).limit(limit)
    res = await session.execute(query)
    users = res.scalars().all()

    items = []
    for u in users:
        items.append({
            "id": u.id,
            "username": u.username,
            "first_name": u.first_name,
            "role": u.role,
            "status": u.status,
            "total_jobs": u.total_jobs,
            "successful_jobs": u.successful_jobs,
            "failed_jobs": u.failed_jobs,
            "first_seen_at": u.first_seen_at.isoformat() if u.first_seen_at else None,
            "last_seen_at": u.last_seen_at.isoformat() if u.last_seen_at else None,
        })

    return {"total": total, "banned_count": banned_count, "items": items, "limit": limit, "offset": offset}


@router.post("/{user_id}/status")
async def update_user_status(
    user_id: int,
    req: StatusRequest,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    if req.status not in (UserStatus.ACTIVE.value, UserStatus.BANNED.value):
        raise HTTPException(status_code=400, detail="Invalid status")

    stmt = select(User).where(User.id == user_id)
    res = await session.execute(stmt)
    user = res.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.status = req.status
    await session.commit()
    await AuditService.log_action(session, "USER_BAN" if req.status == UserStatus.BANNED.value else "USER_UNBAN", admin["sub"], f"User {user_id} set to {req.status}")
    return {"status": "updated", "user_id": user_id, "new_status": req.status}


@router.post("/{user_id}/role")
async def update_user_role(
    user_id: int,
    req: RoleRequest,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    if req.role not in (UserRole.USER.value, UserRole.ADMIN.value):
        raise HTTPException(status_code=400, detail="Invalid role")

    stmt = select(User).where(User.id == user_id)
    res = await session.execute(stmt)
    user = res.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.role = req.role
    await session.commit()
    await AuditService.log_action(session, "USER_ROLE_CHANGE", admin["sub"], f"User {user_id} role set to {req.role}")
    return {"status": "updated", "user_id": user_id, "new_role": req.role}


@router.delete("/{identifier}")
async def delete_user(
    identifier: Optional[str] = None,
    user_id: Optional[Any] = None,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    """Permanently delete user and their associated records from the database by ID or username."""
    user = None
    target = str(identifier if identifier is not None else user_id or "").strip()
    clean_id = target.lstrip("@")
    if clean_id.isdigit():
        uid = int(clean_id)
        stmt = select(User).where(User.id == uid)
        res = await session.execute(stmt)
        user = res.scalar_one_or_none()

    if not user:
        stmt = select(User).where(func.lower(User.username) == clean_id.lower())
        res = await session.execute(stmt)
        user = res.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail=f"User '{identifier}' not found in database")

    user_id = user.id
    username = user.username or user.first_name or str(user.id)
    await session.delete(user)
    await session.commit()
    admin_user = admin.get("username") or admin.get("sub", "admin")
    await AuditService.log_action(
        session, "USER_DELETE", admin_user, f"Deleted user {user_id} (@{username})"
    )
    return {
        "status": "deleted",
        "user_id": user_id,
        "username": username,
        "message": f"User {username} ({user_id}) permanently deleted from database",
    }
