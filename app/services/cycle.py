"""The poll-and-process cycle and its bookkeeping.

A process runs one cycle at a time. Whoever asks for a cycle (the scheduler, POST /process, the
refresh button, a new category) gets an answer at once: the cycle runs as a background task and
is recorded as a row of the runs table. Several processes may share a database; what keeps a
paper from being analysed twice is the claim in app/services/processor.py, not this module.
"""
import asyncio
import logging
from datetime import datetime, timezone

from app.db import crud
from app.services import feed_poller, processor

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()
_current_run_id: int | None = None
_current_task: asyncio.Task | None = None
_rerun_requested = False
_stopping = False
_followups: set[asyncio.Task] = set()


def is_running() -> bool:
    # A queued follow-up cycle counts: there is no idle moment between the two.
    return _current_run_id is not None or bool(_followups)


def current_run_id() -> int | None:
    return _current_run_id


async def start_cycle(queue_if_running: bool = False) -> tuple[int, bool]:
    """Start a cycle in the background. Returns (run_id, started).

    While a cycle is running in this process no second one is started: the answer is the id of
    the running cycle and started=False. With queue_if_running, one more cycle is run right
    after it (used when a category is added, so its feed is polled without waiting for the
    next scheduled cycle).
    """
    global _current_run_id, _current_task, _rerun_requested
    async with _lock:
        if _current_run_id is not None:
            if queue_if_running:
                _rerun_requested = True
            return _current_run_id, False
        run = await crud.create_run()
        _current_run_id = run.id
        _current_task = asyncio.create_task(_run_cycle(run.id), name=f"cycle-{run.id}")
        return run.id, True


async def wait_for_cycle() -> None:
    """Wait until the cycle running in this process, if any, has finished."""
    task = _current_task
    if task is not None:
        await asyncio.wait([task])


async def shutdown() -> None:
    """Stop the running cycle. Its paper in work keeps its lease until that runs out."""
    global _stopping
    _stopping = True
    tasks = [t for t in (_current_task, *_followups) if t is not None and not t.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=30)


async def _run_cycle(run_id: int) -> None:
    global _current_run_id, _current_task, _rerun_requested
    counts = {"new_papers": 0, "updated": 0, "skipped": 0, "processed": 0, "failed": 0}
    error: str | None = None
    cancelled = False
    logger.info("Cycle %d: starting poll and process", run_id)

    async def polled(stats: feed_poller.PollStats) -> None:
        counts.update(new_papers=stats.new_papers, updated=stats.updated, skipped=stats.skipped)
        await crud.update_run(run_id, **counts)

    async def worked(stats: processor.ProcessStats) -> None:
        counts.update(processed=stats.processed, failed=stats.failed)
        await crud.update_run(run_id, **counts)

    try:
        await feed_poller.sync_categories_from_yaml()
        await feed_poller.poll_all(on_progress=polled)
        logger.info(
            "Cycle %d: %d new papers, %d updated, %d skipped",
            run_id, counts["new_papers"], counts["updated"], counts["skipped"],
        )
        await processor.process_pending_papers(on_progress=worked)
        logger.info("Cycle %d: %d processed, %d failed", run_id, counts["processed"], counts["failed"])
    except asyncio.CancelledError:
        cancelled = True
        error = "Cycle was stopped before it finished"
        raise
    except Exception as e:
        logger.exception("Cycle %d failed", run_id)
        error = (str(e) or type(e).__name__)[:500]
    finally:
        try:
            await asyncio.shield(
                crud.update_run(
                    run_id, finished_at=datetime.now(timezone.utc), error_message=error, **counts
                )
            )
        except BaseException:
            logger.warning("Could not record the end of cycle %d", run_id, exc_info=True)
        _current_run_id = None
        _current_task = None
        if _rerun_requested and not cancelled and not _stopping:
            _rerun_requested = False
            followup = asyncio.get_running_loop().create_task(start_cycle())
            _followups.add(followup)
            followup.add_done_callback(_followups.discard)
