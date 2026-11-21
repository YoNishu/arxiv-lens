"""Tables of the tracker: subscribed categories, papers with their versions, texts and timeline,
analyses, cycle records and chat conversations.

Every stored time comes from app.clock, so simulated time reaches the defaults as well. The
schema of an existing database is brought here by the recorded steps of app/db/migrate.py.
"""
import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app import clock


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
    added_at: Mapped[datetime] = mapped_column(UTCDateTime, default=clock.now)

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
    # arXiv id without its version suffix, e.g. 2401.90001; the current version is in `version`.
    arxiv_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # Title, authors, abstract and the fields below describe the current version.
    title: Mapped[str] = mapped_column(String, nullable=False)
    authors: Mapped[list] = mapped_column(JSON, nullable=False)
    abstract: Mapped[str] = mapped_column(Text, nullable=False)
    pdf_url: Mapped[str] = mapped_column(String, nullable=False)
    # First submission; a replacement leaves it alone.
    published_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    # The API's `updated` of the current version; unknown for rows stored before it was kept.
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    categories: Mapped[list] = mapped_column(JSON, nullable=False)
    journal_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    doi: Mapped[str | None] = mapped_column(String, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    # When the tracker first saw the current version withdrawn; a withdrawn paper is not analysed.
    withdrawn_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    status: Mapped[PaperStatus] = mapped_column(
        Enum(PaperStatus), default=PaperStatus.pending, nullable=False
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Number of processing tries since the paper was stored, last retried or last replaced.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Lease of the worker analysing the paper: its token and the clock's time of the last renewal.
    lease_token: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=clock.now)

    category = relationship("Category", back_populates="papers")
    analysis = relationship("Analysis", back_populates="paper", uselist=False, cascade="all, delete-orphan")
    category_links = relationship(
        "PaperCategory", lazy="selectin", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def category_ids(self) -> list[int]:
        return sorted({link.category_id for link in self.category_links})


class PaperVersion(Base):
    """What one stored version of a paper said. Versions the tracker never saw have no row."""

    __tablename__ = "paper_versions"
    __table_args__ = (UniqueConstraint("paper_id", "version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    abstract: Mapped[str] = mapped_column(Text, nullable=False)
    authors: Mapped[list] = mapped_column(JSON, nullable=False)
    categories: Mapped[list] = mapped_column(JSON, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=clock.now)


class PaperEvent(Base):
    """One entry of a paper's timeline; the id gives the order."""

    __tablename__ = "paper_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # The feed whose listing led to the entry; empty for processing entries.
    feed: Mapped[str | None] = mapped_column(String, nullable=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=clock.now)


class PaperText(Base):
    """The stored text of a paper: the file in the home's texts folder and what it should hold."""

    __tablename__ = "paper_texts"

    paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    chars: Mapped[int] = mapped_column(Integer, nullable=False)
    # pdf, or abstract when the PDF gave no text
    extraction: Mapped[str] = mapped_column(String(8), nullable=False)
    extracted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=clock.now)


class Analysis(Base):
    __tablename__ = "analyses"
    __table_args__ = (UniqueConstraint("paper_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), nullable=False)
    # The version of the paper the analysis describes; older than papers.version after a replacement.
    paper_version: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    key_contributions: Mapped[list] = mapped_column(JSON, nullable=False)
    methodology: Mapped[str] = mapped_column(Text, nullable=False)
    limitations: Mapped[str | None] = mapped_column(Text, nullable=True)
    keywords: Mapped[list] = mapped_column(JSON, nullable=False)
    novelty_score: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=clock.now)

    paper = relationship("Paper", back_populates="analysis")


# ---------------------------------------------------------------------------
# Ingestion bookkeeping
# ---------------------------------------------------------------------------

class Run(Base):
    """One poll-and-process cycle."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=clock.now, nullable=False)
    # Stamped after every feed and every paper: a run whose stamp is older than the lease
    # belongs to a process that is gone.
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    new_papers: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    feeds = relationship(
        "RunFeed",
        order_by="RunFeed.feed",
        lazy="selectin",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class RunFeed(Base):
    """What one cycle did with one feed, kept also when the feed failed half-way."""

    __tablename__ = "run_feeds"

    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    # The arXiv code, not the category id: the category may be removed later.
    feed: Mapped[str] = mapped_column(String, primary_key=True)
    listed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_papers: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class SeededCategory(Base):
    """arXiv codes from the seed list that were already offered to this database.

    A code is seeded once: if its category is removed later, the next cycles leave it removed.
    """

    __tablename__ = "seeded_categories"

    arxiv_code: Mapped[str] = mapped_column(String, primary_key=True)
    seeded_at: Mapped[datetime] = mapped_column(UTCDateTime, default=clock.now)


class SchemaVersion(Base):
    """One row per schema step applied to this database (app/db/migrate.py)."""

    __tablename__ = "schema_version"

    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    applied_at: Mapped[datetime] = mapped_column(UTCDateTime, default=clock.now)


# ---------------------------------------------------------------------------
# Chat domain
# ---------------------------------------------------------------------------

class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String, nullable=False, default="New Chat")
    research_interest: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=clock.now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=clock.now, onupdate=clock.now)

    messages = relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by=lambda: [Message.created_at, Message.id],
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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=clock.now)

    conversation = relationship("Conversation", back_populates="messages")
