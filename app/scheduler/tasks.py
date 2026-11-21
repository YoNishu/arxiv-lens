import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.services import cycle

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def poll_and_process():
    """Scheduled cycle. If a cycle is already running in this process, wait for that one."""
    try:
        run_id, started = await cycle.start_cycle()
        if started:
            logger.info("Scheduler: started cycle %d", run_id)
        else:
            logger.info("Scheduler: cycle %d is already running", run_id)
        await cycle.wait_for_cycle()
    except Exception:
        logger.exception("Scheduler: poll_and_process failed")


def start_scheduler():
    scheduler.add_job(
        poll_and_process,
        trigger=IntervalTrigger(hours=settings.poll_interval_hours),
        id="poll_and_process",
        replace_existing=True,
    )
    # Catch-up job on startup
    scheduler.add_job(poll_and_process, id="startup_catchup", replace_existing=True)
    scheduler.start()
    logger.info("Scheduler started (interval: %dh)", settings.poll_interval_hours)


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")
