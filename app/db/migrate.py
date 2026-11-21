"""Recorded, ordered schema steps: an empty database and the databases of earlier versions all
reach the current schema by the same four steps.

The schema_version table holds one row per applied step; a database without it is at version
0. Each step looks at what it finds (a missing column, a missing table, rows in the old form)
and changes only that, and it runs in a transaction of its own that first claims the step's
row, so two processes starting at once apply every step exactly once.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import Text, func, inspect, literal_column, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from app import clock, ids
from app.db.models import (
    Analysis,
    Base,
    Paper,
    PaperCategory,
    PaperEvent,
    PaperStatus,
    PaperText,
    PaperVersion,
    Run,
    RunFeed,
    SchemaVersion,
    SeededCategory,
)

logger = logging.getLogger(__name__)

LATEST = 4

_ADVISORY_LOCK_KEY = 727401
_EARLIEST = datetime.min.replace(tzinfo=timezone.utc)

_papers = Paper.__table__
_analyses = Analysis.__table__
_links = PaperCategory.__table__
_version_rows = PaperVersion.__table__
_event_rows = PaperEvent.__table__
_text_rows = PaperText.__table__
_step_rows = SchemaVersion.__table__


def _time_type(conn) -> str:
    return "TIMESTAMP WITH TIME ZONE" if conn.dialect.name == "postgresql" else "DATETIME"


def _insert(conn, table):
    """An INSERT that can be told to do nothing on a conflict, in the database's own dialect."""
    if conn.dialect.name == "postgresql":
        return postgresql.insert(table)
    return sqlite.insert(table)


def _columns(conn, table: str) -> set[str]:
    # A fresh inspector each time: it caches, and the steps change what it would report.
    insp = inspect(conn)
    if not insp.has_table(table):
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def _has_table(conn, table: str) -> bool:
    return inspect(conn).has_table(table)


def _add_column(conn, table: str, name: str, ddl: str) -> None:
    if name not in _columns(conn, table):
        conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _drop_column(conn, table: str, name: str) -> None:
    if name in _columns(conn, table):
        conn.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN {name}")


def _create_missing(conn, *tables) -> None:
    Base.metadata.create_all(conn, tables=[t.__table__ for t in tables])


# ---------------------------------------------------------------------------
# Step 1: one row per paper
# ---------------------------------------------------------------------------

def _versions(conn) -> None:
    if not _has_table(conn, "papers"):
        Base.metadata.create_all(conn)
        return

    legacy = "version" not in _columns(conn, "papers")
    _add_column(conn, "papers", "version", "INTEGER NOT NULL DEFAULT 1")
    _add_column(conn, "papers", "attempts", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "papers", "lease_token", "VARCHAR")
    _add_column(conn, "papers", "lease_at", _time_type(conn))
    _add_column(conn, "analyses", "paper_version", "INTEGER NOT NULL DEFAULT 1")
    _create_missing(conn, PaperCategory, Run, SeededCategory, PaperVersion)

    if not legacy:
        # The version is already in its own column; an analysis is taken to describe it.
        conn.execute(
            _analyses.update().values(
                paper_version=select(_papers.c.version)
                .where(_papers.c.id == _analyses.c.paper_id)
                .scalar_subquery()
            )
        )
        return
    _merge_legacy_rows(conn)


