"""The pages under /ui: digest, categories, browsing, one paper, chat.

The paper page shows what the database knows of a paper (its versions' story in the timeline,
its stored text) and, when insights have been built, its related papers.
"""
import logging

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import home, ids
from app.db import crud, history
from app.insights import store
from app.services import cycle, textstore

logger = logging.getLogger(__name__)

ui_router = APIRouter(prefix="/ui")
# Found through the project folder, so the server starts from any working directory.
templates = Jinja2Templates(directory=home.ROOT / "app" / "ui" / "templates")

# The paper page shows this much of a stored text.
TEXT_PREVIEW_CHARS = 10_000


@ui_router.get("", response_class=HTMLResponse)
async def digest_page(request: Request):
    logger.debug("GET /ui — loading digest")
    papers = await crud.get_daily_digest()
    logger.info("Digest page: %d papers in last 24h", len(papers))
    return templates.TemplateResponse(
        request=request, name="digest.html",
        context={"papers": papers, "refreshing": cycle.is_running()},
    )


@ui_router.post("/refresh")
async def refresh(request: Request):
    logger.info("POST /ui/refresh — manual refresh triggered")
    # The cycle runs in the background; the page comes back at once.
    run_id, started = await cycle.start_cycle()
    logger.info("Manual refresh: cycle %d %s", run_id, "started" if started else "already running")
    return RedirectResponse("/ui", status_code=303)


@ui_router.get("/categories", response_class=HTMLResponse)
async def categories_page(request: Request):
    cats = await crud.get_all_categories()
    return templates.TemplateResponse(request=request, name="categories.html", context={"categories": cats})


@ui_router.post("/categories/add")
async def add_category_form(request: Request, arxiv_code: str = Form(...)):
    code = arxiv_code.strip()
    logger.info("POST /ui/categories/add — code=%s", code)
    if not ids.is_category_code(code):
        logger.warning("Not a category code: %s", arxiv_code)
        return RedirectResponse("/ui/categories", status_code=303)
    existing = await crud.get_category_by_code(code)
    if not existing:
        await crud.create_category(name=code, arxiv_code=code)
        # Poll the new feed in the background; if a cycle is running, one more follows it.
        await cycle.start_cycle(queue_if_running=True)
    return RedirectResponse("/ui/categories", status_code=303)


@ui_router.post("/categories/{category_id}/delete")
async def delete_category_form(category_id: int):
    logger.info("POST /ui/categories/%d/delete", category_id)
    await crud.delete_category(category_id)
    return RedirectResponse("/ui/categories", status_code=303)


@ui_router.get("/papers", response_class=HTMLResponse)
async def browse_papers_page(
    request: Request,
    q: str = Query("", alias="q"),
    category: int = Query(0, alias="category"),
    min_score: int = Query(0, alias="min_score"),
    sort: str = Query("newest", alias="sort"),
    page: int = Query(1, alias="page"),
):
    logger.info("GET /ui/papers — q=%s category=%s score>=%s sort=%s page=%d", q, category, min_score, sort, page)
    cat_id = category if category > 0 else None
    score = min_score if min_score > 0 else None
    papers, total = await crud.browse_papers(
        query=q or None, category_id=cat_id, min_score=score, sort=sort, page=page,
    )
    categories = await crud.get_all_categories()
    total_pages = max(1, (total + 19) // 20)
    return templates.TemplateResponse(
        request=request,
        name="browse.html",
        context={
            "papers": papers, "total": total, "page": page, "total_pages": total_pages,
            "q": q, "category": category, "min_score": min_score, "sort": sort,
            "categories": categories,
        },
    )


async def _feeds_of(paper) -> list[str]:
    """The codes of the categories whose feeds listed the paper, sorted."""
    codes = {c.id: c.arxiv_code for c in await crud.get_all_categories()}
    return sorted(codes[i] for i in paper.category_ids if i in codes)


def _stored_text(paper) -> str | None:
    """The text of the current version, or else the one the stored analysis was made from."""
    text = textstore.read(paper.arxiv_id, paper.version)
    if text is None and paper.analysis is not None and paper.analysis.paper_version != paper.version:
        text = textstore.read(paper.arxiv_id, paper.analysis.paper_version)
    return text


@ui_router.get("/paper/{paper_id}", response_class=HTMLResponse)
async def paper_detail_page(request: Request, paper_id: int):
    logger.debug("GET /ui/paper/%d", paper_id)
    paper = await crud.get_paper(paper_id)
    if not paper:
        logger.warning("Paper %d not found", paper_id)
        return HTMLResponse("Paper not found", status_code=404)
    text = _stored_text(paper)
    try:
        related = store.related(paper.arxiv_id)
    except store.NoResult:
        related = None
    return templates.TemplateResponse(
        request=request,
        name="paper.html",
        context={
            "paper": paper,
            "feeds": await _feeds_of(paper),
            "timeline": await history.timeline(paper.id),
            "text": text[:TEXT_PREVIEW_CHARS] if text is not None else None,
            "text_cut": text is not None and len(text) > TEXT_PREVIEW_CHARS,
            "related": related,
        },
    )


@ui_router.post("/paper/{paper_id}/retry")
async def retry_paper_form(paper_id: int):
    logger.info("POST /ui/paper/%d/retry", paper_id)
    # A paper that is not failed (any more) is left as it is; the page shows its state.
    await crud.retry_paper(paper_id)
    return RedirectResponse(f"/ui/paper/{paper_id}", status_code=303)


@ui_router.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    return templates.TemplateResponse(request=request, name="chat.html", context={"conversation_id": None})


@ui_router.get("/chat/{conversation_id}", response_class=HTMLResponse)
async def chat_with_conversation(request: Request, conversation_id: int):
    return templates.TemplateResponse(
        request=request, name="chat.html", context={"conversation_id": conversation_id}
    )
