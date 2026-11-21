"""Request and response models of the REST routes in app/api/routes.py.

Responses are read straight from the database rows (from_attributes), so a model lists the
columns a client gets to see.
"""
from datetime import datetime

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class CategoryCreate(BaseModel):
    arxiv_code: str
    name: str | None = None
    description: str | None = None


class ChatRequest(BaseModel):
    conversation_id: int | None = None
    message: str
    research_interest: str | None = None


class ConversationCreate(BaseModel):
    title: str = "New Chat"
    research_interest: str | None = None


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class CategoryResponse(BaseModel):
    id: int
    name: str
    arxiv_code: str
    description: str | None
    added_at: datetime

    model_config = {"from_attributes": True}


class PaperResponse(BaseModel):
    id: int
    # The paper's lowest linked category; category_ids lists all of them, ascending.
    category_id: int
    category_ids: list[int]
    arxiv_id: str
    version: int
    title: str
    authors: list[str]
    abstract: str
    pdf_url: str
    published_at: datetime
    updated_at: datetime | None
    categories: list[str]
    comment: str | None
    withdrawn_at: datetime | None
    status: str
    attempts: int
    error_message: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class RunFeedResponse(BaseModel):
    feed: str
    listed: int
    new_papers: int
    updated: int
    skipped: int
    error_message: str | None

    model_config = {"from_attributes": True}


class RunResponse(BaseModel):
    id: int
    started_at: datetime
    finished_at: datetime | None
    new_papers: int
    updated: int
    skipped: int
    processed: int
    failed: int
    error_message: str | None
    # One row per feed the cycle polled, by feed code.
    feeds: list[RunFeedResponse] = []

    model_config = {"from_attributes": True}


class StatusResponse(BaseModel):
    running: bool
    papers: dict[str, int]
    run: RunResponse | None


class AnalysisResponse(BaseModel):
    id: int
    paper_id: int
    # The version of the paper this analysis was made from.
    paper_version: int
    summary: str
    key_contributions: list[str]
    methodology: str
    limitations: str | None
    keywords: list[str]
    novelty_score: int
    created_at: datetime

    model_config = {"from_attributes": True}


class DigestItem(BaseModel):
    paper: PaperResponse
    category_name: str
    analysis: AnalysisResponse

    model_config = {"from_attributes": True}
