import logging
import socket
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import and_, cast, delete, func, or_, select, update, String as SAString
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db.database import IS_SQLITE, async_session
from app.db.models import (
    Analysis,
    Category,
    Conversation,
    Message,
    MessageRole,
    Paper,
    PaperCategory,
    PaperStatus,
    Run,
    SeededCategory,
)

if IS_SQLITE:
    from sqlalchemy.dialects.sqlite import insert as _dialect_insert
else:
    from sqlalchemy.dialects.postgresql import insert as _dialect_insert

logger = logging.getLogger(__name__)

# A paper is tried this many times by the cycles before it stays failed.
MAX_ATTEMPTS = 3

_CHUNK = 500

_papers = Paper.__table__
_links = PaperCategory.__table__


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _chunks(items: list, size: int = _CHUNK):
    for start in range(0, len(items), size):
        yield items[start:start + size]


# ---------------------------------------------------------------------------
# Category CRUD
# ---------------------------------------------------------------------------

async def get_all_categories() -> list[Category]:
    async with async_session() as s:
        result = await s.execute(select(Category).order_by(Category.added_at.desc()))
        return list(result.scalars().all())


async def get_category_by_code(arxiv_code: str) -> Category | None:
    async with async_session() as s:
        result = await s.execute(select(Category).where(Category.arxiv_code == arxiv_code))
        return result.scalar_one_or_none()


async def create_category(name: str, arxiv_code: str, description: str | None = None) -> Category:
    async with async_session() as s:
        cat = Category(name=name, arxiv_code=arxiv_code, description=description)
        s.add(cat)
        await s.commit()
        await s.refresh(cat)
        logger.info("Created category id=%d code=%s", cat.id, arxiv_code)
        return cat


async def delete_category(category_id: int) -> None:
    """Delete a category, and with it only the papers that no other category lists."""
    async with async_session() as s:
        # Drop the memberships first; this also takes the write lock before anything is read.
        await s.execute(delete(_links).where(_links.c.category_id == category_id))

        # Papers that are still listed by another category move to their next first category.
        first_remaining = (
            select(func.min(_links.c.category_id))
            .where(_links.c.paper_id == _papers.c.id)
            .correlate(_papers)
            .scalar_subquery()
        )
        still_listed = (
            select(_links.c.paper_id)
            .where(_links.c.paper_id == _papers.c.id)
            .correlate(_papers)
            .exists()
        )
        await s.execute(
            update(_papers)
            .where(_papers.c.category_id == category_id, still_listed)
            .values(category_id=first_remaining)
        )

        # The rest belonged to this category alone.
        orphans = select(_papers.c.id).where(_papers.c.category_id == category_id)
        await s.execute(delete(Analysis.__table__).where(Analysis.__table__.c.paper_id.in_(orphans)))
        await s.execute(delete(_papers).where(_papers.c.category_id == category_id))
        await s.execute(delete(Category.__table__).where(Category.__table__.c.id == category_id))
        await s.commit()
        logger.info("Deleted category id=%d", category_id)


async def seed_categories(candidates: list[dict]) -> list[str]:
    """Create the categories.yaml categories this database has not been offered before.

    Each code is recorded when first seen, so a seeded category that is removed later is not
    created again. Returns the codes created now.
    """
    created: list[str] = []
    if not candidates:
        return created
    async with async_session() as s:
        for c in candidates:
            code = c["arxiv_code"]
            first_time = (
                await s.execute(
                    _dialect_insert(SeededCategory.__table__)
                    .values(arxiv_code=code, seeded_at=_utcnow())
                    .on_conflict_do_nothing(index_elements=["arxiv_code"])
                    .returning(SeededCategory.__table__.c.arxiv_code)
                )
            ).first()
            if not first_time:
                continue
            inserted = (
                await s.execute(
                    _dialect_insert(Category.__table__)
                    .values(
                        name=c.get("name") or code,
                        arxiv_code=code,
                        description=c.get("description"),
                        added_at=_utcnow(),
                    )
                    .on_conflict_do_nothing(index_elements=["arxiv_code"])
                    .returning(Category.__table__.c.id)
                )
            ).first()
            if inserted:
                created.append(code)
        await s.commit()
    return created