def _merge_legacy_rows(conn) -> None:
    """Fold the rows of one paper (one row per announced version) into one row.

    The row with the highest version is kept; every version the group held becomes a
    paper_versions row of it, and the paper is done only when its analysis describes the
    version it now has.
    """
    rows = conn.execute(
        select(
            _papers.c.id,
            _papers.c.arxiv_id,
            _papers.c.category_id,
            _papers.c.title,
            _papers.c.abstract,
            _papers.c.authors,
            _papers.c.categories,
            _papers.c.created_at,
        ).order_by(_papers.c.id)
    ).all()
    analysis_of: dict[int, tuple] = {}
    for a_id, paper_id, created_at in conn.execute(
        select(_analyses.c.id, _analyses.c.paper_id, _analyses.c.created_at).order_by(_analyses.c.id)
    ):
        analysis_of[paper_id] = (created_at or _EARLIEST, a_id)

    # Rows come in id order, so the groups are met in the order of their lowest row id.
    groups: dict[str, list[tuple[int, object]]] = {}
    for row in rows:
        base, version = ids.split(row.arxiv_id)
        groups.setdefault(base, []).append((version or 1, row))

    merged = 0
    for base, members in groups.items():
        keeper_version, keeper = max(
            members, key=lambda m: (m[0], m[1].id in analysis_of, m[1].id)
        )
        others = [row.id for _, row in members if row.id != keeper.id]

        # The analysis that stays: the keeper's own, else the newest of the other rows.
        kept_analysis = None
        kept_version = None
        if keeper.id in analysis_of:
            kept_analysis, kept_version = analysis_of[keeper.id][1], keeper_version
        else:
            candidates = [
                (analysis_of[row.id], version) for version, row in members if row.id in analysis_of
            ]
            if candidates:
                (_, kept_analysis), kept_version = max(candidates)
        if others:
            drop = _analyses.c.paper_id.in_(others)
            if kept_analysis is not None:
                drop = drop & (_analyses.c.id != kept_analysis)
            conn.execute(_analyses.delete().where(drop))
        if kept_analysis is not None:
            conn.execute(
                _analyses.update()
                .where(_analyses.c.id == kept_analysis)
                .values(paper_id=keeper.id, paper_version=kept_version)
            )

        # The other rows go before the keeper takes the id without suffix, which one of them
        # may hold.
        if others:
            conn.execute(_papers.delete().where(_papers.c.id.in_(others)))

        by_version: dict[int, object] = {}
        for version, row in members:
            best = by_version.get(version)
            if best is None or (row.id in analysis_of, row.id) > (best.id in analysis_of, best.id):
                by_version[version] = row
        for version in sorted(by_version):
            row = by_version[version]
            conn.execute(
                _version_rows.insert().values(
                    paper_id=keeper.id,
                    version=version,
                    title=row.title,
                    abstract=row.abstract,
                    authors=row.authors,
                    categories=row.categories,
                    comment=None,
                    updated_at=None,
                    seen_at=row.created_at,
                )
            )

        category_ids = sorted({row.category_id for _, row in members})
        stamps = [row.created_at for _, row in members if row.created_at is not None]
        values = dict(
            arxiv_id=base,
            version=keeper_version,
            category_id=category_ids[0],
            status=PaperStatus.done if kept_version == keeper_version else PaperStatus.pending,
            attempts=0,
            error_message=None,
            lease_token=None,
            lease_at=None,
        )
        if stamps:
            values["created_at"] = min(stamps)
        conn.execute(_papers.update().where(_papers.c.id == keeper.id).values(**values))
        for category_id in category_ids:
            conn.execute(
                _insert(conn, _links)
                .values(paper_id=keeper.id, category_id=category_id)
                .on_conflict_do_nothing(index_elements=["paper_id", "category_id"])
            )
        merged += len(others)
    logger.info("Older rows merged: %d papers kept, %d rows folded into them", len(groups), merged)


# ---------------------------------------------------------------------------
# Step 2: the clock's lease, new paper columns, texts out of the analyses
# ---------------------------------------------------------------------------

def _clock_texts(conn) -> None:
    time_type = _time_type(conn)
    _drop_column(conn, "papers", "lease_host")
    _drop_column(conn, "papers", "lease_mono")
    _add_column(conn, "papers", "comment", "TEXT")
    _add_column(conn, "papers", "updated_at", time_type)
    _add_column(conn, "papers", "withdrawn_at", time_type)
    _add_column(conn, "runs", "heartbeat_at", time_type)
    _create_missing(conn, PaperText)

    if "full_text" not in _columns(conn, "analyses"):
        return
    # Imported here: the text store needs the engine, which the other steps do not.
    from app.services import textstore

    full_text = literal_column("analyses.full_text", Text)
    found = conn.execute(
        select(
            _analyses.c.paper_id,
            _analyses.c.paper_version,
            _analyses.c.created_at,
            _papers.c.arxiv_id,
            full_text,
        )
        .join_from(_analyses, _papers, _papers.c.id == _analyses.c.paper_id)
        .order_by(_analyses.c.id)
    ).all()
    moved = 0
    for paper_id, version, created_at, arxiv_id, text in found:
        if text is None:
            continue
        # The file is written before the row is committed; a step that fails leaves files
        # without rows, and the next attempt writes them again.
        textstore.write(arxiv_id, version, text)
        conn.execute(
            _text_rows.insert().values(
                paper_id=paper_id,
                version=version,
                sha256=textstore.digest(text),
                chars=len(text),
                extraction="abstract" if text.startswith("[Text extraction failed") else "pdf",
                extracted_at=created_at or clock.now(),
            )
        )
        moved += 1
    conn.exec_driver_sql("ALTER TABLE analyses DROP COLUMN full_text")
    logger.info("Moved %d paper texts into the text store", moved)


