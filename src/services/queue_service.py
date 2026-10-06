import json
from typing import Any, Dict, List, Optional, Tuple, Union
import redis.asyncio as aioredis
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.config import settings
from src.core.constants import (
    REDIS_KEY_ACTIVE_JOBS,
    REDIS_KEY_ACTIVE_SUBTITLE,
    REDIS_KEY_ACTIVE_VIDEO,
    REDIS_KEY_CACHE_LOCK_PREFIX,
    REDIS_KEY_CANCEL_PREFIX,
    REDIS_KEY_PROGRESS_PREFIX,
    REDIS_KEY_QUEUE,
    REDIS_KEY_QUEUE_PAUSED,
    REDIS_KEY_QUEUE_SUBTITLE,
    REDIS_KEY_QUEUE_VIDEO,
    JobStatus,
)
from src.core.logger import setup_logger
from src.core.redis import get_redis_client
from src.models.job import Job
from src.models.job_request import JobRequest

logger = setup_logger("queue_service")

# Atomic slot reservation Lua script
LUA_ACQUIRE_JOB = """
local is_paused = redis.call('GET', KEYS[3])
if is_paused == '1' then
    return nil
end

local active_count = redis.call('SCARD', KEYS[2])
local max_active = tonumber(ARGV[1])

if active_count >= max_active then
    return nil
end

local job_id = redis.call('LPOP', KEYS[1])
if job_id then
    redis.call('SADD', KEYS[2], job_id)
    return job_id
end

return nil
"""