# ---------------------------------------------------------------------------
# Paper CRUD — ingestion
# ---------------------------------------------------------------------------

async def get_stored_versions(arxiv_ids: list[str]) -> dict[str, tuple[int, int]]:
    """Map each stored arXiv id (without version) to (paper id, stored version)."""
    found: dict[str, tuple[int, int]] = {}
    if not arxiv_ids:
        return found
    async with async_session() as s:
        for chunk in _chunks(list(arxiv_ids)):
            result = await s.execute(
                select(_papers.c.arxiv_id, _papers.c.id, _papers.c.version).where(
                    _papers.c.arxiv_id.in_(chunk)
                )
            )
            for arxiv_id, paper_id, version in result:
                found[arxiv_id] = (paper_id, version)
    return found


async def _link(s: AsyncSession, paper_ids: list[int], category_id: int) -> None:
    for paper_id in paper_ids:
        await s.execute(
            _dialect_insert(_links)
            .values(paper_id=paper_id, category_id=category_id)
            .on_conflict_do_nothing(index_elements=["paper_id", "category_id"])
        )
    # papers.category_id is the first (lowest id) category of the paper.
    for chunk in _chunks(paper_ids):
        await s.execute(
            update(_papers)
            .where(_papers.c.id.in_(chunk), _papers.c.category_id > category_id)
            .values(category_id=category_id)
        )


async def link_papers_to_category(paper_ids: list[int], category_id: int) -> None:
    """Record that the feed of a category has listed these stored papers."""
    if not paper_ids:
        return
    async with async_session() as s:
        await _link(s, list(paper_ids), category_id)
        await s.commit()


async def store_new_papers(papers: list[dict], category_id: int) -> int:
    """Store papers that are not stored yet and list all of them under the category.

    Safe against another cycle or process storing the same paper at the same time: a paper that
    is already there is left alone and only gains the category. Returns how many were inserted.
    """
    if not papers:
        return 0
    inserted = 0
    async with async_session() as s:
        paper_ids: list[int] = []
        for p in papers:
            values = {"category_id": category_id, "status": PaperStatus.pending, "attempts": 0,
                      "created_at": _utcnow(), **p}
            row = (
                await s.execute(
                    _dialect_insert(_papers)
                    .values(**values)
                    .on_conflict_do_nothing(index_elements=["arxiv_id"])
                    .returning(_papers.c.id)
                )
            ).first()
            if row:
                inserted += 1
                paper_ids.append(row[0])
            else:
                existing = (
                    await s.execute(select(_papers.c.id).where(_papers.c.arxiv_id == p["arxiv_id"]))
                ).first()
                if existing:
                    paper_ids.append(existing[0])
        await _link(s, paper_ids, category_id)
        await s.commit()
    logger.info("Stored %d new papers (of %d offered)", inserted, len(papers))
    return inserted


async def apply_replacement(
    paper_id: int, version: int, title: str, abstract: str, pdf_url: str
) -> bool:
    """Move a stored paper to a newer version; its status and analysis stay as they are."""
    async with async_session() as s:
        result = await s.execute(
            update(_papers)
            .where(_papers.c.id == paper_id, _papers.c.version < version)
            .values(version=version, title=title, abstract=abstract, pdf_url=pdf_url)
        )
        await s.commit()
        return result.rowcount == 1


# ---------------------------------------------------------------------------
# Paper CRUD — processing (claims, leases, attempts)
# ---------------------------------------------------------------------------

def _host_key() -> str | None:
    """Names this machine and its current boot, or None where that cannot be told.

    Processes with the same key read the same monotonic clock.
    """
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None
    return f"{socket.gethostname()}/{boot_id}" if boot_id else None


_HOST = _host_key()

_LEASE_CLEARED = {"lease_token": None, "lease_at": None, "lease_host": None, "lease_mono": None}


def _lease_stamp() -> dict:
    """The renewal time of a lease, on both clocks (see Paper.lease_at)."""
    return {"lease_at": _utcnow(), "lease_host": _HOST, "lease_mono": time.monotonic()}


