import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC timestamps on every backend.

    PostgreSQL keeps the offset itself (TIMESTAMP WITH TIME ZONE). SQLite stores plain text and
    hands back naive values, so values are normalised to UTC on the way in and marked as UTC on
    the way out. The column type emitted for PostgreSQL is unchanged.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PaperStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    done = "done"
    failed = "failed"


class MessageRole(str, enum.Enum):
    user = "user"
    assistant = "assistant"
    tool = "tool"


# ---------------------------------------------------------------------------
# Content domain
# ---------------------------------------------------------------------------

class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    arxiv_code: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    added_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    # Papers whose first (lowest id) category is this one. Membership of a paper in all of its
    # categories lives in paper_categories; deleting a category goes through crud.delete_category.
    papers = relationship("Paper", back_populates="category", passive_deletes=True)


class PaperCategory(Base):
    """A paper belongs to every subscribed category whose feed has listed it."""

    __tablename__ = "paper_categories"

    paper_id: Mapped[int] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True
    )
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class Paper(Base):
    __tablename__ = "papers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # The first (lowest id) category the paper belongs to; kept in step with paper_categories.
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"), nullable=False)
    # arXiv id without its version suffix, e.g. 2401.00001; the version is in `version`.
    arxiv_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    title: Mapped[str] = mapped_column(String, nullable=False)
    authors: Mapped[list] = mapped_column(JSON, nullable=False)
    abstract: Mapped[str] = mapped_column(Text, nullable=False)
    pdf_url: Mapped[str] = mapped_column(String, nullable=False)
    published_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    categories: Mapped[list] = mapped_column(JSON, nullable=False)
    journal_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    doi: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[PaperStatus] = mapped_column(
        Enum(PaperStatus), default=PaperStatus.pending, nullable=False
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Number of processing tries since the paper was stored or last reset by a retry.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Lease of the worker analysing the paper: its token and the time it last renewed the lease.
    # The renewal time is kept twice: as UTC time, and as the monotonic clock reading of the
    # worker's machine (lease_host names the machine and its boot). Workers on the same machine
    # compare monotonic readings, which no clock adjustment can move; others compare UTC time.
    lease_token: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    lease_host: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_mono: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    category = relationship("Category", back_populates="papers")
    analysis = relationship("Analysis", back_populates="paper", uselist=False, cascade="all, delete-orphan")
    category_links = relationship(
        "PaperCategory", lazy="selectin", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def category_ids(self) -> list[int]:
        ids = sorted({link.category_id for link in self.category_links})
        return ids or [self.category_id]


class Analysis(Base):
    __tablename__ = "analyses"
    __table_args__ = (UniqueConstraint("paper_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), nullable=False)
    full_text: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    key_contributions: Mapped[list] = mapped_column(JSON, nullable=False)
    methodology: Mapped[str] = mapped_column(Text, nullable=False)
    limitations: Mapped[str | None] = mapped_column(Text, nullable=True)
    keywords: Mapped[list] = mapped_column(JSON, nullable=False)
    novelty_score: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    paper = relationship("Paper", back_populates="analysis")


# ---------------------------------------------------------------------------
# Ingestion bookkeeping
# ---------------------------------------------------------------------------

class Run(Base):
    """One poll-and-process cycle."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    new_papers: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class SeededCategory(Base):
    """arXiv codes from categories.yaml that were already offered to this database.

    A code is seeded once: if its category is removed later, the next cycles leave it removed.
    """

    __tablename__ = "seeded_categories"

    arxiv_code: Mapped[str] = mapped_column(String, primary_key=True)
    seeded_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# ---------------------------------------------------------------------------
# Chat domain
# ---------------------------------------------------------------------------

class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String, nullable=False, default="New Chat")
    research_interest: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    messages = relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[MessageRole] = mapped_column(Enum(MessageRole), nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_calls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    conversation = relationship("Conversation", back_populates="messages")
