"""REST routes over the insights result folder: stats, topics, trends, related papers, weekly digests.

Every route is a thin projection of app.insights.store: numbers are passed on as the files hold
them and lists keep the store's order. Nothing is built on demand and the database is not opened;
a home without a result answers 404.
"""
import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.insights import store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/insights")

NO_RESULT = "No insights have been built"


class AsOf(BaseModel):
    day: str
    week: str
    month: str


class Inputs(BaseModel):
    papers: int
    analysed: int
    failed: int
    pending: int
    withdrawn: int
    feeds: list[str]


class StatsResponse(BaseModel):
    as_of: AsOf
    settings: str
    inputs: Inputs
    stages: dict[str, dict[str, int]]
    # The number of files the build wrote and stats.json names.
    files: int


class PaperRef(BaseModel):
    arxiv_id: str
    title: str | None


class TopicItem(BaseModel):
    topic: int
    name: str
    blurb: str
    source: str
    size: int
    recent: int
    terms: list[str]


class TopicMember(BaseModel):
    arxiv_id: str
    title: str | None
    sim: float


class TopicDetail(TopicItem):
    exemplars: list[PaperRef]
    members: list[TopicMember]
    # Members before the cut to `limit`.
    member_count: int


class RisingTerm(BaseModel):
    term: str
    recent: int
    baseline: int
    z: float


class Burst(BaseModel):
    term: str
    count: int
    before: int


class TopicMomentum(BaseModel):
    topic: int
    name: str | None
    counts: list[int]
    # Numerator and denominator of the weekly slope over the six counts.
    slope: list[int]


class TrendsResponse(BaseModel):
    feed: str
    weeks: list[str]
    papers: list[int]
    analysed: list[int]
    rising: list[RisingTerm]
    bursts: list[Burst]
    topics: list[TopicMomentum]


class TopicRef(BaseModel):
    topic: int
    name: str | None


class SimilarPaper(BaseModel):
    arxiv_id: str
    title: str | None
    score: float


class RelatedResponse(BaseModel):
    arxiv_id: str
    title: str
    state: str
    withdrawn: bool
    feeds: list[str]
    topic: TopicRef | None
    similar: list[SimilarPaper]
    cites: list[PaperRef]
    cited_by: list[PaperRef]
    cited_by_count: int
    external: int


class RankedPaper(BaseModel):
    rank: int
    arxiv_id: str
    title: str | None
    authors: list[str]
    score: float
    novelty: int
    grounding: int
    heat: int
    percentile: int
    topic: str | None


class WeeklyDigest(BaseModel):
    feed: str
    week: str
    closed: bool
    ranked: int
    # Every member of the week's ranking, not only the ones its page shows.
    papers: list[RankedPaper]


def _read(look_up, *args):
    """Call a store function; a folder without a result is a 404 for every route."""
    try:
        return look_up(*args)
    except store.NoResult:
        logger.info("No insights result to answer from")
        raise HTTPException(status_code=404, detail=NO_RESULT) from None


@router.get("/stats", response_model=StatsResponse)
async def get_stats():
    stats = _read(store.stats)
    return {
        "as_of": stats["as_of"],
        "settings": stats["settings"],
        "inputs": stats["inputs"],
        "stages": stats["stages"],
        "files": len(stats.get("files", {})),
    }


@router.get("/topics", response_model=list[TopicItem])
async def list_topics():
    topics = _read(store.topics)
    return sorted(topics, key=lambda t: t["topic"])


@router.get("/topics/{topic}", response_model=TopicDetail)
async def get_topic(topic: int, limit: int = Query(50, ge=1, le=500)):
    found = _read(store.topic, topic)
    if found is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    members = found["members"]
    found["member_count"] = len(members)
    found["members"] = members[:limit]
    return found


@router.get("/trends/{feed}", response_model=TrendsResponse)
async def get_trends(feed: str):
    found = _read(store.trends, feed)
    if found is None:
        raise HTTPException(status_code=404, detail=f"No trends for {feed}")
    return found


@router.get("/papers/{arxiv_id:path}/related", response_model=RelatedResponse)
async def get_related(arxiv_id: str):
    found = _read(store.related, arxiv_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Paper not found")
    return found


@router.get("/digests/{feed}/{week}", response_model=WeeklyDigest)
async def get_digest(feed: str, week: str):
    found = _read(store.digest, feed, week)
    if found is None:
        raise HTTPException(status_code=404, detail=f"No digest for {feed} {week}")
    return found
