from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from src.bot.bot_instance import get_bot
from src.core.database import get_db
from src.core.redis import get_redis_client
from src.core.logger import get_logger
from src.models.cache import CacheEntry
from src.services.audit_service import AuditService
from src.web.auth import get_current_admin

logger = get_logger("cache_routes")
router = APIRouter(prefix="/api/cache", tags=["Cache"])


@router.get("")
async def list_cache_entries(
    search: Optional[str] = None,
    operation: Optional[str] = None,
    limit: int = Query(default=50, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    query = select(CacheEntry)
    count_query = select(func.count(CacheEntry.id))

    if operation:
        query = query.where(CacheEntry.operation == operation)
        count_query = count_query.where(CacheEntry.operation == operation)
    if search:
        f = CacheEntry.title.ilike(f"%{search}%") | CacheEntry.source_id.ilike(f"%{search}%")
        query = query.where(f)
        count_query = count_query.where(f)

    total = await session.scalar(count_query) or 0
    query = query.order_by(desc(CacheEntry.created_at)).offset(offset).limit(limit)
    res = await session.execute(query)
    entries = res.scalars().all()

    items = []
    for c in entries:
        items.append({
            "id": c.id,
            "cache_key": c.cache_key,
            "source_id": c.source_id,
            "title": c.title,
            "operation": c.operation,
            "codec": c.codec,
            "resolution": c.resolution,
            "subtitle_lang": c.subtitle_lang,
            "file_size": c.file_size,
            "telegram_channel_id": c.telegram_channel_id,
            "telegram_message_id": c.telegram_message_id,
            "hit_count": c.hit_count,
            "is_valid": c.is_valid,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "last_used_at": c.last_used_at.isoformat() if c.last_used_at else None,
        })

    return {"total": total, "items": items, "limit": limit, "offset": offset}


@router.post("/{entry_id}/invalidate")
async def invalidate_cache(
    entry_id: int,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    stmt = select(CacheEntry).where(CacheEntry.id == entry_id)
    res = await session.execute(stmt)
    entry = res.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Cache entry not found")

    entry.is_valid = False
    await session.commit()
    await AuditService.log_action(session, "CACHE_INVALIDATE", admin["sub"], f"Invalidated cache {entry.cache_key}")
    return {"status": "invalidated", "cache_key": entry.cache_key}


@router.delete("/{entry_id}")
async def delete_cache_record(
    entry_id: int,
    delete_from_telegram: bool = False,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    stmt = select(CacheEntry).where(CacheEntry.id == entry_id)
    res = await session.execute(stmt)
    entry = res.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Cache entry not found")

    if delete_from_telegram:
        try:
            bot = get_bot()
            await bot.delete_message(
                chat_id=entry.telegram_channel_id,
                message_id=entry.telegram_message_id,
            )
        except Exception as e:
            pass

    await session.delete(entry)
    await session.commit()
    await AuditService.log_action(session, "CACHE_DELETE", admin["sub"], f"Deleted cache entry {entry.id}")
    return {"status": "deleted"}


@router.post("/clear")
async def clear_all_cache(
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    result = await session.execute(delete(CacheEntry))
    deleted_count = result.rowcount or 0
    await session.commit()

    redis_deleted = 0
    try:
        r = get_redis_client()
        meta_keys = [k async for k in r.scan_iter("ytdl:metadata:*")]
        sub_keys = [k async for k in r.scan_iter("ytdlp:subtitles:*")]
        all_keys = meta_keys + sub_keys
        if all_keys:
            redis_deleted = await r.delete(*all_keys)
    except Exception as e:
        logger.warning("Error clearing Redis cache keys: %s", e)

    await AuditService.log_action(
        session,
        "CACHE_CLEAR_ALL",
        admin["sub"],
        f"Cleared all cache ({deleted_count} DB entries, {redis_deleted} Redis keys)",
    )

    return {
        "status": "success",
        "deleted_db_entries": deleted_count,
        "deleted_redis_keys": redis_deleted,
        "message": f"Successfully deleted {deleted_count} cache entries and flushed Redis cache.",
    }
