from aiogram import Bot, Router
from aiogram.types import CallbackQuery
from sqlalchemy import func, select
from src.core.config import settings
from src.core.constants import DeliveryStatus, JobStatus
from src.core.database import AsyncSessionLocal
from src.core.logger import setup_logger
from src.models.job import Job
from src.models.job_request import JobRequest
from src.services.queue_service import QueueService

logger = setup_logger("queue_handler")
queue_router = Router()


@queue_router.callback_query(lambda c: c.data and c.data.startswith("q_stat:"))
async def on_queue_status(callback: CallbackQuery, bot: Bot):
    job_id = callback.data.split(":")[1]

    async with AsyncSessionLocal() as session:
        stmt = select(Job).where(Job.id == job_id)
        res = await session.execute(stmt)
        job = res.scalar_one_or_none()

        if not job:
            await callback.answer("Job not found.", show_alert=True)
            return

        pos = await QueueService.get_derived_position(job_id, queue_type=job.queue_type)
        active_ids = await QueueService.get_active_job_ids(queue_type=job.queue_type)
        queued_ids = await QueueService.get_queued_job_ids(queue_type=job.queue_type)
        queue_limit = (
            getattr(settings, "MAX_ACTIVE_SUBTITLE_JOBS", 2)
            if job.queue_type == "SUBTITLE"
            else getattr(settings, "MAX_ACTIVE_VIDEO_JOBS", 1)
        )

        pos_str = f"#{pos}" if pos else ("Active (In Progress)" if job_id in active_ids else job.status)

        quality_str = f"{job.output_codec} · {job.resolution}" if job.resolution else (job.output_codec or job.subtitle_lang or "")

        status_text = (
            f"📊 <b>Queue Status</b>\n\n"
            f"<b>Title:</b> {job.title[:35]}...\n"
            f"<b>Request:</b> {job.operation} {quality_str}\n"
            f"<b>Queue:</b> {job.queue_type}\n"
            f"<b>Position:</b> {pos_str}\n"
            f"<b>Active jobs:</b> {len(active_ids)} / {queue_limit}\n"
            f"<b>Waiting in queue:</b> {len(queued_ids)}\n"
            f"<b>Current stage:</b> {job.status}"
        )

        alert_text = (
            f"📊 Queue Status: {job.status}\n\n"
            f"Position: {pos_str}\n"
            f"Active: {len(active_ids)} / {queue_limit}\n"
            f"In queue: {len(queued_ids)}"
        )
        try:
            await callback.answer(alert_text, show_alert=True)
        except Exception:
            await callback.answer()


from src.worker.notifier import StatusNotifier


@queue_router.callback_query(lambda c: c.data and c.data.startswith("q_cancel:"))
async def on_cancel_job(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    job_id = callback.data.split(":")[1]

    async with AsyncSessionLocal() as session:
        # 1. Validate job ownership and chat context
        stmt = select(JobRequest).where(
            JobRequest.job_id == job_id, JobRequest.user_id == user_id
        )
        res = await session.execute(stmt)
        user_req = res.scalar_one_or_none()

        if not user_req:
            await callback.answer("You are not authorized or not subscribed to this job.", show_alert=True)
            return

        job_stmt = select(Job).where(Job.id == job_id)
        job_res = await session.execute(job_stmt)
        job = job_res.scalar_one_or_none()

        if not job or job.status in (JobStatus.COMPLETED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value):
            await callback.answer("This job has already finished or was cancelled.", show_alert=True)
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            return

        # 2. Check if the job is still queued vs already active
        # Handle races between cancellation and job acquisition
        if job.status != JobStatus.QUEUED.value:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            await callback.answer("Job has already started downloading and cannot be cancelled.", show_alert=True)
            return

        # 3. Atomically remove from the Redis queue
        was_removed = await QueueService.remove_from_queue(job_id, queue_type=job.queue_type)
        if not was_removed:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            await callback.answer("Job has already started downloading and cannot be cancelled.", show_alert=True)
            return

        # 4. Confirmed queued: remove user request
        await session.delete(user_req)
        await session.commit()

        # Check remaining subscribers
        count_stmt = select(func.count(JobRequest.id)).where(JobRequest.job_id == job_id)
        count_res = await session.execute(count_stmt)
        remaining_subscribers = count_res.scalar() or 0

        if remaining_subscribers > 0:
            logger.info(
                f"User {user_id} unsubscribed from queued job {job_id}. {remaining_subscribers} subscribers remain."
            )
            # Re-enqueue for remaining subscribers if it was removed
            await QueueService.push_job(job_id, queue_type=job.queue_type)
            await callback.answer("You have cancelled your subscription. The job continues for other users.", show_alert=True)
            try:
                await callback.message.edit_text("❌ <i>You cancelled your request.</i>", reply_markup=None)
            except Exception:
                try:
                    await callback.message.edit_caption(caption="❌ <i>You cancelled your request.</i>", reply_markup=None)
                except Exception:
                    pass
            return

        # 5. Zero subscribers remain: update PostgreSQL state and release cache lock
        logger.info(f"Zero subscribers remain for queued job {job_id}. Marking CANCELLED.")
        job.status = JobStatus.CANCELLED.value
        await session.commit()

        await QueueService.signal_cancel(job_id)
        if getattr(job, "cache_key", None):
            await QueueService.release_cache_lock(job.cache_key, job_id)

        try:
            await callback.message.edit_text("❌ <b>Job cancelled and removed from queue.</b>", reply_markup=None)
        except Exception:
            try:
                await callback.message.edit_caption(caption="❌ <b>Job cancelled and removed from queue.</b>", reply_markup=None)
            except Exception:
                pass

        await callback.answer("Job cancelled.")
