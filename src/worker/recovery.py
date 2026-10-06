import asyncio
import os
import shutil
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.config import settings
from src.core.constants import (
    REDIS_KEY_ACTIVE_JOBS,
    REDIS_KEY_ACTIVE_SUBTITLE,
    REDIS_KEY_ACTIVE_VIDEO,
    REDIS_KEY_CACHE_LOCK_PREFIX,
    JobStatus,
)
from src.core.logger import setup_logger
from src.core.redis import get_redis_client
from src.models.job import Job
from src.services.queue_service import QueueService

logger = setup_logger("worker_recovery")


class WorkerRecovery:
    @classmethod
    async def perform_startup_recovery(cls, session: AsyncSession) -> None:
        """
        Executed when the worker boots up:
        - Clears dangling Redis active sets for video, subtitle, and legacy queues.
        - Identifies interrupted in-flight jobs in PostgreSQL and recovers them to QUEUED,
          re-enqueueing them to their respective logical queue.
        - Verifies that all PostgreSQL QUEUED jobs are present in the Redis queues.
        - Cleans stale local temporary and transfer directories asynchronously.
        """
        logger.info("Performing worker startup recovery...")

        # 1. Clean Redis active job sets from prior worker run
        r = get_redis_client()
        for active_key in (REDIS_KEY_ACTIVE_VIDEO, REDIS_KEY_ACTIVE_SUBTITLE, REDIS_KEY_ACTIVE_JOBS):
            active_job_ids = await r.smembers(active_key)
            for jid in active_job_ids:
                await r.srem(active_key, jid)
                logger.info(f"Released dangling active lock in {active_key} for job: {jid}")

        # 2. Identify and recover interrupted in-flight jobs in PostgreSQL
        in_flight_statuses = [
            JobStatus.PREPARING.value,
            JobStatus.DOWNLOADING.value,
            JobStatus.PROCESSING.value,
            JobStatus.UPLOADING.value,
        ]
        stmt = select(Job).where(Job.status.in_(in_flight_statuses))
        res = await session.execute(stmt)
        abandoned_jobs = res.scalars().all()

        for job in abandoned_jobs:
            logger.warning(
                "Recovering interrupted job %s (was %s, queue=%s). Requeuing for processing.",
                job.id,
                job.status,
                job.queue_type,
            )
            job.status = JobStatus.QUEUED.value
            job.error_code = None
            job.error_message = None
            # Re-enqueue to appropriate logical queue
            await QueueService.push_job(job.id, queue_type=job.queue_type)

        # 3. Ensure any existing QUEUED jobs in PostgreSQL are present in Redis
        queued_stmt = select(Job).where(Job.status == JobStatus.QUEUED.value)
        queued_res = await session.execute(queued_stmt)
        for q_job in queued_res.scalars().all():
            pos = await QueueService.get_derived_position(q_job.id, queue_type=q_job.queue_type)
            if pos is None:
                logger.info(
                    "Re-enqueuing missing queued job %s to %s queue", q_job.id, q_job.queue_type
                )
                await QueueService.push_job(q_job.id, queue_type=q_job.queue_type)

        await session.commit()

        # 4. Clean temporary storage directory /tmp/ytdl/
        temp_dir = settings.TEMP_DIR
        if os.path.exists(temp_dir):
            try:
                for item in os.listdir(temp_dir):
                    item_path = os.path.join(temp_dir, item)
                    if os.path.isdir(item_path):
                        await asyncio.to_thread(shutil.rmtree, item_path, True)
                        logger.info(f"Cleaned stale temp folder: {item_path}")
            except Exception as e:
                logger.error(f"Error during temp directory cleanup: {e}")

        # 5. Clean transfer staging directory if configured
        transfer_dir = getattr(settings, "TRANSFER_DIR", None)
        if transfer_dir and os.path.exists(transfer_dir):
            try:
                for item in os.listdir(transfer_dir):
                    item_path = os.path.join(transfer_dir, item)
                    if os.path.isdir(item_path):
                        await asyncio.to_thread(shutil.rmtree, item_path, True)
                        logger.info(f"Cleaned stale transfer folder: {item_path}")
            except Exception as e:
                logger.error(f"Error during transfer directory cleanup: {e}")

        logger.info("Startup recovery complete.")

