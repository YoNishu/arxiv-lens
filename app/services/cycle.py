"""The poll-and-process cycle and its bookkeeping.

run_cycle() is the one way a cycle runs, whoever asks for it: the command line awaits it, the
server and its scheduler start it as a background task through start_cycle(), so every request
is answered at once. Each cycle is a row of the runs table with one run_feeds row per feed.
Several processes may share a database; what keeps a paper from being analysed twice is the
claim in app/services/processor.py, not this module.
"""
import asyncio
import logging

from app import clock
from app.db import crud
from app.db.models import Run
from app.services import feed_poller, processor
from app.services.sources import current as current_sources

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()
_current_run_id: int | None = None
_current_task: asyncio.Task | None = None
_rerun_requested = False
_stopping = False
_followups: set[asyncio.Task] = set()

_COUNTS = ("new_papers", "updated", "skipped", "processed", "failed")


async def run_cycle(sources=None, run_id: int | None = None) -> Run:
    """Poll every feed, then analyse every paper that is due. Returns the run as stored.

    A failing feed or paper is recorded where it happened and the cycle goes on; only an error
    that stops the whole cycle is recorded as the run's error.
    """
    await crud.close_abandoned_runs()
    if run_id is None:
        run_id = (await crud.create_run()).id
    totals = dict.fromkeys(_COUNTS, 0)
    error: str | None = None
    logger.info("Cycle %d: starting poll and process", run_id)

    async def polled(stats: feed_poller.FeedStats) -> None:
        await crud.add_run_feed(
            run_id, stats.feed, stats.listed, stats.new_papers, stats.updated, stats.skipped,
            stats.error_message,
        )
        totals["new_papers"] += stats.new_papers
        totals["updated"] += stats.updated
        totals["skipped"] += stats.skipped
        await crud.update_run(run_id, **totals)

    async def worked(stats: processor.ProcessStats) -> None:
        totals.update(processed=stats.processed, failed=stats.failed)
        await crud.update_run(run_id, **totals)

    try:
        if sources is None:
            sources = current_sources()
        await crud.seed_categories(sources.seed_categories())
        await feed_poller.poll_all(sources, on_feed=polled)
        logger.info(
            "Cycle %d: %d new papers, %d updated, %d skipped",
            run_id, totals["new_papers"], totals["updated"], totals["skipped"],
        )
        await processor.process_pending_papers(sources, on_progress=worked)
        logger.info("Cycle %d: %d processed, %d failed", run_id, totals["processed"], totals["failed"])
    except asyncio.CancelledError:
        error = "Cycle was stopped before it finished"
        raise
    except Exception as e:
        logger.exception("Cycle %d failed", run_id)
        error = (str(e) or type(e).__name__)[:500]
    finally:
        try:
            # Shielded: a cancelled cycle still records how it ended.
            await asyncio.shield(
                crud.update_run(run_id, finished_at=clock.now(), error_message=error, **totals)
            )
        except BaseException:
            logger.warning("Could not record the end of cycle %d", run_id, exc_info=True)
    return await crud.get_run(run_id)


def is_running() -> bool:
    # A queued follow-up cycle counts: there is no idle moment between the two.
    return _current_run_id is not None or bool(_followups)


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
        _current_task = asyncio.create_task(_in_background(run.id), name=f"cycle-{run.id}")
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


async def _in_background(run_id: int) -> None:
    global _current_run_id, _current_task, _rerun_requested
    cancelled = False
    try:
        await run_cycle(run_id=run_id)
    except asyncio.CancelledError:
        cancelled = True
        raise
    except Exception:
        # The run row says what it can; the server keeps answering.
        logger.exception("Cycle %d could not run", run_id)
    finally:
        _current_run_id = None
        _current_task = None
        if _rerun_requested and not cancelled and not _stopping:
            _rerun_requested = False
            followup = asyncio.get_running_loop().create_task(start_cycle())
            _followups.add(followup)
            followup.add_done_callback(_followups.discard)
