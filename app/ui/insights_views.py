"""The insights pages under /ui: topics, one topic, trends per feed, weekly rankings.

They read the result folder through app.insights.store. A home without a result still gets the
page frame with a short note, never an error. Papers in a result are named by arXiv id, so
links to them go through /ui/arxiv/<id>, which finds the paper page by its database id.
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import home
from app.db import crud
from app.insights import store

logger = logging.getLogger(__name__)

insights_ui_router = APIRouter(prefix="/ui")
# The folder the other pages use; found through the project folder like theirs.
templates = Jinja2Templates(directory=home.ROOT / "app" / "ui" / "templates")

# A topic page lists this many of its closest members.
TOPIC_MEMBERS = 50
# The weekly page shows the papers of the first ranks, as the digest pages do.
WEEKLY_RANKS = 10


def _page(request: Request, name: str, **context):
    return templates.TemplateResponse(request=request, name=name, context=context)


def _feeds() -> list[str]:
    return list(store.stats().get("inputs", {}).get("feeds", []))


@insights_ui_router.get("/topics", response_class=HTMLResponse)
async def topics_page(request: Request):
    try:
        topics = sorted(store.topics(), key=lambda t: t["topic"])
    except store.NoResult:
        return _page(request, "topics.html", empty=True)
    return _page(request, "topics.html", empty=False, topics=topics)


@insights_ui_router.get("/topics/{topic}", response_class=HTMLResponse)
async def topic_page(request: Request, topic: int):
    try:
        found = store.topic(topic)
    except store.NoResult:
        return _page(request, "topic.html", empty=True)
    if found is None:
        logger.warning("Topic %d not found", topic)
        return PlainTextResponse("Topic not found", status_code=404)
    return _page(
        request, "topic.html", empty=False, topic=found,
        members=found["members"][:TOPIC_MEMBERS], member_count=len(found["members"]),
    )


@insights_ui_router.get("/trends", response_class=HTMLResponse)
async def trends_first_page(request: Request):
    return _trends(request, None)


@insights_ui_router.get("/trends/{feed}", response_class=HTMLResponse)
async def trends_page(request: Request, feed: str):
    return _trends(request, feed)


def _trends(request: Request, feed: str | None):
    try:
        feeds = _feeds()
        chosen = feed if feed is not None else (feeds[0] if feeds else None)
        found = store.trends(chosen) if chosen is not None else None
    except store.NoResult:
        return _page(request, "trends.html", empty=True)
    if feed is not None and found is None:
        logger.warning("No trends for %s", feed)
        return PlainTextResponse(f"No trends for {feed}", status_code=404)
    weeks = []
    topics = []
    if found is not None:
        weeks = list(zip(found["weeks"], found["papers"], found["analysed"]))
        for entry in found["topics"]:
            numerator, denominator = entry["slope"]
            topics.append({**entry, "per_week": numerator / denominator})
    return _page(
        request, "trends.html", empty=False, feeds=feeds, feed=chosen,
        trends=found, weeks=weeks, topics=topics,
    )


@insights_ui_router.get("/weekly", response_class=HTMLResponse)
async def weekly_first_page(request: Request):
    return _weekly(request, None, None)


@insights_ui_router.get("/weekly/{feed}", response_class=HTMLResponse)
async def weekly_feed_page(request: Request, feed: str):
    return _weekly(request, feed, None)


@insights_ui_router.get("/weekly/{feed}/{week}", response_class=HTMLResponse)
async def weekly_page(request: Request, feed: str, week: str):
    return _weekly(request, feed, week)


def _weekly(request: Request, feed: str | None, week: str | None):
    try:
        feeds = _feeds()
        chosen = feed if feed is not None else (feeds[0] if feeds else None)
        weeks = store.weeks(chosen) if chosen is not None else []
        if feed is not None and feed not in feeds and not weeks:
            return _no_digest(feed, week)
        if week is None and not weeks:
            # A feed of the library without a ranked week yet: the frame and a note.
            return _page(request, "weekly.html", empty=False, feeds=feeds, feed=chosen, digest=None)
        shown = week if week is not None else weeks[-1]
        digest = store.digest(chosen, shown)
    except store.NoResult:
        return _page(request, "weekly.html", empty=True)
    if digest is None:
        return _no_digest(chosen, shown)
    at = weeks.index(shown) if shown in weeks else None
    before = weeks[at - 1] if at is not None and at > 0 else None
    after = weeks[at + 1] if at is not None and at + 1 < len(weeks) else None
    papers = [p for p in digest["papers"] if p["rank"] <= WEEKLY_RANKS]
    return _page(
        request, "weekly.html", empty=False, feeds=feeds, feed=chosen, digest=digest,
        papers=papers, before=before, after=after,
    )


def _no_digest(feed: str, week: str | None):
    text = f"No digest for {feed} {week}" if week is not None else f"No digest for {feed}"
    logger.warning("%s", text)
    return PlainTextResponse(text, status_code=404)


@insights_ui_router.get("/arxiv/{arxiv_id:path}")
async def arxiv_paper(arxiv_id: str):
    paper = await crud.get_paper_by_arxiv_id(arxiv_id)
    if paper is None:
        logger.warning("Paper %s not found", arxiv_id)
        return PlainTextResponse("Paper not found", status_code=404)
    return RedirectResponse(f"/ui/paper/{paper.id}", status_code=303)
