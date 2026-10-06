from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.config import settings
from src.core.constants import JobStatus
from src.core.database import get_db
from src.models.job import Job
from src.services.audit_service import AuditService
from src.services.queue_service import QueueService
from src.web.auth import get_current_admin

router = APIRouter(prefix="/api/queue", tags=["Queue"])


class ConcurrencyRequest(BaseModel):
    max_active_jobs: int


@router.get("")
async def get_queue_state(
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    active_ids = await QueueService.get_active_job_ids()
    queued_ids = await QueueService.get_queued_job_ids()
    is_paused = await QueueService.is_paused()

    active_jobs = []
    if active_ids:
        stmt = select(Job).where(Job.id.in_(active_ids))
        res = await session.execute(stmt)
        for job in res.scalars().all():
            progress_data = await QueueService.get_progress(job.id)
            active_jobs.append({
                "id": job.id,
                "title": job.title,
                "operation": job.operation,
                "codec": job.output_codec,
                "resolution": job.resolution,
                "stage": progress_data.get("stage", job.status),
                "progress": progress_data.get("progress", 0.0),
                "speed": progress_data.get("speed", ""),
                "eta": progress_data.get("eta", ""),
                "started_at": job.started_at.isoformat() if job.started_at else None,
            })

    queued_jobs = []
    if queued_ids:
        stmt = select(Job).where(Job.id.in_(queued_ids))
        res = await session.execute(stmt)
        job_map = {j.id: j for j in res.scalars().all()}
        for idx, jid in enumerate(queued_ids, 1):
            j = job_map.get(jid)
            if j:
                queued_jobs.append({
                    "position": idx,
                    "id": j.id,
                    "title": j.title,
                    "operation": j.operation,
                    "codec": j.output_codec,
                    "resolution": j.resolution,
                    "queued_at": j.created_at.isoformat() if j.created_at else None,
                })

    return {
        "is_paused": is_paused,
        "max_active_jobs": settings.MAX_ACTIVE_JOBS,
        "active_jobs": active_jobs,
        "queued_jobs": queued_jobs,
    }


@router.post("/pause")
async def pause_queue(
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    await QueueService.pause_queue()
    await AuditService.log_action(session, "QUEUE_PAUSE", admin["sub"], "Paused processing queue")
    return {"status": "paused"}


@router.post("/resume")
async def resume_queue(
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    await QueueService.resume_queue()
    await AuditService.log_action(session, "QUEUE_RESUME", admin["sub"], "Resumed processing queue")
    return {"status": "resumed"}


@router.post("/clear")
async def clear_queue(
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    """Transaction-safe clearing of queued jobs."""
    result = await QueueService.clear_queued_jobs(session, admin_username=admin["sub"])
    return result


from src.services.setting_service import SETTING_MAX_ACTIVE_JOBS, SettingService


@router.post("/concurrency")
async def update_concurrency(
    req: ConcurrencyRequest,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    if req.max_active_jobs < 1 or req.max_active_jobs > 8:
        raise HTTPException(status_code=400, detail="Concurrency must be between 1 and 8")

    old_val = settings.MAX_ACTIVE_JOBS
    await SettingService.save_single_setting(
        SETTING_MAX_ACTIVE_JOBS, req.max_active_jobs, description="Max Active Processing Jobs", session=session
    )

    await AuditService.log_action(
        session,
        "SETTING_UPDATE",
        admin["sub"],
        f"Updated MAX_ACTIVE_JOBS from {old_val} to {req.max_active_jobs}",
    )
    return {
        "status": "updated",
        "max_active_jobs": settings.MAX_ACTIVE_JOBS,
        "warning": "Values > 2 may create high CPU pressure on 2-core VPS during video transcoding."
        if req.max_active_jobs > 2
        else None,
    }


@router.post("/cancel/{job_id}")
async def cancel_job(
    job_id: str,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    await QueueService.signal_cancel(job_id)
    await QueueService.remove_from_queue(job_id)
    stmt = select(Job).where(Job.id == job_id)
    res = await session.execute(stmt)
    job = res.scalar_one_or_none()
    if job:
        job.status = JobStatus.CANCELLED.value
        await session.commit()
    await AuditService.log_action(session, "JOB_CANCEL", admin["sub"], f"Cancelled job {job_id}")
    return {"status": "cancelled", "job_id": job_id}


@router.post("/retry/{job_id}")
async def retry_job(
    job_id: str,
    session: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    stmt = select(Job).where(Job.id == job_id)
    res = await session.execute(stmt)
    job = res.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job.status = JobStatus.QUEUED.value
    job.error_code = None
    job.error_message = None
    await session.commit()

    pos = await QueueService.push_job(job.id, queue_type=job.queue_type)
    await AuditService.log_action(session, "JOB_RETRY", admin["sub"], f"Retried job {job_id}")
    return {"status": "requeued", "position": pos, "queue": job.queue_type}
