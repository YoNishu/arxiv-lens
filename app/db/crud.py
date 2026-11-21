"""Database operations of ingestion, processing, the cycle records and the chat.

Each function opens its own session and commits once. A function that changes a paper writes
the timeline entry of that change in the same transaction. Functions that read before they
write start with the write, so that on SQLite the transaction holds the write lock before it
reads anything. Every ordering ends in a unique column.
"""
import dataclasses
import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, cast, delete, func, insert, or_, select, update, String as SAString
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import clock
from app.config import settings
from app.db import history
from app.db.database import IS_SQLITE, async_session
from app.db.models import (
    Analysis,
    Category,
    Conversation,
    Message,
    MessageRole,
    Paper,
    PaperCategory,
    PaperEvent,
    PaperStatus,
    PaperText,
    PaperVersion,
    Run,
    RunFeed,
    SeededCategory,
)
from app.services import textstore

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
_analyses = Analysis.__table__
_texts = PaperText.__table__
_runs = Run.__table__

# The fields of a paper row that describe one version, as the API gives them.
_PAPER_FIELDS = (
    "arxiv_id", "version", "title", "authors", "abstract", "pdf_url", "published_at", "updated_at",
    "categories", "journal_ref", "doi", "comment",
)
# What a replacement overwrites: everything of the new version except the first submission.
_REPLACED_FIELDS = (
    "version", "title", "authors", "abstract", "pdf_url", "categories", "journal_ref", "doi",
    "comment", "updated_at",
)

_LEASE_CLEARED = {"lease_token": None, "lease_at": None}


def _chunks(items: list, size: int = _CHUNK):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _as_dict(values) -> dict:
    if dataclasses.is_dataclass(values):
        return dataclasses.asdict(values)
    return dict(values)


def _says_withdrawn(comment: str | None) -> bool:
    # The same test as feed_poller.is_withdrawal; the data layer does not import the poller.
    return "withdrawn" in (comment or "").lower()


# ---------------------------------------------------------------------------
# Category CRUD
# ---------------------------------------------------------------------------

async def get_all_categories() -> list[Category]:
    async with async_session() as s:
        result = await s.execute(select(Category).order_by(Category.added_at.desc(), Category.id.desc()))
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


async def delete_category(category_id: int) -> tuple[int, int] | None:
    """Delete a category, and with it only the papers that no other category lists.

    Returns (papers deleted, papers kept that the category listed), or None for an unknown id.
    """
    async with async_session() as s:
        # Dropping the memberships first takes the write lock before anything is read.
        listed = [
            row[0]
            for row in await s.execute(
                delete(_links).where(_links.c.category_id == category_id).returning(_links.c.paper_id)
            )
        ]
        known = (await s.execute(select(Category.id).where(Category.id == category_id))).first()
        if known is None:
            await s.rollback()
            return None

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

        # The rest belonged to this category alone. Their text files are named now and removed
        # once the rows are gone for good.
        orphans = select(_papers.c.id).where(_papers.c.category_id == category_id)
        orphan_ids = [row[0] for row in await s.execute(orphans.order_by(_papers.c.id))]
        files = (
            await s.execute(
                select(_papers.c.arxiv_id, _texts.c.version)
                .join_from(_texts, _papers, _papers.c.id == _texts.c.paper_id)
                .where(_papers.c.category_id == category_id)
                .order_by(_papers.c.id)
            )
        ).all()
        for table in (_analyses, _texts, PaperVersion.__table__, PaperEvent.__table__):
            await s.execute(delete(table).where(table.c.paper_id.in_(orphans)))
        await s.execute(delete(_papers).where(_papers.c.category_id == category_id))
        await s.execute(delete(Category.__table__).where(Category.__table__.c.id == category_id))
        await s.commit()
    for arxiv_id, version in files:
        textstore.remove(arxiv_id, version)
    gone = set(orphan_ids)
    kept = len({paper_id for paper_id in listed if paper_id not in gone})
    logger.info("Deleted category id=%d: %d papers deleted, %d kept", category_id, len(gone), kept)
    return len(gone), kept