# ---------------------------------------------------------------------------
# Step 3: version rows and the first timeline entry of papers stored before
# ---------------------------------------------------------------------------

def _timeline(conn) -> None:
    _create_missing(conn, PaperVersion, PaperEvent)

    has_current = (
        select(_version_rows.c.id)
        .where(_version_rows.c.paper_id == _papers.c.id, _version_rows.c.version == _papers.c.version)
        .correlate(_papers)
        .exists()
    )
    conn.execute(
        _version_rows.insert().from_select(
            ["paper_id", "version", "title", "abstract", "authors", "categories", "comment",
             "updated_at", "seen_at"],
            select(
                _papers.c.id,
                _papers.c.version,
                _papers.c.title,
                _papers.c.abstract,
                _papers.c.authors,
                _papers.c.categories,
                _papers.c.comment,
                _papers.c.updated_at,
                _papers.c.created_at,
            )
            .where(~has_current)
            .order_by(_papers.c.id),
        )
    )

    first_version = (
        select(func.min(_version_rows.c.version))
        .where(_version_rows.c.paper_id == _papers.c.id)
        .correlate(_papers)
        .scalar_subquery()
    )
    has_entry = select(_event_rows.c.id).where(_event_rows.c.paper_id == _papers.c.id).correlate(_papers).exists()
    conn.execute(
        _event_rows.insert().from_select(
            ["paper_id", "kind", "version", "feed", "at"],
            select(
                _papers.c.id,
                literal_column("'announced'"),
                first_version,
                literal_column("NULL"),
                _papers.c.created_at,
            )
            .where(~has_entry)
            .order_by(_papers.c.id),
        )
    )


# ---------------------------------------------------------------------------
# Step 4: per-feed records of a cycle
# ---------------------------------------------------------------------------

def _run_feeds(conn) -> None:
    _create_missing(conn, RunFeed)


STEPS = [
    (1, "versions", _versions),
    (2, "clock_texts", _clock_texts),
    (3, "timeline", _timeline),
    (4, "run_feeds", _run_feeds),
]


def current_version(conn) -> int:
    """The highest applied step; 0 when the database has no schema_version table."""
    if not _has_table(conn, "schema_version"):
        return 0
    return conn.execute(select(func.max(_step_rows.c.version))).scalar() or 0


def upgrade(conn) -> list[tuple[int, str]]:
    """Bring the database to LATEST and return the steps this call applied.

    `conn` is a synchronous connection outside any transaction of the caller; the function
    commits as it goes.
    """
    version = current_version(conn)
    # The read ends here: on SQLite a transaction that has read and then writes can fail at
    # once when another process wrote in between.
    conn.rollback()
    if version == LATEST:
        return []
    if version > LATEST:
        from app.cli import CliError

        raise CliError(f"database schema {version} is newer than this version ({LATEST})", 2)

    conn.execute(CreateTable(_step_rows, if_not_exists=True))
    conn.commit()

    applied: list[tuple[int, str]] = []
    for number, name, step in STEPS:
        if number <= version:
            continue
        try:
            if conn.dialect.name == "postgresql":
                conn.exec_driver_sql(f"SELECT pg_advisory_xact_lock({_ADVISORY_LOCK_KEY})")
            # The claim comes first, so the transaction holds the write lock before it reads.
            claimed = conn.execute(
                _insert(conn, _step_rows)
                .values(version=number, name=name, applied_at=clock.now())
                .on_conflict_do_nothing(index_elements=["version"])
            ).rowcount
            if not claimed:
                # Another process has applied this step meanwhile.
                conn.rollback()
                continue
            step(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        applied.append((number, name))
        logger.info("Schema step %d %s applied", number, name)
    return applied
