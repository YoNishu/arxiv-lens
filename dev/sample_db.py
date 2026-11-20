"""Writes a small database the way ArxivLens stores things, for trying changes against existing data.

Usage, from the project root:   DATABASE_URL=<url> python dev/sample_db.py

The URL needs a driver that is installed: asyncpg for PostgreSQL, or aiosqlite for a SQLite file
(sqlite+aiosqlite:///sample.db), which requirements.txt does not include.

It creates the tables and adds two categories (cs.AI, cs.LG) and six paper rows, the kinds of rows a database
that has been polling for a while ends up with:

  2406.00001v1   cs.AI   done, analysis "A1"      the same paper stored twice, once per version
  2406.00001v2   cs.LG   done, analysis "A2"
  2406.00002v1   cs.AI   done, analysis "B1"      an old version that was analysed ...
  2406.00002v3   cs.LG   pending                  ... and a newer one that was not
  2406.00003v1   cs.LG   failed
  2406.00004v2   cs.LG   processing               left behind by a process that stopped

PDF links point at dev/standin.py on its default port.
"""
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so "python dev/sample_db.py" finds the app package

from app.db.database import async_session, engine
from app.db.models import Analysis, Base, Category, Paper

ROWS = [
    ("2406.00001v1", 1, "done", "A1"),
    ("2406.00001v2", 2, "done", "A2"),
    ("2406.00002v1", 1, "done", "B1"),
    ("2406.00002v3", 2, "pending", None),
    ("2406.00003v1", 2, "failed", None),
    ("2406.00004v2", 2, "processing", None),
]


async def main() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_session() as session:
        session.add_all([Category(name="AI", arxiv_code="cs.AI"), Category(name="LG", arxiv_code="cs.LG")])
        await session.flush()
        for number, (arxiv_id, category_id, status, summary) in enumerate(ROWS, start=1):
            session.add(Paper(
                category_id=category_id,
                arxiv_id=arxiv_id,
                title=f"Paper {arxiv_id[:10]}",
                authors=["A. Author"],
                abstract=f"Abstract of {arxiv_id}.",
                pdf_url=f"http://127.0.0.1:9100/pdf/{arxiv_id}",
                published_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
                categories=["cs.AI", "cs.LG"],
                status=status,
                error_message="analysis failed" if status == "failed" else None,
            ))
            if summary:
                session.add(Analysis(
                    paper_id=number,
                    full_text="Full text.",
                    summary=summary,
                    key_contributions=["Contribution"],
                    methodology="Method.",
                    keywords=["kw"],
                    novelty_score=5,
                ))
        await session.commit()
    await engine.dispose()
    print(f"wrote {len(ROWS)} papers")


if __name__ == "__main__":
    asyncio.run(main())