class QueueService:
    @staticmethod
    def get_queue_key(queue_type: str = "VIDEO") -> str:
        """Returns the Redis list key for the given logical queue ('VIDEO' or 'SUBTITLE')."""
        if str(queue_type).upper() == "SUBTITLE":
            return REDIS_KEY_QUEUE_SUBTITLE
        return REDIS_KEY_QUEUE_VIDEO

    @staticmethod
    def get_active_key(queue_type: str = "VIDEO") -> str:
        """Returns the Redis active jobs set key for the given logical queue."""
        if str(queue_type).upper() == "SUBTITLE":
            return REDIS_KEY_ACTIVE_SUBTITLE
        return REDIS_KEY_ACTIVE_VIDEO

    @staticmethod
    async def is_paused() -> bool:
        r = get_redis_client()
        val = await r.get(REDIS_KEY_QUEUE_PAUSED)
        return val == "1"

    @staticmethod
    async def pause_queue() -> None:
        r = get_redis_client()
        await r.set(REDIS_KEY_QUEUE_PAUSED, "1")
        logger.info("Global queue has been PAUSED by admin")

    @staticmethod
    async def resume_queue() -> None:
        r = get_redis_client()
        await r.delete(REDIS_KEY_QUEUE_PAUSED)
        logger.info("Global queue has been RESUMED by admin")

    @classmethod
    async def push_job(cls, job_id: str, queue_type: str = "VIDEO") -> int:
        """
        Pushes a new job ID to the end of the specified FIFO queue (RPUSH).
        Also maps job_id -> queue_type in Redis for fast status and release lookups.
        Returns the derived 1-based queue position in that specific queue.
        """
        r = get_redis_client()
        q_key = cls.get_queue_key(queue_type)
        q_name = "SUBTITLE" if str(queue_type).upper() == "SUBTITLE" else "VIDEO"

        await r.rpush(q_key, job_id)
        # Store queue mapping with 24-hour expiration
        await r.set(f"ytdl:job_queue:{job_id}", q_name, ex=86400)

        # For backward compatibility with legacy single-queue consumers/tests
        if q_key != REDIS_KEY_QUEUE:
            await r.rpush(REDIS_KEY_QUEUE, job_id)

        queue_len = await r.llen(q_key)
        logger.info(f"Job {job_id} pushed to [{q_name}] queue ({q_key}). Current length: {queue_len}")
        return queue_len

    @classmethod
    async def acquire_next_job(
        cls,
        queue_type: Union[str, int] = "VIDEO",
        max_active_jobs: Optional[int] = None,
    ) -> Optional[str]:
        """
        Atomically checks queue pause state and active jobs limit for the requested queue.
        Supports both acquire_next_job("VIDEO", 1) and legacy acquire_next_job(max_active_jobs=1).
        """
        if isinstance(queue_type, int):
            max_active = queue_type
            q_type = "VIDEO"
        else:
            q_type = str(queue_type).upper()
            if max_active_jobs is not None:
                max_active = max_active_jobs
            else:
                max_active = (
                    getattr(settings, "MAX_ACTIVE_SUBTITLE_JOBS", 2)
                    if q_type == "SUBTITLE"
                    else getattr(settings, "MAX_ACTIVE_VIDEO_JOBS", 1)
                )

        q_key = cls.get_queue_key(q_type)
        act_key = cls.get_active_key(q_type)

        r = get_redis_client()
        job_id = await r.eval(
            LUA_ACQUIRE_JOB,
            3,
            q_key,
            act_key,
            REDIS_KEY_QUEUE_PAUSED,
            max_active,
        )

        if job_id:
            # Also keep legacy active set updated for global queries
            await r.sadd(REDIS_KEY_ACTIVE_JOBS, job_id)
            # Remove from legacy queue list if present
            await r.lrem(REDIS_KEY_QUEUE, 0, job_id)

        return job_id

    @classmethod
    async def release_active_job(cls, job_id: str, queue_type: Optional[str] = None) -> None:
        """Removes a job from active set(s) upon completion, failure, or cancellation."""
        r = get_redis_client()
        if queue_type:
            act_key = cls.get_active_key(queue_type)
            await r.srem(act_key, job_id)
        else:
            # Clean from all active sets to guarantee no leaked locks
            await r.srem(REDIS_KEY_ACTIVE_VIDEO, job_id)
            await r.srem(REDIS_KEY_ACTIVE_SUBTITLE, job_id)

        await r.srem(REDIS_KEY_ACTIVE_JOBS, job_id)
        await r.delete(f"ytdl:job_queue:{job_id}")
        logger.info(f"Job {job_id} removed from active set(s)")

    @classmethod
    async def get_active_job_ids(cls, queue_type: Optional[str] = None) -> List[str]:
        """Returns active job IDs for a specific queue, or across all queues when queue_type is None."""
        r = get_redis_client()
        if queue_type:
            members = await r.smembers(cls.get_active_key(queue_type))
            return list(members)

        v_members = set(await r.smembers(REDIS_KEY_ACTIVE_VIDEO))
        s_members = set(await r.smembers(REDIS_KEY_ACTIVE_SUBTITLE))
        legacy_members = set(await r.smembers(REDIS_KEY_ACTIVE_JOBS))
        return list(v_members | s_members | legacy_members)

    @classmethod
    async def get_queued_job_ids(cls, queue_type: Optional[str] = None) -> List[str]:
        """Returns queued job IDs for a specific queue, or across all queues when queue_type is None."""
        r = get_redis_client()
        if queue_type:
            return await r.lrange(cls.get_queue_key(queue_type), 0, -1)

        v_queued = await r.lrange(REDIS_KEY_QUEUE_VIDEO, 0, -1)
        s_queued = await r.lrange(REDIS_KEY_QUEUE_SUBTITLE, 0, -1)
        # Deduplicate while preserving FIFO ordering
        seen = set()
        combined = []
        for jid in v_queued + s_queued:
            if jid not in seen:
                seen.add(jid)
                combined.append(jid)
        return combined

    @classmethod
    async def get_derived_position(cls, job_id: str, queue_type: Optional[str] = None) -> Optional[int]:
        """
        Calculates derived 1-based queue position.
        If queue_type is specified, checks that specific queue.
        If queue_type is None, checks video queue then subtitle queue.
        Returns None if job is not currently queued.
        """
        r = get_redis_client()
        if queue_type:
            queued = await r.lrange(cls.get_queue_key(queue_type), 0, -1)
            try:
                return queued.index(job_id) + 1
            except ValueError:
                return None

        # Check video queue first
        v_queued = await r.lrange(REDIS_KEY_QUEUE_VIDEO, 0, -1)
        try:
            return v_queued.index(job_id) + 1
        except ValueError:
            pass

        # Check subtitle queue next
        s_queued = await r.lrange(REDIS_KEY_QUEUE_SUBTITLE, 0, -1)
        try:
            return s_queued.index(job_id) + 1
        except ValueError:
            pass

        # Check legacy queue
        l_queued = await r.lrange(REDIS_KEY_QUEUE, 0, -1)
        try:
            return l_queued.index(job_id) + 1
        except ValueError:
            return None

    @classmethod
    async def remove_from_queue(cls, job_id: str, queue_type: Optional[str] = None) -> bool:
        """Removes a job from the specified queue (or from all queues if queue_type is None)."""
        r = get_redis_client()
        if queue_type:
            removed = await r.lrem(cls.get_queue_key(queue_type), 0, job_id)
            await r.lrem(REDIS_KEY_QUEUE, 0, job_id)
            return removed > 0

        rem_v = await r.lrem(REDIS_KEY_QUEUE_VIDEO, 0, job_id)
        rem_s = await r.lrem(REDIS_KEY_QUEUE_SUBTITLE, 0, job_id)
        rem_l = await r.lrem(REDIS_KEY_QUEUE, 0, job_id)
        return (rem_v + rem_s + rem_l) > 0


    @staticmethod
    async def acquire_cache_lock(cache_key: str, job_id: str, ttl_seconds: int = 3600) -> bool:
        """
        Prevents multiple workers or requests from creating duplicate jobs for the same cache key.
        """
        r = get_redis_client()
        lock_key = f"{REDIS_KEY_CACHE_LOCK_PREFIX}{cache_key}"
        # Set if not exists
        acquired = await r.set(lock_key, job_id, nx=True, ex=ttl_seconds)
        return bool(acquired)

    @staticmethod
    async def get_cache_lock_owner(cache_key: str) -> Optional[str]:
        r = get_redis_client()
        lock_key = f"{REDIS_KEY_CACHE_LOCK_PREFIX}{cache_key}"
        return await r.get(lock_key)

    @staticmethod
    async def release_cache_lock(cache_key: str) -> None:
        r = get_redis_client()
        lock_key = f"{REDIS_KEY_CACHE_LOCK_PREFIX}{cache_key}"
        await r.delete(lock_key)

    @staticmethod
    async def set_progress(
        job_id: str,
        stage: str,
        progress_pct: float = 0.0,
        speed: str = "",
        eta: str = "",
    ) -> None:
        r = get_redis_client()
        data = {
            "stage": stage,
            "progress": round(progress_pct, 1),
            "speed": speed,
            "eta": eta,
        }
        await r.set(f"{REDIS_KEY_PROGRESS_PREFIX}{job_id}", json.dumps(data), ex=3600)

    @staticmethod
    async def get_progress(job_id: str) -> Dict:
        r = get_redis_client()
        val = await r.get(f"{REDIS_KEY_PROGRESS_PREFIX}{job_id}")
        if val:
            try:
                return json.loads(val)
            except Exception:
                pass
        return {"stage": "Preparing", "progress": 0.0, "speed": "", "eta": ""}

    @staticmethod
    async def signal_cancel(job_id: str) -> None:
        r = get_redis_client()
        await r.set(f"{REDIS_KEY_CANCEL_PREFIX}{job_id}", "1", ex=3600)

    @staticmethod
    async def is_cancelled(job_id: str) -> bool:
        r = get_redis_client()
        val = await r.get(f"{REDIS_KEY_CANCEL_PREFIX}{job_id}")
        return val == "1"

    @classmethod
    async def check_user_limits(
        cls, session: AsyncSession, user_id: int
    ) -> Tuple[bool, Optional[str]]:
        """
        Enforces MAX_CONCURRENT_PER_USER and MAX_QUEUED_PER_USER.
        """
        # Count active/queued jobs for this user
        stmt = (
            select(func.count(JobRequest.id))
            .join(Job, Job.id == JobRequest.job_id)
            .where(
                JobRequest.user_id == user_id,
                Job.status.in_([
                    JobStatus.QUEUED.value,
                    JobStatus.PREPARING.value,
                    JobStatus.DOWNLOADING.value,
                    JobStatus.PROCESSING.value,
                    JobStatus.UPLOADING.value,
                ]),
            )
        )
        res = await session.execute(stmt)
        active_or_queued_count = res.scalar() or 0

        if active_or_queued_count >= settings.MAX_CONCURRENT_PER_USER + settings.MAX_QUEUED_PER_USER:
            return False, f"⚠️ You have reached the maximum allowed concurrent/queued jobs limit ({active_or_queued_count}). Please wait for your current tasks to finish."

        return True, None

    @classmethod
    async def clear_queued_jobs(
        cls, session: AsyncSession, admin_username: str = "admin"
    ) -> Dict[str, Any]:
        """
        Transaction-safe queue clearing:
        1. Identify queued job IDs from Redis FIFO queues and DB.
        2. Filter out any job that is currently active/processing.
        3. Within PostgreSQL transaction, mark identified queued jobs as CANCELLED.
        4. Atomically drain Redis queue lists and delete queue mapping keys.
        5. Record audit log.
        Guarantees that active processing jobs are NEVER cancelled or deleted.
        """
        r = get_redis_client()
        active_ids = set(await cls.get_active_job_ids())

        # Collect all IDs from Redis queues
        raw_v = [m.decode() if isinstance(m, bytes) else str(m) for m in await r.lrange(REDIS_KEY_QUEUE_VIDEO, 0, -1)]
        raw_s = [m.decode() if isinstance(m, bytes) else str(m) for m in await r.lrange(REDIS_KEY_QUEUE_SUBTITLE, 0, -1)]
        raw_l = [m.decode() if isinstance(m, bytes) else str(m) for m in await r.lrange(REDIS_KEY_QUEUE, 0, -1)]
        redis_queued_ids = set(raw_v) | set(raw_s) | set(raw_l)

        # Also find any jobs in DB marked as QUEUED or PENDING
        stmt = select(Job.id).where(Job.status.in_([JobStatus.QUEUED.value, JobStatus.PENDING.value]))
        res = await session.execute(stmt)
        db_queued_ids = set(res.scalars().all())

        all_candidate_ids = (redis_queued_ids | db_queued_ids) - active_ids
        cancelled_ids = list(all_candidate_ids)

        if cancelled_ids:
            # 1. Update PostgreSQL within transaction
            await session.execute(
                update(Job)
                .where(Job.id.in_(cancelled_ids), Job.id.not_in(active_ids) if active_ids else True)
                .values(
                    status=JobStatus.CANCELLED.value,
                    error_message="Cancelled by admin queue clearing",
                )
            )
            await session.commit()

            # 2. Clear from Redis queues atomically
            if hasattr(r, "pipeline"):
                pipe = r.pipeline()
                pipe.delete(REDIS_KEY_QUEUE_VIDEO)
                pipe.delete(REDIS_KEY_QUEUE_SUBTITLE)
                pipe.delete(REDIS_KEY_QUEUE)
                for jid in cancelled_ids:
                    pipe.delete(f"ytdl:job_queue:{jid}")
                    pipe.set(f"{REDIS_KEY_CANCEL_PREFIX}{jid}", "1", ex=3600)
                await pipe.execute()
            else:
                await r.delete(REDIS_KEY_QUEUE_VIDEO)
                await r.delete(REDIS_KEY_QUEUE_SUBTITLE)
                await r.delete(REDIS_KEY_QUEUE)
                for jid in cancelled_ids:
                    await r.delete(f"ytdl:job_queue:{jid}")
                    await r.set(f"{REDIS_KEY_CANCEL_PREFIX}{jid}", "1", ex=3600)

            # 3. Audit log
            try:
                from src.services.audit_service import AuditService
                await AuditService.log_action(
                    session,
                    "QUEUE_CLEAR",
                    admin_username,
                    f"Cleared {len(cancelled_ids)} queued jobs from queue",
                )
            except Exception as ae:
                logger.warning("Could not write audit log for queue clear: %s", ae)
        else:
            await r.delete(REDIS_KEY_QUEUE_VIDEO, REDIS_KEY_QUEUE_SUBTITLE, REDIS_KEY_QUEUE)

        return {"status": "cleared", "count": len(cancelled_ids), "cleared_ids": cancelled_ids}
