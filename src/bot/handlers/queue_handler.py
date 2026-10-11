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


@queue_router.callback_query(lambda c: c.data and c.data.startswith("q_cancel:"))
async def on_cancel_job(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    job_id = callback.data.split(":")[1]

    async with AsyncSessionLocal() as session:
        # Find user's specific request
        stmt = select(JobRequest).where(
            JobRequest.job_id == job_id, JobRequest.user_id == user_id
        )
        res = await session.execute(stmt)
        user_req = res.scalar_one_or_none()

        if not user_req:
            await callback.answer("You are not subscribed to this job.", show_alert=True)
            return

        # Delete only this user's subscription
        await session.delete(user_req)
        await session.commit()

        # Check remaining subscribers for this job
        count_stmt = select(func.count(JobRequest.id)).where(JobRequest.job_id == job_id)
        count_res = await session.execute(count_stmt)
        remaining_subscribers = count_res.scalar() or 0

        if remaining_subscribers > 0:
            logger.info(
                f"User {user_id} unsubscribed from job {job_id}. {remaining_subscribers} subscribers remain."
            )
            await callback.answer("You have cancelled your subscription. The job continues for other users.", show_alert=True)
            try:
                await callback.message.edit_text("❌ <i>You cancelled your request.</i>")
            except Exception:
                pass
            return

        # Zero subscribers remain: cancel the underlying job!
        logger.info(f"Zero subscribers remain for job {job_id}. Terminating job.")
        await QueueService.signal_cancel(job_id)
        await QueueService.remove_from_queue(job_id)

        job_stmt = select(Job).where(Job.id == job_id)
        job_res = await session.execute(job_stmt)
        job = job_res.scalar_one_or_none()
        if job and job.status not in (JobStatus.COMPLETED.value, JobStatus.FAILED.value):
            job.status = JobStatus.CANCELLED.value
            await session.commit()

        try:
            await callback.message.edit_text("❌ <b>Job cancelled and removed from queue.</b>")
        except Exception:
            pass

        await callback.answer("Job cancelled.")
