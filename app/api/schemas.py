from datetime import datetime

from pydantic import BaseModel, model_validator


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
    category_id: int
    category_ids: list[int]
    arxiv_id: str
    version: int
    title: str
    authors: list[str]
    abstract: str
    pdf_url: str
    published_at: datetime
    categories: list[str]
    status: str
    attempts: int
    error_message: str | None
    created_at: datetime

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def _first_category(self):
        # category_id is the first of the categories the paper belongs to
        self.category_ids = sorted(self.category_ids)
        if self.category_ids:
            self.category_id = self.category_ids[0]
        return self


class RunResponse(BaseModel):
    id: int
    started_at: datetime
    finished_at: datetime | None
    new_papers: int
    updated: int
    skipped: int
    processed: int
    failed: int

    model_config = {"from_attributes": True}


class StatusResponse(BaseModel):
    running: bool
    papers: dict[str, int]
    run: RunResponse | None


class AnalysisResponse(BaseModel):
    id: int
    paper_id: int
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