async def seed_categories(candidates: list[dict]) -> list[str]:
    """Create the seed categories this database has not been offered before.

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
                    .values(arxiv_code=code, seeded_at=clock.now())
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
                        added_at=clock.now(),
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


async def store_new_papers(rows: list, category_id: int, feed: str) -> int:
    """Store papers that are not stored yet and list all of them under the category.

    Each row holds the fields of one API entry. Safe against another cycle or process storing
    the same paper at the same time: a paper that is already there is left alone and only gains
    the category. An inserted paper gets its version row and its first timeline entries. Returns
    how many were inserted.
    """
    if not rows:
        return 0
    inserted = 0
    async with async_session() as s:
        paper_ids: list[int] = []
        for raw in rows:
            fields = _as_dict(raw)
            p = {name: fields.get(name) for name in _PAPER_FIELDS if name in fields}
            now = clock.now()
            withdrawn = _says_withdrawn(p.get("comment"))
            values = {"category_id": category_id, "status": PaperStatus.pending, "attempts": 0,
                      "created_at": now, "withdrawn_at": now if withdrawn else None, **p}
            row = (
                await s.execute(
                    _dialect_insert(_papers)
                    .values(**values)
                    .on_conflict_do_nothing(index_elements=["arxiv_id"])
                    .returning(_papers.c.id)
                )
            ).first()
            if row:
                paper_id = row[0]
                inserted += 1
                paper_ids.append(paper_id)
                version = values.get("version") or 1
                await history.add_version(s, paper_id, {**values, "version": version}, now)
                await history.record(s, paper_id, "announced", version, feed)
                if withdrawn:
                    await history.record(s, paper_id, "withdrawn", version, feed)
            else:
                existing = (
                    await s.execute(select(_papers.c.id).where(_papers.c.arxiv_id == p["arxiv_id"]))
                ).first()
                if existing:
                    paper_ids.append(existing[0])
        await _link(s, paper_ids, category_id)
        await s.commit()
    logger.info("Stored %d new papers (of %d offered)", inserted, len(rows))
    return inserted


async def apply_replacement(paper_id: int, values, feed: str) -> bool:
    """Move a stored paper to a newer version and make it pending for that version.

    All fields of the new version are taken over; the first submission time and the time the
    paper was first stored stay. The analysis and the text of the older version stay until the
    next successful try replaces them. Returns False when the paper is not older than `values`.
    """
    fields = _as_dict(values)
    new = {name: fields.get(name) for name in _REPLACED_FIELDS}
    withdrawn = _says_withdrawn(new.get("comment"))
    async with async_session() as s:
        # withdrawn_at is left out of this UPDATE so that RETURNING hands back its old value.
        before = (
            await s.execute(
                update(_papers)
                .where(_papers.c.id == paper_id, _papers.c.version < new["version"])
                .values(
                    **new,
                    status=PaperStatus.pending,
                    attempts=0,
                    error_message=None,
                    **_LEASE_CLEARED,
                )
                .returning(_papers.c.withdrawn_at)
            )
        ).first()
        if before is None:
            await s.rollback()
            return False
        now = clock.now()
        was_withdrawn = before[0] is not None
        if withdrawn and not was_withdrawn:
            await s.execute(update(_papers).where(_papers.c.id == paper_id).values(withdrawn_at=now))
        elif was_withdrawn and not withdrawn:
            await s.execute(update(_papers).where(_papers.c.id == paper_id).values(withdrawn_at=None))
        await history.add_version(s, paper_id, new, now)
        await history.record(s, paper_id, "replaced", new["version"], feed)
        if withdrawn and not was_withdrawn:
            await history.record(s, paper_id, "withdrawn", new["version"], feed)
        await s.commit()
        return True


# ---------------------------------------------------------------------------
# Paper CRUD — processing (claims, leases, attempts)
# ---------------------------------------------------------------------------

def _lease_expired():
    """True for a lease that was not renewed for PROCESSING_LEASE_SECONDS by the clock.

    Strict: a lease stamped at instant T is alive at T and for the whole lease period after it.
    """
    cutoff = clock.now() - timedelta(seconds=settings.processing_lease_seconds)
    return or_(_papers.c.lease_at.is_(None), _papers.c.lease_at < cutoff)


def _claimable():
    """What a cycle may take: a pending paper, a failed one with tries left, or a paper in work
    whose lease ran out (its worker is gone; such a paper is taken over whatever its count).
    A withdrawn paper is never taken."""
    return and_(
        _papers.c.withdrawn_at.is_(None),
        or_(
            _papers.c.status == PaperStatus.pending,
            and_(_papers.c.status == PaperStatus.failed, _papers.c.attempts < MAX_ATTEMPTS),
            and_(_papers.c.status == PaperStatus.processing, _lease_expired()),
        ),
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
    moment, or None when it is not there to be taken (done, exhausted, withdrawn, or another
    worker's).
    """
    async with async_session() as s:
        result = await s.execute(
            update(_papers)
            .where(_papers.c.id == paper_id, _claimable())
            .values(
                status=PaperStatus.processing,
                attempts=_papers.c.attempts + 1,
                lease_token=token,
                lease_at=clock.now(),
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
            .values(lease_at=clock.now())
        )
        await s.commit()
        return result.rowcount == 1


async def complete_paper(paper_id: int, token: str, version: int, analysis: dict) -> bool:
    """Store the analysis of `version` and mark the paper done.

    Only when this worker still holds the paper and the paper is still at the version the
    worker read at its claim: an analysis of a version that has since been replaced is refused.
    """
    async with async_session() as s:
        result = await s.execute(
            update(_papers)
            .where(
                _papers.c.id == paper_id,
                _papers.c.lease_token == token,
                _papers.c.status == PaperStatus.processing,
                _papers.c.version == version,
            )
            .values(status=PaperStatus.done, error_message=None, **_LEASE_CLEARED)
        )
        if result.rowcount != 1:
            await s.rollback()
            return False
        await s.execute(delete(_analyses).where(_analyses.c.paper_id == paper_id))
        await s.execute(
            insert(_analyses).values(
                paper_id=paper_id,
                paper_version=version,
                summary=analysis["summary"],
                key_contributions=analysis["key_contributions"],
                methodology=analysis["methodology"],
                limitations=analysis.get("limitations"),
                keywords=analysis["keywords"],
                novelty_score=analysis["novelty_score"],
                created_at=clock.now(),
            )
        )
        await history.record(s, paper_id, "analysed", version)
        await s.commit()
        logger.info(
            "Created analysis for paper %d v%d: score=%d, keywords=%s",
            paper_id, version, analysis["novelty_score"], analysis["keywords"],
        )
        return True


async def fail_paper(paper_id: int, token: str, error_message: str) -> bool:
    async with async_session() as s:
        row = (
            await s.execute(
                update(_papers)
                .where(
                    _papers.c.id == paper_id,
                    _papers.c.lease_token == token,
                    _papers.c.status == PaperStatus.processing,
                )
                .values(status=PaperStatus.failed, error_message=error_message, **_LEASE_CLEARED)
                .returning(_papers.c.version)
            )
        ).first()
        if row is None:
            await s.rollback()
            return False
        await history.record(s, paper_id, "failed", row[0])
        await s.commit()
        return True


async def retry_paper(paper_id: int) -> str:
    """Make a failed paper pending again with 0 attempts. Returns ok, not_found or not_failed."""
    async with async_session() as s:
        row = (
            await s.execute(
                update(_papers)
                .where(_papers.c.id == paper_id, _papers.c.status == PaperStatus.failed)
                .values(status=PaperStatus.pending, attempts=0, error_message=None, **_LEASE_CLEARED)
                .returning(_papers.c.version)
            )
        ).first()
        if row is not None:
            await history.record(s, paper_id, "retried", row[0])
            await s.commit()
            return "ok"
        await s.rollback()
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
    # An unknown status is the caller's mistake, told before anything is asked of the database.
    wanted = PaperStatus(status) if status else None
    async with async_session() as s:
        stmt = select(Paper).options(selectinload(Paper.category), selectinload(Paper.analysis))
        if category_id:
            stmt = stmt.where(_in_category(category_id))
        if wanted is not None:
            stmt = stmt.where(Paper.status == wanted)
        stmt = stmt.order_by(Paper.published_at.desc(), Paper.id.desc())
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


async def get_paper_by_arxiv_id(arxiv_id: str) -> Paper | None:
    async with async_session() as s:
        result = await s.execute(
            select(Paper)
            .options(selectinload(Paper.category), selectinload(Paper.analysis))
            .where(Paper.arxiv_id == arxiv_id)
        )
        return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Analysis CRUD
# ---------------------------------------------------------------------------

async def get_analysis_by_paper(paper_id: int) -> Analysis | None:
    async with async_session() as s:
        result = await s.execute(select(Analysis).where(Analysis.paper_id == paper_id))
        return result.scalar_one_or_none()


async def get_last_analysis_time() -> datetime | None:
    """When the newest analysis was stored, or None before the first one."""
    async with async_session() as s:
        return (await s.execute(select(func.max(Analysis.created_at)))).scalar()


async def get_daily_digest() -> list[Paper]:
    async with async_session() as s:
        cutoff = clock.now() - timedelta(hours=24)
        result = await s.execute(
            select(Paper)
            .join(Paper.analysis)
            .options(selectinload(Paper.analysis), selectinload(Paper.category))
            .where(Analysis.created_at >= cutoff)
            .order_by(Analysis.novelty_score.desc(), Analysis.created_at.desc(), Paper.id.desc())
        )
        papers = list(result.scalars().all())
        # Fallback: if no papers in last 24h, return the latest 20
        if not papers:
            result = await s.execute(
                select(Paper)
                .join(Paper.analysis)
                .options(selectinload(Paper.analysis), selectinload(Paper.category))
                .order_by(Analysis.created_at.desc(), Analysis.id.desc())
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
            .order_by(Analysis.novelty_score.desc(), Paper.id.desc())
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
            stmt = stmt.order_by(Analysis.novelty_score.desc(), Paper.published_at.desc(), Paper.id.desc())
        elif sort == "oldest":
            stmt = stmt.order_by(Paper.published_at.asc(), Paper.id.asc())
        else:  # newest
            stmt = stmt.order_by(Paper.published_at.desc(), Paper.id.desc())

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
        now = clock.now()
        run = Run(started_at=now, heartbeat_at=now)
        s.add(run)
        await s.commit()
        await s.refresh(run)
        return run


async def update_run(run_id: int, **values) -> None:
    """Change the run's row; every call is also a sign of life of the cycle."""
    async with async_session() as s:
        await s.execute(
            update(_runs).where(_runs.c.id == run_id).values({**values, "heartbeat_at": clock.now()})
        )
        await s.commit()


async def add_run_feed(
    run_id: int,
    feed: str,
    listed: int,
    new_papers: int,
    updated: int,
    skipped: int,
    error_message: str | None,
) -> None:
    async with async_session() as s:
        await s.execute(
            insert(RunFeed.__table__).values(
                run_id=run_id,
                feed=feed,
                listed=listed,
                new_papers=new_papers,
                updated=updated,
                skipped=skipped,
                error_message=error_message,
            )
        )
        await s.commit()


async def close_abandoned_runs() -> int:
    """Close the open runs of processes that are gone; returns how many were closed.

    A live cycle stamps heartbeat_at after every feed and every paper, so only a run silent
    for longer than the lease is closed, at its last sign of life.
    """
    last_sign = func.coalesce(_runs.c.heartbeat_at, _runs.c.started_at)
    cutoff = clock.now() - timedelta(seconds=settings.processing_lease_seconds)
    async with async_session() as s:
        result = await s.execute(
            update(_runs)
            .where(_runs.c.finished_at.is_(None), last_sign < cutoff)
            .values(
                finished_at=last_sign,
                error_message="Cycle did not finish",
            )
        )
        await s.commit()
        closed = result.rowcount or 0
    if closed:
        logger.info("Closed %d runs left open by a process that is gone", closed)
    return closed


async def get_run(run_id: int) -> Run | None:
    async with async_session() as s:
        result = await s.execute(select(Run).options(selectinload(Run.feeds)).where(Run.id == run_id))
        return result.scalar_one_or_none()


async def get_latest_run() -> Run | None:
    async with async_session() as s:
        result = await s.execute(
            select(Run).options(selectinload(Run.feeds)).order_by(Run.id.desc()).limit(1)
        )
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
            select(Conversation).order_by(Conversation.updated_at.desc(), Conversation.id.desc())
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
            .order_by(Message.created_at.asc(), Message.id.asc())
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
            conv.updated_at = clock.now()
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