def _lease_expired():
    """True for a lease that was not renewed for PROCESSING_LEASE_SECONDS.

    A lease stamped on this machine since its last boot is judged by the monotonic clock, which
    wall-clock adjustments cannot move; any other lease by UTC time.
    """
    lease = settings.processing_lease_seconds
    wall_cutoff = _utcnow() - timedelta(seconds=lease)
    by_wall_clock = or_(_papers.c.lease_at.is_(None), _papers.c.lease_at < wall_cutoff)
    if _HOST is None:
        return by_wall_clock
    stamped_here = and_(_papers.c.lease_host == _HOST, _papers.c.lease_mono.is_not(None))
    stamped_elsewhere = or_(
        _papers.c.lease_host.is_(None), _papers.c.lease_host != _HOST, _papers.c.lease_mono.is_(None)
    )
    return or_(
        and_(stamped_here, _papers.c.lease_mono < time.monotonic() - lease),
        and_(stamped_elsewhere, by_wall_clock),
    )


def _claimable():
    """What a cycle may take: a pending paper, a failed one with tries left, or a paper in work
    whose lease ran out (its worker is gone; such a paper is taken over whatever its count)."""
    return or_(
        _papers.c.status == PaperStatus.pending,
        and_(_papers.c.status == PaperStatus.failed, _papers.c.attempts < MAX_ATTEMPTS),
        and_(_papers.c.status == PaperStatus.processing, _lease_expired()),
    )


async def get_claimable_paper_ids() -> list[int]:
    """Papers a cycle should try now: pending, failed with tries left, or abandoned mid-way."""
    async with async_session() as s:
        result = await s.execute(
            select(_papers.c.id)
            .where(_claimable())
            .order_by(_papers.c.created_at.asc(), _papers.c.id.asc())
        )
        return [row[0] for row in result]


async def claim_paper(paper_id: int, token: str) -> Paper | None:
    """Take the paper for one worker. Atomic: of several claimants exactly one gets it.

    Counts the try in attempts and starts the lease. Returns the paper as stored at that
    moment, or None when it is not there to be taken (done, exhausted, or another worker's).
    """
    async with async_session() as s:
        result = await s.execute(
            update(_papers)
            .where(_papers.c.id == paper_id, _claimable())
            .values(
                status=PaperStatus.processing,
                attempts=_papers.c.attempts + 1,
                lease_token=token,
                **_lease_stamp(),
            )
        )
        await s.commit()
        if result.rowcount != 1:
            return None
        return await s.get(Paper, paper_id)


async def renew_lease(paper_id: int, token: str) -> bool:
    async with async_session() as s:
        result = await s.execute(
            update(_papers)
            .where(
                _papers.c.id == paper_id,
                _papers.c.lease_token == token,
                _papers.c.status == PaperStatus.processing,
            )
            .values(**_lease_stamp())
        )
        await s.commit()
        return result.rowcount == 1


async def complete_paper(paper_id: int, token: str, full_text: str, analysis: dict) -> bool:
    """Store the analysis and mark the paper done, only if this worker still holds it."""
    async with async_session() as s:
        result = await s.execute(
            update(_papers)
            .where(
                _papers.c.id == paper_id,
                _papers.c.lease_token == token,
                _papers.c.status == PaperStatus.processing,
            )
            .values(status=PaperStatus.done, error_message=None, **_LEASE_CLEARED)
        )
        if result.rowcount != 1:
            await s.rollback()
            return False
        await s.execute(delete(Analysis.__table__).where(Analysis.__table__.c.paper_id == paper_id))
        s.add(
            Analysis(
                paper_id=paper_id,
                full_text=full_text,
                summary=analysis["summary"],
                key_contributions=analysis["key_contributions"],
                methodology=analysis["methodology"],
                limitations=analysis.get("limitations"),
                keywords=analysis["keywords"],
                novelty_score=analysis["novelty_score"],
            )
        )
        await s.commit()
        logger.info(
            "Created analysis for paper %d — score=%d, keywords=%s",
            paper_id, analysis["novelty_score"], analysis["keywords"],
        )
        return True


async def fail_paper(paper_id: int, token: str, error_message: str) -> bool:
    async with async_session() as s:
        result = await s.execute(
            update(_papers)
            .where(
                _papers.c.id == paper_id,
                _papers.c.lease_token == token,
                _papers.c.status == PaperStatus.processing,
            )
            .values(status=PaperStatus.failed, error_message=error_message, **_LEASE_CLEARED)
        )
        await s.commit()
        return result.rowcount == 1


