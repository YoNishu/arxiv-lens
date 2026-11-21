import logging

from app.db import crud
from app.db.models import PaperStatus
from app.services import pdf_extractor, paper_analyzer

logger = logging.getLogger(__name__)


async def process_paper(paper_id: int, pdf_url: str, title: str, abstract: str) -> None:
    blob_name = f"paper_{paper_id}.pdf"
    local_path = None

    try:
        await crud.update_paper_status(paper_id, PaperStatus.processing)

        local_path = await pdf_extractor.download_pdf(pdf_url, paper_id)
        await pdf_extractor.upload_to_blob(local_path, blob_name)

        full_text = pdf_extractor.extract_text(local_path)
        if not full_text.strip():
            logger.warning("Empty text extraction for paper %d", paper_id)
            full_text = f"[Text extraction failed — abstract only]\n\n{abstract}"

        analysis = await paper_analyzer.analyze_paper(full_text, title=title, abstract=abstract)

        await crud.create_analysis(
            paper_id=paper_id,
            full_text=full_text,
            summary=analysis["summary"],
            key_contributions=analysis["key_contributions"],
            methodology=analysis["methodology"],
            limitations=analysis.get("limitations"),
            keywords=analysis["keywords"],
            novelty_score=analysis["novelty_score"],
        )
        await crud.update_paper_status(paper_id, PaperStatus.done)
        logger.info("Paper %d processed successfully", paper_id)

    except Exception as e:
        logger.exception("Failed to process paper %d", paper_id)
        await crud.update_paper_status(
            paper_id, PaperStatus.failed, error_message=str(e)[:500]
        )
    finally:
        if local_path:
            pdf_extractor.cleanup_local_files(local_path)
        await pdf_extractor.delete_blob(blob_name)


async def process_pending_papers() -> int:
    papers = await crud.get_pending_papers()
    logger.info("Processing %d pending papers", len(papers))
    count = 0
    for p in papers:
        await process_paper(p.id, p.pdf_url, p.title, p.abstract)
        count += 1
    return count
