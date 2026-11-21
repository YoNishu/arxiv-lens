import json
import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.api.schemas import (
    AnalysisResponse,
    CategoryCreate,
    CategoryResponse,
    ChatRequest,
    ConversationCreate,
    DigestItem,
    PaperResponse,
    RunResponse,
    StatusResponse,
)
from app.db import crud
from app.services import chat_agent, cycle

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

@router.get("/categories", response_model=list[CategoryResponse])
async def list_categories():
    logger.debug("GET /categories")
    return await crud.get_all_categories()


@router.post("/categories", response_model=CategoryResponse, status_code=201)
async def add_category(body: CategoryCreate):
    logger.info("POST /categories — arxiv_code=%s", body.arxiv_code)
    existing = await crud.get_category_by_code(body.arxiv_code)
    if existing:
        raise HTTPException(409, f"Already subscribed to {body.arxiv_code}")
    cat = await crud.create_category(
        name=body.name or body.arxiv_code,
        arxiv_code=body.arxiv_code,
        description=body.description,
    )
    # Poll the new feed in the background; if a cycle is running, one more follows it.
    await cycle.start_cycle(queue_if_running=True)
    return cat


@router.delete("/categories/{category_id}", status_code=204)
async def remove_category(category_id: int):
    logger.info("DELETE /categories/%d", category_id)
    await crud.delete_category(category_id)


# ---------------------------------------------------------------------------
# Papers
# ---------------------------------------------------------------------------

@router.get("/papers", response_model=list[PaperResponse])
async def list_papers(
    category_id: int | None = Query(None),
    status: str | None = Query(None),
):
    logger.debug("GET /papers category_id=%s status=%s", category_id, status)
    return await crud.get_papers(category_id=category_id, status=status)


@router.get("/papers/{paper_id}/analysis", response_model=AnalysisResponse)
async def get_paper_analysis(paper_id: int):
    logger.debug("GET /papers/%d/analysis", paper_id)
    analysis = await crud.get_analysis_by_paper(paper_id)
    if not analysis:
        logger.warning("Analysis not found for paper %d", paper_id)
        raise HTTPException(404, "Analysis not found")
    return analysis


@router.post("/papers/{paper_id}/retry", response_model=PaperResponse, status_code=202)
async def retry_paper(paper_id: int):
    """Give a failed paper a fresh start: pending, with 0 attempts. The next cycle picks it up."""
    logger.info("POST /papers/%d/retry", paper_id)
    outcome = await crud.retry_paper(paper_id)
    if outcome == "not_found":
        raise HTTPException(404, "Paper not found")
    if outcome == "not_failed":
        raise HTTPException(409, "Only a failed paper can be retried")
    return await crud.get_paper(paper_id)


# ---------------------------------------------------------------------------
# Digest & processing
# ---------------------------------------------------------------------------

@router.get("/daily-digest", response_model=list[DigestItem])
async def daily_digest():
    papers = await crud.get_daily_digest()
    items = []
    for p in papers:
        items.append(
            DigestItem(
                paper=PaperResponse.model_validate(p),
                category_name=p.category.name if p.category else "Unknown",
                analysis=AnalysisResponse.model_validate(p.analysis),
            )
        )
    return items


@router.post("/process", status_code=202)
async def trigger_process():
    """Start a poll+process cycle in the background and answer at once.

    While a cycle is running in this process, no second one is started: the answer carries the
    running cycle's run_id and started=false. Progress and results are in GET /status.
    """
    logger.info("POST /process — manual poll+process triggered")
    run_id, started = await cycle.start_cycle()
    run = await crud.get_run(run_id)
    return {
        "message": "Cycle started" if started else "A cycle is already running",
        "run_id": run_id,
        "started": started,
        # Counters of the cycle so far (it has only just begun when started is true).
        "new_papers_found": run.new_papers if run else 0,
        "papers_processed": run.processed if run else 0,
    }


@router.get("/status", response_model=StatusResponse)
async def status():
    """Whether a cycle is running in this process, paper counts per status, the latest cycle."""
    # Read `running` before the database: a cycle writes its end to the runs table before it
    # stops counting as running, so running=false always comes with the finished record.
    running = cycle.is_running()
    run = await crud.get_latest_run()
    papers = await crud.count_papers_by_status()
    return StatusResponse(
        running=running,
        papers=papers,
        run=RunResponse.model_validate(run) if run else None,
    )


@router.get("/last-updated")
async def last_updated():
    papers = await crud.get_daily_digest()
    if papers and papers[0].analysis:
        return {"last_updated": papers[0].analysis.created_at.isoformat()}
    return {"last_updated": None}


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

@router.get("/conversations")
async def list_conversations():
    convs = await crud.get_conversations()
    return [
        {
            "id": c.id,
            "title": c.title,
            "research_interest": c.research_interest,
            "created_at": c.created_at.isoformat(),
            "updated_at": c.updated_at.isoformat(),
        }
        for c in convs
    ]


@router.post("/conversations", status_code=201)
async def create_conversation(body: ConversationCreate):
    conv = await crud.create_conversation(
        title=body.title, research_interest=body.research_interest
    )
    return {"id": conv.id, "title": conv.title, "research_interest": conv.research_interest}


@router.get("/conversations/{conversation_id}/messages")
async def get_messages(conversation_id: int):
    conv = await crud.get_conversation(conversation_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    msgs = await crud.get_conversation_messages(conversation_id)
    return [
        {
            "id": m.id,
            "role": m.role.value,
            "content": m.content,
            "tool_calls": m.tool_calls,
            "tool_call_id": m.tool_call_id,
            "created_at": m.created_at.isoformat(),
        }
        for m in msgs
    ]


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: int):
    await crud.delete_conversation(conversation_id)


@router.put("/conversations/{conversation_id}/research-interest")
async def update_research_interest(conversation_id: int, body: dict):
    conv = await crud.get_conversation(conversation_id)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    await crud.update_conversation_research_interest(
        conversation_id, body.get("research_interest", "")
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Chat (SSE)
# ---------------------------------------------------------------------------

@router.post("/chat")
async def chat_endpoint(body: ChatRequest):
    logger.info("POST /chat — conv_id=%s, message_len=%d", body.conversation_id, len(body.message))
    conv_id = body.conversation_id
    if not conv_id:
        conv = await crud.create_conversation(research_interest=body.research_interest)
        conv_id = conv.id
    elif body.research_interest:
        await crud.update_conversation_research_interest(conv_id, body.research_interest)

    async def event_stream():
        yield f"data: {json.dumps({'type': 'meta', 'conversation_id': conv_id})}\n\n"
        async for chunk in chat_agent.chat_stream(conv_id, body.message):
            yield f"data: {chunk}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
