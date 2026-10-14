import asyncio
from pathlib import Path
import signal
from src.core.config import settings
from src.core.database import AsyncSessionLocal
from src.core.logger import setup_logger
from src.services.queue_service import QueueService
from src.worker.processor import JobProcessor
from src.worker.recovery import WorkerRecovery

logger = setup_logger("worker_main")
running = True


def handle_stop_signals():
    global running
    logger.info("Shutdown signal received. Stopping worker loop...")
    running = False


async def periodic_telemetry(processor: JobProcessor):
    while running:
        try:
            await processor.report_telemetry()
        except Exception as e:
            logger.debug("Error in telemetry loop: %s", e)
        await asyncio.sleep(15)


async def worker_loop():
    global running
    logger.info("Starting background worker...")

    processor = JobProcessor()

    # 1. Startup recovery
    async with AsyncSessionLocal() as session:
        await WorkerRecovery.perform_startup_recovery(session)

    # Load persistent application settings from PostgreSQL
    from src.services.setting_service import SettingService
    try:
        await SettingService.load_public_settings_to_runtime()
    except Exception as e:
        logger.warning("Could not load application settings from DB in worker: %s", e)

    # Listen for runtime config reloads via Redis
    async def listen_for_setting_reloads():
        from src.core.redis import get_redis_client
        from src.services.must_join_service import MustJoinService
        while running:
            try:
                r = get_redis_client()
                pubsub = r.pubsub()
                await pubsub.subscribe("app:config:reload", "must_join:channels:reload")
                async for message in pubsub.listen():
                    if not running:
                        break
                    if message and message.get("type") == "message":
                        channel = message.get("channel")
                        if isinstance(channel, bytes):
                            channel = channel.decode("utf-8")
                        if channel == "must_join:channels:reload":
                            logger.info("Worker received must-join channels reload signal. Clearing channels cache...")
                            MustJoinService.clear_local_channels_cache()
                        elif channel == "app:config:reload":
                            try:
                                await SettingService.load_public_settings_to_runtime()
                                logger.info("Worker reloaded public application settings from PostgreSQL.")
                            except Exception as e:
                                logger.warning("Worker failed to reload application settings: %s", e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Worker settings reload pubsub error: %s", e)
                await asyncio.sleep(3.0)

    setting_reload_task = asyncio.create_task(listen_for_setting_reloads())

    # Launch telemetry task
    telemetry_task = asyncio.create_task(periodic_telemetry(processor))

    # 2. Decoupled Consumer Loops: Video and Subtitles run completely independently
    async def video_consumer_loop():
        limit = getattr(settings, "MAX_ACTIVE_VIDEO_JOBS", 1)
        logger.info(f"[Video Consumer] Listening on VIDEO queue (Concurrency limit: {limit})...")
        while running:
            try:
                ready_file = Path(settings.RUNTIME_READY_FILE)
                if not ready_file.is_file():
                    await asyncio.sleep(1.0)
                    continue

                active_limit = getattr(settings, "MAX_ACTIVE_VIDEO_JOBS", 1)
                job_id = await QueueService.acquire_next_job(queue_type="VIDEO", max_active_jobs=active_limit)
                if job_id:
                    logger.info(f"[Video Consumer] Acquired job {job_id}. Starting execution...")
                    asyncio.create_task(processor.process_job(job_id))
                else:
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in video consumer loop: {e}", exc_info=True)
                await asyncio.sleep(2.0)
        logger.info("[Video Consumer] Loop stopped.")

    async def subtitle_consumer_loop():
        limit = getattr(settings, "MAX_ACTIVE_SUBTITLE_JOBS", 2)
        logger.info(f"[Subtitle Consumer] Listening on SUBTITLE queue (Concurrency limit: {limit})...")
        while running:
            try:
                ready_file = Path(settings.RUNTIME_READY_FILE)
                if not ready_file.is_file():
                    await asyncio.sleep(1.0)
                    continue

                active_limit = getattr(settings, "MAX_ACTIVE_SUBTITLE_JOBS", 2)
                job_id = await QueueService.acquire_next_job(queue_type="SUBTITLE", max_active_jobs=active_limit)
                if job_id:
                    logger.info(f"[Subtitle Consumer] Acquired job {job_id}. Starting execution...")
                    asyncio.create_task(processor.process_job(job_id))
                else:
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in subtitle consumer loop: {e}", exc_info=True)
                await asyncio.sleep(2.0)
        logger.info("[Subtitle Consumer] Loop stopped.")

    worker_mode = getattr(settings, "WORKER_MODE", "all").lower()
    logger.info(
        "Worker active in mode '%s' (Video limit: %d, Subtitle limit: %d)",
        worker_mode,
        settings.MAX_ACTIVE_VIDEO_JOBS,
        settings.MAX_ACTIVE_SUBTITLE_JOBS,
    )

    consumer_tasks = []
    if worker_mode in ("all", "video"):
        consumer_tasks.append(asyncio.create_task(video_consumer_loop()))
    if worker_mode in ("all", "subtitle"):
        consumer_tasks.append(asyncio.create_task(subtitle_consumer_loop()))

    try:
        await asyncio.gather(*consumer_tasks)
    finally:
        telemetry_task.cancel()
        setting_reload_task.cancel()
        for ct in consumer_tasks:
            ct.cancel()

    logger.info("Worker consumer loops finished.")



def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_stop_signals)
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(worker_loop())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