async def retry_paper(paper_id: int) -> str:
    """Make a failed paper pending again with 0 attempts. Returns ok, not_found or not_failed."""
    async with async_session() as s:
        result = await s.execute(
            update(_papers)
            .where(_papers.c.id == paper_id, _papers.c.status == PaperStatus.failed)
            .values(status=PaperStatus.pending, attempts=0, error_message=None, **_LEASE_CLEARED)
        )
        await s.commit()
        if result.rowcount == 1:
            return "ok"
        exists = (await s.execute(select(_papers.c.id).where(_papers.c.id == paper_id))).first()
        return "not_failed" if exists else "not_found"


async def count_papers_by_status() -> dict[str, int]:
    counts = {status.value: 0 for status in PaperStatus}
    async with async_session() as s:
        result = await s.execute(select(Paper.status, func.count(Paper.id)).group_by(Paper.status))
        for status, n in result:
            counts[status.value if isinstance(status, PaperStatus) else str(status)] = n
    return counts


def _in_category(category_id: int):
    return Paper.id.in_(select(PaperCategory.paper_id).where(PaperCategory.category_id == category_id))


async def get_papers(
    category_id: int | None = None, status: str | None = None
) -> list[Paper]:
    async with async_session() as s:
        stmt = select(Paper).options(selectinload(Paper.category), selectinload(Paper.analysis))
        if category_id:
            stmt = stmt.where(_in_category(category_id))
        if status:
            stmt = stmt.where(Paper.status == PaperStatus(status))
        stmt = stmt.order_by(Paper.published_at.desc())
        result = await s.execute(stmt)
        return list(result.scalars().all())


async def get_paper(paper_id: int) -> Paper | None:
    async with async_session() as s:
        result = await s.execute(
            select(Paper)
            .options(selectinload(Paper.category), selectinload(Paper.analysis))
            .where(Paper.id == paper_id)
        )
        return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Analysis CRUD
# ---------------------------------------------------------------------------

async def get_analysis_by_paper(paper_id: int) -> Analysis | None:
    async with async_session() as s:
        result = await s.execute(select(Analysis).where(Analysis.paper_id == paper_id))
        return result.scalar_one_or_none()


async def get_daily_digest() -> list[Paper]:
    async with async_session() as s:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        result = await s.execute(
            select(Paper)
            .join(Paper.analysis)
            .options(selectinload(Paper.analysis), selectinload(Paper.category))
            .where(Analysis.created_at >= cutoff)
            .order_by(Analysis.novelty_score.desc())
        )
        papers = list(result.scalars().all())
        # Fallback: if no papers in last 24h, return the latest 20
        if not papers:
            result = await s.execute(
                select(Paper)
                .join(Paper.analysis)
                .options(selectinload(Paper.analysis), selectinload(Paper.category))
                .order_by(Analysis.created_at.desc())
                .limit(20)
            )
            papers = list(result.scalars().all())
        return papers


async def search_papers(query: str) -> list[Paper]:
    logger.debug("Searching papers for query='%s'", query)
    async with async_session() as s:
        pattern = f"%{query}%"
        result = await s.execute(
            select(Paper)
            .join(Paper.analysis)
            .options(selectinload(Paper.analysis), selectinload(Paper.category))
            .where(
                Paper.title.ilike(pattern)
                | Analysis.summary.ilike(pattern)
                | cast(Analysis.keywords, SAString).ilike(pattern)
                | Paper.abstract.ilike(pattern)
            )
            .order_by(Analysis.novelty_score.desc())
        )
        return list(result.scalars().unique().all())


