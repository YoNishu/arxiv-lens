"""Analysing stored papers: claim, text, analysis, completion.

A paper is claimed in the database before anyone works on it, so of all cycles and processes
that reach for it only one does. Its text is kept in the text store before the model is asked,
so a failed model call never costs the PDF a second time.
"""
import asyncio
import logging
import uuid
from dataclasses import dataclass

from app.config import settings
from app.db import crud
from app.services import pdf_extractor, textstore

logger = logging.getLogger(__name__)

# Outcomes of process_paper
DONE = "done"
FAILED = "failed"
NOT_CLAIMED = "not_claimed"  # another worker has it, or there is nothing left to do
LOST = "lost"  # the paper was taken away while this worker was on it; nothing was stored


@dataclass
class ProcessStats:
    processed: int = 0
    failed: int = 0


def _heartbeat_interval() -> float:
    # Renew well inside the lease, so one late renewal does not cost the paper.
    return max(0.1, min(settings.processing_lease_seconds / 4, 60.0))


async def _keep_lease(paper_id: int, token: str) -> None:
    """Renew the lease for as long as the worker is on the paper."""
    interval = _heartbeat_interval()
    while True:
        await asyncio.sleep(interval)
        try:
            if not await crud.renew_lease(paper_id, token):
                logger.warning("Paper %d is no longer held by this worker", paper_id)
                return
        except Exception:
            logger.warning("Could not renew the lease of paper %d", paper_id, exc_info=True)


async def process_paper(paper_id: int, sources) -> str:
    """Analyse one paper, if this worker can take it. Returns one of the outcomes above.

    Every claim counts as a try in papers.attempts. If the worker is stopped mid-way (shutdown,
    kill), the paper stays in work without anyone renewing its lease, and a later cycle of any
    process takes it over.
    """
    token = uuid.uuid4().hex
    paper = await crud.claim_paper(paper_id, token)
    if paper is None:
        return NOT_CLAIMED

    blob_name = f"paper_{paper.id}.pdf"
    staged = False
    lease = asyncio.create_task(_keep_lease(paper.id, token))
    try:
        text = await textstore.get(paper)
        if text is None:
            data = await sources.pdf(paper)
            if pdf_extractor.blob_storage_configured():
                # Marked before the upload: a half-finished upload is deleted as well.
                staged = True
                await pdf_extractor.upload_to_blob(data, blob_name)
            # In a thread: a large PDF must not stop the event loop, which renews the lease.
            text = await asyncio.to_thread(pdf_extractor.extract_text, data)
            extraction = "pdf"
            if not text.strip():
                logger.warning("Empty text extraction for paper %d", paper.id)
                text = textstore.PLACEHOLDER + paper.abstract
                extraction = "abstract"
            await textstore.put(paper, text, extraction)

        analysis = await sources.analysis(paper, text)
        if await crud.complete_paper(paper.id, token, paper.version, analysis):
            logger.info("Paper %d processed successfully", paper.id)
            return DONE
        logger.warning("Paper %d was taken over meanwhile; its analysis was not stored", paper.id)
        return LOST

    except Exception as e:
        logger.exception("Failed to process paper %d", paper.id)
        message = (str(e) or type(e).__name__)[:500]
        if await crud.fail_paper(paper.id, token, message):
            return FAILED
        return LOST
    finally:
        lease.cancel()
        if staged:
            await pdf_extractor.delete_blob(blob_name)


async def process_pending_papers(sources, on_progress=None) -> ProcessStats:
    """Try every paper that is due: pending, failed with tries left, or abandoned mid-way."""
    stats = ProcessStats()
    paper_ids = await crud.get_claimable_paper_ids()
    logger.info("Processing %d papers", len(paper_ids))
    for paper_id in paper_ids:
        try:
            outcome = await process_paper(paper_id, sources)
        except Exception:
            # e.g. the database went away; the lease lets a later cycle pick the paper up
            logger.exception("Processing of paper %d was cut short", paper_id)
            continue
        if outcome == DONE:
            stats.processed += 1
        elif outcome == FAILED:
            stats.failed += 1
        if on_progress and outcome in (DONE, FAILED):
            await on_progress(stats)
    return stats
