"""Stage 1: one row per stored paper, read from the database in one transaction.

papers.jsonl holds the rows, ascending arxiv_id: what the tracker stored for a paper, its feeds, the
day it was announced, its state from the durable facts (an analysis of the current version, an error
text), and the keywords, novelty and text hash of the current version when it was analysed. Every
later stage works from these rows and from the paper texts; none of them opens the database.
"""
from datetime import datetime, timezone

from app import clock
from app.insights import work

STAGE_VERSION = 1


async def read() -> list[dict]:
    """The facts of every stored paper, ascending arxiv_id, all from one state of the database."""
    from sqlalchemy import select

    from app.db import database
    from app.db.models import Analysis, Category, Paper, PaperCategory, PaperText, PaperVersion

    await database.open_database()
    async with database.engine.connect() as conn:
        if conn.dialect.name == "sqlite":
            # The sqlite3 driver opens a transaction only before a write; without this BEGIN every
            # SELECT below would see the database as it is at that moment.
            await conn.exec_driver_sql("BEGIN")
        else:
            conn = await conn.execution_options(isolation_level="REPEATABLE READ")
        try:
            papers = (await conn.execute(
                select(
                    Paper.id, Paper.arxiv_id, Paper.version, Paper.title, Paper.authors, Paper.abstract,
                    Paper.categories, Paper.published_at, Paper.updated_at, Paper.created_at,
                    Paper.withdrawn_at, Paper.error_message,
                ).order_by(Paper.id)
            )).all()
            feeds = (await conn.execute(
                select(PaperCategory.paper_id, Category.arxiv_code)
                .join(Category, Category.id == PaperCategory.category_id)
                .order_by(PaperCategory.paper_id, Category.arxiv_code)
            )).all()
            analyses = (await conn.execute(
                select(Analysis.paper_id, Analysis.paper_version, Analysis.keywords, Analysis.novelty_score)
                .order_by(Analysis.paper_id)
            )).all()
            texts = (await conn.execute(
                select(PaperText.paper_id, PaperText.version, PaperText.sha256, PaperText.chars)
                .order_by(PaperText.paper_id)
            )).all()
            versions = (await conn.execute(
                select(PaperVersion.paper_id, PaperVersion.version, PaperVersion.title, PaperVersion.abstract)
                .order_by(PaperVersion.paper_id, PaperVersion.version)
            )).all()
        finally:
            await conn.rollback()

    feeds_of: dict[int, list[str]] = {}
    for paper_id, code in feeds:
        feeds_of.setdefault(paper_id, []).append(code)
    analysis_of = {
        paper_id: {"paper_version": paper_version, "keywords": keywords, "novelty_score": novelty}
        for paper_id, paper_version, keywords, novelty in analyses
    }
    text_of = {
        paper_id: {"version": version, "sha256": sha256, "chars": chars}
        for paper_id, version, sha256, chars in texts
    }
    first_of: dict[int, dict] = {}
    for paper_id, _version, title, abstract in versions:
        # Rows come by ascending version, so the first one seen is the first stored version.
        if paper_id not in first_of:
            first_of[paper_id] = {"title": title, "abstract": abstract}

    result = []
    for row in papers:
        result.append({
            "arxiv_id": row.arxiv_id,
            "version": row.version,
            "title": row.title,
            "authors": row.authors,
            "abstract": row.abstract,
            "categories": row.categories,
            "published_at": row.published_at,
            "updated_at": row.updated_at,
            "created_at": row.created_at,
            "withdrawn_at": row.withdrawn_at,
            "error_message": row.error_message,
            # Sorted here, not by the database, so that codes compare by code point on every backend.
            "feeds": sorted(feeds_of.get(row.id, [])),
            "analysis": analysis_of.get(row.id),
            "text": text_of.get(row.id),
            "first": first_of.get(row.id),
        })
    result.sort(key=lambda facts: facts["arxiv_id"])
    return result


def _utc_day(dt: datetime) -> str:
    # A time without a zone is UTC: that is how the database hands stored times back.
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return "%04d-%02d-%02d" % (dt.year, dt.month, dt.day)


def project(facts: dict) -> dict:
    """The row of papers.jsonl for one paper."""
    work.count("snapshot.rows")
    version = facts["version"]
    analysis = facts["analysis"]
    text = facts["text"]
    first = facts["first"]
    if analysis is not None and analysis["paper_version"] == version:
        state = "analysed"
    elif facts["error_message"] is not None:
        state = "failed"
    else:
        state = "pending"
    analysed = state == "analysed"
    # A text of another version describes other words than the row's title and abstract.
    current_text = analysed and text is not None and text["version"] == version
    return {
        "arxiv_id": facts["arxiv_id"],
        "version": version,
        "title": facts["title"],
        "authors": list(facts["authors"]),
        "abstract": facts["abstract"],
        "categories": list(facts["categories"]),
        "feeds": sorted(facts["feeds"]),
        "published": clock.format_instant(facts["published_at"]),
        "updated": None if facts["updated_at"] is None else clock.format_instant(facts["updated_at"]),
        "announced": _utc_day(facts["created_at"]),
        "state": state,
        "withdrawn": facts["withdrawn_at"] is not None,
        "keywords": list(analysis["keywords"]) if analysed else None,
        "novelty": analysis["novelty_score"] if analysed else None,
        "text_sha256": text["sha256"] if current_text else None,
        "text_chars": text["chars"] if current_text else None,
        "first_title": facts["title"] if first is None else first["title"],
        "first_abstract": facts["abstract"] if first is None else first["abstract"],
    }


def run(ctx) -> dict[str, int]:
    from app.db import database

    rows = [project(facts) for facts in database.run(read())]
    ctx.write_jsonl("papers.jsonl", rows)
    ctx.papers = rows
    return {"papers": len(rows)}
