import asyncio
import logging
import uuid
from dataclasses import dataclass

from app.config import settings
from app.db import crud
from app.services import pdf_extractor, paper_analyzer

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


async def process_paper(paper_id: int) -> str:
    """Analyse one paper, if this worker can take it. Returns one of the outcomes above.

    The claim is atomic in the database, so of all cycles and processes that reach for a paper
    only one works on it. Every claim counts as a try in papers.attempts.

    If the worker is stopped mid-way (shutdown, kill), the paper stays in work without anyone
    renewing its lease, and a later cycle of any process takes it over.
    """
    token = uuid.uuid4().hex
    paper = await crud.claim_paper(paper_id, token)
    if paper is None:
        return NOT_CLAIMED

    use_blob = pdf_extractor.blob_storage_configured()
    blob_name = f"paper_{paper_id}.pdf"
    local_path = None
    lease = asyncio.create_task(_keep_lease(paper_id, token))

    try:
        local_path = await pdf_extractor.download_pdf(paper.pdf_url, paper_id, tag=token[:8])
        if use_blob:
            await pdf_extractor.upload_to_blob(local_path, blob_name)

        # In a thread: a large PDF must not stop the event loop, which renews the lease.
        full_text = await asyncio.to_thread(pdf_extractor.extract_text, local_path)
        if not full_text.strip():
            logger.warning("Empty text extraction for paper %d", paper_id)
            full_text = f"[Text extraction failed — abstract only]\n\n{paper.abstract}"

        analysis = await paper_analyzer.analyze_paper(
            full_text, title=paper.title, abstract=paper.abstract
        )

        if await crud.complete_paper(paper_id, token, full_text, analysis):
            logger.info("Paper %d processed successfully", paper_id)
            return DONE
        logger.warning("Paper %d was taken over meanwhile; its analysis was not stored", paper_id)
        return LOST

    except Exception as e:
        logger.exception("Failed to process paper %d", paper_id)
        message = (str(e) or type(e).__name__)[:500]
        if await crud.fail_paper(paper_id, token, message):
            return FAILED
        return LOST
    finally:
        lease.cancel()
        if local_path:
            pdf_extractor.cleanup_local_files(local_path)
        if use_blob:
            await pdf_extractor.delete_blob(blob_name)


async def process_pending_papers(on_progress=None) -> ProcessStats:
    """Try every paper that is due: pending, failed with tries left, or abandoned mid-way."""
    stats = ProcessStats()
    paper_ids = await crud.get_claimable_paper_ids()
    logger.info("Processing %d papers", len(paper_ids))
    for paper_id in paper_ids:
        try:
            outcome = await process_paper(paper_id)
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
