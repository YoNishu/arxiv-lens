"""Schema creation and the in-place upgrade of a database written by an older version.

Older versions stored one row per announced version of a paper, with the version inside
arxiv_id (2401.00001v1, 2401.00001v2, ...), each row tied to a single category. The upgrade:

  * adds the new columns to papers and creates the new tables,
  * moves the version suffix of arxiv_id into version,
  * collapses the rows of one paper into the row with the highest version, which joins the
    categories of all of them and keeps its own analysis or else takes the newest one,
  * marks every paper with an analysis as done, and every other paper as pending with 0 attempts
    (so papers left failed or processing by the old code are picked up again).

It runs inside the transaction of the caller and is skipped for databases already upgraded.
"""
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import delete, insert, inspect, select, update

from app.db.models import Analysis, Base, Paper, PaperCategory, PaperStatus

logger = logging.getLogger(__name__)

_VERSIONED_ID = re.compile(r"^(?P<base>.+?)v(?P<version>\d+)$")
_ADVISORY_LOCK_KEY = 727401

_NEW_PAPER_COLUMNS = {
    "version": "INTEGER NOT NULL DEFAULT 1",
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "lease_token": "VARCHAR",
    "lease_at": {"postgresql": "TIMESTAMP WITH TIME ZONE", "default": "DATETIME"},
    "lease_host": "VARCHAR",
    "lease_mono": {"postgresql": "DOUBLE PRECISION", "default": "FLOAT"},
}


def split_arxiv_id(raw: str) -> tuple[str, int | None]:
    """'2401.00001v3' -> ('2401.00001', 3); '2401.00001' -> ('2401.00001', None)."""
    raw = (raw or "").strip()
    m = _VERSIONED_ID.match(raw)
    if m:
        return m.group("base"), int(m.group("version"))
    return raw, None


def upgrade(conn) -> None:
    """Create or upgrade the schema. `conn` is a synchronous Connection inside a transaction."""
    dialect = conn.dialect.name
    # One process at a time: two app processes may start against the same database.
    if dialect == "postgresql":
        conn.exec_driver_sql(f"SELECT pg_advisory_xact_lock({_ADVISORY_LOCK_KEY})")
    elif dialect == "sqlite":
        try:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        except Exception:  # a transaction is already open on this connection
            logger.debug("BEGIN IMMEDIATE not issued", exc_info=True)

    insp = inspect(conn)
    legacy = False
    if insp.has_table("papers"):
        existing = {c["name"] for c in insp.get_columns("papers")}
        legacy = "version" not in existing
        for name, ddl in _NEW_PAPER_COLUMNS.items():
            if name in existing:
                continue
            if isinstance(ddl, dict):
                ddl = ddl.get(dialect, ddl["default"])
            conn.exec_driver_sql(f"ALTER TABLE papers ADD COLUMN {name} {ddl}")

    Base.metadata.create_all(conn)

    if legacy:
        logger.info("Database written by an older version found: upgrading it in place")
        stats = _migrate_rows(conn)
        logger.info(
            "Upgrade complete: %(papers)d papers kept, %(merged)d duplicate rows merged, "
            "%(done)d done, %(pending)d pending", stats,
        )


def _migrate_rows(conn) -> dict:
    papers, analyses, links = Paper.__table__, Analysis.__table__, PaperCategory.__table__

    rows = conn.execute(select(papers.c.id, papers.c.arxiv_id, papers.c.category_id)).all()
    analysis_of: dict[int, tuple] = {}
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    for a_id, paper_id, created_at in conn.execute(
        select(analyses.c.id, analyses.c.paper_id, analyses.c.created_at)
    ):
        analysis_of[paper_id] = (created_at or epoch, a_id)

    groups: dict[str, list[tuple[int, int, int]]] = {}
    for paper_id, arxiv_id, category_id in rows:
        base, version = split_arxiv_id(arxiv_id)
        groups.setdefault(base, []).append((version or 1, paper_id, category_id))

    stats = {"papers": 0, "merged": 0, "done": 0, "pending": 0}
    link_rows: list[dict] = []
    for base, members in groups.items():
        # Keep the row with the highest version (an analysed row, then the newest row, on a tie).
        version, keeper, _ = max(members, key=lambda m: (m[0], m[1] in analysis_of, m[1]))
        losers = [m[1] for m in members if m[1] != keeper]
        category_ids = sorted({m[2] for m in members})

        has_analysis = keeper in analysis_of
        if not has_analysis:
            candidates = [(analysis_of[p], p) for p in losers if p in analysis_of]
            if candidates:
                (_, adopted_id), _ = max(candidates)
                conn.execute(update(analyses).where(analyses.c.id == adopted_id).values(paper_id=keeper))
                has_analysis = True
        if losers:
            conn.execute(delete(analyses).where(analyses.c.paper_id.in_(losers)))
            conn.execute(delete(papers).where(papers.c.id.in_(losers)))

        conn.execute(
            update(papers)
            .where(papers.c.id == keeper)
            .values(
                arxiv_id=base,
                version=version,
                category_id=category_ids[0],
                status=PaperStatus.done if has_analysis else PaperStatus.pending,
                attempts=0,
                error_message=None,
                lease_token=None,
                lease_at=None,
                lease_host=None,
                lease_mono=None,
            )
        )
        link_rows.extend({"paper_id": keeper, "category_id": c} for c in category_ids)
        stats["papers"] += 1
        stats["merged"] += len(losers)
        stats["done" if has_analysis else "pending"] += 1

    for start in range(0, len(link_rows), 500):
        conn.execute(insert(links), link_rows[start:start + 500])
    return stats
