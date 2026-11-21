"""Version rows and the timeline of a paper.

add_version and record write inside the caller's transaction, so an entry is stored exactly
when the change it tells of is. The timeline kinds are announced, replaced, withdrawn,
analysed, failed and retried; the id of an entry is its place in the timeline.
"""
from sqlalchemy import insert, select

from app import clock
from app.db.database import async_session
from app.db.models import PaperEvent, PaperVersion

_versions = PaperVersion.__table__
_events = PaperEvent.__table__


async def add_version(s, paper_id: int, values: dict, seen_at) -> None:
    """Keep what one version of the paper said; `values` holds the version and its fields."""
    await s.execute(
        insert(_versions).values(
            paper_id=paper_id,
            version=values["version"],
            title=values["title"],
            abstract=values["abstract"],
            authors=values["authors"],
            categories=values["categories"],
            comment=values.get("comment"),
            updated_at=values.get("updated_at"),
            seen_at=seen_at,
        )
    )


async def record(s, paper_id: int, kind: str, version: int, feed: str | None = None) -> None:
    """Add one timeline entry, stamped with the clock's now."""
    await s.execute(
        insert(_events).values(paper_id=paper_id, kind=kind, version=version, feed=feed, at=clock.now())
    )


async def versions(paper_id: int) -> list[PaperVersion]:
    async with async_session() as s:
        result = await s.execute(
            select(PaperVersion).where(PaperVersion.paper_id == paper_id).order_by(PaperVersion.version.asc())
        )
        return list(result.scalars().all())


async def timeline(paper_id: int) -> list[PaperEvent]:
    async with async_session() as s:
        result = await s.execute(
            select(PaperEvent).where(PaperEvent.paper_id == paper_id).order_by(PaperEvent.id.asc())
        )
        return list(result.scalars().all())