async def browse_papers(
    query: str | None = None,
    category_id: int | None = None,
    min_score: int | None = None,
    sort: str = "newest",
    page: int = 1,
    per_page: int = 20,
) -> tuple[list[Paper], int]:
    """Browse all papers with search, category filter, score filter, sorting, and pagination."""
    async with async_session() as s:
        stmt = (
            select(Paper)
            .join(Paper.analysis)
            .options(selectinload(Paper.analysis), selectinload(Paper.category))
        )
        count_stmt = select(func.count(Paper.id)).join(Paper.analysis)

        if query:
            pattern = f"%{query}%"
            filt = (
                Paper.title.ilike(pattern)
                | Analysis.summary.ilike(pattern)
                | cast(Analysis.keywords, SAString).ilike(pattern)
                | Paper.abstract.ilike(pattern)
            )
            stmt = stmt.where(filt)
            count_stmt = count_stmt.where(filt)

        if category_id:
            stmt = stmt.where(_in_category(category_id))
            count_stmt = count_stmt.where(_in_category(category_id))

        if min_score is not None:
            stmt = stmt.where(Analysis.novelty_score >= min_score)
            count_stmt = count_stmt.where(Analysis.novelty_score >= min_score)

        if sort == "score":
            stmt = stmt.order_by(Analysis.novelty_score.desc())
        elif sort == "oldest":
            stmt = stmt.order_by(Paper.published_at.asc())
        else:  # newest
            stmt = stmt.order_by(Paper.published_at.desc())

        total = (await s.execute(count_stmt)).scalar() or 0
        offset = (page - 1) * per_page
        stmt = stmt.offset(offset).limit(per_page)

        result = await s.execute(stmt)
        return list(result.scalars().unique().all()), total


# ---------------------------------------------------------------------------
# Run CRUD (one row per poll-and-process cycle)
# ---------------------------------------------------------------------------

async def create_run() -> Run:
    async with async_session() as s:
        run = Run(started_at=_utcnow())
        s.add(run)
        await s.commit()
        await s.refresh(run)
        return run


async def update_run(run_id: int, **values) -> None:
    if not values:
        return
    async with async_session() as s:
        await s.execute(update(Run.__table__).where(Run.__table__.c.id == run_id).values(**values))
        await s.commit()


async def get_run(run_id: int) -> Run | None:
    async with async_session() as s:
        return await s.get(Run, run_id)


async def get_latest_run() -> Run | None:
    async with async_session() as s:
        result = await s.execute(select(Run).order_by(Run.id.desc()).limit(1))
        return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Conversation + Message CRUD
# ---------------------------------------------------------------------------

async def create_conversation(
    title: str = "New Chat", research_interest: str | None = None
) -> Conversation:
    async with async_session() as s:
        c = Conversation(title=title, research_interest=research_interest)
        s.add(c)
        await s.commit()
        await s.refresh(c)
        logger.info("Created conversation id=%d title='%s'", c.id, title)
        return c


async def get_conversations() -> list[Conversation]:
    async with async_session() as s:
        result = await s.execute(
            select(Conversation).order_by(Conversation.updated_at.desc())
        )
        return list(result.scalars().all())


async def get_conversation(conversation_id: int) -> Conversation | None:
    async with async_session() as s:
        return await s.get(Conversation, conversation_id)


async def get_conversation_messages(conversation_id: int) -> list[Message]:
    async with async_session() as s:
        result = await s.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc())
        )
        return list(result.scalars().all())


async def add_message(
    conversation_id: int,
    role: MessageRole,
    content: str | None = None,
    tool_calls: list | None = None,
    tool_call_id: str | None = None,
) -> Message:
    async with async_session() as s:
        msg = Message(
            conversation_id=conversation_id,
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
        )
        s.add(msg)
        conv = await s.get(Conversation, conversation_id)
        if conv:
            conv.updated_at = datetime.now(timezone.utc)
        await s.commit()
        await s.refresh(msg)
        return msg


async def delete_conversation(conversation_id: int) -> None:
    async with async_session() as s:
        await s.execute(delete(Conversation).where(Conversation.id == conversation_id))
        await s.commit()
        logger.info("Deleted conversation id=%d", conversation_id)


async def update_conversation_research_interest(
    conversation_id: int, research_interest: str
) -> None:
    async with async_session() as s:
        conv = await s.get(Conversation, conversation_id)
        if conv:
            conv.research_interest = research_interest
            await s.commit()


async def update_conversation_title(conversation_id: int, title: str) -> None:
    async with async_session() as s:
        conv = await s.get(Conversation, conversation_id)
        if conv:
            conv.title = title
            await s.commit()
